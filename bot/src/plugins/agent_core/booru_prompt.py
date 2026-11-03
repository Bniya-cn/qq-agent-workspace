"""Illustrious / NoobAI 用的 Danbooru 标签提示词。

标签名来自 Danbooru 标签组，不是自造短语：
- 质量串不用 Pony 的 score_9，Illustrious 不认，还可能把 score 画成字。
- 角色标签把括号转义，避免被 CLIP 当成权重。
- 捆绑用 arms_behind_back、bound_wrists、bound_thighs、ball_gag 这类已有标签，
  避免「手被绑住」写成自然语言后模型仍画出能活动的手。
"""
from __future__ import annotations

import re

# Illustrious 质量串。NoobAI 再补 highres。都不要 score_9 / source_anime。
_QUALITY = {
    "illustrious": ("masterpiece", "best quality", "very aesthetic", "absurdres", "newest"),
    "noobai": ("masterpiece", "best quality", "newest", "absurdres", "highres"),
}

_BASE_NEGATIVE = (
    "lowres",
    "worst quality",
    "low quality",
    "bad anatomy",
    "bad hands",
    "missing fingers",
    "extra digits",
    "extra limbs",
    "text",
    "watermark",
    "signature",
    "blurry",
)

# 手被绑在背后时，这些标签会把胳膊拉回身前。
_BOUND_ARM_NEGATIVE = (
    "holding",
    "outstretched arms",
    "arms at sides",
    "spread arms",
    "hands on hips",
)

# 用户口语 -> Danbooru 标签。更具体的词放在「绳」「液体」前面，避免被收成笼统标签。
# crotch rope、vibrator、pussy juice、egg vibrator 都是 Danbooru 已有标签。
_NSFW_RULES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"口球|球塞|ball\s*gag|gagged|堵嘴", re.I), ("ball gag", "gagged")),
    (re.compile(r"手背后|双手背后|绑手|反绑|arms?\s*behind\s*back", re.I), ("arms behind back", "bound arms", "bound wrists")),
    (re.compile(r"手臂.{0,4}绳|上臂|arm\s*rope", re.I), ("bound arms", "rope")),
    (re.compile(r"大腿.{0,4}绳|绑腿|thigh\s*rope|bound\s*thighs", re.I), ("bound thighs", "rope")),
    (re.compile(r"股间绳|胯下绳|crotch\s*rope", re.I), ("crotch rope",)),
    (re.compile(r"遥控.{0,6}震动|remote\s*control\s*vibrator", re.I), ("remote control vibrator", "vibrator")),
    (re.compile(r"跳蛋|egg\s*vibrator", re.I), ("egg vibrator", "vibrator")),
    (re.compile(r"震动器|振动器|按摩棒|vibrator", re.I), ("vibrator",)),
    (re.compile(r"绳|捆绑|bondage|shibari|缚", re.I), ("bondage", "bound", "rope", "shibari", "shibari over clothes")),
    (re.compile(r"害羞|难为情|embarrassed|shy", re.I), ("shy", "embarrassed")),
    (re.compile(r"脸红|blush", re.I), ("blush",)),
    (re.compile(r"爱液|淫水|pussy\s*juice", re.I), ("pussy juice", "wet")),
    (re.compile(r"口水|唾液|saliva|drool", re.I), ("saliva", "drooling")),
    (re.compile(r"液体|湿身|湿透|湿漉", re.I), ("wet", "pussy juice", "saliva", "drooling", "sweat")),
    (re.compile(r"泪|哭|tears", re.I), ("tears",)),
    (re.compile(r"汗|sweat", re.I), ("sweat",)),
)


def escape_tag(tag: str) -> str:
    """Danbooru 括号标签写成 mika_\\(blue_archive\\)，避免 () 被当成权重。"""
    text = tag.strip().replace("\\", "")
    return text.replace("(", r"\(").replace(")", r"\)")


def nsfw_tags_for(user_text: str) -> list[str]:
    """按用户原文收集标准 NSFW 标签，去重并保持题库顺序。"""
    found: list[str] = []
    for pattern, tags in _NSFW_RULES:
        if pattern.search(user_text):
            for tag in tags:
                if tag not in found:
                    found.append(tag)
    return found


def arms_are_bound(tags: list[str]) -> bool:
    return "arms behind back" in tags or "bound wrists" in tags


def build_booru_prompt(
    *,
    user_text: str,
    character_tags: list[str],
    feature_tags: list[str] | None = None,
    clothing_tags: list[str] | None = None,
    scene_tags: list[str] | None = None,
    count: str = "1girl",
    base: str = "illustrious",
) -> tuple[str, str]:
    """按 Illustrious 的标签顺序拼正负向提示词。

    顺序：质量 -> 人数 -> 角色触发词 -> 外形 -> 服装 -> 姿势/NSFW -> 场景。
    角色触发词放在服装前面，避免衣服标签盖过 LoRA 触发词。
    """
    quality = _QUALITY.get(base, _QUALITY["illustrious"])
    nsfw = nsfw_tags_for(user_text)
    parts: list[str] = [*quality, count]
    parts.extend(escape_tag(tag) for tag in character_tags if tag.strip())
    parts.extend(tag.strip() for tag in (feature_tags or []) if tag.strip())
    parts.extend(tag.strip() for tag in (clothing_tags or []) if tag.strip())
    parts.extend(nsfw)
    parts.extend(tag.strip() for tag in (scene_tags or []) if tag.strip())
    # 去掉重复，保留第一次出现的位置。
    seen: set[str] = set()
    ordered: list[str] = []
    for tag in parts:
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(tag)
    negative = list(_BASE_NEGATIVE)
    if arms_are_bound(nsfw):
        negative.extend(_BOUND_ARM_NEGATIVE)
    return ", ".join(ordered), ", ".join(negative)
