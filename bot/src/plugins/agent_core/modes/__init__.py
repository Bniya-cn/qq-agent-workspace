"""双新增工作模式分包：
- 闲聊模式 (small_talk)：模仿群聊“路易斯”风格，极短口语（1~12字）、去标点、去空白双换行；
- 训练模式 (training_mode)：自动采集指定 QQ 群全量对话，生成微调数据集 (.jsonl) 并自动提炼更新群人设与黑话提示词。
"""
from __future__ import annotations

from .small_talk import (
    SMALL_TALK_MAX_CHARS,
    build_small_talk_system_prompt,
    format_small_talk_output,
    normalize_qq_linebreaks,
)
from .training_mode import (
    TRAINING_DIR,
    fetch_and_train_group_history,
    get_training_status,
    is_training_group,
    record_group_message_for_training,
    trigger_group_training_now,
)

__all__ = [
    "SMALL_TALK_MAX_CHARS",
    "TRAINING_DIR",
    "build_small_talk_system_prompt",
    "fetch_and_train_group_history",
    "format_small_talk_output",
    "get_training_status",
    "is_training_group",
    "normalize_qq_linebreaks",
    "record_group_message_for_training",
    "trigger_group_training_now",
]
