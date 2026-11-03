"""按 QQ 号保存已受理对话；24 小时无有效用户消息后自动清理过期会话。

关键增强：
1. 原子追加：remember() 在数据库事务内重读最新 history 后原子追加本轮记录，绝不在释放 user_turn 锁后用旧内存快照整份覆盖 SQLite。
2. 角色与风格标记：每条 assistant 记录标记 character 与 style（保持老数据兼容），在构建上下文时过滤或标注异角色/旧群友接梗口吻，防止把其他角色或旧镜像坏口吻当成阿米娅必须模仿的历史。
3. 已送达与待发送区分：明确记录 delivered_text 与 pending_segments，绝不假定用户已经看过因投递中断而未发出的后半段。
"""
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

from .config_manager import BOT_ROOT, state

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
        if not isinstance(item.get("user"), str) or (not ans and item.get("delivery_status") != "received_only"):
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

    def remember(
        self,
        session: Session,
        user: str,
        reply: str,
        *,
        document: dict | None = None,
        character: Optional[str] = None,
        style: Optional[str] = None,
        delivery_status: str = "delivered",
        pending_segments: Optional[list[str]] = None,
    ):
        char_val = (character or getattr(state, "active_character", "amiya") or "amiya").strip().lower()
        if char_val in ("none", "default", ""):
            char_val = "amiya"
        style_val = (style or getattr(state, "small_talk_style", "pure_persona") or "pure_persona").strip().lower()

        assistant_stored = reply[:8000]
        if pending_segments:
            assistant_stored = (
                f"{assistant_stored}\n[系统记录：受发送节奏或回执中断影响，尚有 {len(pending_segments)} 段后续内容未向用户送达，请勿假定用户已读]"
            )

        turn_entry: dict = {
            "user": user[:4000],
            "assistant": assistant_stored,
            "ts": int(time.time()),
            "visibility": "group" if current_group_id() is not None else "private",
            "character": char_val,
            "style": style_val,
            "delivery_status": delivery_status,
        }
        if pending_segments:
            turn_entry["pending_segments"] = [s[:1200] for s in pending_segments[:10]]
        if isinstance(session.active_state, dict) and session.active_state.get("has_active_state"):
            turn_entry["active_state"] = session.active_state

        with self._connect() as db:
            # 原子读取数据库当前最新 history，防止释放 user_turn 锁后用旧内存快照覆盖并发写入的新对话
            row = db.execute(
                "SELECT history, document FROM sessions WHERE uid = ? AND session_id = ? AND last_user_at > ?",
                (session.uid, session.session_id, time.time() - TTL_SECONDS),
            ).fetchone()
            if row is not None:
                try:
                    db_hist = _clean_history_list(json.loads(row[0] or "[]"))
                except Exception:
                    db_hist = _clean_history_list(session.history)
                db_doc = json.loads(row[1]) if row[1] else session.document
            else:
                db_hist = _clean_history_list(session.history)
                db_doc = session.document

            history = db_hist + [turn_entry]
            history = history[-MAX_TURNS:]
            while len(history) > 1 and sum(len(t["user"]) + len(t["assistant"]) for t in history) > MAX_HISTORY_CHARS:
                history.pop(0)

            eff_doc = document if document is not None else db_doc
            if eff_doc:
                eff_doc = dict(eff_doc, content=str(eff_doc.get("content", ""))[:MAX_DOCUMENT_CHARS])

            changed = db.execute(
                "UPDATE sessions SET history = ?, document = ? "
                "WHERE uid = ? AND session_id = ? AND last_user_at > ?",
                (
                    json.dumps(history, ensure_ascii=False),
                    json.dumps(eff_doc, ensure_ascii=False) if eff_doc else None,
                    session.uid,
                    session.session_id,
                    time.time() - TTL_SECONDS,
                ),
            ).rowcount
        if changed:
            session.history = history
            session.document = eff_doc

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
def use_session(session: Session | None, group_id: Optional[str] = None):
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


def current_group_id() -> str | None:
    return _current_group_id.get()


