"""双工作模式分包：
- 闲聊与表达组装 (small_talk)：固定人格优先、场景表达参考、12字/40字日常门禁与复杂问题完整回答直通；
- 训练模式与异步任务队列 (training_mode)：真实引用关系配对、防机器人自污染、受控后台任务队列与状态查询。
"""
from __future__ import annotations

from .small_talk import (
    SMALL_TALK_MAX_CHARS,
    SMALL_TALK_QUESTION_MAX_CHARS,
    build_small_talk_system_prompt,
    format_small_talk_output,
    is_complex_or_long_query,
    is_explicit_user_question,
    normalize_qq_linebreaks,
    strip_mechanical_doctor_prefix,
)
from .training_mode import (
    TRAINING_DIR,
    fetch_and_train_group_history,
    get_training_status,
    get_training_status_async,
    is_training_group,
    record_group_message_for_training,
    training_job_manager,
    trigger_group_training_now,
)

__all__ = [
    "SMALL_TALK_MAX_CHARS",
    "SMALL_TALK_QUESTION_MAX_CHARS",
    "TRAINING_DIR",
    "build_small_talk_system_prompt",
    "fetch_and_train_group_history",
    "format_small_talk_output",
    "get_training_status",
    "get_training_status_async",
    "is_complex_or_long_query",
    "is_explicit_user_question",
    "is_training_group",
    "normalize_qq_linebreaks",
    "record_group_message_for_training",
    "strip_mechanical_doctor_prefix",
    "training_job_manager",
    "trigger_group_training_now",
]
