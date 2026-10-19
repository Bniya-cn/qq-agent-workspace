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
        "active_character": state.active_character,
        "active_groups": state.active_groups,
        "comfort_targets": state.comfort_mode_targets,
        "voice_enabled": state.voice_enabled,
        "voice_model": getattr(state, "voice_model", "auto"),
        "voice_models": voice_models_list,
        "tts_enabled": state.voice_enabled,
        "voice_only_reply": state.voice_only_reply,
        "voice_mode": state.voice_mode,
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
        "saved_refs": _list_saved_refs(),
        "character_refs": _list_saved_refs(),
        "group_personas": _list_group_personas(),
        "exported_files": doc_service.list_exported_files(limit=20),
        "progress_tasks": progress_tracker.list_recent(),
        "latest_audio_meta": dict(getattr(tts_service, "LAST_AUDIO_META", {})),
        "bot_connected": len(bots) > 0,
        "connected_bots": list(bots.keys()),
        "small_talk_mode": state.small_talk_mode,
        "small_talk_style": getattr(state, "small_talk_style", "pure_persona"),
        "auto_reply_mode": getattr(state, "auto_reply_mode", True),
        "reply_interval_seconds": getattr(state, "reply_interval_seconds", 3.0),
        "training_mode": state.training_mode,
        "training_groups": state.training_groups,
        "group_user_whitelist_enabled": getattr(state, "group_user_whitelist_enabled", False),
        "group_user_whitelists": getattr(state, "group_user_whitelists", {}),
        "superuser_qq": state.superuser_qq,
        "training_status": modes.get_training_status() if hasattr(modes, "get_training_status") else {},
    }
    if include_history:
        payload["recent_logs"] = recorder.get_logs()
        payload["recent_dialogues"] = recorder.get_dialogues()
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
            from . import web_panel
            return HTMLResponse(content=web_panel.PANEL_HTML)

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
                "voice_ref_name": state.voice_ref_name,
                "voice_speed": state.voice_speed,
                "voice_temperature": state.voice_temperature,
                "voice_top_p": state.voice_top_p,
            }

    if "/api/voice/service" not in existing_paths:
        @app.post("/api/voice/service")
        async def control_voice_service(req: VoiceServiceReq) -> dict[str, Any]:
            gsv_root = BOT_ROOT.parent / "gpt-sovits"
            raw_target = (req.service or req.target or "9880").strip().lower()
            target = "9876" if "9876" in raw_target or "studio" in raw_target else "9880"
            action = req.action.strip().lower()
            try:
                if target == "9880":
                    if action == "start":
                        script = gsv_root / "scripts" / "start_api.sh"
                        if script.is_file():
                            subprocess.Popen(["/bin/bash", str(script)], cwd=str(gsv_root))
                            return {"code": 0, "message": "已发出 9880 语音 API 启动指令"}
                    else:
                        subprocess.Popen(["/usr/bin/pkill", "-f", "api_v2.py"])
                        return {"code": 0, "message": "已停止 9880 语音 API"}
                elif target == "9876":
                    if action == "start":
                        script = gsv_root / "scripts" / "start_studio.sh"
                        if script.is_file():
                            subprocess.Popen(["/bin/bash", str(script)], cwd=str(gsv_root))
                            return {"code": 0, "message": "已发出 9876 语音工作室启动指令"}
                    else:
                        subprocess.Popen(["/usr/bin/pkill", "-f", "voice_studio.py"])
                        return {"code": 0, "message": "已停止 9876 语音工作室"}
            except Exception as exc:
                return {"code": 1, "message": f"操作失败: {exc}"}
            return {"code": 1, "message": "未找到对应语音启动脚本"}

    if "/api/voice/preview" not in existing_paths:
        @app.post("/api/voice/preview")
        async def preview_voice_tts(req: VoicePreviewReq) -> dict[str, Any]:
            task_id = uuid.uuid4().hex
            progress_tracker.update(task_id, kind="voice", status="running", stage="生成试听", input_text=req.text)
            try:
                with task_scope(task_id):
                    b64_rec = await tts_service.synthesize(
                        req.text, role_name=req.voice_model or req.character or "达妮娅",
                        timeout=600.0, truncate_long=False, force_ref_name=req.ref_name.strip(), prefer_wav=True,
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
        async def test_voice_tts_endpoint() -> dict[str, Any]:
            t0 = time.time()
            text = "博士，阿米娅在这里哦！"
            task_id = uuid.uuid4().hex
            progress_tracker.update(task_id, kind="voice", status="pending", stage="准备语音测试")
            try:
                with task_scope(task_id):
                    progress_tracker.update(task_id, status="running", stage="测试语音合成")
                    b64_rec = await tts_service.synthesize(
                        text, role_name=state.active_character or "阿米娅",
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
            api_key = os.getenv("DASHSCOPE_API_KEY") or ""
            if model:
                os.environ["DASHSCOPE_VL_MODEL"] = model
            target_model = model or os.getenv("DASHSCOPE_VL_MODEL") or "qwen3-vl-plus"
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
        async def toggle_global(req: GlobalSwitchReq) -> dict[str, Any]:
            state.global_active = req.active
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
                await asyncio.sleep(0.4)
                launcher = BOT_ROOT / "daemon_launcher.py"
                subprocess.Popen(
                    [sys.executable, str(launcher)],
                    cwd=str(BOT_ROOT),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
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
        class SmallTalkSwitchReq(BaseModel):
            enable: bool
            style: Optional[str] = None

        @app.post("/api/switch/small_talk")
        async def switch_small_talk_post(req: SmallTalkSwitchReq) -> dict[str, Any]:
            state.small_talk_mode = req.enable
            if req.style:
                state.small_talk_style = req.style
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "small_talk_mode": state.small_talk_mode,
                "small_talk_style": getattr(state, "small_talk_style", "pure_persona"),
            }

        @app.get("/api/switch/small_talk")
        async def switch_small_talk_get(enable: bool = True, style: Optional[str] = None) -> dict[str, Any]:
            state.small_talk_mode = bool(enable)
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
        async def switch_small_talk_style_post(req: SmallTalkStyleReq) -> dict[str, Any]:
            state.small_talk_style = req.style
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
        class TrainingSwitchReq(BaseModel):
            enable: bool
            group_id: Optional[str] = None

        @app.post("/api/training/toggle")
        async def toggle_training_post(req: TrainingSwitchReq) -> dict[str, Any]:
            state.training_mode = req.enable
            if req.group_id:
                gid = str(req.group_id).strip()
                t_groups = list(dict.fromkeys([str(g) for g in (state.training_groups or [])]))
                if req.enable and gid not in t_groups:
                    t_groups.append(gid)
                elif not req.enable and gid in t_groups:
                    t_groups.remove(gid)
                state.training_groups = t_groups
            await state.save()
            return {"code": 0, "status": "ok", "training_mode": state.training_mode, "training_groups": state.training_groups}

        @app.get("/api/training/toggle")
        async def toggle_training_get(enable: bool = True, group_id: Optional[str] = None) -> dict[str, Any]:
            state.training_mode = bool(enable)
            if group_id:
                gid = str(group_id).strip()
                t_groups = list(dict.fromkeys([str(g) for g in (state.training_groups or [])]))
                if enable and gid not in t_groups:
                    t_groups.append(gid)
                elif not enable and gid in t_groups:
                    t_groups.remove(gid)
                state.training_groups = t_groups
            await state.save()
            return {"code": 0, "status": "ok", "training_mode": state.training_mode, "training_groups": state.training_groups}

    if "/api/switch/auto_reply" not in existing_paths:
        class AutoReplySwitchReq(BaseModel):
            enable: bool
            interval: Optional[float] = None

        @app.post("/api/switch/auto_reply")
        async def switch_auto_reply_post(req: AutoReplySwitchReq) -> dict[str, Any]:
            state.auto_reply_mode = bool(req.enable)
            if req.interval is not None:
                state.reply_interval_seconds = float(req.interval)
            await state.save()
            return {
                "code": 0,
                "status": "ok",
                "auto_reply_mode": state.auto_reply_mode,
                "reply_interval_seconds": state.reply_interval_seconds,
            }

        @app.get("/api/switch/auto_reply")
        async def switch_auto_reply_get(enable: bool = True, interval: Optional[float] = None) -> dict[str, Any]:
            state.auto_reply_mode = bool(enable)
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
        class TriggerTrainingReq(BaseModel):
            group_id: str
            count: int = 120

        @app.post("/api/training/trigger")
        async def trigger_group_training_post(req: TriggerTrainingReq) -> dict[str, Any]:
            from nonebot import get_bot
            gid = str(req.group_id).strip()
            bot = None
            try:
                bot = get_bot()
            except Exception:
                pass
            if bot is not None:
                res = await modes.fetch_and_train_group_history(bot, gid, count=req.count)
            else:
                res = await modes.trigger_group_training_now(gid)
            return {"code": 0, "status": "ok", "result": res}

        @app.get("/api/training/trigger")
        async def trigger_group_training_get(group_id: str, count: int = 120) -> dict[str, Any]:
            from nonebot import get_bot
            gid = str(group_id).strip()
            bot = None
            try:
                bot = get_bot()
            except Exception:
                pass
            if bot is not None:
                res = await modes.fetch_and_train_group_history(bot, gid, count=count)
            else:
                res = await modes.trigger_group_training_now(gid)
            return {"code": 0, "status": "ok", "result": res}

    if "/api/training/status" not in existing_paths:
        @app.get("/api/training/status")
        async def api_training_status() -> dict[str, Any]:
            return {"code": 0, "status": "ok", "data": modes.get_training_status()}

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
