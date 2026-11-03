"""阶段 1 定向验证测试套件：
1. 群聊 45 秒连续对话注意力窗口（Attention Window）：
   - 回复某位用户后开启 45 秒活跃焦点窗口，随连续互动刷新并按轮次衰减；
   - 焦点窗口内后续跟进消息（无 @ 其他人、无 [reply] 引用其他群友）免 @ 获得准优先级跟进；
   - 当用户 @ 其他人或 [reply] 引用其他群友时，不触发焦点跟进；
   - 超过 45 秒或超过连续免 @ 轮次上限后自动退出焦点。
2. 群聊旁观抑制（Cross-Talk / Bystander Suppression）：
   - 当检测到群内其他两名群友高频互相 [reply] 对线或密集双人交替争论时，自动触发旁观抑制，压低阿米娅的普通主动插话率。
3. 自主工作之「约定兑现账本」（Promise & Agenda Loop）：
   - 授权主人（1785381042）自然语言时间约定自动提取（"今晚 10 点提醒我看编译原理课件"、"10 分钟后叫我喝水"、"帮我记一下明天早八交教案"）并落盘 `pending_promises.json`；
   - 非授权群友无法注册约定或触发本机执行器；
   - 后台 15~30s 轻量级心跳扫描（零大模型调用），到点后通过 Fake QQ 投递器与 Fake Mac 执行器兑现承诺并更新账本状态。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot
nonebot.init()

from src.plugins.agent_core import (
    autonomous_agent,
    group_chat_manager as gcm,
)

_TZ = ZoneInfo("Asia/Shanghai")


class TestStage1FocusAndPromisesV12(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def test_01_45s_attention_window_refresh_decay_and_quasi_priority(self):
        """验证 45 秒连续对话注意力窗口：开启、连续互动刷新、按轮次衰减、免 @ 准优先跟进及引用他人拦截。"""
        mgr = gcm.SmartGroupConcurrentManager(merge_window_sec=0.15)
        att = mgr.attention
        self.assertEqual(att.window_sec, 45.0)

        t0 = 1000.0
        # 1. 阿米娅在群 711676140 回复用户 1785381042 后，开启 45 秒焦点窗口
        score_0 = att.record_bot_reply("711676140", "1785381042", now=t0, was_explicit=True)
        self.assertEqual(score_0, 1.0)
        self.assertTrue(att.is_in_focus_window("711676140", "1785381042", now=t0 + 10.0))

        # 2. 12 秒后用户继续发言（未 @ 他人、未 [reply] 他人），免 @ 获得准优先级跟进并刷新窗口、递增轮次
        ok_f1 = att.should_followup_in_focus(
            "711676140",
            "1785381042",
            has_other_at=False,
            reply_to_user_id=None,
            bot_self_id="1412773476",
            text="那接下来第二步该怎么准备呢",
            now=t0 + 12.0,
        )
        self.assertTrue(ok_f1)
        st_1 = att.get_focus_state("711676140", "1785381042", now=t0 + 12.0)
        self.assertEqual(st_1["consecutive_turns"], 1)
        self.assertLess(st_1["score"], 1.0)
        self.assertGreater(st_1["score"], 0.7)

        # 3. 若用户在窗口期内 @ 其他人 或 [reply] 引用其他群友（如 999888），绝不触发对阿米娅的跟进
        ok_other_at = att.should_followup_in_focus(
            "711676140",
            "1785381042",
            has_other_at=True,
            reply_to_user_id=None,
            bot_self_id="1412773476",
            text="你觉得呢",
            now=t0 + 15.0,
        )
        self.assertFalse(ok_other_at)

        ok_reply_other = att.should_followup_in_focus(
            "711676140",
            "1785381042",
            has_other_at=False,
            reply_to_user_id="999888",
            bot_self_id="1412773476",
            text="我同意你的看法",
            now=t0 + 16.0,
        )
        self.assertFalse(ok_reply_other)

        # 4. 超过 45 秒未互动后焦点自动过期（例如距上次活跃 t0+12 过了 45.1 秒）
        self.assertFalse(att.is_in_focus_window("711676140", "1785381042", now=t0 + 12.0 + 45.1))
        self.assertEqual(att.attention_score("711676140", "1785381042", now=t0 + 12.0 + 45.1), 0.0)

        # 5. 验证合并窗口与优先调度器中的准优先级（is_quasi_priority）行为
        att.record_bot_reply("711676140", "1785381042", now=2000.0, was_explicit=True)
        q_follow = gcm.QueuedGroupMessage(
            message_id="msg_f1",
            group_id="711676140",
            user_id="1785381042",
            nickname="博士",
            text="顺便问一下复试占多少比例",
            timestamp=2005.0,
        )
        batch = await mgr.enqueue_and_collect_batch(q_follow)
        self.assertIsNotNone(batch)
        self.assertTrue(batch.primary_message.is_quasi_priority)
        self.assertTrue(batch.is_priority_batch)
        self.assertTrue(batch.is_quasi_priority_batch)

    async def test_02_bystander_suppression_on_peer_cross_talk(self):
        """验证旁观抑制：当群内两名群友高频互相 [reply] 对线或密集争论时，自动压低主动插话率。"""
        mgr = gcm.SmartGroupConcurrentManager(merge_window_sec=0.05)
        att = mgr.attention
        t0 = 5000.0

        # 群友 A(111) 与 群友 B(222) 互相 [reply] 对线
        att.record_group_message(
            "873321151", "111", reply_to_user_id="222", bot_self_id="1412773476", text="你这写法根本跑不通吧？", now=t0
        )
        s1 = att.get_bystander_suppression("873321151", now=t0 + 1.0)
        self.assertFalse(s1["suppressed"])

        s2 = att.record_group_message(
            "873321151", "222", reply_to_user_id="111", bot_self_id="1412773476", text="怎么可能，你才没看懂逻辑！", now=t0 + 4.0
        )
        self.assertTrue(s2["suppressed"])
        self.assertLessEqual(s2["suppression_factor"], 0.2)
        self.assertEqual(sorted(s2["participants"]), ["111", "222"])
        self.assertIn("peer_reply_cross_talk", s2["reason"])

        # 验证自主协调器在旁观抑制生效且未开空间时，识趣保持安静返回 bystander_suppressed
        coord = autonomous_agent.AutonomousAgentCoordinator(state_path=self.root / "auto_bystander.json")
        fake_state = SimpleNamespace(
            autonomous_group_chat_enabled=True,
            autonomous_qzone_enabled=False,
            autonomous_quiet_start_hour=23,
            autonomous_quiet_end_hour=8,
            active_character="amiya",
        )
        with patch.object(autonomous_agent, "group_chat_manager", mgr), \
             patch.object(autonomous_agent, "state", fake_state):
            coord.enqueue_group_event(
                message_id="c1", group_id="873321151", user_id="111", nickname="甲", text="你这写法根本跑不通吧？", timestamp=t0
            )
            coord.enqueue_group_event(
                message_id="c2", group_id="873321151", user_id="222", nickname="乙", text="怎么可能，你才没看懂逻辑！", timestamp=t0 + 4.0
            )
            res = await coord.evaluate_group_batch("873321151", now_ts=1729393200.0)
            self.assertEqual(res["action"], "no_action")
            self.assertTrue(str(res["reason"]).startswith("bystander_suppressed:"))

    async def test_03_promise_extraction_persistence_and_heartbeat_fulfillment(self):
        """验证约定与挂起任务表：自然语言解析、pending_promises.json 持久化、Owner 锁与心跳到期兑现。"""
        # 基准时间：2026-10-28 16:00:00+08:00
        base_dt = datetime.datetime(2026, 10, 28, 16, 0, 0, tzinfo=_TZ)
        base_ts = base_dt.timestamp()

        # 1. 验证三类典型自然语言约定解析
        p1 = autonomous_agent.parse_owner_promise_text("今晚 10 点提醒我看编译原理课件", now_ts=base_ts)
        self.assertIsNotNone(p1)
        expected_10pm = datetime.datetime(2026, 10, 28, 22, 0, 0, tzinfo=_TZ).timestamp()
        self.assertEqual(p1["trigger_ts"], expected_10pm)
        self.assertIn("看编译原理课件", p1["content"])
        self.assertEqual(p1["task_type"], "hybrid_remind")

        p2 = autonomous_agent.parse_owner_promise_text("10 分钟后叫我喝水", now_ts=base_ts)
        self.assertIsNotNone(p2)
        self.assertEqual(p2["trigger_ts"], base_ts + 600.0)
        self.assertEqual(p2["content"], "喝水")

        p3 = autonomous_agent.parse_owner_promise_text("帮我记一下明天早八交教案", now_ts=base_ts)
        self.assertIsNotNone(p3)
        expected_tmr_8am = datetime.datetime(2026, 10, 29, 8, 0, 0, tzinfo=_TZ).timestamp()
        self.assertEqual(p3["trigger_ts"], expected_tmr_8am)
        self.assertIn("交教案", p3["content"])
        self.assertEqual(p3["task_type"], "mac_reminder")

        # 2. 验证持久化账本 pending_promises.json 与非授权用户拦截
        promises_file = self.root / "pending_promises.json"
        coord = autonomous_agent.AutonomousAgentCoordinator(
            state_path=self.root / "auto_state.json",
            promises_path=promises_file,
            promise_heartbeat_sec=15.0,
        )

        # 非授权群友发言绝不落盘约定
        non_owner_item = coord.capture_owner_promise(
            "99999999", "10 分钟后叫我喝水", group_id="711676140", now_ts=base_ts
        )
        self.assertIsNone(non_owner_item)
        self.assertFalse(promises_file.exists())

        # 授权主人 1785381042 发言自动提取并持久化到 pending_promises.json
        owner_item = coord.capture_owner_promise(
            "1785381042", "10 分钟后叫我喝水", group_id="711676140", now_ts=base_ts
        )
        self.assertIsNotNone(owner_item)
        self.assertEqual(owner_item.uid, "1785381042")
        self.assertEqual(owner_item.group_id, "711676140")
        self.assertEqual(owner_item.status, "pending")
        self.assertTrue(promises_file.is_file())

        saved_json = json.loads(promises_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved_json["promises"]), 1)
        self.assertEqual(saved_json["promises"][0]["content"], "喝水")

        # 3. 验证轻量级心跳检查：未到期时不触发，到期后通过 Fake QQ 与 Fake Mac 执行器兑现承诺
        qq_sent: list[tuple[str, str]] = []
        mac_called: list[str] = []

        async def fake_send_group(gid: str, text: str) -> bool:
            qq_sent.append((gid, text))
            return True

        async def fake_mac_actuator(item: autonomous_agent.PendingPromiseItem) -> dict:
            mac_called.append(f"{item.task_type}:{item.content}")
            return {"ok": True}

        coord.configure_consumer(
            send_group_fn=fake_send_group,
            mac_actuator_fn=fake_mac_actuator,
        )

        # 5 分钟后（未到期）：心跳扫描不触发发送
        early_res = await coord.run_promise_heartbeat_tick(now_ts=base_ts + 300.0)
        self.assertEqual(early_res, [])
        self.assertEqual(len(qq_sent), 0)
        self.assertEqual(len(mac_called), 0)

        # 10 分钟零 1 秒（已到期）：心跳扫描立即兑现承诺，零大模型调用
        due_res = await coord.run_promise_heartbeat_tick(now_ts=base_ts + 601.0)
        self.assertEqual(len(due_res), 1)
        self.assertTrue(due_res[0]["delivered"])
        self.assertEqual(due_res[0]["status"], "delivered")
        self.assertEqual(len(qq_sent), 1)
        self.assertEqual(qq_sent[0][0], "711676140")
        self.assertIn("喝水", qq_sent[0][1])
        self.assertEqual(mac_called, ["hybrid_remind:喝水"])

        # 再次扫描不会重复触发已兑现任务
        again_res = await coord.run_promise_heartbeat_tick(now_ts=base_ts + 620.0)
        self.assertEqual(again_res, [])
        self.assertEqual(len(qq_sent), 1)


if __name__ == "__main__":
    unittest.main()
