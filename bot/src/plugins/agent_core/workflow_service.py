"""百炼工作流双向对接服务：本地文件上传临时 OSS (oss://) -> 按文件类型路由百炼工作流 -> 产出结果回传下载至本地服务器。"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from nonebot import logger

from .config_manager import BOT_ROOT, ensure_bot_env

_DASHSCOPE_UPLOAD_POLICY_URL = "https://dashscope.aliyuncs.com/api/v1/uploads"
_DASHSCOPE_APP_BASE = "https://dashscope.aliyuncs.com/api/v1/apps"
_WORKFLOW_OUT_DIR = BOT_ROOT / "data" / "cache" / "workflow_outputs"

# 默认绑定你在百炼创建的「戴璟粼的画画」工作流 App ID
_DEFAULT_WORKFLOW_APP_ID = "64f8bb7ee67d42a1a01fd3571562eadf"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}
_DOC_EXTS = {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".pptx", ".ppt", ".xlsx", ".xls", ".csv", ".html"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".amr", ".ogg", ".flac", ".aac", ".wma"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}


def detect_file_type(filename_or_uri: str) -> str:
    """根据文件名、路径或 Data URI 自动识别文件类型：image / doc / audio / video / none。"""
    if not filename_or_uri:
        return "none"
    s = filename_or_uri.strip().lower()
    if s.startswith("data:image/"):
        return "image"
    if s.startswith("data:audio/"):
        return "audio"
    if s.startswith("data:video/"):
        return "video"
    if s.startswith("data:application/pdf"):
        return "doc"

    clean_path = s.split("?", 1)[0]
    ext = Path(clean_path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _DOC_EXTS:
        return "doc"
    if ext in _AUDIO_EXTS:
        return "audio"
    if ext in _VIDEO_EXTS:
        return "video"
    return "image" if "qpic.cn" in s else "none"


def _data_uri_to_bytes_and_ext(data_uri: str) -> tuple[bytes, str]:
    """将 data:image/png;base64,... 解码为 (bytes, '.png')。"""
    if ";base64," not in data_uri:
        return b"", ".bin"
    header, b64_part = data_uri.split(";base64,", 1)
    mime = header.replace("data:", "").strip().lower()
    ext = mimetypes.guess_extension(mime) or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    return base64.b64decode(b64_part), ext


async def upload_to_bailian_temp_oss(
    api_key: str,
    file_source: str | Path | bytes,
    filename: str = "",
    model_name: str = "qwen-vl-plus",
) -> Optional[str]:
    """将本地文件或 Base64 数据上传至百炼免费临时存储空间（有效期 48 小时），返回 oss:// 链接。"""
    if not api_key:
        return None
    try:
        import httpx
    except ImportError:
        return None

    raw_bytes: bytes = b""
    final_name = filename.strip() or f"upload_{int(time.time() * 1000)}.png"

    if isinstance(file_source, bytes):
        raw_bytes = file_source
    elif isinstance(file_source, Path):
        if not file_source.is_file():
            return None
        raw_bytes = file_source.read_bytes()
        final_name = file_source.name
    elif isinstance(file_source, str):
        s = file_source.strip()
        if s.startswith("oss://"):
            return s
        if s.startswith("data:"):
            raw_bytes, ext = _data_uri_to_bytes_and_ext(s)
            h = hashlib.md5(raw_bytes[:4096]).hexdigest()[:10]
            final_name = f"file_{h}{ext}"
        else:
            p = Path(s)
            if p.is_file():
                raw_bytes = p.read_bytes()
                final_name = p.name

    if not raw_bytes:
        return None

    # 确保文件名仅包含安全 ASCII 字符，防止 OSS key 编码异常
    safe_ext = Path(final_name).suffix.lower() or ".png"
    safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "", Path(final_name).stem) or hashlib.md5(raw_bytes[:2048]).hexdigest()[:10]
    safe_filename = f"{safe_stem}{safe_ext}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    params = {
        "action": "getPolicy",
        "model": model_name,
    }

    try:
        timeout = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            # 步骤 1：获取百炼临时存储上传凭证
            policy_resp = await client.get(_DASHSCOPE_UPLOAD_POLICY_URL, headers=headers, params=params)
            if policy_resp.status_code != 200:
                logger.warning(f"获取百炼上传凭证失败 HTTP {policy_resp.status_code}: {policy_resp.text[:300]}")
                return None
            policy_data = (policy_resp.json() or {}).get("data") or {}
            upload_dir = policy_data.get("upload_dir")
            upload_host = policy_data.get("upload_host")
            if not upload_dir or not upload_host:
                logger.warning(f"百炼上传凭证缺少必要字段: {policy_data}")
                return None

            oss_key = f"{upload_dir}/{safe_filename}"
            form_data = {
                "OSSAccessKeyId": policy_data["oss_access_key_id"],
                "Signature": policy_data["signature"],
                "policy": policy_data["policy"],
                "x-oss-object-acl": policy_data["x_oss_object_acl"],
                "x-oss-forbid-overwrite": policy_data["x_oss_forbid_overwrite"],
                "key": oss_key,
                "success_action_status": "200",
            }
            files = {
                "file": (safe_filename, raw_bytes, mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"),
            }

            # 步骤 2：POST multipart/form-data 上传至百炼临时 OSS
            up_resp = await client.post(upload_host, data=form_data, files=files)
            if up_resp.status_code != 200:
                logger.warning(f"上传文件至百炼 OSS 失败 HTTP {up_resp.status_code}: {up_resp.text[:300]}")
                return None

            oss_url = f"oss://{oss_key}"
            logger.info(f"已上传本地文件至百炼临时 OSS: {oss_url} ({len(raw_bytes)} bytes)")
            return oss_url
    except Exception as exc:
        logger.warning(f"上传百炼临时 OSS 异常: {exc}")
        return None


async def download_workflow_outputs_to_local(text_or_json: str) -> list[Path]:
    """提取百炼工作流返回文本或 JSON 中的产出文件/图片 URL，自动下载落盘到本地 bot/data/cache/workflow_outputs/。"""
    if not text_or_json:
        return []
    urls = re.findall(r"(https?://[^\s\"\'\)\]>]+)", text_or_json)
    if not urls:
        return []
    try:
        import httpx
    except ImportError:
        return []

    _WORKFLOW_OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    async with httpx.AsyncClient(trust_env=False, timeout=30.0, follow_redirects=True, verify=False) as client:
        for u in urls[:4]:
            try:
                resp = await client.get(u)
                if resp.status_code == 200 and resp.content and len(resp.content) > 100:
                    clean_u = u.split("?", 1)[0]
                    ext = Path(clean_u).suffix.lower()
                    if not ext or len(ext) > 6:
                        ctype = (resp.headers.get("content-type") or "").lower()
                        if "png" in ctype:
                            ext = ".png"
                        elif "jpeg" in ctype or "jpg" in ctype:
                            ext = ".jpg"
                        elif "pdf" in ctype:
                            ext = ".pdf"
                        else:
                            ext = ".png"
                    uhash = hashlib.md5(u.encode("utf-8")).hexdigest()[:12]
                    out_p = _WORKFLOW_OUT_DIR / f"wf_{int(time.time())}_{uhash}{ext}"
                    out_p.write_bytes(resp.content)
                    saved_paths.append(out_p)
                    logger.info(f"百炼工作流产出已回传保存至本地: {out_p} ({len(resp.content)} bytes)")
            except Exception as exc:
                logger.warning(f"下载工作流产出文件失败 ({u[:60]}): {exc}")
    return saved_paths


def _extract_workflow_result(pkt: dict[str, Any]) -> str:
    """从百炼工作流响应包中提取文本或图片链接。"""
    output = pkt.get("output") or {}

    # 1. 直接 text 字段
    direct_text = output.get("text")
    if isinstance(direct_text, str) and direct_text.strip() and direct_text.strip() not in {"stop", "null"}:
        return direct_text.strip()

    # 2. workflow_message / choices 列表
    pieces: list[str] = []
    for list_key in ("workflow_message", "choices"):
        items = output.get(list_key) or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                msg = item.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    pieces.append(content.strip())
                elif isinstance(content, list):
                    for seg in content:
                        if isinstance(seg, dict):
                            if seg.get("image"):
                                pieces.append(f"![插画]({seg['image']})")
                            elif seg.get("text"):
                                pieces.append(str(seg["text"]).strip())

    # 3. 兼容多模态节点直接输出 images 数组
    images = output.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, str) and img.startswith("http"):
                pieces.append(f"![插画]({img})")
            elif isinstance(img, dict) and img.get("url"):
                pieces.append(f"![插画]({img['url']})")

    return "\n".join(p for p in pieces if p).strip()


