"""启动时加载共享导演的提示词与配置；机器人使用可取消的异步 HTTP 调用。"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx

from .errors import TTSGenerationError

# gpt-sovits 不是 Python 包。只在导入时加载这一明确依赖，不重载、不修补运行中的插件。
_path = Path(__file__).resolve().parents[4] / "gpt-sovits" / "voice_director.py"
_spec = importlib.util.spec_from_file_location("qq_voice_director_shared", _path)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"无法加载声音导演: {_path}")
_director = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_director)


async def select_delivery(text: str, style: str, role: str, candidates: list[dict]) -> dict:
    cfg = _director._load_env_config()
    query = f"角色：{role}\n台词：{text}\n风格：{style}\n参考音频：{candidates}"
    providers = []

    ds_key = (cfg.get("DEEPSEEK_API_KEY") or "").strip()
    ds_base = (cfg.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    ds_model = (cfg.get("DEEPSEEK_VOICE_DIRECTOR_MODEL") or cfg.get("DEEPSEEK_MODEL") or "deepseek-flash").strip()
    if ds_key:
        model_list = [ds_model]
        if ds_model != "deepseek-chat":
            model_list.append("deepseek-chat")
        for m_name in model_list:
            payload = {
                "model": m_name,
                "messages": [
                    {"role": "system", "content": _director.DIRECTOR_SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 350,
                "temperature": 0.3,
            }
            if "deepseek" in m_name.lower() and "flash" in m_name.lower():
                payload["thinking"] = {"type": "disabled"}
            providers.append((ds_base + "/chat/completions", ds_key, payload, False))

    if cfg.get("DASHSCOPE_API_KEY") and cfg.get("DASHSCOPE_APP_ID_VOICE_DIRECTOR"):
        providers.append((
            f"https://dashscope.aliyuncs.com/api/v1/apps/{cfg['DASHSCOPE_APP_ID_VOICE_DIRECTOR']}/completion",
            cfg["DASHSCOPE_API_KEY"],
            {"input": {"prompt": _director.DIRECTOR_SYSTEM_PROMPT + query}, "parameters": {"has_thoughts": False}},
            True,
        ))

    if cfg.get("DASHSCOPE_API_KEY"):
        q_model = (cfg.get("DASHSCOPE_MODEL") or "qwen-plus").strip()
        providers.append((
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            cfg["DASHSCOPE_API_KEY"],
            {
                "model": q_model,
                "messages": [
                    {"role": "system", "content": _director.DIRECTOR_SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 350,
                "temperature": 0.3,
            },
            False,
        ))

    valid_names = {r["name"] for r in candidates if isinstance(r, dict) and r.get("name")}
    try:
        async with asyncio.timeout(6.5):
            async with httpx.AsyncClient(trust_env=False, timeout=4.0) as client:
                for url, key, payload, app in providers:
                    try:
                        response = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
                        response.raise_for_status()
                        data = response.json()
                        content = data.get("output", {}).get("text", "") if app else data["choices"][0]["message"]["content"]
                        selected = _director._clean_json_output(content)
                        if isinstance(selected, dict) and selected.get("ref_name") in valid_names:
                            return {**selected, "decision_source": "ai_bailian" if app else "ai:" + str(payload["model"])}
                    except (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError):
                        continue
    except TimeoutError:
        pass

    if candidates and valid_names:
        fallback_ref = next((r["name"] for r in candidates if r.get("name") == "default"), candidates[0]["name"])
        return {
            "ref_name": fallback_ref,
            "speed": 1.0,
            "temperature": 0.78,
            "top_p": 0.85,
            "pause": 0.2,
            "decision_source": "ai:fallback_director",
            "reasoning": "云端导演接口响应超时，使用角色基准情绪参考保障语音合成",
        }
    raise TTSGenerationError("AI 声音导演未返回有效情绪选择，改用文字。")
