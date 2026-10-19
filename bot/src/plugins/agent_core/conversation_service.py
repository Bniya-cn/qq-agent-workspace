"""按 QQ 号保存已受理对话；24 小时无有效用户消息后自动清理过期会话。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Optional

from nonebot import logger

from .config_manager import BOT_ROOT

# 用户会话按 QQ 归属，不跟随群聊；受理消息才延长生命周期。
TTL_SECONDS = 24 * 60 * 60
MAX_TURNS = 16
MAX_HISTORY_CHARS = 28000
MAX_DOCUMENT_CHARS = 120000


def _clean_history_list(raw_history: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for item in raw_history or []:
        if not isinstance(item, dict):
            continue
        ans = str(item.get("assistant") or "")
        if not isinstance(item.get("user"), str) or not ans:
            continue
        cleaned.append(item)
    return cleaned


@dataclass
class Session:
    uid: str
    session_id: str
    history: list[dict]
    document: dict | None
    active_state: dict | None = None


class ConversationStore:
    def __init__(self, path: Path):
        self.path = path
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS sessions ("
                    "uid TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                    "last_user_at REAL NOT NULL, history TEXT NOT NULL, document TEXT)"
                )
                yield db
        finally:
            db.close()

    @asynccontextmanager
    async def user_turn(self, uid: str):
        lock, count = self._locks.get(uid, (asyncio.Lock(), 0))
        self._locks[uid] = (lock, count + 1)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=30)
            try:
                yield
            finally:
                lock.release()
        finally:
            _, count = self._locks[uid]
            if count == 1:
                del self._locks[uid]
            else:
                self._locks[uid] = (lock, count - 1)

    def purge(self, now: float | None = None):
        cutoff = (time.time() if now is None else now) - TTL_SECONDS
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE last_user_at <= ?", (cutoff,))

    def begin(self, uid: str, now: float | None = None) -> Session:
        if not uid.isdigit():
            raise ValueError("会话必须使用 QQ 号")
        now = time.time() if now is None else now
        with self._connect() as db:
            db.execute("DELETE FROM sessions WHERE last_user_at <= ?", (now - TTL_SECONDS,))
            row = db.execute("SELECT session_id, history, document FROM sessions WHERE uid = ?", (uid,)).fetchone()
            if row:
                cleaned_hist = _clean_history_list(json.loads(row[1]))
                restored_state = None
                if cleaned_hist and isinstance(cleaned_hist[-1].get("active_state"), dict):
                    restored_state = cleaned_hist[-1]["active_state"]
                session = Session(uid, row[0], cleaned_hist, json.loads(row[2]) if row[2] else None, restored_state)
                db.execute(
                    "UPDATE sessions SET last_user_at = ?, history = ? WHERE uid = ?",
                    (now, json.dumps(cleaned_hist, ensure_ascii=False), uid),
                )
            else:
                session = Session(uid, uuid.uuid4().hex, [], None, None)
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, NULL)", (uid, session.session_id, now, "[]"))
        return session

    def remember(self, session: Session, user: str, reply: str, *, document: dict | None = None):
        turn_entry: dict = {"user": user[:4000], "assistant": reply[:8000], "ts": int(time.time())}
        if isinstance(session.active_state, dict) and session.active_state.get("has_active_state"):
            turn_entry["active_state"] = session.active_state
        history = _clean_history_list(session.history) + [turn_entry]
        history = history[-MAX_TURNS:]
        while len(history) > 1 and sum(len(t["user"]) + len(t["assistant"]) for t in history) > MAX_HISTORY_CHARS:
            history.pop(0)
        document = document if document is not None else session.document
        if document:
            document = dict(document, content=str(document.get("content", ""))[:MAX_DOCUMENT_CHARS])
        with self._connect() as db:
            changed = db.execute(
                "UPDATE sessions SET history = ?, document = ? "
                "WHERE uid = ? AND session_id = ? AND last_user_at > ?",
                (
                    json.dumps(history, ensure_ascii=False),
                    json.dumps(document, ensure_ascii=False) if document else None,
                    session.uid,
                    session.session_id,
                    time.time() - TTL_SECONDS,
                ),
            ).rowcount
        if changed:
            session.history = history
            session.document = document

    def checkpoint(self, session: Session, document: dict):
        with self._connect() as db:
            changed = db.execute(
                "UPDATE sessions SET document = ? WHERE uid = ? AND session_id = ? AND last_user_at > ?",
                (json.dumps(document, ensure_ascii=False), session.uid, session.session_id, time.time() - TTL_SECONDS),
            ).rowcount
        if changed:
            session.document = document

    def clear_active_state(self, uid: str):
        with self._connect() as db:
            row = db.execute("SELECT history FROM sessions WHERE uid = ?", (uid,)).fetchone()
            if row:
                try:
                    hist = json.loads(row[0])
                    for turn in hist:
                        turn.pop("active_state", None)
                    db.execute("UPDATE sessions SET history = ? WHERE uid = ?", (json.dumps(hist, ensure_ascii=False), uid))
                except Exception:
                    pass


store = ConversationStore(BOT_ROOT / "data" / "conversations.sqlite3")
_session: ContextVar[Session | None] = ContextVar("qq_conversation", default=None)
_include_history: ContextVar[bool] = ContextVar("qq_include_history", default=True)
_current_group_id: ContextVar[Optional[str]] = ContextVar("qq_current_group_id", default=None)


@contextmanager
def use_session(session: Session, group_id: Optional[str] = None):
    token = _session.set(session)
    g_token = _current_group_id.set(group_id)
    try:
        yield
    finally:
        _current_group_id.reset(g_token)
        _session.reset(token)


@contextmanager
def history_enabled(enabled: bool):
    token = _include_history.set(enabled)
    try:
        yield
    finally:
        _include_history.reset(token)


def current() -> Session | None:
    return _session.get()


def get_active_state() -> dict | None:
    session = current()
    if session is None or not _include_history.get():
        return None
    if isinstance(session.active_state, dict) and session.active_state.get("has_active_state"):
        return session.active_state
    return None


def set_active_state(state_obj: dict | None) -> None:
    session = current()
    if session is not None:
        if isinstance(state_obj, dict) and state_obj.get("has_active_state"):
            session.active_state = state_obj
        else:
            session.active_state = None


_STATE_RELEASE_KEYWORDS = (
    # 封嘴/口部束缚解除
    "撕下", "撕掉", "撕了", "拿掉", "拿开", "取下", "解开", "松开", "脱下", "摘下",
    "不封了", "解封", "说话吧", "张嘴", "别呜了", "撕下来", "已经撕", "没有胶带", "帮我撕",
    # 运动/体罚/负重解除
    "停", "停下", "停下来", "不用做", "别做", "不用跑", "别跑", "不用扛", "别扛", "不用背",
    "放下", "放下吧", "休息", "休息吧", "休息一下", "好了", "可以了", "行了", "算你过",
    "饶了你", "合格了", "结束", "结束惩罚", "站起来", "回来吧", "恢复正常", "解除状态", "不玩了",
    # 通用管理命令
    "/clear_state", "解除", "恢复",
)


def check_and_fast_release_state(raw_user_text: str) -> bool:
    """在生成前快速检测用户是否正在解除当前状态（停止俯卧撑/跑步/放下重物/撕胶带），命中立即清空状态。"""
    st = get_active_state()
    if not st:
        return False
    text = (raw_user_text or "").strip()
    if any(kw in text for kw in _STATE_RELEASE_KEYWORDS):
        set_active_state(None)
        return True
    return False


def format_active_state_directive() -> str:
    st = get_active_state()
    if not st:
        return ""
    desc = str(st.get("state_description") or "").strip()
    rel = str(st.get("release_condition") or "用户明确下令解除/停止/休息").strip()
    if st.get("speech_blocked"):
        return (
            f"\n【当前对该 QQ 用户持续生效的受限状态（最高优先级）】：你当前正处于「{desc or '嘴巴被封住/无法说话'}」状态（解除条件：{rel}）。"
            "在用户明确下令解除（如撕下胶带、拿开）之前，你在物理上根本无法开口说出任何正常词汇！"
            "你的整条回复必须 100% 仅由「唔……唔唔！嗯……呜呜……唔？」等闭口鼻音拟声词组成，绝不能包含任何正常说话的字词！"
        )
    return (
        f"\n【当前对该 QQ 用户持续生效的处境/身体状态（最高优先级）】：你当前正处于「{desc}」状态（解除条件：{rel}）。"
        "在用户明确下令停止或解除（如休息、放下重物、停止运动）之前，即使当前用户在聊其他话题（如考公、日常闲聊），"
        "你的语音台词与文字回复也必须全程保持该状态下的真实生理反应（如剧烈喘息「呼……哈……」、吃力感、计数）并自然提及该状态，绝不能若无其事地忽略它！"
    )


def history_messages() -> list[dict]:
    session = current()
    if session is None or not _include_history.get():
        return []
    return [
        message
        for turn in _clean_history_list(session.history)[-12:]
        for message in (
            {"role": "user", "content": turn["user"]},
            {"role": "assistant", "content": turn["assistant"]},
        )
    ]


_ASSOCIATIVE_REASONING_HINT = (
    "\n【上下文准则】：请结合历史对话与当前消息保持自然连贯。"
    "若历史仅涉及已完结的普通知识问答，无需在新话题中生硬重复无关旧事。"
)


def messages(system: str, user_content) -> list[dict]:
    has_ctx = bool(current() and _include_history.get())
    active_dir = format_active_state_directive()
    sys_prompt = (system or "") + ((_ASSOCIATIVE_REASONING_HINT + active_dir) if has_ctx else "")
    return [{"role": "system", "content": sys_prompt}, *history_messages(), {"role": "user", "content": user_content}]


def app_prompt(prompt: str) -> str:
    session = current()
    history = _clean_history_list(session.history)[-10:] if (session and _include_history.get()) else []
    active_dir = format_active_state_directive().strip()
    if not history and not active_dir:
        return prompt

    parts: list[str] = [_ASSOCIATIVE_REASONING_HINT.strip()]
    if active_dir:
        parts.append(active_dir)
    if history:
        formatted_turns: list[str] = []
        for turn in history:
            u_txt = str(turn.get("user") or "").strip()
            a_txt = str(turn.get("assistant") or "").strip()
            if len(a_txt) > 260:
                a_txt = a_txt[:260] + "…"
            formatted_turns.append(f"- 用户曾说：{u_txt}\n  你的回复：{a_txt}")
        if formatted_turns:
            parts.append(
                "【近期对话背景与持续生效的角色状态（若存在封嘴/背沙子等未解除状态，必须跨话题持续执行）】：\n"
                + "\n".join(formatted_turns)
            )
    parts.append("【用户当前最新请求】：\n" + prompt)
    if active_dir:
        parts.append(active_dir)
    return "\n\n".join(parts)


def remember(user: str, reply: str, *, document: dict | None = None):
    session = current()
    if session:
        try:
            if document is not None and session.document and session.document.get("content") == document.get("content"):
                document = {**session.document, **document}
            store.remember(session, user, reply, document=document)
        except Exception as exc:
            logger.warning(f"保存用户会话失败，已发送的回复不受影响: {exc}")
