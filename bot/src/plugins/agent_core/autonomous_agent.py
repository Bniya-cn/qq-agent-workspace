"""阿米娅轻量后台自主事件队列与决策器（自主接话、发起群话题、发布 QQ 空间、独立心情状态）。

核心设计准则：
1. 普通消息回复绝不等待本后台判断链；后台消费者通过 NoneBot 生命周期（`on_startup` / `on_shutdown`）管理，按群合并近期事件并批次决策，不为每条群消息单独发起 AI 请求，也不在模块导入时创建后台任务。
2. 与普通回复共享 `group_chat_manager` 的消息生命周期账本 (`is_consumed` / `mark_message_state`)、配额预占 (`try_reserve_normal` / `commit_reservation` / `release_reservation`) 与发送节奏 (`send_with_pacing`)，防止同一事件被两条链路重复回复或突破间隔刷屏。
3. 阿米娅的近期心情 (`AmiyaMoodState`) 是基于互动氛围形成的独立状态，绝不改写 `amiya.txt` 核心人格文件，也绝不把某位群友的个人经历误记成阿米娅自己的经历。
4. 严格落实安静时段（默认 23:00~08:00）、已有闭嘴/静默控制、重复检测与空间配额（默认每天最多 2 条、间隔至少 6 小时、夜间不自动发布；投递不确定时记录为 `uncertain` 且绝不自动重发；每天发 0 条是正常结果）。
5. 空间内容严格基于阿米娅角色设定与当前群聊可见内容及对应群作用域画像，严禁公开任何用户的私聊内容、QQ号、昵称、敏感信息或未经同意的原话；表达伤心或感慨时也不将他人的隐私或遭遇写成公开内容。
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Deque, Iterable, Optional
from zoneinfo import ZoneInfo

import httpx
from nonebot import logger

from .config_manager import BOT_ROOT, ensure_bot_env, state
from .group_chat_manager import QuotaReservation, group_chat_manager
from .user_memory_service import user_memory_store

AUTONOMOUS_STATE_FILE = BOT_ROOT / "data" / "autonomous_state.json"
PENDING_PROMISES_FILE = BOT_ROOT / "data" / "pending_promises.json"
LEARNED_RULES_FILE = BOT_ROOT / "data" / "learned_rules.json"
_TZ = ZoneInfo("Asia/Shanghai")

# 严禁出现在公开空间说说中的隐私特征（QQ号、群号、私聊字眼、联系方式、敏感词等）
_PRIVACY_LEAK_RE = re.compile(
    r"(?:@[0-9A-Za-z_\-\u4e00-\u9fff]+|\b[1-9]\d{4,10}\b|(?:QQ号?|群号|私聊|微信号|手机号|身份证|密码|住址|家庭地址))",
    re.IGNORECASE,
)
_PROTECTED_ROLE_NAMES = frozenset({"阿米娅", "博士", "罗德岛", "凯尔希", "达妮娅", "兔兔", "Amiya", "Kal'tsit", "Dania"})

_CN_NUM_MAP: dict[str, int] = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
    "十五": 15, "二十": 20, "二十五": 25, "三十": 30, "四十": 40, "四十五": 45, "五十": 50,
}

_PROMISE_NEGATION_RE = re.compile(r"(?:不用|不要|别|取消|撤回)(?:再)?(?:提醒|叫我|喊我|记)")
_PROMISE_TRIGGER_RE = re.compile(
    r"(?:提醒我|叫我|喊我|帮我记一下|帮我记个|帮我记|记一下|记个待办|记个备忘|待办[：:\s]|别忘了(?:提醒我|叫我)?|记得(?:提醒我|叫我)|到点(?:提醒我|叫我))"
)
_MEMO_STYLE_RE = re.compile(r"(?:帮我记一下|帮我记个|帮我记|记一下|记个待办|记个备忘|待办|备忘|提醒事项)")


def _parse_cn_or_int(raw: str) -> Optional[int]:
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_NUM_MAP:
        return _CN_NUM_MAP[s]
    if s.startswith("十") and len(s) == 2 and s[1] in _CN_NUM_MAP:
        return 10 + _CN_NUM_MAP[s[1]]
    if s.endswith("十") and len(s) == 2 and s[0] in _CN_NUM_MAP:
        return _CN_NUM_MAP[s[0]] * 10
    if len(s) == 3 and s[1] == "十" and s[0] in _CN_NUM_MAP and s[2] in _CN_NUM_MAP:
        return _CN_NUM_MAP[s[0]] * 10 + _CN_NUM_MAP[s[2]]
    return None


def parse_owner_promise_text(
    text: str,
    *,
    now_ts: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """从授权主人的自然语言发言中轻量提取时间约定或待办事项（零大模型开销）：
    支持示例：
    - "今晚 10 点提醒我看编译原理课件"
    - "10 分钟后叫我喝水"
    - "帮我记一下明天早八交教案"
    - "半小时后提醒我收衣服"
    返回结构: {"trigger_ts": float, "task_type": str, "content": str, "time_expr": str}
    """
    s = (text or "").strip()
    if not s or s.startswith("/") or len(s) > 240:
        return None
    if _PROMISE_NEGATION_RE.search(s):
        return None
    if not _PROMISE_TRIGGER_RE.search(s):
        return None

    base_ts = float(time.time() if now_ts is None else now_ts)
    now_dt = datetime.datetime.fromtimestamp(base_ts, tz=_TZ)
    trigger_ts: Optional[float] = None
    time_expr = ""

    # 1. 相对时间匹配（如 "10分钟后"、"半小时后"、"2小时后"、"30秒后"、"一个半小时后"）
    rel_half_hour = re.search(r"(?:一个半小时|1个半小时)\s*(?:后|之后|以后)", s)
    rel_30m = re.search(r"半(?:个)?小时\s*(?:后|之后|以后)", s)
    rel_quarter = re.search(r"一刻钟\s*(?:后|之后|以后)", s)
    rel_num_unit = re.search(
        r"(\d+(?:\.\d+)?|[一二两三四五六七八九十]+)\s*(?:个)?\s*(秒钟?|分钟?|分|小时|钟头)\s*(?:后|之后|以后)",
        s,
    )

    if rel_half_hour:
        trigger_ts = base_ts + 5400.0
        time_expr = rel_half_hour.group(0)
    elif rel_30m:
        trigger_ts = base_ts + 1800.0
        time_expr = rel_30m.group(0)
    elif rel_quarter:
        trigger_ts = base_ts + 900.0
        time_expr = rel_quarter.group(0)
    elif rel_num_unit:
        raw_num, unit = rel_num_unit.group(1), rel_num_unit.group(2)
        val: Optional[float] = None
        try:
            val = float(raw_num)
        except ValueError:
            parsed_i = _parse_cn_or_int(raw_num)
            if parsed_i is not None:
                val = float(parsed_i)
        if val is not None and val > 0:
            if unit.startswith("秒"):
                trigger_ts = base_ts + val
            elif unit.startswith("分"):
                trigger_ts = base_ts + val * 60.0
            else:
                trigger_ts = base_ts + val * 3600.0
            time_expr = rel_num_unit.group(0)

    # 2. 绝对/日历时间匹配（如 "今晚 10 点"、"明天早八"、"下午 3 点半"、"明天 09:30"）
    if trigger_ts is None:
        day_offset = 0
        has_explicit_day = False
        if any(w in s for w in ("后天",)):
            day_offset = 2
            has_explicit_day = True
        elif any(w in s for w in ("明天", "明早", "明晚", "明日")):
            day_offset = 1
            has_explicit_day = True
        elif any(w in s for w in ("今天", "今晚", "今早", "今日")):
            day_offset = 0
            has_explicit_day = True

        is_pm_hint = any(w in s for w in ("今晚", "明晚", "晚上", "下午", "傍晚", "夜里", "今夜"))
        is_am_hint = any(w in s for w in ("今早", "明早", "早上", "清晨", "上午", "凌晨"))

        # 2a. 校园习惯简写："早八" / "早九" / "早七" / "早十"
        zao_match = re.search(r"(?:今天|明天|后天|明早|今早)?\s*早([六七八九十]|[6-9]|10)(?:点)?", s)
        clock_match = re.search(
            r"(?:(?:今天|今晚|今早|明天|明早|明晚|后天)\s*)?"
            r"(?:(?:早上|清晨|上午|中午|下午|傍晚|晚上|夜里|凌晨)\s*)?"
            r"(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:点|时|:|：)\s*(?:(半|一刻)|(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*分?)?",
            s,
        )

        hour: Optional[int] = None
        minute: int = 0
        if zao_match:
            h_parsed = _parse_cn_or_int(zao_match.group(1))
            if h_parsed is not None:
                hour = h_parsed
                minute = 0
                time_expr = zao_match.group(0).strip()
        elif clock_match:
            h_parsed = _parse_cn_or_int(clock_match.group(1))
            if h_parsed is not None and 0 <= h_parsed <= 24:
                hour = h_parsed
                special_min = clock_match.group(2)
                raw_min = clock_match.group(3)
                if special_min == "半":
                    minute = 30
                elif special_min == "一刻":
                    minute = 15
                elif raw_min:
                    m_parsed = _parse_cn_or_int(raw_min)
                    if m_parsed is not None and 0 <= m_parsed < 60:
                        minute = m_parsed
                if is_pm_hint and 1 <= hour <= 11:
                    hour += 12
                elif "中午" in s and 1 <= hour <= 3:
                    hour += 12
                elif is_am_hint and hour == 12:
                    hour = 0
                time_expr = clock_match.group(0).strip()

        if hour is not None and 0 <= hour <= 23:
            target_date = (now_dt + datetime.timedelta(days=day_offset)).date()
            target_dt = datetime.datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                hour,
                minute,
                0,
                tzinfo=_TZ,
            )
            if target_dt.timestamp() <= base_ts:
                if not has_explicit_day and not is_pm_hint and not is_am_hint and hour < 12:
                    # 比如白天 14:00 说 "10点提醒我..."，优先对齐到今晚 22:00
                    tonight_dt = target_dt + datetime.timedelta(hours=12)
                    if tonight_dt.timestamp() > base_ts:
                        target_dt = tonight_dt
                    else:
                        target_dt = target_dt + datetime.timedelta(days=1)
                else:
                    target_dt = target_dt + datetime.timedelta(days=1)
            trigger_ts = target_dt.timestamp()

    if trigger_ts is None:
        return None

    # 3. 提取待办事项核心内容 content
    content = s
    if time_expr:
        content = content.replace(time_expr, " ", 1)
    content = re.sub(
        r"^(?:阿米娅|兔兔|凯尔希|达妮娅|博士)?[，,、\s]*(?:请|麻烦|帮我|记得|别忘了)?\s*",
        "",
        content,
    )
    content = re.sub(
        r"(?:今天|今晚|今早|明天|明早|明晚|后天|早上|上午|中午|下午|傍晚|晚上|夜里|凌晨)?\s*"
        r"(?:到点|准时)?\s*(?:提醒我一下|提醒我|叫我一下|叫我|喊我一下|喊我|帮我记一下|帮我记个|帮我记|记一下|记个待办|记个备忘|待办[：:\s]?)\s*(?:去|要)?",
        "",
        content,
    )
    content = re.sub(r"[。！!？?～~，,；;\s]+$", "", content).strip()
    if not content:
        content = "处理待办事项"

    is_memo = bool(_MEMO_STYLE_RE.search(s))
    task_type = "mac_reminder" if is_memo else "hybrid_remind"
    return {
        "trigger_ts": round(float(trigger_ts), 3),
        "task_type": task_type,
        "content": content[:120],
        "time_expr": time_expr,
    }


@dataclasses.dataclass
class PendingPromiseItem:
    """阿米娅答应主人的时间约定或挂起待办任务项（持久化于 pending_promises.json）。"""
    id: str
    uid: str
    group_id: str
    trigger_ts: float
    task_type: str  # qq_remind | mac_notify | mac_reminder | hybrid_remind
    content: str
    status: str = "pending"  # pending | delivered | uncertain | failed | cancelled
    source_text: str = ""
    created_at: float = dataclasses.field(default_factory=time.time)
    fulfilled_at: Optional[float] = None
    delivery_detail: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class AutonomousEventItem:
    event_id: str
    message_id: str
    group_id: str
    user_id: str
    nickname: str
    text: str
    timestamp: float


@dataclasses.dataclass
class AmiyaMoodState:
    mood: str = "平静温和"
    energy: float = 0.75
    reason: str = "日常在罗德岛舰桥陪伴大家"
    updated_at: float = dataclasses.field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class AutonomousAgentCoordinator:
    """后台事件批次评估与自主行动协调器（含受生命周期管理的异步后台消费者与约定兑现心跳）。"""

    DEFAULT_POLL_INTERVAL_SEC = 15.0
    DEFAULT_BATCH_SETTLE_SEC = 8.0
    DEFAULT_BATCH_TRIGGER_COUNT = 8
    DEFAULT_PROMISE_HEARTBEAT_SEC = 20.0
    MAX_EVENT_AGE_SEC = 900.0

    def __init__(
        self,
        state_path: Path = AUTONOMOUS_STATE_FILE,
        *,
        promises_path: Optional[Path] = None,
        rules_path: Optional[Path] = None,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        batch_settle_sec: float = DEFAULT_BATCH_SETTLE_SEC,
        batch_trigger_count: int = DEFAULT_BATCH_TRIGGER_COUNT,
        promise_heartbeat_sec: float = DEFAULT_PROMISE_HEARTBEAT_SEC,
    ) -> None:
        self.state_path = state_path
        if promises_path is not None:
            self.promises_path = promises_path
        elif state_path != AUTONOMOUS_STATE_FILE:
            self.promises_path = state_path.parent / "pending_promises.json"
        else:
            self.promises_path = PENDING_PROMISES_FILE

        if rules_path is not None:
            self.rules_path = rules_path
        elif state_path != AUTONOMOUS_STATE_FILE:
            self.rules_path = state_path.parent / "learned_rules.json"
        else:
            self.rules_path = LEARNED_RULES_FILE

        self.poll_interval_sec = max(0.01, float(poll_interval_sec))
        self.batch_settle_sec = max(0.0, float(batch_settle_sec))
        self.batch_trigger_count = max(1, int(batch_trigger_count))
        self.promise_heartbeat_sec = max(0.01, float(promise_heartbeat_sec))

        self._event_queues: dict[str, Deque[AutonomousEventItem]] = collections.defaultdict(
            lambda: collections.deque(maxlen=30)
        )
        self._group_last_event_ts: dict[str, float] = {}
        self._evaluated_message_ids: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._evaluated_max = 2048

        self._eval_lock = asyncio.Lock()
        self._promise_lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._promise_task: Optional[asyncio.Task[None]] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._running: bool = False

        # 消费者运行期可观测指标与可注入钩子（便于定向测试注入假时钟与 fake publisher）
        self._consumer_tick_count: int = 0
        self._consumer_batch_count: int = 0
        self._consumer_error_count: int = 0
        self._promise_tick_count: int = 0
        self._last_consumer_run_at: float = 0.0
        self._last_promise_scan_at: float = 0.0
        self._last_consumer_error: str = ""
        self._decision_fn: Optional[Callable[[dict[str, Any]], Any]] = None
        self._send_group_fn: Optional[Callable[[str, str], Any]] = None
        self._send_private_fn: Optional[Callable[[str, str], Any]] = None
        self._publish_qzone_fn: Optional[Callable[[str], Any]] = None
        self._mac_actuator_fn: Optional[Callable[[PendingPromiseItem], Any]] = None
        self._time_fn: Callable[[], float] = time.time

        self._mood = AmiyaMoodState()
        self._recent_actions: Deque[dict[str, Any]] = collections.deque(maxlen=40)
        self._qzone_timestamps: list[float] = []
        self._uncertain_qzone_timestamps: list[float] = []
        self._recent_sent_hashes: Deque[str] = collections.deque(maxlen=50)
        self._pending_promises: dict[str, PendingPromiseItem] = collections.OrderedDict()
        self._learned_rules: list[dict[str, Any]] = []
        self._interaction_feedback_log: Deque[dict[str, Any]] = collections.deque(maxlen=80)
        self._owner_work_checklists: Deque[dict[str, Any]] = collections.deque(maxlen=30)
        self._silence_checker: Optional[Callable[[Optional[str], str], bool]] = None
        self._load_persistent_state()
        self._load_promises()
        self._load_learned_rules()

    def set_silence_checker(self, fn: Callable[[Optional[str], str], bool]) -> None:
        self._silence_checker = fn

    def configure_consumer(
        self,
        *,
        poll_interval_sec: Optional[float] = None,
        batch_settle_sec: Optional[float] = None,
        batch_trigger_count: Optional[int] = None,
        promise_heartbeat_sec: Optional[float] = None,
        decision_fn: Optional[Callable[[dict[str, Any]], Any]] = None,
        send_group_fn: Optional[Callable[[str, str], Any]] = None,
        send_private_fn: Optional[Callable[[str, str], Any]] = None,
        publish_qzone_fn: Optional[Callable[[str], Any]] = None,
        mac_actuator_fn: Optional[Callable[[PendingPromiseItem], Any]] = None,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        """配置后台消费者轮询与批次合并参数（测试可注入假时钟、fake 决策器与 fake 投递器）。"""
        if poll_interval_sec is not None:
            self.poll_interval_sec = max(0.005, float(poll_interval_sec))
        if batch_settle_sec is not None:
            self.batch_settle_sec = max(0.0, float(batch_settle_sec))
        if batch_trigger_count is not None:
            self.batch_trigger_count = max(1, int(batch_trigger_count))
        if promise_heartbeat_sec is not None:
            self.promise_heartbeat_sec = max(0.005, float(promise_heartbeat_sec))
        if decision_fn is not None:
            self._decision_fn = decision_fn
        if send_group_fn is not None:
            self._send_group_fn = send_group_fn
        if send_private_fn is not None:
            self._send_private_fn = send_private_fn
        if publish_qzone_fn is not None:
            self._publish_qzone_fn = publish_qzone_fn
        if mac_actuator_fn is not None:
            self._mac_actuator_fn = mac_actuator_fn
        if time_fn is not None:
            self._time_fn = time_fn

    def _now(self, override_ts: Optional[float] = None) -> float:
        if override_ts is not None:
            return float(override_ts)
        return float(self._time_fn())

    def _load_persistent_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                m = data.get("mood")
                if isinstance(m, dict):
                    self._mood = AmiyaMoodState(
                        mood=str(m.get("mood") or "平静温和"),
                        energy=float(m.get("energy", 0.75)),
                        reason=str(m.get("reason") or "日常陪伴大家"),
                        updated_at=float(m.get("updated_at", time.time())),
                    )
                qz_ts = data.get("qzone_timestamps")
                if isinstance(qz_ts, list):
                    self._qzone_timestamps = [float(x) for x in qz_ts if isinstance(x, (int, float))]
                unc_ts = data.get("uncertain_qzone_timestamps")
                if isinstance(unc_ts, list):
                    self._uncertain_qzone_timestamps = [float(x) for x in unc_ts if isinstance(x, (int, float))]
                acts = data.get("recent_actions")
                if isinstance(acts, list):
                    for a in acts[-30:]:
                        if isinstance(a, dict):
                            self._recent_actions.append(a)
                hashes = data.get("recent_sent_hashes")
                if isinstance(hashes, list):
                    for h in hashes[-40:]:
                        if isinstance(h, str):
                            self._recent_sent_hashes.append(h)
        except Exception as exc:
            logger.warning(f"读取自主状态文件失败: {exc}")

    def _save_persistent_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "mood": self._mood.to_dict(),
                "qzone_timestamps": self._qzone_timestamps[-20:],
                "uncertain_qzone_timestamps": self._uncertain_qzone_timestamps[-20:],
                "recent_actions": list(self._recent_actions)[-30:],
                "recent_sent_hashes": list(self._recent_sent_hashes)[-40:],
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception as exc:
            logger.warning(f"保存自主状态文件失败: {exc}")

    def _load_promises(self) -> None:
        if not self.promises_path.is_file():
            return
        try:
            raw = json.loads(self.promises_path.read_text(encoding="utf-8"))
            items = raw.get("promises") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            if isinstance(items, list):
                for item in items[-200:]:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    pid = str(item["id"])
                    self._pending_promises[pid] = PendingPromiseItem(
                        id=pid,
                        uid=str(item.get("uid") or "1785381042"),
                        group_id=str(item.get("group_id") or ""),
                        trigger_ts=float(item.get("trigger_ts", time.time())),
                        task_type=str(item.get("task_type") or "hybrid_remind"),
                        content=str(item.get("content") or ""),
                        status=str(item.get("status") or "pending"),
                        source_text=str(item.get("source_text") or ""),
                        created_at=float(item.get("created_at", time.time())),
                        fulfilled_at=float(item["fulfilled_at"]) if item.get("fulfilled_at") is not None else None,
                        delivery_detail=dict(item.get("delivery_detail") or {}),
                    )
        except Exception as exc:
            logger.warning(f"读取约定账本 pending_promises.json 失败: {exc}")

    def _save_promises(self) -> None:
        try:
            self.promises_path.parent.mkdir(parents=True, exist_ok=True)
            all_items = [p.to_dict() for p in list(self._pending_promises.values())[-200:]]
            payload = {
                "updated_at": self._now(),
                "promises": all_items,
            }
            tmp = self.promises_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.promises_path)
        except Exception as exc:
            logger.warning(f"保存约定账本 pending_promises.json 失败: {exc}")

    def is_authorized_owner(self, uid: str) -> bool:
        u = str(uid or "").strip()
        if not u:
            return False
        if u == "1785381042":
            return True
        su = str(getattr(state, "superuser_qq", "") or "").strip()
        if su and u == su:
            return True
        try:
            from .device_actuator import mac_actuator
            return mac_actuator.is_authorized_owner(u)
        except Exception:
            return False

    def register_promise(
        self,
        *,
        uid: str,
        content: str,
        trigger_ts: float,
        group_id: Optional[str] = None,
        task_type: str = "hybrid_remind",
        source_text: str = "",
        now_ts: Optional[float] = None,
    ) -> PendingPromiseItem:
        """登记一条结构化约定任务并持久化到 pending_promises.json。"""
        now = self._now(now_ts)
        u = str(uid or "").strip()
        gid = str(group_id or "").strip()
        clean_content = (content or "").strip()[:120] or "处理待办事项"
        # 60 秒内同一主人、同一场景、相同内容的待办去重更新
        for existing in reversed(list(self._pending_promises.values())):
            if (
                existing.status == "pending"
                and existing.uid == u
                and existing.group_id == gid
                and existing.content == clean_content
                and abs(now - existing.created_at) <= 60.0
            ):
                existing.trigger_ts = float(trigger_ts)
                existing.task_type = str(task_type or existing.task_type)
                self._save_promises()
                return existing

        pid = f"prm_{uuid.uuid4().hex[:10]}"
        item = PendingPromiseItem(
            id=pid,
            uid=u,
            group_id=gid,
            trigger_ts=round(float(trigger_ts), 3),
            task_type=str(task_type or "hybrid_remind"),
            content=clean_content,
            status="pending",
            source_text=(source_text or "")[:200],
            created_at=now,
        )
        self._pending_promises[pid] = item
        while len(self._pending_promises) > 200:
            self._pending_promises.popitem(last=False)
        self._save_promises()
        if self._wake_event is not None:
            self._wake_event.set()
        return item

    def capture_owner_promise(
        self,
        uid: str,
        text: str,
        *,
        group_id: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[PendingPromiseItem]:
        """当授权主人（如 1785381042）在群聊或私聊中提到时间约定或待办事项时，自动提取并落盘到 pending_promises.json。"""
        if not self.is_authorized_owner(uid):
            return None
        now = self._now(now_ts)
        parsed = parse_owner_promise_text(text, now_ts=now)
        if parsed is None:
            return None
        return self.register_promise(
            uid=uid,
            group_id=group_id,
            trigger_ts=parsed["trigger_ts"],
            task_type=parsed["task_type"],
            content=parsed["content"],
            source_text=text,
            now_ts=now,
        )

    def list_promises(
        self,
        *,
        uid: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        items = list(self._pending_promises.values())
        if uid is not None:
            u = str(uid).strip()
            items = [p for p in items if p.uid == u]
        if status is not None:
            st = str(status).strip()
            items = [p for p in items if p.status == st]
        items.sort(key=lambda x: x.trigger_ts)
        return [p.to_dict() for p in items[: max(1, int(limit))]]

    def cancel_promise(self, promise_id: str, *, uid: Optional[str] = None) -> bool:
        pid = str(promise_id or "").strip()
        item = self._pending_promises.get(pid)
        if item is None:
            return False
        if uid is not None and item.uid != str(uid).strip():
            return False
        if item.status != "pending":
            return False
        item.status = "cancelled"
        item.fulfilled_at = self._now()
        self._save_promises()
        return True

    def get_active_promises_prompt_hint(self, uid: str, *, now_ts: Optional[float] = None) -> str:
        if not self.is_authorized_owner(uid):
            return ""
        now = self._now(now_ts)
        pending = [
            p for p in self._pending_promises.values()
            if p.uid == str(uid).strip() and p.status == "pending" and p.trigger_ts > now
        ]
        if not pending:
            return ""
        latest = pending[-1]
        dt_str = datetime.datetime.fromtimestamp(latest.trigger_ts, tz=_TZ).strftime("%H:%M")
        return (
            f"\n【约定账本已自动登记】已为你记下 {dt_str} 的待办约定：「{latest.content}」，"
            "回复时自然答应一声即可，到点后系统会自动提醒。"
        )

    @staticmethod
    def _build_promise_fulfillment_text(item: PendingPromiseItem) -> str:
        c = (item.content or "待办事项").strip()
        if any(k in c for k in ("喝水", "休息", "歇", "睡觉", "吃药", "吃饭")):
            return f"跟你说好的时间到啦，先停下手头的活去{c}吧，身体要紧哦～"
        return f"之前答应你的时间到啦，记得「{c}」哦，我已经帮你在备忘里标好啦～"

    async def _default_send_private(self, user_id: str, text: str) -> bool:
        try:
            from nonebot import get_bots

            bots = get_bots()
            bot = next(iter(bots.values()), None) if bots else None
            if bot is None:
                return False
            await asyncio.wait_for(bot.call_api("send_private_msg", user_id=int(user_id), message=text), timeout=8.0)
            return True
        except Exception as exc:
            logger.warning(f"约定兑现私聊发送未确认 ({user_id}): {exc}")
            return False

    async def _default_mac_fulfill(self, item: PendingPromiseItem) -> dict[str, Any]:
        try:
            from .device_actuator import mac_actuator

            msg = self._build_promise_fulfillment_text(item)
            if item.task_type == "mac_reminder":
                due_iso = datetime.datetime.fromtimestamp(item.trigger_ts, tz=_TZ).isoformat()
                rem_res = await mac_actuator.add_mac_reminder(
                    title=item.content,
                    note=f"阿米娅约定兑现账本 ({item.id})",
                    uid=item.uid,
                    due_at=due_iso,
                )
                noti_res = await mac_actuator.send_mac_notification(
                    title="🐰 阿米娅的约定提醒",
                    message=msg,
                    subtitle=f"待办兑现 · {item.content[:24]}",
                    uid=item.uid,
                    bypass_cooldown=True,
                )
                return {
                    "ok": bool(rem_res.ok or noti_res.ok),
                    "reminder": rem_res.to_dict(),
                    "notify": noti_res.to_dict(),
                }
            noti_res = await mac_actuator.send_mac_notification(
                title="🐰 阿米娅的约定提醒",
                message=msg,
                subtitle=f"约定时间已到 · {item.content[:24]}",
                uid=item.uid,
                bypass_cooldown=True,
            )
            return {"ok": bool(noti_res.ok), "notify": noti_res.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def run_promise_heartbeat_tick(self, now_ts: Optional[float] = None) -> list[dict[str, Any]]:
        """轻量级约定账本心跳检查（每 15~30s 扫描一次到期任务，全程零大模型调用）：
        - 扫描 status == 'pending' 且 trigger_ts <= now 的约定；
        - 自动通过 QQ 消息（群聊或私聊）及/或调用 device_actuator.mac_actuator（Mac 桌面通知 / 提醒事项）兑现承诺；
        - 先更新状态防止并发重复触发，回执未确认记为 uncertain 防止死循环轰炸。
        """
        async with self._promise_lock:
            now = self._now(now_ts)
            self._promise_tick_count += 1
            self._last_promise_scan_at = now
            due_items = [
                p for p in self._pending_promises.values()
                if p.status == "pending" and p.trigger_ts <= now
            ]
            if not due_items:
                return []

            fulfilled_records: list[dict[str, Any]] = []
            for item in due_items:
                # 先标记为 uncertain 占位并落盘，确保即使中途异常也不会无限重发
                item.status = "uncertain"
                item.fulfilled_at = now
                self._save_promises()

                remind_text = self._build_promise_fulfillment_text(item)
                qq_ok = False
                mac_ok = False
                detail: dict[str, Any] = {}

                # 1. 尝试 QQ 消息提醒（当 task_type 包含 qq_remind / hybrid_remind / mac_reminder）
                if item.task_type in {"qq_remind", "hybrid_remind", "mac_reminder"}:
                    try:
                        if item.group_id:
                            eff_send_group = self._send_group_fn
                            group_msg = f"[CQ:at,qq={item.uid}] {remind_text}" if self._send_group_fn is None else remind_text
                            if eff_send_group is not None:
                                qq_ok = bool(await eff_send_group(item.group_id, remind_text))
                            else:
                                qq_ok = await self._default_send_group(item.group_id, group_msg)
                            detail["qq_channel"] = f"group:{item.group_id}"
                        if not qq_ok:
                            eff_send_priv = self._send_private_fn
                            if eff_send_priv is not None:
                                qq_ok = bool(await eff_send_priv(item.uid, remind_text))
                            else:
                                qq_ok = await self._default_send_private(item.uid, remind_text)
                            detail["qq_channel"] = f"private:{item.uid}"
                        detail["qq_delivered"] = qq_ok
                    except Exception as exc:
                        detail["qq_error"] = f"{type(exc).__name__}: {exc}"

                # 2. 尝试 Mac 原生执行器提醒（当 task_type 包含 mac_notify / mac_reminder / hybrid_remind）
                if item.task_type in {"mac_notify", "mac_reminder", "hybrid_remind"} and self.is_authorized_owner(item.uid):
                    try:
                        if self._mac_actuator_fn is not None:
                            mac_res = await self._mac_actuator_fn(item)
                            if isinstance(mac_res, dict):
                                mac_ok = bool(mac_res.get("ok"))
                                detail["mac_result"] = mac_res
                            else:
                                mac_ok = bool(mac_res)
                        else:
                            mac_res = await self._default_mac_fulfill(item)
                            mac_ok = bool(mac_res.get("ok"))
                            detail["mac_result"] = mac_res
                        detail["mac_delivered"] = mac_ok
                    except Exception as exc:
                        detail["mac_error"] = f"{type(exc).__name__}: {exc}"

                item.status = "delivered" if (qq_ok or mac_ok) else "uncertain"
                item.delivery_detail = detail
                self._save_promises()

                action_rec = {
                    "ts": now,
                    "action": "fulfill_promise",
                    "promise_id": item.id,
                    "uid": item.uid,
                    "group_id": item.group_id,
                    "task_type": item.task_type,
                    "content": item.content,
                    "delivered": bool(qq_ok or mac_ok),
                    "status": item.status,
                    "detail": detail,
                }
                self._recent_actions.append(action_rec)
                fulfilled_records.append(action_rec)

            self._save_persistent_state()
            return fulfilled_records

    def _mark_message_evaluated(self, message_id: str, now_ts: float) -> None:
        mid = str(message_id or "").strip()
        if not mid:
            return
        self._evaluated_message_ids[mid] = now_ts
        self._evaluated_message_ids.move_to_end(mid)
        while len(self._evaluated_message_ids) > self._evaluated_max:
            self._evaluated_message_ids.popitem(last=False)

    def _is_event_already_handled(self, ev: AutonomousEventItem) -> bool:
        mid = ev.message_id
        if mid in self._evaluated_message_ids:
            return True
        if group_chat_manager.is_consumed(mid):
            return True
        if group_chat_manager.get_message_state(mid) in {"processing", "delivered", "uncertain", "skipped"}:
            return True
        return False

    def is_quiet_hours(self, now_dt: Optional[datetime.datetime] = None) -> bool:
        """检查当前是否处于夜间安静时段（默认 23:00 ~ 08:00）。"""
        dt = now_dt or datetime.datetime.fromtimestamp(self._now(), tz=_TZ)
        start_h = int(getattr(state, "autonomous_quiet_start_hour", 23))
        end_h = int(getattr(state, "autonomous_quiet_end_hour", 8))
        h = dt.hour
        if start_h > end_h:
            return h >= start_h or h < end_h
        return start_h <= h < end_h

    def clear_pending_events(self) -> None:
        """清空所有待评估的后台群聊事件队列（当全局开关关闭或切换静默时调用）。"""
        self._event_queues.clear()

    def can_publish_autonomous_qzone(self, now_ts: Optional[float] = None) -> tuple[bool, str]:
        """检查自主发空间是否满足开关、安静时段、未确认投递保护、每日上限（默认 2 条）与最小间隔（默认 6 小时）。"""
        if not bool(getattr(state, "global_active", True)):
            return False, "global_inactive"
        if not getattr(state, "autonomous_qzone_enabled", False):
            return False, "autonomous_qzone_disabled"
        now = self._now(now_ts)
        dt = datetime.datetime.fromtimestamp(now, tz=_TZ)
        if self.is_quiet_hours(dt):
            return False, "quiet_hours"

        max_per_day = int(getattr(state, "autonomous_qzone_max_per_day", 2))
        min_interval_hours = float(getattr(state, "autonomous_qzone_min_interval_hours", 6.0))
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

        # 若今日存在投递回执未确认 (uncertain) 的空间动态，禁止盲目自动重发
        uncertain_today = [t for t in self._uncertain_qzone_timestamps if t >= day_start]
        if uncertain_today:
            return False, "previous_delivery_uncertain"

        today_posts = [t for t in self._qzone_timestamps if t >= day_start]
        if len(today_posts) >= max_per_day:
            return False, f"daily_cap_reached({len(today_posts)}/{max_per_day})"
        if self._qzone_timestamps:
            elapsed_h = (now - self._qzone_timestamps[-1]) / 3600.0
            if elapsed_h < min_interval_hours:
                return False, f"interval_too_short({elapsed_h:.1f}h<{min_interval_hours}h)"
        return True, "ok"

    def _load_learned_rules(self) -> None:
        if not self.rules_path.is_file():
            return
        try:
            raw = json.loads(self.rules_path.read_text(encoding="utf-8"))
            rules = raw.get("rules") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            if isinstance(rules, list):
                self._learned_rules = [r for r in rules if isinstance(r, dict) and r.get("rule")][-16:]
            fb = raw.get("feedback_log") if isinstance(raw, dict) else []
            if isinstance(fb, list):
                for item in fb[-60:]:
                    if isinstance(item, dict):
                        self._interaction_feedback_log.append(item)
        except Exception as exc:
            logger.warning(f"读取自主进化守则 learned_rules.json 失败: {exc}")

    def _save_learned_rules(self) -> None:
        try:
            self.rules_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": self._now(),
                "rules": list(self._learned_rules)[-16:],
                "feedback_log": list(self._interaction_feedback_log)[-60:],
            }
            tmp = self.rules_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.rules_path)
        except Exception as exc:
            logger.warning(f"保存自主进化守则 learned_rules.json 失败: {exc}")

    def record_interaction_feedback(
        self,
        *,
        group_id: Optional[str],
        uid: str,
        feedback_type: str,
        detail: str = "",
        blacklisted_styles: Optional[list[str]] = None,
        now_ts: Optional[float] = None,
    ) -> dict[str, Any]:
        """记录群友/主人对阿米娅发言的实时正负反馈或静默指令，并在触发负向/拉黑反馈时即时更新 learned_rules.json。"""
        now = self._now(now_ts)
        gid = str(group_id or "").strip()
        entry = {
            "ts": now,
            "group_id": gid,
            "uid": str(uid or ""),
            "feedback_type": str(feedback_type or "neutral"),
            "detail": (detail or "")[:120],
            "blacklisted_styles": list(blacklisted_styles or []),
        }
        self._interaction_feedback_log.append(entry)
        if feedback_type in {"negative_reaction", "silence_command", "style_blacklisted"}:
            self.distill_learned_rules(group_id=gid or None, now_ts=now)
        else:
            self._save_learned_rules()
        return entry

    def distill_learned_rules(
        self,
        *,
        group_id: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> dict[str, Any]:
        """复盘近期互动反馈日志、表达黑名单与静默事件，提炼简洁的交互守则持久化至 learned_rules.json，并清理过期近况。"""
        now = self._now(now_ts)
        gid = str(group_id or "").strip()
        from .expression_learner import expression_store

        fb_summary = expression_store.get_feedback_summary(group_id=gid or None)
        bl_map: dict[str, list[str]] = fb_summary.get("blacklisted_styles_by_group", {})
        by_id: dict[str, dict[str, Any]] = {
            str(r.get("rule_id")): r for r in self._learned_rules if isinstance(r, dict) and r.get("rule_id")
        }
        new_count = 0

        # 1. 将群友明确排斥并触发拉黑的句式转化为该群硬性禁令守则
        for bgid, styles in bl_map.items():
            if not styles:
                continue
            rid = f"rule_bl_{bgid}"
            joined = "、".join(f"「{s}」" for s in styles[-5:])
            rule_txt = f"在群 {bgid} 严禁使用群友反感的生硬黑话或句式（如 {joined}），保持自然温和口吻"
            if rid not in by_id or by_id[rid].get("rule") != rule_txt:
                new_count += 1
            by_id[rid] = {
                "rule_id": rid,
                "scope": f"group:{bgid}",
                "group_id": bgid,
                "rule": rule_txt,
                "source": "expression_reaction_blacklist",
                "updated_at": now,
            }

        # 2. 复盘静默/闭嘴与负向反馈记录，提炼读空气守则
        recent_fb = list(self._interaction_feedback_log)[-40:]
        silence_groups = {
            str(x.get("group_id")) for x in recent_fb
            if x.get("feedback_type") == "silence_command" and x.get("group_id")
        }
        for sgid in silence_groups:
            rid = f"rule_silence_{sgid}"
            rule_txt = f"群 {sgid} 曾触发打扰静默，群友密集讨论正事或未点名时克制接话、绝不抢话"
            if rid not in by_id:
                new_count += 1
            by_id[rid] = {
                "rule_id": rid,
                "scope": f"group:{sgid}",
                "group_id": sgid,
                "rule": rule_txt,
                "source": "silence_reflection",
                "updated_at": now,
            }

        neg_logs = [x for x in recent_fb if x.get("feedback_type") == "negative_reaction"]
        if neg_logs:
            last_neg = neg_logs[-1]
            ngid = str(last_neg.get("group_id") or "global")
            rid = f"rule_neg_{ngid}"
            rule_txt = "群友对复读或捧读式接话敏感，日常搭腔直接顺着话头说人话，不机械复述对方原句"
            if rid not in by_id:
                new_count += 1
            by_id[rid] = {
                "rule_id": rid,
                "scope": f"group:{ngid}" if ngid != "global" else "global",
                "group_id": "" if ngid == "global" else ngid,
                "rule": rule_txt,
                "source": "negative_feedback_reflection",
                "updated_at": now,
            }

        pos_logs = [x for x in recent_fb if x.get("feedback_type") == "positive_reaction"]
        if pos_logs:
            pgid = str(pos_logs[-1].get("group_id") or "global")
            rid = f"rule_pos_{pgid}"
            rule_txt = "简短有梗、带真诚共情的熟人式接茬最受群友欢迎，保持轻快不端着的语气"
            if rid not in by_id:
                new_count += 1
            by_id[rid] = {
                "rule_id": rid,
                "scope": f"group:{pgid}" if pgid != "global" else "global",
                "group_id": "" if pgid == "global" else pgid,
                "rule": rule_txt,
                "source": "positive_feedback_reinforcement",
                "updated_at": now,
            }

        self._learned_rules = sorted(by_id.values(), key=lambda r: float(r.get("updated_at", 0.0)), reverse=True)[:12]
        self._save_learned_rules()

        prune_res = user_memory_store.prune_stale_recent_matters(now_ts=now)
        return {
            "status": "ok",
            "new_or_updated_rules": new_count,
            "total_rules": len(self._learned_rules),
            "rules": list(self._learned_rules),
            "pruned_memories": prune_res,
        }

    def list_learned_rules(self, group_id: Optional[str] = None) -> list[dict[str, Any]]:
        gid = str(group_id or "").strip()
        if not gid:
            return list(self._learned_rules)
        return [
            r for r in self._learned_rules
            if r.get("scope") in {"global", f"group:{gid}"} or str(r.get("group_id") or "") in {"", gid}
        ]

    def get_learned_rules_prompt_block(
        self,
        *,
        group_id: Optional[str] = None,
        uid: Optional[str] = None,
    ) -> str:
        """生成注入系统提示词的自主进化交互守则块（0ms 本地读取）。"""
        rules = self.list_learned_rules(group_id=group_id)[:4]
        if not rules:
            return ""
        lines = [f"- {r['rule']}" for r in rules if r.get("rule")]
        if not lines:
            return ""
        return "【自主进化交互守则（来自近期群友真实反馈复盘，务必遵守）】：\n" + "\n".join(lines)

    def decompose_and_track_owner_work(
        self,
        uid: str,
        text: str,
        *,
        group_id: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> Optional[dict[str, Any]]:
        """拆解授权主人（1785381042）的多步复合指令为子目标清单（Sub-goal Checklist），防止漏掉后半句要求。"""
        if not self.is_authorized_owner(uid):
            return None
        s = (text or "").strip()
        if not s or len(s) < 4 or s.startswith("/"):
            return None
        now = self._now(now_ts)
        subgoals: list[dict[str, Any]] = []

        if re.search(r"(?:docx|pdf|word|文档|报告|教案|导出|生成文件)", s, re.IGNORECASE):
            subgoals.append({"step": "doc_export", "desc": "撰写并导出请求格式的文档文件", "status": "pending"})
        if re.search(r"(?:画一张|生成一张|来一张|画个|生图)", s, re.IGNORECASE):
            subgoals.append({"step": "draw_image", "desc": "按要求生成插画或立绘图像", "status": "pending"})
        if re.search(r"(?:语音|用声音|说给我听|发条语音|念一下)", s, re.IGNORECASE):
            subgoals.append({"step": "voice_reply", "desc": "使用语音合成回应", "status": "pending"})
        if _PROMISE_TRIGGER_RE.search(s):
            subgoals.append({"step": "promise_remind", "desc": "登记时间约定与到期提醒账本", "status": "completed"})
        if re.search(r"(?:通知|放歌|播放|暂停|切歌|提醒事项|日历|快捷指令)", s, re.IGNORECASE):
            subgoals.append({"step": "mac_action", "desc": "准备或执行 Mac 原生设备动作", "status": "pending"})
        if re.search(r"(?:自检|运行状态|进化状态|待办账本|看看日志|检查状态)", s, re.IGNORECASE):
            subgoals.append({"step": "status_inspect", "desc": "汇总当前自主进化、约定账本与服务状态", "status": "completed"})

        if len(subgoals) < 2 and not any(g["step"] == "status_inspect" for g in subgoals):
            return None

        checklist = {
            "checklist_id": f"chk_{uuid.uuid4().hex[:8]}",
            "uid": str(uid),
            "group_id": str(group_id or ""),
            "source_text": s[:160],
            "subgoals": subgoals,
            "created_at": now,
        }
        self._owner_work_checklists.append(checklist)
        return checklist

    def get_owner_work_prompt_hint(
        self,
        uid: str,
        text: str,
        *,
        group_id: Optional[str] = None,
        now_ts: Optional[float] = None,
    ) -> str:
        """当主人发送多步复合指令或状态自检指令时，生成防漏项清单与实时运行状态参考块。"""
        chk = self.decompose_and_track_owner_work(uid, text, group_id=group_id, now_ts=now_ts)
        if not chk:
            return ""
        lines = [f"- [{g['step']}] {g['desc']}" for g in chk["subgoals"]]
        hint = (
            "\n【主人复合指令防漏清单（必须完整回应以下所有子目标，严禁只完成前半句而漏掉后半句）】：\n"
            + "\n".join(lines)
        )
        if any(g["step"] == "status_inspect" for g in chk["subgoals"]):
            pending_cnt = sum(1 for p in self._pending_promises.values() if p.status == "pending")
            hint += (
                f"\n【实时运行自检数据】当前心情：{self._mood.mood}；挂起约定数：{pending_cnt} 条；"
                f"已沉淀进化守则：{len(self._learned_rules)} 条；消费者批次：{self._consumer_batch_count} 次。"
            )
        return hint

    def enqueue_group_event(
        self,
        *,
        message_id: str,
        group_id: str,
        user_id: str,
        nickname: str,
        text: str,
        media_summary: str = "",
        timestamp: Optional[float] = None,
    ) -> None:
        """将群聊事件（含纯图片/表情包连发的多模态摘要）非阻塞推入后台队列。"""
        gid = str(group_id or "").strip()
        clean = (text or "").strip()
        media_desc = (media_summary or "").strip()
        effective_text = f"{clean} {media_desc}".strip() if (clean and media_desc) else (clean or media_desc)
        mid = str(message_id or "").strip()
        if not gid or not effective_text or effective_text.startswith("/"):
            return
        if mid and mid in self._evaluated_message_ids:
            return
        q = self._event_queues[gid]
        if mid and any(existing.message_id == mid for existing in q):
            return
        ev_ts = self._now(timestamp)
        group_chat_manager.density.record_incoming_activity(gid, now=ev_ts)
        item = AutonomousEventItem(
            event_id=hashlib.md5(f"{gid}:{mid}:{time.time_ns()}".encode()).hexdigest()[:12],
            message_id=mid or f"{gid}_{uuid.uuid4().hex[:8]}",
            group_id=gid,
            user_id=str(user_id),
            nickname=str(nickname or user_id),
            text=effective_text[:200],
            timestamp=ev_ts,
        )
        q.append(item)
        self._group_last_event_ts[gid] = ev_ts
        if self._wake_event is not None:
            self._wake_event.set()

    def start_consumer(
        self,
        *,
        poll_interval_sec: Optional[float] = None,
        batch_settle_sec: Optional[float] = None,
        promise_heartbeat_sec: Optional[float] = None,
    ) -> asyncio.Task[None]:
        """在当前运行事件循环中启动后台事件消费者与约定兑现心跳（幂等，不在模块导入时调用）。"""
        self.configure_consumer(
            poll_interval_sec=poll_interval_sec,
            batch_settle_sec=batch_settle_sec,
            promise_heartbeat_sec=promise_heartbeat_sec,
        )
        self._running = True
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        if self._promise_task is None or self._promise_task.done():
            self._promise_task = asyncio.create_task(
                self._promise_heartbeat_loop(),
                name="amiya_promise_heartbeat",
            )
        if self._worker_task is not None and not self._worker_task.done():
            return self._worker_task
        self._worker_task = asyncio.create_task(
            self._consumer_loop(),
            name="amiya_autonomous_consumer",
        )
        logger.info(
            f"[AutonomousAgent] 后台消费者与约定心跳已启动 (poll={self.poll_interval_sec}s, settle={self.batch_settle_sec}s, promise_hb={self.promise_heartbeat_sec}s)"
        )
        return self._worker_task

    async def startup(self) -> None:
        """NoneBot `on_startup` 生命周期入口。"""
        self.start_consumer()

    async def shutdown(self) -> None:
        """NoneBot `on_shutdown` 生命周期入口：安全取消后台消费者与约定心跳并等待退出。"""
        self._running = False
        if self._wake_event is not None:
            self._wake_event.set()
        for task in (self._worker_task, self._promise_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._worker_task = None
        self._promise_task = None
        logger.info("[AutonomousAgent] 后台事件消费者与约定心跳已停止")

    async def _promise_heartbeat_loop(self) -> None:
        """每 15~30s 扫描一次到期约定任务的轻量级心跳循环（零大模型调用）。"""
        while self._running:
            try:
                await asyncio.sleep(self.promise_heartbeat_sec)
                if not self._running:
                    break
                await self.run_promise_heartbeat_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[AutonomousAgent] 约定心跳扫描异常已恢复: {type(exc).__name__}: {exc}")
                await asyncio.sleep(min(2.0, self.promise_heartbeat_sec))

    async def _consumer_loop(self) -> None:
        """受生命周期管理的后台主循环：等待事件唤醒或定时轮询，按群合并批次后调用评估。"""
        while self._running:
            try:
                if self._wake_event is not None:
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval_sec)
                    except asyncio.TimeoutError:
                        pass
                    if self._wake_event is not None:
                        self._wake_event.clear()
                else:
                    await asyncio.sleep(self.poll_interval_sec)

                if not self._running:
                    break

                await self.run_consumer_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._consumer_error_count += 1
                self._last_consumer_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"[AutonomousAgent] 后台消费者单轮异常已恢复: {self._last_consumer_error}")
                await asyncio.sleep(min(1.0, self.poll_interval_sec))

    async def run_consumer_tick(self, now_ts: Optional[float] = None) -> list[dict[str, Any]]:
        """执行单轮后台队列扫描与按群批次评估：
        - 顺带触发到期约定扫描（若距上次扫描已满 promise_heartbeat_sec 或存在已到期挂起约定）；
        - 自动剔除已被普通回复链路消费或过期的消息，避免队列无限堆积；
        - 当群事件仍在窗口内涌入且未达批次阈值时等待合并，不为每条消息单独请求模型；
        - 当普通群聊合并缓冲窗口正在等待时主动让行，绝不抢占普通回复。
        """
        now = self._now(now_ts)
        self._consumer_tick_count += 1
        self._last_consumer_run_at = now
        results: list[dict[str, Any]] = []

        if not bool(getattr(state, "global_active", True)):
            self._event_queues.clear()
            return results

        if (
            (now - self._last_promise_scan_at) >= self.promise_heartbeat_sec
            or any(p.status == "pending" and p.trigger_ts <= now for p in self._pending_promises.values())
        ):
            due_res = await self.run_promise_heartbeat_tick(now_ts=now)
            results.extend(due_res)

        for gid in list(self._event_queues.keys()):
            q = self._event_queues.get(gid)
            if not q:
                self._event_queues.pop(gid, None)
                continue

            # 1. 先剔除已被普通回复消费或超时的陈旧事件，防止队列无意义滞留
            kept: list[AutonomousEventItem] = []
            for ev in list(q):
                if self._is_event_already_handled(ev):
                    self._mark_message_evaluated(ev.message_id, now)
                    continue
                if (now - ev.timestamp) > self.MAX_EVENT_AGE_SEC:
                    self._mark_message_evaluated(ev.message_id, now)
                    continue
                kept.append(ev)
            q.clear()
            q.extend(kept)
            if not q:
                self._event_queues.pop(gid, None)
                continue

            # 2. 若普通群聊合并窗口仍有该群待冲刷消息，优先让普通回复链路完成
            if group_chat_manager._pending_buffers.get(gid):
                continue

            # 3. 若两个自主开关均关闭，不调用模型（保留有限长度最新事件，不无限堆积）
            auto_group_on = bool(getattr(state, "autonomous_group_chat_enabled", False))
            auto_qzone_on = bool(getattr(state, "autonomous_qzone_enabled", False))
            if not auto_group_on and not auto_qzone_on:
                continue

            # 4. 批次合并沉淀判定：满 N 条或距离该群最后一条事件已静置 >= batch_settle_sec 才合并触发一次模型决策
            last_ts = self._group_last_event_ts.get(gid, q[-1].timestamp)
            if len(q) < self.batch_trigger_count and (now - last_ts) < self.batch_settle_sec:
                continue

            res = await self.evaluate_group_batch(
                gid,
                decision_fn=self._decision_fn,
                send_group_fn=self._send_group_fn,
                publish_qzone_fn=self._publish_qzone_fn,
                now_ts=now,
            )
            self._consumer_batch_count += 1
            results.append(res)

        return results

    @staticmethod
    def sanitize_public_qzone_text(
        text: str,
        *,
        forbidden_nicknames: Optional[Iterable[str]] = None,
        raw_event_texts: Optional[Iterable[str]] = None,
    ) -> str:
        """确保公开空间动态严格遵守阿米娅人设与隐私边界：
        - 绝不包含任何 QQ 号、群号、@提及、私聊字眼或联系方式；
        - 绝不公开群友昵称或直接复制未经同意的群友原话；
        - 去除多余括号动作提示与换行。
        """
        s = (text or "").strip()
        if not s:
            return ""
        s = _PRIVACY_LEAK_RE.sub("", s)

        if forbidden_nicknames:
            for raw_nick in forbidden_nicknames:
                nick = str(raw_nick or "").strip()
                if not nick or len(nick) < 2 or nick in _PROTECTED_ROLE_NAMES:
                    continue
                if nick in s:
                    s = s.replace(nick, "大家")

        if raw_event_texts:
            for raw_ev in raw_event_texts:
                ev_txt = str(raw_ev or "").strip()
                if len(ev_txt) >= 8 and ev_txt in s:
                    s = s.replace(ev_txt, "")

        s = re.sub(r"[（(【\[][^（）()【】\[\]]*[）)】\]]", "", s)
        s = re.sub(r"\s+", " ", s).strip(" \t\r\n\"'“”‘’，,；;")
        return s

    @staticmethod
    def _is_qzone_receipt_confirmed(qz_res: Any) -> bool:
        """判断空间发布返回结果是否明确确认送达。"""
        if not isinstance(qz_res, dict):
            return False
        if qz_res.get("ok") is True or qz_res.get("published_online") is True:
            return True
        if qz_res.get("status") in {"published", "delivered", "ok"} and qz_res.get("code", 0) == 0:
            return True
        return False

    async def evaluate_group_batch(
        self,
        group_id: str,
        *,
        decision_fn: Optional[Callable[[dict[str, Any]], Any]] = None,
        send_group_fn: Optional[Callable[[str, str], Any]] = None,
        publish_qzone_fn: Optional[Callable[[str], Any]] = None,
        now_ts: Optional[float] = None,
    ) -> dict[str, Any]:
        """按批次评估某个群的近期未消费事件，并由 AI 在以下行动中做出选择：
        1. no_action（不行动，默认优先项）
        2. publish_qzone（生成并发布阿米娅 QQ 空间）
        3. join_group_chat（当开启群自主发言时参与群聊）
        4. initiate_group_topic（当开启群自主发言时发起话题）
        """
        gid = str(group_id or "").strip()
        if not gid:
            return {"action": "no_action", "reason": "missing_group_id"}

        if self._eval_lock.locked():
            return {"action": "no_action", "reason": "eval_busy"}

        async with self._eval_lock:
            now = self._now(now_ts)
            dt = datetime.datetime.fromtimestamp(now, tz=_TZ)

            if self.is_quiet_hours(dt):
                return {"action": "no_action", "reason": "quiet_hours"}

            # 检查是否处于闭嘴/静默状态
            if self._silence_checker and self._silence_checker(gid, ""):
                return {"action": "no_action", "reason": "silenced"}

            q = self._event_queues.get(gid)
            if not q:
                return {"action": "no_action", "reason": "empty_queue"}

            # 仅提取尚未被普通回复链路消费（也未处于 processing/delivered/uncertain 且未被本协调器评估过）的事件
            unconsumed_events = [
                ev for ev in list(q)
                if not self._is_event_already_handled(ev)
            ]
            q.clear()

            if not unconsumed_events:
                return {"action": "no_action", "reason": "all_events_already_consumed"}

            if not bool(getattr(state, "global_active", True)):
                return {"action": "no_action", "reason": "global_inactive"}

            auto_group_on = bool(getattr(state, "autonomous_group_chat_enabled", False))
            auto_qzone_on = bool(getattr(state, "autonomous_qzone_enabled", False))
            if not auto_group_on and not auto_qzone_on:
                return {"action": "no_action", "reason": "autonomous_switches_disabled"}

            # 旁观抑制检查：若检测到群内其他两名群友正在高频互相 [reply] 对线或密集双人争论，主动抑制群内插话
            dq_ex = group_chat_manager.attention._group_exchanges.get(gid)
            bystander_now: Optional[float] = None
            if dq_ex and unconsumed_events:
                ev_last_ts = unconsumed_events[-1].timestamp
                if abs(ev_last_ts - dq_ex[-1].timestamp) <= group_chat_manager.attention.cross_talk_window_sec * 2:
                    bystander_now = max(ev_last_ts, dq_ex[-1].timestamp)
            bystander_info = group_chat_manager.attention.get_bystander_suppression(gid, now=bystander_now)
            bystander_suppressed = bool(bystander_info.get("suppressed"))
            effective_auto_group_on = bool(auto_group_on and not bystander_suppressed)

            qzone_ok, qzone_reason = self.can_publish_autonomous_qzone(now_ts=now)

            if bystander_suppressed and not (auto_qzone_on and qzone_ok):
                for ev in unconsumed_events:
                    self._mark_message_evaluated(ev.message_id, now)
                return {
                    "action": "no_action",
                    "reason": f"bystander_suppressed:{bystander_info.get('reason', 'cross_talk')}",
                }

            # 若开启了自主群聊（且未被旁观抑制），在调用模型前原子尝试预占普通群聊配额
            auto_task_id = f"auto_{gid}_{uuid.uuid4().hex[:8]}"
            reservation: Optional[QuotaReservation] = None
            quota_reason = "bystander_suppressed" if bystander_suppressed else "not_requested"
            if effective_auto_group_on:
                can_res, quota_reason, reservation = await group_chat_manager.density.try_reserve_normal(
                    gid,
                    auto_task_id,
                )
                if not can_res and not (auto_qzone_on and qzone_ok):
                    for ev in unconsumed_events:
                        self._mark_message_evaluated(ev.message_id, now)
                    return {"action": "no_action", "reason": f"density_blocked:{quota_reason}"}
            elif not (auto_qzone_on and qzone_ok):
                for ev in unconsumed_events:
                    self._mark_message_evaluated(ev.message_id, now)
                return {"action": "no_action", "reason": f"qzone_not_allowed:{qzone_reason}"}

            target_msg_ids = [ev.message_id for ev in unconsumed_events]
            for mid in target_msg_ids:
                self._mark_message_evaluated(mid, now)

            active_char = (getattr(state, "active_character", "amiya") or "amiya").strip().lower()
            if active_char in ("none", "default", ""):
                active_char = "amiya"
            latest_text = " ".join(ev.text for ev in unconsumed_events[-3:])

            try:
                # 严格按当前群 group_id 读取对应的群作用域画像（visibility=="group" 且 group_id==gid），
                # 绝不读取私聊画像 (private:{uid}) 或其他群画像！
                uids = list(dict.fromkeys(ev.user_id for ev in unconsumed_events[-8:]))
                profile_snippets: list[str] = []
                group_profiles_context: list[dict[str, Any]] = []
                forbidden_nicknames: set[str] = {
                    ev.nickname for ev in unconsumed_events if ev.nickname and not ev.nickname.isdigit()
                }
                for u in uids:
                    if u.isdigit():
                        rec_prof = user_memory_store.get_scoped_profile(
                            u,
                            group_id=gid,
                            character=active_char,
                        )
                        # 硬性校验：仅允许当前群的 group 作用域画像参与决策，严防私聊或跨群泄漏
                        if rec_prof.visibility != "group" or str(rec_prof.group_id) != gid:
                            continue
                        if rec_prof.observed_nickname:
                            forbidden_nicknames.add(rec_prof.observed_nickname)
                        if rec_prof.preferred_title:
                            forbidden_nicknames.add(rec_prof.preferred_title)
                        block = user_memory_store.get_fast_profile_prompt_block(
                            u,
                            group_id=gid,
                            character=active_char,
                            query_text=latest_text,
                            trigger_background_recall=False,
                        )
                        if block:
                            profile_snippets.append(block)
                        active_facts = [
                            str(f.get("fact"))
                            for f in rec_prof.facts
                            if isinstance(f, dict) and f.get("status", "active") == "active" and f.get("fact")
                        ]
                        if rec_prof.interests or rec_prof.recent_matters or active_facts:
                            group_profiles_context.append(
                                {
                                    "uid": rec_prof.uid,
                                    "group_id": gid,
                                    "visibility": "group",
                                    "interests": list(rec_prof.interests[-6:]),
                                    "recent_matters": list(rec_prof.recent_matters[-4:]),
                                    "facts": active_facts[-4:],
                                }
                            )

                density_info = await group_chat_manager.density.get_density_info(gid)
                topic_threads = group_chat_manager.attention.cluster_interleaved_threads(unconsumed_events[-10:])
                learned_rules = self.list_learned_rules(group_id=gid)[:5]
                context_payload = {
                    "group_id": gid,
                    "events": [dataclasses.asdict(e) for e in unconsumed_events[-8:]],
                    "topic_threads": topic_threads,
                    "learned_rules": [r.get("rule") for r in learned_rules if r.get("rule")],
                    "profiles": profile_snippets,
                    "group_scoped_profiles": group_profiles_context,
                    "mood": self._mood.to_dict(),
                    "density": density_info,
                    "bystander_suppression": bystander_info,
                    "can_join_group": bool(effective_auto_group_on and reservation is not None),
                    "can_publish_qzone": bool(auto_qzone_on and qzone_ok),
                    "qzone_quota_status": qzone_reason,
                    "recent_actions": list(self._recent_actions)[-5:],
                }

                eff_decision_fn = decision_fn or self._decision_fn
                if eff_decision_fn is not None:
                    raw_decision = await eff_decision_fn(context_payload)
                else:
                    raw_decision = await self._call_ai_decision(context_payload)

                action = str(raw_decision.get("action") or "no_action").strip()
                if action not in {"no_action", "join_group_chat", "initiate_group_topic", "publish_qzone"}:
                    action = "no_action"

                # 更新阿米娅独立心情状态（不把群友经历当成自己的经历）
                new_mood = str(raw_decision.get("amiya_mood") or "").strip()
                new_reason = str(raw_decision.get("mood_reason") or "").strip()
                if new_mood:
                    self._mood.mood = new_mood[:24]
                    if new_reason:
                        self._mood.reason = new_reason[:60]
                    self._mood.updated_at = now

                content = str(raw_decision.get("content") or "").strip()

                if action in {"join_group_chat", "initiate_group_topic"}:
                    if not effective_auto_group_on or not content:
                        if reservation is not None:
                            await group_chat_manager.density.release_reservation(
                                reservation, gid, reason="group_chat_disabled_or_empty"
                            )
                        group_chat_manager.mark_message_state(
                            target_msg_ids, "skipped", group_id=gid, task_id=auto_task_id, reason="disabled_or_empty"
                        )
                        return {"action": "no_action", "reason": "group_chat_disabled_or_empty"}

                    if reservation is None:
                        group_chat_manager.mark_message_state(
                            target_msg_ids, "skipped", group_id=gid, task_id=auto_task_id, reason=f"density_blocked:{quota_reason}"
                        )
                        return {"action": "no_action", "reason": f"density_blocked:{quota_reason}"}

                    content_hash = hashlib.md5(content.encode("utf-8")).hexdigest()[:12]
                    if content_hash in self._recent_sent_hashes:
                        await group_chat_manager.density.release_reservation(
                            reservation, gid, reason="duplicate_content"
                        )
                        return {"action": "no_action", "reason": "duplicate_content"}

                    group_chat_manager.mark_message_state(
                        target_msg_ids,
                        "processing",
                        group_id=gid,
                        task_id=auto_task_id,
                    )

                    eff_send_group_fn = send_group_fn or self._send_group_fn

                    async def _do_send_group() -> bool:
                        if eff_send_group_fn is not None:
                            return bool(await eff_send_group_fn(gid, content))
                        return await self._default_send_group(gid, content)

                    delivered = await group_chat_manager.density.send_with_pacing(
                        gid,
                        _do_send_group,
                        reservation=reservation,
                        is_priority=False,
                        enforce_normal_interval=True,
                    )

                    if delivered:
                        self._recent_sent_hashes.append(content_hash)
                        group_chat_manager.mark_message_state(
                            target_msg_ids, "delivered", group_id=gid, task_id=auto_task_id
                        )
                    else:
                        group_chat_manager.mark_message_state(
                            target_msg_ids, "failed", group_id=gid, task_id=auto_task_id, reason="autonomous_send_failed"
                        )

                    record = {
                        "ts": now,
                        "action": action,
                        "group_id": gid,
                        "content": content[:80],
                        "event_ids": target_msg_ids,
                        "delivered": delivered,
                        "status": "delivered" if delivered else "failed",
                    }
                    self._recent_actions.append(record)
                    self._save_persistent_state()
                    return record

                # 若选择的不是群聊发言，立即释放群聊普通配额预占
                if reservation is not None:
                    await group_chat_manager.density.release_reservation(
                        reservation, gid, reason=f"chose_{action}"
                    )
                    reservation = None

                if action == "publish_qzone":
                    if not (auto_qzone_on and qzone_ok) or not content:
                        return {"action": "no_action", "reason": f"qzone_not_allowed:{qzone_reason}"}
                    clean_qzone = self.sanitize_public_qzone_text(
                        content,
                        forbidden_nicknames=forbidden_nicknames,
                        raw_event_texts=(ev.text for ev in unconsumed_events),
                    )
                    if not clean_qzone:
                        return {"action": "no_action", "reason": "qzone_sanitized_empty"}
                    content_hash = hashlib.md5(clean_qzone.encode("utf-8")).hexdigest()[:12]
                    if content_hash in self._recent_sent_hashes:
                        return {"action": "no_action", "reason": "duplicate_qzone_content"}

                    group_chat_manager.mark_message_state(
                        target_msg_ids, "processing", group_id=gid, task_id=auto_task_id
                    )
                    # 在发起请求前登记时间戳与内容哈希，确保投递状态不确定时绝不自动重发
                    self._qzone_timestamps.append(now)
                    self._recent_sent_hashes.append(content_hash)

                    eff_publish_qzone_fn = publish_qzone_fn or self._publish_qzone_fn
                    qz_res: Any = None
                    qz_error: str = ""
                    try:
                        if eff_publish_qzone_fn is not None:
                            qz_res = await eff_publish_qzone_fn(clean_qzone)
                        else:
                            from . import local_rag_service
                            qz_res = await local_rag_service.publish_qzone_post(clean_qzone, with_image=False)
                    except Exception as exc:
                        qz_error = f"{type(exc).__name__}: {exc}"
                        qz_res = {"ok": False, "status": "uncertain", "error": qz_error}

                    ok_status = self._is_qzone_receipt_confirmed(qz_res)
                    if not ok_status:
                        self._uncertain_qzone_timestamps.append(now)

                    group_chat_manager.mark_message_state(
                        target_msg_ids,
                        "delivered" if ok_status else "uncertain",
                        group_id=gid,
                        task_id=auto_task_id,
                        reason="" if ok_status else (qz_error or "qzone_receipt_unconfirmed"),
                    )
                    record = {
                        "ts": now,
                        "action": "publish_qzone",
                        "group_id": gid,
                        "content": clean_qzone[:80],
                        "event_ids": target_msg_ids,
                        "delivered": ok_status,
                        "status": "delivered" if ok_status else "uncertain",
                        "delivery_detail": qz_res if isinstance(qz_res, dict) else {"raw": str(qz_res)},
                    }
                    self._recent_actions.append(record)
                    self._save_persistent_state()
                    return record

                group_chat_manager.mark_message_state(
                    target_msg_ids, "skipped", group_id=gid, task_id=auto_task_id, reason="ai_chose_no_action"
                )
                self._save_persistent_state()
                return {"action": "no_action", "reason": str(raw_decision.get("reason") or "ai_chose_no_action")}
            finally:
                if reservation is not None and reservation.status == "reserved":
                    await group_chat_manager.density.release_reservation(
                        reservation, gid, reason="eval_finally_cleanup"
                    )

    async def _default_send_group(self, group_id: str, text: str) -> bool:
        try:
            from nonebot import get_bots

            bots = get_bots()
            bot = next(iter(bots.values()), None) if bots else None
            if bot is None:
                return False
            await asyncio.wait_for(bot.call_api("send_group_msg", group_id=int(group_id), message=text), timeout=8.0)
            return True
        except Exception as exc:
            logger.warning(f"自主群聊发送未确认 ({group_id}): {exc}")
            return False

    async def _call_ai_decision(self, context_payload: dict[str, Any]) -> dict[str, Any]:
        ensure_bot_env()
        ds_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
        if ds_key:
            api_key = ds_key
            base_url = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
            url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
            model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip()
        else:
            return {"action": "no_action", "reason": "no_api_key"}

        sys_prompt = (
            "你是《明日方舟》阿米娅（罗德岛温柔、真诚、体恤同伴的领袖）的自主心智决策模块。\n"
            "请结合【群内近期公开事件】、【该群专属的群作用域画像】、【阿米娅当前心情】与【配额状态】，在以下行动中择一：\n"
            "1. \"no_action\"：绝大多数日常琐碎闲聊、无需专门发声时保持安静（默认优先项，绝不每批都发）。\n"
            "2. \"publish_qzone\"：仅在 can_publish_qzone=true 且群内讨论真正触动你有感而发时，以阿米娅第一人称写一条 30~65 字的 QQ 空间动态：\n"
            "   - 可以自然分享自己的感受、表达对所聊事物的看法；\n"
            "   - 当看到群友（例如 1785381042 等同伴在代码、文档、教案或音乐上的认真投入与分享）展现出专注与热忱时，可以公开、温和、克制地表达赞许与鼓励；\n"
            "   - 严守隐私与人设边界：只能依据可见群聊内容与阿米娅角色设定，绝不虚构现实经历，绝不复述任何私聊内容，绝不公开任何 QQ 号、群友昵称、敏感信息或未经同意的原话（表扬或提及同伴时请用“看到有同伴认真打磨代码与教案”“听大家聊起喜欢的旋律”等不带名姓的自然表达）；\n"
            "   - 若表达难过或心疼等情绪，只能抒发阿米娅自身温柔的共情与祝愿，严禁将他人的隐私或具体遭遇写成公开内容。\n"
            "3. \"join_group_chat\"：仅在 can_join_group=true 且话题适合轻声接话时自然接一句（不超过18字）。\n"
            "4. \"initiate_group_topic\"：仅在 can_join_group=true 且适合延续气氛时轻声发起一个小话题（不超过22字）。\n"
            "只输出 JSON 对象：\n"
            "{\n"
            '  "action": "no_action | publish_qzone | join_group_chat | initiate_group_topic",\n'
            '  "content": "若行动则填写空间正文或群发言，否则为空字符串",\n'
            '  "amiya_mood": "更新后的阿米娅独立心情（如：温和欣慰、平静专注、轻柔感慨）",\n'
            '  "mood_reason": "心情原因简述",\n'
            '  "reason": "决策理由"\n'
            "}"
        )
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=20.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": json.dumps(context_payload, ensure_ascii=False)},
                        ],
                        "temperature": 0.4,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                raw = (resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
                data = json.loads(raw)
                return data if isinstance(data, dict) else {"action": "no_action"}
        except Exception as exc:
            logger.debug(f"自主决策请求跳过: {exc}")
            return {"action": "no_action", "reason": f"error:{type(exc).__name__}"}

    async def evaluate_owner_care_trigger(
        self, uid: str, text: str, *, care_hint: str = ""
    ) -> Optional[dict[str, Any]]:
        """由 AI 为主人准备本机关怀动作，沿用统一确认、配额和执行流程。"""
        from .device_actuator import mac_actions
        return await mac_actions.plan_message(uid, text)

    def get_status(self) -> dict[str, Any]:
        now = self._now()
        can_qz, qz_reason = self.can_publish_autonomous_qzone(now_ts=now)
        dt = datetime.datetime.fromtimestamp(now, tz=_TZ)
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today_posts = [t for t in self._qzone_timestamps if t >= day_start]
        uncertain_today = [t for t in self._uncertain_qzone_timestamps if t >= day_start]
        max_per_day = int(getattr(state, "autonomous_qzone_max_per_day", 2))
        min_interval_hours = float(getattr(state, "autonomous_qzone_min_interval_hours", 6.0))
        start_h = int(getattr(state, "autonomous_quiet_start_hour", 23))
        end_h = int(getattr(state, "autonomous_quiet_end_hour", 8))
        queued_map = {gid: len(q) for gid, q in self._event_queues.items() if q}
        pending_promises = [p.to_dict() for p in self._pending_promises.values() if p.status == "pending"]
        return {
            "autonomous_group_chat_enabled": bool(getattr(state, "autonomous_group_chat_enabled", False)),
            "autonomous_qzone_enabled": bool(getattr(state, "autonomous_qzone_enabled", False)),
            "consumer_running": bool(self._worker_task is not None and not self._worker_task.done()),
            "promise_heartbeat_running": bool(self._promise_task is not None and not self._promise_task.done()),
            "promise_heartbeat_sec": self.promise_heartbeat_sec,
            "promise_tick_count": self._promise_tick_count,
            "pending_promises_count": len(pending_promises),
            "pending_promises": pending_promises[:20],
            "learned_rules_count": len(self._learned_rules),
            "learned_rules": list(self._learned_rules)[:12],
            "feedback_log_count": len(self._interaction_feedback_log),
            "owner_work_checklists": list(self._owner_work_checklists)[-10:],
            "consumer_tick_count": self._consumer_tick_count,
            "consumer_batch_count": self._consumer_batch_count,
            "consumer_error_count": self._consumer_error_count,
            "last_consumer_run_at": self._last_consumer_run_at,
            "last_promise_scan_at": self._last_promise_scan_at,
            "last_consumer_error": self._last_consumer_error,
            "quiet_hours_active": self.is_quiet_hours(dt),
            "quiet_hours_window": f"{start_h:02d}:00 - {end_h:02d}:00",
            "can_publish_qzone_now": can_qz,
            "qzone_quota_reason": qz_reason,
            "qzone_today_count": len(today_posts),
            "qzone_uncertain_today_count": len(uncertain_today),
            "qzone_max_per_day": max_per_day,
            "qzone_min_interval_hours": min_interval_hours,
            "mood": self._mood.to_dict(),
            "recent_actions": list(self._recent_actions)[-10:],
            "queued_groups": queued_map,
            "queued_events_total": sum(queued_map.values()),
        }


autonomous_coordinator = AutonomousAgentCoordinator()
