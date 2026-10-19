"""只模拟网络与 QQ，验证消息、异步生命周期及业务失败闭环。"""
from __future__ import annotations

import asyncio
import base64
import io
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
nonebot.init()
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException
from src.plugins import agent_core as core
from src.plugins.agent_core import conversation_service as conv, llm_service as llm, tts_service as tts
from src.plugins.agent_core.errors import DrawPipelineError, LLMProviderError, TTSGenerationError
from src.plugins.agent_core.history_recorder import TaskProgressTracker, current_task_id, task_scope
from src.plugins.agent_core.message_pipeline import Intent, parse_message, should_reply


def event(text="继续", *, uid=12345, at=True, segments=None):
    msg = Message(segments) if segments is not None else Message(text)
    ev = GroupMessageEvent.model_validate({
        "time": int(time.time()), "self_id": 1412773476, "post_type": "message", "message_type": "group",
        "sub_type": "normal", "message_id": 700, "group_id": 10001, "user_id": uid,
        "raw_message": str(msg), "message": msg, "to_me": at, "font": 0, "sender": {"user_id": uid},
    })
    return ev


def parsed(ev, **kwargs):
    return parse_message(ev, character=kwargs.get("character", "amiya"),
                         last_interaction=kwargs.get("last"), now=100,
                         detect_draw=llm._auto_detect_draw_intent, detect_formats=core.doc_service.detect_requested_formats)


def wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0\0" * 2400)
    return out.getvalue()


class MessageTests(unittest.TestCase):
    def test_multi_at_and_reply_at(self):
        p = parsed(event(at=False, segments=[MessageSegment.reply(12), MessageSegment.at(23456),
                                            MessageSegment.text("画这个差分图"), MessageSegment.at(1412773476)]))
        self.assertTrue(p.is_at_me)
        self.assertFalse(p.has_other_at)
        self.assertEqual(p.clean_text, "画这个差分图")

    def test_explicit_other_at_blocks_continuous_chat(self):
        p = parsed(event(at=False, segments=[MessageSegment.at(23456), MessageSegment.text("阿米娅继续")]), last=90)
        self.assertFalse(should_reply(p, global_active=True, is_private=False, group_allowed=True))

    def test_wake_and_window_boundaries(self):
        self.assertTrue(parsed(event("达妮娅在吗", at=False), character="dania").is_name_wake)
        self.assertFalse(parsed(event("xamiyay", at=False)).is_name_wake)
        self.assertTrue(parsed(event(at=False), last=40).in_continuous_chat)
        self.assertFalse(parsed(event(at=False), last=39.99).in_continuous_chat)
        self.assertFalse(parsed(event(at=False)).in_continuous_chat)

    def test_intent_uses_input_only(self):
        p = parsed(event("解释这个"))
        p.reply_context = "画一张阿米娅的立绘，并输出docx"
        self.assertEqual(p.intent, Intent.CHAT)
        self.assertEqual(parsed(event("画一张阿米娅")).intent, Intent.TEXT_TO_IMAGE)
        self.assertEqual(parsed(event("生成差分图", segments=[MessageSegment.text("生成差分图"), MessageSegment.image("x")])).intent, Intent.IMAGE_TO_IMAGE)
        self.assertEqual(parsed(event("输出docx文件")).intent, Intent.DOCUMENT)
        self.assertEqual(parsed(event("/status")).intent, Intent.CONTROL)
        self.assertEqual(parsed(event("/drawing")).intent, Intent.CONTROL)
        self.assertEqual(parsed(event("/")).intent, Intent.CONTROL)
        self.assertEqual(parsed(event("解释差分方程")).intent, Intent.CHAT)

    def test_trigger_policy(self):
        p = parsed(event())
        self.assertFalse(should_reply(p, global_active=False, is_private=True, group_allowed=True))
        self.assertFalse(should_reply(p, global_active=True, is_private=False, group_allowed=False))
        self.assertTrue(should_reply(p, global_active=True, is_private=True, group_allowed=False))


