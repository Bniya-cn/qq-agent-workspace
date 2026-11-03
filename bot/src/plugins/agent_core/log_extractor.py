"""离线群聊记录清洗、群风格参考与场景表达一次性后台提炼（Task5 + MaiBot 融合）。

契约与人格保护铁律：
- 由 POST /api/extract/group 或后台训练任务调用 run_log_extraction(group_id, raw_text)
- 落盘 data/groups/{group_id}.json（包含 system_prompt + few_shot_examples）
- 同时在同一次模型调用中提炼 MaiBot 结构的 `scene_expressions` 并写入 `expression_store`
- 【固定人格优先】：提炼出的 system_prompt 仅描述本群的语言风格、节奏与常用表达，严格保留阿米娅自身身份与温柔底色，绝不生成覆盖阿米娅身份的指令（如“你是奶蛙/大肥鱼”）。
- 严格过滤机器人自身的历史发言，防止把旧坏口吻回灌成样本。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger

from .config_manager import BOT_ROOT, ensure_bot_env
from .expression_learner import expression_store, is_persona_compatible_style

GROUPS_DIR = BOT_ROOT / "data" / "groups"


def _deepseek_url_and_model() -> tuple[str, str]:
    ensure_bot_env()
    base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()
    if model == "deepseek-flash" and "api.deepseek.com" in url:
        model = "deepseek-chat"
    return url, model


PERSONA_PROMPT = """你是一个社群语言风格与场景表达分析师。以下是一份真实 QQ 群的脱敏聊天记录。

我们的机器人固定角色是《明日方舟》的卡特斯少女「阿米娅」（温柔真诚、有烟火气、懂幽默）。
请在**不改变阿米娅固定角色身份与温柔底色**的前提下，一次性完成两项提炼，并严格输出为 JSON 对象（不要 Markdown 代码块）：
1. `system_prompt`：总结该群的日常聊天氛围、句长节奏、温和有趣的口头禅，供阿米娅在群聊中作为“表达风格参考”（注意：必须以阿米娅身份为前提，严禁把她定义成其他机器人或粗俗喷子！）。
2. `few_shot_examples`：3 组（6 条，user/assistant 交替）既有该群自然接话感、又符合阿米娅温和少女口吻的典型对话。
3. `scene_expressions`：最多 8 条 MaiBot 结构的“场景—表达方式”（situation -> style），供阿米娅在特定场景下借用句式。

输出格式：
{
  "system_prompt": "你在本群与大家熟稔相处，保持阿米娅温柔真诚的少女本色，同时自然融入本群简短轻快的接话节奏……",
  "few_shot_examples": [
    {"role": "user", "content": "群内高频典型话题"},
    {"role": "assistant", "content": "既带群内轻快节奏又符合阿米娅身份的自然回答"},
    {"role": "user", "content": "第二组典型话题"},
    {"role": "assistant", "content": "第二组自然回答"},
    {"role": "user", "content": "第三组典型话题"},
    {"role": "assistant", "content": "第三组自然回答"}
  ],
  "scene_expressions": [
    {"situation": "群友吐槽工作或学习很累时", "style": "快去歇会儿别硬撑啦", "tone": "温和关心", "meme_intensity": 0.2}
  ]
}

【聊天记录】：