async def run_bailian_workflow(
    *,
    api_key: str,
    user_prompt: str,
    file_sources: Optional[list[str | Path]] = None,
    task_type: str = "draw",
    character: str = "amiya",
    workflow_app_id: str = "",
) -> Optional[str]:
    """
    将本地文件上传至百炼临时 OSS，携带 file_url / file_type / task_type 调用百炼工作流，
    并将工作流产出的图片或文件自动回传落盘到本地服务器。
    """
    ensure_bot_env()
    if not api_key:
        return None
    app_id = (
        workflow_app_id
        or (os.getenv("DASHSCOPE_WORKFLOW_APP_ID") or "").strip()
        or _DEFAULT_WORKFLOW_APP_ID
    )
    if not app_id:
        return None

    try:
        import httpx
    except ImportError:
        return None

    sources = list(file_sources or [])
    oss_urls: list[str] = []
    detected_type = "none"

    for src in sources[:4]:
        s_str = str(src)
        ftype = detect_file_type(s_str)
        if detected_type == "none" and ftype != "none":
            detected_type = ftype
        oss_u = await upload_to_bailian_temp_oss(api_key, src)
        if oss_u:
            oss_urls.append(oss_u)

    primary_url = oss_urls[0] if oss_urls else ""
    biz_params: dict[str, Any] = {
        "file_url": primary_url,
        "file_urls": oss_urls,
        "file_type": detected_type,
        "task_type": task_type,
        "character": character,
    }

    input_obj: dict[str, Any] = {
        "prompt": user_prompt or "请处理上传的文件",
        "biz_params": biz_params,
    }
    if detected_type == "image" and oss_urls:
        input_obj["image_list"] = oss_urls

    payload = {
        "input": input_obj,
        "parameters": {
            "has_thoughts": False,
            "incremental_output": False,
        },
        "debug": {},
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-DashScope-SSE": "enable",
        # 关键请求头：允许百炼工作流内各节点（图片解析/文档解析/多模态生成）直接解析 oss:// 临时链接
        "X-DashScope-OssResourceResolve": "enable",
    }

    url = f"{_DASHSCOPE_APP_BASE}/{app_id}/completion"
    latest_text = ""
    try:
        logger.info(
            f"正在调用百炼工作流 {app_id} (file_type={detected_type}, task_type={task_type}, files={len(oss_urls)})..."
        )
        timeout = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=15.0)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="ignore")
                    logger.warning(f"百炼工作流 HTTP {resp.status_code}: {err_body[:400]}")
                    return None
                ctype = (resp.headers.get("content-type") or "").lower()
                if "text/event-stream" in ctype:
                    async for line in resp.aiter_lines():
                        s = line.strip()
                        if not s or not s.startswith("data:"):
                            continue
                        raw_json = s[5:].strip()
                        if not raw_json or raw_json == "[DONE]":
                            continue
                        try:
                            pkt = json.loads(raw_json)
                            piece = _extract_workflow_result(pkt)
                            if piece:
                                latest_text = piece
                        except json.JSONDecodeError:
                            continue
                else:
                    body_bytes = await resp.aread()
                    data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    latest_text = _extract_workflow_result(data)

        if latest_text.strip():
            # 自动将工作流产出的图片/文件回传下载到本地 bot/data/cache/workflow_outputs/
            await download_workflow_outputs_to_local(latest_text)
            return latest_text.strip()
    except Exception as exc:
        logger.warning(f"调用百炼工作流异常: {exc}")

    return None
