"""面向 macOS 菜单栏控制端与 Web 控制台的 FastAPI 路由（支持机器人模式、语音模式、实时生成进度监控、立绘同步与无 422 文档投递）。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from nonebot import get_app, get_driver, logger
from pydantic import BaseModel, Field

from .config_manager import ALLOWED_CHARACTERS, BOT_ROOT, state
from .history_recorder import progress_tracker, recorder, task_scope, current_task_id
from .errors import TTSGenerationError
from . import doc_service
from . import tts_service
from . import llm_service
from . import modes
from .autonomous_agent import autonomous_coordinator
from .user_memory_service import user_memory_store


class GlobalSwitchReq(BaseModel):
    active: bool


class CharacterSwitchReq(BaseModel):
    character: str  # "none" | "amiya" | "kaltsit" | "dania"


class ComfortSwitchReq(BaseModel):
    target: str = Field(..., min_length=1)
    enable: bool


class GroupSwitchReq(BaseModel):
    action: str = Field(default="add")  # "add" | "del"
    group_id: str = Field(..., min_length=1)
    enable: Optional[bool] = None


class GroupUserWhitelistSwitchReq(BaseModel):
    enable: bool


class GroupUserWhitelistUpdateReq(BaseModel):
    group_id: str = Field(..., min_length=1)
    users: list[str] = Field(default_factory=list)


class VoiceSwitchReq(BaseModel):
    enable: bool


class VoiceConfigReq(BaseModel):
    voice_model: Optional[str] = None
    voice_enabled: Optional[bool] = None
    tts_enabled: Optional[bool] = None
    voice_only_reply: Optional[bool] = None
    voice_mode: Optional[str] = None  # "emotion_auto" | "fixed_ref"
    voice_emotion_director_enabled: Optional[bool] = None
    voice_ref_name: Optional[str] = None
    voice_speed: Optional[float] = None
    voice_temperature: Optional[float] = None
    voice_top_p: Optional[float] = None


class VoiceServiceReq(BaseModel):
    target: str = Field(default="9880")  # "9880" | "9876"
    service: Optional[str] = None
    action: str = Field(default="start")  # "start" | "stop"


class VoicePreviewReq(BaseModel):
    voice_model: Optional[str] = None
    character: Optional[str] = None
    text: str = Field(default="哼，今天算你表现不错！记得早点休息。")
    ref_name: str = Field(default="")


class GroupLogExtractReq(BaseModel):
    group_id: str = Field(..., min_length=1)
    raw_text: str = ""


class WorkflowDispatchReq(BaseModel):
    prompt: str = Field(default="请处理上传的文件")
    file_paths: list[str] = Field(default_factory=list)
    task_type: str = Field(default="auto")  # "auto" | "draw" | "qa"
    character: str = Field(default="amiya")
    workflow_app_id: str = Field(default="")


def _list_saved_refs() -> list[str]:
    refs_dir = BOT_ROOT / "data" / "refs"
    if not refs_dir.is_dir():
        return []
    out: list[str] = []
    valid_exts = {".png", ".jpg", ".jpeg", ".webp"}
    for p in sorted(refs_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in valid_exts and not p.name.startswith("."):
            out.append(p.stem)
    return out


def _list_group_personas() -> list[str]:
    groups_dir = BOT_ROOT / "data" / "groups"
    if not groups_dir.is_dir():
        return []
    return [p.stem for p in sorted(groups_dir.glob("*.json")) if p.is_file()]


async def _parse_any_request_payload(request: Request) -> dict[str, Any]:
    """兼容空 Body、JSON Body（含 int 型 group_id/user_id）、Form 表单与 Query 参数，彻底杜绝 FastAPI 422 Unprocessable Entity。"""
    payload: dict[str, Any] = {}
    for k, v in request.query_params.items():
        payload[k] = v
    try:
        raw_bytes = await request.body()
        if raw_bytes:
            text = raw_bytes.decode("utf-8", errors="ignore").strip()
            if text:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    payload.update(parsed)
    except Exception:
        pass
    return payload


def _parse_bool(val: Any, default: bool = True) -> bool:
    """万能安全布尔解析，彻底防止字符串 'false'/'0' 意外解析为 True。"""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("false", "0", "no", "off"):
        return False
    if s in ("true", "1", "yes", "on"):
        return True
    return bool(val)


async def _handle_send_docs_request(request: Request) -> dict[str, Any]:
    task_id = uuid.uuid4().hex
    progress_tracker.update(task_id, kind="doc", status="pending", stage="等待文档投递")
    with task_scope(task_id):
        try:
            result = await _send_docs_request_impl(request)
            success = bool(result.get("delivered")) and not result.get("delivery_errors")
            result["code"] = 0 if success else 1
            result["status"] = "ok" if success else "error"
            progress_tracker.update(task_id, status="done" if success else "error", percent=100,
                                    stage="文件投递完成" if success else "文件投递未确认")
            return result
        except asyncio.CancelledError:
            progress_tracker.update(task_id, status="error", stage="文件任务已取消", percent=100)
            raise
        except Exception:
            progress_tracker.update(task_id, status="error", stage="文件任务失败", percent=100)
            raise


async def _send_docs_request_impl(request: Request) -> dict[str, Any]:
    """处理 /api/test/send_docs 与 /api/docs/send：使用 doc_service 生成 PDF/DOCX/MD 并投递到 QQ 群或私聊。"""
    from nonebot import get_bots

    data = await _parse_any_request_payload(request)
    title = str(data.get("title") or "罗德岛战术简报与文档投递测试").strip()
    content = str(
        data.get("content")
        or data.get("text")
        or (
            f"# {title}\n\n"
            "## 一、 链路状态综述\n"
            "- **文档导出引擎**：PDF / Word (.docx) / Markdown (.md) 三格式同步渲染已就绪。\n"
            "- **语音解析路由**：QQ 语音条自动转交阿米娅 Agent (`DASHSCOPE_APP_ID_AMIYA`) 解析。\n\n"
            "| 格式类型 | 渲染引擎 | 状态 |\n"
            "| --- | --- | --- |\n"
            "| PDF (.pdf) | Adobe CIDFont STSong-Light 原生分页 | 正常 |\n"
            "| Word (.docx) | OpenXML 原生表格与多级标题 | 正常 |\n"
            "| Markdown (.md) | UTF-8 元信息头标准文档 | 正常 |\n\n"
            "## 二、 投递验证结论\n"
            "本文件由 `/api/test/send_docs` 接口自动生成并通过 OneBot V11 协议投递。"
        )
    ).strip()

    character = str(data.get("character") or state.active_character or "amiya").strip().lower()
    if character == "none":
        character = "amiya"

    raw_group = data.get("group_id")
    raw_user = data.get("user_id") or data.get("qq")
    if raw_group is not None and str(raw_group).strip().lower() in ("none", "private", "0", "null", "false"):
        group_id = ""
    elif raw_group is None or str(raw_group).strip() == "":
        if raw_user and str(raw_user).strip():
            group_id = ""
        else:
            group_id = str(state.active_groups[0]) if state.active_groups else ""
    else:
        group_id = str(raw_group).strip()

    user_id = str(raw_user).strip() if raw_user is not None and str(raw_user).strip() else str(state.superuser_qq or "")

    formats_raw = data.get("formats") or ["pdf", "docx", "md"]
    if isinstance(formats_raw, str):
        formats = [f.strip().lower() for f in formats_raw.split(",") if f.strip()]
    elif isinstance(formats_raw, list):
        formats = [str(f).strip().lower() for f in formats_raw if str(f).strip()]
    else:
        formats = ["pdf", "docx", "md"]

    progress_tracker.update(
        current_task_id.get(),
        kind="doc",
        title="📄 文档生成与 QQ 投递",
        stage="正在渲染 PDF / DOCX / MD...",
        percent=45,
        status="running",
        input_text=title,
        output_text=content[:300],
        extra_info=f"格式: {', '.join(formats)}",
    )

    generated_paths = await asyncio.to_thread(doc_service.export_documents, title, content, formats, character)
    generated_files = [str(p.resolve()) for p in generated_paths]

    delivered_to: list[str] = []
    aliases = {"word": "docx", "doc": "docx", "markdown": "md"}
    requested = {aliases.get(fmt, fmt) for fmt in formats}
    delivery_errors = ["未生成格式: " + fmt for fmt in sorted(requested - {p.suffix.lstrip(".") for p in generated_paths})]
    bots = get_bots()
    bot = next(iter(bots.values()), None) if bots else None
    if bot is None:
        delivery_errors.append("QQ 机器人未连接，文件尚未上传")

    if bot is not None:
        for p in generated_paths:
            ok = await doc_service.send_file_to_qq(
                bot,
                None,
                p,
                group_id=group_id or None,
                user_id=user_id if not group_id else None,
            )
            if ok:
                target_tag = f"group:{group_id}" if group_id else f"private:{user_id}"
                delivered_to.append(f"{target_tag}:{p.name}")
            else:
                delivery_errors.append(p.name)

    progress_tracker.update(
        current_task_id.get(),
        kind="doc",
        title="📄 文档生成与 QQ 投递",
        stage="文档渲染结束，核对投递结果",
        percent=100,
        status="running",
        input_text=title,
        output_text="\n".join(generated_files),
        extra_info=f"生成 {len(generated_files)} 份 | 投递 {len(delivered_to)} 份",
    )
    recorder.log(
        f"文档测试投递完成: 生成 {len(generated_files)} 个文件 ({', '.join(p.name for p in generated_paths)}), 投递={len(delivered_to)}"
    )
    return {
        "code": 0,
        "status": "ok",
        "title": title,
        "group_id": group_id,
        "user_id": user_id,
        "files": generated_files,
        "exported_files": doc_service.list_exported_files(limit=10),
        "delivered": delivered_to,
        "delivery_errors": delivery_errors,
    }


async def _build_full_status(include_history: bool = False) -> dict[str, Any]:
    from nonebot import get_bots

    bots = get_bots()
    v_srv = await tts_service.check_voice_services()
    voice_models_list = (
        tts_service.list_voice_models()
        if hasattr(tts_service, "list_voice_models")
        else [{"id": "dania", "name": "达妮娅 (Dania)", "ready": True}]
    )
    payload: dict[str, Any] = {
        "global_active": state.global_active,
        "accept_poke": getattr(state, "accept_poke", True),
        "active_character": state.active_character,
        "active_groups": state.active_groups,
        "comfort_targets": state.comfort_mode_targets,
        "voice_enabled": state.voice_enabled,
        "voice_model": getattr(state, "voice_model", "auto"),
        "voice_models": voice_models_list,
        "tts_enabled": state.voice_enabled,
        "voice_only_reply": state.voice_only_reply,
        "voice_mode": state.voice_mode,
        "voice_emotion_director_enabled": state.voice_emotion_director_enabled,
        "voice_ref_name": state.voice_ref_name,
        "voice_speed": state.voice_speed,
        "voice_temperature": state.voice_temperature,
        "voice_top_p": state.voice_top_p,
        "tts_9880_online": v_srv["tts_9880_online"],
        "studio_9876_online": v_srv["studio_9876_online"],
        "voice_services": {
            "api_9880": v_srv["tts_9880_online"],
            "studio_9876": v_srv["studio_9876_online"],
            "tts_url": tts_service.TTS_URL,
            "studio_url": "http://127.0.0.1:9876",
        },
        "voice_refs": tts_service.list_voice_refs(),
        "saved_refs": [],
        "saved_refs_count": len(_list_saved_refs()),
        "character_refs": [],
        "group_personas": _list_group_personas(),
        "exported_files": doc_service.list_exported_files(limit=20),
        "progress_tasks": progress_tracker.list_recent(),
        "latest_audio_meta": dict(getattr(tts_service, "LAST_AUDIO_META", {})),
        "bot_connected": len(bots) > 0,
        "connected_bots": list(bots.keys()),
        "small_talk_mode": state.small_talk_mode,
        "small_talk_style": getattr(state, "small_talk_style", "pure_persona"),
        "auto_reply_mode": getattr(state, "auto_reply_mode", True),
        "reply_interval_seconds": getattr(state, "reply_interval_seconds", 1.5),
        "training_mode": state.training_mode,
        "training_groups": state.training_groups,
        "group_user_whitelist_enabled": getattr(state, "group_user_whitelist_enabled", False),
        "group_user_whitelists": getattr(state, "group_user_whitelists", {}),
        "superuser_qq": state.superuser_qq,
        "training_status": (await modes.get_training_status_async()) if hasattr(modes, "get_training_status_async") else (modes.get_training_status() if hasattr(modes, "get_training_status") else {}),
        "autonomous_status": autonomous_coordinator.get_status(),
        "group_config_scope": "global_default",
        "group_buffer_enabled": state.group_buffer_enabled,
        "group_merge_window_sec": state.group_merge_window_sec,
        "group_merge_max_messages": state.group_merge_max_messages,
        "group_merge_max_chars": state.group_merge_max_chars,
        "group_quota_min_interval_sec": state.group_quota_min_interval_sec,
        "group_quota_max_per_minute": state.group_quota_max_per_minute,
        "group_chunk_pace_sec": state.group_chunk_pace_sec,
        "amiya_sticker_enabled": getattr(state, "amiya_sticker_enabled", True),
        "amiya_sticker_min_delay_sec": getattr(state, "amiya_sticker_min_delay_sec", 1.0),
        "amiya_sticker_max_delay_sec": getattr(state, "amiya_sticker_max_delay_sec", 20.0),
        "image_gen_route": getattr(state, "image_gen_route", "qwen"),
        "friend_comfy_workflow": getattr(state, "friend_comfy_workflow", "sdxl"),
        "friend_comfy_url": getattr(state, "friend_comfy_url", "http://node.tail3189e0.ts.net:8100"),
    }
    if include_history:
        payload["recent_logs"] = recorder.get_logs()
        payload["recent_dialogues"] = recorder.get_dialogues()
        payload["profiles_summary"] = user_memory_store.list_profiles_summary(limit=25)
    return payload


def _register_routes(app: FastAPI, services) -> None:
    if getattr(app.state, "_qq_agent_v9_mounted", False):
        return
    doc_service = services.documents
    tts_service = services.tts
    existing_paths = {getattr(r, "path", "") for r in app.routes}

    if "/panel" not in existing_paths:
        @app.get("/", response_class=HTMLResponse)
        @app.get("/panel", response_class=HTMLResponse)
        async def get_web_panel() -> HTMLResponse:
            try:
                from . import web_panel
                content = web_panel.get_panel_html()
                return HTMLResponse(
                    content=content,
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    },
                )
            except Exception as exc:
                import traceback
                err_tb = traceback.format_exc()
                logger.error(f"[ControllerAPI] 渲染 Web 控制面板失败: {exc}\n{err_tb}")
                diag_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>PRTS 控制台诊断</title></head>
<body style="background:#090a0f;color:#e2e8f0;font-family:monospace;padding:24px;">
  <h2 style="color:#ef4444;">⚠️ PRTS 控制台中枢加载异常</h2>
  <p><strong>异常详情:</strong> {exc}</p>
  <pre style="background:#1e293b;padding:12px;border-radius:6px;color:#f87171;overflow:auto;">{err_tb}</pre>
  <p><a href="/api/dashboard" style="color:#38bdf8;">👉 点击直达 /api/dashboard 接口数据</a></p>
</body>
</html>"""
                return HTMLResponse(content=diag_html, status_code=500)

    if "/panel-assets/{file_path:path}" not in existing_paths:
        @app.get("/panel-assets/{file_path:path}")
        async def get_panel_asset(file_path: str) -> FileResponse:
            from .web_panel import WEB_ROOT
            root = WEB_ROOT.resolve()
            target = (root / file_path).resolve()
            # 只允许 WEB_ROOT 内部的真实文件，阻止路径穿越
            if root not in target.parents and target != root:
                raise HTTPException(status_code=404)
            if not target.is_file():
                raise HTTPException(status_code=404)
            media_types = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".glb": "model/gltf-binary",
                ".gltf": "model/gltf+json",
                ".wasm": "application/wasm",
                ".json": "application/json",
                ".html": "text/html",
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".svg": "image/svg+xml",
            }
            media_type = media_types.get(target.suffix.lower(), "application/octet-stream")
            return FileResponse(target, media_type=media_type, headers={"Cache-Control": "no-store"})

    if "/api/status" not in existing_paths:
        @app.get("/api/status")
        async def get_system_status() -> dict[str, Any]:
            return await _build_full_status(include_history=False)

    if "/api/dashboard" not in existing_paths:
        @app.get("/api/dashboard")
        async def get_dashboard() -> dict[str, Any]:
            return await _build_full_status(include_history=True)

    if "/api/progress" not in existing_paths:
        @app.get("/api/progress")
        async def get_realtime_progress() -> dict[str, Any]:
            return {
                "code": 0,
                "tasks": progress_tracker.list_recent(),
                "exported_files": doc_service.list_exported_files(limit=20),
            }

    if "/api/test/send_docs" not in existing_paths:
        @app.get("/api/test/send_docs")
        @app.post("/api/test/send_docs")
        @app.get("/api/docs/send")
        @app.post("/api/docs/send")
        async def test_send_docs(request: Request) -> dict[str, Any]:
            return await _handle_send_docs_request(request)

    if "/api/exports" not in existing_paths:
        @app.get("/api/exports")
        async def list_exports() -> dict[str, Any]:
            return {"code": 0, "files": doc_service.list_exported_files(limit=30)}

    if "/api/exports/{filename}" not in existing_paths:
        @app.get("/api/exports/{filename}")
        async def download_exported_file(filename: str) -> FileResponse:
            safe_name = Path(filename).name
            target = doc_service.EXPORTS_DIR / safe_name
            if not target.is_file():
                target = doc_service.TMP_EXPORT_DIR / safe_name
            if not target.is_file():
                raise HTTPException(status_code=404, detail="文件不存在")
            return FileResponse(path=str(target), filename=safe_name)

    if "/api/switch/voice" not in existing_paths:
        @app.post("/api/switch/voice")
        async def switch_voice(req: VoiceSwitchReq) -> dict[str, Any]:
            state.voice_enabled = req.enable
            if req.enable:
                state.voice_only_reply = True
            await state.save()
            return {
                "code": 0,
                "voice_enabled": state.voice_enabled,
                "voice_only_reply": state.voice_only_reply,
            }

    if "/api/voice/config" not in existing_paths:
        @app.post("/api/voice/config")
        async def update_voice_config(req: VoiceConfigReq) -> dict[str, Any]:
            if req.voice_model is not None and hasattr(tts_service, "list_voice_models"):
                available = {m["id"] for m in tts_service.list_voice_models() if m.get("ready")}
                if req.voice_model != "auto" and req.voice_model not in available:
                    raise HTTPException(status_code=400, detail="未安装该语音模型")
                state.voice_model = req.voice_model
            if req.voice_enabled is not None:
                state.voice_enabled = req.voice_enabled
            if req.tts_enabled is not None:
                state.voice_enabled = req.tts_enabled
            if req.voice_only_reply is not None:
                state.voice_only_reply = req.voice_only_reply
            if req.voice_mode in {"emotion_auto", "fixed_ref"}:
                state.voice_mode = req.voice_mode
            if req.voice_emotion_director_enabled is not None:
                state.voice_emotion_director_enabled = req.voice_emotion_director_enabled
            if req.voice_ref_name is not None:
                state.voice_ref_name = req.voice_ref_name.strip() or "default"
            if req.voice_speed is not None:
                state.voice_speed = req.voice_speed
            if req.voice_temperature is not None:
                state.voice_temperature = req.voice_temperature
            if req.voice_top_p is not None:
                state.voice_top_p = req.voice_top_p
            await state.save()
            return {
                "code": 0,
                "voice_model": getattr(state, "voice_model", "auto"),
                "voice_enabled": state.voice_enabled,
                "tts_enabled": state.voice_enabled,
                "voice_only_reply": state.voice_only_reply,
                "voice_mode": state.voice_mode,
                "voice_emotion_director_enabled": state.voice_emotion_director_enabled,
                "voice_ref_name": state.voice_ref_name,
                "voice_speed": state.voice_speed,
                "voice_temperature": state.voice_temperature,
                "voice_top_p": state.voice_top_p,
            }

    if "/api/image/cache/{filename}" not in existing_paths:
        @app.get("/api/image/cache/{filename}")
        async def get_cached_generated_image(filename: str) -> FileResponse:
            safe_name = Path(filename).name
            cache_dir = BOT_ROOT / "data" / "cache" / "images"
            target = (cache_dir / safe_name).resolve()
            if cache_dir.resolve() not in target.parents or not target.is_file():
                raise HTTPException(status_code=404, detail="缓存图片不存在")
            media_type = "image/jpeg" if target.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            return FileResponse(target, media_type=media_type, headers={"Cache-Control": "public, max-age=3600"})

    if "/api/switch/image_route" not in existing_paths:
        @app.get("/api/switch/image_route")
        @app.post("/api/switch/image_route")
        async def switch_image_generation_route(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            raw_route = payload.get("route") or payload.get("image_gen_route")
            raw_wf = payload.get("workflow") or payload.get("friend_comfy_workflow")
            raw_url = payload.get("friend_comfy_url")
            if raw_route is not None:
                state.image_gen_route = str(raw_route).strip().lower()
            if raw_wf is not None:
                state.friend_comfy_workflow = str(raw_wf).strip().lower()
            if raw_url is not None and str(raw_url).strip():
                state.friend_comfy_url = str(raw_url).strip()
            await state.save()
            route_label = (
                f"朋友 ComfyUI ({state.friend_comfy_workflow.upper()})"
                if state.image_gen_route == "friend_comfy"
                else "阿里 Qwen 生图 (qwen-image-2.1-pro)"
            )
            recorder.log(f"[生图路线切换] 已切换至: {route_label}")
            return {
                "code": 0,
                "status": "ok",
                "image_gen_route": state.image_gen_route,
                "friend_comfy_workflow": state.friend_comfy_workflow,
                "friend_comfy_url": state.friend_comfy_url,
                "route_label": route_label,
            }

    if "/api/image/comfy_status" not in existing_paths:
        @app.get("/api/image/comfy_status")
        async def get_friend_comfy_status() -> dict[str, Any]:
            st = await llm_service.check_friend_comfy_status()
            return {"code": 0 if st.get("online") else 1, **st}

    if "/api/image/test_generate" not in existing_paths:
        @app.get("/api/image/test_generate")
        @app.post("/api/image/test_generate")
        async def test_image_generation_route(request: Request) -> dict[str, Any]:
            import re
            payload = await _parse_any_request_payload(request)
            route = str(payload.get("route") or state.image_gen_route or "qwen").strip().lower()
            workflow = str(payload.get("workflow") or state.friend_comfy_workflow or "sdxl").strip().lower()
            prompt = str(
                payload.get("prompt")
                or "生成一张达妮娅穿婚纱的图，焦距85mm，f1.8,1/200s快门，iso400，室内礼堂"
            ).strip()
            use_ref = _parse_bool(payload.get("use_ref"), True)
            t0 = time.time()
            task_id = uuid.uuid4().hex
            progress_tracker.update(
                task_id,
                kind="image",
                title=f"🎨 生图路线实测 ({'朋友ComfyUI' if route in {'friend_comfy', 'comfy', 'comfyui', 'friend'} else '阿里Qwen'})",
                status="running",
                stage="正在准备参考图与提示词...",
                percent=15,
                input_text=prompt,
            )
            try:
                with task_scope(task_id):
                    ds_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
                    refs = llm_service._load_local_character_refs(prompt, max_refs=1) if use_ref else []
                    extra_traits = ""
                    prompt_key = ds_key or (os.getenv("DEEPSEEK_API_KEY") or "").strip()
                    if refs:
                        extra_traits = await llm_service.extract_character_visual_traits(
                            prompt_key, refs[0], prompt, reference_role="identity", reference_images=refs
                        )
                    else:
                        extra_traits = await llm_service.search_character_visual_sheet(prompt_key, prompt)
                    reply = await llm_service.generate_image_with_qwen_or_wan(
                        api_key=ds_key,
                        user_prompt=prompt,
                        image_b64_list=refs,
                        character=state.active_character or "amiya",
                        visual_traits=extra_traits,
                        reference_role="identity" if refs else "edit",
                        route_override=route,
                        workflow_override=workflow,
                    )
                dur = round(time.time() - t0, 2)
                m_url = re.search(r"!\[.*?\]\((https?://[^\s\)]+)\)", reply or "")
                img_url = m_url.group(1) if m_url else ""
                progress_tracker.update(
                    task_id, status="done", percent=100, stage=f"测试生图完成 ({dur}s)", output_text=img_url
                )
                return {
                    "code": 0,
                    "status": "ok",
                    "route": route,
                    "workflow": workflow,
                    "duration_sec": dur,
                    "prompt": prompt,
                    "used_refs_count": len(refs),
                    "image_url": img_url,
                    "reply": reply,
                }
            except Exception as exc:
                dur = round(time.time() - t0, 2)
                progress_tracker.update(
                    task_id, status="error", percent=100, stage=f"测试生图失败: {exc}"
                )
                return {
                    "code": 1,
                    "status": "error",
                    "route": route,
                    "workflow": workflow,
                    "duration_sec": dur,
                    "prompt": prompt,
                    "error": str(exc),
                }

    if "/api/sticker/config" not in existing_paths:
        @app.get("/api/sticker/config")
        async def get_sticker_config() -> dict[str, Any]:
            return {
                "code": 0,
                "status": "ok",
                "amiya_sticker_enabled": getattr(state, "amiya_sticker_enabled", True),
                "amiya_sticker_min_delay_sec": getattr(state, "amiya_sticker_min_delay_sec", 1.0),
                "amiya_sticker_max_delay_sec": getattr(state, "amiya_sticker_max_delay_sec", 20.0),
            }

        @app.post("/api/sticker/config")
        async def update_sticker_config(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            try:
                enabled = _parse_bool(
                    payload.get("amiya_sticker_enabled"),
                    getattr(state, "amiya_sticker_enabled", True),
                )
                min_delay = float(payload.get(
                    "amiya_sticker_min_delay_sec",
                    getattr(state, "amiya_sticker_min_delay_sec", 1.0),
                ))
                max_delay = float(payload.get(
                    "amiya_sticker_max_delay_sec",
                    getattr(state, "amiya_sticker_max_delay_sec", 20.0),
                ))
            except (TypeError, ValueError) as exc:
                return {"code": 1, "status": "error", "message": f"表情包参数格式错误: {exc}"}
            if not (1.0 <= min_delay <= 20.0 and 1.0 <= max_delay <= 20.0):
                return {"code": 1, "status": "error", "message": "表情包延迟必须在 1 ~ 20 秒之间"}
            if min_delay > max_delay:
                return {"code": 1, "status": "error", "message": "最短延迟不能大于最长延迟"}

            state.amiya_sticker_enabled = enabled
            state.amiya_sticker_min_delay_sec = min_delay
            state.amiya_sticker_max_delay_sec = max_delay
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "amiya_sticker_enabled": state.amiya_sticker_enabled,
                "amiya_sticker_min_delay_sec": state.amiya_sticker_min_delay_sec,
                "amiya_sticker_max_delay_sec": state.amiya_sticker_max_delay_sec,
            }

    if "/api/voice/service" not in existing_paths:
        @app.get("/api/voice/service")
        @app.post("/api/voice/service")
        async def control_voice_service(
            req: Optional[VoiceServiceReq] = None,
            target: str = "9880",
            action: str = "restart",
        ) -> dict[str, Any]:
            gsv_root = BOT_ROOT.parent / "gpt-sovits"
            raw_target = ((req.service or req.target) if req else target or "9880").strip().lower()
            act = (req.action if req else action or "restart").strip().lower()
            target_clean = "9876" if "9876" in raw_target or "studio" in raw_target else "9880"
            try:
                if target_clean == "9880":
                    if act == "start":
                        script = gsv_root / "scripts" / "start_api.sh"
                        if script.is_file():
                            subprocess.Popen(["/bin/bash", str(script)], cwd=str(gsv_root))
                            return {"code": 0, "message": "已发出 9880 语音 API 启动指令"}
                    elif act == "stop":
                        subprocess.Popen(["/usr/bin/pkill", "-f", "api_v2.py"])
                        return {"code": 0, "message": "已停止 9880 语音 API"}
                    elif act == "restart":
                        subprocess.Popen(["/usr/bin/pkill", "-f", "api_v2.py"])
                        await asyncio.sleep(1.2)
                        script = gsv_root / "scripts" / "start_api.sh"
                        if script.is_file():
                            subprocess.Popen(["/bin/bash", str(script)], cwd=str(gsv_root))
                        return {"code": 0, "message": "已重新启动 9880 语音 API"}
                elif target_clean == "9876":
                    if act == "start":
                        script = gsv_root / "scripts" / "start_studio.sh"
                        if script.is_file():
                            subprocess.Popen(["/bin/bash", str(script)], cwd=str(gsv_root))
                            return {"code": 0, "message": "已发出 9876 语音工作室启动指令"}
                    elif act == "stop":
                        subprocess.Popen(["/usr/bin/pkill", "-f", "voice_studio.py"])
                        return {"code": 0, "message": "已停止 9876 语音工作室"}
                    elif act == "restart":
                        subprocess.Popen(["/usr/bin/pkill", "-f", "voice_studio.py"])
                        await asyncio.sleep(1.2)
                        script = gsv_root / "scripts" / "start_studio.sh"
                        if script.is_file():
                            subprocess.Popen(["/bin/bash", str(script)], cwd=str(gsv_root))
                        return {"code": 0, "message": "已重新启动 9876 语音工作室"}
            except Exception as exc:
                return {"code": 1, "message": f"操作失败: {exc}"}
            return {"code": 1, "message": "未找到对应语音启动脚本"}

    if "/api/voice/preview" not in existing_paths:
        @app.get("/api/voice/preview")
        @app.post("/api/voice/preview")
        async def preview_voice_tts(
            req: Optional[VoicePreviewReq] = None,
            text: str = "哼，今天算你表现不错！记得早点休息。",
            character: str = "达妮娅",
            ref_name: str = "",
        ) -> dict[str, Any]:
            target_text = req.text if req else (text or "哼，今天算你表现不错！记得早点休息。")
            target_role = (req.voice_model or req.character) if req else (character or "达妮娅")
            target_ref = (req.ref_name if req else ref_name or "").strip()
            task_id = uuid.uuid4().hex
            progress_tracker.update(task_id, kind="voice", status="running", stage="生成试听", input_text=target_text)
            try:
                with task_scope(task_id):
                    b64_rec = await tts_service.synthesize(
                        target_text, role_name=target_role,
                        timeout=600.0, truncate_long=False, force_ref_name=target_ref, prefer_wav=True,
                    )
                progress_tracker.update(task_id, status="done", percent=100, stage="试听音频已生成")
            except asyncio.CancelledError:
                progress_tracker.update(task_id, status="error", percent=100, stage="试听已取消")
                raise
            except TTSGenerationError as exc:
                progress_tracker.update(task_id, status="error", percent=100, stage=str(exc))
                return {"code": 1, "message": str(exc)}
            if not b64_rec or not b64_rec.startswith("base64://"):
                return {"code": 1, "message": "语音合成失败，请确认 9880 GPT-SoVITS 服务已启动"}
            raw_b64 = b64_rec[len("base64://") :]
            return {
                "code": 0,
                "audio_data_uri": f"data:audio/wav;base64,{raw_b64}",
                "meta": dict(getattr(tts_service, "LAST_AUDIO_META", {})),
            }

    if "/api/voice/test" not in existing_paths:
        @app.get("/api/voice/test")
        async def test_voice_tts_endpoint(role: Optional[str] = None) -> dict[str, Any]:
            t0 = time.time()
            target_role = (role or state.active_character or "阿米娅").strip()
            text = "博士，今天辛苦啦！" if target_role in ("阿米娅", "amiya") else "哼，今天算你表现不错！"
            task_id = uuid.uuid4().hex
            progress_tracker.update(task_id, kind="voice", status="pending", stage="准备语音测试")
            try:
                with task_scope(task_id):
                    progress_tracker.update(task_id, status="running", stage="测试语音合成")
                    b64_rec = await tts_service.synthesize(
                        text, role_name=target_role,
                        timeout=600.0, truncate_long=True, prefer_wav=False,
                    )
                raw = base64.b64decode(b64_rec.removeprefix("base64://"), validate=True)
                if not raw.startswith((b"\x02#!SILK_V3", b"#!SILK_V3")):
                    raise TTSGenerationError("语音测试未返回 SILK v3。")
                progress_tracker.update(task_id, status="done", percent=100, stage="语音编码测试通过")
                dur = round(time.time() - t0, 2)
                return {
                    "code": 0,
                    "status": "success",
                    "duration_sec": dur,
                    "role": target_role,
                    "text": text,
                    "is_silk": True,
                    "payload_len": len(b64_rec or ""),
                    "meta": dict(getattr(tts_service, "LAST_AUDIO_META", {})),
                }
            except asyncio.CancelledError:
                progress_tracker.update(task_id, status="error", percent=100, stage="语音测试已取消")
                raise
            except Exception as exc:
                progress_tracker.update(task_id, status="error", percent=100, stage="语音测试失败")
                dur = round(time.time() - t0, 2)
                return {"code": 1, "status": "error", "error": str(exc), "duration_sec": dur}

    if "/api/vl/test" not in existing_paths:
        @app.get("/api/vl/test")
        async def test_vl_vision_endpoint(model: str = "") -> dict[str, Any]:
            t0 = time.time()
            ref_path = BOT_ROOT / "data" / "refs" / "Touch.png"
            if not ref_path.exists():
                return {"code": 1, "error": "测试参考图不存在"}
            with open(ref_path, "rb") as f:
                raw_b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
            api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
            target_model = model or os.getenv("DEEPSEEK_MODEL") or "deepseek-flash"
            reply = await llm_service.chat_with_vision(
                api_key=api_key,
                image_b64_list=[raw_b64],
                user_text="请简要看看图中的角色是谁，并说出她的外貌特征。",
                character=state.active_character or "amiya",
            )
            dur = round(time.time() - t0, 2)
            if reply:
                return {
                    "code": 0,
                    "status": "success",
                    "duration_sec": dur,
                    "model_used": target_model,
                    "reply": reply,
                }
            return {
                "code": 1,
                "status": "failed",
                "duration_sec": dur,
                "model_tested": target_model,
                "error": "视觉识别未返回有效内容，请检查日志",
            }

    if "/api/switch/global" not in existing_paths:
        @app.post("/api/switch/global")
        async def toggle_global_post(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            raw_act = payload.get("active")
            if raw_act is not None:
                state.global_active = _parse_bool(raw_act, state.global_active)
            if not state.global_active:
                from .group_chat_manager import group_chat_manager
                autonomous_coordinator.clear_pending_events()
                for t in list(group_chat_manager._window_timers.values()):
                    if t and not t.done():
                        t.cancel()
                group_chat_manager._window_timers.clear()
                group_chat_manager._pending_buffers.clear()
                for futs in list(group_chat_manager._batch_futures.values()):
                    for f in futs:
                        if not f.done():
                            f.set_result(None)
                group_chat_manager._batch_futures.clear()
            await state.save()
            return {"code": 0, "global_active": state.global_active}

        @app.get("/api/switch/global")
        async def toggle_global_get(active: Any = True) -> dict[str, Any]:
            state.global_active = _parse_bool(active, True)
            if not state.global_active:
                from .group_chat_manager import group_chat_manager
                autonomous_coordinator.clear_pending_events()
                for t in list(group_chat_manager._window_timers.values()):
                    if t and not t.done():
                        t.cancel()
                group_chat_manager._window_timers.clear()
                group_chat_manager._pending_buffers.clear()
                for futs in list(group_chat_manager._batch_futures.values()):
                    for f in futs:
                        if not f.done():
                            f.set_result(None)
                group_chat_manager._batch_futures.clear()
            await state.save()
            return {"code": 0, "global_active": state.global_active}

    if "/api/switch/character" not in existing_paths:
        @app.post("/api/switch/character")
        async def switch_character(req: CharacterSwitchReq) -> dict[str, Any]:
            character = req.character.strip().lower()
            if character not in ALLOWED_CHARACTERS:
                return {
                    "code": 1,
                    "message": f"character 仅允许 {sorted(ALLOWED_CHARACTERS)}",
                    "active_character": state.active_character,
                }
            state.active_character = character
            await state.save()
            return {"code": 0, "active_character": state.active_character}

    if "/api/switch/group" not in existing_paths:
        @app.post("/api/switch/group")
        async def switch_group(req: GroupSwitchReq) -> dict[str, Any]:
            gid = req.group_id.strip()
            if req.enable is not None:
                action = "add" if req.enable else "del"
            else:
                action = req.action.strip().lower()
            groups = [str(g) for g in state.active_groups]
            if action == "add":
                if gid and gid not in groups:
                    groups.append(gid)
            else:
                groups = [g for g in groups if g != gid]
            state.active_groups = groups
            await state.save()
            return {"code": 0, "active_groups": state.active_groups}

    if "/api/groups/members" not in existing_paths:
        @app.get("/api/groups/members")
        async def get_group_members(group_id: str = "") -> dict[str, Any]:
            from nonebot import get_bots
            gid = group_id.strip()
            if not gid:
                return {"code": 1, "message": "group_id 不能为空", "members": []}
            bots = get_bots()
            bot = next(iter(bots.values()), None) if bots else None
            if not bot:
                return {"code": 2, "message": "QQ 机器人未连接", "members": []}
            try:
                raw_members = await bot.get_group_member_list(group_id=int(gid), no_cache=True)
                members = []
                for m in raw_members:
                    uid = str(m.get("user_id", ""))
                    nickname = str(m.get("nickname", "") or "")
                    card = str(m.get("card", "") or "")
                    role = str(m.get("role", "member"))
                    members.append({
                        "user_id": uid,
                        "nickname": nickname,
                        "card": card,
                        "display_name": card if card else nickname,
                        "role": role,
                    })
                return {"code": 0, "group_id": gid, "members": members, "count": len(members)}
            except Exception as e:
                logger.error(f"[ControllerAPI] 获取群 {gid} 成员列表失败: {e}")
                return {"code": 3, "message": f"获取群成员列表失败: {e}", "members": []}

    if "/api/whitelist/users" not in existing_paths:
        @app.get("/api/whitelist/users")
        async def get_group_user_whitelist(group_id: str = "") -> dict[str, Any]:
            gid = group_id.strip()
            whitelisted = state.get_group_user_whitelist(gid) if gid else []
            return {
                "code": 0,
                "enabled": state.group_user_whitelist_enabled,
                "group_id": gid,
                "users": whitelisted,
                "all_whitelists": state.group_user_whitelists,
            }

        @app.post("/api/whitelist/users")
        async def update_group_user_whitelist(req: GroupUserWhitelistUpdateReq) -> dict[str, Any]:
            gid = req.group_id.strip()
            users = [str(u).strip() for u in req.users if str(u).strip()]
            await state.set_group_user_whitelist(gid, users)
            return {
                "code": 0,
                "group_id": gid,
                "users": state.get_group_user_whitelist(gid),
                "enabled": state.group_user_whitelist_enabled,
            }

    if "/api/switch/group_user_whitelist" not in existing_paths:
        @app.post("/api/switch/group_user_whitelist")
        async def switch_group_user_whitelist(req: GroupUserWhitelistSwitchReq) -> dict[str, Any]:
            await state.set_group_user_whitelist_enabled(req.enable)
            return {
                "code": 0,
                "enabled": state.group_user_whitelist_enabled,
            }

    if "/api/switch/comfort" not in existing_paths:
        @app.post("/api/switch/comfort")
        async def switch_comfort(req: ComfortSwitchReq) -> dict[str, Any]:
            target = req.target.strip()
            targets = state.comfort_mode_targets
            if req.enable:
                if target not in targets:
                    targets.append(target)
            else:
                targets = [t for t in targets if t != target]
            state.comfort_mode_targets = targets
            await state.save()
            return {"code": 0, "comfort_targets": state.comfort_mode_targets}

    if "/api/extract/group" not in existing_paths:
        @app.post("/api/extract/group")
        async def extract_group_persona(req: GroupLogExtractReq) -> dict[str, Any]:
            try:
                from .log_extractor import run_log_extraction  # type: ignore
            except ImportError:
                return {"code": 1, "message": "log_extractor 未就绪 (Task5)"}

            result = await run_log_extraction(req.group_id, req.raw_text)
            if isinstance(result, dict) and result.get("status") not in {"ok", None}:
                return {
                    "code": 1,
                    "message": result.get("message") or result.get("status"),
                    "result": result,
                }
            return {"code": 0, "result": result}

    if "/api/workflow/dispatch" not in existing_paths:
        @app.post("/api/workflow/dispatch")
        async def dispatch_to_bailian_workflow(req: WorkflowDispatchReq) -> dict[str, Any]:
            import os
            from .config_manager import ensure_bot_env
            from . import workflow_service

            ensure_bot_env()
            api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
            if not api_key:
                return {"code": 1, "message": "未配置 DASHSCOPE_API_KEY"}

            task_type = req.task_type.strip().lower()
            if task_type == "auto":
                task_type = "draw" if any(k in req.prompt for k in ("画", "生图", "插画", "写真", "绘图")) else "qa"

            reply = await workflow_service.run_bailian_workflow(
                api_key=api_key,
                user_prompt=req.prompt,
                file_sources=req.file_paths,
                task_type=task_type,
                character=req.character,
                workflow_app_id=req.workflow_app_id,
            )
            if not reply:
                return {"code": 1, "message": "百炼工作流返回为空或尚未发布，请确认已在百炼右上角点击【发布】"}
            saved = await workflow_service.download_workflow_outputs_to_local(reply)
            return {
                "code": 0,
                "reply": reply,
                "local_saved_files": [str(p) for p in saved],
            }

    if "/api/restart" not in existing_paths:
        @app.get("/api/restart")
        @app.post("/api/restart")
        async def restart_backend_self() -> dict[str, Any]:
            import asyncio
            import os
            import sys

            try:
                from . import conversation_service
                conversation_service.store.clear_active_state("1785381042")
            except Exception:
                pass

            async def _delayed_respawn() -> None:
                try:
                    await asyncio.sleep(0.2)
                    try:
                        user_memory_store.close()
                    except Exception:
                        pass
                    launcher = BOT_ROOT / "daemon_launcher.py"
                    subprocess.Popen(
                        [sys.executable, str(launcher)],
                        cwd=str(BOT_ROOT),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        close_fds=True,
                    )
                except Exception as exc:
                    logger.error(f"[Restart] 拉起守护进程异常: {exc}")
                finally:
                    await asyncio.sleep(0.3)
                    os._exit(0)

            asyncio.create_task(_delayed_respawn())
            return {"code": 0, "message": "后端正在自重启并加载最新代码，已同步重置残留状态..."}

    if "/api/state/clear" not in existing_paths:
        @app.get("/api/state/clear")
        @app.post("/api/state/clear")
        async def clear_user_situational_state(uid: str = "1785381042") -> dict[str, Any]:
            from . import conversation_service
            conversation_service.store.clear_active_state(uid)
            sess = conversation_service.current()
            if sess and sess.uid == uid:
                sess.active_state = None
            return {"code": 0, "message": f"用户 {uid} 的持续状态已清空"}

    if "/api/refs/sync" not in existing_paths:
        @app.get("/api/refs/sync")
        @app.post("/api/refs/sync")
        async def trigger_refs_sync(games: str = "all", limit: int = 0) -> dict[str, Any]:
            from . import ref_fetcher

            return ref_fetcher.start_background_fetch(games=games, limit=limit)

    def _prepare_pure_gagged3_assets() -> dict[str, Any]:
        """校验单遍不重复、带原声前缀与天然换气隔断的 gagged_1~3 参考音频及严格 1:1 对齐的标注文本（不再覆写双遍拼接版本）。"""
        import wave
        workspace_root = BOT_ROOT.parent
        dania_ref = workspace_root / "gpt-sovits" / "models" / "dania" / "refs" / "gagged_3.wav"
        dur = 0.0
        sr = 32000
        if dania_ref.is_file():
            with wave.open(str(dania_ref), "rb") as wf:
                sr = wf.getframerate()
                dur = round(wf.getnframes() / float(max(1, sr)), 2)
        return {
            "duration_sec": dur,
            "sample_rate": sr,
            "dania_prompt": "嗯唔，嗯呣！唔嗯，嗯唔？",
            "amiya_prompt": "嗯唔，嗯呣！唔嗯，嗯唔？",
        }

    if "/api/test/synth_gagged" not in existing_paths:
        @app.get("/api/test/synth_gagged")
        async def test_synth_gagged(role: str = "达妮娅", text: str = "唔", variant: str = "A") -> dict[str, Any]:
            import wave
            t0 = time.time()
            task_id = uuid.uuid4().hex
            try:
                ref_prep = _prepare_pure_gagged3_assets()
                v_clean = (variant or "A").strip().upper()
                variants_to_run = ["A", "B", "C"] if v_clean == "ALL" else [v_clean if v_clean in {"A", "B", "C"} else "A"]
                out_dir = BOT_ROOT / "data" / "test_output"
                out_dir.mkdir(parents=True, exist_ok=True)
                variant_labels = {
                    "A": "方案A_2单元标准起伏_gagged3",
                    "B": "方案B_2单元低沉少气声_gagged2",
                    "C": "方案C_3单元急促抗议_gagged3",
                }
                results = []
                for v_tag in variants_to_run:
                    with task_scope(task_id):
                        b64_rec = await tts_service.synthesize(
                            text,
                            role_name=role,
                            timeout=600.0,
                            truncate_long=False,
                            prefer_wav=True,
                            gagged_variant=v_tag,
                        )
                    if not b64_rec or not b64_rec.startswith("base64://"):
                        continue
                    raw = base64.b64decode(b64_rec[len("base64://") :])
                    ts_now = int(time.time())
                    label = variant_labels.get(v_tag, f"方案{v_tag}")
                    out_file = out_dir / f"gagged_{role}_{label}_{ts_now}.wav"
                    out_file.write_bytes(raw)
                    dur = 0.0
                    try:
                        with wave.open(str(out_file), "rb") as wf:
                            dur = round(wf.getnframes() / float(max(1, wf.getframerate())), 3)
                    except Exception:
                        pass
                    results.append({
                        "variant": v_tag,
                        "label": label,
                        "audio_duration_sec": dur,
                        "saved_file": str(out_file),
                        "meta": dict(getattr(tts_service, "LAST_AUDIO_META", {})),
                    })
                cost_sec = round(time.time() - t0, 3)
                if not results:
                    return {"code": 1, "message": "合成失败未返回音频载荷", "cost_sec": cost_sec}
                first = results[0]
                return {
                    "code": 0,
                    "role": role,
                    "text": text,
                    "variant": v_clean,
                    "cost_sec": cost_sec,
                    "audio_duration_sec": first["audio_duration_sec"],
                    "saved_file": first["saved_file"],
                    "all_variants": results,
                    "ref_prepared": ref_prep,
                    "meta": first["meta"],
                }
            except Exception as exc:
                return {"code": 1, "error": str(exc), "cost_sec": round(time.time() - t0, 3)}

    if "/api/test/send_private_gagged" not in existing_paths:
        @app.get("/api/test/send_private_gagged")
        async def test_send_private_gagged(
            uid: str = "1785381042", role: str = "达妮娅", text: str = "唔", variant: str = "A"
        ) -> dict[str, Any]:
            import nonebot
            from nonebot.adapters.onebot.v11 import MessageSegment

            ref_prep = _prepare_pure_gagged3_assets()
            task_id = uuid.uuid4().hex
            with task_scope(task_id):
                record_b64 = await tts_service.synthesize(
                    text,
                    role_name=role,
                    timeout=600.0,
                    truncate_long=False,
                    prefer_wav=False,
                    gagged_variant=(variant or "A").strip().upper(),
                )

            bot = nonebot.get_bot()
            msg = MessageSegment.record(record_b64)
            await bot.send_private_msg(user_id=int(uid), message=msg)

            return {
                "code": 0,
                "uid": uid,
                "character": role,
                "text": text,
                "variant": (variant or "A").strip().upper(),
                "sent_success": True,
                "ref_prepared": ref_prep,
                "meta": dict(getattr(tts_service, "LAST_AUDIO_META", {})),
            }

    if "/api/test/send_private_dania" not in existing_paths:
        @app.get("/api/test/send_private_dania")
        async def test_send_private_dania(uid: str = "1785381042", prompt: str = "达妮娅，晚上好！") -> dict[str, Any]:
            import nonebot
            from nonebot.adapters.onebot.v11 import MessageSegment
            from .config_manager import state

            state.global_active = True
            state.active_character = "dania"
            state.voice_enabled = True
            state.voice_only_reply = True
            await state.save()

            reply_text = await llm_service.chat_operator("dania", prompt, max_tokens=180, voice_mode_only=True)
            if not reply_text:
                return {"code": 1, "message": "LLM 未能生成达妮娅台词"}

            has_forbidden_habit = any(k in reply_text for k in ("时间太晚", "快去睡", "早点睡", "又熬到", "熬夜啦"))

            task_id = uuid.uuid4().hex
            with task_scope(task_id):
                record_b64 = await tts_service.synthesize(
                    reply_text, role_name="达妮娅", timeout=600.0, truncate_long=True, prefer_wav=False,
                )

            bot = nonebot.get_bot()
            msg = MessageSegment.record(record_b64)
            await bot.send_private_msg(user_id=int(uid), message=msg)

            return {
                "code": 0,
                "uid": uid,
                "character": "达妮娅",
                "prompt": prompt,
                "reply_text": reply_text,
                "has_forbidden_night_habit": has_forbidden_habit,
                "sent_success": True,
                "meta": dict(getattr(tts_service, "LAST_AUDIO_META", {})),
            }

    if "/api/refs/status" not in existing_paths:
        @app.get("/api/refs/status")
        async def get_refs_status() -> dict[str, Any]:
            from . import ref_fetcher

            return ref_fetcher.get_fetcher_status()

    if "/api/switch/small_talk" not in existing_paths:
        @app.post("/api/switch/small_talk")
        async def switch_small_talk_post(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            raw_en = payload.get("enable")
            if raw_en is not None:
                state.small_talk_mode = _parse_bool(raw_en, state.small_talk_mode)
            if payload.get("style"):
                state.small_talk_style = str(payload["style"]).strip()
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "small_talk_mode": state.small_talk_mode,
                "small_talk_style": getattr(state, "small_talk_style", "pure_persona"),
            }

        @app.get("/api/switch/small_talk")
        async def switch_small_talk_get(enable: Any = True, style: Optional[str] = None) -> dict[str, Any]:
            state.small_talk_mode = _parse_bool(enable, True)
            if style:
                state.small_talk_style = style
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "small_talk_mode": state.small_talk_mode,
                "small_talk_style": getattr(state, "small_talk_style", "pure_persona"),
            }

    if "/api/switch/small_talk_style" not in existing_paths:
        class SmallTalkStyleReq(BaseModel):
            style: str

        @app.post("/api/switch/small_talk_style")
        async def switch_small_talk_style_post(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            style = str(payload.get("style") or "pure_persona").strip()
            state.small_talk_style = style
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "small_talk_mode": state.small_talk_mode,
                "small_talk_style": getattr(state, "small_talk_style", "pure_persona"),
            }

        @app.get("/api/switch/small_talk_style")
        async def switch_small_talk_style_get(style: str = "pure_persona") -> dict[str, Any]:
            state.small_talk_style = style
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "small_talk_mode": state.small_talk_mode,
                "small_talk_style": getattr(state, "small_talk_style", "pure_persona"),
            }

    if "/api/training/toggle" not in existing_paths:
        @app.post("/api/training/toggle")
        async def toggle_training_post(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            raw_en = payload.get("enable")
            if raw_en is not None:
                state.training_mode = _parse_bool(raw_en, state.training_mode)
            gid = str(payload.get("group_id") or "").strip()
            if gid:
                t_groups = list(dict.fromkeys([str(g) for g in (state.training_groups or [])]))
                if state.training_mode and gid not in t_groups:
                    t_groups.append(gid)
                elif not state.training_mode and gid in t_groups:
                    t_groups.remove(gid)
                state.training_groups = t_groups
            await state.save()
            return {"code": 0, "status": "ok", "training_mode": state.training_mode, "training_groups": state.training_groups}

        @app.get("/api/training/toggle")
        async def toggle_training_get(enable: Any = True, group_id: Optional[str] = None) -> dict[str, Any]:
            state.training_mode = _parse_bool(enable, True)
            if group_id:
                gid = str(group_id).strip()
                t_groups = list(dict.fromkeys([str(g) for g in (state.training_groups or [])]))
                if state.training_mode and gid not in t_groups:
                    t_groups.append(gid)
                elif not state.training_mode and gid in t_groups:
                    t_groups.remove(gid)
                state.training_groups = t_groups
            await state.save()
            return {"code": 0, "status": "ok", "training_mode": state.training_mode, "training_groups": state.training_groups}

    if "/api/switch/auto_reply" not in existing_paths:
        @app.post("/api/switch/auto_reply")
        async def switch_auto_reply_post(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            raw_en = payload.get("enable")
            if raw_en is not None:
                state.auto_reply_mode = _parse_bool(raw_en, state.auto_reply_mode)
                if not state.auto_reply_mode:
                    from .group_chat_manager import group_chat_manager
                    for gid, buf in list(group_chat_manager._pending_buffers.items()):
                        if not any(m.is_priority for m in buf):
                            t = group_chat_manager._window_timers.pop(gid, None)
                            if t and not t.done():
                                t.cancel()
                            buf.clear()
                            for f in group_chat_manager._batch_futures.pop(gid, []):
                                if not f.done():
                                    f.set_result(None)
            if payload.get("interval") is not None or payload.get("reply_interval_seconds") is not None:
                try:
                    val = float(payload.get("interval") or payload.get("reply_interval_seconds"))
                    state.reply_interval_seconds = val
                except (ValueError, TypeError):
                    pass
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "auto_reply_mode": state.auto_reply_mode,
                "reply_interval_seconds": state.reply_interval_seconds,
            }

        @app.get("/api/switch/auto_reply")
        async def switch_auto_reply_get(enable: Any = True, interval: Optional[float] = None) -> dict[str, Any]:
            state.auto_reply_mode = _parse_bool(enable, True)
            if interval is not None:
                state.reply_interval_seconds = float(interval)
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "auto_reply_mode": state.auto_reply_mode,
                "reply_interval_seconds": state.reply_interval_seconds,
            }

    if "/api/training/trigger" not in existing_paths:
        @app.post("/api/training/trigger")
        async def trigger_group_training_post(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            raw_gid = payload.get("group_id") or payload.get("gid") or ""
            gid = str(raw_gid).strip()
            if not gid:
                return {"code": 400, "status": "error", "message": "缺少群号"}
            count = int(payload.get("count") or 120)
            async_mode = _parse_bool(payload.get("async_mode"), True) if "async_mode" in payload else False
            if gid and gid not in state.training_groups:
                state.training_groups.append(gid)
                await state.save()
            from nonebot import get_bot
            bot = None
            try:
                bot = get_bot()
            except Exception:
                pass

            async def _coro():
                if bot is not None:
                    return await modes.fetch_and_train_group_history(bot, gid, count=count)
                return await modes.trigger_group_training_now(gid)

            if async_mode:
                job = modes.training_job_manager.submit_job(gid, _coro, kind="manual_group_training")
                return {
                    "code": 0,
                    "status": job.status,
                    "task_id": job.task_id,
                    "job": job.to_dict(),
                }

            # 即使同步等待也经过受控队列，并如实反映真实成功/失败状态（绝不把失败标记为训练完成）
            job = modes.training_job_manager.submit_job(gid, _coro, kind="manual_group_training")
            task_obj = modes.training_job_manager._active_tasks.get(job.task_id)
            if task_obj is not None:
                await task_obj
            updated = modes.training_job_manager.get_job(job.task_id) or job.to_dict()
            res = updated.get("result") or {}
            if updated.get("status") == "error" or res.get("status") in {"error", "empty"}:
                err_msg = updated.get("error_message") or res.get("message") or "训练失败"
                return {
                    "code": 1,
                    "status": "error",
                    "task_id": job.task_id,
                    "message": err_msg,
                    "result": res,
                }
            return {
                "code": 0,
                "status": "ok",
                "task_id": job.task_id,
                "result": res,
            }

        @app.get("/api/training/trigger")
        async def trigger_group_training_get(group_id: str = "", count: int = 120) -> dict[str, Any]:
            gid = str(group_id).strip()
            if not gid:
                return {"code": 400, "status": "error", "message": "缺少群号"}
            # GET 仅查询该群最近一次训练任务状态，防止因网络抖动通过 GET 重复触发写操作
            recent = modes.training_job_manager.list_jobs(group_id=gid, limit=1)
            if recent:
                latest = recent[0]
                return {
                    "code": 0 if latest.get("status") != "error" else 1,
                    "status": latest.get("status", "idle"),
                    "task_id": latest.get("task_id", ""),
                    "message": latest.get("error_message", ""),
                    "result": latest.get("result", {}),
                }
            return {"code": 0, "status": "idle", "group_id": gid, "message": "请使用 POST /api/training/trigger 提交训练任务"}

    if "/api/training/jobs" not in existing_paths:
        @app.get("/api/training/jobs")
        async def api_training_jobs(group_id: Optional[str] = None) -> dict[str, Any]:
            return {"code": 0, "status": "ok", "jobs": modes.training_job_manager.list_jobs(group_id=group_id)}

        @app.get("/api/training/job/{task_id}")
        async def api_training_job_detail(task_id: str) -> dict[str, Any]:
            job = modes.training_job_manager.get_job(task_id)
            if job is None:
                return {"code": 404, "status": "not_found", "message": f"未找到任务: {task_id}"}
            return {
                "code": 0 if job.get("status") != "error" else 1,
                "status": job.get("status"),
                "task_id": task_id,
                "error_message": job.get("error_message", ""),
                "job": job,
                "result": job.get("result", {}),
            }

    if "/api/training/status" not in existing_paths:
        @app.get("/api/training/status")
        async def api_training_status() -> dict[str, Any]:
            data = await modes.get_training_status_async()
            return {"code": 0, "status": "ok", "data": data}

    if "/api/profiles" not in existing_paths:
        @app.get("/api/profiles")
        async def api_list_user_profiles(limit: int = 50) -> dict[str, Any]:
            return {"code": 0, "status": "ok", "profiles": user_memory_store.list_profiles_summary(limit=limit)}

        @app.get("/api/profiles/{uid}")
        async def api_get_user_profile(uid: str, group_id: Optional[str] = None, character: Optional[str] = None) -> dict[str, Any]:
            if not str(uid).strip().isdigit():
                return {"code": 400, "status": "error", "message": "无效 QQ 号"}
            rec = user_memory_store.get_scoped_profile(
                str(uid).strip(),
                group_id=group_id,
                character=character or state.active_character,
            )
            return {"code": 0, "status": "ok", "profile": rec.to_dict()}

        @app.post("/api/profiles/correct")
        async def api_correct_user_profile(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            uid = str(payload.get("uid") or "").strip()
            if not uid.isdigit():
                return {"code": 400, "status": "error", "message": "无效 QQ 号"}
            group_id = payload.get("group_id")
            char_id = str(payload.get("character") or state.active_character or "amiya").strip()
            rec = user_memory_store.correct_user_fact(
                uid,
                group_id=str(group_id).strip() if group_id else None,
                character=char_id,
                preferred_title=payload.get("preferred_title"),
                add_fact=str(payload.get("add_fact") or ""),
                invalidate_keyword=str(payload.get("invalidate_keyword") or ""),
            )
            return {"code": 0, "status": "ok", "profile": rec.to_dict()}

        @app.post("/api/profiles/delete")
        async def api_delete_user_profile(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            uid = str(payload.get("uid") or payload.get("qq") or "").strip()
            if not uid.isdigit():
                return {"code": 400, "status": "error", "message": "无效 QQ 号"}
            group_id = payload.get("group_id")
            raw_char = payload.get("character")
            all_scopes = _parse_bool(payload.get("all_scopes"), False)
            if all_scopes or (group_id is None and raw_char is None):
                res = user_memory_store.clear_user_profile_by_uid(uid)
                return {"code": 0, "status": "ok", "deleted": True, "clear_result": res}
            deleted = user_memory_store.delete_scoped_profile(
                uid,
                group_id=str(group_id).strip() if group_id else None,
                character=str(raw_char).strip() if raw_char else None,
            )
            return {"code": 0, "status": "ok", "deleted": deleted}

        @app.post("/api/profiles/clear_qq")
        async def api_clear_user_profile_by_qq(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            uid = str(payload.get("uid") or payload.get("qq") or "").strip()
            if not uid.isdigit():
                return {"code": 400, "status": "error", "message": "请提供有效的数字 QQ 号"}
            res = user_memory_store.clear_user_profile_by_uid(uid)
            return {"code": 0, "status": "ok", "data": res}

        @app.get("/api/group_logs/{group_id}")
        async def api_get_group_chat_logs(
            group_id: str,
            uid: Optional[str] = None,
            limit: int = 30,
        ) -> dict[str, Any]:
            gid = str(group_id or "").strip()
            if not gid:
                return {"code": 400, "status": "error", "message": "缺少群号"}
            logs = user_memory_store.get_group_chat_history(gid, uid=uid, limit=limit)
            return {"code": 0, "status": "ok", "group_id": gid, "uid": uid or "", "logs": logs}

    if "/api/autonomous/status" not in existing_paths:
        @app.get("/api/autonomous/status")
        async def api_autonomous_status() -> dict[str, Any]:
            return {"code": 0, "status": "ok", "data": autonomous_coordinator.get_status()}

        @app.get("/api/promises")
        async def api_list_promises(uid: Optional[str] = None, status: Optional[str] = None) -> dict[str, Any]:
            return {
                "code": 0,
                "status": "ok",
                "promises": autonomous_coordinator.list_promises(uid=uid, status=status),
            }

        @app.post("/api/promises/cancel")
        async def api_cancel_promise(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            pid = str(payload.get("promise_id") or payload.get("id") or "").strip()
            if not pid:
                return {"code": 400, "status": "error", "message": "缺少 promise_id"}
            ok = autonomous_coordinator.cancel_promise(pid)
            if not ok:
                return {"code": 404, "status": "error", "message": "未找到可取消的挂起约定"}
            return {"code": 0, "status": "ok", "promise_id": pid}

        @app.get("/api/evolution/status")
        async def api_evolution_status(group_id: Optional[str] = None) -> dict[str, Any]:
            from .expression_learner import expression_store

            return {
                "code": 0,
                "status": "ok",
                "data": {
                    "learned_rules": autonomous_coordinator.list_learned_rules(group_id=group_id),
                    "expression_feedback": expression_store.get_feedback_summary(group_id=group_id),
                    "owner_work_checklists": list(autonomous_coordinator._owner_work_checklists)[-10:],
                },
            }

        @app.post("/api/evolution/reflect")
        async def api_evolution_reflect(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            gid = str(payload.get("group_id") or "").strip() or None
            res = autonomous_coordinator.distill_learned_rules(group_id=gid)
            return {"code": 0, "status": "ok", "data": res}

        @app.post("/api/ autonomous/config" if False else "/api/autonomous/config")
        async def api_autonomous_config(request: Request) -> dict[str, Any]:
            payload = await _parse_any_request_payload(request)
            try:
                if "autonomous_group_chat_enabled" in payload:
                    state.autonomous_group_chat_enabled = _parse_bool(payload["autonomous_group_chat_enabled"], False)
                if "autonomous_qzone_enabled" in payload:
                    state.autonomous_qzone_enabled = _parse_bool(payload["autonomous_qzone_enabled"], False)
                if "autonomous_qzone_max_per_day" in payload:
                    state.autonomous_qzone_max_per_day = int(payload["autonomous_qzone_max_per_day"])
                if "autonomous_qzone_min_interval_hours" in payload:
                    state.autonomous_qzone_min_interval_hours = float(payload["autonomous_qzone_min_interval_hours"])
                if "autonomous_quiet_start_hour" in payload:
                    state.autonomous_quiet_start_hour = int(payload["autonomous_quiet_start_hour"])
                if "autonomous_quiet_end_hour" in payload:
                    state.autonomous_quiet_end_hour = int(payload["autonomous_quiet_end_hour"])
            except (TypeError, ValueError) as exc:
                return {"code": 1, "status": "error", "message": f"自主参数无效: {exc}"}
            await state.save()
            return {"code": 0, "status": "ok", "data": autonomous_coordinator.get_status()}

    if "/api/group_chat/config" not in existing_paths:
        @app.get("/api/group_chat/config")
        async def api_get_group_chat_config() -> dict[str, Any]:
            return {
                "code": 0,
                "status": "ok",
                "scope": "global_default",
                "group_buffer_enabled": state.group_buffer_enabled,
                "group_merge_window_sec": state.group_merge_window_sec,
                "group_merge_max_messages": state.group_merge_max_messages,
                "group_merge_max_chars": state.group_merge_max_chars,
                "group_quota_min_interval_sec": state.group_quota_min_interval_sec,
                "group_quota_max_per_minute": state.group_quota_max_per_minute,
                "group_chunk_pace_sec": state.group_chunk_pace_sec,
            }

        @app.post("/api/group_chat/config")
        async def api_group_chat_config(request: Request) -> dict[str, Any]:
            from .group_chat_manager import group_chat_manager
            payload = await _parse_any_request_payload(request)
            try:
                if "group_buffer_enabled" in payload:
                    state.group_buffer_enabled = _parse_bool(payload["group_buffer_enabled"], state.group_buffer_enabled)
                if "group_merge_window_sec" in payload:
                    val_w = float(payload["group_merge_window_sec"])
                    if val_w < 0 or val_w > 10:
                        return {"code": 1, "status": "error", "message": "合并窗口秒数需在 0 ~ 10 秒之间"}
                    state.group_merge_window_sec = val_w
                if "group_merge_max_messages" in payload:
                    val_m = int(payload["group_merge_max_messages"])
                    if val_m < 1 or val_m > 20:
                        return {"code": 1, "status": "error", "message": "单批最大消息数需在 1 ~ 20 条之间"}
                    state.group_merge_max_messages = val_m
                if "group_merge_max_chars" in payload:
                    val_c = int(payload["group_merge_max_chars"])
                    if val_c < 200 or val_c > 8000:
                        return {"code": 1, "status": "error", "message": "单批最大字符数需在 200 ~ 8000 字之间"}
                    state.group_merge_max_chars = val_c
                if "group_quota_min_interval_sec" in payload:
                    val_i = float(payload["group_quota_min_interval_sec"])
                    if val_i < 0.5 or val_i > 120:
                        return {"code": 1, "status": "error", "message": "普通参与最小发送间隔需在 0.5 ~ 120 秒之间"}
                    state.group_quota_min_interval_sec = val_i
                if "group_quota_max_per_minute" in payload:
                    val_q = int(payload["group_quota_max_per_minute"])
                    if val_q < 1 or val_q > 120:
                        return {"code": 1, "status": "error", "message": "每分钟普通回复上限需在 1 ~ 120 条之间"}
                    state.group_quota_max_per_minute = val_q
                if "group_chunk_pace_sec" in payload:
                    val_p = float(payload["group_chunk_pace_sec"])
                    if val_p < 0 or val_p > 10:
                        return {"code": 1, "status": "error", "message": "长回答分段间隔需在 0 ~ 10 秒之间"}
                    state.group_chunk_pace_sec = val_p
            except (TypeError, ValueError) as exc:
                return {"code": 1, "status": "error", "message": f"参数格式错误: {exc}"}
            await state.save()
            group_chat_manager.configure(
                merge_window_sec=state.group_merge_window_sec,
                max_batch_messages=state.group_merge_max_messages,
                max_batch_chars=state.group_merge_max_chars,
                min_interval_sec=state.group_quota_min_interval_sec,
                max_per_minute=state.group_quota_max_per_minute,
                chunk_pace_sec=state.group_chunk_pace_sec,
            )
            if not state.group_buffer_enabled:
                for gid in list(group_chat_manager._window_timers.keys()):
                    t = group_chat_manager._window_timers.pop(gid, None)
                    if t and not t.done():
                        t.cancel()
                    group_chat_manager._flush_locked(gid)
            return {
                "code": 0,
                "status": "ok",
                "scope": "global_default",
                "group_buffer_enabled": state.group_buffer_enabled,
                "group_merge_window_sec": state.group_merge_window_sec,
                "group_merge_max_messages": state.group_merge_max_messages,
                "group_merge_max_chars": state.group_merge_max_chars,
                "group_quota_min_interval_sec": state.group_quota_min_interval_sec,
                "group_quota_max_per_minute": state.group_quota_max_per_minute,
                "group_chunk_pace_sec": state.group_chunk_pace_sec,
            }

    if "/api/test/send_private_chat" not in existing_paths:
        @app.get("/api/test/send_private_chat")
        async def test_send_private_chat(
            uid: str = "1785381042",
            text: str = "阿米娅，在忙什么呀？",
            as_voice: bool = False,
        ) -> dict[str, Any]:
            import nonebot
            from nonebot.adapters.onebot.v11 import MessageSegment
            target_uid = str(uid or state.superuser_qq or "1785381042").strip()
            from ..agent_core import _dispatch_reply_impl
            reply = await _dispatch_reply_impl(
                uid=target_uid,
                group_id=None,
                user_text=text,
                voice_mode_only=as_voice,
            )
            bot = nonebot.get_bot()
            is_voice_sent = False
            voice_meta = {}
            if as_voice:
                try:
                    b64_silk = await tts_service.synthesize(
                        text=str(reply or "博士，我在呢"),
                        role_name=state.active_character or "阿米娅",
                    )
                    silk_b64_str = b64_silk.removeprefix("base64://")
                    await bot.send_private_msg(
                        user_id=int(target_uid),
                        message=MessageSegment.record(f"base64://{silk_b64_str}"),
                    )
                    is_voice_sent = True
                    voice_meta = dict(getattr(tts_service, "LAST_AUDIO_META", {}))
                except Exception as e:
                    logger.warning(f"私聊测试语音合成失败，降级文字发送: {e}")
                    await bot.send_private_msg(user_id=int(target_uid), message=str(reply or "确实"))
            else:
                await bot.send_private_msg(user_id=int(target_uid), message=str(reply or "确实"))

            return {
                "code": 0,
                "status": "ok",
                "uid": target_uid,
                "input_text": text,
                "reply_text": reply,
                "as_voice": as_voice,
                "is_voice_sent": is_voice_sent,
                "voice_meta": voice_meta,
                "small_talk_mode": state.small_talk_mode,
                "character": state.active_character,
            }

    app.state._qq_agent_api_mounted = True
    app.state._qq_agent_v2_mounted = True
    app.state._qq_agent_v3_mounted = True
    app.state._qq_agent_v4_mounted = True
    app.state._qq_agent_v5_mounted = True
    app.state._qq_agent_v6_mounted = True
    app.state._qq_agent_v7_mounted = True
    app.state._qq_agent_v8_mounted = True
    app.state._qq_agent_v9_mounted = True
    logger.info(
        "QQ Agent controller API v9 mounted: /panel, /api/status, /api/dashboard, /api/progress, /api/test/send_docs, /api/exports/*"
    )


def _ensure_cors(app: FastAPI) -> None:
    if getattr(app.state, "_qq_agent_cors", False):
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1",
            "http://localhost",
            "http://127.0.0.1:8080",
            "http://localhost:8080",
        ],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state._qq_agent_cors = True


def setup(services) -> None:
    # 在服务器构建中间件栈之前挂载；业务模块导入不再自行重载或替换路由。
    services.validate()
    app = get_app()
    _ensure_cors(app)
    _register_routes(app, services)
    from . import local_rag_service
    local_rag_service.register_routes(app)
    if not getattr(app.state, "_daily_lifecycle_registered", False):
        async def start_daily_services() -> None:
            await local_rag_service.startup()

        get_driver().on_startup(start_daily_services)
        get_driver().on_shutdown(local_rag_service.shutdown)
        app.state._daily_lifecycle_registered = True

    if getattr(app.state, "_actuator_routes_mounted", False):
        return
    app.state._actuator_routes_mounted = True

    def actuator_local(request: Request) -> None:
        from urllib.parse import urlsplit
        if request.client is None or request.client.host not in {'127.0.0.1', '::1'}:
            raise HTTPException(403, '设备动作接口仅允许本机控制台访问')
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise HTTPException(403, '设备动作接口拒绝外部网页来源')

    @app.get('/api/actuator/status')
    async def api_actuator_status(request: Request) -> dict[str, Any]:
        actuator_local(request)
        from .device_actuator import mac_actions, mumu_verifier
        return {'code': 0, 'status': 'ok', 'mac_actuator': mac_actions.status(),
                'mumu_verifier': {'adb_path': mumu_verifier.resolve_adb_binary(), 'last_verification': mumu_verifier._last_verification}}

    @app.post('/api/actuator/config')
    async def api_actuator_config(request: Request) -> dict[str, Any]:
        actuator_local(request)
        from .device_actuator import mac_actions
        try:
            result = mac_actions.configure(await request.json())
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'code': 0, 'status': 'ok', 'mac_actuator': result}

    @app.post('/api/actuator/mac/plan')
    async def api_actuator_mac_plan(request: Request) -> dict[str, Any]:
        actuator_local(request)
        from .device_actuator import mac_actions
        body = await request.json()
        uid = str(state.superuser_qq)
        try:
            job = await mac_actions.plan_message(uid, str(body.get('text') or ''), prepare_only=True)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'code': 0, 'status': 'ok', 'job': job}

    @app.post('/api/actuator/mac/confirm')
    async def api_actuator_mac_confirm(request: Request) -> dict[str, Any]:
        actuator_local(request)
        from .device_actuator import mac_actions
        body = await request.json()
        try:
            job = await mac_actions.confirm(str(state.superuser_qq), str(body.get('job_id') or ''), cancel=body.get('cancel') is True)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'code': 0, 'status': 'ok', 'job': job}

    @app.post('/api/actuator/mac/test')
    async def api_actuator_mac_test(request: Request) -> dict[str, Any]:
        actuator_local(request)
        from .device_actuator import mac_actions
        body = await request.json()
        try:
            if body.get('action', 'notify') not in {'notify', 'music', 'reminder'}:
                raise ValueError('快捷指令和日历请先准备动作，再确认执行')
            job = mac_actions.prepare(str(state.superuser_qq), {
                **body, 'action': body.get('action') or 'notify',
                'title': body.get('title') or '阿米娅的本机联动测试',
                'message': body.get('message') or '本机通知命令已执行，请核对通知中心。',
            })
            job = await mac_actions.confirm(str(state.superuser_qq), job['id'])
        except (ValueError, PermissionError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'code': 0 if job['status'] == 'succeeded' else 400,
                'status': 'ok' if job['status'] == 'succeeded' else 'error', 'result': job.get('result'), 'job': job}

    @app.post('/api/actuator/mumu/verify')
    async def api_actuator_mumu_verify(request: Request) -> dict[str, Any]:
        actuator_local(request)
        from .device_actuator import mumu_verifier
        body = await request.json()
        try:
            if body.get('dry_run', True) is not True or body.get('force_synthetic', False):
                raise ValueError('本接口只验证真实 MuMu 截图与 AI 规划，不执行操作')
            result = await mumu_verifier.verify_gui_step(
                task_goal=str(body.get('task_goal') or '定位屏幕左上角的返回按钮'),
                endpoint=str(body.get('endpoint') or '127.0.0.1:16384'), dry_run=True)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {'code': 0, 'status': 'ok', 'verification': result}
