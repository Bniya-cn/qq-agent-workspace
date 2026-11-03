"""纯文本文件任务的文档正文生成服务（支持 DeepSeek / DashScope 双引擎自动容灾与断点续写）。"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
from nonebot import logger

from .config_manager import ensure_bot_env
from . import conversation_service as conversation
from .doc_service import is_placeholder_document
from .errors import DocumentGenerationError

MAX_INPUT_CHARS = 32000
MAX_BODY_CHARS = 60000
MAX_OUTPUT_TOKENS = 8192
TOTAL_TIMEOUT = 480
MAX_REQUESTS = 4
_slots = asyncio.Semaphore(2)


def _merge_partial(body: str, chunk: str) -> str:
    # 重连后模型可能重述上一段，只消除足够长的精确重叠，不猜测语义相同内容。
    for size in range(min(len(body), len(chunk), 2000), 19, -1):
        if body.endswith(chunk[:size]):
            return body + chunk[size:]
    return body + chunk


def _build_candidate_endpoints() -> list[tuple[str, str, str]]:
    """构建 (api_key, base_url, model) 候选列表：
    当 api.deepseek.com 配置了非标准模型名（如 deepseek-flash）时自动追加 deepseek-chat，并纳入百炼 qwen-plus 作为容灾后备。
    """
    ensure_bot_env()
    candidates: list[tuple[str, str, str]] = []
    ds_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    ds_base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    ds_model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()
    if ds_key:
        if "api.deepseek.com" in ds_base.lower() and ds_model.lower() == "deepseek-flash":
            candidates.append((ds_key, ds_base, "deepseek-chat"))
            candidates.append((ds_key, ds_base, ds_model))
        else:
            candidates.append((ds_key, ds_base, ds_model))
            if "api.deepseek.com" in ds_base.lower() and ds_model != "deepseek-chat":
                candidates.append((ds_key, ds_base, "deepseek-chat"))

    return candidates


async def generate(prompt: str, formats: list[str], *, resume: dict | None = None) -> str:
    candidates = _build_candidate_endpoints()
    if not candidates:
        raise DocumentGenerationError("未配置 DEEPSEEK_API_KEY，暂时无法生成文档正文。")
    if len(prompt) > MAX_INPUT_CHARS:
        raise DocumentGenerationError("这次文档要求超过 32000 字，请拆分任务后重试。")
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=30)
    except asyncio.TimeoutError:
        raise DocumentGenerationError("文档生成队列暂满，请稍后重试。")

    body = str((resume or {}).get("content") or "")
    session = conversation.current()
    active_model = candidates[0][2]

    def checkpoint(complete: bool = False):
        if session:
            conversation.store.checkpoint(
                session,
                {
                    "prompt": prompt,
                    "content": body,
                    "formats": formats,
                    "complete": complete,
                    "writer": active_model,
                    "stage": "body_ready" if complete else "draft",
                },
            )

    try:
        checkpoint()
        async with asyncio.timeout(TOTAL_TIMEOUT):
            last_err_msg = ""
            for attempt in range(MAX_REQUESTS):
                key, base, model = candidates[min(attempt, len(candidates) - 1)]
                active_model = model
                system = (
                    "你负责完成用户委托的专业教案或深度文档。请直接输出结构严谨、细节充实的完整 Markdown 正文，"
                    "包括清晰的各级标题、教学目标、重难点、具体教学过程/推导步骤、实验或互动设计、板书结构、表格及课后分层练习。"
                    "不要寒暄，不要声称生成了附件、完成了代码运行或上传文件，不要询问是否导出。"
                    "尽量在本次输出内写完；如果提供了未完成草稿，严格从中断处继续，不要重写前文。"
                    "LaTeX 公式请使用标准 $...$ 或 $$...$$ 定界符。"
                )
                messages = conversation.messages(system, prompt)
                if body:
                    messages += [
                        {"role": "assistant", "content": body},
                        {"role": "user", "content": "以上是已保存草稿，请从中断位置继续写到完成，不要重复已有正文。"},
                    ]
                if len(json.dumps(messages, ensure_ascii=False).encode()) > 300000:
                    raise DocumentGenerationError("上下文超过本次文档请求的预算，已保留草稿。请缩小要求后重试。")

                max_tok = MAX_OUTPUT_TOKENS if "deepseek" in model.lower() else 4096
                payload: dict = {"model": model, "messages": messages, "max_tokens": max_tok, "stream": True}
                if "flash" in model.lower() and "api.deepseek.com" not in base.lower():
                    payload["thinking"] = {"type": "disabled"}

                prefix = body
                segment = ""
                finish = None
                saved_at = time.monotonic()
                error = None
                try:
                    limits = httpx.Limits(max_keepalive_connections=0, max_connections=2)
                    timeout = httpx.Timeout(connect=10.0, read=90.0, write=20.0, pool=10.0)
                    async with httpx.AsyncClient(trust_env=False, limits=limits, timeout=timeout) as client:
                        async with client.stream(
                            "POST",
                            base + "/chat/completions",
                            json=payload,
                            headers={"Authorization": f"Bearer {key}"},
                        ) as resp:
                            if resp.status_code >= 400:
                                err_body = (await resp.aread()).decode("utf-8", errors="replace")[:240]
                                last_err_msg = f"{model} HTTP {resp.status_code}: {err_body}"
                                logger.warning(f"文档生成节点异常 ({last_err_msg})，尝试切换后备节点")
                                error = f"HTTP_{resp.status_code}"
                                continue
                            if "text/event-stream" not in resp.headers.get("content-type", "").lower():
                                packet = json.loads(await resp.aread())
                                choices = packet.get("choices") or []
                                if choices:
                                    segment = str(choices[0].get("message", {}).get("content") or "")
                                    finish = choices[0].get("finish_reason")
                                    body = _merge_partial(prefix, segment)
                                    if len(body) > MAX_BODY_CHARS:
                                        body = body[:MAX_BODY_CHARS]
                                        checkpoint()
                                        raise DocumentGenerationError("正文达到 60000 字工程上限，已保存为草稿，请拆分文档。")
                            else:
                                async for line in resp.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    data = line[5:].strip()
                                    if data == "[DONE]":
                                        break
                                    try:
                                        packet = json.loads(data)
                                    except json.JSONDecodeError:
                                        continue
                                    if packet.get("error"):
                                        last_err_msg = str(packet.get("error"))
                                        error = "STREAM_ERROR"
                                        break
                                    for choice in packet.get("choices") or []:
                                        segment += str(choice.get("delta", {}).get("content") or "")
                                        finish = choice.get("finish_reason") or finish
                                    body = _merge_partial(prefix, segment)
                                    if len(body) > MAX_BODY_CHARS:
                                        body = body[:MAX_BODY_CHARS]
                                        checkpoint()
                                        raise DocumentGenerationError("正文达到 60000 字工程上限，已保存为草稿，请拆分文档。")
                                    if time.monotonic() - saved_at >= 2:
                                        checkpoint()
                                        saved_at = time.monotonic()
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    error = type(exc).__name__
                    last_err_msg = str(exc)
                    logger.warning(f"文档流式传输中断 ({model}): {exc}")

                checkpoint()
                if finish == "stop" and not error and body.strip():
                    if is_placeholder_document(body):
                        error = "PLACEHOLDER"
                    else:
                        checkpoint(complete=True)
                        return body.strip()
                if finish in {"content_filter", "tool_calls"}:
                    raise DocumentGenerationError("模型未完成正文，已保留草稿。")
                if attempt < MAX_REQUESTS - 1:
                    await asyncio.sleep(min(2 ** attempt, 4) if error else 0)
            raise DocumentGenerationError(f"文档本次生成未完成（{last_err_msg or '网络超时'}），已保留草稿；可回复“导出”继续。")
    except asyncio.TimeoutError:
        checkpoint()
        raise DocumentGenerationError("文档生成达到 8 分钟总时限，已保留草稿；可回复“导出”继续。")
    except asyncio.CancelledError:
        checkpoint()
        raise
    finally:
        _slots.release()
