"""业务异常携带可直接展示的提示；底层原因通过异常链保留在日志中。"""


class AgentCoreError(Exception):
    user_message = "处理请求时遇到异常，请稍后重试。"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.user_message)


class DocumentGenerationError(AgentCoreError):
    user_message = "文档生成或上传失败，已保存的正文可回复“导出”重试。"


class DrawPipelineError(AgentCoreError):
    user_message = "图片生成或下载失败，请稍后重试；本次没有成功交付图片。"


class TTSGenerationError(AgentCoreError):
    user_message = "语音服务暂不可用，已改用文字回复。"


class LLMProviderError(AgentCoreError):
    user_message = "模型服务暂未返回有效回答，请稍后重试。"


class MessageDeliveryError(AgentCoreError):
    user_message = "QQ 消息投递未获成功确认，请检查聊天记录后重试。"
