"""校验百炼优先、本地补齐、产物完整性和按 QQ 保存的边界。"""
import asyncio
import io
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
nonebot.init()
from src.plugins.agent_core import bailian_file_renderer as cloud, conversation_service as conv, doc_service as docs
from src.plugins.agent_core import document_writer as writer
import httpx


class FilePipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = conv.ConversationStore(self.root / "sessions.db")
        for item in (patch.object(conv, "store", self.store), patch.object(docs, "EXPORTS_DIR", self.root / "exports"),
                     patch.object(docs, "TMP_EXPORT_DIR", self.root / "tmp")):
            item.start()
            self.addCleanup(item.stop)

    async def test_cloud_artifact_reused_without_regenerating_body(self):
        renderer = AsyncMock(return_value={"md": "完整正文测试".encode()})
        session = self.store.begin("12345")
        with patch.object(docs, "_file_renderer", renderer), conv.use_session(session):
            first = await docs.prepare_documents("标题", "完整正文测试", ["md"])
            second = await docs.prepare_documents("标题", "完整正文测试", ["md"])
        self.assertEqual(first, second)
        self.assertEqual(renderer.await_count, 1)
        self.assertEqual(session.document["files"][0]["renderer"], "bailian")

    async def test_cloud_failure_falls_back_to_real_local_file(self):
        with patch.object(docs, "_file_renderer", AsyncMock(side_effect=TimeoutError)):
            paths = await docs.prepare_documents("标题", "完整正文测试", ["md"])
        self.assertEqual(len(paths), 1)
        self.assertIn("完整正文测试", paths[0].read_text())

    def test_cloud_does_not_accept_html_or_incomplete_text(self):
        self.assertFalse(cloud._valid_content(b"<html>error</html>", "md", "完整正文测试"))
        self.assertFalse(cloud._valid_content("正文丢失".encode(), "md", "完整正文测试"))
        self.assertTrue(cloud._valid_content("# 标题\n完整正文测试".encode(), "md", "完整正文测试"))
        self.assertFalse(cloud._valid_content(b"PKinvalid", "docx", "完整正文测试"))

    def test_download_rejects_local_target(self):
        for url in ("http://example.org/x", "https://127.0.0.1/x", "https://user:password@example.org/x"):
            with self.assertRaises(Exception):
                cloud._validate_url(url)

    def test_24h_expiry_is_based_on_user_activity(self):
        self.assertEqual(conv.TTL_SECONDS, 86400)
        now = time.time()
        with patch.object(conv.time, "time", return_value=now):
            session = self.store.begin("12345")
            self.store.remember(session, "问题", "回答", document={"content": "完整正文"})
        with patch.object(conv.time, "time", return_value=now + 86399):
            self.assertEqual(self.store.begin("67890").history, [])
            self.store.remember(session, "追加", "机器人回答不会延长用户时间")
        with patch.object(conv.time, "time", return_value=now + 86400):
            restored = self.store.begin("12345")
            self.assertNotEqual(session.session_id, restored.session_id)
            self.assertIsNone(restored.document)
            self.assertFalse(restored.history)

    async def test_cancelled_waiter_does_not_leak_user_lock(self):
        async with self.store.user_turn("12345"):
            async def wait():
                async with self.store.user_turn("12345"):
                    raise AssertionError("不应抢到锁")
            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        self.assertFalse(self.store._locks)

    async def test_long_truncated_body_cannot_be_complete(self):
        session = self.store.begin("12345")
        response = httpx.Response(200, json={"choices": [{"message": {"content": "未完成正文" * 100}, "finish_reason": "length"}]})
        client = httpx.AsyncClient
        with conv.use_session(session), patch.object(writer, "_build_candidate_endpoints", return_value=[("test", "https://test", "test")]), patch.object(writer, "MAX_REQUESTS", 1), patch.object(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(lambda req: response), **kw)):
            with self.assertRaises(writer.DocumentGenerationError):
                await writer.generate("输出docx", ["docx"])
        self.assertFalse(session.document["complete"])
        self.assertGreater(len(session.document["content"]), 200)

    async def test_stream_without_stop_cannot_be_complete(self):
        session = self.store.begin("12345")
        content = 'data: {"choices":[{"delta":{"content":"' + "未完成正文" * 50 + '"}}]}\n\ndata: [DONE]\n\n'
        client = httpx.AsyncClient
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())
        with conv.use_session(session), patch.object(writer, "_build_candidate_endpoints", return_value=[("test", "https://test", "test")]), patch.object(writer, "MAX_REQUESTS", 1), patch.object(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(lambda req: response), **kw)):
            with self.assertRaises(writer.DocumentGenerationError):
                await writer.generate("输出docx", ["docx"])
        self.assertFalse(session.document["complete"])

    async def test_file_without_destination_cannot_report_sent(self):
        path = self.root / "file.md"
        path.write_text("正文")
        self.assertFalse(await docs.send_file_to_qq(None, None, path))