class ProgressTests(unittest.TestCase):
    def test_terminal_tasks_cannot_resurrect(self):
        tracker = TaskProgressTracker()
        tracker.update("a", status="pending", kind="image")
        tracker.update("a", status="running", percent=15)
        tracker.update("a", status="error", percent=100)
        tracker.update("a", status="running", percent=75)
        self.assertEqual(tracker.tasks["a"]["status"], "error")
        self.assertEqual(tracker.tasks["a"]["kind"], "image")

    def test_task_scope_is_reset(self):
        with task_scope("a"):
            self.assertEqual(current_task_id.get(), "a")
            with task_scope("b"):
                self.assertEqual(current_task_id.get(), "b")
            self.assertEqual(current_task_id.get(), "a")
        self.assertIsNone(current_task_id.get())

    def test_progress_only_updates_bound_request(self):
        tracker = TaskProgressTracker()
        tracker.update("a", status="running")
        tracker.update("b", status="running")
        with patch.object(core.recorder, "progress_tracker", tracker), task_scope("a"):
            llm._report_llm_progress("image", "图片已生成", 100, status="done")
        self.assertEqual(tracker.tasks["a"]["status"], "running")
        self.assertEqual(tracker.tasks["b"]["percent"], 0)

    def test_services_and_routes_remain_singletons(self):
        services = core.services
        self.assertIs(core.llm_service, services.llm)
        self.assertIs(core.document_writer, services.writer)
        before = list(nonebot.get_app().routes)
        core.controller_api.setup(services)
        self.assertEqual(nonebot.get_app().routes, before)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = conv.ConversationStore(Path(self.temp.name) / "context.db")
        self.tracker = TaskProgressTracker()
        self.bot = SimpleNamespace(send=AsyncMock(return_value={"message_id": 9}))
        self.fake_state = SimpleNamespace(global_active=True, active_character="amiya", active_groups=["10001"], voice_enabled=False)
        self.stack = []
        for item in (patch.object(conv, "store", self.store), patch.object(core, "state", self.fake_state),
                     patch.object(core.recorder, "progress_tracker", self.tracker),
                     patch.object(core, "_resolve_reply_and_forward_context", AsyncMock(return_value=("", []))),
                     patch.object(core, "_resolve_input_files", AsyncMock(return_value=[]))):
            item.start()
            self.addCleanup(item.stop)
        core._cooldown.clear()
        core._active_chat_window.clear()

    def status(self):
        return next(iter(self.tracker.tasks.values()))["status"]

    async def test_unknown_exception_has_reply_and_error_state(self):
        with patch.object(core, "_dispatch_reply", AsyncMock(side_effect=AttributeError("NoneType"))):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.status(), "error")
        self.assertTrue(self.bot.send.await_args)
        self.assertIsNone(conv.current())
        self.assertIsNone(current_task_id.get())

    async def test_empty_llm_has_reply_and_error_state(self):
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="")):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.status(), "error")
        self.assertIn("模型", str(self.bot.send.await_args.args[1]))

    async def test_voice_failure_sends_original_text_and_saves_context(self):
        self.fake_state.voice_enabled = True
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="博士，今天辛苦了。")), patch.object(tts, "synthesize", AsyncMock(side_effect=TTSGenerationError())):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.status(), "done")
        self.assertEqual(self.bot.send.await_args.args[1], "博士，今天辛苦了。")
        self.assertEqual(len(self.store.begin("12345").history), 1)

    async def test_voice_delivery_failure_sends_text(self):
        self.fake_state.voice_enabled = True
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="原始回答。")), patch.object(tts, "synthesize", AsyncMock(return_value="base64://abc")), patch.object(core, "_safe_send_voice", AsyncMock(return_value=False)):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.bot.send.await_args.args[1], "原始回答。")
        self.assertEqual(self.status(), "done")

    async def test_cancellation_releases_context_and_finishes_task(self):
        entered = asyncio.Event()
        async def slow(**kwargs):
            entered.set()
            await asyncio.sleep(60)
        with patch.object(core, "_dispatch_reply", slow):
            task = asyncio.create_task(core.handle_agent_message(self.bot, event()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.status(), "error")
        self.assertFalse(self.store._locks)
        self.assertIsNone(conv.current())

    async def test_nonebot_control_exception_is_not_user_error(self):
        with patch.object(core, "_dispatch_reply", AsyncMock(side_effect=FinishedException)):
            with self.assertRaises(FinishedException):
                await core.handle_agent_message(self.bot, event())
        self.bot.send.assert_not_awaited()

    async def test_done_happens_after_delivery(self):
        observed = []
        async def send(*args, **kwargs):
            observed.append(self.status())
            return {"message_id": 1}
        self.bot.send = send
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="回答")):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(observed, ["running"])
        self.assertEqual(self.status(), "done")

    async def test_send_failure_cannot_be_done(self):
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="回答")), patch.object(core, "_safe_send", AsyncMock(return_value=False)):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.status(), "error")

    async def test_draw_no_image_cannot_send_success_text(self):
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="我已经画好了")):
            await core.handle_agent_message(self.bot, event("画一张阿米娅"))
        self.assertEqual(self.status(), "error")
        self.assertNotIn("我已经画好了", str(self.bot.send.await_args_list))

    async def test_unaddressed_group_message_not_saved(self):
        await core.handle_agent_message(self.bot, event("普通群消息", at=False))
        self.assertFalse(self.tracker.tasks)
        self.assertFalse(self.store.path.exists())


class TTSTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tts._failures = 0
        tts._open_until = 0
        tts._TTS_MPS_LOCK = asyncio.Lock()

    def test_cleaning_preserves_emphasis_but_removes_noise(self):
        out = tts._clean("**你好**（微笑（轻轻））😄\n```python\nsecret()\n```\n[博士](https://a.cn) *抱住* ![x](https://x.cn/a.png)")
        self.assertIn("你好", out)
        self.assertIn("博士", out)
        for bad in ("微笑", "轻轻", "secret", "https", "抱住", "😄", "*", "`"):
            self.assertNotIn(bad, out)
        self.assertLessEqual(len(tts._smart_trim_for_tts("很长的句子，" * 200)), 150)

    async def test_total_budget_includes_lock_wait(self):
        async with tts._TTS_MPS_LOCK:
            started = time.monotonic()
            with self.assertRaises(TTSGenerationError):
                await tts.synthesize("你好", timeout=0.02)
            self.assertLess(time.monotonic() - started, 0.2)

    async def test_five_failures_open_circuit(self):
        for _ in range(tts.TTS_CIRCUIT_FAILURES):
            with patch.object(tts, "resolve_voice_role", side_effect=ValueError("未安装")):
                with self.assertRaises(TTSGenerationError):
                    await tts.synthesize("你好")
        self.assertGreater(tts._open_until, time.monotonic())

    async def test_invalid_silk_raises(self):
        response = httpx.Response(200, content=wav(), request=httpx.Request("POST", "http://localhost"))
        delivery = {"text": "你好", "params": {}, "meta": {}}
        saved = {"send_bytes": b"invalid", "wav_bytes": wav(), "is_silk": False, "meta": {"wav_path": "test"}}
        with patch.object(tts, "resolve_voice_role", return_value="test"), patch.object(tts, "_load_role_refs", return_value={}), patch.object(tts, "_select_voice_delivery", AsyncMock(return_value=delivery)), patch("httpx.AsyncClient.post", AsyncMock(return_value=response)), patch.object(tts, "_save_and_convert_audio", return_value=saved):
            with self.assertRaises(TTSGenerationError):
                await tts.synthesize("你好")

    async def test_ai_director_can_pick_any_candidate(self):
        candidates = [{"name": "default", "audio": "/tmp/a", "text": "你好"}, {"name": "hf_010", "audio": "/tmp/b", "text": "你好"}]
        decision = {"ref_name": "hf_010", "decision_source": "ai:test"}
        with patch.object(tts, "_voice_candidates", return_value=candidates), patch.object(tts, "select_delivery", AsyncMock(return_value=decision)):
            result = await tts._select_voice_delivery("非常开心！", "test", {})
        self.assertEqual(result["meta"]["reference"], "hf_010")

    async def test_ai_failure_cannot_silently_choose_neutral(self):
        candidates = [{"name": "default", "audio": "/tmp/a", "text": "你好"}]
        with patch.object(tts, "_voice_candidates", return_value=candidates), patch.object(tts, "select_delivery", AsyncMock(side_effect=TTSGenerationError())):
            with self.assertRaises(TTSGenerationError):
                await tts._select_voice_delivery("非常开心！", "test", {})


class DrawTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        llm._draw_failures = 0
        llm._draw_open_until = 0

    async def test_empty_image_result_is_typed_error(self):
        with patch.object(llm, "_generate_image_with_qwen_or_wan", AsyncMock(return_value=None)):
            with self.assertRaises(DrawPipelineError):
                await llm.generate_image_with_qwen_or_wan(api_key="test", user_prompt="画图")

    def test_different_pose_is_not_multi_character(self):
        self.assertFalse(llm._is_multi_character_scene("生成达妮娅的不同姿势差分图", has_ref_image=True))
        self.assertTrue(llm._reference_refinement_contamination("生成达妮娅的不同姿势差分图", "达妮娅是单人角色", "双人同框：左侧角色达妮娅，右侧角色菲比"))

    async def test_refusal_is_not_retried_without_input(self):
        generator = AsyncMock(side_effect=DrawPipelineError())
        with patch.object(llm, "_dashscope_key", return_value="test"), patch.object(llm, "extract_character_visual_traits", AsyncMock(return_value="原图特征")), patch.object(llm, "generate_image_with_qwen_or_wan", generator):
            with self.assertRaises(DrawPipelineError):
                await llm.chat_operator("amiya", "生成差分图", is_draw=True, image_b64_list=["data:image/png;base64,abc"])
        self.assertEqual(generator.await_count, 1)
        self.assertEqual(generator.await_args.kwargs["image_b64_list"], ["data:image/png;base64,abc"])

    async def test_provider_empty_reply_raises(self):
        response = httpx.Response(200, json={"choices": []}, request=httpx.Request("POST", "https://test"))
        with patch("httpx.AsyncClient.post", AsyncMock(return_value=response)):
            with self.assertRaises(LLMProviderError):
                await llm._post_chat_completions(api_key="test", base_url="https://test", model="test", system="", user="你好", max_tokens=10)


if __name__ == "__main__":
    unittest.main()
