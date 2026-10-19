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
    # QQ 引用消息紧随的第一个 At 是协议附带的回复对象，不参与其他人定向判断。
    skip_reply_at = False
    for seg in segments:
        if seg.type == "reply":
            skip_reply_at = True
            continue
        if seg.type == "at":
            target = str(seg.data.get("qq", ""))
            if skip_reply_at and (sender is None or target == str(sender)):
                skip_reply_at = False
                if target != self_id:
                    continue
            at_ids.append(target)
        if seg.type != "text" or str(seg.data.get("text", "")).strip():
            skip_reply_at = False
    at_me = self_id in at_ids or bool(event.is_tome())
    aliases = ALIASES.get(character, ALIASES["amiya"])
    name_wake = any(re.search(rf"(?<![a-zA-Z0-9_]){re.escape(a)}(?![a-zA-Z0-9_])", clean_text, re.I)
                    if a.isascii() else a in clean_text for a in aliases)
    return ParsedMessage(
        clean_text, "", at_me, name_wake,
        last_interaction is not None and 0 <= now - last_interaction <= 60 and len(clean_text) >= 2,
        classify_intent(clean_text, has_image=any(s.type == "image" for s in segments),
                        detect_draw=detect_draw, detect_formats=detect_formats),
        any(target not in ("", self_id) for target in at_ids),
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
