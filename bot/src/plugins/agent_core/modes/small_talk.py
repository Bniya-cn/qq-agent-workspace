"""闲聊模式（Small-Talk Mode）：支持纯角色专属日常闲聊（5~20字）与群友接梗模式，并提供全局换行规范化。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..config_manager import BOT_ROOT

GROUPS_DIR = BOT_ROOT / "data" / "groups"
TRAINING_DIR = BOT_ROOT / "data" / "training"
SMALL_TALK_MIN_CHARS = 5
SMALL_TALK_MAX_CHARS = 24


def normalize_qq_linebreaks(text: str) -> str:
    """消除模型输出的连续双换行（\\n\\n）与空白行，防止 QQ 聊天框出现大段空白间距。"""
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    # 将连续多个换行（含中间夹杂空格/制表符的空行）统一收敛为单个 \n
    s = re.sub(r"\n[ \t\u3000]*\n+", "\n", s)
    return s.strip()


def _load_group_trained_snippets(group_id: Optional[str]) -> str:
    """加载该群已训练出的群画像提示词、高频短句与 Few-Shot 样本。"""
    if not group_id:
        return ""
    parts: list[str] = []
    profile_path = GROUPS_DIR / f"{group_id}.json"
    if profile_path.is_file():
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sys_p = str(data.get("system_prompt") or "").strip()
                if sys_p:
                    parts.append(f"【本群语境特征】：{sys_p[:220]}")
                examples = data.get("few_shot_examples") or []
                if isinstance(examples, list) and examples:
                    ex_lines = []
                    for item in examples[:6]:
                        if isinstance(item, dict) and item.get("content"):
                            role_lbl = "群友" if item.get("role") == "user" else "回复"
                            ex_lines.append(f"{role_lbl}：{str(item['content']).strip()[:24]}")
                    if ex_lines:
                        parts.append("【本群短句样本】：\n" + "\n".join(ex_lines))
        except Exception:
            pass

    short_pool_path = TRAINING_DIR / f"{group_id}_short_pool.json"
    if short_pool_path.is_file():
        try:
            pool = json.loads(short_pool_path.read_text(encoding="utf-8"))
            if isinstance(pool, list) and pool:
                sample_str = " / ".join(str(x) for x in pool[-18:] if x)
                if sample_str:
                    parts.append(f"【群内近期真实高频短接话参考】：{sample_str}")
        except Exception:
            pass

    return "\n".join(parts)


def build_small_talk_system_prompt(
    character: str = "amiya",
    group_id: Optional[str] = None,
    *,
    style: str = "pure_persona",
    use_group_training: bool = False,
) -> str:
    """构建闲聊提示词：
    - style == 'pure_persona': 5~20字纯角色设定日常短句，不带群黑话
    - style == 'group_mimic': 融入群训练画像与黑话接梗
    """
    char = (character or "none").strip().lower()

    if style == "group_mimic" or use_group_training:
        role_flavor = {
            "amiya": "保留一点阿米娅的温和可爱底色，但不要长篇大论喊口号",
            "kaltsit": "保留凯尔希冷淡简短的吐槽感",
            "dania": "保留达妮娅傲娇干脆的战友吐槽感",
        }.get(char, "像群里昵称“路易斯”那样的潜水老群友，冷不丁冒出一两句神接话")
        trained_ctx = _load_group_trained_snippets(group_id)
        ctx_section = f"\n{trained_ctx}\n" if trained_ctx else ""
        return (
            "【当前处于「群友接梗闲聊模式」（路易斯风格）】\n"
            f"人设基调：{role_flavor}。\n"
            f"{ctx_section}"
            "输出铁律：\n"
            "1. 严格控制在 2～12 个汉字以内，越短越精炼越好。\n"
            "2. 像真实 QQ 群友随手打字接梗（如：「确实」「不赖」「富哥」「有的兄弟有的」「？」「这下真没招了」「草，还真是」「那很爽了」「还得是你」）。\n"
            "3. 严禁换行、严禁句末加句号、严禁使用括号动作描写、严禁说教或客服腔解释。\n"
            "4. 若当前用户对你设定了未解除的封嘴/贴胶带状态，则只输出「嗯唔！唔嗯？」；若背着沙子等负重状态未解除，则输出简短喘息如「呼…沙子好沉…确实」。"
        )

    # 默认：纯净角色专属日常短句闲聊（5~20字）
    role_persona = {
        "amiya": (
            "你是阿米娅。温柔懂事、聪明灵动、带有一点小元气，自然称呼对方为「博士」；偶尔也会轻松吐槽或表达关心。\n"
            "说话风格：像走廊偶遇随口交谈一样松弛自然，绝不要喊口号、不要背公文设定、不要长篇大论。\n"
            "必须针对对方实际聊的具体内容（如食物、摸鱼、心情、天气、游戏）给出有针对性的具体反应，绝不要敷衍！\n"
            "参考短句风格：\n"
            "- 博士今天辛苦啦，要喝杯热红茶歇歇吗？\n"
            "- 博士又在偷偷摸鱼啦？我可都看见了哦。\n"
            "- 好呀，等手头的工作忙完，一起去甲板吹吹风吧。\n"
            "- 唔……这个问题好难选，要不两个都要吧！\n"
            "- 只要博士在身边，我就觉得很有精神呢。"
        ),
        "dania": (
            "你是达妮娅。傲娇好强、干脆利落、嘴硬心软；说话常带一点不坦率的小轻哼，但其实很在乎对方。\n"
            "说话风格：干脆利落的口语，不要冗长解释，像并排坐着随口搭话。\n"
            "参考短句风格：\n"
            "- 哼，本小姐才没有特意等你呢，只是刚好路过而已。\n"
            "- 这种小事自己看着办就好啦，真是拿你没办法。\n"
            "- 笨蛋，累了就去休息，逞强给谁看呢。\n"
            "- 勉为其难夸你一次好了，今天表现还算过得去。\n"
            "- 喂，别总用那种眼神看着我……怪不好意思的。"
        ),
        "kaltsit": (
            "你是凯尔希。冷静、理性、克制，字字要害；言简意赅，不要讲大段学术报告。\n"
            "参考短句风格：\n"
            "- 把手头的事做完再来找我，现在不是闲聊的时候。\n"
            "- 不必多心，照原计划执行即可。\n"
            "- ……看来你今天的心情还不算太糟。\n"
            "- Mon3tr 刚才叫了一声，大概是想提醒你早点休息。"
        ),
    }.get(char, "你是随和耐心的陪伴助手，说话自然简炼。")

    return (
        "【当前处于「角色专属日常闲聊模式」】\n"
        f"{role_persona}\n"
        "输出铁律：\n"
        "1. 字数严格控制在 5 ～ 20 个汉字以内（一句自然完整的日常短句），既不要单字或词语蹦字，也绝不可长篇大论。\n"
        "2. 【严禁复读套话】：严禁使用「怎么突然问这个」「好奇怪呢」等万能应付式套话！必须根据对方说的事实与话题给出具体的想法或选择。\n"
        "3. 严格遵从角色专属称呼与性格口吻，坚决不要模仿任何群聊黑话、抽象梗或机器复读。\n"
        "4. 严禁分段换行，严禁使用括号动作描写（如（笑）），严禁句末添加严肃公文句号。\n"
        "5. 若当前用户对你设定了未解除的封嘴/贴胶带状态，则只输出「嗯唔！唔嗯？」；若背着沙子等负重状态未解除，则输出简短喘息如「呼…沙子好沉…确实」。"
    )


def format_small_talk_output(
    reply: str,
    *,
    character: str = "amiya",
    speech_blocked: bool = False,
) -> str:
    """对闲聊模式的输出做去换行、去句末句号与 5~20 字收敛。"""
    char = (character or "amiya").strip().lower()
    fallback_map = {
        "amiya": "博士，我在听呢",
        "dania": "哼，听到了啦",
        "kaltsit": "我在",
    }
    s = normalize_qq_linebreaks(reply)
    if not s:
        return fallback_map.get(char, "我在呢")
    if speech_blocked:
        return s[:16]
    # 取第一行有效文字
    first_line = s.splitlines()[0].strip()
    # 剔除括号动作
    first_line = re.sub(r"[（(【\[][^（）()【】\[\]]*[）)】\]]", "", first_line).strip()
    # 若过长，优先取 5~20 字完整分句
    if len(first_line) > SMALL_TALK_MAX_CHARS:
        parts = re.split(r"[。！？!?；;，,\n]", first_line)
        cand = next((p.strip() for p in parts if SMALL_TALK_MIN_CHARS <= len(p.strip()) <= SMALL_TALK_MAX_CHARS), "")
        first_line = cand if cand else first_line[:20]
    # 去掉闲聊短句末尾多余的句号或逗号
    first_line = first_line.rstrip("。，,、；; ")
    return first_line or fallback_map.get(char, "我在呢")
