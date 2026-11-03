"""只模拟网络与 QQ，验证消息、异步生命周期及业务失败闭环。"""
from __future__ import annotations

import asyncio
import base64
import io
import os
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
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException
from src.plugins import agent_core as core
from src.plugins.agent_core import config_manager as config, conversation_service as conv, llm_service as llm, tts_service as tts
from src.plugins.agent_core.errors import DrawPipelineError, LLMProviderError, TTSGenerationError
from src.plugins.agent_core.history_recorder import TaskProgressTracker, current_task_id, task_scope
from src.plugins.agent_core.message_pipeline import Intent, parse_message, should_reply, classify_amiya_interaction


def event(text="继续", *, uid=12345, at=True, segments=None):
    msg = Message(segments) if segments is not None else Message(text)
    ev = GroupMessageEvent.model_validate({
        "time": int(time.time()), "self_id": 1412773476, "post_type": "message", "message_type": "group",
        "sub_type": "normal", "message_id": 700, "group_id": 10001, "user_id": uid,
        "raw_message": str(msg), "message": msg, "to_me": at, "font": 0, "sender": {"user_id": uid},
    })
    return ev


def private_event(text="继续", *, uid=12345):
    msg = Message(text)
    return PrivateMessageEvent.model_validate({
        "time": int(time.time()), "self_id": 1412773476, "post_type": "message", "message_type": "private",
        "sub_type": "friend", "message_id": 701, "user_id": uid,
        "raw_message": str(msg), "message": msg, "to_me": True, "font": 0, "sender": {"user_id": uid},
    })


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

    def test_amiya_silence_requires_a_directed_stop_or_clear_frustration(self):
        self.assertEqual(classify_amiya_interaction("阿米娅闭嘴", is_at_me=False, is_private=False), "silence")
        self.assertEqual(classify_amiya_interaction("你烦不烦", is_at_me=True, is_private=False), "silence")
        self.assertEqual(classify_amiya_interaction("我不耐烦听阿米娅一直说", is_at_me=False, is_private=False), "silence")
        self.assertIsNone(classify_amiya_interaction("今天工作真烦", is_at_me=True, is_private=False))
        self.assertIsNone(classify_amiya_interaction("闭嘴", is_at_me=False, is_private=False))
        self.assertIsNone(classify_amiya_interaction("闭嘴", is_at_me=True, is_private=False, character="kaltsit"))
        self.assertEqual(classify_amiya_interaction("阿米娅你可以继续说话了", is_at_me=False, is_private=False), "wake")


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


class VoiceDirectorToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_setting_persists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            manager = config.ConfigManager(path)
            self.assertFalse(manager.voice_emotion_director_enabled)
            manager.voice_emotion_director_enabled = True
            await manager.save()

            reloaded = config.ConfigManager(path)

        self.assertTrue(reloaded.voice_emotion_director_enabled)

    async def test_disabled_director_uses_local_fast_path(self):
        info = {"neutral_ref": "default", "inference": {}}
        candidates = [{"name": "default", "audio": "default.wav", "text": "你好。", "tags": []}]
        fake_state = SimpleNamespace(voice_emotion_director_enabled=False, voice_speed=1.0, voice_temperature=0.78, voice_top_p=0.85)
        with patch.object(tts, "state", fake_state), patch.object(tts, "_voice_candidates", return_value=candidates), patch.object(tts, "select_delivery", new_callable=AsyncMock) as select_delivery:
            decision = await tts._select_voice_delivery("今天辛苦了。", "凯尔希", info)

        select_delivery.assert_not_awaited()
        self.assertEqual(decision["meta"]["emotion_source"], "fast_local_rule")

    async def test_enabled_director_uses_ai_path(self):
        info = {"neutral_ref": "default", "inference": {}}
        candidates = [{"name": "default", "audio": "default.wav", "text": "你好。", "tags": []}]
        fake_state = SimpleNamespace(voice_emotion_director_enabled=True, voice_speed=1.0, voice_temperature=0.78, voice_top_p=0.85)
        ai_choice = {"ref_name": "default", "decision_source": "ai:test", "reasoning": "按整段语义选择"}
        with patch.object(tts, "state", fake_state), patch.object(tts, "_voice_candidates", return_value=candidates), patch.object(tts, "select_delivery", new_callable=AsyncMock, return_value=ai_choice) as select_delivery:
            decision = await tts._select_voice_delivery("今天辛苦了。", "凯尔希", info)

        select_delivery.assert_awaited_once()
        self.assertEqual(decision["meta"]["emotion_source"], "ai:test")


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
                     patch.object(core.local_rag_service.small_talk.user_memory_store, "record_interaction_sync"),
                     patch.object(core.local_rag_service.small_talk.user_memory_store, "get_fast_profile_prompt_block", return_value=""),
                     patch.object(core, "_resolve_reply_and_forward_context", AsyncMock(return_value=("", []))),
                     patch.object(core, "_resolve_input_files", AsyncMock(return_value=[]))):
            item.start()
            self.addCleanup(item.stop)
        core._cooldown.clear()
        core._active_chat_window.clear()
        core._AMIYA_SILENCE_UNTIL.clear()
        core._voice_reply_lock = asyncio.Lock()

    def status(self):
        return next(iter(self.tracker.tasks.values()))["status"]

    async def test_unknown_exception_has_reply_and_error_state(self):
        # Regression v8: 群聊异常静默保护不刷屏；私聊异常采用生活化自然兜底，绝不输出机械报错词
        with patch.object(core, "_dispatch_reply", AsyncMock(side_effect=AttributeError("NoneType"))):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.status(), "error")
        self.bot.send.assert_not_awaited()
        self.assertIsNone(conv.current())
        self.assertIsNone(current_task_id.get())

        core._cooldown.clear()
        with patch.object(core, "_dispatch_reply", AsyncMock(side_effect=AttributeError("NoneType"))):
            await core.handle_agent_message(self.bot, private_event())
        self.assertTrue(self.bot.send.await_args)
        sent_text = str(self.bot.send.await_args.args[1])
        for robotic_word in ("处理请求", "稍后重试", "异常", "超时"):
            self.assertNotIn(robotic_word, sent_text)

    async def test_empty_llm_has_reply_and_error_state(self):
        # Regression v8: 群聊空返回静默不打扰；私聊自然接茬兜底
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="")):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.status(), "error")
        self.bot.send.assert_not_awaited()

        core._cooldown.clear()
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="")):
            await core.handle_agent_message(self.bot, private_event())
        sent_text = str(self.bot.send.await_args.args[1])
        self.assertIn("刚刚说的什么", sent_text)
        for robotic_word in ("处理请求", "稍后重试", "异常", "超时"):
            self.assertNotIn(robotic_word, sent_text)

    async def test_voice_failure_is_silent_and_releases_gate(self):
        self.fake_state.voice_enabled = True
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="今天辛苦了。")), patch.object(tts, "synthesize", AsyncMock(side_effect=TTSGenerationError())):
            await core.handle_agent_message(self.bot, event())
        self.assertEqual(self.status(), "error")
        self.bot.send.assert_not_awaited()
        self.assertFalse(core._voice_reply_lock.locked())
        self.assertEqual(len(self.store.begin("12345").history), 0)

    async def test_voice_delivery_failure_in_private_never_sends_text(self):
        self.fake_state.voice_enabled = True
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="原始回答。")), patch.object(tts, "synthesize", AsyncMock(return_value="base64://abc")), patch.object(core, "_safe_send_voice", AsyncMock(return_value=False)):
            await core.handle_agent_message(self.bot, private_event())
        self.bot.send.assert_not_awaited()
        self.assertEqual(self.status(), "error")
        self.assertFalse(core._voice_reply_lock.locked())

    async def test_voice_busy_records_messages_without_queueing_replies(self):
        self.fake_state.voice_enabled = True
        started, finish = asyncio.Event(), asyncio.Event()

        async def synth(*args, **kwargs):
            started.set()
            await finish.wait()
            return "base64://abc"

        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="今天辛苦了。")), patch.object(tts, "synthesize", AsyncMock(side_effect=synth)) as synthesis, patch.object(core, "_safe_send_voice", AsyncMock(return_value=True)) as send_voice:
            owner = asyncio.create_task(core.handle_agent_message(self.bot, private_event("先说一句")))
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                await core.handle_agent_message(self.bot, private_event("忙的时候补充的内容"))
                history = self.store.begin("12345").history
                self.assertEqual(history[-1]["user"], "忙的时候补充的内容")
                self.assertEqual(history[-1]["delivery_status"], "received_only")
                with conv.use_session(self.store.begin("12345")):
                    self.assertFalse(any(m["role"] == "assistant" for m in conv.history_messages()))
            finally:
                finish.set()
                await owner
            self.assertEqual(synthesis.await_count, 1)
            self.assertEqual(send_voice.await_count, 1)
        self.bot.send.assert_not_awaited()
        self.assertFalse(core._voice_reply_lock.locked())
        self.assertEqual(len(self.store.begin("12345").history), 2)

    async def test_cancelled_voice_releases_gate(self):
        self.fake_state.voice_enabled = True
        started = asyncio.Event()

        async def synth(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="今天辛苦了。")), patch.object(tts, "synthesize", AsyncMock(side_effect=synth)):
            owner = asyncio.create_task(core.handle_agent_message(self.bot, private_event()))
            await asyncio.wait_for(started.wait(), timeout=2)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner
        self.assertFalse(core._voice_reply_lock.locked())
        self.bot.send.assert_not_awaited()

    async def test_voice_small_talk_keeps_ai_emotion_tag_and_voice_prompt(self):
        self.fake_state.small_talk_mode = True
        self.fake_state.training_mode = False
        with patch.object(llm, "chat", AsyncMock(return_value="[开心]今天也辛苦啦。")) as chat, patch.object(llm, "resolve_and_enforce_situational_state", AsyncMock(return_value="[开心]今天也辛苦啦。")):
            reply = await core._dispatch_reply(uid="12345", group_id=None, user_text="今天好开心", is_long_task=False, is_draw=False, image_b64_list=[], file_paths=[], voice_mode_only=True)
        self.assertEqual(reply, "[开心]今天也辛苦啦。")
        self.assertIn("纯语音回复模式", chat.await_args.args[0])
        self.assertEqual(chat.await_args.kwargs["max_tokens"], 55)
        self.assertFalse(core.local_rag_service.small_talk.user_memory_store.get_fast_profile_prompt_block.call_args.kwargs["trigger_background_recall"])

    async def test_fast_voice_reply_preserves_tag_in_small_talk_mode(self):
        rag = core.local_rag_service
        self.fake_state.small_talk_mode = True
        self.fake_state.training_mode = False
        prompt = "今天开心" + core._get_voice_prompt_suffix("amiya")
        with patch.object(rag, "state", self.fake_state), patch.object(rag, "ensure_bot_env"), patch.object(rag.vector_kb, "search", AsyncMock(return_value=([], 0))), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch.object(llm, "_post_chat_completions", AsyncMock(return_value="[开心]今天也辛苦啦。")) as chat:
            with conv.use_session(self.store.begin("12345")):
                reply = await rag.fast_chat_reply("amiya", prompt, voice_mode_only=True)
        self.assertEqual(reply, "[开心]今天也辛苦啦。")
        self.assertIn("纯语音回复模式", chat.await_args.kwargs["messages_override"][-1]["content"])
        self.assertFalse(rag.small_talk.user_memory_store.get_fast_profile_prompt_block.call_args.kwargs["trigger_background_recall"])

    async def test_export_skips_busy_messages_without_assistant_reply(self):
        session = self.store.begin("12345")
        with conv.use_session(session):
            self.store.remember(session, "生成说明", "已有的完整正文")
            self.store.remember(session, "语音忙碌时的补充", "", delivery_status="received_only")
            with patch.object(core.doc_service, "is_placeholder_document", return_value=False), patch.object(core.doc_service, "prepare_documents", AsyncMock(return_value=[Path("/tmp/example.docx")])) as prepare, patch.object(core, "_send_exported_documents", AsyncMock(return_value=True)):
                await core._export_previous_reply(self.bot, private_event(), "导出docx", None, "12345")
        self.assertEqual(prepare.await_args.args[1], "已有的完整正文")

    async def test_amiya_apologizes_once_then_silences_until_explicit_wake(self):
        with patch.object(llm, "chat", AsyncMock(return_value="对不起，我刚才打扰到你了。我先安静十秒。")):
            await core.handle_agent_message(self.bot, private_event("阿米娅，闭嘴"))
        self.assertEqual(self.bot.send.await_count, 1)
        self.assertEqual(self.status(), "done")
        self.assertTrue(core._amiya_is_silenced("private:12345"))

        core._cooldown.clear()
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="我会接着听你说。")) as dispatch:
            await core.handle_agent_message(self.bot, private_event("你好"))
            dispatch.assert_not_awaited()
            self.assertEqual(self.bot.send.await_count, 1)

            core._touch_cooldown("12345")
            await core.handle_agent_message(self.bot, private_event("阿米娅，你可以继续说话了"))
            dispatch.assert_awaited_once()
        self.assertEqual(self.bot.send.await_count, 2)
        self.assertFalse(core._amiya_is_silenced("private:12345"))

    async def test_any_group_member_can_explicitly_wake_amiya(self):
        with patch.object(llm, "chat", AsyncMock(return_value="对不起，我刚才打扰到你了。我先安静十秒。")):
            await core.handle_agent_message(self.bot, event("阿米娅，闭嘴", uid=12345, at=False))
        self.assertTrue(core._amiya_is_silenced("group:10001"))

        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="我会接着听大家说。")) as dispatch:
            await core.handle_agent_message(self.bot, event("阿米娅，可以继续说话了", uid=67890, at=False))

        dispatch.assert_awaited_once()
        self.assertFalse(core._amiya_is_silenced("group:10001"))

    async def test_poke_toggle_disables_notice_reply(self):
        fake_event = SimpleNamespace(target_id=1412773476, self_id=1412773476, group_id=10001, user_id=12345)
        with patch.object(core, "state", SimpleNamespace(accept_poke=False, global_active=True)), \
             patch.object(core.local_rag_service, "generate_poke_reaction", AsyncMock()) as generate:
            await core.handle_poke_notify(self.bot, fake_event)
        generate.assert_not_awaited()
        self.bot.send.assert_not_awaited()

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
        fake_state = SimpleNamespace(voice_emotion_director_enabled=True, voice_speed=1.0, voice_temperature=0.78, voice_top_p=0.85)
        with patch.object(tts, "state", fake_state), patch.object(tts, "_voice_candidates", return_value=candidates), patch.object(tts, "select_delivery", AsyncMock(return_value=decision)):
            result = await tts._select_voice_delivery("非常开心！", "test", {})
        self.assertEqual(result["meta"]["reference"], "hf_010")

    async def test_ai_failure_cannot_silently_choose_neutral(self):
        candidates = [{"name": "default", "audio": "/tmp/a", "text": "你好"}]
        fake_state = SimpleNamespace(voice_emotion_director_enabled=True, voice_speed=1.0, voice_temperature=0.78, voice_top_p=0.85)
        with patch.object(tts, "state", fake_state), patch.object(tts, "_voice_candidates", return_value=candidates), patch.object(tts, "select_delivery", AsyncMock(side_effect=TTSGenerationError())):
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

    async def test_explicit_image_model_uses_reference_as_character_identity_only(self):
        payloads = []

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"output": {"choices": [{"message": {"content": [{"image": "https://images.example/amiya.png"}]}}]}}

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, url, *, json, headers):
                payloads.append(json)
                return FakeResponse()

        with patch("httpx.AsyncClient", lambda **kwargs: FakeClient()), patch.object(llm, "_report_llm_progress"):
            result = await llm.generate_image_with_qwen_or_wan(
                api_key="test",
                user_prompt="阿米娅在电脑和笔记本之间认真工作",
                image_b64_list=["data:image/png;base64,cmVmZXJlbmNl"],
                image_model="qwen-image-3.0-pro",
                character_reference_only=True,
                generation_timeout_sec=240.0,
            )

        self.assertIn("amiya.png", result)
        self.assertEqual(len(payloads), 1)
        content = payloads[0]["input"]["messages"][0]["content"]
        self.assertEqual(payloads[0]["model"], "qwen-image-3.0-pro")
        self.assertEqual(content[0]["image"], "data:image/png;base64,cmVmZXJlbmNl")
        self.assertIn("只用于锁定角色身份", content[-1]["text"])
        self.assertIn("不要复刻原图的姿势", content[-1]["text"])
        self.assertNotIn("双人同框", content[-1]["text"])

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

    async def test_bare_at_uses_recent_group_context_without_error(self):
        bot = SimpleNamespace(
            self_id=1412773476,
            send=AsyncMock(return_value={"message_id": 88}),
            call_api=AsyncMock(return_value={
                "messages": [
                    {"message_id": 101, "sender": {"user_id": 23456, "nickname": "群友甲"}, "message": [{"type": "text", "data": {"text": "琴柳拿过几次危机合约冠军？"}}]},
                    {"message_id": 102, "sender": {"user_id": 34567, "nickname": "群友乙"}, "message": [{"type": "text", "data": {"text": "别逗你琴姐笑了"}}]},
                ]
            }),
        )
        ctx = await core._build_bare_at_group_context(bot, "711676140", "1785381042", "博士", "490264976")
        self.assertIn("琴柳拿过几次危机合约冠军", ctx)
        self.assertIn("单纯艾特了你一下", ctx)
        self.assertIn("不强制指定回复某一个人的需求", ctx)


if __name__ == "__main__":
    unittest.main()
