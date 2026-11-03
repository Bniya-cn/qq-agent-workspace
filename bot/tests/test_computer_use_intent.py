"""主人私聊的电脑操作意图识别与确认门禁测试。"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.agent_core import llm_service
from src.plugins.agent_core.computer_use_service import MacComputerUseService
from src.plugins.agent_core.device_actuator import mac_actuator

OWNER = "1785381042"


class CaptureBot:
    def __init__(self):
        self.messages = []

    async def call_api(self, api, **kwargs):
        self.messages.append((api, kwargs))


class TestComputerUseOwnerIntent(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = MacComputerUseService()
        self.bot = CaptureBot()
        self.owner_patch = patch.object(mac_actuator, "is_authorized_owner", side_effect=lambda uid: str(uid) == OWNER)
        self.enabled_patch = patch.object(mac_actuator, "enabled", True)
        self.owner_patch.start()
        self.enabled_patch.start()
        self.addCleanup(self.owner_patch.stop)
        self.addCleanup(self.enabled_patch.stop)

    async def asyncTearDown(self):
        await self.service.shutdown()

    async def test_owner_request_is_reported_and_waits_for_plain_confirmation(self):
        response = '{"decision":"computer_task","task":"打开 macOS 自带计算器","summary":"打开系统计算器"}'
        with patch.object(llm_service, "chat", AsyncMock(return_value=response)) as chat:
            candidate = await self.service.observe_owner_input(OWNER, "帮我打开计算器", self.bot, "private:1")
            self.assertTrue(candidate)
            await asyncio.gather(*tuple(self.service._intent_tasks))

            self.assertEqual(chat.await_count, 1)
            self.assertIn(OWNER, self.service._pending_proposals)
            self.assertEqual(self.service._active_job_id, None)
            self.assertIn("确认操作", self.bot.messages[-1][1]["message"])
            self.assertIn("还没有操作电脑", self.bot.messages[-1][1]["message"])

            with patch.object(self.service, "start", AsyncMock(return_value="job-1")) as start:
                consumed = await self.service.handle_owner_confirmation(OWNER, "确认操作", self.bot)
            self.assertTrue(consumed)
            start.assert_awaited_once_with(OWNER, "打开 macOS 自带计算器", self.bot)
            self.assertNotIn(OWNER, self.service._pending_proposals)

    async def test_non_owner_cannot_trigger_classifier_or_confirmation(self):
        with patch.object(llm_service, "chat", AsyncMock()) as chat:
            candidate = await self.service.observe_owner_input("999999", "帮我打开计算器", self.bot, "private:2")
            confirmed = await self.service.handle_owner_confirmation("999999", "确认操作", self.bot)
        self.assertFalse(candidate)
        self.assertFalse(confirmed)
        chat.assert_not_awaited()
        self.assertEqual(self.bot.messages, [])

    async def test_other_whitelisted_accounts_are_not_computer_owners(self):
        with patch.object(mac_actuator, "is_authorized_owner", return_value=True):
            candidate = await self.service.observe_owner_input("999999", "帮我打开计算器", self.bot, "private:other")
        self.assertFalse(candidate)
        self.assertEqual(self.bot.messages, [])

    async def test_non_action_decision_creates_no_proposal(self):
        with patch.object(llm_service, "chat", AsyncMock(return_value='{"decision":"none","task":"","summary":""}')):
            await self.service.observe_owner_input(OWNER, "你觉得我应该用 Codex 还是别的工具？", self.bot, "private:3")
            await asyncio.gather(*tuple(self.service._intent_tasks))
        self.assertEqual(self.service._pending_proposals, {})
        self.assertEqual(self.bot.messages, [])

    async def test_owner_message_without_action_keywords_is_still_judged(self):
        with patch.object(llm_service, "chat", AsyncMock(return_value='{"decision":"none","task":"","summary":""}')) as chat:
            candidate = await self.service.observe_owner_input(OWNER, "今天风挺舒服的", self.bot, "private:ordinary")
            await asyncio.gather(*tuple(self.service._intent_tasks))
        self.assertFalse(candidate)
        chat.assert_awaited_once()
        self.assertEqual(self.service._pending_proposals, {})
        self.assertEqual(self.bot.messages, [])

    async def test_new_owner_message_invalidates_stale_proposal(self):
        self.service._pending_proposals[OWNER] = {
            "uid": OWNER, "task": "打开计算器", "summary": "打开计算器", "expires_at": 9999999999,
        }
        candidate = await self.service.observe_owner_input(OWNER, "今天天气不错", self.bot, "private:4")
        self.assertFalse(candidate)
        self.assertNotIn(OWNER, self.service._pending_proposals)
        self.assertFalse(await self.service.handle_owner_confirmation(OWNER, "确认操作", self.bot))

    async def test_waiting_step_accepts_plain_confirmation(self):
        decision = asyncio.Event()
        self.service._jobs["job-2"] = {
            "id": "job-2", "uid": OWNER, "status": "waiting_owner", "decision": decision,
            "confirmed": False, "task_handle": None,
        }
        self.service._active_job_id = "job-2"
        consumed = await self.service.handle_owner_confirmation(OWNER, "确认操作", self.bot)
        self.assertTrue(consumed)
        self.assertTrue(self.service._jobs["job-2"]["confirmed"])
        self.assertTrue(decision.is_set())
        self.assertIn("收到确认", self.bot.messages[-1][1]["message"])


if __name__ == "__main__":
    unittest.main()
