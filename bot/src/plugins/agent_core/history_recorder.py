"""运行历史记录器：内存保存最近关键日志、AI 对话结果与实时生成进度（供控制端面板展示）。"""
from __future__ import annotations

import collections
import datetime
import time as _time_mod
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from nonebot import logger


class TaskProgressTracker:
    """记录文字、生图、语音、文档导出各阶段实时进度与完整未截断信息，供 /api/progress 与前端面板轮询展示。"""

    def __init__(self, max_items: int = 30) -> None:
        self.max_items = max_items
        self.tasks: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []

    def update(
        self,
        task_id: str,
        *,
        kind: str | None = None,          # "text" | "image" | "voice" | "doc"
        title: str = "",
        stage: str = "",
        percent: int | None = None,
        status: str | None = None,   # pending -> running -> done/error
        source: str = "",
        input_text: str = "",
        prompt_text: str = "",
        output_text: str = "",
        extra_info: str = "",
    ) -> None:
        now = _time_mod.time()
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        item = self.tasks.get(task_id)
        if item and item["status"] in {"done", "error"}:
            return
        if status is not None and status not in {"pending", "running", "done", "error"}:
            raise ValueError(f"非法任务状态: {status}")
        if item and item["status"] == "running" and status == "pending":
            raise ValueError("运行中的任务不能退回等待")
        if not item:
            item = {
                "task_id": task_id,
                "kind": kind or "text",
                "title": title or kind or "text",
                "stage": stage,
                "percent": int(percent or 0),
                "status": status or "pending",
                "source": source,
                "input_text": input_text,
                "prompt_text": prompt_text,
                "output_text": output_text,
                "extra_info": extra_info,
                "start_ts": now,
                "updated_at": now_str,
                "elapsed_s": 0.0,
                "steps": [],
            }
            self.tasks[task_id] = item
            self.order.append(task_id)
            for old_id in list(self.order):
                if len(self.order) <= self.max_items:
                    break
                old_item = self.tasks.get(old_id)
                if old_item is None:
                    # 序列可能残留已被其他清理路径移除的任务 ID；跳过它，避免新消息记录失败。
                    self.order.remove(old_id)
                    continue
                if old_item["status"] in {"done", "error"}:
                    self.order.remove(old_id)
                    self.tasks.pop(old_id, None)
        else:
            if kind:
                item["kind"] = kind
            if title:
                item["title"] = title
            if stage:
                item["stage"] = stage
            if percent is not None:
                item["percent"] = int(percent)
            if status:
                item["status"] = status
            if source:
                item["source"] = source
            if input_text:
                item["input_text"] = input_text
            if prompt_text:
                item["prompt_text"] = prompt_text
            if output_text:
                item["output_text"] = output_text
            if extra_info:
                item["extra_info"] = extra_info
            item["updated_at"] = now_str
            item["elapsed_s"] = round(max(0.0, now - float(item.get("start_ts", now))), 1)

        if stage:
            steps = item.get("steps") or []
            if not steps or steps[-1].get("stage") != stage:
                steps.append({"time": now_str, "stage": stage, "percent": item["percent"]})
                item["steps"] = steps[-12:]

    def list_recent(self) -> list[dict[str, Any]]:
        now = _time_mod.time()
        out = []
        for tid in reversed(self.order):
            it = self.tasks.get(tid)
            if not it:
                continue
            copy_it = dict(it)
            if copy_it.get("status") == "running":
                copy_it["elapsed_s"] = round(max(0.0, now - float(copy_it.get("start_ts", now))), 1)
            out.append(copy_it)
        return out


progress_tracker = TaskProgressTracker()
current_task_id: ContextVar[str | None] = ContextVar("agent_task_id", default=None)


@contextmanager
def task_scope(task_id: str):
    token = current_task_id.set(task_id)
    try:
        yield
    finally:
        current_task_id.reset(token)


class HistoryRecorder:
    def __init__(self, max_logs: int = 40, max_dialogues: int = 20):
        self._logs: collections.deque[dict[str, str]] = collections.deque(maxlen=max_logs)
        self._dialogues: collections.deque[dict[str, Any]] = collections.deque(maxlen=max_dialogues)
        self.progress_tracker = progress_tracker

    def log(self, text: str, level: str = "INFO") -> None:
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self._logs.append({"time": now, "level": level, "text": text})
        if level.upper() in {"WARN", "WARNING"}:
            logger.warning(text)
        elif level.upper() == "ERROR":
            logger.error(text)
        else:
            logger.info(text)

    def record_dialogue(
        self,
        *,
        source: str,
        sender_id: str,
        user_text: str,
        character: str,
        reply: str,
        voice: bool = False,
    ) -> None:
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self._dialogues.append(
            {
                "time": now,
                "source": source,
                "sender_id": sender_id,
                "user_text": user_text,
                "character": character,
                "reply": reply,
                "voice": voice,
            }
        )

    def get_logs(self) -> list[dict[str, str]]:
        return list(self._logs)

    def get_dialogues(self) -> list[dict[str, Any]]:
        return list(self._dialogues)


recorder = HistoryRecorder()
