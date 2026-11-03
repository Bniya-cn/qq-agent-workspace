"""模拟模型接口，验证游戏画风、完整需求与参考图来源的实际请求。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot

nonebot.init()
from src.plugins.agent_core import llm_service as llm
from src.plugins.agent_core.errors import DrawPipelineError


class ImagePromptTests(unittest.IsolatedAsyncioTestCase):
    def test_natural_draw_requests_route_in_private_and_group(self):
        from bot.tests.test_agent_pipeline import event, private_event, parsed
        from src.plugins.agent_core.message_pipeline import Intent
        from src.plugins.agent_core import local_rag_service

        for text in (
            "我想看男管理员和庄方宜接吻的图，你给我生成",
            "我想看博士娶阿米娅的图",
            "生成男管理员和庄方宜接吻的图",
            "生成阿米娅在办公室工作的图",
            "达妮娅穿婚纱的图，焦距50mm，f1.8,1/200s快门，iso400",
            "生成达妮娅穿婚纱的图，焦距50mm，f1.8,1/200s快门，iso400",
            "生成一张达妮娅穿婚纱的图，焦距50mm，f1.8,1/200s快门，iso400",
        ):
            for make_event in (private_event, event):
                with self.subTest(text=text, context=make_event.__name__):
                    result = parsed(make_event(text))
                    self.assertEqual(result.intent, Intent.TEXT_TO_IMAGE)
                    self.assertFalse(local_rag_service.should_use_fast_engine(text, is_draw=True))
        for text in ("生成图片的模型有哪些", "这张图是谁", "怎么生成男管理员的图", "不要生成男管理员的图"):
            self.assertFalse(llm._auto_detect_draw_intent(text), text)

    async def render_payload(self, *, through_chat=False, **kwargs):
        payloads = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, *, json, headers):
                payloads.append(json)
                class Response:
                    status_code = 200

                    def json(self):
                        return {"output": {"choices": [{"message": {"content": [{"image": "https://example.test/result.png"}]}}]}}
                return Response()

        with patch("httpx.AsyncClient", lambda **kw: Client()), patch.object(llm, "_report_llm_progress"):
            if through_chat:
                await llm.chat_operator("amiya", kwargs["user_prompt"], is_draw=True)
            else:
                await llm._generate_image_with_qwen_or_wan(api_key="test", **kwargs)
        self.assertEqual(len(payloads), 1)
        return payloads[0]

    async def test_both_character_references_reach_deepseek_and_qwen_in_order(self):
        refs = {"管理员（男）": "data:image/png;base64,bWFsZQ==", "庄方宜": "data:image/png;base64,emh1YW5n"}
        caller = AsyncMock(return_value="管理员（男）与庄方宜靠近接吻，分别保留两张立绘中的发型、配饰和服装，在完整的室内场景中互动。")
        with patch.object(llm, "_dashscope_key", return_value="test"), patch.object(llm, "_normalize_ref_file_to_data_uri", side_effect=lambda path: refs[path.stem]) as loader, patch.object(llm, "_build_chat_candidates", return_value=[("test", "https://example.test", "deepseek-flash")]), patch.object(llm, "_post_chat_completions", caller):
            payload = await self.render_payload(through_chat=True, user_prompt="/draw 男管理员和庄方宜接吻的图")
        self.assertEqual([call.args[0].stem for call in loader.call_args_list], list(refs))
        self.assertEqual(caller.await_count, 1)
        deepseek_content = caller.await_args.kwargs["messages_override"][1]["content"]
        self.assertEqual([item["image_url"]["url"] for item in deepseek_content if "image_url" in item], list(refs.values()))
        qwen_content = payload["input"]["messages"][0]["content"]
        self.assertEqual([item["image"] for item in qwen_content if "image" in item], list(refs.values()))
        for text in (deepseek_content[0]["text"], qwen_content[-1]["text"]):
            self.assertIn("参考图1只对应「管理员（男）」", text)
            self.assertIn("参考图2只对应「庄方宜」", text)
        self.assertIn("参考图必须用来锁定", qwen_content[-1]["text"])
        self.assertIn("分类属于《明日方舟：终末地》", qwen_content[-1]["text"])

    async def test_missing_character_reference_stops_before_generation(self):
        renderer = AsyncMock()
        with patch.object(llm, "_dashscope_key", return_value="test"), patch.object(llm, "_normalize_ref_file_to_data_uri", side_effect=["male-reference", None]), patch.object(llm, "generate_image_with_qwen_or_wan", renderer):
            with self.assertRaisesRegex(DrawPipelineError, "庄方宜"):
                await llm.chat_operator("amiya", "/draw 男管理员和庄方宜接吻的图", is_draw=True)
        renderer.assert_not_awaited()

    async def test_story_references_include_card_character_without_forcing_live_person(self):
        raw = "画一张男管理员拿庄方宜打算包养他的钱去抽通行证结果只抽到了一堆夜刀的图"
        caller = AsyncMock(return_value="管理员打开抽卡结果，卡片上重复出现夜刀的形象，庄方宜在旁边看到结果露出惊讶的表情，保留两人服装特征。")
        with patch.object(llm, "_dashscope_key", return_value="test"), patch.object(llm, "_normalize_ref_file_to_data_uri", side_effect=lambda path: "data:image/png;base64,dGVzdA=="), patch.object(llm, "_build_chat_candidates", return_value=[("test", "https://example.test", "deepseek-flash")]), patch.object(llm, "_post_chat_completions", caller):
            payload = await self.render_payload(through_chat=True, user_prompt=raw)
        self.assertIn("卡片、照片、屏幕", caller.await_args.kwargs["system"])
        self.assertIn("不代表该角色必须真人入镜", caller.await_args.kwargs["system"])
        content = payload["input"]["messages"][0]["content"]
        self.assertEqual(len([item for item in content if "image" in item]), 3)
        self.assertIn("参考图3只对应「夜刀」", content[-1]["text"])
        self.assertIn(raw, content[-1]["text"])
        self.assertNotIn("企鹅", content[-1]["text"])

    async def test_missing_single_character_reference_does_not_become_text_only(self):
        renderer = AsyncMock()
        with patch.object(llm, "_dashscope_key", return_value="test"), patch.object(llm, "_load_local_character_refs", return_value=[]), patch.object(llm, "generate_image_with_qwen_or_wan", renderer):
            with self.assertRaisesRegex(DrawPipelineError, "庄方宜"):
                await llm.chat_operator("amiya", "/draw 庄方宜在工作", is_draw=True)
        renderer.assert_not_awaited()

    def test_endfield_original_files_are_selected_for_each_character(self):
        for name, filename in (("管理员（男）", "管理员（男）.png"), ("庄方宜", "庄方宜.png")):
            with self.subTest(name=name), patch.object(llm, "_normalize_ref_file_to_data_uri", side_effect=lambda path: str(path)):
                refs = llm._load_local_character_refs(name, max_refs=1)
                self.assertEqual(len(refs), 1)
                self.assertEqual(Path(refs[0]).parent.name, "Endfield")
                self.assertEqual(Path(refs[0]).name, filename)

    def test_identical_display_names_use_unique_asset_aliases(self):
        with self.assertRaises(DrawPipelineError):
            llm._image_character_matches("BlueArchive 星野（武装）")
        for asset_id in ("10098", "10099"):
            hits = llm._image_character_matches(f"BlueArchive 星野（武装） {asset_id}")
            self.assertEqual(len(hits), 1)
            self.assertTrue(hits[0]["path"].name.startswith(asset_id + "_"))
        hits = llm._image_character_matches("BlueArchive 星野（武装）10098和BlueArchive 星野（武装）10099")
        self.assertEqual([hit["path"].name.split('_', 1)[0] for hit in hits], ["10098", "10099"])

    def test_gender_and_outfits_do_not_collapse_to_base_character(self):
        for name, expected in (("女管理员", "管理员（女）"), ("管理员（男）", "管理员（男）"), ("日富美（泳装）", "日富美（泳装）"), ("日富美（赛车）", "日富美（赛车）")):
            with self.subTest(name=name):
                matches = llm._image_character_matches(name + "与阿米娅合影")
                self.assertEqual(matches[0]["name"], expected)
                self.assertEqual(matches[0]["path"].stem.split('_', 1)[-1] if matches[0]["game"] == "BlueArchive" else matches[0]["path"].stem, expected)
        normalized = llm._normalize_prompt_with_aliases("女管理员与庄方宜合影")
        self.assertIn("管理员（女）", normalized)
        self.assertNotIn("管理员（男）", normalized)

    def test_first_character_controls_game_style_in_both_orders(self):
        for prompt, game in (
            ("阿米娅与阿露一起喝茶", "明日方舟"),
            ("阿露与阿米娅一起喝茶", "蔚蓝档案"),
            ("菲比与三月七并肩散步", "鸣潮"),
            ("三月七与菲比并肩散步", "崩坏：星穹铁道"),
            ("陈千语与阿米娅并肩走路", "明日方舟：终末地"),
            ("原神胡桃与阿米娅喝茶", "原神"),
        ):
            with self.subTest(prompt=prompt):
                self.assertIn(f"分类属于《{game}》", llm._image_style_hint(prompt))
        self.assertEqual(llm._get_matched_character_keys("菲比啾比与阿米娅合影")[0], "菲比啾比")
        self.assertEqual(llm._get_matched_character_keys("大肥鱼（DeepSeek）独照"), ["大肥鱼"])

    def test_cross_game_names_require_disambiguation(self):
        with self.assertRaisesRegex(DrawPipelineError, "多个角色或换装"):
            llm._image_style_hint("胡桃与阿米娅喝茶")
        self.assertIn("分类属于《蔚蓝档案》", llm._image_style_hint("Kurumi与阿米娅喝茶"))

    async def test_full_request_and_realistic_style_reach_qwen_21(self):
        raw = "阿米娅的写实照片，用水彩画风表现。" + "保留画面细节。" * 260 + "最后要求：右下角写完整标题。"
        payload = await self.render_payload(user_prompt=raw, visual_traits="阿米娅在室内工作的自然场景。", image_model="qwen-image-2.1-pro")
        text = payload["input"]["messages"][0]["content"][-1]["text"]
        self.assertIn(raw, text)
        self.assertNotIn("interacting romantically", text)
        self.assertNotIn("Wuthering Waves / Blue Archive", text)
        self.assertFalse(payload["parameters"]["prompt_extend"])
        self.assertFalse(payload["parameters"]["enable_thinking"])
        self.assertNotIn("negative_prompt", payload["parameters"])

    async def test_identity_reference_is_dynamic_and_qwen_30_parameters(self):
        payload = await self.render_payload(user_prompt="阿露在办公室工作", image_b64_list=["data:image/png;base64,cmVm"], character_reference_only=True, image_model="qwen-image-3.0-pro")
        content = payload["input"]["messages"][0]["content"]
        self.assertIn("参考图必须用来锁定", content[-1]["text"])
        self.assertIn("不要复刻原图的姿势", content[-1]["text"])
        self.assertNotIn("阿米娅", content[-1]["text"])
        self.assertEqual(content[0]["image"], "data:image/png;base64,cmVm")
        self.assertTrue(payload["parameters"]["prompt_extend"])
        self.assertTrue(payload["parameters"]["enable_thinking"])
        self.assertIn("negative_prompt", payload["parameters"])

    async def test_chat_operator_preserves_reference_source(self):
        for supplied, expected_role in (([], "identity"), (["user-image"], "edit")):
            with self.subTest(role=expected_role):
                extractor = AsyncMock(return_value="参考图外观与本次画面。")
                renderer = AsyncMock(return_value="image result")
                with patch.object(llm, "_dashscope_key", return_value="test"), patch.object(llm, "_load_local_character_refs", return_value=["local-image"]) as loader, patch.object(llm, "extract_character_visual_traits", extractor), patch.object(llm, "generate_image_with_qwen_or_wan", renderer):
                    await llm.chat_operator("amiya", "画阿米娅工作" if not supplied else "编辑阿米娅的差分图", is_draw=True, image_b64_list=supplied)
                self.assertEqual(extractor.await_args.kwargs["reference_role"], expected_role)
                self.assertEqual(renderer.await_args.kwargs["reference_role"], expected_role)
                self.assertEqual(renderer.await_args.kwargs["image_b64_list"], supplied or ["local-image"])
                if supplied:
                    loader.assert_not_called()

    async def test_reference_prompt_uses_one_call_and_does_not_pollute_cache(self):
        raw = "阿米娅在办公室工作。" + "保留用户约束。" * 80
        result = "阿米娅坐在办公室桌边认真工作，手中握笔，窗外光线柔和，保留她的兔耳与蓝色眼睛。"
        caller = AsyncMock(return_value=result)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            cache.write_text(json.dumps({"阿米娅": "历史场景"}, ensure_ascii=False))
            before = cache.read_bytes()
            with patch.object(llm, "_REFS_CACHE_FILE", cache), patch.object(llm, "_build_chat_candidates", return_value=[("test", "https://example.test", "deepseek-flash")]), patch.object(llm, "_post_chat_completions", caller), patch.object(llm, "_report_llm_progress"):
                out = await llm.extract_character_visual_traits("unused", "image-uri", raw, reference_role="identity")
            self.assertEqual(out, result)
            self.assertEqual(cache.read_bytes(), before)
        self.assertEqual(caller.await_count, 1)
        message = caller.await_args.kwargs["messages_override"][1]["content"]
        self.assertIn(raw, message[0]["text"])
        self.assertEqual(message[1]["image_url"]["url"], "image-uri")
        self.assertEqual(caller.await_args.kwargs["timeout"].read, 120.0)
        self.assertEqual(caller.await_args.kwargs["timeout"].write, 120.0)
        self.assertTrue(caller.await_args.kwargs["enable_thinking"])

    async def test_wedding_prompt_keeps_the_second_person(self):
        raw = "生成一张博士娶阿米娅的图"
        result = "婚礼现场，博士身着黑色西装站在左侧，阿米娅身着白色婚纱站在右侧，两人相对，阿米娅只出现一次。"
        caller = AsyncMock(return_value=result)
        with patch.object(llm, "_build_chat_candidates", return_value=[("test", "https://example.test", "deepseek-flash")]), patch.object(llm, "_post_chat_completions", caller), patch.object(llm, "_report_llm_progress"):
            out = await llm.extract_character_visual_traits("unused", "image-uri", raw, reference_role="identity")
        self.assertEqual(out, result)
        self.assertIn("禁止把参考角色复制成两个人", caller.await_args.kwargs["system"])
        self.assertIn("85mm及以上要从头部拍到腰部或膝上", caller.await_args.kwargs["system"])
        self.assertTrue(caller.await_args.kwargs["enable_thinking"])

    async def test_focal_length_changes_framing_while_reference_image_is_sent(self):
        common = dict(visual_traits="浅景深全身婚纱照。", character_reference_only=True, image_b64_list=["data:image/png;base64,cmVm"], image_model="qwen-image-2.1-pro")
        payload50 = await self.render_payload(user_prompt="生成一张达妮娅穿婚纱的图，焦距50mm，f1.8,1/200s快门，iso400", **common)
        payload85 = await self.render_payload(user_prompt="生成一张达妮娅穿婚纱的图，焦距85mm，f1.8,1/200s快门，iso400", **common)
        text50 = payload50["input"]["messages"][0]["content"][-1]["text"]
        text85 = payload85["input"]["messages"][0]["content"][-1]["text"]
        self.assertIn("50mm标准镜头", text50)
        self.assertIn("人物大半身和周围一段环境同时清楚可读", text50)
        self.assertIn("85mm中长焦", text85)
        self.assertIn("从头部拍到腰部或膝上", text85)
        self.assertIn("不保留参考图的原套服装", text85)
        self.assertIn("必须仍是参考图里的本人", text85)
        self.assertIn("原作立绘", text85)
        self.assertIn("参考图的姿势无效", text85)
        self.assertNotEqual(text50, text85)
        for payload in (payload50, payload85):
            content = payload["input"]["messages"][0]["content"]
            self.assertEqual(content[0]["image"], "data:image/png;base64,cmVm")
            self.assertIn("参考图必须用来锁定", content[-1]["text"])
            self.assertFalse(payload["parameters"]["prompt_extend"])

    def test_any_new_clothing_replaces_the_reference_outfit(self):
        for text in ("生成一张达妮娅穿黑色风衣的图", "把阿米娅换成白衬衫", "阿米娅穿校服"):
            with self.subTest(text=text):
                self.assertIn("原套服装", llm._costume_override_hint(text))
        self.assertEqual(llm._costume_override_hint("阿露在办公室工作"), "")
        self.assertEqual(llm._costume_override_hint("生成一张达妮娅的图"), "")

    async def test_text_prompt_preserves_complete_request(self):
        raw = "阿露与阿米娅在图书馆，采用水彩画风。" + "不要省略指定细节。" * 80
        caller = AsyncMock(return_value="两位角色在图书馆阅读，保持各自的外观与服饰，采用用户要求的水彩画风，纸张纹理自然。")
        with patch.object(llm, "_build_chat_candidates", return_value=[("test", "https://example.test", "deepseek-flash")]), patch.object(llm, "_post_chat_completions", caller), patch.object(llm, "_report_llm_progress"):
            await llm.search_character_visual_sheet("unused", raw)
        request = caller.await_args.kwargs
        self.assertIn(raw, request["user"])
        self.assertIn("分类属于《蔚蓝档案》", request["user"])
        self.assertIn("优先采用用户指定的画风", request["user"])


if __name__ == "__main__":
    unittest.main()
