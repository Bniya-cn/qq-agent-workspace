"""Regression Revision v8 专项验证矩阵单元测试。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot
nonebot.init()

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, PokeNotifyEvent, Message
from src.plugins import agent_core as core
from src.plugins.agent_core import conversation_service as conv, local_rag_service as rag
from src.plugins.agent_core.history_recorder import TaskProgressTracker
from src.plugins.agent_core.modes import small_talk


class TestRegressionV8Matrix(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = conv.ConversationStore(Path(self.temp.name) / "context.db")
        self.tracker = TaskProgressTracker()
        self.bot = SimpleNamespace(
            send=AsyncMock(return_value={"message_id": 10}),
            call_api=AsyncMock(return_value={"message_id": 11}),
            send_group_msg=AsyncMock(return_value={"message_id": 12}),
        )
        self.fake_state = SimpleNamespace(
            global_active=True,
            active_character="amiya",
            active_groups=["10001"],
            voice_enabled=False,
            small_talk_mode=True,
            small_talk_style="pure_persona",
        )
        for item in (
            patch.object(conv, "store", self.store),
            patch.object(core, "state", self.fake_state),
            patch.object(core.recorder, "progress_tracker", self.tracker),
            patch.object(core, "_resolve_reply_and_forward_context", AsyncMock(return_value=("", []))),
            patch.object(core, "_resolve_input_files", AsyncMock(return_value=[])),
        ):
            item.start()
            self.addCleanup(item.stop)
        core._cooldown.clear()
        core._active_chat_window.clear()
        if hasattr(core, "_poke_cooldown"):
            core._poke_cooldown.clear()

    def test_01_amiya_preset_and_handbooks_enriched(self):
        """验证阿米娅 preset 与双路径 handbook 均已扩充核心灵魂细节。"""
        preset_txt = (core.BOT_ROOT / "data" / "presets" / "amiya.txt").read_text(encoding="utf-8")
        hb1 = (core.BOT_ROOT / "data" / "knowledge" / "amiya_handbook.txt").read_text(encoding="utf-8")
        hb2 = (core.BOT_ROOT / "data" / "knowledge" / "amiya" / "handbook.txt").read_text(encoding="utf-8")

        for kw in ("雷姆必拓", "胡萝卜", "小提琴", "指环", "兔耳", "严禁每句话开头都机械复读"):
            self.assertIn(kw, preset_txt)
        for kw in ("档案资料一", "档案资料二", "档案资料三", "档案资料四", "特蕾西娅", "黑曜石指环"):
            self.assertIn(kw, hb1)
            self.assertIn(kw, hb2)

    def test_02_dynamic_12_40_char_gate_and_doctor_prefix_stripping(self):
        """验证 12字/40字 动态字数门禁与句首机械‘博士’前缀剥离。"""
        self.assertFalse(small_talk.is_explicit_user_question("今天好热啊"))
        self.assertFalse(small_talk.is_explicit_user_question("终于下班了"))
        self.assertTrue(small_talk.is_explicit_user_question("阿米娅你最喜欢吃什么"))
        self.assertTrue(small_talk.is_explicit_user_question("这题怎么做？"))

        # 非提问日常：剥离“博士，”并收敛至 <= 12 字
        daily_out = small_talk.format_small_talk_output(
            "博士，赶紧去吹会儿空调吃根冰棍吧，外面的太阳实在太晒了。",
            character="amiya",
            user_text="今天好热啊",
        )
        self.assertFalse(daily_out.startswith("博士"))
        self.assertLessEqual(len(daily_out), 12)

        # 主动提问：允许放宽至 <= 40 字，同样剥离句首“博士，”
        q_out = small_talk.format_small_talk_output(
            "博士，食堂刚出炉的焦糖胡萝卜派！外壳酥脆内馅又甜又软，每次闻到都走不动道。",
            character="amiya",
            user_text="阿米娅你最喜欢吃什么",
        )
        self.assertFalse(q_out.startswith("博士"))
        self.assertGreater(len(q_out), 12)
        self.assertLessEqual(len(q_out), 40)

        # 默认兜底语更新验证
        empty_out = small_talk.format_small_talk_output("", character="amiya")
        self.assertEqual(empty_out, "刚刚说啥呢，我正听着")

    async def test_03_zero_robotic_error_messages_in_group_and_private(self):
        """验证超时或异常时：群聊静默不打扰，私聊自然口吻兜底，绝不出现‘处理请求超时/稍后重试/异常’。"""
        grp_ev = GroupMessageEvent.model_validate({
            "time": int(time.time()), "self_id": 1412773476, "post_type": "message", "message_type": "group",
            "sub_type": "normal", "message_id": 801, "group_id": 10001, "user_id": 12345,
            "raw_message": "你好", "message": Message("你好"), "to_me": True, "font": 0, "sender": {"user_id": 12345},
        })
        with patch.object(core, "_dispatch_reply", AsyncMock(side_effect=TimeoutError())):
            await core.handle_agent_message(self.bot, grp_ev)
        self.bot.send.assert_not_awaited()
        self.assertEqual(next(iter(self.tracker.tasks.values()))["status"], "error")

        core._cooldown.clear()
        prv_ev = PrivateMessageEvent.model_validate({
            "time": int(time.time()), "self_id": 1412773476, "post_type": "message", "message_type": "private",
            "sub_type": "friend", "message_id": 802, "user_id": 12345,
            "raw_message": "你好", "message": Message("你好"), "to_me": True, "font": 0, "sender": {"user_id": 12345},
        })
        with patch.object(core, "_dispatch_reply", AsyncMock(side_effect=TimeoutError())):
            await core.handle_agent_message(self.bot, prv_ev)
        self.bot.send.assert_awaited_once()
        reply_str = str(self.bot.send.await_args.args[1])
        for forbidden in ("处理请求", "稍后重试", "异常", "超时", "接口"):
            self.assertNotIn(forbidden, reply_str)

    async def test_04_morning_tarot_single_bubble_with_image(self):
        """验证早安塔罗广播将文字与图片打包在同一个 Message 气泡中发送。"""
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2400
        with patch.object(rag, "generate_morning_tarot_quote", AsyncMock(return_value=("早安~今日【太阳·正位】，宜吃顿好的，忌空腹喝冰美式✨", "太阳·正位"))), \
             patch.object(rag, "_try_generate_validated_image", AsyncMock(return_value=fake_png)) as generate_image, \
             patch("nonebot.get_bots", return_value={"1412773476": self.bot}), \
             patch.object(rag, "state", self.fake_state):
            res = await rag.broadcast_morning_greeting(with_image=True)
        self.assertTrue(generate_image.await_args.kwargs["use_amiya_ref"])
        self.assertEqual(generate_image.await_args.kwargs["image_model"], rag.DAILY_IMAGE_MODEL)
        self.assertTrue(res.get("ok") or res.get("delivered_groups") or self.bot.call_api.await_count or self.bot.send_group_msg.await_count)
        # 检查实际发出的消息对象同时包含 text 和 image 段（单气泡）
        sent_msg = None
        if self.bot.send_group_msg.await_args:
            sent_msg = self.bot.send_group_msg.await_args.kwargs.get("message")
        elif self.bot.call_api.await_args:
            sent_msg = self.bot.call_api.await_args.kwargs.get("message")
        self.assertIsNotNone(sent_msg)
        seg_types = [seg.type for seg in sent_msg]
        self.assertIn("text", seg_types)
        self.assertIn("image", seg_types)
        self.assertEqual(len(seg_types), 2)

    async def test_05_poke_notify_realtime_reaction_and_cooldown(self):
        """验证戳一戳事件实时生成 <=12字、不含‘博士’的灵动反应，并具备 3 秒防刷冷却。"""
        poke_ev = PokeNotifyEvent.model_validate({
            "time": int(time.time()),
            "self_id": 1412773476,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 10001,
            "user_id": 54321,
            "target_id": 1412773476,
        })
        with patch.object(rag, "generate_poke_reaction", AsyncMock(return_value="唔…耳朵不能乱捏的啦")):
            await core.handle_poke_notify(self.bot, poke_ev)
            self.assertEqual(self.bot.send.await_count, 1)
            sent_poke = str(self.bot.send.await_args.args[1])
            self.assertLessEqual(len(sent_poke), 12)
            self.assertNotIn("博士", sent_poke)

            # 3秒内重复戳一戳触发冷却保护
            await core.handle_poke_notify(self.bot, poke_ev)
            self.assertEqual(self.bot.send.await_count, 1)


if __name__ == "__main__":
    unittest.main()