def visible_history(
    session: Session,
    *,
    current_character: Optional[str] = None,
    current_style: Optional[str] = None,
) -> list[dict]:
    history = _clean_history_list(session.history)
    # 旧记录没有来源标记；公开回复不使用这类记录，避免将私聊带入群聊。
    # 生命周期统一由 begin() 的 24 小时闲置 TTL 管理；消息数量仍受 MAX_TURNS 限制。
    if current_group_id() is not None:
        history = [turn for turn in history if turn.get("visibility") == "group"]

    target_char = (current_character or getattr(state, "active_character", "amiya") or "amiya").strip().lower()
    if target_char in ("none", "default", ""):
        target_char = "amiya"
    target_style = (current_style or getattr(state, "small_talk_style", "pure_persona") or "pure_persona").strip().lower()

    filtered: list[dict] = []
    for turn in history:
        # 老数据未记录 character/style 时保持向后兼容，但过滤明显被污染的旧「大肥鱼/本鱼」回复
        t_char = str(turn.get("character") or "").strip().lower()
        t_style = str(turn.get("style") or "").strip().lower()
        ans_txt = str(turn.get("assistant") or "")
        if any(bad in ans_txt for bad in ("本鱼", "奶蛙", "大肥鱼")):
            continue
        if t_char and t_char not in ("none", "default") and t_char != target_char:
            # 异角色历史只保留用户的提问事实提示，不把异角色语气当作当前角色的 assistant 范本
            continue
        if target_style == "pure_persona" and t_style == "group_mimic":
            # 当切回纯角色模式时，不把旧群友接梗口吻当作必须模仿的 assistant 历史
            continue
        filtered.append(turn)
    return filtered


def get_active_state() -> dict | None:
    session = current()
    if session is None or not _include_history.get():
        return None
    if isinstance(session.active_state, dict) and session.active_state.get("has_active_state"):
        if current_group_id() is not None and session.active_state.get("_visibility") != "group":
            return None
        return session.active_state
    return None


def set_active_state(state_obj: dict | None) -> None:
    session = current()
    if session is not None:
        if isinstance(state_obj, dict) and state_obj.get("has_active_state"):
            session.active_state = dict(state_obj, _visibility="group" if current_group_id() is not None else "private")
        else:
            session.active_state = None


_STATE_RELEASE_KEYWORDS = (
    "撕下", "撕掉", "撕了", "拿掉", "拿开", "取下", "解开", "松开", "脱下", "摘下",
    "不封了", "解封", "说话吧", "张嘴", "别呜了", "撕下来", "已经撕", "没有胶带", "帮我撕",
    "停", "停下", "停下来", "不用做", "别做", "不用跑", "别跑", "不用扛", "别扛", "不用背",
    "放下", "放下吧", "休息", "休息吧", "休息一下", "好了", "可以了", "行了", "算你过",
    "饶了你", "合格了", "结束", "结束惩罚", "站起来", "回来吧", "恢复正常", "解除状态", "不玩了",
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
        for turn in visible_history(session)[-12:]
        for message in (
            {"role": "user", "content": turn["user"]},
            {"role": "assistant", "content": turn["assistant"]},
        )
        if message["content"]
    ]


_ASSOCIATIVE_REASONING_HINT = (
    "\n【上下文准则】：请结合历史对话与当前消息保持自然连贯。"
    "若历史仅涉及已完结的普通知识问答，无需在新话题中生硬重复无关旧事。"
    "用户纠正上一条回答时，先核对自己刚才的结论，不把纠正当成无关玩笑；没有原图依据时不要编造新的识别结论。"
)


def messages(system: str, user_content) -> list[dict]:
    has_ctx = bool(current() and _include_history.get())
    active_dir = format_active_state_directive()
    sys_prompt = (system or "") + ((_ASSOCIATIVE_REASONING_HINT + active_dir) if has_ctx else "")
    return [{"role": "system", "content": sys_prompt}, *history_messages(), {"role": "user", "content": user_content}]


def app_prompt(prompt: str) -> str:
    session = current()
    history = visible_history(session)[-10:] if (session and _include_history.get()) else []
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
            formatted_turns.append(f"- 用户曾说：{u_txt}" + (f"\n  你的回复：{a_txt}" if a_txt else "\n  （语音忙碌期间收到，尚未回复）"))
        if formatted_turns:
            parts.append(
                "【近期对话背景与持续生效的角色状态（若存在封嘴/背沙子等未解除状态，必须跨话题持续执行）】：\n"
                + "\n".join(formatted_turns)
            )
    parts.append("【用户当前最新请求】：\n" + prompt)
    if active_dir:
        parts.append(active_dir)
    return "\n\n".join(parts)


def remember(
    user: str,
    reply: str,
    *,
    document: dict | None = None,
    character: Optional[str] = None,
    style: Optional[str] = None,
    delivery_status: str = "delivered",
    pending_segments: Optional[list[str]] = None,
):
    session = current()
    if session:
        try:
            if document is not None and session.document and session.document.get("content") == document.get("content"):
                document = {**session.document, **document}
            store.remember(
                session,
                user,
                reply,
                document=document,
                character=character,
                style=style,
                delivery_status=delivery_status,
                pending_segments=pending_segments,
            )
        except Exception as exc:
            logger.warning(f"保存用户会话失败，已发送的回复不受影响: {exc}")
