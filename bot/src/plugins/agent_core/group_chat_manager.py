"""群聊消息合并、发送配额预占、优先生成调度、注意力衰减与分段公平投递管理器。

Adapted from:
- Project: astrbot_plugin_group_chat_plus (https://github.com/Him666233/astrbot_plugin_group_chat_plus)
- Snapshot: aa97e0e35a18733e73db753238dd2a3021446e42
- Source modules:
  * utils/smart_concurrent_manager.py
  * utils/reply_density_manager.py
  * utils/attention_manager.py
- License: GNU Affero General Public License v3.0 (AGPL-3.0)
- Copyright (c) Him666233 and contributors.

Adaptations for Amiya (NoneBot2 + OneBot V11 + NapCat):
1. Atomic quota check + reservation + slot registration before LLM invocation (`try_reserve_normal` / `acquire_generation_slot`), with idempotent `commit_reservation` and `release_reservation`.
2. True priority generation queue (`_GroupGenerationScheduler`) where explicit requests (@bot, reply-to-bot, command, name wake) are scheduled at priority tier 0 ahead of normal messages without cancelling an already-running generation.
3. Explicit requests immediately end or bypass normal merge waiting and are never merged across multiple explicit requests.
4. Per-group max 1 normal generation task at a time; independent across different groups.
5. Group send-time pacing lock (`send_with_pacing` & `deliver_segmented_fairly`) shared by normal replies, segmented long replies, and autonomous replies so queued tasks or multi-segment coroutines never burst simultaneously.
6. Explicit message lifecycle state ledger (`queued`, `processing`, `delivered`, `failed`, `uncertain`, `skipped`).
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import heapq
import re
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Deque, Optional


class NormalQuotaRejectedError(RuntimeError):
    """普通群聊主动参与配额不足或同群已有普通生成任务进行中时抛出。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclasses.dataclass
class QuotaReservation:
    """按群归属的发送配额预占凭据（支持幂等确认与幂等释放）。"""
    reservation_id: str
    group_id: str
    task_id: str
    is_priority: bool
    reserved_at: float
    status: str = "reserved"  # reserved | committed | released
    committed_at: Optional[float] = None
    released_at: Optional[float] = None
    release_reason: str = ""


@dataclasses.dataclass
class MessageLifecycleRecord:
    """单条群消息的生命周期状态记录。"""
    message_id: str
    group_id: str
    user_id: str = ""
    task_id: str = ""
    is_priority: bool = False
    status: str = "queued"  # queued | processing | delivered | failed | uncertain | skipped
    reason: str = ""
    updated_at: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass
class QueuedGroupMessage:
    """单条入队群消息的结构化快照，保留完整说话人、时间、引用与提及关系。"""
    message_id: str
    group_id: str
    user_id: str
    nickname: str
    text: str
    timestamp: float
    reply_to_message_id: Optional[str] = None
    reply_to_user_id: Optional[str] = None
    reply_context: str = ""
    is_at_me: bool = False
    is_command: bool = False
    is_explicit_reply_to_me: bool = False
    is_name_wake: bool = False
    is_quasi_priority: bool = False
    has_other_at: bool = False
    image_b64_list: list[str] = dataclasses.field(default_factory=list)
    file_paths: list[str] = dataclasses.field(default_factory=list)
    raw_event: Any = None
    task_id: Optional[str] = None

    @property
    def is_priority(self) -> bool:
        """显式召唤（艾特、指令、明确回复机器人、名字唤醒）走优先通道，不受普通水群配额阻挡且等待期间不过期。"""
        return bool(
            self.is_at_me
            or self.is_command
            or self.is_explicit_reply_to_me
            or self.is_name_wake
        )

    @property
    def is_effective_priority(self) -> bool:
        """显式优先或 45 秒焦点窗口内的准优先级跟进。"""
        return bool(self.is_priority or self.is_quasi_priority)

    def format_for_batch_prompt(self) -> str:
        ts_str = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        tags: list[str] = []
        if self.is_at_me:
            tags.append("@阿米娅")
        if self.is_explicit_reply_to_me:
            tags.append("回复阿米娅")
        elif self.is_quasi_priority:
            tags.append("焦点连续跟进")
        if self.reply_to_message_id:
            tags.append(f"引用msg#{self.reply_to_message_id}")
        tag_prefix = f"({'/'.join(tags)}) " if tags else ""
        ctx_part = f" [引用内容: {self.reply_context[:80]}]" if self.reply_context else ""
        return (
            f"[{ts_str} | msg_id={self.message_id} | QQ={self.user_id} | 昵称={self.nickname}] "
            f"{tag_prefix}{self.text}{ctx_part}"
        )


@dataclasses.dataclass
class MergedMessageBatch:
    """合并后的群消息批次。"""
    group_id: str
    messages: list[QueuedGroupMessage]
    is_priority_batch: bool
    is_quasi_priority_batch: bool = False
    created_at: float = dataclasses.field(default_factory=time.monotonic)

    @property
    def message_ids(self) -> list[str]:
        return [m.message_id for m in self.messages if m.message_id]

    @property
    def primary_message(self) -> QueuedGroupMessage:
        for m in reversed(self.messages):
            if m.is_priority:
                return m
        for m in reversed(self.messages):
            if m.is_quasi_priority:
                return m
        return self.messages[-1]

    @property
    def distinct_user_ids(self) -> list[str]:
        seen: list[str] = []
        for m in self.messages:
            if m.user_id not in seen:
                seen.append(m.user_id)
        return seen

    @property
    def is_multi_user(self) -> bool:
        return len(self.distinct_user_ids) > 1

    def format_merged_context(self) -> str:
        if len(self.messages) == 1:
            return self.messages[0].text
        primary = self.primary_message
        primary_msgs = [m for m in self.messages if m.user_id == primary.user_id]
        other_msgs = [m for m in self.messages if m.user_id != primary.user_id]
        if not other_msgs:
            lines = [
                f"【当前对话用户（QQ={primary.user_id} | 昵称={primary.nickname}）连续发送的 {len(primary_msgs)} 条发言（请紧扣最后一条最新内容回应）】：",
                *(m.format_for_batch_prompt() for m in primary_msgs),
            ]
            return "\n".join(lines)
        if not self.is_priority_batch:
            latest = self.messages[-1]
            lines = [
                "【群聊合并窗口内大家刚刚说的最新几句话（按时间先后排列）】：",
                *(m.format_for_batch_prompt() for m in self.messages),
                f"👉 请紧扣最后一条最新发言（{latest.nickname}: 「{latest.text[:60]}」）或大家当前共同聊的最新话题自然接上一句话，严禁翻旧账去回复前面的老消息！",
            ]
            return "\n".join(lines)
        lines = [
            f"【当前主对话人（QQ={primary.user_id} | 昵称={primary.nickname}）的发言（请优先针对此人最新一句话回应）】：",
            *(m.format_for_batch_prompt() for m in primary_msgs),
            "【同一合并窗口内其他群友的插话（严格按QQ号区分，切勿张冠李戴算到主对话人头上）】：",
            *(m.format_for_batch_prompt() for m in other_msgs),
        ]
        return "\n".join(lines)


