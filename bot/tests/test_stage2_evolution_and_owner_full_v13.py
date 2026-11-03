"""Stage 2 自主进化 + 自主回复闭环 + 主人账号 (1785381042) 全类型工作/回复与前端面板连通性综合测试套件。

严格遵守红线：
1. 所有 QQ 群聊、私聊、文件上传与空间发布均通过 FakeBot / Mock 投递器拦截验证，绝不调用真实外部 QQ 发送接口；
2. 覆盖主人账号 1785381042 的全部交互与工作类型：
   - 日常文本回复 + 群内 45s 焦点免 @ 连续追问
   - 语音合成（TTS）回复模式与情绪选音
   - 文档生成与导出投递（.docx + .pdf 真实落盘与上传链路）
   - 插画生图（TEXT_TO_IMAGE）与角色设定立绘归档
   - 时间约定自动抓取与心跳兑现（pending_promises.json）
   - 复合多步指令子目标拆解防漏（Sub-goal Checklist）与 Mac 原生动作准备
   - 发言后 60s 群友正负反馈奖惩、自动拉黑、每日复盘守则（learned_rules.json）与过期近况归档
   - 多人无引用交叉话题聚类、多模态事件入队、群热度自适应冷却节奏
   - WebUI 控制面板与后端 API 全量连通性校验
"""
from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from fastapi.testclient import TestClient
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent

import src.plugins.agent_core as core
from src.plugins.agent_core import controller_api
from src.plugins.agent_core.autonomous_agent import AutonomousAgentCoordinator
from src.plugins.agent_core.config_manager import ConfigManager
from src.plugins.agent_core.expression_learner import MaiBotSceneExpressionStore
from src.plugins.agent_core.group_chat_manager import AttentionManager, group_chat_manager
from src.plugins.agent_core.modes.small_talk import build_small_talk_system_prompt
from src.plugins.agent_core.user_memory_service import UserMemoryService

OWNER_UID = "1785381042"
TEST_GROUP_ID = "873321151"


class FakeBot:
    def __init__(self, self_id: str = "999999"):
        self.self_id = self_id
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api: str, **kwargs):
        self.calls.append((api, kwargs))
        return {"message_id": len(self.calls) + 1000, "status": "ok"}

    async def send(self, event, message, **kwargs):
        self.calls.append(("send", {"event": event, "message": message, **kwargs}))
        return {"message_id": len(self.calls) + 2000}


def _make_private_event(text: str, user_id: str = OWNER_UID, message_id: int = 801, segments=None) -> PrivateMessageEvent:
    msg = Message(segments) if segments is not None else Message(text)
    return PrivateMessageEvent(
        time=int(time.time()),
        self_id=999999,
        post_type="message",
        sub_type="friend",
        user_id=int(user_id),
        message_type="private",
        message_id=message_id,
        message=msg,
        original_message=msg,
        raw_message=str(msg),
        font=0,
        sender={"user_id": int(user_id), "nickname": "博士"},
    )


def _make_group_event(
    text: str,
    *,
    group_id: str = TEST_GROUP_ID,
    user_id: str = OWNER_UID,
    nickname: str = "博士",
    at_bot: bool = False,
    reply_to_uid: str | None = None,
    reply_to_msg_id: int | None = None,
    message_id: int = 901,
    extra_segments=None,
) -> GroupMessageEvent:
    msg = Message()
    if at_bot:
        msg.append(MessageSegment.at(999999))
        msg.append(MessageSegment.text(" " + text))
    elif text:
        msg.append(MessageSegment.text(text))
    if extra_segments:
        for seg in extra_segments:
            msg.append(seg)
    ev = GroupMessageEvent(
        time=int(time.time()),
        self_id=999999,
        post_type="message",
        sub_type="normal",
        user_id=int(user_id),
        group_id=int(group_id),
        message_type="group",
        message_id=message_id,
        message=msg,
        original_message=msg,
        raw_message=text,
        font=0,
        sender={"user_id": int(user_id), "nickname": nickname, "card": nickname},
        to_me=at_bot,
    )
    if reply_to_uid is not None:
        ev.reply = SimpleNamespace(
            message_id=reply_to_msg_id or 500,
            sender=SimpleNamespace(user_id=int(reply_to_uid), nickname=f"用户{reply_to_uid}"),
            message=Message("上一条消息"),
        )
    return ev


class Stage2EvolutionAndOwnerFullTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        core._cooldown.clear()
        core._active_chat_window.clear()
        core._AMIYA_SILENCE_UNTIL.clear()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_01_autonomous_evolution_reaction_scoring_blacklist_and_learned_rules(self) -> None:
        """验证发言后 60 秒内群友正负向反应评分、自动拉黑、防复活，以及每日复盘生成 learned_rules.json 与过期近况归档。"""
        store = MaiBotSceneExpressionStore(storage_dir=self.tmp_path / "expressions")
        inserted = store.upsert_expressions(
            TEST_GROUP_ID,
            [
                {"situation": "讨论写代码加班时", "style": "这也太顶了吧", "tone": "轻快感叹", "meme_intensity": 0.3},
                {"situation": "讨论写代码加班时", "style": "快去歇会儿别硬撑啦", "tone": "温和关心", "meme_intensity": 0.2},
            ],
        )
        self.assertEqual(inserted, 2)

        # 1. 阿米娅生成表达参考块时自动登记 60s 曝光窗口
        ref_block = store.build_expression_reference_block(TEST_GROUP_ID, "今天写代码加班好累")
        self.assertIn("这也太顶了吧", ref_block)

        # 2. 群友在 15s 内给出正向反馈（"哈哈哈哈会说多说"），表达加权强化
        t0 = time.time()
        items_before = {x.style: x.expr_id for x in store.load_group_expressions(TEST_GROUP_ID)}
        store.record_bot_expression_usage(
            TEST_GROUP_ID,
            [items_before["快去歇会儿别硬撑啦"]],
            reply_text="快去歇会儿别硬撑啦",
            now_ts=t0,
        )
        pos_ev = store.observe_group_reaction(
            TEST_GROUP_ID,
            OWNER_UID,
            "哈哈哈哈会说多说，太贴心了",
            reply_to_bot=True,
            now_ts=t0 + 12.0,
        )
        self.assertIsNotNone(pos_ev)
        self.assertEqual(pos_ev["reaction"], "positive")
        after_pos = {x.style: x for x in store.load_group_expressions(TEST_GROUP_ID)}
        self.assertGreater(after_pos["快去歇会儿别硬撑啦"].score_bias, 0.0)
        self.assertEqual(after_pos["快去歇会儿别硬撑啦"].positive_feedback, 1)

        # 3. 另一条表达被群友吐槽“太尬了别学这句人机话”，立即扣分并自动拉入黑名单
        store.record_bot_expression_usage(
            TEST_GROUP_ID,
            [items_before["这也太顶了吧"]],
            reply_text="这也太顶了吧",
            now_ts=t0 + 20.0,
        )
        neg_ev = store.observe_group_reaction(
            TEST_GROUP_ID,
            "22222222",
            "太尬了别学这句人机话",
            reply_to_bot=True,
            now_ts=t0 + 30.0,
        )
        self.assertIsNotNone(neg_ev)
        self.assertEqual(neg_ev["reaction"], "negative")
        self.assertIn("这也太顶了吧", neg_ev["blacklisted_styles"])

        # 4. 被拉黑的表达绝不再被检索选中，且重新 upsert 也不会复活
        selected_after = store.select_scene_expressions(TEST_GROUP_ID, "讨论写代码加班时")
        selected_styles = [x.style for x in selected_after]
        self.assertNotIn("这也太顶了吧", selected_styles)
        self.assertIn("快去歇会儿别硬撑啦", selected_styles)

        resurrect_count = store.upsert_expressions(
            TEST_GROUP_ID,
            [{"situation": "讨论写代码加班时", "style": "这也太顶了吧", "tone": "轻快"}],
        )
        self.assertEqual(resurrect_count, 0)

        # 5. 过期 recent_matters 自动衰减归档 + learned_rules.json 守则提炼
        mem_service = UserMemoryService(
            db_path=self.tmp_path / "profiles.sqlite3",
            mem0_dir=self.tmp_path / "mem0_qdrant",
        )
        rec = mem_service.record_interaction_sync(
            uid=OWNER_UID,
            user_text="我明天要考编译原理，我平时最喜欢喝拿铁",
            schedule_background_mem0=False,
        )
        self.assertTrue(any("考编译原理" in m for m in rec.recent_matters))
        # 模拟 10 天后复盘清理，临时的“明天要考编译原理”被自动归档，长期兴趣“拿铁”完整保留
        future_ts = time.time() + 10 * 86400.0
        prune_res = mem_service.prune_stale_recent_matters(uid=OWNER_UID, now_ts=future_ts)
        self.assertGreaterEqual(prune_res["archived_count"], 1)
        rec_after = mem_service.get_scoped_profile(OWNER_UID)
        self.assertEqual(rec_after.recent_matters, [])
        self.assertIn("拿铁", rec_after.interests)

        coord = AutonomousAgentCoordinator(
            state_path=self.tmp_path / "auto_state.json",
            promises_path=self.tmp_path / "promises.json",
            rules_path=self.tmp_path / "learned_rules.json",
        )
        with patch("src.plugins.agent_core.expression_learner.expression_store", store), patch(
            "src.plugins.agent_core.autonomous_agent.user_memory_store", mem_service
        ):
            coord.record_interaction_feedback(
                group_id=TEST_GROUP_ID,
                uid="22222222",
                feedback_type="negative_reaction",
                detail="太尬了别学这句人机话",
                blacklisted_styles=["这也太顶了吧"],
            )
            prompt_block = coord.get_learned_rules_prompt_block(group_id=TEST_GROUP_ID, uid=OWNER_UID)
            self.assertIn("这也太顶了吧", prompt_block)
            self.assertTrue((self.tmp_path / "learned_rules.json").is_file())

    async def test_02_autonomous_reply_topic_clustering_multimodal_and_adaptive_pacing(self) -> None:
        """验证无引用交叉话题聚类、纯图片/表情包多模态入队与群聊热度自适应冷却。"""
        # 1. 交叉话题聚类：A/B 聊编译原理实验，C/D 聊晚饭吃火锅，均无 [reply] 标签
        now = 10000.0
        raw_events = [
            {"message_id": "1", "user_id": "101", "nickname": "甲", "text": "编译原理的词法分析实验你们写完了吗", "timestamp": now + 1},
            {"message_id": "2", "user_id": "202", "nickname": "乙", "text": "今晚去食堂吃麻辣香锅还是海底捞火锅呀", "timestamp": now + 2},
            {"message_id": "3", "user_id": "102", "nickname": "丙", "text": "词法分析那个状态机我还在调正则表达式", "timestamp": now + 4},
            {"message_id": "4", "user_id": "203", "nickname": "丁", "text": "吃火锅吧，正好天冷想吃肥牛卷和虾滑", "timestamp": now + 5},
        ]
        threads = AttentionManager.cluster_interleaved_threads(raw_events)
        self.assertGreaterEqual(len(threads), 2)
        all_previews = " | ".join(t["topic_preview"] for t in threads)
        self.assertIn("词法分析", all_previews)
        self.assertIn("火锅", all_previews)

        # 2. 群热度自适应冷却节奏（冷清期 0.75x vs 刷屏热聊期 1.35x）
        density_cls = type(group_chat_manager.density)
        density = density_cls(min_interval_sec=8.0, max_per_minute=6)
        cold_info = density.record_incoming_activity(TEST_GROUP_ID, now=5000.0)
        self.assertEqual(cold_info["heat_level"], "cold")
        self.assertLess(cold_info["effective_min_interval_sec"], 8.0)

        for i in range(15):
            hot_info = density.record_incoming_activity(TEST_GROUP_ID, now=5010.0 + i * 2.0)
        self.assertEqual(hot_info["heat_level"], "hot")
        self.assertGreater(hot_info["effective_min_interval_sec"], 8.0)

        # 3. 纯图片/表情包多模态事件入队与批次决策上下文验证
        coord = AutonomousAgentCoordinator(
            state_path=self.tmp_path / "auto_state2.json",
            promises_path=self.tmp_path / "promises2.json",
            rules_path=self.tmp_path / "rules2.json",
        )
        coord.enqueue_group_event(
            message_id="img_99",
            group_id=TEST_GROUP_ID,
            user_id=OWNER_UID,
            nickname="博士",
            text="",
            media_summary="[分享了1张图片/表情包]",
            timestamp=6000.0,
        )
        self.assertEqual(len(coord._event_queues[TEST_GROUP_ID]), 1)
        self.assertIn("图片/表情包", coord._event_queues[TEST_GROUP_ID][0].text)

        # 4. 全局开关关闭 (global_active=False) 时，严禁后台自主入队或触发任何群聊接话（防“赛博女鬼”偷跑）
        inactive_state = SimpleNamespace(
            global_active=False,
            autonomous_group_chat_enabled=True,
            autonomous_qzone_enabled=True,
            autonomous_quiet_start_hour=23,
            autonomous_quiet_end_hour=8,
            active_character="amiya",
        )
        with patch("src.plugins.agent_core.autonomous_agent.state", inactive_state):
            coord.enqueue_group_event(
                message_id="msg_when_off",
                group_id=TEST_GROUP_ID,
                user_id=OWNER_UID,
                nickname="博士",
                text="阿米娅我不是把你关了吗",
                timestamp=6010.0,
            )
            eval_res = await coord.evaluate_group_batch(TEST_GROUP_ID, now_ts=6015.0)
            self.assertEqual(eval_res["action"], "no_action")
            self.assertEqual(eval_res["reason"], "global_inactive")
            self.assertEqual(len(coord._event_queues.get(TEST_GROUP_ID, [])), 0)

    async def test_03_owner_1785381042_all_work_and_reply_types_e2e(self) -> None:
        """使用 1785381042 账号全链路测试：文本回复、45s 焦点续聊、语音合成回复、Word/PDF 文档导出投递、插画生图、约定兑现、复合指令子目标拆解。"""
        cfg_file = self.tmp_path / "config.json"
        cfg = ConfigManager(cfg_file)
        cfg.global_active = True
        cfg.active_character = "amiya"
        cfg.small_talk_mode = True
        cfg.active_groups = [TEST_GROUP_ID]
        cfg.group_buffer_enabled = False

        coord = AutonomousAgentCoordinator(
            state_path=self.tmp_path / "owner_auto.json",
            promises_path=self.tmp_path / "owner_promises.json",
            rules_path=self.tmp_path / "owner_rules.json",
        )
        bot = FakeBot()

        with patch.object(core, "state", cfg), patch(
            "src.plugins.agent_core.autonomous_agent.autonomous_coordinator", coord
        ):
            # --- 类型 A：1785381042 群内 @ 唤醒文本回复 + 45s 焦点免 @ 连续追问 + 时间约定自动抓取 ---
            with patch.object(core.llm_service, "chat", new=AsyncMock(return_value="好呀，15分钟后我准时叫你喝水")):
                ev1 = _make_group_event(
                    "15分钟后提醒我喝水",
                    group_id=TEST_GROUP_ID,
                    user_id=OWNER_UID,
                    at_bot=True,
                    message_id=1001,
                )
                await core.handle_agent_message(bot, ev1)

            self.assertTrue(group_chat_manager.attention.is_in_focus_window(TEST_GROUP_ID, OWNER_UID))
            promises = coord.list_promises(uid=OWNER_UID, status="pending")
            self.assertEqual(len(promises), 1)
            self.assertIn("喝水", promises[0]["content"])

            # 焦点窗口内免 @ 第二句追问
            core._cooldown.clear()
            with patch.object(core.llm_service, "chat", new=AsyncMock(return_value="放心吧，我都记在备忘里啦")):
                ev2 = _make_group_event(
                    "顺便看看今天还有啥安排吗",
                    group_id=TEST_GROUP_ID,
                    user_id=OWNER_UID,
                    at_bot=False,
                    message_id=1002,
                )
                await core.handle_agent_message(bot, ev2)
            self.assertGreaterEqual(len(bot.calls), 2)

            # 触发约定心跳到期兑现（Fake 投递器拦截验证）
            due_ts = promises[0]["trigger_ts"] + 2.0
            priv_sent: list[tuple[str, str]] = []
            mac_sent: list[str] = []

            async def _fake_priv(u: str, m: str) -> bool:
                priv_sent.append((u, m))
                return True

            async def _fake_mac(item) -> dict:
                mac_sent.append(item.content)
                return {"ok": True}

            coord.configure_consumer(send_private_fn=_fake_priv, mac_actuator_fn=_fake_mac)
            fulfilled = await coord.run_promise_heartbeat_tick(now_ts=due_ts)
            self.assertEqual(len(fulfilled), 1)
            self.assertEqual(fulfilled[0]["status"], "delivered")
            self.assertEqual(len(mac_sent), 1)

            # --- 类型 B：1785381042 语音回复模式（Voice Mode + TTS 合成投递） ---
            core._cooldown.clear()
            cfg.voice_enabled = True
            fake_wav_b64 = "base64://" + base64.b64encode(b"RIFF$\x00\x00\x00WAVEfmt ").decode()
            with patch.object(
                core.llm_service, "chat", new=AsyncMock(return_value="今天辛苦啦，早点休息哦")
            ), patch.object(
                core.tts_service, "synthesize", new=AsyncMock(return_value=fake_wav_b64)
            ) as mock_tts:
                ev_voice = _make_private_event("阿米娅，跟我说句晚安", user_id=OWNER_UID, message_id=1003)
                await core.handle_agent_message(bot, ev_voice)
                mock_tts.assert_awaited_once()
            self.assertTrue(
                any(
                    "record" in str(call[1].get("message", ""))
                    for call in bot.calls
                )
            )
            cfg.voice_enabled = False

            # --- 类型 C：1785381042 文档撰写与真实 .docx / .pdf 生成上传 ---
            core._cooldown.clear()
            sample_md = (
                "# 编译原理词法分析教案\n\n"
                "## 一、 教学目标\n"
                "理解正规式与有限自动机（DFA/NFA）的等价转换。\n\n"
                "## 二、 核心步骤\n"
                "1. Thompson 构造法将正规式转为 NFA。\n"
                "2. 子集构造法将 NFA 确定化为 DFA。\n"
            )
            uploaded_files: list[str] = []

            async def _fake_send_file(b, ev, path: Path, group_id=None, user_id=None) -> bool:
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 100)
                uploaded_files.append(path.name)
                return True

            with patch.object(
                core.document_writer, "generate", new=AsyncMock(return_value=sample_md)
            ), patch.object(
                core.doc_service, "send_file_to_qq", side_effect=_fake_send_file
            ):
                ev_doc = _make_private_event(
                    "帮我写一份编译原理词法分析教案，并导出 docx 和 pdf 文件发我",
                    user_id=OWNER_UID,
                    message_id=1004,
                )
                await core.handle_agent_message(bot, ev_doc)

            self.assertTrue(any(f.endswith(".docx") for f in uploaded_files), f"未生成 docx: {uploaded_files}")
            self.assertTrue(any(f.endswith(".pdf") for f in uploaded_files), f"未生成 pdf: {uploaded_files}")

            # --- 类型 D：1785381042 插画生图（TEXT_TO_IMAGE） ---
            core._cooldown.clear()
            tiny_png = base64.b64encode(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
            ).decode()
            with patch.object(
                core, "_dispatch_reply", new=AsyncMock(return_value="给你画好啦 ![插画](https://example.com/amiya.png)")
            ), patch.object(
                core, "_render_mixed_message", new=AsyncMock(return_value=Message(MessageSegment.image(f"base64://{tiny_png}")))
            ):
                ev_draw = _make_private_event("画一张阿米娅在舰桥拉小提琴的插画", user_id=OWNER_UID, message_id=1005)
                await core.handle_agent_message(bot, ev_draw)
            self.assertTrue(
                any(
                    any(getattr(seg, "type", "") == "image" for seg in call[1].get("message", []))
                    for call in bot.calls
                    if isinstance(call[1].get("message"), Message)
                )
            )

            # --- 类型 E：1785381042 复合多步指令子目标拆解（防漏后半句） ---
            chk = coord.decompose_and_track_owner_work(
                OWNER_UID,
                "先帮我把刚才的内容整理成pdf文档，再定个20分钟后提醒我发给助教，顺便汇报一下运行状态",
            )
            self.assertIsNotNone(chk)
            steps = [g["step"] for g in chk["subgoals"]]
            self.assertIn("doc_export", steps)
            self.assertIn("promise_remind", steps)
            self.assertIn("status_inspect", steps)
            hint = coord.get_owner_work_prompt_hint(
                OWNER_UID,
                "先帮我把刚才的内容整理成pdf文档，再定个20分钟后提醒我发给助教，顺便汇报一下运行状态",
            )
            self.assertIn("主人复合指令防漏清单", hint)
            self.assertIn("实时运行自检数据", hint)

    async def test_04_webui_frontend_panel_and_api_connectivity(self) -> None:
        """验证前端控制面板静态资源与全部核心 API 路由（含新增 /api/promises、/api/evolution/*）连通性。"""
        app = nonebot.get_app()
        controller_api.setup(core.services)
        client = TestClient(app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000))

        # 1. 前端页面与静态脚本/样式连通性
        r_panel = client.get("/panel")
        self.assertEqual(r_panel.status_code, 200)
        self.assertIn("html", r_panel.text.lower())

        r_js = client.get("/panel-assets/panel.js")
        self.assertEqual(r_js.status_code, 200)
        self.assertGreater(len(r_js.text), 1000)

        r_css = client.get("/panel-assets/panel.css")
        self.assertEqual(r_css.status_code, 200)
        self.assertGreater(len(r_css.text), 500)

        # 2. 核心状态与主人画像 API
        r_status = client.get("/api/status")
        self.assertEqual(r_status.status_code, 200)
        self.assertIn("active_character", r_status.json())

        r_prof = client.get(f"/api/profiles/{OWNER_UID}")
        self.assertEqual(r_prof.status_code, 200)
        self.assertEqual(r_prof.json().get("code"), 0)
        self.assertEqual(r_prof.json()["profile"]["uid"], OWNER_UID)

        # 3. 自主行为、约定账本与进化自省 API
        r_auto = client.get("/api/autonomous/status")
        self.assertEqual(r_auto.status_code, 200)
        self.assertEqual(r_auto.json().get("code"), 0)
        self.assertIn("learned_rules_count", r_auto.json()["data"])

        r_prm = client.get(f"/api/promises?uid={OWNER_UID}")
        self.assertEqual(r_prm.status_code, 200)
        self.assertEqual(r_prm.json().get("code"), 0)

        r_evo = client.get("/api/evolution/status")
        self.assertEqual(r_evo.status_code, 200)
        self.assertEqual(r_evo.json().get("code"), 0)
        self.assertIn("learned_rules", r_evo.json()["data"])

        r_ref = client.post("/api/evolution/reflect", json={"group_id": TEST_GROUP_ID})
        self.assertEqual(r_ref.status_code, 200)
        self.assertEqual(r_ref.json().get("code"), 0)
        self.assertEqual(r_ref.json()["data"]["status"], "ok")

        r_act = client.get("/api/actuator/status")
        self.assertEqual(r_act.status_code, 200)
        self.assertEqual(r_act.json().get("code"), 0)


if __name__ == "__main__":
    unittest.main()
