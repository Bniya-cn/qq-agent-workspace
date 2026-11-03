"""场景表达与群风格学习器（固定人格优先）。

Adapted from:
- Project: MaiBot (https://github.com/Mai-with-u/MaiBot)
- Release / Snapshot: v1.3.15 (062d45cf8bd4272ac57d7ec03fcf405e5a458211)
- Source modules:
  * src/learners/expression_learner.py
  * src/learners/expression_style_utils.py
  * src/chat/replyer/maisaka_expression_selector.py
- License: GNU General Public License v3.0 (GPL-3.0)
- Copyright (c) Mai-with-u and MaiBot contributors.

Adaptations for Amiya (NoneBot2 + OneBot V11 + NapCat):
1. Extracted core "situation -> expression style" normalization, deduplication, batch concurrency gate, and weighted/lexical selector without importing MaiBot's internal ORM or plugin runtime.
2. Enforced strict dialog-pair hygiene: only pairs messages with explicit reply relations or verified direct user-to-user conversational turns; never pairs unrelated adjacent messages and never feeds the bot's own historical replies back into the learning pool.
3. Combined expression extraction and Amiya persona compatibility filtering in a single background pass; learned expressions are injected only as optional phrasing references (max 3 items, 0ms local lookup) and never overwrite Amiya's core persona files.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Final, Optional, Sequence

import httpx
from nonebot import logger

from .config_manager import BOT_ROOT, ensure_bot_env

EXPRESSIONS_DIR = BOT_ROOT / "data" / "expressions"

PROMPT_EXAMPLE_EXPRESSION_STYLES: Final[frozenset[str]] = frozenset(
    {
        "我嘞个xxxx",
        "对对对",
        "这么强！",
        "这也太离谱了吧",
        "笑死我了",
    }
)

# 违背阿米娅核心人格底线的粗俗/攻击性黑名单（学习与注入双层拦截）
_INCOMPATIBLE_STYLE_RE = re.compile(
    r"(?:傻[逼bB]|脑残|去死|滚蛋|妈的|他妈|cnm|nmsl|wdnmd|草泥马|弱智|废物|给爷爬|本鱼|奶蛙)",
    re.IGNORECASE,
)

# 发言后 60 秒内群友正向/负向反馈识别模式（用于表达方式闭环进化与自动拉黑）
_POSITIVE_REACTION_RE = re.compile(
    r"(?:哈哈|hhh|2333*|笑死|草|太真了|确实|会说多说|牛的|神回复|绷不住|好活|爱听|太懂了|真聪明|可爱|贴贴|摸摸|天才|绝了)",
    re.IGNORECASE,
)
_NEGATIVE_REACTION_RE = re.compile(
    r"(?:^[?？!！。…]{1,4}$|人机|捧读|太尬|尬死|别学|少学|闭嘴|难绷|怪怪的|好奇怪|什么鬼|复读机|AI味|机器人味|不好笑|冷场|有病吧)",
    re.IGNORECASE,
)
_HARD_BLACKLIST_TRIGGER_RE = re.compile(
    r"(?:别学|少学|人机|太尬|尬死|复读机|AI味|机器人味)",
    re.IGNORECASE,
)


def strip_expression_style_usage_prefix(style: str) -> str:
    """移除表达学习 prompt 示例带出的“使用/可以用”前缀（移植自 MaiBot expression_style_utils.py）。"""
    normalized = str(style or "").strip()
    for prefix in ("使用", "可以用", "用", "表达为"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
    return normalized.strip("“”\"'「」『』 ")


def normalize_expression_style_for_learning(style: str) -> str:
    """规范化准备学习或使用的表达风格文本。"""
    s = strip_expression_style_usage_prefix(style)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_prompt_example_expression_style(style: str) -> bool:
    """判断表达风格是否是 prompt 示例内容泄漏。"""
    normalized = normalize_expression_style_for_learning(style)
    return normalized in PROMPT_EXAMPLE_EXPRESSION_STYLES


def is_persona_compatible_style(style: str, character: str = "amiya") -> bool:
    """检查候选表达是否与固定角色人格兼容（不粗俗、不冒充其他机器人自称）。"""
    s = normalize_expression_style_for_learning(style)
    if not s or len(s) < 2 or len(s) > 28:
        return False
    if is_prompt_example_expression_style(s):
        return False
    if _INCOMPATIBLE_STYLE_RE.search(s):
        return False
    return True


@dataclasses.dataclass(frozen=True)
class ExpressionLearningAcquireResult:
    """表达学习批次并发闸门的申请结果（移植自 MaiBot expression_learner.py）。"""
    acquired: bool
    reason: str = ""
    active_count: int = 0
    max_count: int = 2


class ExpressionLearningBatchGate:
    """控制表达学习批次的群聊流互斥与全局并发上限。"""

    def __init__(self, max_concurrent: int = 2) -> None:
        self._lock = asyncio.Lock()
        self._active_group_ids: set[str] = set()
        self.max_concurrent = max(1, int(max_concurrent))

    async def acquire(self, group_id: str) -> ExpressionLearningAcquireResult:
        gid = str(group_id or "").strip()
        async with self._lock:
            active_count = len(self._active_group_ids)
            if gid in self._active_group_ids:
                return ExpressionLearningAcquireResult(False, "session_busy", active_count, self.max_concurrent)
            if active_count >= self.max_concurrent:
                return ExpressionLearningAcquireResult(False, "global_limit", active_count, self.max_concurrent)
            self._active_group_ids.add(gid)
            return ExpressionLearningAcquireResult(True, "", active_count + 1, self.max_concurrent)

    async def release(self, group_id: str) -> None:
        gid = str(group_id or "").strip()
        async with self._lock:
            self._active_group_ids.discard(gid)


@dataclasses.dataclass
class SceneExpressionItem:
    """单条“场景—表达方式”条目（含发言后群友实时反馈评分与拉黑状态）。"""
    expr_id: str
    group_id: str
    situation: str
    style: str
    tone: str = "自然口语"
    length_hint: str = "short"
    meme_intensity: float = 0.3
    count: int = 1
    source_msg_id: str = ""
    updated_at: float = dataclasses.field(default_factory=time.time)
    positive_feedback: int = 0
    negative_feedback: int = 0
    score_bias: float = 0.0
    blacklisted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _char_ngrams(text: str) -> set[str]:
    clean = re.sub(r"[^\w\u4e00-\u9fff]", "", (text or "").lower())
    if not clean:
        return set()
    grams = {ch for ch in clean}
    for i in range(len(clean) - 1):
        grams.add(clean[i : i + 2])
    return grams


class MaiBotSceneExpressionStore:
    """管理各群的“场景—表达方式”缓存、去重持久化、0ms 快速匹配选择与 60s 群友反馈奖惩。"""

    FEEDBACK_WINDOW_SEC: Final[float] = 60.0

    def __init__(self, storage_dir: Path = EXPRESSIONS_DIR) -> None:
        self.storage_dir = storage_dir
        self.gate = ExpressionLearningBatchGate(max_concurrent=2)
        self._cache: dict[str, list[SceneExpressionItem]] = {}
        self._pending_turns: dict[str, list[dict[str, Any]]] = {}
        self._msg_index: dict[str, dict[str, dict[str, Any]]] = {}
        self._recent_bot_exposures: dict[str, dict[str, Any]] = {}
        self._group_blacklisted_styles: dict[str, set[str]] = {}
        self._feedback_events: list[dict[str, Any]] = []

    def _group_file(self, group_id: str) -> Path:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        return self.storage_dir / f"{group_id}_expressions.json"

    def load_group_expressions(self, group_id: str) -> list[SceneExpressionItem]:
        gid = str(group_id or "").strip()
        if not gid:
            return []
        if gid in self._cache:
            return list(self._cache[gid])
        path = self._group_file(gid)
        items: list[SceneExpressionItem] = []
        bl_set = self._group_blacklisted_styles.setdefault(gid, set())
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for row in raw:
                        if not isinstance(row, dict):
                            continue
                        style = normalize_expression_style_for_learning(str(row.get("style") or ""))
                        situation = str(row.get("situation") or "").strip()
                        if not situation or not is_persona_compatible_style(style):
                            continue
                        is_bl = bool(row.get("blacklisted", False))
                        if is_bl:
                            bl_set.add(style)
                        items.append(
                            SceneExpressionItem(
                                expr_id=str(row.get("expr_id") or hashlib.md5(f"{situation}:{style}".encode()).hexdigest()[:10]),
                                group_id=gid,
                                situation=situation,
                                style=style,
                                tone=str(row.get("tone") or "自然口语"),
                                length_hint=str(row.get("length_hint") or "short"),
                                meme_intensity=float(row.get("meme_intensity", 0.3)),
                                count=int(row.get("count", 1)),
                                source_msg_id=str(row.get("source_msg_id") or ""),
                                updated_at=float(row.get("updated_at", time.time())),
                                positive_feedback=int(row.get("positive_feedback", 0)),
                                negative_feedback=int(row.get("negative_feedback", 0)),
                                score_bias=float(row.get("score_bias", 0.0)),
                                blacklisted=is_bl,
                            )
                        )
            except Exception as exc:
                logger.warning(f"加载群 {gid} 场景表达失败: {exc}")
        self._cache[gid] = items
        return list(items)

    def save_group_expressions(self, group_id: str, items: list[SceneExpressionItem]) -> None:
        gid = str(group_id or "").strip()
        if not gid:
            return
        self._cache[gid] = list(items)
        bl_set = self._group_blacklisted_styles.setdefault(gid, set())
        for x in items:
            if x.blacklisted:
                bl_set.add(x.style)
        path = self._group_file(gid)
        tmp = path.with_suffix(".tmp")
        payload = [x.to_dict() for x in items[-160:]]
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def upsert_expressions(
        self,
        group_id: str,
        candidates: Sequence[dict[str, Any]],
        *,
        character: str = "amiya",
    ) -> int:
        """规范化、过滤、去重并合并场景表达条目（已拉黑表达绝不复活）。"""
        gid = str(group_id or "").strip()
        if not gid:
            return 0
        current = self.load_group_expressions(gid)
        bl_styles = self._group_blacklisted_styles.setdefault(gid, set())
        by_key: dict[tuple[str, str], SceneExpressionItem] = {
            (x.situation, x.style): x for x in current
        }
        added_or_updated = 0
        now = time.time()
        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            situation = str(raw.get("situation") or "").strip()[:36]
            style = normalize_expression_style_for_learning(str(raw.get("style") or ""))
            if not situation or not is_persona_compatible_style(style, character=character):
                continue
            if style in bl_styles:
                continue
            tone = str(raw.get("tone") or "轻快自然").strip()[:16]
            length_hint = "short" if len(style) <= 12 else "medium"
            try:
                meme_intensity = min(1.0, max(0.0, float(raw.get("meme_intensity", 0.3))))
            except (TypeError, ValueError):
                meme_intensity = 0.3

            key = (situation, style)
            if key in by_key:
                existing = by_key[key]
                if existing.blacklisted:
                    continue
                existing.count += 1
                existing.updated_at = now
                added_or_updated += 1
            else:
                # 若同一 style 已存在相近场景，更新频次而非无限膨胀
                same_style = next((v for v in by_key.values() if v.style == style), None)
                if same_style is not None:
                    if same_style.blacklisted:
                        continue
                    same_style.count += 1
                    same_style.updated_at = now
                    added_or_updated += 1
                    continue
                item = SceneExpressionItem(
                    expr_id=hashlib.md5(f"{gid}:{situation}:{style}".encode("utf-8")).hexdigest()[:10],
                    group_id=gid,
                    situation=situation,
                    style=style,
                    tone=tone,
                    length_hint=length_hint,
                    meme_intensity=meme_intensity,
                    count=1,
                    source_msg_id=str(raw.get("source_id") or ""),
                    updated_at=now,
                )
                by_key[key] = item
                added_or_updated += 1

        if added_or_updated:
            merged = sorted(
                by_key.values(),
                key=lambda x: (not x.blacklisted, x.count + x.positive_feedback - x.negative_feedback, x.updated_at),
                reverse=True,
            )[:160]
            self.save_group_expressions(gid, merged)
        return added_or_updated

    def record_bot_expression_usage(
        self,
        group_id: str,
        expr_ids: Sequence[str],
        *,
        reply_text: str = "",
        target_uid: str = "",
        now_ts: Optional[float] = None,
    ) -> None:
        """登记阿米娅在本群刚刚发言时参考或命中的表达 ID，开启 60s 群友反馈观测窗口。"""
        gid = str(group_id or "").strip()
        if not gid or not expr_ids:
            return
        now = float(time.time() if now_ts is None else now_ts)
        self._recent_bot_exposures[gid] = {
            "ts": now,
            "expr_ids": [str(eid) for eid in expr_ids if eid],
            "reply_text": (reply_text or "")[:120],
            "target_uid": str(target_uid or ""),
            "feedback_uids": set(),
        }

    def observe_group_reaction(
        self,
        group_id: str,
        sender_uid: str,
        text: str,
        *,
        reply_to_bot: bool = False,
        now_ts: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """观测阿米娅发言后 60 秒内的群友反应，对刚使用的场景表达执行正向加权或负向扣分/自动拉黑。"""
        gid = str(group_id or "").strip()
        uid = str(sender_uid or "").strip()
        clean = (text or "").strip()
        if not gid or not uid or not clean or clean.startswith("/"):
            return None
        exposure = self._recent_bot_exposures.get(gid)
        if not exposure:
            return None
        now = float(time.time() if now_ts is None else now_ts)
        elapsed = now - float(exposure.get("ts", 0.0))
        if elapsed < 0.0 or elapsed > self.FEEDBACK_WINDOW_SEC:
            self._recent_bot_exposures.pop(gid, None)
            return None

        feedback_uids: set[str] = exposure.setdefault("feedback_uids", set())
        if uid in feedback_uids:
            return None

        is_neg = bool(_NEGATIVE_REACTION_RE.search(clean))
        is_pos = bool(_POSITIVE_REACTION_RE.search(clean)) and not is_neg
        if not is_neg and not is_pos:
            return None

        # 若只是单独问号“？”，要求是回复机器人或紧跟机器人发言 25 秒内才算负向反馈，避免误伤群友间互相发问号
        if is_neg and re.fullmatch(r"[?？!！。…]{1,4}", clean) and not reply_to_bot and elapsed > 25.0:
            return None

        target_ids = set(exposure.get("expr_ids") or [])
        if not target_ids:
            return None

        items = self.load_group_expressions(gid)
        if not items:
            return None

        hard_blacklist = bool(_HARD_BLACKLIST_TRIGGER_RE.search(clean))
        updated_ids: list[str] = []
        blacklisted_styles: list[str] = []
        bl_set = self._group_blacklisted_styles.setdefault(gid, set())

        for item in items:
            if item.expr_id not in target_ids:
                continue
            if is_neg:
                item.negative_feedback += 1
                item.count = max(0, item.count - 1)
                item.score_bias = round(max(-1.0, item.score_bias - (0.40 if hard_blacklist else 0.22)), 3)
                item.updated_at = now
                if hard_blacklist or item.negative_feedback >= 2 or item.score_bias <= -0.35:
                    item.blacklisted = True
                    bl_set.add(item.style)
                    blacklisted_styles.append(item.style)
                updated_ids.append(item.expr_id)
            elif is_pos and not item.blacklisted:
                item.positive_feedback += 1
                item.count += 1
                item.score_bias = round(min(0.60, item.score_bias + 0.15), 3)
                item.updated_at = now
                updated_ids.append(item.expr_id)

        if not updated_ids:
            return None

        feedback_uids.add(uid)
        self.save_group_expressions(gid, items)
        ev_record = {
            "ts": now,
            "group_id": gid,
            "sender_uid": uid,
            "reaction": "negative" if is_neg else "positive",
            "trigger_text": clean[:60],
            "expr_ids": updated_ids,
            "blacklisted_styles": blacklisted_styles,
            "reply_preview": exposure.get("reply_text", "")[:60],
        }
        self._feedback_events.append(ev_record)
        if len(self._feedback_events) > 100:
            del self._feedback_events[:-100]
        return ev_record

    def get_feedback_summary(self, group_id: Optional[str] = None) -> dict[str, Any]:
        """返回表达学习的反馈统计与黑名单摘要（供每日复盘与 WebUI 监控使用）。"""
        gid = str(group_id or "").strip() if group_id else ""
        events = [e for e in self._feedback_events if not gid or e.get("group_id") == gid]
        bl_map = (
            {gid: sorted(self._group_blacklisted_styles.get(gid, set()))}
            if gid
            else {k: sorted(v) for k, v in self._group_blacklisted_styles.items() if v}
        )
        return {
            "recent_feedback_events": events[-20:],
            "positive_count": sum(1 for e in events if e.get("reaction") == "positive"),
            "negative_count": sum(1 for e in events if e.get("reaction") == "negative"),
            "blacklisted_styles_by_group": bl_map,
        }

    def record_verified_turn_for_learning(
        self,
        *,
        group_id: str,
        message_id: str = "",
        sender_uid: str = "",
        sender_name: str = "",
        text: str = "",
        reply_to_message_id: Optional[str] = None,
        reply_to_text: str = "",
        bot_self_id: str = "",
        timestamp: Optional[float] = None,
    ) -> bool:
        """收集真实群友素材（严格过滤机器人自身回复与无关联孤立发言）。"""
        gid = str(group_id or "").strip()
        uid = str(sender_uid or "").strip()
        clean = (text or "").strip()
        if not gid or not uid or not clean:
            return False
        # 严禁将机器人自己的历史回复回灌进学习池
        if bot_self_id and uid == str(bot_self_id).strip():
            return False
        if any(bot_alias in (sender_name or "").lower() for bot_alias in ("阿米娅", "amiya", "大肥鱼", "奶蛙")):
            return False
        if clean.startswith("/") or len(clean) < 2 or len(clean) > 120:
            return False
        if not is_persona_compatible_style(clean[:28]):
            return False

        now = float(timestamp or time.time())
        msg_map = self._msg_index.setdefault(gid, {})
        if message_id:
            msg_map[str(message_id)] = {
                "message_id": str(message_id),
                "uid": uid,
                "name": sender_name,
                "text": clean,
                "ts": now,
            }
            if len(msg_map) > 300:
                oldest_k = next(iter(msg_map))
                msg_map.pop(oldest_k, None)

        # 仅当存在明确引用关系（reply_to_message_id / reply_to_text）时构建可信对话对，
        # 或作为真实语境发言进入表达学习缓冲池（绝不把时间相近但无关的两人发言硬配成 SFT 问答对！）
        resolved_context = (reply_to_text or "").strip()
        if not resolved_context and reply_to_message_id and str(reply_to_message_id) in msg_map:
            ref_entry = msg_map[str(reply_to_message_id)]
            if ref_entry.get("uid") != uid:
                resolved_context = str(ref_entry.get("text") or "")

        buf = self._pending_turns.setdefault(gid, [])
        buf.append(
            {
                "message_id": str(message_id or len(buf) + 1),
                "uid": uid,
                "name": sender_name,
                "text": clean,
                "reply_context": resolved_context,
                "has_explicit_reply": bool(resolved_context),
                "ts": now,
            }
        )
        if len(buf) > 120:
            del buf[:-120]
        return True

    def select_scene_expressions(
        self,
        group_id: Optional[str],
        query_text: str,
        *,
        top_k: int = 3,
        max_meme_intensity: float = 0.75,
    ) -> list[SceneExpressionItem]:
        """移植自 MaiBot maisaka_expression_selector.py 的快速选择器（0ms 本地缓存检索，自动跳过已拉黑或负分表达）。"""
        if not group_id:
            return []
        gid = str(group_id).strip()
        candidates = self.load_group_expressions(gid)
        if not candidates:
            return []

        bl_styles = self._group_blacklisted_styles.get(gid, set())
        q_grams = _char_ngrams(query_text)
        scored: list[tuple[float, SceneExpressionItem]] = []
        for item in candidates:
            if item.blacklisted or item.style in bl_styles:
                continue
            if item.meme_intensity > max_meme_intensity:
                continue
            sit_grams = _char_ngrams(item.situation)
            style_grams = _char_ngrams(item.style)
            overlap_sit = len(q_grams & sit_grams) / max(1.0, math.sqrt(len(q_grams) * max(1, len(sit_grams)))) if q_grams else 0.0
            overlap_style = len(q_grams & style_grams) / max(1.0, math.sqrt(len(q_grams) * max(1, len(style_grams)))) if q_grams else 0.0
            freq_bonus = min(0.25, math.log1p(max(0, item.count)) * 0.08)
            score = overlap_sit * 0.72 + overlap_style * 0.18 + freq_bonus + float(item.score_bias)
            if score < -0.05:
                continue
            scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        relevant = [item for score, item in scored if score >= 0.12][:top_k]
        if len(relevant) < min(2, top_k) and scored:
            high_freq = [
                item for score, item in scored[: min(6, len(scored))]
                if item not in relevant and score >= 0.0
            ]
            if high_freq:
                relevant.extend(high_freq[: max(0, min(2, top_k) - len(relevant))])
        return relevant[:top_k]

    def build_expression_reference_block(
        self,
        group_id: Optional[str],
        query_text: str,
        *,
        character: str = "amiya",
        top_k: int = 3,
        record_exposure: bool = True,
    ) -> str:
        """构建注入系统提示词的轻量场景表达参考块（严格声明固定人格优先，并自动登记 60s 反馈窗口）。"""
        selected = self.select_scene_expressions(group_id, query_text, top_k=top_k)
        if not selected:
            return ""
        if record_exposure and group_id:
            self.record_bot_expression_usage(
                str(group_id),
                [item.expr_id for item in selected],
            )
        char_label = {"amiya": "阿米娅", "kaltsit": "凯尔希", "dania": "达妮娅"}.get(character, "阿米娅")
        lines = [
            f"- 当处于「{item.situation}」场景时，可自然借用类似「{item.style}」的句式节奏（语气：{item.tone}）"
            for item in selected
        ]
        return (
            f"\n【本群场景表达参考（仅借用群聊句式节奏与熟人表达感；你始终是{char_label}，核心人格、温柔底色与身份立场绝对优先，严禁被参考句式覆盖身份）】：\n"
            + "\n".join(lines)
        )

    async def run_background_expression_learning(
        self,
        group_id: str,
        raw_messages: Optional[Sequence[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """在后台单次调用中同时完成 MaiBot“场景—表达方式”提炼与阿米娅人格适配过滤。"""
        gid = str(group_id or "").strip()
        if not gid:
            return {"status": "error", "message": "缺少 group_id"}

        gate_res = await self.gate.acquire(gid)
        if not gate_res.acquired:
            return {
                "status": "busy",
                "group_id": gid,
                "reason": gate_res.reason,
                "message": f"表达学习并发受限 ({gate_res.reason})",
            }

        try:
            if raw_messages is not None:
                turns = list(raw_messages)
            else:
                turns = list(self._pending_turns.get(gid, []))
                if gid in self._pending_turns:
                    self._pending_turns[gid].clear()
            if not turns:
                return {"status": "empty", "group_id": gid, "learned_count": 0}

            lines: list[str] = []
            for idx, t in enumerate(turns[-60:], 1):
                txt = str(t.get("text") or "").strip()
                if not txt:
                    continue
                ctx = str(t.get("reply_context") or "").strip()
                prefix = f"[回复「{ctx[:30]}」] " if ctx else ""
                lines.append(f"[{idx}] {prefix}{txt}")

            if not lines:
                return {"status": "empty", "group_id": gid, "learned_count": 0}

            ensure_bot_env()
            ds_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
            if ds_key:
                api_key = ds_key
                base_url = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
                url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
                model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip()
            else:
                return {"status": "error", "group_id": gid, "message": "未配置 DEEPSEEK_API_KEY"}

            sys_prompt = (
                "你是场景表达风格提炼器（基于 MaiBot 场景—表达方式结构，并内置阿米娅人格兼容过滤）。\n"
                "请从给定群聊发言中提炼最多 8 条自然、鲜活、适合温柔真诚少女阿米娅在群聊中借用的“场景—表达方式”。\n"
                "过滤铁律：\n"
                "1. 剔除粗俗辱骂、攻击性黑话、其他机器人的自称（如“本鱼”“奶蛙”）或破坏阿米娅温柔真诚底色的句子。\n"
                "2. style 必须是通用句式或简短口头表达（2~18字），不带“使用”前缀，不泄露固定示例（如“我嘞个xxxx”“对对对”）。\n"
                "3. 只输出合法 JSON 对象格式：\n"
                "{\n"
                '  "expressions": [\n'
                '    {"situation": "听到朋友熬夜加班或很累时", "style": "快去歇会儿别硬撑啦", "tone": "温和关心", "meme_intensity": 0.2, "source_id": "1"}\n'
                "  ]\n"
                "}"
            )
            user_prompt = "【群聊真实发言片段】：\n" + "\n".join(lines)

            async with httpx.AsyncClient(trust_env=False, timeout=45.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.3,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                content = (resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()

            raw_json = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
            parsed = json.loads(raw_json)
            expr_list = parsed.get("expressions") if isinstance(parsed, dict) else []
            if not isinstance(expr_list, list):
                expr_list = []

            count = self.upsert_expressions(gid, expr_list[:20], character="amiya")
            if raw_messages is None and gid in self._pending_turns:
                self._pending_turns[gid].clear()
            return {
                "status": "ok",
                "group_id": gid,
                "learned_count": count,
                "total_expressions": len(self.load_group_expressions(gid)),
            }
        except Exception as exc:
            logger.warning(f"群 {gid} 场景表达后台学习异常: {exc}")
            return {"status": "error", "group_id": gid, "message": str(exc)}
        finally:
            await self.gate.release(gid)


expression_store = MaiBotSceneExpressionStore()
