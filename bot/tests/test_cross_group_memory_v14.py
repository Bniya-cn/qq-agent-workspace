"""测试实时群聊入库、连续对话被群友打断后的未回复记忆召回、分群分QQ号记录、跨群画像共享与按QQ号定向清理。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.agent_core.group_chat_manager import MergedMessageBatch, QueuedGroupMessage
from src.plugins.agent_core.user_memory_service import UserMemoryService

OWNER_UID = "1785381042"


class CrossGroupMemoryAndInterruptedChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.mem = UserMemoryService(
            db_path=self.tmp_path / "user_profiles.sqlite3",
            mem0_dir=self.tmp_path / "mem0_qdrant",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_01_interrupted_chat_carrot_recall_and_cross_group_sharing(self) -> None:
        """验证：博士说“我给你爸爸发了一根胡萝卜”后被群友插话截断，再次呼叫或换群时阿米娅依然记得。"""
        group_a = "873321151"
        group_b = "711676140"

        # 1. 消息一到达即入库（初始 replied=False）
        self.mem.ingest_realtime_message(
            uid=OWNER_UID,
            user_text="我给你爸爸发了一根胡萝卜",
            group_id=group_a,
            sender_nickname="博士",
            message_id="m_101",
            schedule_background_mem0=False,
        )
        # 群友插话
        self.mem.ingest_realtime_message(
            uid="22222222",
            user_text="大家今晚吃什么夜宵",
            group_id=group_a,
            sender_nickname="群友甲",
            message_id="m_102",
            schedule_background_mem0=False,
        )

        # 2. 按白名单群 + QQ号独立查询记录，互不混淆
        owner_logs = self.mem.get_group_chat_history(group_a, uid=OWNER_UID, limit=10)
        self.assertEqual(len(owner_logs), 1)
        self.assertEqual(owner_logs[0]["text"], "我给你爸爸发了一根胡萝卜")
        self.assertFalse(owner_logs[0]["replied"])

        other_logs = self.mem.get_group_chat_history(group_a, uid="22222222", limit=10)
        self.assertEqual(len(other_logs), 1)
        self.assertEqual(other_logs[0]["text"], "大家今晚吃什么夜宵")

        # 3. 再次呼叫阿米娅时，同群提示词包含刚才被打断的“我给你爸爸发了一根胡萝卜”
        ctx_in_group_a = self.mem.get_shared_user_context_block(OWNER_UID, current_group_id=group_a)
        self.assertIn("我给你爸爸发了一根胡萝卜", ctx_in_group_a)

        # 4. 换到另一个白名单群 B（711676140），跨群共享记忆依然包含这句话与提取出的互动事实
        ctx_in_group_b = self.mem.get_shared_user_context_block(OWNER_UID, current_group_id=group_b)
        self.assertIn("我给你爸爸发了一根胡萝卜", ctx_in_group_b)

        # 5. 标记已回复后，replied 状态更新为 True
        self.mem.mark_message_replied(uid=OWNER_UID, group_id=group_a, message_id="m_101", bot_reply="噗，胡萝卜收到啦")
        owner_logs_after = self.mem.get_group_chat_history(group_a, uid=OWNER_UID, limit=10)
        self.assertTrue(owner_logs_after[0]["replied"])

    def test_02_merged_batch_strictly_separates_primary_speaker_and_bystanders(self) -> None:
        """验证群合并窗口将主对话人发言与其他群友插话严格按 QQ 号拆分展示。"""
        m1 = QueuedGroupMessage(
            message_id="1",
            group_id="873321151",
            user_id=OWNER_UID,
            nickname="博士",
            text="我给你爸爸发了一根胡萝卜",
            timestamp=100.0,
            is_at_me=True,
        )
        m2 = QueuedGroupMessage(
            message_id="2",
            group_id="873321151",
            user_id="33333333",
            nickname="路人乙",
            text="我刚才去买奶茶了",
            timestamp=101.0,
        )
        batch = MergedMessageBatch(
            group_id="873321151",
            messages=[m1, m2],
            is_priority_batch=True,
        )
        formatted = batch.format_merged_context()
        self.assertIn(f"当前主对话人（QQ={OWNER_UID}", formatted)
        self.assertIn("同一合并窗口内其他群友的插话", formatted)
        self.assertIn("QQ=33333333", formatted)

    def test_03_targeted_clear_user_profile_by_uid(self) -> None:
        """验证按 QQ 号定向清理全作用域画像与聊天记录，不影响其他群友。"""
        self.mem.ingest_realtime_message(
            uid=OWNER_UID,
            user_text="我平时喜欢喝冰美式",
            group_id="873321151",
            sender_nickname="博士",
            message_id="m_201",
            schedule_background_mem0=False,
        )
        self.mem.ingest_realtime_message(
            uid="22222222",
            user_text="我喜欢喝热牛奶",
            group_id="873321151",
            sender_nickname="群友甲",
            message_id="m_202",
            schedule_background_mem0=False,
        )
        res = self.mem.clear_user_profile_by_uid(OWNER_UID)
        self.assertEqual(res["uid"], OWNER_UID)
        self.assertTrue(res["cleared"])
        self.assertGreaterEqual(len(res["deleted_scopes"]), 1)
        self.assertGreaterEqual(res["removed_chat_logs"], 1)
        self.assertEqual(self.mem.get_shared_user_context_block(OWNER_UID, current_group_id="873321151"), "")
        self.assertIn("热牛奶", self.mem.get_shared_user_context_block("22222222", current_group_id="873321151"))


if __name__ == "__main__":
    unittest.main()
