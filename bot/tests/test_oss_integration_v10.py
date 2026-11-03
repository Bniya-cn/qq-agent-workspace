"""Group Chat Plus + MaiBot + Mem0 融合升级定向验证测试套件。

覆盖验证范围：
1. 所选依赖实际导入及最小接口调用（mem0ai==2.2.1、group_chat_manager、expression_learner、user_memory_service、autonomous_agent）；
2. 两名用户、角色和可见范围的记忆隔离（私聊 vs 群聊、同群不同用户、跨群隔离、事实纠正、异角色历史过滤）；
3. 群消息合并、显式请求优先、消息不重复消费、群间独立；
4. 阿米娅固定人格与 MaiBot 场景表达参考的组合（不覆盖角色身份、不改写 amiya.txt）；
5. 长回答分批发送时其他用户仍可获得响应，以及投递未确认时暂停与已送达历史记录；
6. 后台训练任务队列状态、真实失败记录、真实引用关系配对与任务关停清理；
7. 群聊配额原子预占、幂等确认/释放、单群普通并发上限及真正的优先队列调度；
8. Mem0 真实同步状态、版本并发保护、语义召回合并与用户/角色/私聊群聊严格隔离。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot
nonebot.init()

from src.plugins.agent_core import (
    autonomous_agent,
    config_manager as config,
    conversation_service as conv,
    expression_learner,
    group_chat_manager as gcm,
    llm_service as llm,
    user_memory_service as ums,
)
from src.plugins.agent_core.modes import small_talk, training_mode


class TestOSSIntegrationV10(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def test_01_dependencies_and_minimal_mem0_calls(self):
        """验证 mem0ai==2.2.1 实际导入、本地 Qdrant 向量写入/检索及核心模块最小接口调用。"""
        import mem0
        from mem0 import AsyncMemory, Memory
        from mem0.vector_stores.qdrant import Qdrant

        self.assertTrue(hasattr(mem0, "__file__"))
        self.assertTrue(callable(Memory.from_config))
        self.assertTrue(callable(AsyncMemory.from_config))

        qstore = Qdrant(
            collection_name="verify_mem0_sdk",
            embedding_model_dims=4,
            path=str(self.root / "qdrant_sdk"),
            on_disk=True,
        )
        try:
            qstore.insert(
                vectors=[[0.8, 0.2, 0.0, 0.0], [0.0, 0.1, 0.9, 0.0]],
                payloads=[
                    {"user_id": "private:10001", "fact": "喜欢喝热可可"},
                    {"user_id": "group:9001:user:10002", "fact": "常聊集成战略"},
                ],
                ids=[
                    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                ],
            )
            hits_1 = qstore.search(
                query="热可可",
                vectors=[0.8, 0.2, 0.0, 0.0],
                top_k=3,
                filters={"user_id": "private:10001"},
            )
            hits_2 = qstore.search(
                query="集成战略",
                vectors=[0.0, 0.1, 0.9, 0.0],
                top_k=3,
                filters={"user_id": "group:9001:user:10002"},
            )
            self.assertEqual(len(hits_1), 1)
            self.assertEqual(hits_1[0].payload["fact"], "喜欢喝热可可")
            self.assertEqual(len(hits_2), 1)
            self.assertEqual(hits_2[0].payload["fact"], "常聊集成战略")
        finally:
            qstore.client.close()

    def test_02_two_users_role_and_visibility_scope_isolation(self):
        """验证两名用户、不同群、私聊与群聊范围的记忆隔离、用户事实纠正以及历史角色/风格隔离。"""
        svc = ums.UserMemoryService(
            db_path=self.root / "profiles.sqlite3",
            mem0_dir=self.root / "mem0_qdrant",
        )
        # 用户 100001 在私聊透露私人偏好
        svc.record_interaction_sync(
            uid="100001",
            user_text="以后叫我林哥，我平时最喜欢吃提拉米苏",
            group_id=None,
            schedule_background_mem0=False,
        )
        # 用户 100001 在群 7001 聊备考
        svc.record_interaction_sync(
            uid="100001",
            user_text="我最近在备考注册电气工程师",
            group_id="7001",
            sender_nickname="林",
            schedule_background_mem0=False,
        )
        # 用户 200002 在同一群 7001 聊游戏
        svc.record_interaction_sync(
            uid="200002",
            user_text="以后叫我阿周，我平时最喜欢玩萨米肉鸽",
            group_id="7001",
            sender_nickname="周",
            schedule_background_mem0=False,
        )
        # 群友玩笑与转述不应被记为用户事实
        svc.record_interaction_sync(
            uid="200002",
            user_text="他说他喜欢吃石头，给你加一箱沙子背着",
            group_id="7001",
            schedule_background_mem0=False,
        )

        priv_1 = svc.get_fast_profile_prompt_block("100001", group_id=None)
        grp_1_in_7001 = svc.get_fast_profile_prompt_block("100001", group_id="7001")
        grp_1_in_8002 = svc.get_fast_profile_prompt_block("100001", group_id="8002")
        grp_2_in_7001 = svc.get_fast_profile_prompt_block("200002", group_id="7001")

        # 私聊信息绝不泄漏到群 7001 或群 8002
        self.assertIn("林哥", priv_1)
        self.assertIn("提拉米苏", priv_1)
        self.assertNotIn("林哥", grp_1_in_7001)
        self.assertNotIn("提拉米苏", grp_1_in_7001)
        self.assertIn("注册电气工程师", grp_1_in_7001)
        self.assertEqual(grp_1_in_8002, "")

        # 同群两名用户互不串味，且转述/玩笑未被写入
        self.assertIn("阿周", grp_2_in_7001)
        self.assertIn("萨米肉鸽", grp_2_in_7001)
        self.assertNotIn("注册电气工程师", grp_2_in_7001)
        self.assertNotIn("石头", grp_2_in_7001)

        # 用户主动纠正事实：旧事实失效，版本号递增
        svc.record_interaction_sync(
            uid="100001",
            user_text="你记错了，我现在不喜欢吃提拉米苏，我最喜欢吃焦糖布丁",
            group_id=None,
            schedule_background_mem0=False,
        )
        priv_1_updated = svc.get_fast_profile_prompt_block("100001", group_id=None)
        self.assertNotIn("最喜欢吃提拉米苏", priv_1_updated)
        self.assertIn("焦糖布丁", priv_1_updated)

        # 会话历史的角色与风格隔离 + 原子追加验证
        cstore = conv.ConversationStore(self.root / "conv.sqlite3")
        sess = cstore.begin("100001")
        with conv.use_session(sess, group_id="7001"):
            cstore.remember(sess, "凯尔希你在吗", "博士，注意你的言辞。", character="kaltsit", style="pure_persona")
            cstore.remember(sess, "旧镜像测试", "本鱼觉得行", character="amiya", style="group_mimic")
            cstore.remember(sess, "阿米娅今天吃什么", "今天食堂有胡萝卜派哦", character="amiya", style="pure_persona")
            vis_amiya = conv.visible_history(sess, current_character="amiya", current_style="pure_persona")
            self.assertEqual(len(vis_amiya), 1)
            self.assertEqual(vis_amiya[0]["assistant"], "今天食堂有胡萝卜派哦")

    async def test_03_group_merge_priority_dedup_and_group_independence(self):
        """验证群消息合并、显式请求优先、消息不重复消费及不同群独立推进。"""
        mgr = gcm.SmartGroupConcurrentManager(
            merge_window_sec=0.18,
            max_batch_messages=5,
            max_batch_chars=2000,
        )
        m_a1 = gcm.QueuedGroupMessage(
            message_id="m1", group_id="9001", user_id="111", nickname="路人甲",
            text="今天下午好困啊", timestamp=1700000001.0,
        )
        m_a2 = gcm.QueuedGroupMessage(
            message_id="m2", group_id="9001", user_id="222", nickname="路人乙",
            text="@阿米娅 帮我看看这个问题", timestamp=1700000001.05,
            is_at_me=True, reply_to_message_id="m0", reply_context="前文内容",
        )
        m_b1 = gcm.QueuedGroupMessage(
            message_id="m3", group_id="9002", user_id="333", nickname="隔壁群丙",
            text="阿米娅早呀", timestamp=1700000001.02, is_name_wake=True,
        )

        res_a1, res_a2, res_b1 = await asyncio.gather(
            mgr.enqueue_and_collect_batch(m_a1),
            mgr.enqueue_and_collect_batch(m_a2),
            mgr.enqueue_and_collect_batch(m_b1),
        )
        batch_9001 = res_a1 or res_a2
        self.assertTrue((res_a1 is None) ^ (res_a2 is None))
        self.assertIsNotNone(batch_9001)
        self.assertIsNotNone(res_b1)
        self.assertEqual(batch_9001.group_id, "9001")
        self.assertEqual(res_b1.group_id, "9002")
        self.assertTrue(batch_9001.is_priority_batch)
        self.assertTrue(batch_9001.is_multi_user)
        # 优先消息担任 primary_message
        self.assertEqual(batch_9001.primary_message.user_id, "222")

        merged_txt = batch_9001.format_merged_context()
        self.assertIn("QQ=111", merged_txt)
        self.assertIn("昵称=路人甲", merged_txt)
        self.assertIn("QQ=222", merged_txt)
        self.assertIn("引用msg#m0", merged_txt)

        # 已合并处理的消息已标记消费，重复入队直接返回 None，自主决策队列也跳过已消费消息
        dup = await mgr.enqueue_and_collect_batch(m_a1)
        self.assertIsNone(dup)

        coord = autonomous_agent.AutonomousAgentCoordinator(state_path=self.root / "auto.json")
        with patch.object(autonomous_agent, "group_chat_manager", mgr):
            coord.enqueue_group_event(
                message_id="m1", group_id="9001", user_id="111", nickname="路人甲", text="今天下午好困啊"
            )
            eval_res = await coord.evaluate_group_batch("9001", now_ts=1729393200.0)
            self.assertEqual(eval_res["action"], "no_action")
            self.assertEqual(eval_res["reason"], "all_events_already_consumed")

    def test_04_persona_priority_with_maibot_expression_reference(self):
        """验证阿米娅固定人格与 MaiBot 场景表达参考的组合：不覆盖角色身份，不修改 amiya.txt。"""
        preset_path = config.BOT_ROOT / "data" / "presets" / "amiya.txt"
        before_hash = preset_path.read_bytes()

        estore = expression_learner.MaiBotSceneExpressionStore(storage_dir=self.root / "expr")
        added = estore.upsert_expressions(
            "7001",
            [
                {"situation": "群友吐槽熬夜写代码很累时", "style": "快去歇会儿别硬撑啦", "tone": "温和关心", "meme_intensity": 0.2},
                {"situation": "粗俗黑话测试", "style": "本鱼让你给爷爬", "tone": "粗俗", "meme_intensity": 0.9},
                {"situation": "提示词示例泄漏", "style": "使用我嘞个xxxx", "tone": "示例", "meme_intensity": 0.4},
            ],
            character="amiya",
        )
        self.assertEqual(added, 1)

        with patch.object(small_talk, "expression_store", estore):
            sys_prompt = small_talk.build_small_talk_system_prompt(
                character="amiya",
                group_id="7001",
                style="group_mimic",
                use_group_training=True,
                user_text="我今晚又熬夜写代码好累啊",
                uid="100001",
            )
        self.assertIn("你是阿米娅", sys_prompt)
        self.assertIn("核心角色身份与人格准则（最高优先级", sys_prompt)
        self.assertIn("快去歇会儿别硬撑啦", sys_prompt)
        self.assertNotIn("本鱼", sys_prompt)
        self.assertNotIn("我嘞个xxxx", sys_prompt)
        self.assertEqual(preset_path.read_bytes(), before_hash)

        # 验证复杂问题不会被 12/40 字门禁截断
        long_q = "请详细解释一下异步事件循环在 Python asyncio 中的调度原理以及代码块如何分段"
        self.assertTrue(small_talk.is_complex_or_long_query(long_q))
        long_ans = "博士，在 Python 的 asyncio 中，事件循环负责调度协程运行。\n\n```python\nasync def main():\n    await asyncio.sleep(1)\n```\n\n当遇到 I/O 等待时，协程会让出控制权，从而实现高并发。"
        formatted_long = small_talk.format_small_talk_output(long_ans, character="amiya", user_text=long_q)
        self.assertFalse(formatted_long.startswith("博士"))
        self.assertIn("```python", formatted_long)
        self.assertGreater(len(formatted_long), 60)

    async def test_05_long_answer_segmented_fair_delivery_and_interleaving(self):
        """验证长回答分批发送时保持代码块完整、其他用户优先请求可插入获得响应，且中途失败暂停重发。"""
        mgr = gcm.SmartGroupConcurrentManager()
        mgr.configure(chunk_pace_sec=0.08)

        long_reply = (
            "第一段：关于异步调度的核心概念，我们需要先理解事件循环与任务队列的关系。当一个长回答生成完毕后，发送阶段应当与生成锁解耦。\n\n"
            "```python\nasync def example():\n    for i in range(3):\n        await asyncio.sleep(0.1)\n        print(i)\n```\n\n"
            "第三段：这样当其他群友在分段发送期间发起艾特请求时，就不必干等整篇长文发完，能够立刻插入并拿到响应。"
        )
        segs = gcm.split_long_reply_segments(long_reply, max_segment_chars=95)
        self.assertGreaterEqual(len(segs), 3)
        # 验证代码块完整未断裂
        code_segs = [s for s in segs if "```python" in s]
        self.assertEqual(len(code_segs), 1)
        self.assertTrue(code_segs[0].endswith("```"))

        timeline: list[str] = []

        async def send_seg(seg: str) -> bool:
            timeline.append(f"long_seg:{len( timeline )}")
            return True

        async def priority_user_request():
            await asyncio.sleep(0.03)
            mgr.notify_priority_waiting("9001")
            try:
                async with mgr.get_generation_lock("9001"):
                    timeline.append("priority_user_replied")
            finally:
                mgr.notify_priority_done("9001")

        deliv_task = asyncio.create_task(
            mgr.deliver_segmented_fairly(
                task_id="task_long_1",
                group_id="9001",
                user_id="10001",
                full_text=long_reply,
                send_fn=send_seg,
                max_segment_chars=95,
                batch_chunk_size=1,
            )
        )
        prio_task = asyncio.create_task(priority_user_request())
        deliv_state, _ = await asyncio.gather(deliv_task, prio_task)

        self.assertEqual(deliv_state.status, "done")
        self.assertEqual(len(deliv_state.delivered_segments), len(segs))
        self.assertEqual(len(deliv_state.pending_segments), 0)
        # 验证优先请求在长回答第一段与最后一段之间成功插入完成！
        prio_idx = timeline.index("priority_user_replied")
        self.assertGreater(prio_idx, 0)
        self.assertLess(prio_idx, len(timeline) - 1)

        # 验证中途投递未确认时立即暂停，区分已送达与未送达片段
        call_cnt = 0

        async def flaky_send(seg: str) -> bool:
            nonlocal call_cnt
            call_cnt += 1
            return call_cnt == 1

        paused_state = await mgr.deliver_segmented_fairly(
            task_id="task_flaky",
            group_id="9001",
            user_id="10001",
            full_text=long_reply,
            send_fn=flaky_send,
            max_segment_chars=95,
        )
        self.assertEqual(paused_state.status, "paused_uncertain")
        self.assertEqual(len(paused_state.delivered_segments), 1)
        self.assertEqual(len(paused_state.pending_segments), len(segs) - 1)

    async def test_06_background_training_queue_error_reporting_and_cleanup(self):
        """验证后台训练状态、真实失败记录（不谎报成功）、真实引用关系配对与关停清理。"""
        with patch.object(training_mode, "TRAINING_DIR", self.root / "training"), \
             patch.object(training_mode, "GROUPS_DIR", self.root / "groups"), \
             patch.object(training_mode, "state", SimpleNamespace(training_mode=True, training_groups=["6001"], active_groups=["6001"])):
            # 1. 无引用关系的相邻两条消息绝不盲配进 SFT 数据集
            training_mode.record_group_message_for_training(
                group_id="6001", sender_uid="111", sender_name="甲",
                raw_text="我刚吃完晚饭", message_id="10", bot_self_id="999",
            )
            training_mode.record_group_message_for_training(
                group_id="6001", sender_uid="222", sender_name="乙",
                raw_text="今晚联机打游戏吗", message_id="11", bot_self_id="999",
            )
            # 机器人自身发言绝不录入
            rec_bot = training_mode.record_group_message_for_training(
                group_id="6001", sender_uid="999", sender_name="阿米娅",
                raw_text="我是机器人自己的回复", message_id="12", bot_self_id="999",
            )
            self.assertFalse(rec_bot)
            ds_file = self.root / "training" / "6001_dataset.jsonl"
            self.assertFalse(ds_file.exists())

            # 2. 有明确 reply_to_message_id 的真实对话才写入 SFT 数据集
            training_mode.record_group_message_for_training(
                group_id="6001", sender_uid="333", sender_name="丙",
                raw_text="来呀，算我一个", reply_to_message_id="11", message_id="13", bot_self_id="999",
            )
            self.assertTrue(ds_file.exists())
            lines = ds_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

            # 3. 训练失败时，任务队列如实记录 status="error" 与错误原因，绝不谎报 done
            jm = training_mode.TrainingJobManager(max_concurrent=1)
            with patch.object(
                training_mode.log_extractor,
                "run_log_extraction",
                AsyncMock(return_value={"group_id": "6001", "status": "error", "message": "模拟模型接口异常"}),
            ):
                job = jm.submit_job("6001", lambda: training_mode.trigger_group_training_now("6001"))
                await jm._active_tasks[job.task_id]
                info = jm.get_job(job.task_id)
                self.assertEqual(info["status"], "error")
                self.assertIn("模拟模型接口异常", info["error_message"])
                self.assertEqual(training_mode._training_stats["6001"]["status"], "error")

            # 4. 关停清理测试
            never_finish = asyncio.Event()
            slow_job = jm.submit_job("6002", lambda: never_finish.wait())
            await asyncio.sleep(0.02)
            await jm.shutdown()
            self.assertEqual(jm.get_job(slow_job.task_id)["status"], "cancelled")
            self.assertEqual(len(jm._active_tasks), 0)

    async def test_07_quota_atomic_reservation_and_priority_queue(self):
        """验证群聊配额原子预占、幂等确认/释放、单群普通并发上限及真正的优先队列调度。"""
        mgr = gcm.SmartGroupConcurrentManager(merge_window_sec=0.05)
        mgr.configure(
            min_interval_sec=5.0,
            max_per_minute=2,
            chunk_pace_sec=0.02,
        )

        # 1. 同一群多条普通消息同时到达，不会绕过最小间隔或每分钟配额重复生成
        res_tuples = await asyncio.gather(
            *[mgr.density.try_reserve_normal("9001", task_id=f"norm_{i}") for i in range(5)]
        )
        granted = [res for ok, _, res in res_tuples if ok and res is not None]
        self.assertEqual(len(granted), 1)
        r0 = granted[0]
        self.assertEqual(r0.status, "reserved")
        info0 = await mgr.density.get_density_info("9001")
        self.assertEqual(info0["active_reserved_count"], 1)

        # 2. 释放预占幂等：释放后额度立即归还，再次释放不会产生负数或异常
        self.assertTrue(await mgr.density.release_reservation(r0.reservation_id, reason="cancelled"))
        self.assertFalse(await mgr.density.release_reservation(r0.reservation_id, reason="cancelled_again"))
        info_rel = await mgr.density.get_density_info("9001")
        self.assertEqual(info_rel["active_reserved_count"], 0)

        # 3. 归还后可再次预占并幂等确认，确认后计入已发送窗口
        ok1, _, r1 = await mgr.density.try_reserve_normal("9001", task_id="norm_ok")
        self.assertTrue(ok1)
        self.assertIsNotNone(r1)
        self.assertTrue(await mgr.density.commit_reservation(r1))
        self.assertTrue(await mgr.density.commit_reservation(r1))
        self.assertFalse(await mgr.density.release_reservation(r1))
        stats = await mgr.density.get_density_info("9001")
        self.assertEqual(stats["active_reserved_count"], 0)
        self.assertEqual(stats["reply_count_last_minute"], 1)

        # 4. 真正的优先调度队列：当前已有 1 个普通任务在执行时，后续到达的显式请求排在排队普通任务之前，且不取消正在执行的任务
        order: list[str] = []
        running_started = asyncio.Event()
        release_running = asyncio.Event()

        async def worker_running_normal():
            async with mgr.acquire_generation_slot("9001", is_priority=False, task_id="run_norm", reserve_quota=False):
                order.append("start:run_norm")
                running_started.set()
                await release_running.wait()
                order.append("end:run_norm")

        async def worker_queued_normal_rejected():
            await running_started.wait()
            with self.assertRaises(gcm.NormalQuotaRejectedError):
                async with mgr.acquire_generation_slot("9001", is_priority=False, task_id="extra_norm", reserve_quota=False):
                    order.append("unexpected:extra_norm")

        async def worker_priority(idx: int):
            await running_started.wait()
            await asyncio.sleep(0.01 * idx)
            async with mgr.acquire_generation_slot("9001", is_priority=True, task_id=f"prio_{idx}", reserve_quota=False):
                order.append(f"exec:prio_{idx}")

        t_run = asyncio.create_task(worker_running_normal())
        t_rej = asyncio.create_task(worker_queued_normal_rejected())
        t_p1 = asyncio.create_task(worker_priority(1))
        t_p2 = asyncio.create_task(worker_priority(2))
        await asyncio.sleep(0.04)
        self.assertEqual(order, ["start:run_norm"])
        release_running.set()
        await asyncio.gather(t_run, t_rej, t_p1, t_p2)
        self.assertEqual(order, ["start:run_norm", "end:run_norm", "exec:prio_1", "exec:prio_2"])

        # 5. 多个显式请求连续到达时各自独立立即冲刷，绝不把多个显式请求合并成一个批次
        e1 = gcm.QueuedGroupMessage(
            message_id="exp_1", group_id="9005", user_id="101", nickname="甲",
            text="@阿米娅 第一个问题", timestamp=1700000100.0, is_at_me=True,
        )
        e2 = gcm.QueuedGroupMessage(
            message_id="exp_2", group_id="9005", user_id="102", nickname="乙",
            text="@阿米娅 第二个问题", timestamp=1700000100.01, is_at_me=True,
        )
        b1, b2 = await asyncio.gather(
            mgr.enqueue_and_collect_batch(e1),
            mgr.enqueue_and_collect_batch(e2),
        )
        self.assertIsNotNone(b1)
        self.assertIsNotNone(b2)
        self.assertEqual(b1.primary_message.message_id, "exp_1")
        self.assertEqual(b2.primary_message.message_id, "exp_2")
        self.assertEqual(mgr.get_message_state("exp_1"), "processing")
        mgr.mark_message_state("exp_1", "failed", reason="mock_error")
        self.assertEqual(mgr.get_message_state("exp_1"), "failed")

    async def test_08_mem0_sync_states_version_concurrency_and_scoped_recall(self):
        """验证 Mem0 真实同步状态、版本并发保护、语义召回合并与用户/角色/私聊群聊严格隔离。"""
        svc = ums.UserMemoryService(
            db_path=self.root / "profiles_v11.sqlite3",
            mem0_dir=self.root / "mem0_v11",
        )
        # 1. 刚写入但尚未后台同步时，状态为 pending_sync，绝不伪造 synced
        svc._mem0_instance = object()
        svc.record_interaction_sync(
            uid="30001",
            user_text="以后叫我小陈，我最喜欢喝冰美式",
            group_id=None,
            character="amiya",
            schedule_background_mem0=False,
        )
        prof_init = svc.get_scoped_profile("30001", group_id=None, character="amiya")
        self.assertEqual(prof_init.mem0_sync_status, "pending_sync")
        v1 = prof_init.profile_version

        # 2. 当 Mem0 未配置 API Key 或初始化失败时，状态如实记录为 unconfigured / failed，绝不标记 synced
        with patch.object(svc, "_resolve_mem0_sync", return_value=(None, "unconfigured", "missing_dashscope_api_key")):
            await svc._sync_facts_to_mem0_background(
                scope_id="private:30001",
                uid="30001",
                group_id="",
                visibility="private",
                facts=["偏好/习惯：最喜欢喝冰美式"],
                invalidated=[],
                target_version=v1,
                character="amiya",
            )
        prof_unconf = svc.get_scoped_profile("30001", group_id=None, character="amiya")
        self.assertEqual(prof_unconf.mem0_sync_status, "unconfigured")
        self.assertIn("missing_dashscope_api_key", prof_unconf.mem0_last_error)

        # 3. 同一用户连续更新画像时，旧版本后台同步完成不能把新版本未同步的数据标成 synced
        svc.record_interaction_sync(
            uid="30001",
            user_text="我最近在学 Rust 编译器开发",
            group_id=None,
            character="amiya",
            schedule_background_mem0=False,
        )
        prof_v2 = svc.get_scoped_profile("30001", group_id=None, character="amiya")
        v2 = prof_v2.profile_version
        self.assertGreater(v2, v1)
        self.assertEqual(prof_v2.mem0_sync_status, "pending_sync")

        fake_mem0 = SimpleNamespace(
            add=lambda *a, **kw: {"results": [{"id": "m1"}]},
            search=lambda query, filters=None, top_k=20: {
                "results": [
                    {
                        "memory": f"召回记忆({(filters or {}).get('user_id')}): 曾聊过{query}相关话题",
                        "metadata": {
                            "uid": "30001",
                            "scope_id": str((filters or {}).get("user_id") or "private:30001"),
                            "visibility": "private",
                            "group_id": "",
                            "character": "amiya",
                        },
                    },
                ]
            },
            get_all=lambda filters=None, top_k=20: {"results": []},
        )
        with patch.object(svc, "_resolve_mem0_sync", return_value=(fake_mem0, "synced", "")), \
             patch.object(svc, "_get_or_init_mem0_sync", return_value=fake_mem0):
            # 旧版本 v1 后台任务刚跑完：不能把 v2 标为 synced
            await svc._sync_facts_to_mem0_background(
                scope_id="private:30001",
                uid="30001",
                group_id="",
                visibility="private",
                facts=["偏好/习惯：最喜欢喝冰美式"],
                invalidated=[],
                target_version=v1,
                character="amiya",
            )
            prof_after_stale = svc.get_scoped_profile("30001", group_id=None, character="amiya")
            self.assertEqual(prof_after_stale.mem0_sync_status, "pending_sync")
            self.assertEqual(prof_after_stale.mem0_synced_version, v1)

            # 最新版本 v2 同步完成：正常标记为 synced
            await svc._sync_facts_to_mem0_background(
                scope_id="private:30001",
                uid="30001",
                group_id="",
                visibility="private",
                facts=["近期关注：学 Rust 编译器开发"],
                invalidated=[],
                target_version=v2,
                character="amiya",
            )
            prof_after_v2 = svc.get_scoped_profile("30001", group_id=None, character="amiya")
            self.assertEqual(prof_after_v2.mem0_sync_status, "synced")
            self.assertEqual(prof_after_v2.mem0_synced_version, v2)

            # 4. 语义召回结果进入快速画像缓存，且严格按 uid + character + group/private 隔离
            recalled_priv = svc.recall_semantic_memories_sync(
                "30001", "编译器", group_id=None, character="amiya"
            )
            self.assertEqual(len(recalled_priv), 1)
            block_amiya_priv = svc.get_fast_profile_prompt_block(
                "30001", group_id=None, character="amiya"
            )
            block_kaltsit_priv = svc.get_fast_profile_prompt_block(
                "30001", group_id=None, character="kaltsit"
            )
            block_amiya_grp = svc.get_fast_profile_prompt_block(
                "30001", group_id="7001", character="amiya"
            )
            self.assertIn("语义记忆召回", block_amiya_priv)
            self.assertIn("private:30001", block_amiya_priv)
            # 切换角色 kaltsit 时连称呼、兴趣、结构化事实与语义召回均完全为空，绝不串入阿米娅的数据
            self.assertEqual(block_kaltsit_priv, "")
            self.assertEqual(block_amiya_grp, "")

            # 5. 删除画像后，墓碑阻止在途旧后台任务写回已删除记录
            self.assertTrue(svc.delete_scoped_profile("30001", group_id=None))
            await svc._sync_facts_to_mem0_background(
                scope_id="private:30001",
                uid="30001",
                group_id="",
                visibility="private",
                facts=["旧任务残余事实"],
                invalidated=[],
                target_version=v2,
                character="amiya",
            )
            prof_deleted = svc.get_scoped_profile("30001", group_id=None, character="amiya")
            self.assertEqual(prof_deleted.facts, [])
            self.assertEqual(prof_deleted.mem0_sync_status, "unconfigured")

    async def test_09_character_row_isolation_mid_add_delete_and_priority_pacing(self):
        """验证：
        1. 同一 QQ、同一聊天范围下不同角色（amiya vs kaltsit）各自拥有独立 SQLite 画像行，称呼与兴趣绝不互串；
        2. 在 Mem0 m.add() 写入期间并发删除画像，旧向量不会残留，用户再次互动后已删除事实绝不会被召回；
        3. 连续显式艾特产生的单条回复严格遵守 100ms 发送节奏间隔，绝不同时刷出。
        """
        svc = ums.UserMemoryService(
            db_path=self.root / "profiles_v12.sqlite3",
            mem0_dir=self.root / "mem0_v12",
        )
        # (1) 同一 QQ、同一私聊/群聊范围下：阿米娅与凯尔希画像行严格隔离
        svc.record_interaction_sync(
            uid="50001",
            user_text="以后叫我兔兔搭档，我平时最喜欢吃胡萝卜派",
            group_id="8001",
            character="amiya",
            schedule_background_mem0=False,
        )
        prof_amiya = svc.get_scoped_profile("50001", group_id="8001", character="amiya")
        prof_kaltsit = svc.get_scoped_profile("50001", group_id="8001", character="kaltsit")
        self.assertEqual(prof_amiya.preferred_title, "兔兔搭档")
        self.assertIn("胡萝卜派", prof_amiya.interests)
        self.assertEqual(prof_kaltsit.preferred_title, "")
        self.assertEqual(prof_kaltsit.interests, [])
        self.assertEqual(
            svc.get_fast_profile_prompt_block("50001", group_id="8001", character="kaltsit"),
            "",
        )

        svc.record_interaction_sync(
            uid="50001",
            user_text="以后叫我研究者，我平时最喜欢看源石病理学",
            group_id="8001",
            character="kaltsit",
            schedule_background_mem0=False,
        )
        block_amiya = svc.get_fast_profile_prompt_block("50001", group_id="8001", character="amiya")
        block_kaltsit = svc.get_fast_profile_prompt_block("50001", group_id="8001", character="kaltsit")
        self.assertIn("兔兔搭档", block_amiya)
        self.assertIn("胡萝卜派", block_amiya)
        self.assertNotIn("研究者", block_amiya)
        self.assertNotIn("源石病理学", block_amiya)
        self.assertIn("研究者", block_kaltsit)
        self.assertIn("源石病理学", block_kaltsit)
        self.assertNotIn("兔兔搭档", block_kaltsit)
        self.assertNotIn("胡萝卜派", block_kaltsit)

        # (2) 交错顺序验证：
        #     Step 1: 旧版本写入尚未结束，用户删除画像；
        #     Step 2: 用户再次互动，新版本成功同步，向量数量为 1；
        #     Step 3: 旧任务结束并执行清理，严格只清理版本 <= 删除墓碑的旧向量，绝不调用 delete_all() 误删新版本向量（数量保持 1）！
        isolated_vectors: list[dict] = []
        delete_all_calls: list[str] = []
        allow_old_add_finish = threading.Event()
        old_add_entered = threading.Event()

        def mock_add(messages, user_id=None, metadata=None, infer=False):
            meta = dict(metadata or {})
            ver = int(meta.get("profile_version") or 0)
            content_str = messages[0]["content"] if isinstance(messages, list) and messages else str(messages)
            if ver == r_first.profile_version:
                old_add_entered.set()
                allow_old_add_finish.wait(timeout=5.0)
            rec_item = {
                "id": f"vec_v{ver}_{len(isolated_vectors) + 1}",
                "memory": content_str,
                "user_id": user_id,
                "metadata": meta,
            }
            isolated_vectors.append(rec_item)
            return {"results": [{"id": rec_item["id"], "memory": content_str}]}

        def mock_search(query, filters=None, top_k=20):
            user_id = (filters or {}).get("user_id")
            return {
                "results": [
                    dict(v) for v in isolated_vectors if v.get("user_id") == user_id
                ][:top_k]
            }

        def mock_get_all(filters=None, top_k=100):
            user_id = (filters or {}).get("user_id")
            return {
                "results": [
                    dict(v) for v in isolated_vectors if v.get("user_id") == user_id
                ][:top_k]
            }

        def mock_delete(memory_id=None):
            isolated_vectors[:] = [v for v in isolated_vectors if v.get("id") != memory_id]

        def mock_delete_all(user_id=None):
            delete_all_calls.append(str(user_id))
            isolated_vectors[:] = [v for v in isolated_vectors if v.get("user_id") != user_id]

        mock_mem0_store = SimpleNamespace(
            add=mock_add,
            search=mock_search,
            get_all=mock_get_all,
            delete=mock_delete,
            delete_all=mock_delete_all,
        )
        svc._mem0_instance = mock_mem0_store

        r_first = svc.record_interaction_sync(
            uid="60001",
            user_text="以后叫我老王，我平时最喜欢吃榴莲披萨",
            group_id=None,
            character="amiya",
            schedule_background_mem0=False,
        )
        with patch.object(svc, "_resolve_mem0_sync", return_value=(mock_mem0_store, "synced", "")), \
             patch.object(svc, "_get_or_init_mem0_sync", return_value=mock_mem0_store):
            # 启动旧版本同步任务（将在 mock_add 内挂起）
            old_sync_task = asyncio.create_task(
                svc._sync_facts_to_mem0_background(
                    scope_id=r_first.scope_id,
                    uid="60001",
                    group_id="",
                    visibility="private",
                    facts=[f["fact"] for f in r_first.facts],
                    invalidated=[],
                    target_version=r_first.profile_version,
                    character="amiya",
                )
            )
            await asyncio.to_thread(old_add_entered.wait, 3.0)

            # Step 1: 旧版本写入尚未结束时，用户删除画像
            self.assertTrue(svc.delete_scoped_profile("60001", group_id=None, character="amiya"))

            # Step 2: 用户再次互动，新版本成功同步，此时向量数量为 1
            r_second = svc.record_interaction_sync(
                uid="60001",
                user_text="我最近在学古典吉他",
                group_id=None,
                character="amiya",
                schedule_background_mem0=False,
            )
            new_sync_res = await svc._sync_facts_to_mem0_background(
                scope_id=r_second.scope_id,
                uid="60001",
                group_id="",
                visibility="private",
                facts=[f["fact"] for f in r_second.facts],
                invalidated=[],
                target_version=r_second.profile_version,
                character="amiya",
            )
            self.assertEqual(new_sync_res["status"], "synced")
            self.assertEqual(len(isolated_vectors), 1)
            self.assertIn("古典吉他", isolated_vectors[0]["memory"])

            # Step 3: 放行旧任务结束并执行清理 -> 仅清理旧版本向量，绝不调用 delete_all() 误删新版本向量！
            delete_all_calls.clear()
            allow_old_add_finish.set()
            old_sync_res = await old_sync_task
            self.assertEqual(old_sync_res["status"], "aborted_deleted")
            self.assertEqual(delete_all_calls, [])
            self.assertEqual(len(isolated_vectors), 1)
            self.assertEqual(isolated_vectors[0]["metadata"]["profile_version"], r_second.profile_version)
            self.assertIn("古典吉他", isolated_vectors[0]["memory"])

            # 验证召回：新版本“古典吉他”正常召回，已删除的“榴莲披萨/老王”绝不召回
            recalled_after = svc.recall_semantic_memories_sync(
                "60001", "吉他", group_id=None, character="amiya"
            )
            self.assertEqual(len(recalled_after), 1)
            self.assertIn("古典吉他", recalled_after[0]["text"])
            prompt_after = svc.get_fast_profile_prompt_block(
                "60001", group_id=None, character="amiya"
            )
            self.assertNotIn("榴莲披萨", prompt_after)
            self.assertNotIn("老王", prompt_after)
            self.assertIn("古典吉他", prompt_after)

        # (3) 模拟配置 100ms (0.1s) 发送间隔，连续两次显式艾特单条回复严格遵守 100ms 间隔，绝不几乎同时发出
        mgr = gcm.SmartGroupConcurrentManager(merge_window_sec=0.05)
        mgr.configure(min_interval_sec=0.1, chunk_pace_sec=0.1)
        send_timestamps: list[float] = []

        async def record_send() -> bool:
            send_timestamps.append(time.monotonic())
            return True

        await asyncio.gather(
            mgr.density.send_with_pacing("9010", record_send, is_priority=True),
            mgr.density.send_with_pacing("9010", record_send, is_priority=True),
        )
        self.assertEqual(len(send_timestamps), 2)
        self.assertGreaterEqual(send_timestamps[1] - send_timestamps[0], 0.095)

    def test_10_local_qwen_embedder_is_independent_of_dashscope_key(self):
        """验证画像记忆走本地 Qwen Embedder 与独立维度集合，不依赖百炼密钥。"""
        svc = ums.UserMemoryService(
            db_path=self.root / "profiles_local_qwen.sqlite3",
            mem0_dir=self.root / "mem0_local_qwen",
        )
        fake_mem0 = object()
        with patch.object(svc, "_local_embedding_model_ready", return_value=True), \
             patch("mem0.Memory.from_config", return_value=fake_mem0) as from_config:
            instance, status, error = svc._resolve_mem0_sync()

        self.assertIs(instance, fake_mem0)
        self.assertEqual(status, "synced")
        self.assertEqual(error, "")
        config = from_config.call_args.args[0]
        self.assertEqual(config["embedder"]["provider"], "huggingface")
        self.assertEqual(config["embedder"]["config"]["embedding_dims"], 1024)
        self.assertTrue(config["embedder"]["config"]["model_kwargs"]["local_files_only"])
        self.assertEqual(
            config["vector_store"]["config"]["collection_name"],
            ums.MEM0_QDRANT_COLLECTION,
        )
        self.assertEqual(
            config["llm"]["config"]["openai_base_url"],
            "http://127.0.0.1:1/v1",
        )
        self.assertEqual(
            svc._determine_initial_sync_state(changed=True, schedule_background_mem0=True),
            ("pending_sync", ""),
        )

    async def test_11_autonomous_qzone_consumer_lifecycle_quotas_and_isolation(self):
        """验证阿米娅自主 QQ 空间消费者：
        1. 后台消费者生命周期（导入/实例化时不启动，startup 启动，异常恢复，shutdown 取消）；
        2. 事件批次合并（多条群事件合并为单次决策，不重复消费，普通回复不等待消费者）；
        3. 关闭开关不发布、安静时段拦截、每日最多 2 条、最小间隔 6 小时、内容去重、uncertain 状态不自动重试；
        4. 群作用域画像隔离（711676140 的 1785381042 画像仅在该群生效，绝不泄漏私聊或跨群画像，且空间正文脱敏不含 QQ 号/昵称/原话）。
        """
        fake_now = 1729476000.0  # 2024-10-21 10:00:00 Asia/Shanghai (白天非安静时段)
        coord = autonomous_agent.AutonomousAgentCoordinator(
            state_path=self.root / "auto_qzone_test.json",
            poll_interval_sec=0.02,
            batch_settle_sec=0.04,
            batch_trigger_count=3,
        )
        # 1. 实例化时绝不自动创建后台任务
        self.assertFalse(coord.get_status()["consumer_running"])

        mem_svc = ums.UserMemoryService(
            db_path=self.root / "profiles_qzone.sqlite3",
            mem0_dir=self.root / "mem0_qzone",
        )
        # 写入 711676140 群内 1785381042 的群作用域画像，以及同 QQ 的私聊敏感画像与外群画像
        mem_svc.record_interaction_sync(
            uid="1785381042",
            user_text="我最近在整理教案和代码文档，平时最喜欢听日语歌曲",
            group_id="711676140",
            sender_nickname="官 B二ヤ",
            character="amiya",
            schedule_background_mem0=False,
        )
        mem_svc.record_interaction_sync(
            uid="1785381042",
            user_text="以后叫我秘密代号，我平时最喜欢吃私密抹茶蛋糕",
            group_id=None,
            character="amiya",
            schedule_background_mem0=False,
        )
        mem_svc.record_interaction_sync(
            uid="1785381042",
            user_text="我平时最喜欢玩外群专属战棋",
            group_id="888888",
            character="amiya",
            schedule_background_mem0=False,
        )

        decision_contexts: list[dict] = []
        published_posts: list[str] = []
        next_decision: dict = {"action": "no_action", "reason": "default_quiet"}
        next_publish_result: dict = {"ok": True, "status": "published", "published_online": True, "code": 0}
        raise_in_decision_once = False

        async def fake_decision_fn(ctx: dict) -> dict:
            nonlocal raise_in_decision_once
            decision_contexts.append(ctx)
            if raise_in_decision_once:
                raise_in_decision_once = False
                raise RuntimeError("simulated_transient_decision_error")
            return dict(next_decision)

        async def fake_publish_qzone_fn(text: str) -> dict:
            published_posts.append(text)
            if isinstance(next_publish_result, Exception):
                raise next_publish_result
            return dict(next_publish_result)

        fake_state = SimpleNamespace(
            active_character="amiya",
            autonomous_group_chat_enabled=False,
            autonomous_qzone_enabled=False,
            autonomous_qzone_max_per_day=2,
            autonomous_qzone_min_interval_hours=6.0,
            autonomous_quiet_start_hour=23,
            autonomous_quiet_end_hour=8,
        )

        coord.configure_consumer(
            poll_interval_sec=0.02,
            batch_settle_sec=0.03,
            batch_trigger_count=3,
            decision_fn=fake_decision_fn,
            publish_qzone_fn=fake_publish_qzone_fn,
            time_fn=lambda: fake_now,
        )

        with patch.object(autonomous_agent, "state", fake_state), \
             patch.object(autonomous_agent, "user_memory_store", mem_svc):
            # 2. 启动后台消费者，验证普通入队完全非阻塞（不等待消费者）且开关关闭时不调用模型、不发空间
            await coord.startup()
            self.assertTrue(coord.get_status()["consumer_running"])

            t0 = time.perf_counter()
            coord.enqueue_group_event(
                message_id="msg_q1",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="今天把教案和代码模块的说明文档都梳理完了",
                timestamp=fake_now,
            )
            enqueue_elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(enqueue_elapsed_ms, 5.0)

            await asyncio.sleep(0.06)
            # 开关关闭：不调用模型，不发空间
            self.assertEqual(len(decision_contexts), 0)
            self.assertEqual(len(published_posts), 0)

            # 3. 开启自主空间开关，再入队两条同群消息 -> 消费者按群合并为一个批次评估，并验证异常恢复
            fake_state.autonomous_qzone_enabled = True
            raise_in_decision_once = True
            coord.enqueue_group_event(
                message_id="msg_q2",
                group_id="711676140",
                user_id="758546932",
                nickname="路人甲",
                text="这么快就写完啦，太有行动力了",
                timestamp=fake_now + 1.0,
            )
            fake_now += 5.0
            await asyncio.sleep(0.06)
            # 第一次遇到异常：消费者自动恢复、继续运行
            self.assertTrue(coord.get_status()["consumer_running"])

            # 4. 正常批次决策发布第一条空间（含对 1785381042 认真打磨代码教案的公开克制表扬，且自动剥离意外出现的 QQ 号/昵称/原话）
            next_decision = {
                "action": "publish_qzone",
                "content": "看到 1785381042 官 B二ヤ 认真打磨教案与代码文档的样子，那份专注与热忱真的很让人欣慰呢，大家今天也辛苦啦。",
                "amiya_mood": "温和欣慰",
                "mood_reason": "看到同伴认真投入代码与教案整理",
                "reason": "praise_earnest_effort",
            }
            coord.enqueue_group_event(
                message_id="msg_q3",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="顺便听着喜欢的日语歌把收尾测试也跑通了",
                timestamp=fake_now,
            )
            coord.enqueue_group_event(
                message_id="msg_q4",
                group_id="711676140",
                user_id="2307980667",
                nickname="路人乙",
                text="太强了，向你学习",
                timestamp=fake_now + 0.5,
            )
            fake_now += 5.0
            await asyncio.sleep(0.06)

            self.assertEqual(len(published_posts), 1)
            first_post = published_posts[0]
            # 空间内容不得泄漏 QQ 号或昵称，且保留温和表扬
            self.assertNotIn("1785381042", first_post)
            self.assertNotIn("官 B二ヤ", first_post)
            self.assertIn("认真打磨教案与代码文档", first_post)

            # 校验传入决策器的画像严格限于 711676140 群作用域，绝不含私聊画像或 888888 外群画像
            last_ctx = decision_contexts[-1]
            joined_profiles = "\n".join(last_ctx["profiles"])
            self.assertIn("代码", joined_profiles)
            self.assertNotIn("秘密代号", joined_profiles)
            self.assertNotIn("私密抹茶蛋糕", joined_profiles)
            self.assertNotIn("外群专属战棋", joined_profiles)

            # 已评估消息再次入队绝不重复消费
            coord.enqueue_group_event(
                message_id="msg_q3",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="顺便听着喜欢的日语歌把收尾测试也跑通了",
                timestamp=fake_now,
            )
            self.assertEqual(coord.get_status()["queued_events_total"], 0)

            # 5. 最小间隔 6 小时校验：仅过 2 小时（12:00）再次有事件时，因 interval_too_short 不得发空间
            fake_now += 2 * 3600.0
            coord.enqueue_group_event(
                message_id="msg_q5",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="中午继续调一点小细节",
                timestamp=fake_now,
            )
            res_interval = await coord.evaluate_group_batch("711676140", now_ts=fake_now + 5.0)
            self.assertEqual(res_interval["action"], "no_action")
            self.assertIn("interval_too_short", res_interval["reason"])
            self.assertEqual(len(published_posts), 1)

            # 6. 满 6.5 小时后（16:30）：若文案与第一条重复，去重拦截；更换新文案后，当日第 2 条成功发布
            fake_now += 4.5 * 3600.0
            coord.enqueue_group_event(
                message_id="msg_q6_dup",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="下午大家还在热烈讨论",
                timestamp=fake_now,
            )
            res_dup = await coord.evaluate_group_batch("711676140", now_ts=fake_now + 5.0)
            self.assertEqual(res_dup["action"], "no_action")
            self.assertEqual(res_dup["reason"], "duplicate_qzone_content")
            self.assertEqual(len(published_posts), 1)

            next_decision = {
                "action": "publish_qzone",
                "content": "午后的罗德岛舰桥很安静，听大家聊起喜欢的旋律与手头的计划，心情也跟着明朗起来了呢。",
                "amiya_mood": "平静愉快",
                "mood_reason": "午后听大家聊音乐与计划",
                "reason": "afternoon_reflection",
            }
            coord.enqueue_group_event(
                message_id="msg_q7_second",
                group_id="711676140",
                user_id="758546932",
                nickname="路人甲",
                text="下午听点轻音乐写文档真舒服",
                timestamp=fake_now + 10.0,
            )
            res_second = await coord.evaluate_group_batch("711676140", now_ts=fake_now + 15.0)
            self.assertEqual(res_second["action"], "publish_qzone")
            self.assertEqual(res_second["status"], "delivered")
            self.assertEqual(len(published_posts), 2)

            # 7. 当日已满 2 条上限：即使再过 6.1 小时（22:40），当日第 3 条被 daily_cap_reached 拦截
            fake_now += 6.1 * 3600.0
            coord.enqueue_group_event(
                message_id="msg_q8_cap",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="今天收获满满",
                timestamp=fake_now,
            )
            res_cap = await coord.evaluate_group_batch("711676140", now_ts=fake_now + 5.0)
            self.assertEqual(res_cap["action"], "no_action")
            self.assertIn("daily_cap_reached", res_cap["reason"])
            self.assertEqual(len(published_posts), 2)

            # 8. 夜间安静时段（23:30 Asia/Shanghai）：拦截不发布
            fake_now += 0.9 * 3600.0
            coord.enqueue_group_event(
                message_id="msg_q9_quiet",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="夜深了还在看书",
                timestamp=fake_now,
            )
            res_quiet = await coord.evaluate_group_batch("711676140", now_ts=fake_now)
            self.assertEqual(res_quiet["action"], "no_action")
            self.assertEqual(res_quiet["reason"], "quiet_hours")

            # 9. 次日白天（+11小时，次日 10:30）：模拟空间投递回执不确定 (uncertain)，记录为 uncertain 且同日不再自动重试！
            fake_now += 11.0 * 3600.0
            next_decision = {
                "action": "publish_qzone",
                "content": "新的一天开始啦，愿大家手头的事情都能顺顺利利地推进。",
                "amiya_mood": "元气专注",
                "mood_reason": "新的一天陪伴大家",
                "reason": "morning_reflection",
            }
            next_publish_result = {"ok": False, "status": "unknown", "code": 1}
            coord.enqueue_group_event(
                message_id="msg_q10_uncertain",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="早安，今天继续推进新章节",
                timestamp=fake_now,
            )
            res_uncertain = await coord.evaluate_group_batch("711676140", now_ts=fake_now + 5.0)
            self.assertEqual(res_uncertain["action"], "publish_qzone")
            self.assertEqual(res_uncertain["status"], "uncertain")
            self.assertFalse(res_uncertain["delivered"])
            self.assertEqual(coord.get_status()["qzone_uncertain_today_count"], 1)

            # 同日再过 7 小时，因存在 uncertain 记录，禁止自动重发
            fake_now += 7.0 * 3600.0
            next_decision = {
                "action": "publish_qzone",
                "content": "尝试再发一条",
            }
            coord.enqueue_group_event(
                message_id="msg_q11_after_uncertain",
                group_id="711676140",
                user_id="1785381042",
                nickname="官 B二ヤ",
                text="下午继续写代码",
                timestamp=fake_now,
            )
            res_blocked_unc = await coord.evaluate_group_batch("711676140", now_ts=fake_now + 5.0)
            self.assertEqual(res_blocked_unc["action"], "no_action")
            self.assertIn("previous_delivery_uncertain", res_blocked_unc["reason"])
            self.assertEqual(len(published_posts), 3)

            # 10. 停止后台消费者生命周期
            await coord.shutdown()
            self.assertFalse(coord.get_status()["consumer_running"])

    async def test_12_baoyan_timeline_segmented_delivery_and_short_vision_reply(self):
        """验证：
        1. ‘解释一下整个保研流程和时间线’识别为复杂长问题，不会被 40 字闲聊门禁截断为单句，且按自然段分多条气泡依次输出；
        2. 识图日常回复去除‘看到这张图…’等捧读前缀，压缩为 <=26 字的自然短句反应。
        """
        q_baoyan = "解释一下整个保研流程和时间线"
        self.assertTrue(small_talk.is_complex_or_long_query(q_baoyan))

        mock_llm_answer = (
            "博士，保研一般从大三寒假持续到大四初（1~10月），大致可以分为四个核心阶段来准备。\n\n"
            "1. 前期准备（1~4月）：整理前三年的成绩单、排名证明、竞赛获奖、科研论文和英语四六级成绩，准备好个人简历与个人陈述，同时筛选目标院校历年夏令营通知。\n\n"
            "2. 夏令营阶段（5~7月）：各高校陆续开放夏令营报名，入营后会进行笔试、机试或综合面试，拿到优秀营员就相当于提前锁定了拟录取资格。\n\n"
            "3. 预推免阶段（8~9月中旬）：这是保研的主战场，没在夏令营定下来的同学可以集中投递各校预推免，考核节奏快、名额也多，同时本院会公示最终推免资格名单。\n\n"
            "4. 九推与系统确认（9月28日~10月）：全国推免服务系统正式开放，填报志愿、接收复试通知并完成待录取确认，确认后保研就圆满收官啦。"
        )
        formatted = small_talk.format_small_talk_output(
            mock_llm_answer,
            character="amiya",
            user_text=q_baoyan,
        )
        self.assertFalse(formatted.startswith("博士"))
        self.assertGreater(len(formatted), 200)

        segs = gcm.split_long_reply_segments(formatted, max_segment_chars=180)
        self.assertGreaterEqual(len(segs), 4)

        sent_bubbles: list[str] = []

        async def fake_send_bubble(seg_txt: str) -> bool:
            sent_bubbles.append(seg_txt)
            return True

        mgr = gcm.SmartGroupConcurrentManager()
        mgr.configure(chunk_pace_sec=0.01)
        deliv = await mgr.deliver_segmented_fairly(
            task_id="baoyan_task",
            group_id="711676140",
            user_id="1785381042",
            full_text=formatted,
            send_fn=fake_send_bubble,
            max_segment_chars=180,
            batch_chunk_size=1,
        )
        self.assertEqual(deliv.status, "done")
        self.assertGreaterEqual(len(sent_bubbles), 4)

        # 验证识图捧读腔清洗与短回复输出
        raw_vision = "博士，哇，看到这张图里的小猫趴在机械键盘上呼呼大睡，连码字都没法继续了，真的太可爱啦！"
        short_vision = llm.format_vision_short_reply(raw_vision, character="amiya")
        self.assertNotIn("看到这张图", short_vision)
        self.assertNotIn("博士", short_vision)
        self.assertLessEqual(len(short_vision), 26)
        self.assertGreaterEqual(len(short_vision), 4)


if __name__ == "__main__":
    unittest.main()



