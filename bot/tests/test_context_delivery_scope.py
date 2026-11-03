"""验证群聊投递后的会话来源、纠正追问及私聊隔离。"""
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.tests import test_agent_pipeline as pipeline

event, conv, core, llm, tts = pipeline.event, pipeline.conv, pipeline.core, pipeline.llm, pipeline.tts


class ContextDeliveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = pipeline.HandlerTests.setUp

    async def test_vision_answer_is_visible_to_next_group_correction(self):
        session = self.store.begin("12345")
        with conv.use_session(session):
            self.store.remember(session, "私聊保密内容", "私聊专属回答")
        answer = "上排桑葚，迷迭香，星熊，赫默，缪缪，下排菲亚梅塔"
        with patch.object(core, "_resolve_reply_and_forward_context", AsyncMock(return_value=("", [{"url": "test-image"}]))), patch.object(core, "_resolve_input_images_b64", AsyncMock(return_value=["data:image/png;base64,dGVzdA=="])), patch.object(core, "_dispatch_reply", AsyncMock(return_value=answer)):
            await core.handle_agent_message(self.bot, event("这上面都是谁啊"))
        history = self.store.begin("12345").history
        self.assertEqual(history[-1]["assistant"], answer)
        self.assertEqual(history[-1]["visibility"], "group")
        self.assertEqual(history[0]["visibility"], "private")
        captured = []

        async def capture(**kwargs):
            captured.extend(conv.messages("测试角色", kwargs["user_text"]))
            return "刚才是我认错了，需要重新核对原图。"

        core._cooldown.clear()
        with patch.object(core, "_dispatch_reply", AsyncMock(side_effect=capture)):
            await core.handle_agent_message(self.bot, event("哪来的星熊啊兔兔"))
        self.assertIn(answer, [m["content"] for m in captured if m["role"] == "assistant"])
        self.assertNotIn("私聊专属回答", str(captured))
        self.assertIsNone(conv.current())

    async def test_voice_delivery_keeps_group_visibility(self):
        self.fake_state.voice_enabled = True
        with patch.object(core, "_dispatch_reply", AsyncMock(return_value="刚才是我认错了。")), patch.object(tts, "synthesize", AsyncMock(return_value="base64://voice")), patch.object(core, "_safe_send_voice", AsyncMock(return_value=True)):
            await core.handle_agent_message(self.bot, event("刚才认错了吧"))
        self.assertEqual(self.store.begin("12345").history[-1]["visibility"], "group")

    async def test_fast_engine_request_includes_previous_group_answer(self):
        session = self.store.begin("12345")
        with conv.use_session(session):
            self.store.remember(session, "私聊的问题", "不能公开的私聊答案")
        with conv.use_session(session, group_id="10001"):
            self.store.remember(session, "这上面都是谁啊", "我刚才说第三个是星熊")
            caller = AsyncMock(return_value="我刚才认错了，得再核对原图。")
            rag = core.local_rag_service
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch.object(rag, "ensure_bot_env"), patch.object(rag, "state", SimpleNamespace(chat_1v1_enabled=True, small_talk_mode=False)), patch.object(rag.vector_kb, "search", AsyncMock(return_value=([], 0.0))), patch.object(rag, "_build_fast_system_prompt", return_value="测试角色"), patch.object(llm, "_post_chat_completions", caller):
                reply = await rag.fast_chat_reply("amiya", "哪来的星熊啊兔兔", group_id="10001")
        self.assertTrue(reply)
        messages = caller.await_args.kwargs["messages_override"]
        self.assertIn("我刚才说第三个是星熊", [m["content"] for m in messages if m["role"] == "assistant"])
        self.assertNotIn("不能公开的私聊答案", str(messages))

    def test_group_history_uses_idle_ttl_instead_of_75_seconds(self):
        now = time.time()
        with patch.object(conv.time, "time", return_value=now):
            session = self.store.begin("12345")
            with conv.use_session(session, group_id="10001"):
                for i in range(4):
                    self.store.remember(session, f"第{i}问", f"第{i}答")
        with patch.object(conv.time, "time", return_value=now + 120):
            session = self.store.begin("12345")
            with conv.use_session(session, group_id="10001"):
                self.assertEqual(len(conv.visible_history(session)), 4)
        with patch.object(conv.time, "time", return_value=now + 120 + conv.TTL_SECONDS + 1):
            self.assertEqual(self.store.begin("12345").history, [])


if __name__ == "__main__":
    unittest.main()
