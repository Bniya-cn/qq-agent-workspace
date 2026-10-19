"""仅在进程启动时构建服务单例；配置由原 state 原位更新，源码更新必须重启。"""
from dataclasses import dataclass
from types import ModuleType

from . import conversation_service, doc_service, document_writer, llm_service, tts_service
from .bailian_file_renderer import render_files
from .config_manager import state
from .history_recorder import recorder
from .message_pipeline import is_pure_vision_qa_query

_raw_detect_draw = llm_service._auto_detect_draw_intent


def _guarded_detect_draw_intent(text: str, has_image: bool = False) -> bool:
    clean = llm_service._strip_reply_context_wrapper(text or "").strip()
    if is_pure_vision_qa_query(clean):
        return False
    return _raw_detect_draw(clean, has_image=has_image)


llm_service._auto_detect_draw_intent = _guarded_detect_draw_intent


@dataclass(frozen=True)
class ServiceRegistry:
    conversation: ModuleType
    documents: ModuleType
    writer: ModuleType
    llm: ModuleType
    tts: ModuleType

    def validate(self) -> None:
        for module, entry in ((self.conversation, "use_session"), (self.documents, "prepare_documents"),
                              (self.writer, "generate"), (self.llm, "chat_operator"), (self.tts, "synthesize")):
            if not callable(getattr(module, entry, None)):
                raise RuntimeError(f"服务初始化失败: {module.__name__}.{entry}")


services = ServiceRegistry(conversation_service, doc_service, document_writer, llm_service, tts_service)
services.validate()
doc_service.configure_file_renderer(render_files)
