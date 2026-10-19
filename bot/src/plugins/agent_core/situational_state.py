"""按 QQ 账号独立持久化的持续性身体/处境状态分辨器（封嘴鼻音哼唧、负重喘息、运动计数与解除判定）。"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx
from nonebot import logger

from . import conversation_service as conversation
from .audio_dsp import is_muffled_gagged_text, to_nasal_whimper_text

_SITUATIONAL_STATE_CUES = (
    "嘴", "胶带", "胶布", "封", "堵", "塞", "绑", "捆", "止咬", "哑", "禁言", "不准说", "不能说", "别说话", "闭嘴",
    "唔", "呜呜", "沙子", "背着", "扛着", "抱着", "顶着", "举着", "提着", "挂着", "绑着", "戴着", "穿着",
    "重物", "箱子", "石头", "负重", "惩罚", "体罚", "俯卧撑", "深蹲", "跑步", "跑圈", "平板支撑", "蛙跳", "仰卧起坐",
    "蹲", "跪", "趴", "站着", "跑", "累", "喘",
    "取下", "撕下", "撕掉", "拿掉", "拿开", "解开", "放下", "松开", "脱下", "摘下", "恢复", "停下", "休息",
    "你现在", "给你", "让你", "罚你", "命令你",
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
    if conversation.check_and_fast_release_state(clean_user):
        logger.info(f"持续状态已被用户指令快速解除 [uid={session.uid}]: '{clean_user[:30]}'")
        return draft_reply

    active_state = conversation.get_active_state()
    recent_turns = session.history[-8:] if session.history else []
    combined_scan = clean_user + " " + " ".join(str(t.get("user") or "") for t in recent_turns)

    if not active_state and not any(k in combined_scan for k in _SITUATIONAL_STATE_CUES):
        return draft_reply

    api_key, base_url, model = resolve_credentials_fn()
    if not api_key:
        if active_state and active_state.get("speech_blocked"):
            return to_nasal_whimper_text(draft_reply, character)
        return draft_reply

    role_label = {
        "amiya": "阿米娅（自称阿米娅，称呼对方为博士）",
        "kaltsit": "凯尔希（称呼对方为博士）",
        "dania": "达妮娅（傲娇嘴硬，称呼对方为漂泊者或你）",
    }.get(character, character)
    hist_lines = [f"- 用户曾说：{t.get('user', '')}\n  角色回复：{t.get('assistant', '')}" for t in recent_turns]
    hist_block = "\n".join(hist_lines) if hist_lines else "（无历史记录）"
    prev_state_str = json.dumps(active_state, ensure_ascii=False) if active_state else "无"

    judge_system = (
        "你是一个角色扮演持续状态与物理常理裁判器。请根据【该QQ用户的近期对话历史】、【此前已记录的持续状态】和【用户本轮最新消息】，"
        "按真实物理逻辑与实际情况进行分辨，判断角色当前是否处于尚未解除的身体限制、封嘴或负重/运动/特殊处境中，并检查【角色初步回复草稿】是否符合该状态。\n"
        "判断准则：\n"
        "1. 状态持续与解除：若用户在本轮或历史中对角色设定了持续性身体限制或处境（例如：“你现在嘴巴被封起来了”“贴上胶布”“给你加了一箱沙子你背着”“去做俯卧撑”等），"
        "在该用户明确下达解除指令（如“取下胶带”“撕掉”“把沙子放下”“停下”“休息吧”“恢复正常”）之前，无论用户当前切换到什么新话题（如“我们聊聊考公吧”），该状态都必须一直生效（has_active_state=true）！\n"
        "   一旦用户说“停/休息/放下/不用做了/撕掉胶带/恢复”，has_active_state 立即变为 false。\n"
        "2. 物理封嘴/贴胶布状态 (speech_blocked=true)：若当前持续状态导致角色物理上无法张嘴吐字（如嘴巴被胶布/胶带封住、被堵住且未取下），speech_blocked 必须为 true。\n"
        "   此时角色完全说不出任何词，严禁使用汉字「嗯」（底层会被转成“恩”导致像清嗓子）和「呼/哈/啊/哼」或省略号「……」；\n"
        "   corrected_reply 必须重写为 5～7 个字的短促、带慌乱与抗议情绪起伏的纯「唔/呜」交替短句（例如：\"唔呜！唔呜唔？呜唔！\"）。\n"
        "3. 持续负重/体罚/运动消耗状态 (has_active_state=true, speech_blocked=false)：例如“背着一箱沙子”“正在做俯卧撑/跑步”且尚未被允许放下或停止。\n"
        "   此时即使当前用户在聊其他话题（如考公、日常问答），角色的回复也必须一边体现真实的吃力感与呼吸节奏，一边自然提及背上的沙子/正在进行的动作，同时以角色口吻回应当前话题（严禁使用括号动作描写，控制在 30 字以内）。\n"
        "请仅输出纯 JSON 对象，不要任何 Markdown 代码块或多余文字：\n"
        "{\n"
        '  "has_active_state": true,\n'
        '  "speech_blocked": false,\n'
        '  "state_description": "简短描述当前持续状态（若已解除或无则为空字符串）",\n'
        '  "release_condition": "解除条件（如：用户取下胶带 / 用户让放下沙子或休息）",\n'
        '  "reply_violates_state": true,\n'
        '  "corrected_reply": "修正后的回复文本（若未违背则为空字符串）"\n'
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
            "max_tokens": 300,
            "temperature": 0.2,
        }
        if "deepseek" in eff_model.lower() and "flash" in eff_model.lower() and "api.deepseek.com" not in base_url.lower():
            payload["thinking"] = {"type": "disabled"}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(trust_env=False, timeout=8.0) as client:
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
                        logger.info(f"持续状态修正回复 [uid={session.uid}]: '{draft_reply[:25]}' -> '{final_reply[:35]}'")
                    if blocked:
                        final_reply = to_nasal_whimper_text(final_reply if is_muffled_gagged_text(final_reply) else corrected, character)
                    return final_reply
    except Exception as exc:
        logger.warning(f"持续状态分辨器跳过: {exc}")

    st_now = conversation.get_active_state()
    if st_now and st_now.get("speech_blocked"):
        return to_nasal_whimper_text(draft_reply, character)
    return draft_reply
