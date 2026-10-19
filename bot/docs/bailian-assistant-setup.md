# 通用联网助手（百炼）

与阿米娅应用**分开**建。控制台打开「联网搜索」，提示词用通用助手，不要挂明日方舟人设。

```
DASHSCOPE_API_KEY=...
DASHSCOPE_APP_ID_ASSISTANT=应用ID
```

暂无专用 App 也可以：有 `DASHSCOPE_API_KEY` 时走千问 `enable_search`。

代码：`llm_service.chat_assistant(user)`；粗意图：`looks_like_web_intent(text)`。