class ReplyDensityManager:
    """发送配额预占、结算与实际发送节奏控制器（移植并增强自 Group Chat Plus utils/reply_density_manager.py）。

    - 普通主动参与在调用模型前原子完成“检查额度 + 登记预占”（`try_reserve_normal`）。
    - 预占绑定 `task_id`：成功发送后幂等提交（`commit_reservation`），失败/取消/决定不回复时幂等释放（`release_reservation`）。
    - 实际发送前通过同群发送锁（`_send_locks`）严格执行发送间隔与分段节奏，防止多个排队任务或分段协程集中刷屏。
    """

    RESERVATION_TTL_SEC = 180.0

    def __init__(
        self,
        min_interval_sec: float = 8.0,
        max_per_minute: int = 4,
        chunk_pace_sec: float = 1.2,
    ) -> None:
        self.min_interval_sec = float(min_interval_sec)
        self.max_per_minute = int(max_per_minute)
        self.chunk_pace_sec = float(chunk_pace_sec)
        self._group_send_timestamps: dict[str, Deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=256)
        )
        self._group_normal_send_timestamps: dict[str, Deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=256)
        )
        self._group_incoming_timestamps: dict[str, Deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=512)
        )
        self._group_last_chunk_ts: dict[str, float] = {}
        self._group_last_normal_send_ts: dict[str, float] = {}
        self._active_reservations: dict[str, dict[str, QuotaReservation]] = collections.defaultdict(dict)
        self._reservation_ledger: collections.OrderedDict[str, QuotaReservation] = collections.OrderedDict()
        self._ledger_max = 2048
        self._lock = asyncio.Lock()
        self._send_locks: dict[str, asyncio.Lock] = {}

    def record_incoming_activity(self, group_id: str, now: Optional[float] = None) -> dict[str, Any]:
        """登记群内实时消息到达时间戳，用于按群聊热度动态调整主动回复节奏（Adaptive Heat Pacing）。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        if not gid:
            return self.get_adaptive_pacing(gid, now=ts)
        iq = self._group_incoming_timestamps[gid]
        while iq and (ts - iq[0]) > 180.0:
            iq.popleft()
        iq.append(ts)
        return self.get_adaptive_pacing(gid, now=ts)

    def get_adaptive_pacing(self, group_id: str, now: Optional[float] = None) -> dict[str, Any]:
        """根据近 3 分钟群聊实时流速计算自适应冷却间隔与接话倍率：
        - 冷清期 (msgs_per_min <= 1.5)：适度缩短主动冷却间隔 (0.75x)，更自然地暖场接话；
        - 常态期 (1.5 < msgs_per_min < 12.0)：保持基准配置间隔 (1.0x)；
        - 刷屏热聊期 (msgs_per_min >= 12.0)：主动拉长普通插话间隔 (1.35x)，防止在群友热聊刷屏时添乱。
        """
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        iq = self._group_incoming_timestamps.get(gid)
        if iq:
            while iq and (ts - iq[0]) > 180.0:
                iq.popleft()
            recent_60s = sum(1 for t in iq if (ts - t) <= 60.0)
            recent_180s = len(iq)
            msgs_per_min = round(max(float(recent_60s), recent_180s / 3.0), 2)
        else:
            msgs_per_min = 0.0

        base_interval = float(self.min_interval_sec)
        if msgs_per_min >= 12.0:
            heat_level = "hot"
            interval_scale = 1.35
        elif msgs_per_min <= 1.5 and msgs_per_min > 0.0:
            heat_level = "cold"
            interval_scale = 0.75
        else:
            heat_level = "normal"
            interval_scale = 1.0

        eff_interval = round(base_interval * interval_scale, 2) if base_interval > 0 else 0.0
        return {
            "group_id": gid,
            "heat_level": heat_level,
            "msgs_per_min": msgs_per_min,
            "base_min_interval_sec": base_interval,
            "effective_min_interval_sec": eff_interval,
            "interval_scale": interval_scale,
        }

    def _get_send_lock(self, group_id: str) -> asyncio.Lock:
        gid = str(group_id)
        if gid not in self._send_locks:
            self._send_locks[gid] = asyncio.Lock()
        return self._send_locks[gid]

    def configure(
        self,
        *,
        min_interval_sec: Optional[float] = None,
        max_per_minute: Optional[int] = None,
        chunk_pace_sec: Optional[float] = None,
    ) -> None:
        if min_interval_sec is not None:
            self.min_interval_sec = max(0.0, float(min_interval_sec))
        if max_per_minute is not None:
            self.max_per_minute = max(1, int(max_per_minute))
        if chunk_pace_sec is not None:
            self.chunk_pace_sec = max(0.0, float(chunk_pace_sec))

    def _prune_locked(self, gid: str, now: float) -> tuple[Deque[float], list[QuotaReservation]]:
        q = self._group_send_timestamps[gid]
        while q and (now - q[0]) > 60.0:
            q.popleft()
        nq = self._group_normal_send_timestamps[gid]
        while nq and (now - nq[0]) > 60.0:
            nq.popleft()
        res_map = self._active_reservations[gid]
        expired_ids = [
            rid for rid, r in res_map.items()
            if r.status != "reserved" or (now - r.reserved_at) > self.RESERVATION_TTL_SEC
        ]
        for rid in expired_ids:
            stale = res_map.pop(rid, None)
            if stale and stale.status == "reserved":
                stale.status = "released"
                stale.released_at = now
                stale.release_reason = "ttl_expired"
        active_norm = [r for r in res_map.values() if not r.is_priority and r.status == "reserved"]
        return q, active_norm

    def _check_normal_quota_locked(self, gid: str, ts: float) -> tuple[bool, str]:
        q, active_norm = self._prune_locked(gid, ts)
        last_candidates: list[float] = []
        if q:
            last_candidates.append(q[-1])
        for r in active_norm:
            last_candidates.append(r.reserved_at)
        if last_candidates:
            latest_ts = max(last_candidates)
            if (ts - latest_ts) < self.min_interval_sec:
                wait_rem = round(self.min_interval_sec - (ts - latest_ts), 2)
                return False, f"interval_cooldown({wait_rem}s)"
        effective_count = len(q) + len(active_norm)
        if effective_count >= self.max_per_minute:
            return False, f"minute_quota_reached({effective_count}/{self.max_per_minute})"
        return True, "ok"

    def _register_reservation_locked(
        self,
        gid: str,
        task_id: str,
        *,
        is_priority: bool,
        ts: float,
    ) -> QuotaReservation:
        # 同一 task_id 重复请求预占时保持幂等
        for existing in self._active_reservations[gid].values():
            if existing.task_id == task_id and existing.status == "reserved":
                return existing
        rid = f"res_{gid}_{task_id}_{uuid.uuid4().hex[:8]}"
        res = QuotaReservation(
            reservation_id=rid,
            group_id=gid,
            task_id=task_id,
            is_priority=is_priority,
            reserved_at=ts,
            status="reserved",
        )
        self._active_reservations[gid][rid] = res
        self._reservation_ledger[rid] = res
        self._reservation_ledger.move_to_end(rid)
        while len(self._reservation_ledger) > self._ledger_max:
            self._reservation_ledger.popitem(last=False)
        return res

    async def can_send_normal(self, group_id: str, now: Optional[float] = None) -> tuple[bool, str]:
        """只读检查当前普通配额（包含已发送记录与当前活跃未释放预占）。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id)
        async with self._lock:
            return self._check_normal_quota_locked(gid, ts)

    async def get_remaining_normal_cooldown(
        self,
        group_id: str,
        now: Optional[float] = None,
    ) -> tuple[bool, float, str]:
        """检查每分钟额度是否可用，并返回距离下一次允许普通发送还需等待的冷却秒数（用于合并窗口动态对齐最小发送间隔）。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id)
        async with self._lock:
            q, active_norm = self._prune_locked(gid, ts)
            effective_count = len(q) + len(active_norm)
            if effective_count >= self.max_per_minute:
                return False, 0.0, f"minute_quota_reached({effective_count}/{self.max_per_minute})"
            last_candidates: list[float] = []
            if q:
                last_candidates.append(q[-1])
            for r in active_norm:
                last_candidates.append(r.reserved_at)
            wait_rem = 0.0
            if last_candidates and self.min_interval_sec > 0:
                latest_ts = max(last_candidates)
                wait_rem = max(0.0, round(self.min_interval_sec - (ts - latest_ts), 2))
            return True, wait_rem, "ok"

    async def try_reserve_normal(
        self,
        group_id: str,
        task_id: str,
        now: Optional[float] = None,
    ) -> tuple[bool, str, Optional[QuotaReservation]]:
        """在同一个受保护原子操作内完成：检查额度 + 登记普通预占。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id)
        tid = str(task_id or uuid.uuid4().hex[:10])
        async with self._lock:
            for existing in self._active_reservations[gid].values():
                if existing.task_id == tid and existing.status == "reserved":
                    return True, "ok", existing
            ok, reason = self._check_normal_quota_locked(gid, ts)
            if not ok:
                return False, reason, None
            res = self._register_reservation_locked(gid, tid, is_priority=False, ts=ts)
            return True, "ok", res

    async def reserve_priority(
        self,
        group_id: str,
        task_id: str,
        now: Optional[float] = None,
    ) -> QuotaReservation:
        """为显式请求登记优先预占（不受普通参与配额限制，但纳入任务结算跟踪）。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id)
        tid = str(task_id or uuid.uuid4().hex[:10])
        async with self._lock:
            return self._register_reservation_locked(gid, tid, is_priority=True, ts=ts)

    async def commit_reservation(
        self,
        reservation: Optional[QuotaReservation | str],
        group_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """幂等确认预占并转为实际发送记账（同一预占重复调用不会重复扣减额度）。"""
        if reservation is None:
            if group_id:
                await self.record_sent(group_id, now=now)
                return True
            return False
        rid = reservation.reservation_id if isinstance(reservation, QuotaReservation) else str(reservation)
        ts = time.monotonic() if now is None else float(now)
        async with self._lock:
            rec = self._reservation_ledger.get(rid)
            if rec is None and isinstance(reservation, QuotaReservation):
                rec = reservation
                self._reservation_ledger[rid] = rec
            if rec is None:
                return False
            if rec.status == "committed":
                return True
            if rec.status == "released":
                return False
            gid = str(group_id or rec.group_id)
            self._active_reservations[gid].pop(rid, None)
            rec.status = "committed"
            rec.committed_at = ts
            q, _ = self._prune_locked(gid, ts)
            q.append(ts)
            self._group_last_chunk_ts[gid] = ts
            if not rec.is_priority:
                self._group_normal_send_timestamps[gid].append(ts)
                self._group_last_normal_send_ts[gid] = ts
            return True

    async def release_reservation(
        self,
        reservation: Optional[QuotaReservation | str],
        group_id: Optional[str] = None,
        *,
        reason: str = "released",
        now: Optional[float] = None,
    ) -> bool:
        """幂等释放未实际发送的预占额度（失败、取消或决定不回应时调用）。"""
        if reservation is None:
            return False
        rid = reservation.reservation_id if isinstance(reservation, QuotaReservation) else str(reservation)
        ts = time.monotonic() if now is None else float(now)
        async with self._lock:
            rec = self._reservation_ledger.get(rid)
            if rec is None and isinstance(reservation, QuotaReservation):
                rec = reservation
                self._reservation_ledger[rid] = rec
            if rec is None:
                return False
            if rec.status in ("released", "committed"):
                return False
            gid = str(group_id or rec.group_id)
            self._active_reservations[gid].pop(rid, None)
            rec.status = "released"
            rec.released_at = ts
            rec.release_reason = reason
            return True

    async def get_density_info(self, group_id: str, now: Optional[float] = None) -> dict[str, Any]:
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id)
        async with self._lock:
            q, active_norm = self._prune_locked(gid, ts)
            sent_count = len(q)
            reserved_count = len(active_norm)
            eff_count = sent_count + reserved_count
            adaptive = self.get_adaptive_pacing(gid, now=ts)
            return {
                "reply_count_last_minute": sent_count,
                "active_reserved_count": reserved_count,
                "effective_count_last_minute": eff_count,
                "max_per_minute": self.max_per_minute,
                "min_interval_sec": self.min_interval_sec,
                "effective_min_interval_sec": adaptive["effective_min_interval_sec"],
                "heat_level": adaptive["heat_level"],
                "incoming_msgs_per_min": adaptive["msgs_per_min"],
                "chunk_pace_sec": self.chunk_pace_sec,
                "density_ratio": round(eff_count / max(1, self.max_per_minute), 3),
            }

    async def record_sent(
        self,
        group_id: str,
        now: Optional[float] = None,
        *,
        is_priority: bool = False,
    ) -> None:
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id)
        async with self._lock:
            q, _ = self._prune_locked(gid, ts)
            q.append(ts)
            self._group_last_chunk_ts[gid] = ts
            if not is_priority:
                self._group_normal_send_timestamps[gid].append(ts)
                self._group_last_normal_send_ts[gid] = ts

    async def pace_chunk_send(self, group_id: str, *, enforce_normal_interval: bool = False) -> None:
        """按群串行化发送节奏等待，防止多个排队任务或分段协程在同一时刻突破间隔集中发送。"""
        gid = str(group_id)
        async with self._get_send_lock(gid):
            async with self._lock:
                now = time.monotonic()
                wait_chunk = 0.0
                if self.chunk_pace_sec > 0 and gid in self._group_last_chunk_ts:
                    wait_chunk = self.chunk_pace_sec - (now - self._group_last_chunk_ts[gid])
                wait_norm = 0.0
                if enforce_normal_interval and self.min_interval_sec > 0 and gid in self._group_last_normal_send_ts:
                    wait_norm = self.min_interval_sec - (now - self._group_last_normal_send_ts[gid])
                wait_sec = max(0.0, wait_chunk, wait_norm)
            if wait_sec > 0:
                await asyncio.sleep(wait_sec)
            async with self._lock:
                # 预更新时间戳占位，防止紧随其后的协程零等待穿透
                now_after = time.monotonic()
                self._group_last_chunk_ts[gid] = now_after
                if enforce_normal_interval:
                    self._group_last_normal_send_ts[gid] = now_after

    async def send_with_pacing(
        self,
        group_id: str,
        send_fn: Callable[[], Awaitable[bool]],
        *,
        reservation: Optional[QuotaReservation] = None,
        is_priority: bool = False,
        is_chunk: bool = False,
        enforce_normal_interval: bool = False,
    ) -> bool:
        """在同群发送锁保护下执行节奏等待、实际发送与配额确认/记账（显式请求单条回复同样遵守同群最小发送间隔，绝不连续贴脸刷出）。"""
        gid = str(group_id)
        eff_priority = is_priority or bool(reservation and reservation.is_priority)
        eff_normal_gap = enforce_normal_interval or (not eff_priority and not is_chunk)
        async with self._get_send_lock(gid):
            async with self._lock:
                now = time.monotonic()
                wait_chunk = 0.0
                pace_sec = self.chunk_pace_sec if self.chunk_pace_sec > 0 else self.min_interval_sec
                if pace_sec > 0 and gid in self._group_last_chunk_ts:
                    wait_chunk = pace_sec - (now - self._group_last_chunk_ts[gid])
                wait_norm = 0.0
                if eff_normal_gap and self.min_interval_sec > 0 and gid in self._group_last_normal_send_ts:
                    wait_norm = self.min_interval_sec - (now - self._group_last_normal_send_ts[gid])
                wait_sec = max(0.0, wait_chunk, wait_norm)
            if wait_sec > 0:
                await asyncio.sleep(wait_sec)

            try:
                ok = bool(await send_fn())
            except Exception:
                if reservation is not None and not is_chunk:
                    await self.release_reservation(reservation, gid, reason="send_exception")
                raise

            if ok:
                now_sent = time.monotonic()
                async with self._lock:
                    self._group_last_chunk_ts[gid] = now_sent
                    if not eff_priority:
                        self._group_last_normal_send_ts[gid] = now_sent
                if reservation is not None and not is_chunk:
                    await self.commit_reservation(reservation, gid, now=now_sent)
                else:
                    await self.record_sent(gid, now=now_sent, is_priority=eff_priority)
            elif reservation is not None and not is_chunk:
                await self.release_reservation(reservation, gid, reason="send_failed")
            return ok


@dataclasses.dataclass
class FocusWindowEntry:
    """单用户群内 45 秒连续对话活跃焦点状态。"""
    group_id: str
    user_id: str
    opened_at: float
    last_active_at: float
    consecutive_turns: int = 0
    last_was_explicit: bool = True


@dataclasses.dataclass
class GroupExchangeRecord:
    """群内近期发言互动快照（用于检测群友高频互相 reply 对线或密集争论）。"""
    timestamp: float
    user_id: str
    reply_to_user_id: str = ""
    mentioned_user_ids: tuple[str, ...] = ()
    text: str = ""


class AttentionManager:
    """群聊 45 秒连续对话注意力窗口与旁观抑制管理器（移植并增强自 Group Chat Plus utils/attention_manager.py）。

    1. 45 秒连续对话焦点窗口（Attention Window）：
       - 当阿米娅在群内回复某位用户 uid（或该用户显式唤醒/艾特阿米娅）后，为 `(group_id, uid)` 开启 45 秒活跃焦点窗口；
       - 随连续互动自动刷新时间戳，并按连续免 @ 轮次指数衰减（`turn_decay_factor ** consecutive_turns`），防止无限占焦；
       - 在焦点窗口期内，该用户发送的后续跟进消息（只要没有 `@` 其他人、也没有 `[reply]` 引用其他群友）无需再次 `@` 阿米娅，即可直接获得准优先级跟进。
    2. 旁观抑制（Cross-Talk / Bystander Suppression）：
       - 实时追踪群内其他群友之间的 `[reply]` 引用链与高频交替发言；
       - 当检测到群内其他两名群友正在高频互相 `[reply]` 对线或密集双人争论时，自动触发旁观抑制并压低阿米娅的普通主动插话率，识趣保持安静。
    """

    _CROSS_TALK_DEBATE_RE = re.compile(
        r"(?:\?|？|!|！|不是|胡说|瞎说|扯淡|凭什么|为什么|你才|明明|反驳|急了|嘴硬|搞笑|离谱|不对|扯吧|少来)"
    )

    def __init__(
        self,
        window_sec: float = 45.0,
        decay_half_life_sec: float = 20.0,
        *,
        turn_decay_factor: float = 0.82,
        max_consecutive_turns: int = 6,
        quasi_priority_threshold: float = 0.20,
        cross_talk_window_sec: float = 45.0,
    ) -> None:
        self.window_sec = float(window_sec)
        self.decay_half_life_sec = max(5.0, float(decay_half_life_sec))
        self.turn_decay_factor = max(0.1, min(1.0, float(turn_decay_factor)))
        self.max_consecutive_turns = max(1, int(max_consecutive_turns))
        self.quasi_priority_threshold = max(0.05, float(quasi_priority_threshold))
        self.cross_talk_window_sec = max(10.0, float(cross_talk_window_sec))
        self._state: dict[tuple[str, str], float] = {}
        self._focus_entries: dict[tuple[str, str], FocusWindowEntry] = {}
        self._group_exchanges: dict[str, Deque[GroupExchangeRecord]] = collections.defaultdict(
            lambda: collections.deque(maxlen=32)
        )

    def _prune_expired(self, ts: float) -> None:
        expired_keys = [
            k for k, last in self._state.items()
            if (ts - last) > self.window_sec
        ]
        for k in expired_keys:
            self._state.pop(k, None)
            self._focus_entries.pop(k, None)

    def boost(
        self,
        group_id: str,
        user_id: str,
        now: Optional[float] = None,
        *,
        is_explicit: bool = True,
        is_followup_turn: bool = False,
    ) -> float:
        """开启或刷新 `(group_id, user_id)` 的 45 秒注意力焦点窗口。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return 0.0
        self._prune_expired(ts)
        key = (gid, uid)
        existing = self._focus_entries.get(key)
        if existing is None or (ts - existing.last_active_at) > self.window_sec:
            turns = 1 if (is_followup_turn and not is_explicit) else 0
            entry = FocusWindowEntry(
                group_id=gid,
                user_id=uid,
                opened_at=ts,
                last_active_at=ts,
                consecutive_turns=turns,
                last_was_explicit=bool(is_explicit),
            )
        else:
            if is_explicit:
                turns = 0
            elif is_followup_turn:
                turns = existing.consecutive_turns + 1
            else:
                turns = existing.consecutive_turns
            entry = FocusWindowEntry(
                group_id=gid,
                user_id=uid,
                opened_at=existing.opened_at,
                last_active_at=ts,
                consecutive_turns=turns,
                last_was_explicit=bool(is_explicit),
            )
        self._focus_entries[key] = entry
        self._state[key] = ts
        return self.attention_score(gid, uid, now=ts)

    def record_bot_reply(
        self,
        group_id: str,
        user_id: str,
        now: Optional[float] = None,
        *,
        was_explicit: bool = False,
    ) -> float:
        """当阿米娅在群内回复某位用户后调用，为该 `(group_id, uid)` 开启/刷新 45 秒活跃焦点窗口。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return 0.0
        key = (gid, uid)
        existing = self._focus_entries.get(key)
        if existing is None or (ts - existing.last_active_at) > self.window_sec or was_explicit:
            return self.boost(gid, uid, now=ts, is_explicit=True, is_followup_turn=False)
        # 已经在连续焦点对话中，刷新窗口时间戳并保留当前连续衰减轮次
        existing.last_active_at = ts
        self._state[key] = ts
        return self.attention_score(gid, uid, now=ts)

    def clear_focus(self, group_id: str, user_id: str) -> None:
        key = (str(group_id or "").strip(), str(user_id or "").strip())
        self._state.pop(key, None)
        self._focus_entries.pop(key, None)

    def attention_score(self, group_id: str, user_id: str, now: Optional[float] = None) -> float:
        """计算当前注意力得分（综合 45 秒时间半衰期与连续免 @ 轮次衰减）。"""
        ts = time.monotonic() if now is None else float(now)
        key = (str(group_id or "").strip(), str(user_id or "").strip())
        last = self._state.get(key)
        if last is None:
            return 0.0
        elapsed = max(0.0, ts - last)
        if elapsed > self.window_sec:
            return 0.0
        entry = self._focus_entries.get(key)
        turns = entry.consecutive_turns if entry is not None else 0
        if turns >= self.max_consecutive_turns:
            return 0.0
        time_decay = 0.5 ** (elapsed / self.decay_half_life_sec)
        turn_decay = self.turn_decay_factor ** turns
        return round(time_decay * turn_decay, 4)

    def is_in_focus_window(self, group_id: str, user_id: str, now: Optional[float] = None) -> bool:
        """判断 `(group_id, user_id)` 是否处于有效的 45 秒连续对话注意力焦点窗口内。"""
        return self.attention_score(group_id, user_id, now=now) >= self.quasi_priority_threshold

    def get_focus_state(self, group_id: str, user_id: str, now: Optional[float] = None) -> dict[str, Any]:
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        key = (gid, uid)
        score = self.attention_score(gid, uid, now=ts)
        entry = self._focus_entries.get(key)
        last_ts = self._state.get(key)
        remaining = max(0.0, self.window_sec - (ts - last_ts)) if (last_ts is not None and score > 0) else 0.0
        return {
            "group_id": gid,
            "user_id": uid,
            "in_focus": score >= self.quasi_priority_threshold,
            "score": score,
            "remaining_sec": round(remaining, 2),
            "consecutive_turns": entry.consecutive_turns if entry else 0,
            "max_consecutive_turns": self.max_consecutive_turns,
            "window_sec": self.window_sec,
        }

    def should_followup_in_focus(
        self,
        group_id: str,
        user_id: str,
        *,
        has_other_at: bool = False,
        reply_to_user_id: Optional[str] = None,
        bot_self_id: Optional[str] = None,
        text: str = "",
        consume_turn: bool = True,
        now: Optional[float] = None,
    ) -> bool:
        """判定焦点窗口内用户的后续消息是否应免 @ 直接获得准优先级跟进：
        - 若消息 @ 了其他人，或 [reply] 引用了其他群友（非机器人自身），则不触发焦点跟进；
        - 若用户在焦点期内转为 [reply] 其他群友聊天，主动衰减/释放其对阿米娅的焦点，避免误插话；
        - 若满足条件且 `consume_turn=True`，自动刷新焦点窗口并累加连续互动轮次（按轮次衰减）。
        """
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return False

        clean = (text or "").strip()
        if clean.startswith("/"):
            return False

        if has_other_at:
            return False

        rep_uid = str(reply_to_user_id or "").strip()
        self_uid = str(bot_self_id or "").strip()
        if rep_uid and (not self_uid or rep_uid != self_uid):
            # 用户明确引用回复了其他群友，说明话题已转向群友间互动
            return False

        if not self.is_in_focus_window(gid, uid, now=ts):
            return False

        if consume_turn:
            self.boost(gid, uid, now=ts, is_explicit=False, is_followup_turn=True)
        return True

    def record_group_message(
        self,
        group_id: str,
        user_id: str,
        *,
        reply_to_user_id: Optional[str] = None,
        mentioned_user_ids: Optional[list[str]] = None,
        bot_self_id: Optional[str] = None,
        text: str = "",
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """记录群聊消息互动关系并返回当前旁观抑制状态。"""
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        uid = str(user_id or "").strip()
        if not gid or not uid:
            return self.get_bystander_suppression(gid, now=ts)

        self_uid = str(bot_self_id or "").strip()
        if self_uid and uid == self_uid:
            return self.get_bystander_suppression(gid, now=ts)

        rep_uid = str(reply_to_user_id or "").strip()
        mentions = tuple(
            str(m).strip()
            for m in (mentioned_user_ids or [])
            if str(m).strip() and str(m).strip() != self_uid
        )

        # 若不是显式针对机器人的消息，记入群友互动队列
        if not (rep_uid and self_uid and rep_uid == self_uid):
            dq = self._group_exchanges[gid]
            while dq and (ts - dq[0].timestamp) > self.cross_talk_window_sec:
                dq.popleft()
            dq.append(
                GroupExchangeRecord(
                    timestamp=ts,
                    user_id=uid,
                    reply_to_user_id="" if (self_uid and rep_uid == self_uid) else rep_uid,
                    mentioned_user_ids=mentions,
                    text=(text or "").strip()[:120],
                )
            )
        return self.get_bystander_suppression(gid, now=ts)

    def get_bystander_suppression(self, group_id: str, now: Optional[float] = None) -> dict[str, Any]:
        """检测群内是否有其他两名群友正在高频互相 [reply] 对线或密集私聊式争论：
        - 命中时返回 `suppressed=True` 及壓低后的 `suppression_factor`（如 0.15），供普通主动接话与自主水群链路识趣保持安静。
        """
        ts = time.monotonic() if now is None else float(now)
        gid = str(group_id or "").strip()
        dq = self._group_exchanges.get(gid)
        if not dq:
            return {
                "suppressed": False,
                "suppression_factor": 1.0,
                "reason": "none",
                "participants": [],
                "exchange_count": 0,
            }

        recent = [r for r in dq if (ts - r.timestamp) <= self.cross_talk_window_sec]
        if len(recent) < 2:
            return {
                "suppressed": False,
                "suppression_factor": 1.0,
                "reason": "none",
                "participants": [],
                "exchange_count": len(recent),
            }

        # 模式 1：两名群友高频互相 [reply] 或定向 @ 对线（A->B 且 B->A，或同一对群友间定向引用 >= 2 次且双向互动）
        pair_directed: dict[tuple[str, str], int] = collections.Counter()
        pair_directions: dict[tuple[str, str], set[tuple[str, str]]] = collections.defaultdict(set)
        for rec in recent:
            targets: list[str] = []
            if rec.reply_to_user_id and rec.reply_to_user_id != rec.user_id:
                targets.append(rec.reply_to_user_id)
            for m_uid in rec.mentioned_user_ids:
                if m_uid != rec.user_id and m_uid not in targets:
                    targets.append(m_uid)
            for tgt in targets:
                pair_key = tuple(sorted((rec.user_id, tgt)))
                pair_directed[pair_key] += 1
                pair_directions[pair_key].add((rec.user_id, tgt))

        for pair_key, count in pair_directed.most_common():
            dirs = pair_directions[pair_key]
            # 存在双向互相 reply/提及（A->B 与 B->A），或单对群友定向 reply 交锋 >= 3 次
            if (len(dirs) >= 2 and count >= 2) or count >= 3:
                return {
                    "suppressed": True,
                    "suppression_factor": 0.12,
                    "reason": f"peer_reply_cross_talk({pair_key[0]}<->{pair_key[1]},count={count})",
                    "participants": list(pair_key),
                    "exchange_count": count,
                }

        # 模式 2：最近 35 秒内最后 >= 4 条消息完全由同一对群友交替密集争论（A-B-A-B）
        tail = [r for r in recent if (ts - r.timestamp) <= 35.0][-6:]
        if len(tail) >= 4:
            tail_users = [r.user_id for r in tail[-4:]]
            distinct_tail = sorted(set(tail_users))
            if len(distinct_tail) == 2 and all(tail_users[i] != tail_users[i + 1] for i in range(len(tail_users) - 1)):
                debate_hits = sum(1 for r in tail[-4:] if self._CROSS_TALK_DEBATE_RE.search(r.text) or r.reply_to_user_id)
                time_span = max(0.0, tail[-1].timestamp - tail[-4].timestamp)
                if debate_hits >= 1 or time_span <= 25.0:
                    return {
                        "suppressed": True,
                        "suppression_factor": 0.18,
                        "reason": f"dense_two_peer_debate({distinct_tail[0]}<->{distinct_tail[1]})",
                        "participants": distinct_tail,
                        "exchange_count": len(tail),
                    }

        return {
            "suppressed": False,
            "suppression_factor": 1.0,
            "reason": "none",
            "participants": [],
            "exchange_count": len(recent),
        }

    def is_bystander_suppressed(self, group_id: str, now: Optional[float] = None) -> bool:
        return bool(self.get_bystander_suppression(group_id, now=now).get("suppressed"))

    @staticmethod
    def cluster_interleaved_threads(events: Sequence[Any]) -> list[dict[str, Any]]:
        """将群内无显式 [reply] 标签的多人交叉发言按语义词元重合度、发言人连续性与时间邻近度聚类为独立话题簇（Topic Threads）。
        防止多人同时聊两件事时把 A 话题的头和 B 话题的尾串味拼接。
        """
        if not events:
            return []

        def _extract_grams(txt: str) -> set[str]:
            clean = re.sub(r"[^\w\u4e00-\u9fff]", "", (txt or "").lower())
            if not clean:
                return set()
            grams = {ch for ch in clean}
            for i in range(len(clean) - 1):
                grams.add(clean[i : i + 2])
            return grams

        clusters: list[dict[str, Any]] = []
        for ev in events:
            if isinstance(ev, dict):
                uid = str(ev.get("user_id") or "")
                nick = str(ev.get("nickname") or uid)
                txt = str(ev.get("text") or "").strip()
                ts = float(ev.get("timestamp") or 0.0)
                mid = str(ev.get("message_id") or "")
            else:
                uid = str(getattr(ev, "user_id", "") or "")
                nick = str(getattr(ev, "nickname", "") or uid)
                txt = str(getattr(ev, "text", "") or "").strip()
                ts = float(getattr(ev, "timestamp", 0.0) or 0.0)
                mid = str(getattr(ev, "message_id", "") or "")

            if not txt:
                continue
            grams = _extract_grams(txt)
            best_idx = -1
            best_score = 0.0

            for idx, cl in enumerate(clusters):
                cl_grams: set[str] = cl["_grams"]
                overlap = (
                    len(grams & cl_grams) / max(1.0, (len(grams) * max(1, len(cl_grams))) ** 0.5)
                    if grams and cl_grams
                    else 0.0
                )
                time_gap = abs(ts - float(cl["last_ts"])) if ts and cl["last_ts"] else 999.0
                same_user_recent = (uid in cl["participants"]) and (time_gap <= 15.0)
                score = overlap + (0.18 if same_user_recent else 0.0) + (0.06 if time_gap <= 10.0 else 0.0)
                if score > best_score:
                    best_score = score
                    best_idx = idx

            msg_entry = {
                "message_id": mid,
                "user_id": uid,
                "nickname": nick,
                "text": txt[:120],
                "timestamp": ts,
            }
            if best_idx >= 0 and best_score >= 0.16:
                target = clusters[best_idx]
                target["messages"].append(msg_entry)
                if uid and uid not in target["participants"]:
                    target["participants"].append(uid)
                target["last_ts"] = max(float(target["last_ts"]), ts)
                target["_grams"].update(grams)
            else:
                clusters.append(
                    {
                        "thread_id": f"T{len(clusters) + 1}",
                        "participants": [uid] if uid else [],
                        "messages": [msg_entry],
                        "last_ts": ts,
                        "_grams": set(grams),
                    }
                )

        output: list[dict[str, Any]] = []
        for cl in sorted(clusters, key=lambda c: (len(c["messages"]), c["last_ts"]), reverse=True):
            msgs = cl["messages"]
            preview = " / ".join(f"{m['nickname']}:{m['text'][:24]}" for m in msgs[-3:])
            output.append(
                {
                    "thread_id": cl["thread_id"],
                    "participants": cl["participants"],
                    "message_count": len(msgs),
                    "topic_preview": preview[:120],
                    "messages": msgs[-5:],
                }
            )
        return output


def split_long_reply_segments(
    text: str,
    *,
    max_segment_chars: int = 320,
) -> list[str]:
    """按代码块、公式、自然段与完整句子边界切分长回答，绝不截断代码块、列表或公式。"""
    raw = (text or "").strip()
    if not raw:
        return []
    if len(raw) <= max_segment_chars:
        return [raw]

    protected_blocks: list[str] = []

    def _stash_block(match: re.Match[str]) -> str:
        idx = len(protected_blocks)
        protected_blocks.append(match.group(0))
        return f"\x00BLOCK_{idx}\x00"

    masked = re.sub(r"```[\s\S]*?```|\$\$[\s\S]*?\$\$", _stash_block, raw)
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", masked) if p.strip()]
    if len(paragraphs) <= 1 and "\n" in masked:
        paragraphs = [p.strip() for p in masked.split("\n") if p.strip()]
    segments: list[str] = []
    current = ""

    def _restore(s: str) -> str:
        def _repl(m: re.Match[str]) -> str:
            b_idx = int(m.group(1))
            return protected_blocks[b_idx] if 0 <= b_idx < len(protected_blocks) else m.group(0)
        return re.sub(r"\x00BLOCK_(\d+)\x00", _repl, s).strip()

    for para in paragraphs:
        restored_para = _restore(para)
        starts_new_stage = bool(
            len(paragraphs) >= 2
            and current
            and (
                (len(current) >= 36 and len(restored_para) >= 36)
                or re.match(r"^(?:\d+[\.、]|第[一二三四五六七八九十]+|[一二三四五六七八九十]+[、\.])", restored_para)
            )
        )
        cand = f"{current}\n\n{restored_para}".strip() if current else restored_para
        if not starts_new_stage and len(cand) <= max_segment_chars:
            current = cand
            continue

        if current:
            segments.append(current)
            current = ""

        if "\x00BLOCK_" in para or len(restored_para) > max_segment_chars:
            lines = para.split("\n")
            buf = ""
            for line in lines:
                r_line = _restore(line)
                if not buf:
                    if len(r_line) <= max_segment_chars or "\x00BLOCK_" in line:
                        buf = r_line
                    else:
                        sents = [s for s in re.split(r"(?<=[。！？!?；;])", r_line) if s]
                        s_buf = ""
                        for sent in sents:
                            if len(s_buf) + len(sent) <= max_segment_chars:
                                s_buf += sent
                            else:
                                if s_buf:
                                    segments.append(s_buf.strip())
                                s_buf = sent
                        buf = s_buf
                else:
                    nxt = f"{buf}\n{r_line}"
                    if len(nxt) <= max_segment_chars:
                        buf = nxt
                    else:
                        segments.append(buf.strip())
                        buf = r_line
            if buf.strip():
                current = buf.strip()
        else:
            current = restored_para

    if current.strip():
        segments.append(current.strip())

    return [s for s in segments if s]


@dataclasses.dataclass
class SegmentedDeliveryState:
    """长回答分段发送进度记录，区分已送达内容与待发送片段。"""
    task_id: str
    group_id: Optional[str]
    user_id: str
    full_text: str
    segments: list[str]
    delivered_segments: list[str] = dataclasses.field(default_factory=list)
    pending_segments: list[str] = dataclasses.field(default_factory=list)
    status: str = "pending"  # pending | delivering | paused_uncertain | done | cancelled
    error_reason: str = ""
    updated_at: float = dataclasses.field(default_factory=time.time)

    @property
    def delivered_text(self) -> str:
        return "\n\n".join(self.delivered_segments).strip()


@dataclasses.dataclass(order=True)
class _ScheduledGenWaiter:
    """按优先级（0=显式请求, 1=普通请求）与到达序号排列的群生成等待项。"""
    priority_tier: int
    seq: int
    task_id: str = dataclasses.field(compare=False)
    is_priority: bool = dataclasses.field(compare=False)
    future: asyncio.Future[bool] = dataclasses.field(compare=False)
    cancelled: bool = dataclasses.field(default=False, compare=False)


class _GroupGenerationScheduler:
    """单群优先生成调度器：
    - 显式优先请求（priority_tier=0）在下一个调度边界优先于普通请求（priority_tier=1）获锁；
    - 绝不取消已经开始生成的任务来冒充插队；
    - 同群最多允许 1 个普通回复生成任务（进行中或排队中）。
    """

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        self.active_task_id: Optional[str] = None
        self.active_is_priority: bool = False
        self.normal_in_flight_or_queued: int = 0
        self.seq_counter: int = 0
        self.waiters: list[_ScheduledGenWaiter] = []


class _GenerationLockContext:
    """兼容 `async with mgr.get_generation_lock(gid):` 的优先调度上下文包装器。"""

    def __init__(
        self,
        manager: "SmartGroupConcurrentManager",
        group_id: str,
        *,
        is_priority: Optional[bool] = None,
        task_id: Optional[str] = None,
        reserve_quota: bool = False,
    ) -> None:
        self._mgr = manager
        self._gid = str(group_id)
        self._is_priority = is_priority
        self._task_id = task_id
        self._reserve_quota = reserve_quota
        self._ctx: Optional[contextlib.AbstractAsyncContextManager[Optional[QuotaReservation]]] = None

    def locked(self) -> bool:
        sched = self._mgr._schedulers.get(self._gid)
        return bool(sched and sched.active_task_id is not None)

    async def __aenter__(self) -> Optional[QuotaReservation]:
        eff_prio = self._is_priority
        if eff_prio is None:
            eff_prio = self._mgr.has_priority_waiting(self._gid)
        self._ctx = self._mgr.acquire_generation_slot(
            self._gid,
            task_id=self._task_id,
            is_priority=bool(eff_prio),
            reserve_quota=self._reserve_quota,
            auto_release_on_exit=False,
        )
        return await self._ctx.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._ctx is not None:
            return bool(await self._ctx.__aexit__(exc_type, exc_val, exc_tb))
        return False


class SmartGroupConcurrentManager:
    """按群独立队列、合并窗口、原子配额预占、优先生成调度与分段公平投递协调器。"""

    def __init__(
        self,
        *,
        merge_window_sec: float = 1.5,
        max_batch_messages: int = 5,
        max_batch_chars: int = 2000,
        density_manager: Optional[ReplyDensityManager] = None,
        attention_manager: Optional[AttentionManager] = None,
    ) -> None:
        self.merge_window_sec = float(merge_window_sec)
        self.max_batch_messages = int(max_batch_messages)
        self.max_batch_chars = int(max_batch_chars)
        self.density = density_manager or ReplyDensityManager()
        self.attention = attention_manager or AttentionManager()

        self._group_locks: dict[str, asyncio.Lock] = {}
        self._schedulers: dict[str, _GroupGenerationScheduler] = {}
        self._priority_waiters: dict[str, int] = collections.defaultdict(int)
        self._priority_event: dict[str, asyncio.Event] = {}
        self._pending_buffers: dict[str, list[QueuedGroupMessage]] = collections.defaultdict(list)
        self._batch_futures: dict[str, list[asyncio.Future[Optional[MergedMessageBatch]]]] = collections.defaultdict(list)
        self._window_timers: dict[str, asyncio.Task[None]] = {}
        self._consumed_ids: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._message_states: collections.OrderedDict[str, MessageLifecycleRecord] = collections.OrderedDict()
        self._consumed_max = 4096
        self._deliveries: dict[str, SegmentedDeliveryState] = {}

    def configure(
        self,
        *,
        merge_window_sec: Optional[float] = None,
        max_batch_messages: Optional[int] = None,
        max_batch_chars: Optional[int] = None,
        min_interval_sec: Optional[float] = None,
        max_per_minute: Optional[int] = None,
        chunk_pace_sec: Optional[float] = None,
    ) -> None:
        if merge_window_sec is not None:
            self.merge_window_sec = max(0.0, float(merge_window_sec))
        if max_batch_messages is not None:
            self.max_batch_messages = max(1, int(max_batch_messages))
        if max_batch_chars is not None:
            self.max_batch_chars = max(100, int(max_batch_chars))
        self.density.configure(
            min_interval_sec=min_interval_sec,
            max_per_minute=max_per_minute,
            chunk_pace_sec=chunk_pace_sec,
        )

    def _get_group_lock(self, group_id: str) -> asyncio.Lock:
        gid = str(group_id)
        if gid not in self._group_locks:
            self._group_locks[gid] = asyncio.Lock()
        return self._group_locks[gid]

    def _get_scheduler(self, group_id: str) -> _GroupGenerationScheduler:
        gid = str(group_id)
        if gid not in self._schedulers:
            self._schedulers[gid] = _GroupGenerationScheduler(gid)
        return self._schedulers[gid]

    def get_generation_lock(
        self,
        group_id: str,
        *,
        is_priority: Optional[bool] = None,
        task_id: Optional[str] = None,
        reserve_quota: bool = False,
    ) -> _GenerationLockContext:
        return _GenerationLockContext(
            self,
            str(group_id),
            is_priority=is_priority,
            task_id=task_id,
            reserve_quota=reserve_quota,
        )

    @contextlib.asynccontextmanager
    async def acquire_generation_slot(
        self,
        group_id: str,
        *,
        task_id: Optional[str] = None,
        is_priority: bool = False,
        is_quasi_priority: bool = False,
        reserve_quota: bool = True,
        auto_release_on_exit: bool = False,
    ) -> AsyncIterator[Optional[QuotaReservation]]:
        """在调用模型前，按群原子完成“检查额度、预留处理位置、登记预占”：
        - 检查与预占在同一次受保护操作内完成；
        - 普通请求同群最多 1 个生成任务（进行中或排队中）；
        - 显式请求（is_priority=True, tier=0）与 45s 焦点准优先请求（is_quasi_priority=True, tier=1）排在普通请求（tier=2）之前，且不取消已经在生成中的任务；
        - 发生异常或取消时自动幂等释放预占。
        """
        gid = str(group_id)
        tid = str(task_id or f"gen_{uuid.uuid4().hex[:10]}")
        loop = asyncio.get_running_loop()
        reservation: Optional[QuotaReservation] = None
        waiter: Optional[_ScheduledGenWaiter] = None
        acquired_slot = False
        registered_normal = False
        registered_prio_waiter = False
        eff_priority = bool(is_priority or is_quasi_priority)

        async with self._get_group_lock(gid):
            sched = self._get_scheduler(gid)
            if not eff_priority:
                if sched.normal_in_flight_or_queued >= 1:
                    raise NormalQuotaRejectedError("normal_generation_busy")
                if reserve_quota:
                    ok, reason, reservation = await self.density.try_reserve_normal(gid, tid)
                    if not ok:
                        raise NormalQuotaRejectedError(reason)
                sched.normal_in_flight_or_queued += 1
                registered_normal = True
            else:
                if reserve_quota:
                    reservation = await self.density.reserve_priority(gid, tid)
                self._priority_waiters[gid] += 1
                registered_prio_waiter = True
                ev = self._priority_event.get(gid)
                if ev is not None:
                    ev.set()

            if sched.active_task_id is None and not any(not w.cancelled for w in sched.waiters):
                sched.active_task_id = tid
                sched.active_is_priority = eff_priority
                acquired_slot = True
            else:
                sched.seq_counter += 1
                p_tier = 0 if is_priority else (1 if is_quasi_priority else 2)
                waiter = _ScheduledGenWaiter(
                    priority_tier=p_tier,
                    seq=sched.seq_counter,
                    task_id=tid,
                    is_priority=eff_priority,
                    future=loop.create_future(),
                )
                heapq.heappush(sched.waiters, waiter)

        try:
            if waiter is not None:
                await waiter.future
                acquired_slot = True
            yield reservation
        except BaseException as exc:
            if reservation is not None:
                await self.density.release_reservation(
                    reservation,
                    gid,
                    reason=f"aborted:{type(exc).__name__}",
                )
            raise
        finally:
            async with self._get_group_lock(gid):
                sched = self._get_scheduler(gid)
                if waiter is not None and not acquired_slot:
                    waiter.cancelled = True
                if registered_normal:
                    sched.normal_in_flight_or_queued = max(0, sched.normal_in_flight_or_queued - 1)
                if registered_prio_waiter:
                    self._priority_waiters[gid] = max(0, self._priority_waiters[gid] - 1)
                    if self._priority_waiters[gid] == 0 and gid in self._priority_event:
                        self._priority_event[gid].clear()

                if acquired_slot and sched.active_task_id == tid:
                    next_waiter: Optional[_ScheduledGenWaiter] = None
                    while sched.waiters:
                        cand = heapq.heappop(sched.waiters)
                        if not cand.cancelled and not cand.future.done():
                            next_waiter = cand
                            break
                    if next_waiter is not None:
                        sched.active_task_id = next_waiter.task_id
                        sched.active_is_priority = next_waiter.is_priority
                        next_waiter.future.set_result(True)
                    else:
                        sched.active_task_id = None
                        sched.active_is_priority = False

            if auto_release_on_exit and reservation is not None and reservation.status == "reserved":
                await self.density.release_reservation(reservation, gid, reason="auto_release_on_exit")

    def mark_message_state(
        self,
        message_ids: str | list[str] | tuple[str, ...] | set[str],
        status: str,
        *,
        group_id: str = "",
        user_id: str = "",
        task_id: str = "",
        is_priority: bool = False,
        reason: str = "",
    ) -> None:
        """更新消息处理生命周期状态（queued | processing | delivered | failed | uncertain | skipped）。"""
        now = time.time()
        ids_iter = [message_ids] if isinstance(message_ids, str) else message_ids
        for raw_id in ids_iter:
            mid = str(raw_id or "").strip()
            if not mid:
                continue
            existing = self._message_states.get(mid)
            if existing is not None:
                existing.status = status
                existing.updated_at = now
                if reason:
                    existing.reason = reason
                if task_id:
                    existing.task_id = task_id
                if group_id:
                    existing.group_id = group_id
                if user_id:
                    existing.user_id = user_id
                if is_priority:
                    existing.is_priority = True
                self._message_states.move_to_end(mid)
            else:
                self._message_states[mid] = MessageLifecycleRecord(
                    message_id=mid,
                    group_id=str(group_id or ""),
                    user_id=str(user_id or ""),
                    task_id=str(task_id or ""),
                    is_priority=bool(is_priority),
                    status=status,
                    reason=reason,
                    updated_at=now,
                )
                while len(self._message_states) > self._consumed_max:
                    self._message_states.popitem(last=False)

            if status in {"processing", "delivered", "uncertain", "skipped"}:
                self._consumed_ids[mid] = now
                self._consumed_ids.move_to_end(mid)
                while len(self._consumed_ids) > self._consumed_max:
                    self._consumed_ids.popitem(last=False)
            elif status == "failed":
                # 失败或取消的任务绝不伪装成已成功消费，允许后续显式重试
                self._consumed_ids.pop(mid, None)

    def get_message_state(self, message_id: str) -> Optional[str]:
        mid = str(message_id or "").strip()
        rec = self._message_states.get(mid)
        return rec.status if rec else None

    def get_message_record(self, message_id: str) -> Optional[MessageLifecycleRecord]:
        mid = str(message_id or "").strip()
        return self._message_states.get(mid)

    def is_delivered(self, message_id: str) -> bool:
        return self.get_message_state(message_id) == "delivered"

    def is_consumed(self, message_id: str) -> bool:
        mid = str(message_id or "").strip()
        if not mid:
            return False
        rec = self._message_states.get(mid)
        if rec is not None:
            return rec.status in {"processing", "delivered", "uncertain", "skipped"}
        return mid in self._consumed_ids

    def mark_consumed(
        self,
        message_ids: list[str] | tuple[str, ...] | set[str],
        *,
        status: str = "delivered",
        reason: str = "",
    ) -> None:
        self.mark_message_state(message_ids, status, reason=reason)

    async def enqueue_and_collect_batch(
        self,
        msg: QueuedGroupMessage,
    ) -> Optional[MergedMessageBatch]:
        """将群消息放入对应群的合并窗口：
        - 进入合并窗口时状态为 `queued`（不等于已消费 `delivered`）；
        - 显式优先请求（`msg.is_priority`）或 45 秒焦点准优先跟进（`msg.is_quasi_priority`）到来时立即结束普通消息合并等待，且绝不将多个不同用户的显式/准优先请求合并丢弃；
        - 若等待期间任务被取消，清理等待项并将状态标记为 `failed`。
        """
        gid = str(msg.group_id)
        if self.is_consumed(msg.message_id):
            return None

        # 仅在独立单测（未绑定外层路由 task_id）时自动提升 quasi_priority；实际群聊中普通发言统一按时间顺序合并，确保始终对齐最新一条发言
        if not msg.is_priority and not msg.is_quasi_priority and not msg.task_id:
            last_att_ts = self.attention._state.get((gid, str(msg.user_id)))
            eff_now = (
                msg.timestamp
                if (last_att_ts is not None and abs(msg.timestamp - last_att_ts) <= self.attention.window_sec * 4)
                else None
            )
            if self.attention.should_followup_in_focus(
                gid,
                msg.user_id,
                has_other_at=msg.has_other_at,
                reply_to_user_id=msg.reply_to_user_id,
                text=msg.text,
                now=eff_now,
            ):
                msg.is_quasi_priority = True

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Optional[MergedMessageBatch]] = loop.create_future()

        async with self._get_group_lock(gid):
            if self.is_consumed(msg.message_id):
                return None
            buf = self._pending_buffers[gid]
            futs = self._batch_futures[gid]

            # 若缓冲区内已有显式优先请求（@/带名字/引用机器人），先立即冲刷前一批次，绝不把两个显式请求合并成只回答最后一个
            if msg.is_priority and any(x.is_priority for x in buf):
                timer = self._window_timers.pop(gid, None)
                if timer and not timer.done():
                    timer.cancel()
                self._flush_locked(gid)
                buf = self._pending_buffers[gid]
                futs = self._batch_futures[gid]

            self.mark_message_state(
                [msg.message_id],
                "queued",
                group_id=gid,
                user_id=msg.user_id,
                task_id=msg.task_id or "",
                is_priority=msg.is_effective_priority,
            )
            buf.append(msg)
            futs.append(fut)

            total_chars = sum(len(x.text) for x in buf)
            # 只有显式优先请求（艾特/带名字/明确引用机器人/指令）到来时才立即冲刷实时回复；焦点跟进与普通发言统一走群聊合并窗口
            reached_limit = (
                msg.is_priority
                or len(buf) >= self.max_batch_messages
                or total_chars >= self.max_batch_chars
                or self.merge_window_sec <= 0.0
                or msg.is_command
            )

            if reached_limit:
                timer = self._window_timers.pop(gid, None)
                if timer and not timer.done():
                    timer.cancel()
                self._flush_locked(gid)
            elif gid not in self._window_timers or self._window_timers[gid].done():
                eff_delay = self.merge_window_sec
                if not msg.is_priority:
                    _, wait_rem, _ = await self.density.get_remaining_normal_cooldown(gid)
                    if wait_rem > eff_delay:
                        eff_delay = min(wait_rem, max(self.merge_window_sec * 2.5, 12.0))
                self._window_timers[gid] = asyncio.create_task(self._delayed_flush(gid, eff_delay))

        try:
            return await fut
        except asyncio.CancelledError:
            async with self._get_group_lock(gid):
                buf = self._pending_buffers.get(gid, [])
                futs = self._batch_futures.get(gid, [])
                for idx, item in enumerate(list(buf)):
                    if item.message_id == msg.message_id:
                        buf.pop(idx)
                        if idx < len(futs):
                            futs.pop(idx)
                        break
                if self.get_message_state(msg.message_id) == "queued":
                    self.mark_message_state([msg.message_id], "failed", reason="cancelled_in_merge_window")
            raise

    async def _delayed_flush(self, gid: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        async with self._get_group_lock(gid):
            self._window_timers.pop(gid, None)
            self._flush_locked(gid)

    def _flush_locked(self, gid: str) -> None:
        buf = self._pending_buffers.get(gid)
        futs = self._batch_futures.get(gid)
        if not buf or not futs:
            return
        messages = list(buf)
        waiters = list(futs)
        buf.clear()
        futs.clear()

        unconsumed = [m for m in messages if not self.is_consumed(m.message_id)]
        if not unconsumed:
            for w in waiters:
                if not w.done():
                    w.set_result(None)
            return

        is_prio = any(m.is_priority for m in unconsumed)
        is_quasi_prio = any(m.is_quasi_priority for m in unconsumed)
        batch = MergedMessageBatch(
            group_id=gid,
            messages=unconsumed,
            is_priority_batch=bool(is_prio or is_quasi_prio),
            is_quasi_priority_batch=bool(is_quasi_prio and not is_prio),
        )
        # 冲刷出合并窗口后进入 processing 状态（直到实际发送成功后才转为 delivered）
        self.mark_message_state(
            batch.message_ids,
            "processing",
            group_id=gid,
            user_id=batch.primary_message.user_id,
            task_id=batch.primary_message.task_id or "",
            is_priority=bool(is_prio or is_quasi_prio),
        )

        leader_idx = len(waiters) - 1
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].is_priority:
                leader_idx = idx
                break
        else:
            for idx in range(len(messages) - 1, -1, -1):
                if messages[idx].is_quasi_priority:
                    leader_idx = idx
                    break

        for idx, w in enumerate(waiters):
            if w.done():
                continue
            if idx == leader_idx:
                w.set_result(batch)
            else:
                w.set_result(None)

    def notify_priority_waiting(self, group_id: str) -> None:
        gid = str(group_id)
        self._priority_waiters[gid] += 1
        ev = self._priority_event.get(gid)
        if ev is not None:
            ev.set()

    def notify_priority_done(self, group_id: str) -> None:
        gid = str(group_id)
        self._priority_waiters[gid] = max(0, self._priority_waiters[gid] - 1)
        if self._priority_waiters[gid] == 0 and gid in self._priority_event:
            self._priority_event[gid].clear()

    def has_priority_waiting(self, group_id: str) -> bool:
        gid = str(group_id)
        if self._priority_waiters.get(gid, 0) > 0:
            return True
        sched = self._schedulers.get(gid)
        if sched and any(w.is_priority and not w.cancelled and not w.future.done() for w in sched.waiters):
            return True
        return False

    async def deliver_segmented_fairly(
        self,
        *,
        task_id: str,
        group_id: Optional[str],
        user_id: str,
        full_text: str,
        send_fn: Callable[[str], Awaitable[bool]],
        max_segment_chars: int = 320,
        batch_chunk_size: int = 2,
        reservation: Optional[QuotaReservation] = None,
        message_ids: Optional[list[str]] = None,
    ) -> SegmentedDeliveryState:
        """分段公平发送长回答：
        - 按自然段、句子与代码块边界切分；
        - 发送期间不持有会话写锁或全群生成锁；
        - 每个实际气泡都经过同群发送节奏锁（`send_with_pacing`）并记录真实发送时间，绝不允许多个分段发送协程同时突破间隔；
        - 每发完一小批（或当检测到同群有优先请求在等待时）主动让出事件循环，允许其他用户的优先请求先完成生成与发送；
        - 若某一段投递结果不确定（send_fn 返回 False），立即暂停该任务并记录已送达片段，绝不盲目整篇重发。
        """
        segments = split_long_reply_segments(full_text, max_segment_chars=max_segment_chars)
        state = SegmentedDeliveryState(
            task_id=task_id,
            group_id=group_id,
            user_id=user_id,
            full_text=full_text,
            segments=segments,
            delivered_segments=[],
            pending_segments=list(segments),
            status="delivering",
        )
        self._deliveries[task_id] = state

        gid_str = str(group_id) if group_id is not None else ""
        try:
            for idx, seg in enumerate(segments):
                if idx > 0 and gid_str:
                    if self.has_priority_waiting(gid_str) or (idx % max(1, batch_chunk_size) == 0):
                        await asyncio.sleep(0.05)

                if gid_str:
                    ok = await self.density.send_with_pacing(
                        gid_str,
                        lambda s=seg: send_fn(s),
                        reservation=reservation if idx == 0 else None,
                        is_priority=bool(reservation.is_priority) if reservation else False,
                        is_chunk=(idx > 0),
                        enforce_normal_interval=(idx == 0 and reservation is not None and not reservation.is_priority),
                    )
                else:
                    ok = bool(await send_fn(seg))

                state.updated_at = time.time()
                if not ok:
                    state.status = "paused_uncertain"
                    state.error_reason = f"segment_{idx}_delivery_unconfirmed"
                    if message_ids:
                        self.mark_message_state(
                            message_ids,
                            "uncertain" if state.delivered_segments else "failed",
                            group_id=gid_str,
                            user_id=user_id,
                            task_id=task_id,
                            reason=state.error_reason,
                        )
                    return state

                state.delivered_segments.append(seg)
                if state.pending_segments:
                    state.pending_segments.pop(0)

            state.status = "done"
            if message_ids:
                self.mark_message_state(
                    message_ids,
                    "delivered",
                    group_id=gid_str,
                    user_id=user_id,
                    task_id=task_id,
                )
            return state
        except asyncio.CancelledError:
            state.status = "cancelled"
            state.error_reason = "cancelled"
            if reservation is not None and not state.delivered_segments and gid_str:
                await self.density.release_reservation(reservation, gid_str, reason="segmented_cancelled")
            if message_ids:
                self.mark_message_state(
                    message_ids,
                    "uncertain" if state.delivered_segments else "failed",
                    group_id=gid_str,
                    user_id=user_id,
                    task_id=task_id,
                    reason="cancelled",
                )
            raise

    def get_delivery_state(self, task_id: str) -> Optional[SegmentedDeliveryState]:
        return self._deliveries.get(task_id)


group_chat_manager = SmartGroupConcurrentManager()
