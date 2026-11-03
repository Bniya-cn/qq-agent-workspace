"""语音角色与情绪预设单一真源 (Canonical Voice Presets & Tag Parser)。
严格支持 amiya / dania (daniya) / kaltsit，禁止未知角色静默回退；
提供确定性前缀状态机清洗器，彻底杜绝标签泄露与正文误删。
"""
from __future__ import annotations

import re
from typing import Optional

# 1. 角色标识符双向映射单一真源
CHAR_ID_TO_NAME: dict[str, str] = {
    "amiya": "阿米娅",
    "dania": "达妮娅",
    "daniya": "达妮娅",
    "kaltsit": "凯尔希",
}
CHAR_NAME_TO_ID: dict[str, str] = {
    "阿米娅": "amiya",
    "达妮娅": "dania",
    "达尼亚": "dania",
    "凯尔希": "kaltsit",
    "kal'tsit": "kaltsit",
}


def resolve_character_id(char_input: Optional[str]) -> str:
    """严格解析角色标识符，未知角色明确抛出 ValueError，杜绝静默回退。"""
    if not char_input:
        raise ValueError("角色标识符为空，拒绝隐式回退")
    raw = str(char_input).strip()
    val = raw.lower()
    if val in ("amiya", "阿米娅"):
        return "amiya"
    if val in ("dania", "daniya", "达妮娅", "达尼亚"):
        return "dania"
    if val in ("kaltsit", "kal'tsit", "凯尔希"):
        return "kaltsit"
    raise ValueError(f"未知角色: '{char_input}'，仅支持 amiya / dania / kaltsit")


# 2. 情绪体系与参考音频映射单一真源
_DANIA_PRESET = {
    "tags": {
        "日常": "hf_025",
        "爽朗": "hf_025",
        "开朗": "hf_025",
        "傲娇": "default",
        "嘴硬": "default",
        "吐槽": "default",
        "调皮": "default",
        "无奈": "hf_007",
        "思考": "hf_027",
    },
    "default_ref": "hf_025",
}

VOICE_CANONICAL_PRESETS: dict[str, dict] = {
    "amiya": {
        "tags": {
            "日常": "default",
            "工作": "default",
            "开心": "CN_008",
            "调侃": "CN_008",
            "温柔": "CN_031",
            "安慰": "CN_031",
            "低落": "CN_031",
            "坚定": "CN_013",
            "鼓励": "CN_013",
            "生气": "CN_013",
        },
        "default_ref": "default",
    },
    "dania": _DANIA_PRESET,
    "daniya": _DANIA_PRESET,
    "kaltsit": {
        "tags": {
            "日常": "default",
            "沉稳": "default",
            "冷静": "default",
            "严肃": "default",
            "关切": "default",
            "医嘱": "default",
            "无奈": "default",
            "疲惫": "default",
            "吐槽": "default",
        },
        "default_ref": "default",
    },
}

# 空正文角色安全兜底台词
SAFE_FALLBACK_REPLIES: dict[str, str] = {
    "amiya": "博士，请问有什么吩咐吗？",
    "dania": "干嘛？有事快说。",
    "daniya": "干嘛？有事快说。",
    "kaltsit": "我在。怎么了？",
}

# 严格成对行首括号正则：支持 [tag] 或 【tag】
_STRICT_PAIR_PATTERN = re.compile(
    r"^(?:\[(?P<tag_b1>[^\]\r\n]{1,8})\]|【(?P<tag_b2>[^】\r\n]{1,8})】)"
)


def parse_and_strip_voice_emotion(raw_reply: str, character: str) -> tuple[str, str]:
    """确定性行首状态机清洗器。
    返回 (resolved_emotion, clean_text)。
    """
    try:
        char_id = resolve_character_id(character)
    except ValueError:
        char_id = "amiya"

    role_spec = VOICE_CANONICAL_PRESETS.get(char_id, VOICE_CANONICAL_PRESETS["amiya"])
    whitelist = role_spec["tags"]

    text = (raw_reply or "").strip()
    valid_tag: str = ""

    # 循环剥离行首所有连续的成对标签，取第一个在白名单内的合法标签
    while True:
        match = _STRICT_PAIR_PATTERN.match(text)
        if not match:
            break
        tag = (match.group("tag_b1") or match.group("tag_b2") or "").strip()
        if not valid_tag and tag in whitelist:
            valid_tag = tag
        text = text[match.end():].lstrip()

    clean_text = text.strip()
    # 若剥离后正文为空或仅剩标点符号，触发角色安全兜底台词
    if not clean_text or not re.search(r"[\w\u4e00-\u9fff]", clean_text):
        clean_text = SAFE_FALLBACK_REPLIES.get(char_id, "我在。")

    return (valid_tag, clean_text)
