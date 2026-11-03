"""静态服务注册表：启动时一次性绑定并校验核心服务单例。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import (  # noqa: E402
    bailian_file_renderer,
    conversation_service,
    doc_service,
    document_writer,
    llm_service,
    tts_service,
)
from .config_manager import state  # noqa: E402
from .history_recorder import recorder  # noqa: E402


@dataclass(frozen=True)
class ServiceRegistry:
    state: Any
    recorder: Any
    conversation: Any
    documents: Any
    writer: Any
    llm: Any
    tts: Any

    def validate(self) -> "ServiceRegistry":
        required_callables = (
            ("conversation.store.begin", getattr(getattr(self.conversation, "store", None), "begin", None)),
            ("documents.prepare_documents", getattr(self.documents, "prepare_documents", None)),
            ("writer.generate", getattr(self.writer, "generate", None)),
            ("llm.chat_operator", getattr(self.llm, "chat_operator", None)),
            ("tts.synthesize", getattr(self.tts, "synthesize", None)),
        )
        for name, fn in required_callables:
            if not callable(fn):
                raise RuntimeError(f"ServiceRegistry 校验失败: {name} 不可调用")
        return self


# 默认直接使用本地渲染器导出 docx/pdf/md，不再调用百炼文件制作 App
services = ServiceRegistry(
    state=state,
    recorder=recorder,
    conversation=conversation_service,
    documents=doc_service,
    writer=document_writer,
    llm=llm_service,
    tts=tts_service,
).validate()

SERVICES = services
