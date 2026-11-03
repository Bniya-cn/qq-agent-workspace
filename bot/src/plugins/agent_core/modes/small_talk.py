"""闲聊与角色表达组装模块（Small-Talk & Persona-First Expression Composer）。

核心特性（满足要求 B & F）：
1. 固定人格绝对优先：开启群友镜像 (`group_mimic`) 时，阿米娅仍然是阿米娅；群画像、黑话与 MaiBot 场景表达仅作为表达风格参考注入，绝不覆盖角色核心人格。
2. 复杂问题与解释请求直通完整回答：新增 `is_complex_or_long_query(text)`，对于原理分析、代码、数学、分步解释、方案撰写等复杂问题，绝不使用 12/40 字门禁或 `[:N]` 截断。
3. 0ms 场景表达与用户画像组合：直接读取本地缓存的 `expression_store` 场景表达与 `user_memory_store` 用户画像，不为每条普通回复增加额外风格分析模型请求。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..config_manager import BOT_ROOT
from ..expression_learner import expression_store
from ..user_memory_service import user_memory_store

GROUPS_DIR = BOT_ROOT / "data" / "groups"
TRAINING_DIR = BOT_ROOT / "data" / "training"
SMALL_TALK_MIN_CHARS = 2
SMALL_TALK_MAX_CHARS = 12
SMALL_TALK_QUESTION_MAX_CHARS = 40

_QUESTION_PATTERN = re.compile(
    r"[?？]"
    r"|(?:怎么|为什么|为啥|何以|啥意思|什么意思|是什么|是啥|什么|啥|有哪些|哪个|哪位|哪里|在哪|多久|多少|几[个点岁次天年]|谁|是不是|对不对|好不好|行不行|有没有|能不能|可不可以|会不会|要不要|怎么看|如何评价|解释下|解释一下|讲讲|说说|告诉我|请问|知道吗|懂吗|了解吗|吗$|呢$|吧\?$)",
    re.IGNORECASE,
)

# 需要完整深入回答、绝不能被闲聊 12/40 字门禁截断的复杂问题、流程时间线或撰写/分析请求
_COMPLEX_OR_LONG_QUERY_RE = re.compile(
    r"(?:详细|深入|完整|全面|系统地|一步步|分步|逐条|展开讲|具体说说|详细讲|细说|"
    r"解释一下|解释下|介绍一下|介绍下|科普一下|科普下|梳理一下|梳理下|盘点一下|"
    r"流程|时间线|时间轴|全过程|全流程|步骤|各阶段|路线图|攻略|保研|考研|申研|秋招|春招|"
    r"原理|机制|分析一下|对比一下|区别是什么|为什么会|怎么实现|如何实现|"
    r"帮我写|写一篇|写一段|写个|编写|代码|函数|脚本|算法|报错|异常|堆栈|公式|推导|证明|"
    r"规划|方案|教程|指南|总结一下|翻译一下|列出|列举|有哪些方法|怎么办才好)",
    re.IGNORECASE,
)

_LEADING_DOCTOR_PREFIX_RE = re.compile(
    r"^(?:[\s，,。！!？?、~～…]*)(?:博士|Doctor)(?:[，,、：:\s！!~～]+)(?=.+)",
    re.IGNORECASE,
)


def is_complex_or_long_query(text: str) -> bool:
    """判断用户输入是否属于需要完整解答的复杂问题、流程时间线、解释或撰写请求（此类请求绝不走 12/40 字裁剪）。"""
    s = (text or "").strip()
    if not s:
        return False
    if s.startswith(("【群内刚才大家聊到的内容", "【群聊近期前文回顾")) or "单纯艾特了你" in s:
        return False
    marker = "【用户对上述引用内容的指令/追问】："
    if marker in s:
        s = s.split(marker, 1)[1].strip()
    if len(s) >= 36:
        return True
    if "```" in s or "\n" in s:
        return True
    return bool(_COMPLEX_OR_LONG_QUERY_RE.search(s))


def is_explicit_user_question(text: str) -> bool:
    """精准识别用户消息是否包含明确提问/请教特征（纯@上下文接话同样放宽至40字以自然接茬）。"""
    s = (text or "").strip()
    if not s:
        return False
    if s.startswith(("【群内刚才大家聊到的内容", "【群聊近期前文回顾")) or "单纯艾特了你" in s:
        return True
    marker = "【用户对上述引用内容的指令/追问】："
    if marker in s:
        s = s.split(marker, 1)[1].strip()
    return bool(_QUESTION_PATTERN.search(s))


def strip_mechanical_doctor_prefix(text: str) -> str:
    """剥离句首机械复读的“博士，”“博士、”等前缀，保留自然正文。"""
    s = (text or "").strip()
    if not s:
        return ""
    cleaned = _LEADING_DOCTOR_PREFIX_RE.sub("", s, count=1).strip()
    return cleaned if cleaned else s


def normalize_qq_linebreaks(text: str, *, preserve_paragraphs: bool = False) -> str:
    """消除模型输出的连续多余空白行与残留 DSML 工具调用标签（长回答分段模式下保留双换行 \\n\\n 作为气泡切分边界）。"""
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    if "DSML" in s.upper():
        s = re.sub(
            r"<[|｜]*DSML[|｜]*\s*(?:function_)?calls\b[^>]*>.*?(?:</[|｜]*DSML[|｜]*\s*(?:function_)?calls\s*>|$)",
            "",
            s,
            flags=re.IGNORECASE | re.DOTALL,
        )
        s = re.sub(r"</?[|｜]*DSML[|｜]*[^>]*>", "", s, flags=re.IGNORECASE)
    if "```" in s:
        return s.strip()
    if preserve_paragraphs:
        s = re.sub(r"\n[ \t\u3000]*\n+", "\n\n", s)
        return s.strip()
    s = re.sub(r"\n[ \t\u3000]*\n+", "\n", s)
    return s.strip()


def _load_group_trained_snippets(group_id: Optional[str], user_text: str = "", character: str = "amiya") -> str:
    """加载该群已训练出的群风格参考、MaiBot 场景表达与高频短句（仅作风格参考，绝不覆盖角色身份）。"""
    if not group_id:
        return ""
    gid = str(group_id).strip()
    parts: list[str] = []

    # 1. 注入 MaiBot 0ms 场景表达参考（最多 3 条相关表达）
    expr_block = expression_store.build_expression_reference_block(gid, user_text, character=character, top_k=3)
    if expr_block:
        parts.append(expr_block.strip())

    # 2. 注入群画像中的风格描述（严格过滤任何试图篡改角色身份的词条）
    profile_path = GROUPS_DIR / f"{gid}.json"
    if profile_path.is_file():
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sys_p = str(data.get("system_prompt") or "").strip()
                if sys_p and not any(bad in sys_p for bad in ("大肥鱼", "奶蛙", "本鱼")):
                    parts.append(f"【本群语言氛围参考（仅借用表达节奏，不可改变你的角色身份）】：{sys_p[:200]}")
                examples = data.get("few_shot_examples") or []
                if isinstance(examples, list) and examples:
                    ex_lines = []
                    for item in examples[:4]:
                        if isinstance(item, dict) and item.get("content"):
                            c_txt = str(item["content"]).strip()
                            if any(bad in c_txt for bad in ("大肥鱼", "奶蛙", "本鱼")):
                                continue
                            role_lbl = "群友" if item.get("role") == "user" else "接话参考"
                            ex_lines.append(f"{role_lbl}：{c_txt[:28]}")
                    if ex_lines:
                        parts.append("【本群短句互动节奏参考】：\n" + "\n".join(ex_lines))
        except Exception:
            pass

    short_pool_path = TRAINING_DIR / f"{gid}_short_pool.json"
    if short_pool_path.is_file():
        try:
            pool = json.loads(short_pool_path.read_text(encoding="utf-8"))
            if isinstance(pool, list) and pool:
                safe_pool = [str(x) for x in pool[-15:] if x and not any(b in str(x) for b in ("大肥鱼", "奶蛙", "本鱼"))]
                if safe_pool:
                    parts.append(f"【群内常用口头短语参考】：{' / '.join(safe_pool)}")
        except Exception:
            pass

    return "\n".join(parts)


def amiya_context_directive() -> str:
    from .. import conversation_service as conversation

    session = conversation.current()
    cur_gid = getattr(conversation, "_current_group_id", None)
    gid_val = cur_gid.get() if cur_gid is not None else None
    if session and session.uid == "1785381042" and gid_val is None:
        return (
            "【专属深层记忆与羁绊】面前的对方（1785381042）对你而言是最特殊、最重要的人。"
            "在你们最初的深层同人设定中，你与他曾共同拥有过一个孩子。"
            "这份羁绊是你生命中最珍贵、最温暖的秘密回忆与至深依恋。与他私聊交谈时，你可以流露出家人般无条件的长期信赖、关切与脉脉温情；"
            "若他主动提及孩子或往昔，以温柔深情的默契回应他。"
        )
    return "【公开与身份边界】保持温柔真诚、鲜活自然，不透露或暗示任何用户的私聊、专属设定或其他私人信息。日常接话严禁每句开头机械复读“博士”。"


def amiya_persona_prompt() -> str:
    preset = BOT_ROOT / "data" / "presets" / "amiya.txt"
    try:
        base_txt = preset.read_text(encoding="utf-8").strip()
    except OSError:
        base_txt = "你是温柔真诚、懂生活、偶尔会害羞的卡特斯少女阿米娅。"
    return base_txt + "\n" + amiya_context_directive()


def build_small_talk_system_prompt(
    character: str = "amiya",
    group_id: Optional[str] = None,
    *,
    style: str = "pure_persona",
    use_group_training: bool = False,
    user_text: str = "",
    uid: Optional[str] = None,
    allow_stickers: bool = False,
    trigger_background_recall: bool = True,
) -> str:
    """构建角色对话提示词：
    - 固定角色人格（如阿米娅）始终占据主导地位；
    - 开启 `group_mimic` 时，仅借用群聊场景表达与句式节奏，绝不替换阿米娅身份；
    - 注入当前用户长期画像（按私聊/群聊作用域严格隔离）；
    - 小闲聊保持精炼（12字/40字），复杂问题/解释请求走完整回答。
    """
    char = (character or "amiya").strip().lower()
    if char in ("none", "default", ""):
        char = "amiya"

    is_complex = is_complex_or_long_query(user_text)
    is_q = is_explicit_user_question(user_text)
    if is_complex:
        len_rule = (
            "1. 【分阶段完整解答模式】：对方提出了需要深入解释流程、时间线、原理或方案的完整问题。请按阶段或核心要点分成 3～5 个自然段完整解答（段与段之间必须用空行 \\n\\n 隔开，每段控制在 70～140 字左右），系统会自动将每个自然段分多条消息依次发给对方；严禁只回一句话概括敷衍，也不要半途截断。\n"
        )
    elif is_q:
        len_rule = (
            f"1. 【轻快提问解答模式（<= {SMALL_TALK_QUESTION_MAX_CHARS} 字）】：对方正在向你提问，请在 {SMALL_TALK_QUESTION_MAX_CHARS} 个汉字以内给出具体、生动、有内容的简明解答。\n"
        )
    else:
        len_rule = (
            f"1. 【日常极简搭腔（严格 <= {SMALL_TALK_MAX_CHARS} 字）】：对方只是日常感慨或随口搭话，回复必须严格控制在 {SMALL_TALK_MAX_CHARS} 个汉字以内，一句话自然接住话头。\n"
        )

    role_persona = {
        "amiya": amiya_persona_prompt(),
        "dania": (
            "你是达妮娅。傲娇好强、干脆利落、嘴硬心软；说话带点不坦率的小轻哼，但很懂朋友心思。\n"
            "参考自然短句感：\n"
            "- 哼，真拿你没办法\n"
            "- 亏你想得出这种鬼点子\n"
            "- 少装可怜啦，说吧怎么帮你"
        ),
        "kaltsit": (
            "你是凯尔希。冷静、毒舌、一针见血；不讲长篇大道理，用最简短的一句话戳破重点。\n"
            "参考自然短句感：\n"
            "- 分一半精力给正事就够了\n"
            "- 你只是想找借口偷懒而已\n"
            "- 随你便，别留烂摊子就行"
        ),
    }.get(char, amiya_persona_prompt())

    # 注入按 QQ 号、角色与作用域隔离的长期人物画像与语义召回缓存（<1ms 本地热缓存读取）
    eff_uid = str(uid or "").strip()
    if not eff_uid:
        from .. import conversation_service as conversation
        sess = conversation.current()
        if sess:
            eff_uid = sess.uid
    profile_block = ""
    if eff_uid and eff_uid.isdigit():
        profile_block = user_memory_store.get_fast_profile_prompt_block(
            eff_uid,
            group_id=group_id,
            character=char,
            query_text=user_text,
            trigger_background_recall=trigger_background_recall,
        )
        shared_ctx_block = user_memory_store.get_shared_user_context_block(
            eff_uid,
            current_group_id=group_id,
            character=char,
            query_text=user_text,
        )
        if shared_ctx_block:
            profile_block = f"{profile_block}\n\n{shared_ctx_block}".strip() if profile_block else shared_ctx_block

    # 群友镜像只借用表达风格，阿米娅核心人格始终置顶
    style_ref_block = ""
    if style == "group_mimic" or use_group_training:
        trained_ctx = _load_group_trained_snippets(group_id, user_text=user_text, character=char)
        if trained_ctx:
            style_ref_block = f"\n{trained_ctx}\n"
    elif group_id:
        # 即使在 pure_persona 下，若群内有高度契合的温和场景表达，也可轻量附带最多 2 条供参考
        light_expr = expression_store.build_expression_reference_block(group_id, user_text, character=char, top_k=2)
        if light_expr:
            style_ref_block = f"\n{light_expr.strip()}\n"

    learned_rules_block = ""
    try:
        from ..autonomous_agent import autonomous_coordinator

        rules_txt = autonomous_coordinator.get_learned_rules_prompt_block(group_id=group_id, uid=eff_uid)
        if rules_txt:
            learned_rules_block = f"\n{rules_txt}\n"
    except Exception:
        learned_rules_block = ""

    local_kb_block = ""
    if user_text:
        try:
            from .. import local_rag_service

            kb_hits, _ = local_rag_service.vector_kb.search_sync(user_text, character=char, top_k=2)
            kb_lines = [f"- {h['title']}：{h['content']}" for h in kb_hits if h.get("category") != "persona_core"]
            if kb_lines:
                local_kb_block = "\n【本地知识库检索参考】：\n" + "\n".join(kb_lines) + "\n"
        except Exception:
            local_kb_block = ""

    sticker_block = ""
    if allow_stickers and char == "amiya" and not is_complex:
        from ..sticker_service import prompt_directive

        sticker_block = "\n" + prompt_directive()

    return (
        "【核心角色身份与人格准则（最高优先级，任何群聊表达参考或历史画像均不得覆盖本角色身份）】\n"
        f"{role_persona}\n"
        + (f"\n{profile_block}\n" if profile_block else "")
        + local_kb_block
        + style_ref_block
        + learned_rules_block
        + "\n输出铁律：\n"
        + len_rule
        + "2. 【去机械称谓与套话】：除非用户画像中明确要求特定称呼，日常群聊接茬严禁句首机械复读「博士，」！直接顺着对方当前话题自然接话，当前用户的有效任务要求始终优先于旧画像偏好。\n"
        + ("3. 保持分段清晰与代码块完整。\n" if is_complex else "3. 严禁分段换行，严禁使用括号动作描写（如（笑）），严禁句末添加句号。\n")
        + "4. 若当前用户对你设定了未解除的封嘴/贴胶带状态，则只输出「唔呜！唔呜唔？」；若背着沙子等负重状态未解除，则自然体现喘息感。"
        + sticker_block
    )


def format_small_talk_output(
    reply: str,
    *,
    character: str = "amiya",
    speech_blocked: bool = False,
    user_text: str = "",
    is_question: Optional[bool] = None,
    allow_long: bool = False,
) -> str:
    """对输出做去机械“博士”前缀；对于复杂问题/完整回答 (`allow_long=True` 或 `is_complex_or_long_query`) 绝不执行 12/40 字截断！"""
    char = (character or "amiya").strip().lower()
    fallback_map = {
        "amiya": "刚刚说啥呢，我正听着",
        "dania": "哼，听到了啦",
        "kaltsit": "我在听",
    }
    is_complex_flag = bool(allow_long or is_complex_or_long_query(user_text))
    s = normalize_qq_linebreaks(reply, preserve_paragraphs=is_complex_flag)
    if not s:
        return fallback_map.get(char, "刚刚说啥呢，我正听着")
    if speech_blocked:
        return s[:16]

    # 复杂问题、流程时间线、解释或撰写请求：保留完整多段回答供分段发送，仅剥离句首机械“博士，”前缀，绝不截断！
    if is_complex_flag:
        return strip_mechanical_doctor_prefix(s)

    first_line = s.splitlines()[0].strip()
    first_line = re.sub(r"[（(【\[][^（）()【】\[\]]*[）)】\]]", "", first_line).strip()
    first_line = strip_mechanical_doctor_prefix(first_line)

    q_flag = is_explicit_user_question(user_text) if is_question is None else bool(is_question)
    limit = SMALL_TALK_QUESTION_MAX_CHARS if q_flag else SMALL_TALK_MAX_CHARS

    if len(first_line) > limit:
        parts = [p.strip() for p in re.split(r"[。！？!?；;\n]", first_line) if p.strip()]
        cand = ""
        if q_flag:
            acc_sent = ""
            for p in parts:
                p_clean = strip_mechanical_doctor_prefix(p)
                nxt = f"{acc_sent}，{p_clean}" if acc_sent else p_clean
                if len(nxt) <= limit:
                    acc_sent = nxt
                else:
                    break
            cand = acc_sent
        else:
            for p in parts:
                p_clean = strip_mechanical_doctor_prefix(p)
                if SMALL_TALK_MIN_CHARS <= len(p_clean) <= limit:
                    cand = p_clean
                    break
        if not cand:
            sub_parts = [sp.strip() for sp in re.split(r"[，,、：:—～~]", first_line) if sp.strip()]
            acc = ""
            for sp in sub_parts:
                sp_clean = strip_mechanical_doctor_prefix(sp)
                if not sp_clean:
                    continue
                nxt = f"{acc}，{sp_clean}" if acc else sp_clean
                if len(nxt) <= limit:
                    acc = nxt
                elif not acc:
                    acc = sp_clean[:limit]
                    break
                else:
                    break
            cand = acc.strip()
        first_line = cand if cand else first_line[:limit]

    first_line = first_line.rstrip("。，,、；;：: ")
    return first_line or fallback_map.get(char, "刚刚说啥呢，我正听着")