{{CLEANED_LOG}}
"""

_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")
_SKIP_MARKERS = ("撤回了一条消息", "加入了群聊", "邀请", "本鱼", "奶蛙", "处理请求超时", "稍后重试")
_BOT_LINE_PREFIX_RE = re.compile(r"^(?:阿米娅|Amiya|大肥鱼|奶蛙|bot|Bot)[\s:：]", re.IGNORECASE)


def clean_chat_log(raw_log: str) -> str:
    """去除 CQ 码、系统通知以及机器人自身旧回复，保留最近 2000 行真实群友有效对话。"""
    cleaned: list[str] = []
    for line in (raw_log or "").splitlines():
        line = _CQ_RE.sub("", line).strip()
        if not line or len(line) <= 1:
            continue
        if any(skip in line for skip in _SKIP_MARKERS):
            continue
        if _BOT_LINE_PREFIX_RE.search(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned[-2000:])


def _safe_group_id(group_id: str) -> str:
    gid = (group_id or "").strip()
    if not gid or "/" in gid or "\\" in gid or ".." in gid:
        raise ValueError("invalid group_id")
    return gid


def _parse_persona_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("persona JSON must be an object")
    system_prompt = str(data.get("system_prompt") or "").strip()
    examples = data.get("few_shot_examples")
    if not system_prompt:
        raise ValueError("system_prompt missing")
    # 防止提炼出的提示词覆盖阿米娅身份
    for forbidden in ("大肥鱼", "奶蛙", "本鱼"):
        system_prompt = system_prompt.replace(forbidden, "阿米娅")
    if not isinstance(examples, list) or len(examples) < 2:
        raise ValueError("few_shot_examples missing")
    normalized: list[dict[str, str]] = []
    for item in examples[:6]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content and is_persona_compatible_style(content[:25]):
            normalized.append({"role": role, "content": content})
    if len(normalized) < 2:
        raise ValueError("few_shot_examples invalid")
    scene_exprs = data.get("scene_expressions")
    if not isinstance(scene_exprs, list):
        scene_exprs = []
    return {
        "system_prompt": system_prompt,
        "few_shot_examples": normalized,
        "scene_expressions": scene_exprs,
    }


async def _call_deepseek(cleaned_log: str) -> dict[str, Any]:
    ensure_bot_env()
    ds_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    dash_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    if ds_key:
        api_key = ds_key
        target_url, target_model = _deepseek_url_and_model()
    elif dash_key:
        api_key = dash_key
        target_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        target_model = (os.getenv("DASHSCOPE_MODEL") or "qwen-plus").strip()
    else:
        raise RuntimeError(
            "缺少 DEEPSEEK_API_KEY 或 DASHSCOPE_API_KEY：请在 bot/.env 中配置后重试"
        )
    prompt = PERSONA_PROMPT.replace("{{CLEANED_LOG}}", cleaned_log)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": target_model,
        "messages": [
            {"role": "system", "content": "你只输出合法 JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        "response_format": {"type": "json_object"},
    }
    if "deepseek" in target_model.lower() and "flash" in target_model.lower():
        payload["thinking"] = {"type": "disabled"}
    async with httpx.AsyncClient(trust_env=False, timeout=120.0) as client:
        resp = await client.post(target_url, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()
    message = body["choices"][0]["message"]
    content = message.get("content") or message.get("reasoning_content") or ""
    if not str(content).strip():
        raise RuntimeError("模型返回空 content")
    return _parse_persona_json(str(content))


def _write_group_json(group_id: str, persona: dict[str, Any]) -> Path:
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    path = GROUPS_DIR / f"{group_id}.json"
    out_obj = {
        "system_prompt": persona["system_prompt"],
        "few_shot_examples": persona["few_shot_examples"],
    }
    text = json.dumps(out_obj, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


async def run_log_extraction(group_id: str, raw_text: str) -> dict[str, Any]:
    """清洗 → 单次云端模型同时提炼群表达参考与 MaiBot 场景表达 → 落盘并更新缓存。"""
    try:
        gid = _safe_group_id(group_id)
    except ValueError as exc:
        return {"group_id": group_id, "status": "error", "message": str(exc)}

    cleaned = clean_chat_log(raw_text)
    if not cleaned:
        return {
            "group_id": gid,
            "status": "empty",
            "message": "清洗后无有效对话行",
            "lines": 0,
        }

    line_count = cleaned.count("\n") + 1
    logger.info(f"log_extractor: group={gid} cleaned_lines={line_count}")

    try:
        persona = await _call_deepseek(cleaned)
        path = _write_group_json(gid, persona)
        scene_exprs = persona.get("scene_expressions") or []
        learned_expr_cnt = 0
        if isinstance(scene_exprs, list) and scene_exprs:
            learned_expr_cnt = expression_store.upsert_expressions(gid, scene_exprs, character="amiya")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"log_extractor failed for group={gid}")
        return {
            "group_id": gid,
            "status": "error",
            "message": str(exc),
            "lines": line_count,
        }

    logger.info(f"log_extractor: wrote {path} (scene_expressions={learned_expr_cnt})")
    return {
        "group_id": gid,
        "status": "ok",
        "path": str(path),
        "lines": line_count,
        "system_prompt_preview": persona["system_prompt"][:120],
        "few_shot_count": len(persona["few_shot_examples"]),
        "learned_expressions": learned_expr_cnt,
    }
