"""按 QQ 账号独立持久化的持续性身体/处境状态分辨器（封嘴鼻音哼唧、负重喘息、运动计数与解除判定）。"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx
from nonebot import logger

from . import conversation_service as conversation
from .audio_dsp import is_muffled_gagged_text, to_nasal_whimper_text

# 保留向后兼容导出符号，供 llm_service 引用
_SITUATIONAL_STATE_CUES = (
    "嘴巴被封", "嘴被封", "封住嘴", "贴上胶带", "贴胶带", "胶带封", "堵住嘴", "塞住嘴",
    "捂住嘴", "不能说话", "不准说话", "说不出话", "呜呜", "唔唔",
    "背着", "扛着", "一箱沙子", "沙袋", "负重", "俯卧撑", "深蹲", "跑圈", "体罚",
    "取下胶带", "撕下胶带", "撕掉胶带", "拿开", "放下沙子", "把沙子放下", "不用背了",
)

# 仅在当前消息明确包含施加身体/处境限制的具体词条时才触发新状态判定（避免“给你”“累”“你现在”等日常词在未来8轮反复触发慢速裁判）
_NEW_STATE_TRIGGER_RE = re.compile(
    r"(?:嘴巴|嘴|口).{0,6}(?:被封|封起|封住|贴上|粘住|堵住|塞住|捂住|绑住)"
    r"|(?:贴.{0,4}胶带|贴.{0,4}胶布|封住.{0,4}嘴|堵住.{0,4}嘴|塞住.{0,4}嘴|止咬器|不准说话|不能说话|别说话|闭嘴)"
    r"|(?:背着|扛着|抱着|顶着|举着|提着|绑着).{0,8}(?:沙子|重物|箱子|石头|沙袋|负重)"
    r"|(?:一箱|一袋|十斤|百斤).{0,6}(?:沙子|石头|重物|砖头)"
    r"|(?:罚你|命令你|让你去做|去做|开始做).{0,6}(?:俯卧撑|深蹲|跑圈|平板支撑|蛙跳|仰卧起坐|体罚)",
    re.IGNORECASE,
)


async def resolve_and_enforce_situational_state(
    character: str,
    raw_user_text: str,
    draft_reply: str,
    *,
    resolve_credentials_fn: Callable[[], tuple[Optional[str], str, str]],
    voice_mode_only: bool = False,
) -> str:
    """根据当前 QQ 用户的历史与最新指令，智能分辨持续性身体/处境设定并修正输出。"""
    session = conversation.current()
    if session is None or not conversation._include_history.get():
        return draft_reply

    clean_user = re.sub(r"【当前处于纯语音回复模式[\s\S]*$", "", (raw_user_text or "")).strip()
    if "【用户针对上述内容的提问/指令】：" in clean_user:
        clean_user = clean_user.split("【用户针对上述内容的提问/指令】：", 1)[1].strip()

    if conversation.check_and_fast_release_state(clean_user):
        logger.info(f"持续状态已被用户指令快速解除 [uid={session.uid}]: '{clean_user[:30]}'")
        return draft_reply

    active_state = conversation.get_active_state()
    # 关键性能优化：若当前没有激活的持续状态，仅检查本轮用户消息是否包含明确的状态施加指令（绝不扫描历史8轮普通聊天）
    if not active_state and not _NEW_STATE_TRIGGER_RE.search(clean_user):
        return draft_reply

    # 若已处于封嘴状态且本轮未解除，直接本地0ms转换为唔呜鼻音，无需再调一次大模型浪费3~5秒
    if active_state and active_state.get("speech_blocked") and not _NEW_STATE_TRIGGER_RE.search(clean_user):
        return to_nasal_whimper_text(draft_reply, character)

    api_key, base_url, model = resolve_credentials_fn()
    if not api_key:
        if active_state and active_state.get("speech_blocked"):
            return to_nasal_whimper_text(draft_reply, character)
        return draft_reply

    recent_turns = session.history[-4:] if session.history else []
    role_label = {
        "amiya": "阿米娅（自称阿米娅，称呼对方为博士）",
        "kaltsit": "凯尔希（称呼对方为博士）",
        "dania": "达妮娅（傲娇嘴硬，称呼对方为漂泊者或你）",
    }.get(character, character)
    hist_lines = [f"- 用户曾说：{t.get('user', '')}\n  角色回复：{t.get('assistant', '')}" for t in recent_turns]
    hist_block = "\n".join(hist_lines) if hist_lines else "（无历史记录）"
    prev_state_str = json.dumps(active_state, ensure_ascii=False) if active_state else "无"

    judge_system = (
        "你是一个角色扮演持续状态与物理常理裁判器。请根据【此前已记录的持续状态】和【用户本轮最新消息】，"
        "按真实物理逻辑判断角色当前是否处于尚未解除的身体限制、封嘴或负重/运动处境中，并检查【角色初步回复草稿】是否符合该状态。\n"
        "判断准则：\n"
        "1. 若用户设定了持续性身体限制（如“你现在嘴巴被封起来了”“给你加了一箱沙子你背着”“去做俯卧撑”），"
        "在明确下达解除指令（如“取下胶带”“把沙子放下”“停下”“休息吧”）之前，无论切换到什么话题，has_active_state 必须为 true。\n"
        "2. 物理封嘴状态 (speech_blocked=true)：corrected_reply 必须重写为 5～7 个字的纯「唔/呜」交替短句（如：\"唔呜！唔呜唔？呜唔！\"）。\n"
        "3. 持续负重/运动状态 (has_active_state=true, speech_blocked=false)：角色的回复必须一边体现吃力感与喘息，一边自然提及背上的沙子/动作，同时回应当前话题（严禁括号动作描写，控制在 28 字以内）。\n"
        "请仅输出纯 JSON 对象：\n"
        "{\n"
        '  "has_active_state": true,\n'
        '  "speech_blocked": false,\n'
        '  "state_description": "简短描述当前持续状态",\n'
        '  "release_condition": "解除条件",\n'
        '  "reply_violates_state": true,\n'
        '  "corrected_reply": "修正后的回复文本"\n'
        "}"
    )
    judge_user = (
        f"【角色设定】：{role_label}\n"
        f"【此前已记录的持续状态】：{prev_state_str}\n"
        f"【近期对话历史】：\n{hist_block}\n"
        f"【用户本轮最新消息】：{clean_user}\n"
        f"【角色初步回复草稿】：{draft_reply}"
    )

    try:
        url = f"{base_url.rstrip('/')}/chat/completions"
        eff_model = "deepseek-chat" if ("api.deepseek.com" in base_url.lower() and model.lower() == "deepseek-flash") else model
        payload: dict[str, Any] = {
            "model": eff_model,
            "messages": [
                {"role": "system", "content": judge_system},
                {"role": "user", "content": judge_user},
            ],
            "max_tokens": 220,
            "temperature": 0.2,
        }
        if "deepseek" in eff_model.lower() and "flash" in eff_model.lower() and "api.deepseek.com" not in base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(trust_env=False, timeout=4.5) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                raw_content = (resp.json().get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
                m = re.search(r"\{.*\}", raw_content, re.DOTALL)
                if m:
                    res = json.loads(m.group(0))
                    has_st = bool(res.get("has_active_state"))
                    blocked = bool(res.get("speech_blocked")) if has_st else False
                    if has_st:
                        new_state = {
                            "has_active_state": True,
                            "speech_blocked": blocked,
                            "state_description": str(res.get("state_description") or "").strip(),
                            "release_condition": str(res.get("release_condition") or "").strip(),
                        }
                        conversation.set_active_state(new_state)
                        logger.info(f"持续状态追踪 [uid={session.uid}]: {new_state}")
                    else:
                        if active_state:
                            logger.info(f"持续状态已解除 [uid={session.uid}]")
                        conversation.set_active_state(None)

                    final_reply = draft_reply
                    corrected = str(res.get("corrected_reply") or "").strip()
                    if res.get("reply_violates_state") and corrected:
                        final_reply = corrected
                    if blocked:
                        final_reply = to_nasal_whimper_text(final_reply if is_muffled_gagged_text(final_reply) else corrected, character)
                    return final_reply
    except Exception as exc:
        logger.warning(f"持续状态分辨器跳过: {exc}")

    st_now = conversation.get_active_state()
    if st_now and st_now.get("speech_blocked"):
        return to_nasal_whimper_text(draft_reply, character)
    return draft_reply
