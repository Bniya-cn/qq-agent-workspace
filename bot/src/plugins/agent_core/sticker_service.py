"""阿米娅本地表情包目录、模型决策标记与延迟投递。"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import random
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment

from .config_manager import BOT_ROOT


_PACK_DIR = BOT_ROOT / "data" / "stickers" / "amiya"
_MANIFEST_PATH = _PACK_DIR / "manifest.json"
_COMMUNITY_MANIFEST_PATH = _PACK_DIR / "community" / "manifest.json"
_RECEIVED_MANIFEST_PATH = _PACK_DIR / "received" / "manifest.json"
_CONTROL_RE = re.compile(r"\[\[AMIYA_STICKER:([a-z0-9_-]+)\]\]", re.IGNORECASE)
_CONTROL_LIKE_RE = re.compile(r"\[\[AMIYA_STICKER:[^\]]*\]\]", re.IGNORECASE)
_CURRENT_DECISION: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "amiya_sticker_decision", default=None
)


def _read_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for manifest_path in (_MANIFEST_PATH, _COMMUNITY_MANIFEST_PATH, _RECEIVED_MANIFEST_PATH):
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            items = raw.get("items", [])
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if "待标注" in (item.get("tags") or []):
                continue
            if not (_PACK_DIR / str(item.get("file", ""))).is_file():
                continue
            catalog[str(item["id"])] = item
    return catalog


_CATALOG: dict[str, dict[str, Any]] = {}
_CATALOG_MTIME_NS: Optional[tuple[Optional[int], ...]] = None


def _refresh_catalog() -> dict[str, dict[str, Any]]:
    """清单可能在机器人启动后才被放入本地 data 目录，按文件修改时间轻量刷新。"""
    global _CATALOG, _CATALOG_MTIME_NS
    manifest_paths = (_MANIFEST_PATH, _COMMUNITY_MANIFEST_PATH, _RECEIVED_MANIFEST_PATH)
    mtime_ns = tuple(
        path.stat().st_mtime_ns if path.is_file() else None
        for path in manifest_paths
    )
    if mtime_ns != _CATALOG_MTIME_NS:
        _CATALOG = _read_catalog()
        _CATALOG_MTIME_NS = mtime_ns
    return _CATALOG


def clear_decision() -> None:
    _CURRENT_DECISION.set(None)


def take_decision() -> Optional[str]:
    choice = _CURRENT_DECISION.get()
    _CURRENT_DECISION.set(None)
    return choice


def parse_model_reply(reply: str, *, allow_sticker: bool) -> str:
    """从模型原始输出取出隐藏控制标记，保证标记不会进入 QQ 正文或会话历史。"""
    catalog = _refresh_catalog()
    found = list(_CONTROL_RE.finditer(reply or ""))
    cleaned = _CONTROL_LIKE_RE.sub("", reply or "").strip()
    if allow_sticker and found:
        candidate = found[-1].group(1).strip().lower()
        _CURRENT_DECISION.set(candidate if candidate in catalog else None)
    return cleaned


def prompt_directive() -> str:
    catalog = _refresh_catalog()
    if not catalog:
        return ""
    catalog = "; ".join(
        f"{sid}={item.get('description', '')}（{'/'.join(item.get('tags', []))}）"
        for sid, item in catalog.items()
    )
    return (
        "【阿米娅表情包选择，仅用于本轮轻松日常文字回复】\n"
        "你自行判断是否需要用一张反应图补充语气；绝大多数回合选 none。只有图片能自然增强轻松互动时才选择，"
        "严肃/伤心求助/道歉/复杂任务/工作文档/生图/语音/识图/用户要求安静时必须选 none。"
        "正文保持原有格式与长度，末尾另起一行追加一个机器标记，正文之外不要解释这个标记。"
        "标记只能是 [[AMIYA_STICKER:none]] 或 [[AMIYA_STICKER:ID]]，ID 从以下目录中选一个且要贴合情绪；不发图时必须用 none。\n"
        f"可选目录：{catalog}"
    )


class AmiyaStickerService:
    """以 AI 的单轮回复决策选择本地图，延迟后经现有群聊节奏与配额通道发送。"""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._ingest_tasks: set[asyncio.Task[None]] = set()
        self._ingest_lock = asyncio.Lock()
        self._last_sent: dict[str, float] = {}
        self._cooldown_sec = 60.0

    def has(self, sticker_id: Optional[str]) -> bool:
        return bool(sticker_id and sticker_id in _refresh_catalog())

    async def schedule(
        self,
        *,
        group_id: Optional[str],
        user_id: str,
        task_id: str,
        sticker_id: str,
        send_segment: Callable[[MessageSegment], Awaitable[bool]],
        still_allowed: Callable[[], bool],
    ) -> bool:
        from .config_manager import state

        if not getattr(state, "amiya_sticker_enabled", True):
            return False
        if not self.has(sticker_id):
            return False
        target_key = f"group:{group_id}" if group_id else f"private:{user_id}"
        pending = self._pending.get(target_key)
        if pending and not pending.done():
            return False
        if time.monotonic() - self._last_sent.get(target_key, 0.0) < self._cooldown_sec:
            return False
        min_delay = float(getattr(state, "amiya_sticker_min_delay_sec", 1.0))
        max_delay = float(getattr(state, "amiya_sticker_max_delay_sec", 20.0))
        if not (1.0 <= min_delay <= max_delay <= 20.0):
            min_delay, max_delay = 1.0, 20.0
        delay_sec = random.uniform(min_delay, max_delay)
        task = asyncio.create_task(
            self._deliver(
                target_key=target_key,
                group_id=group_id,
                user_id=str(user_id),
                task_id=task_id,
                sticker_id=sticker_id,
                delay_sec=delay_sec,
                send_segment=send_segment,
                still_allowed=still_allowed,
            ),
            name=f"amiya-sticker-{target_key}",
        )
        self._pending[target_key] = task
        logger.info(f"阿米娅表情包已排程: id={sticker_id} delay={delay_sec:.1f}s target={target_key}")
        return True

    def schedule_received_group_emojis(self, bot: Any, event: Any) -> None:
        """后台收录明确标记为 QQ 表情的群消息；普通图片不自动进入角色素材库。"""
        segments = getattr(event, "original_message", None) or getattr(event, "message", None) or []
        candidates: list[dict[str, str]] = []
        for segment in segments:
            seg_type = getattr(segment, "type", None) or (segment.get("type") if isinstance(segment, dict) else None)
            if seg_type != "image":
                continue
            data = getattr(segment, "data", None) or (segment.get("data") if isinstance(segment, dict) else {}) or {}
            sub_type = str(data.get("sub_type") or data.get("subType") or "").strip().lower()
            summary = str(data.get("summary") or "").strip()
            is_emoji = sub_type in {"1", "sticker", "emoji"} or bool(data.get("emoji_id")) or "表情" in summary
            if not is_emoji:
                continue
            info = {
                "url": str(data.get("url") or "").strip(),
                "file": str(data.get("file") or data.get("file_id") or "").strip(),
                "path": str(data.get("path") or "").strip(),
                "summary": summary[:100],
            }
            if info["url"] or info["file"] or info["path"]:
                candidates.append(info)
        if not candidates:
            return
        task = asyncio.create_task(
            self._ingest_received_group_emojis(bot, candidates),
            name="amiya-sticker-received-ingest",
        )
        self._ingest_tasks.add(task)
        task.add_done_callback(self._ingest_tasks.discard)

    async def _ingest_received_group_emojis(self, bot: Any, candidates: list[dict[str, str]]) -> None:
        from . import _resolve_input_images_b64

        for info in candidates:
            try:
                data_uris = await _resolve_input_images_b64(bot, [info])
                if not data_uris:
                    continue
                header, encoded = data_uris[0].split(",", 1)
                raw = base64.b64decode(encoded)
                if not (100 < len(raw) <= 2_000_000):
                    continue
                if raw.startswith((b"GIF87a", b"GIF89a")):
                    ext = ".gif"
                elif raw.startswith(b"\xff\xd8\xff"):
                    ext = ".jpg"
                elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
                    ext = ".webp"
                else:
                    ext = ".png"
                import hashlib

                sticker_id = "received_" + hashlib.sha256(raw).hexdigest()[:12]
                summary = re.sub(r"\s+", " ", info.get("summary", "")).strip()
                generic_summaries = {"表情", "动画表情", "图片表情", "emoji", "sticker"}
                if summary and summary.lower() not in generic_summaries and len(summary) >= 2:
                    description = f"群聊新收到的表情：{summary}"
                    tags = ["群聊新表情", summary[:24]]
                else:
                    description = "群聊新收到的表情图片，内容待人工标注"
                    tags = ["群聊新表情", "待标注"]
                filename = f"{sticker_id}{ext}"
                async with self._ingest_lock:
                    catalog_raw = {}
                    try:
                        catalog_raw = json.loads(_RECEIVED_MANIFEST_PATH.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        catalog_raw = {"character": "amiya", "items": []}
                    items = catalog_raw.get("items") if isinstance(catalog_raw, dict) else None
                    if not isinstance(items, list):
                        items = []
                        catalog_raw = {"character": "amiya", "items": items}
                    if any(isinstance(item, dict) and item.get("id") == sticker_id for item in items):
                        continue
                    received_dir = _PACK_DIR / "received"
                    await asyncio.to_thread(received_dir.mkdir, parents=True, exist_ok=True)
                    dest = received_dir / filename
                    await asyncio.to_thread(dest.write_bytes, raw)
                    items.append({
                        "id": sticker_id,
                        "file": f"received/{filename}",
                        "format": ext.lstrip("."),
                        "description": description,
                        "tags": tags,
                        "source": "QQ group received emoji",
                    })
                    tmp = _RECEIVED_MANIFEST_PATH.with_suffix(".json.tmp")
                    serialized = json.dumps(catalog_raw, ensure_ascii=False, indent=2)
                    await asyncio.to_thread(tmp.write_text, serialized, encoding="utf-8")
                    await asyncio.to_thread(tmp.replace, _RECEIVED_MANIFEST_PATH)
                logger.info(f"群聊新表情已收录: id={sticker_id} 标注={'待补' if '待标注' in tags else '已取摘要'}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"收录群聊表情失败: {type(exc).__name__}: {exc}")

    async def _deliver(
        self,
        *,
        target_key: str,
        group_id: Optional[str],
        user_id: str,
        task_id: str,
        sticker_id: str,
        delay_sec: float,
        send_segment: Callable[[MessageSegment], Awaitable[bool]],
        still_allowed: Callable[[], bool],
    ) -> None:
        from .group_chat_manager import group_chat_manager

        reservation = None
        try:
            await asyncio.sleep(delay_sec)
            if not still_allowed():
                return
            item = _refresh_catalog().get(sticker_id)
            if not item:
                return
            image_path = _PACK_DIR / str(item["file"])
            image_bytes = image_path.read_bytes()
            if not image_bytes:
                return
            segment = MessageSegment.image(
                "base64://" + base64.b64encode(image_bytes).decode("ascii")
            )

            if group_id:
                allowed, reason, reservation = await group_chat_manager.density.try_reserve_normal(
                    str(group_id), f"{task_id}:sticker:{sticker_id}"
                )
                if not allowed or reservation is None:
                    logger.info(f"阿米娅表情包按群聊配额跳过: target={target_key} reason={reason}")
                    return
                sent = await group_chat_manager.density.send_with_pacing(
                    str(group_id),
                    lambda: send_segment(segment),
                    reservation=reservation,
                )
            else:
                sent = await send_segment(segment)

            if sent:
                self._last_sent[target_key] = time.monotonic()
                logger.info(f"阿米娅表情包已发送: id={sticker_id} target={target_key}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"阿米娅表情包投递失败: id={sticker_id} error={type(exc).__name__}: {exc}")
        finally:
            if reservation is not None and reservation.status == "reserved" and group_id:
                await group_chat_manager.density.release_reservation(
                    reservation, str(group_id), reason="sticker_delivery_exit"
                )
            current = self._pending.get(target_key)
            if current is asyncio.current_task():
                self._pending.pop(target_key, None)

    async def shutdown(self) -> None:
        tasks = list(self._pending.values())
        self._pending.clear()
        tasks.extend(self._ingest_tasks)
        self._ingest_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


amiya_sticker_service = AmiyaStickerService()
