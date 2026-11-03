"""业务异常：底层原因通过异常链保留在后台日志中；前台绝不输出机械客服报错词。"""


class AgentCoreError(Exception):
    user_message = "唔…刚才网络晃了一下，你刚刚说的什么呀"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.user_message)


class DocumentGenerationError(AgentCoreError):
    user_message = "这份文档刚刚整理到一半卡住啦，草稿我还留着，跟我说声“导出”就能接着弄完"


class DrawPipelineError(AgentCoreError):
    user_message = "唔…刚才画笔不小心掉地上了，等我缓一下再帮你画好不好"


class TTSGenerationError(AgentCoreError):
    user_message = "语音生成失败，本轮未发送语音。"


class LLMProviderError(AgentCoreError):
    user_message = "唔…刚才网络晃了一下，你刚刚说的什么呀"


class MessageDeliveryError(AgentCoreError):
    user_message = "刚才消息好像没发出去，我再试一下"
