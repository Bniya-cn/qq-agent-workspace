"""离线群聊记录清洗与人设提取（Task5）。

契约：
- 由 POST /api/extract/group 调用 run_log_extraction(group_id, raw_text)
- 落盘 data/groups/{group_id}.json
- JSON 字段：system_prompt + few_shot_examples（3 组 user/assistant）
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

GROUPS_DIR = BOT_ROOT / "data" / "groups"


def _deepseek_url_and_model() -> tuple[str, str]:
    ensure_bot_env()
    base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip()
    return url, model

PERSONA_PROMPT = """你是一个网络社区亚文化分析师。以下是一份真实 QQ 群的脱敏聊天记录。

请深入分析并输出该群特有的“数字人格”，严格输出为 JSON 格式（不要 Markdown 代码块）：

{
  "system_prompt": "以该群群友口吻写成的系统人设提示词（包含黑话、常用助词、语气倾向）",
  "few_shot_examples": [
    {"role": "user", "content": "群内高频典型提问"},
    {"role": "assistant", "content": "符合该群特色的回答"},
    {"role": "user", "content": "第二组典型提问"},
    {"role": "assistant", "content": "第二组符合该群特色的回答"},
    {"role": "user", "content": "第三组典型提问"},
    {"role": "assistant", "content": "第三组符合该群特色的回答"}
  ]
}

要求：
1. few_shot_examples 必须正好 3 组（6 条，user/assistant 交替）
2. system_prompt 用中文，能直接注入对话系统提示
3. 只输出 JSON 对象本身

【聊天记录】：

{{CLEANED_LOG}}
"""

_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")
_SKIP_MARKERS = ("撤回了一条消息", "加入了群聊", "邀请")


def clean_chat_log(raw_log: str) -> str:
    """去除 CQ 码与系统通知，保留最近 2000 行有效对话。"""
    cleaned: list[str] = []
    for line in (raw_log or "").splitlines():
        line = _CQ_RE.sub("", line)
        if any(skip in line for skip in _SKIP_MARKERS):
            continue
        line = line.strip()
        if len(line) > 1:
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
    system_prompt = data.get("system_prompt")
    examples = data.get("few_shot_examples")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("system_prompt missing")
    if not isinstance(examples, list) or len(examples) < 2:
        raise ValueError("few_shot_examples missing")
    # 规范为最多 6 条（3 组）
    normalized: list[dict[str, str]] = []
    for item in examples[:6]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "")).strip()
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            normalized.append({"role": role, "content": content})
    if len(normalized) < 2:
        raise ValueError("few_shot_examples invalid")
    return {"system_prompt": system_prompt.strip(), "few_shot_examples": normalized}


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
        "temperature": 0.4,
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
    text = json.dumps(persona, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


async def run_log_extraction(group_id: str, raw_text: str) -> dict[str, Any]:
    """清洗 → DeepSeek 提炼 → 写入 data/groups/{group_id}.json。"""
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
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"log_extractor failed for group={gid}")
        return {
            "group_id": gid,
            "status": "error",
            "message": str(exc),
            "lines": line_count,
        }

    logger.info(f"log_extractor: wrote {path}")
    return {
        "group_id": gid,
        "status": "ok",
        "path": str(path),
        "lines": line_count,
        "system_prompt_preview": persona["system_prompt"][:120],
        "few_shot_count": len(persona["few_shot_examples"]),
    }
