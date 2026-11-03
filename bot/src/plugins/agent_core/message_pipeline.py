"""消息解析与准入判定不访问网络、不修改窗口，便于独立测试。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Callable


class Intent(str, Enum):
    CHAT = "chat"
    TEXT_TO_IMAGE = "text_to_image"
    IMAGE_TO_IMAGE = "image_to_image"
    DOCUMENT = "document"
    CONTROL = "control"


@dataclass
class ParsedMessage:
    clean_text: str
    reply_context: str
    is_at_me: bool
    is_name_wake: bool
    in_continuous_chat: bool
    intent: Intent
    has_other_at: bool = False


ALIASES = {
    "amiya": (
        "阿米娅", "兔兔", "amiya", "小兔子", "小兔", "阿米驴", "驴驴", "小卡特斯",
        "剑兔", "近卫兔", "法兔", "奶兔", "魔王兔", "罗德岛领袖", "兔领袖", "卡特斯女孩",
    ),
    "kaltsit": (
        "凯尔希", "老女人", "kaltsit", "kal'tsit", "凯太后", "前妻", "前妻哥", "前妻姐", "前妻女士",
        "老猫", "绿猫", "绿色猞猁", "猞猁大夫", "凯医生", "凯尔希医生", "凯主任", "凯老师", "谜语人", "大菲林",
    ),
    "dania": (
        "达妮娅", "达尼亚", "dania", "daniya", "娅娅", "妮娅", "小达", "达达", "达宝",
        "小妮娅", "达妮娅酱", "星炬达妮娅", "虚质科学部少女", "科学部首席", "拉海洛少女",
    ),
}
_TASK_COMMANDS = {"work", "doc", "pdf", "docx", "word", "md", "markdown", "export", "draw", "workflow"}

_AMIYA_NAME_RE = re.compile(
    "|".join(re.escape(alias) for alias in sorted(ALIASES["amiya"], key=len, reverse=True)),
    re.IGNORECASE,
)
_AMIYA_STOP_RE = re.compile(
    r"闭(?:上)?嘴|住口|别说了|不要再说(?:话)?|别再说(?:话)?|不用(?:再)?说(?:话)?|"
    r"不必(?:再)?说(?:话)?|别继续说(?:话)?|别讲话|不要讲话|别吵(?:了)?|"
    r"安静(?:点|一点)?|闭麦|少说(?:两句)?|别插嘴|消停(?:点|一下)|别来烦我|别烦我|"
    r"不想听你说(?:话)?|我不想听(?:你说话)?|别叨叨|不要说话",
    re.IGNORECASE,
)
_AMIYA_FRUSTRATION_RE = re.compile(
    r"(?:你|阿米娅|兔兔|amiya).{0,10}(?:烦不烦|烦死了|真烦|很烦|吵死了|好吵|太吵|"
    r"啰嗦|废话|聒噪|没完没了|受不了|不耐烦|讨厌|没耐心|无语)|"
    r"(?:烦不烦|烦死了|真烦|很烦|吵死了|好吵|太吵|啰嗦|废话|聒噪|没完没了|"
    r"受不了|不耐烦|讨厌|没耐心|无语).{0,10}(?:你|阿米娅|兔兔|amiya)",
    re.IGNORECASE,
)
_AMIYA_WAKE_RE = re.compile(
    r"(?:继续(?:说|讲|回答)|(?:可以|允许你)(?:继续)?(?:说|讲|回答)|"
    r"恢复.{0,4}(?:说话|发言)|出来.{0,3}(?:说|回答)|开口吧|说吧|回答吧|继续吧)",
    re.IGNORECASE,
)


def classify_amiya_interaction(
    text: str,
    *,
    is_at_me: bool,
    is_private: bool,
    character: str = "amiya",
) -> str | None:
    """仅识别明确指向阿米娅的停话/不耐烦和明确唤醒语，避免误判无关抱怨。"""
    s = (text or "").strip()
    if not s:
        return None

    named_amiya = bool(_AMIYA_NAME_RE.search(s))
    directed = bool(is_at_me or is_private or named_amiya)
    if not directed:
        return None

    if _AMIYA_STOP_RE.search(s) and (named_amiya or (character or "").strip().lower() == "amiya"):
        return "silence"
    if _AMIYA_WAKE_RE.search(s):
        return "wake"
    if _AMIYA_FRUSTRATION_RE.search(s) and (named_amiya or (character or "").strip().lower() == "amiya"):
        return "silence"
    return None

_PURE_QA_OR_TRANSLATE_RE = re.compile(
    r"^(?:请问|帮我|给我|麻烦|能|可以)?(?:看看|看下|看一看|讲讲|说说|解释|翻译|总结|概括|识别|读一下|念一下)?"
    r"(?:这(?:张|幅|个)?(?:图|图片|照片|表情|截图|消息|话|内容)?(?:里|里面|上|上面)?)?"
    r"(?:(?:画|写|讲|说|发)(?:的|着|了)?(?:是)?(?:啥|什么|谁|哪)|是(?:啥|什么|谁|哪|怎么回事)|什么意思|啥意思|什么风格|什么画风|怎么看|对吗|真的假的)[？?！!。~～\s]*$",
    re.IGNORECASE,
)

_EXPLICIT_DRAW_ACTION_RE = re.compile(
    r"(?:帮我|给我|请|来|再)?(?:画一|画张|画幅|画个|生图|生成一张|生成一幅|生成图片|生成差分|差分图|重绘|改图|修图|垫图|图生图|换成|改成|变q版|做成q版|谈恋爱|同框|合影)",
    re.IGNORECASE,
)


def is_pure_vision_qa_query(text: str) -> bool:
    s = (text or "").strip().lower()
    if not s or s.startswith(("/draw", "/workflow")):
        return False
    if _PURE_QA_OR_TRANSLATE_RE.match(s):
        return True
    if any(
        q in s
        for q in (
            "这是啥", "这是什么", "这图是啥", "这图是什么", "图里是什么", "图里是啥",
            "图片里是什么", "写的是什么", "写的啥", "画的是什么", "画的是啥", "画了什么", "画的是谁",
            "什么图", "啥图", "是谁", "哪位", "什么风格", "什么画风",
            "帮我翻译", "翻译一下", "翻译这张", "翻译这幅", "总结一下", "概括一下", "看看这张图", "看下这张图",
        )
    ) and not _EXPLICIT_DRAW_ACTION_RE.search(s):
        return True
    return False


def classify_intent(text: str, *, has_image: bool, detect_draw: Callable[..., bool], detect_formats: Callable[..., Any]) -> Intent:
    command = text.split(maxsplit=1)[0].lower().removeprefix("/") if text.startswith("/") else ""
    if text.startswith("/") and command not in _TASK_COMMANDS:
        return Intent.CONTROL
    if detect_formats(text) or command == "export":
        return Intent.DOCUMENT
    if is_pure_vision_qa_query(text):
        return Intent.CHAT
    if detect_draw(text, has_image=has_image):
        return Intent.IMAGE_TO_IMAGE if has_image else Intent.TEXT_TO_IMAGE
    return Intent.CHAT


def parse_message(event: Any, *, character: str, last_interaction: float | None, now: float,
                  detect_draw: Callable[..., bool], detect_formats: Callable[..., Any]) -> ParsedMessage:
    segments = list(getattr(event, "original_message", None) or event.message)
    clean_text = "".join(str(seg.data.get("text", "")) for seg in segments if seg.type == "text").strip()
    self_id = str(event.self_id)
    reply = getattr(event, "reply", None)
    sender = getattr(getattr(reply, "sender", None), "user_id", None)
    at_ids = []
    # QQ 引用消息紧随的第一个 At 是协议附带的回复对象：
    # - 若同时 @ 了机器人或点了机器人名字，不将其视为“排他性 @ 他人”；
    # - 若既未 @ 机器人也未点机器人名字，则属于群友之间的定向引用回复（has_other_at=True），免艾特模式下也不横插一脚。
    skip_reply_at = False
    skipped_reply_other_at = bool(sender is not None and str(sender) not in ("", self_id))
    for seg in segments:
        if seg.type == "reply":
            skip_reply_at = True
            continue
        if seg.type == "at":
            target = str(seg.data.get("qq", ""))
            if skip_reply_at and (sender is None or target == str(sender)):
                skip_reply_at = False
                if target != self_id:
                    if target:
                        skipped_reply_other_at = True
                    continue
            at_ids.append(target)
        if seg.type != "text" or str(seg.data.get("text", "")).strip():
            skip_reply_at = False
    at_me = self_id in at_ids or bool(event.is_tome())
    aliases = ALIASES.get(character, ALIASES["amiya"])
    name_wake = any(re.search(rf"(?<![a-zA-Z0-9_]){re.escape(a)}(?![a-zA-Z0-9_])", clean_text, re.I)
                    if a.isascii() else a in clean_text for a in aliases)

    # 记录群内多人发言时间轴与当前消息上下文元数据（用于多人连续说话时精准对齐发言时间与对象）
    try:
        from . import local_rag_service
        gid = getattr(event, "group_id", None)
        mid = getattr(event, "message_id", None)
        uid = str(getattr(event, "user_id", "") or "")
        ev_sender = getattr(event, "sender", None)
        sname = (
            getattr(ev_sender, "card", None)
            or getattr(ev_sender, "nickname", None)
            or (ev_sender.get("card") or ev_sender.get("nickname") if isinstance(ev_sender, dict) else "")
            or uid
        )
        meta = local_rag_service.record_group_timeline_event(
            str(gid) if gid is not None else None,
            str(mid or ""),
            uid,
            str(sname).strip(),
            clean_text,
        )
        local_rag_service._CURRENT_MSG_META.set(meta)
    except Exception:
        pass

    is_group_ev = getattr(event, "group_id", None) is not None
    raw_intent = classify_intent(
        clean_text,
        has_image=any(s.type == "image" for s in segments),
        detect_draw=detect_draw,
        detect_formats=detect_formats,
    )
    # 群聊中未 @ 机器人、未带名字唤醒、且非 /draw 指令的普通群友闲聊，绝不触发高耗时生图
    if is_group_ev and raw_intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE):
        if not (at_me or name_wake or clean_text.lower().startswith(("/draw", "/workflow"))):
            raw_intent = Intent.CHAT

    has_other_at = any(target not in ("", self_id) for target in at_ids) or (
        skipped_reply_other_at and not at_me and not name_wake
    )
    return ParsedMessage(
        clean_text, "", at_me, name_wake,
        last_interaction is not None and 0 <= now - last_interaction <= 60 and len(clean_text) >= 2,
        raw_intent,
        has_other_at,
    )


def should_reply(
    parsed: ParsedMessage,
    *,
    global_active: bool,
    is_private: bool,
    group_allowed: bool,
    auto_reply_mode: bool = False,
) -> bool:
    if not global_active or parsed.intent == Intent.CONTROL:
        return False
    if is_private:
        return True
    if not group_allowed:
        return False
    if auto_reply_mode:
        # 免触发词默认回复模式：只要本消息没有 @ 别人，自动接收并回复
        return not parsed.has_other_at
    return parsed.is_at_me or (not parsed.has_other_at and (parsed.is_name_wake or parsed.in_continuous_chat))
