"""Task2–3 后端核心：配置持久化、本地控制 API、全局拦截与模式分发（支持看图问答、引用消息/合并转发解析、24h QQ号级上下文、垫图生图、文档导出与 30s 高清语音模式）。"""
from __future__ import annotations

import asyncio
import contextlib
import base64
import collections
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from nonebot import get_app, get_driver, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="agent_core",
    description="QQ Agent 数据状态、API 网关与核心分发（支持机器人模式与语音模式双工作区）",
    usage=(
        "Web控制台: http://127.0.0.1:8080/panel\n"
        "API: GET /api/status, /api/dashboard；POST /api/switch/*, /api/voice/*\n"
        "管理指令: /on /off /mode amiya|kaltsit|dania|none；/group add|del <id>；/status；/refs；主人可私聊提出电脑任务，阿米娅先汇报计划并等待确认"
    ),
)

from nonebot.exception import MatcherException, ProcessException
from .service_registry import services, state, recorder
from . import controller_api, local_rag_service
from .config_manager import BOT_ROOT, CONFIG_PATH, ALLOWED_CHARACTERS
from .errors import AgentCoreError, DocumentGenerationError, DrawPipelineError, TTSGenerationError, LLMProviderError, MessageDeliveryError
from .message_pipeline import ParsedMessage, Intent, parse_message, should_reply, classify_intent, classify_amiya_interaction
from .history_recorder import task_scope, current_task_id

conversation = services.conversation
document_writer = services.writer
doc_service = services.documents
llm_service = services.llm
tts_service = services.tts
controller_api.setup(services)
from .voice_presets import parse_and_strip_voice_emotion, resolve_character_id

_tts_synthesizing: bool = False
_voice_reply_lock = asyncio.Lock()

PRESETS_DIR = BOT_ROOT / "data" / "presets"
GROUPS_DIR = BOT_ROOT / "data" / "groups"
REFS_DIR = BOT_ROOT / "data" / "refs"
COOLDOWN_SECONDS = 3.0
CHAT_TTS_TIMEOUT = 100.0
_active_chat_window: dict[str, float] = {}  # group:uid -> 上次互动时间戳（支持60s免@连续对话）
_AMIYA_SILENCE_SECONDS = 10.0
_AMIYA_SILENCE_UNTIL: dict[str, float] = {}

# 消息冷却（支持 uid 与 group:group_id 双重防刷屏）
_cooldown: dict[str, float] = {}

# 最近消息图片与全文缓存：支持跨消息引用看图、引用他人消息总结及合并转发解析
_msg_image_cache: collections.OrderedDict[str, list[dict[str, str]]] = collections.OrderedDict()
_msg_full_cache: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
_user_last_images: dict[str, tuple[float, list[dict[str, str]]]] = {}
_group_last_images: dict[str, collections.deque[tuple[float, str, list[dict[str, str]]]]] = {}

_ADMIN_CMDS = frozenset({"on", "off", "mode", "group", "status", "refs"})
_MD5_HEX_RE = re.compile(r"([0-9a-fA-F]{32})")


def _remember_voice_busy_message(uid: str, group_id: Optional[str], text: str) -> None:
    """忙碌消息只原子追加用户侧历史，不产生待回复任务或虚构已发送内容。"""
    if not uid.isdigit() or not text.strip():
        return
    session = conversation.store.begin(uid)
    with conversation.use_session(session, group_id=group_id):
        conversation.store.remember(session, text, "", delivery_status="received_only")

def _get_voice_prompt_suffix(character: str) -> str:
    import os
    prompt_tag_enabled = getattr(state, "voice_emotion_director_enabled", False) or (
        os.getenv("VOICE_PROMPT_TAG_ENABLED", "false").lower() in ("1", "true", "yes")
    )
    base = (
        "\n\n【当前处于纯语音回复模式（最高优先级·极速响应）】\n"
        "你的文字将直接用于极速语音合成：\n"
        "1. 必须只输出极其简短的单句口语（严格控制在 8 至 16 个字以内，坚决不要超过 18 字，禁止长句和从句）！\n"
        "2. 保持角色语气，直接说出最核心的一句回答即可。\n"
    )
    if not prompt_tag_enabled:
        return base + "3. 严禁任何括号、动作神态描写（如（微笑）、【思考】等），不输出任何格式符号。"
    from .voice_presets import VOICE_CANONICAL_PRESETS, resolve_character_id
    try:
        cid = resolve_character_id(character)
    except ValueError:
        cid = "amiya"
    tags_str = "/".join(VOICE_CANONICAL_PRESETS.get(cid, {}).get("tags", {}).keys())
    return base + (
        f"3. 必须在台词最开头附带一个符合当前语气的情绪标签（严格从以下标签中选取一个放入方括号中）：[{tags_str}]。\n"
        "示例：[调侃] 随你怎么叫吧，反正博士开心就好。"
    )

_VOICE_MODE_PROMPT_SUFFIX = ""


def _uid(event: MessageEvent) -> str:
    return str(event.user_id)


def _sender_display_name(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    if sender:
        card = (getattr(sender, "card", None) or "").strip()
        nick = (getattr(sender, "nickname", None) or "").strip()
        if card:
            return card
        if nick:
            return nick
    return _uid(event)


def _is_admin(event: MessageEvent) -> bool:
    su = (state.superuser_qq or "").strip()
    if not su:
        return False
    return _uid(event) == su


async def _maybe_claim_superuser(event: MessageEvent, cmd: str) -> bool:
    """superuser_qq 为空时，首次发管理指令的用户自动认领；已设置时校验管理员身份。"""
    if (state.superuser_qq or "").strip():
        return _is_admin(event)
    if cmd not in _ADMIN_CMDS:
        return False
    state.superuser_qq = _uid(event)
    await state.save()
    logger.info(f"auto-claimed superuser_qq={state.superuser_qq}")
    return True


def _in_cooldown(key: str) -> bool:
    interval = float(getattr(state, "reply_interval_seconds", 1.5) or 1.5)
    last = _cooldown.get(key)
    if last is None:
        return False
    return (time.monotonic() - last) < interval


def _touch_cooldown(key: str) -> None:
    _cooldown[key] = time.monotonic()


def _read_preset(name: str) -> str:
    path = PRESETS_DIR / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_group_profile(group_id: str) -> Optional[dict]:
    path = GROUPS_DIR / f"{group_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _group_allowed(group_id: str) -> bool:
    allowed = {str(g) for g in state.active_groups}
    return group_id in allowed


def _is_comfort_target(uid: str) -> bool:
    targets = {str(t) for t in state.comfort_mode_targets}
    return uid in targets


# ---------------------------------------------------------------------------
# 管理员指令（支持私聊与白名单群聊触发：/on /off /mode /group /status /refs）
# ---------------------------------------------------------------------------

cmd_on = on_command("on", priority=5, block=True)
cmd_off = on_command("off", priority=5, block=True)
cmd_mode = on_command("mode", priority=5, block=True)
cmd_group = on_command("group", priority=5, block=True)
cmd_status = on_command("status", priority=5, block=True)
cmd_refs = on_command("refs", priority=5, block=True)
cmd_clear_profile = on_command("清理画像", aliases={"clear_profile", "重置画像", "删除画像"}, priority=5, block=True)


@cmd_clear_profile.handle()
async def handle_clear_profile(event: MessageEvent, arg: Message = CommandArg()):
    from .user_memory_service import user_memory_store

    caller_uid = _uid(event)
    raw_target = arg.extract_plain_text().strip()
    for seg in event.message:
        if seg.type == "at" and str(seg.data.get("qq") or "").isdigit():
            raw_target = str(seg.data.get("qq")).strip()
            break
    target_uid = raw_target if raw_target else caller_uid
    if not target_uid.isdigit():
        await cmd_clear_profile.finish("用法: /清理画像 <QQ号> （不填则清理你自己的画像记录）")
    if target_uid != caller_uid and not _is_admin(event):
        await cmd_clear_profile.finish("仅主人可定向清理其他 QQ 号的画像。")
    res = user_memory_store.clear_user_profile_by_uid(target_uid)
    await cmd_clear_profile.finish(
        f"🧹 已定向清空 QQ {target_uid} 的全部跨群画像、记忆与聊天记录（共清理 {len(res['deleted_scopes'])} 个作用域、{res['removed_chat_logs']} 条发言记录）。"
    )


@cmd_on.handle()
async def handle_on(event: MessageEvent):
    if not await _maybe_claim_superuser(event, "on"):
        return
    state.global_active = True
    await state.save()
    await cmd_on.finish("🤖 机器人全局响应已开启")


@cmd_off.handle()
async def handle_off(event: MessageEvent):
    if not await _maybe_claim_superuser(event, "off"):
        return
    from .autonomous_agent import autonomous_coordinator
    from .group_chat_manager import group_chat_manager

    state.global_active = False
    _active_chat_window.clear()
    autonomous_coordinator.clear_pending_events()
    group_chat_manager._pending_buffers.clear()
    await state.save()
    await cmd_off.finish("⏸️ 机器人已切换为全局静默")


@cmd_mode.handle()
async def handle_mode(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    if not await _maybe_claim_superuser(event, "mode"):
        return
    raw = arg.extract_plain_text().strip().lower()
    if raw == "voice":
        state.voice_enabled = not state.voice_enabled
        if state.voice_enabled:
            state.voice_only_reply = True
        await state.save()
        await cmd_mode.finish(f"🎙️ 语音模式已{'开启' if state.voice_enabled else '关闭'} (当前人设: {state.active_character})")
    if raw in {"smalltalk", "small_talk", "闲聊", "luis", "路易斯"}:
        state.small_talk_mode = not state.small_talk_mode
        await state.save()
        await cmd_mode.finish(
            f"💬 闲聊模式（路易斯极短回复风）已{'开启' if state.small_talk_mode else '关闭'}"
        )
    if raw.startswith(("train", "训练")):
        parts = arg.extract_plain_text().strip().split()
        from .modes import fetch_and_train_group_history
        if len(parts) >= 2 and parts[1].isdigit():
            gid = parts[1].strip()
            t_groups = [str(g) for g in (state.training_groups or [])]
            if gid not in t_groups:
                t_groups.append(gid)
            state.training_groups = t_groups
            state.training_mode = True
            await state.save()
            res = await fetch_and_train_group_history(bot, gid, count=120)
            await cmd_mode.finish(
                f"📚 训练模式已开启并绑定群 {gid}！\n"
                f"已拉取历史消息: {res.get('fetched_history_count', 0)} 条 | 有效对话行: {res.get('lines', 0)} | SFT样本对: {res.get('dataset_pairs', 0)}"
            )
        state.training_mode = not state.training_mode
        if state.training_mode and isinstance(event, GroupMessageEvent):
            gid = str(event.group_id)
            t_groups = [str(g) for g in (state.training_groups or [])]
            if gid not in t_groups:
                t_groups.append(gid)
                state.training_groups = t_groups
        await state.save()
        await cmd_mode.finish(
            f"📚 群消息自动采集与训练模式已{'开启' if state.training_mode else '关闭'} (目标群: {state.training_groups or state.active_groups})"
        )
    if raw not in ALLOWED_CHARACTERS:
        await cmd_mode.finish(
            "用法:\n"
            "• 切换人设: /mode amiya | kaltsit | dania | none\n"
            "• 开关语音: /mode voice\n"
            "• 开关闲聊(路易斯风): /mode smalltalk (或 /mode 闲聊)\n"
            "• 开关群训练模式: /mode train [群号]\n"
            f"当前人设: {state.active_character} | 语音: {'开' if state.voice_enabled else '关'} | "
            f"闲聊: {'开' if state.small_talk_mode else '关'} | 训练: {'开' if state.training_mode else '关'}"
        )
    state.active_character = raw
    await state.save()
    await cmd_mode.finish(f"🤖 当前机器人模式已切换为: {state.active_character}")


@cmd_group.handle()
async def handle_group(event: MessageEvent, arg: Message = CommandArg()):
    if not await _maybe_claim_superuser(event, "group"):
        return
    parts = arg.extract_plain_text().strip().split()
    if len(parts) < 2 or parts[0] not in {"add", "del"}:
        await cmd_group.finish(
            "用法: /group add|del <群号>\n"
            f"当前白名单群: {state.active_groups}"
        )
    action, gid = parts[0], parts[1].strip()
    groups = [str(g) for g in state.active_groups]
    if action == "add":
        if gid not in groups:
            groups.append(gid)
    else:
        groups = [g for g in groups if g != gid]
    state.active_groups = groups
    await state.save()
    await cmd_group.finish(f"已更新群白名单: {state.active_groups}")


@cmd_status.handle()
async def handle_status(event: MessageEvent):
    if not await _maybe_claim_superuser(event, "status"):
        return
    v_srv = await tts_service.check_voice_services()
    refs = controller_api._list_saved_refs()
    msg = (
        "【PRTS 双模运行状态】\n"
        f"🤖 机器人模式: {'运行中' if state.global_active else '已静默'} | 人设: {state.active_character}\n"
        f"👥 白名单群 ({len(state.active_groups)}): {', '.join(str(g) for g in state.active_groups) or '无'}\n"
        f"🖼️ 已归档立绘 ({len(refs)}): {', '.join(refs) or '无'}\n"
        f"🎙️ 语音模式: {'开启' if state.voice_enabled else '关闭'} ({state.voice_mode}, {state.voice_speed}x, 最长30s)\n"
        f"🔊 9880 API: {'在线' if v_srv['tts_9880_online'] else '离线'} | 9876 Studio: {'在线' if v_srv['studio_9876_online'] else '离线'}\n"
        "💻 Web控制台: http://127.0.0.1:8080/panel"
    )
    await cmd_status.finish(msg)


@cmd_refs.handle()
async def handle_refs(event: MessageEvent):
    refs = controller_api._list_saved_refs()
    if not refs:
        await cmd_refs.finish("当前尚未归档角色参考立绘。在群里发送图片并配文「这是XX的立绘」即可自动归档。")
    await cmd_refs.finish(f"🖼️ 已归档角色原型 ({len(refs)} 张)：{', '.join(refs)}")


# ---------------------------------------------------------------------------
# 消息分发、图片/引用/合并转发提取与模式路由
# ---------------------------------------------------------------------------

_LONG_TASK_KEYWORDS = (
    "教案", "教学设计", "书评", "评价", "评析", "写一篇", "写个", "撰写",
    "分析", "解读", "论述", "论文", "研究", "研讨", "阐述", "解析", "赏析",
    "深度分析", "大纲", "总结报告", "文献综述", "调研报告", "做一份", "做个教案",
    "教学方案", "深度解读", "研读", "策划案", "物理教案", "数学教案", "讲义", "课件", "试卷",
)

_DRAW_KEYWORDS = (
    "画", "绘图", "生图", "插画", "配图", "生成图片", "作画", "画一张", "画幅", "写真",
    "q版", "换成", "改成", "变身", "转成", "二创", "重绘", "改图", "修图", "换装",
    "二次元化", "手办化", "卡通化", "垫图", "图生图", "变q版", "做成q版", "输出更换后的图",
    "生成一张", "生成一幅", "生成个", "来一张", "来一幅", "整一张", "搞一张", "发一张", "弄一张",
    "全身照", "半身照", "自拍", "立绘", "设定图", "人设图", "形象图", "壁纸", "头像", "照片",
)

_DRAW_INTENT_RE = re.compile(
    r"(?:生成|画|绘|做|弄|来|整|搞|发|给|看).{0,14}(?:张|幅|个|份)?.{0,10}(?:图|照片|相片|写真|立绘|插画|肖像|自拍|头像|壁纸|样子|形象|q版|jk|泳装|女仆|旗袍)"
    r"|不知道.{0,10}长什么样.{0,16}(?:生成|画|来|发|看|给|照片|图|立绘)",
    re.IGNORECASE,
)

_SAVE_REF_ONLY_RE = re.compile(
    r"(?:这是|这个是|这张是|记住)([\u4e00-\u9fa5A-Za-z]{2,8}?)(?:的)?(?:角色图|立绘|设定图|原画|形象|原型)",
    re.IGNORECASE,
)

_VISION_HINT_KEYWORDS = (
    "刚才那张图", "上面那张图", "刚才发的图", "上面发的图", "上一张图", "前一张图",
    "看得到图", "看到图吗", "图里写", "图中写", "这是啥图", "这是什么图",
)


def _is_draw_intent(text: str, has_image: bool = False) -> bool:
    return llm_service._auto_detect_draw_intent(text, has_image=has_image)


def _is_long_task_or_draw_intent(text: str, has_image: bool = False) -> bool:
    s = text.strip().lower()
    if s.startswith(("/work", "/doc", "/pdf", "/docx", "/word", "/md", "/export", "/draw", "【任务】")):
        return True
    if doc_service.detect_requested_formats(s):
        return True
    return any(k in s for k in _LONG_TASK_KEYWORDS) or _is_draw_intent(text, has_image)


def _get_ack_message(character: str, text: str, has_ref_img: bool = False) -> str:
    s = text.strip().lower()
    is_draw = _is_draw_intent(s, has_image=has_ref_img)
    if character == "kaltsit":
        if is_draw:
            return "收到委托。图像生成需要调阅外部渲染节点，稍候片刻。"
        return "了解你的需求。这项任务需要调阅资料库进行深度整理，请稍作等待。"
    if character == "dania":
        if is_draw:
            if has_ref_img:
                return "哼，拿这张图让我照着画是吧？等着，我这就帮你盯紧细节画出来。"
            return "行吧行吧，画图这种事也找我……稍微等一下，马上给你弄好。"
        return "啧，这么长一串问题……算了，真拿你没办法，等我帮你翻翻资料整理一下。"
    if is_draw:
        if has_ref_img:
            return "收到参考立绘啦！正在对照原图细节认真绘制，稍微等我一下哦~"
        return "收到啦！我这就构思画面全力绘制，稍微等我一下哦~"
    return "好嘞！这项内容我这就帮你深入整理，稍微等我一下哦~"


def _extract_images_from_segments(segments: Any, *, ignore_stickers: bool = False) -> list[dict[str, str]]:
    """兼容 OneBot Message 对象、list[dict] 或 CQ 码字符串，提取其中的所有图片信息。"""
    if not segments:
        return []
    out: list[dict[str, str]] = []
    if isinstance(segments, str):
        for m in re.finditer(r"\[CQ:image,([^\]]+)\]", segments):
            kv_str = m.group(1)
            data: dict[str, str] = {}
            for part in kv_str.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    data[k.strip()] = v.strip()
            url = data.get("url", "").replace("&amp;", "&")
            file_id = data.get("file", "") or data.get("file_id", "")
            path = data.get("path", "")
            if url or file_id or path:
                out.append({"url": url, "file": file_id, "path": path})
        return out

    for seg in segments:
        seg_type = getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)
        seg_data = getattr(seg, "data", None) or (seg.get("data") if isinstance(seg, dict) else {}) or {}
        if seg_type == "image":
            if ignore_stickers:
                sub_type = str(seg_data.get("sub_type") or "0").strip()
                summary = str(seg_data.get("summary") or "").strip()
                if sub_type == "1" or "表情" in summary or seg_data.get("emoji_id"):
                    continue
            url = str(seg_data.get("url") or "").strip()
            file_id = str(seg_data.get("file") or seg_data.get("file_id") or "").strip()
            path = str(seg_data.get("path") or "").strip()
            if url or file_id or path:
                out.append({"url": url, "file": file_id, "path": path})
    return out


def _extract_images_from_msg(msg: Optional[Message], *, ignore_stickers: bool = False) -> list[dict[str, str]]:
    return _extract_images_from_segments(msg, ignore_stickers=ignore_stickers)


def _extract_text_and_forwards_from_segments(segments: Any) -> tuple[str, list[str]]:
    """从消息段列表或 CQ 字符串中提取纯文本与合并转发 ID (forward_id / resid)。"""
    if not segments:
        return "", []
    texts: list[str] = []
    forward_ids: list[str] = []

    if isinstance(segments, str):
        for m in re.finditer(r"\[CQ:forward,([^\]]+)\]", segments):
            for part in m.group(1).split(","):
                if part.strip().startswith("id="):
                    forward_ids.append(part.strip()[3:])
        clean_t = re.sub(r"\[CQ:[^\]]+\]", "", segments).strip()
        return clean_t, forward_ids

    for seg in segments:
        seg_type = getattr(seg, "type", None) or (seg.get("type") if isinstance(seg, dict) else None)
        seg_data = getattr(seg, "data", None) or (seg.get("data") if isinstance(seg, dict) else {}) or {}
        if seg_type == "text":
            t = str(seg_data.get("text") or "").strip()
            if t:
                texts.append(t)
        elif seg_type == "forward":
            fid = str(seg_data.get("id") or seg_data.get("resid") or "").strip()
            if fid:
                forward_ids.append(fid)
        elif seg_type == "json":
            raw_json = str(seg_data.get("data") or "")
            try:
                jd = json.loads(raw_json)
                meta = jd.get("meta") or {}
                detail = meta.get("detail") or jd.get("detail") or {}
                resid = str(detail.get("resid") or detail.get("id") or jd.get("resid") or "").strip()
                if resid and resid not in forward_ids:
                    forward_ids.append(resid)
                prompt_title = str(jd.get("prompt") or detail.get("source") or "").strip()
                news = detail.get("news") or []
                preview_lines = [str(n.get("text") or "").strip() for n in news if isinstance(n, dict) and n.get("text")]
                if preview_lines:
                    texts.append(f"[{prompt_title or '聊天记录'}预览: {' / '.join(preview_lines)}]")
                elif prompt_title and "聊天记录" not in prompt_title:
                    texts.append(f"[分享卡片: {prompt_title}]")
            except Exception:
                pass
    return " ".join(texts).strip(), forward_ids


def _cache_incoming_images(event: MessageEvent, group_id: Optional[str], uid: str) -> list[dict[str, str]]:
    """记录群聊/私聊中每一条消息的文本、发送者、图片与合并转发 ID，供后续引用回复或上下文联想使用。"""
    raw_msg = getattr(event, "original_message", None) or event.message
    all_imgs = _extract_images_from_msg(raw_msg, ignore_stickers=False)
    plain_text, forward_ids = _extract_text_and_forwards_from_segments(raw_msg)
    sender_name = _sender_display_name(event)
    msg_id = str(getattr(event, "message_id", "") or "").strip()

    if msg_id:
        if all_imgs:
            _msg_image_cache[msg_id] = all_imgs
            while len(_msg_image_cache) > 300:
                _msg_image_cache.popitem(last=False)
        _msg_full_cache[msg_id] = {
            "uid": uid,
            "sender_name": sender_name,
            "text": plain_text or (event.get_plaintext() or "").strip(),
            "images": all_imgs,
            "forward_ids": forward_ids,
            "ts": time.time(),
        }
        while len(_msg_full_cache) > 500:
            _msg_full_cache.popitem(last=False)

    if all_imgs and group_id:
        gdq = _group_last_images.setdefault(str(group_id), collections.deque(maxlen=20))
        gdq.append((time.monotonic(), uid, all_imgs))

    non_sticker_imgs = _extract_images_from_msg(raw_msg, ignore_stickers=True)
    if non_sticker_imgs:
        scope_key = f"{group_id or 'private'}:{uid}"
        _user_last_images[scope_key] = (time.monotonic(), non_sticker_imgs)
    return all_imgs


async def _fetch_forward_messages_text(bot: Bot, forward_id: str, depth: int = 0) -> tuple[str, list[dict[str, str]]]:
    """调用 OneBot get_forward_msg 展开合并转发的聊天记录，提取全部对话文本与内嵌图片。"""
    if not forward_id or depth > 2:
        return "", []
    lines: list[str] = []
    imgs: list[dict[str, str]] = []
    resp = None
    for kwargs in ({"message_id": forward_id}, {"id": forward_id}):
        try:
            resp = await asyncio.wait_for(bot.call_api("get_forward_msg", **kwargs), timeout=8.0)
            if resp:
                break
        except Exception:
            continue
    if not isinstance(resp, dict):
        return "", []

    nodes = resp.get("messages") or resp.get("data", {}).get("messages") if isinstance(resp.get("data"), dict) else resp.get("messages")
    if not isinstance(nodes, list):
        return "", []

    for node in nodes[:60]:
        if not isinstance(node, dict):
            continue
        data_part = node.get("data") if isinstance(node.get("data"), dict) else node
        sender = data_part.get("sender") or node.get("sender") or {}
        sname = str(sender.get("card") or sender.get("nickname") or data_part.get("nickname") or data_part.get("name") or "群友").strip()
        suid = str(sender.get("user_id") or data_part.get("user_id") or data_part.get("uin") or "").strip()
        content = data_part.get("content") or data_part.get("message") or node.get("message") or ""
        node_imgs = _extract_images_from_segments(content, ignore_stickers=False)
        if node_imgs:
            imgs.extend(node_imgs)
        node_txt, sub_fids = _extract_text_and_forwards_from_segments(content)
        for sfid in sub_fids[:2]:
            sub_txt, sub_imgs = await _fetch_forward_messages_text(bot, sfid, depth + 1)
            if sub_txt:
                node_txt = (node_txt + "\n" + sub_txt).strip()
            if sub_imgs:
                imgs.extend(sub_imgs)
        if node_txt or node_imgs:
            img_tag = f" [附图{len(node_imgs)}张]" if node_imgs else ""
            who = f"{sname}({suid})" if suid else sname
            lines.append(f"- {who}: {node_txt}{img_tag}")

    return "\n".join(lines), imgs


async def _resolve_reply_and_forward_context(
    bot: Bot,
    event: MessageEvent,
    group_id: Optional[str],
    uid: str,
    plain_text: str,
) -> tuple[str, list[dict[str, str]]]:
    """解析用户当前消息中的【引用回复 (reply)】与【合并转发聊天记录 (forward/json)】：
    支持 event.reply -> 本地缓存 -> get_msg -> get_group_msg_history 四重回查，确保引用他人图片/文字 100% 可见。
    """
    orig_msg = getattr(event, "original_message", None) or event.message
    direct_imgs = _extract_images_from_msg(orig_msg, ignore_stickers=False)
    _, direct_forward_ids = _extract_text_and_forwards_from_segments(orig_msg)

    reply_id = ""
    for seg in orig_msg:
        if seg.type == "reply":
            reply_id = str(seg.data.get("id") or "").strip()
            if reply_id:
                break

    reply_obj = getattr(event, "reply", None)
    if not reply_id and reply_obj is not None:
        reply_id = str(getattr(reply_obj, "message_id", "") or "").strip()

    reply_sender = ""
    reply_text = ""
    reply_imgs: list[dict[str, str]] = []
    reply_forward_ids: list[str] = []

    # 1. 先从 event.reply 提取
    if reply_obj is not None:
        r_sender_obj = getattr(reply_obj, "sender", None)
        if r_sender_obj:
            r_nick = str(getattr(r_sender_obj, "card", None) or getattr(r_sender_obj, "nickname", None) or "").strip()
            r_uid = str(getattr(r_sender_obj, "user_id", None) or "").strip()
            reply_sender = f"{r_nick}({r_uid})" if (r_nick and r_uid) else (r_nick or r_uid)
        r_msg = getattr(reply_obj, "message", None) or getattr(reply_obj, "raw_message", None)
        if r_msg:
            reply_imgs.extend(_extract_images_from_segments(r_msg, ignore_stickers=False))
            rt, rfids = _extract_text_and_forwards_from_segments(r_msg)
            if rt:
                reply_text = rt
            reply_forward_ids.extend(rfids)

    # 2. 从本地实时消息缓存 _msg_full_cache / _msg_image_cache 补全
    if reply_id:
        cached_item = _msg_full_cache.get(reply_id)
        if cached_item:
            if not reply_sender:
                reply_sender = f"{cached_item.get('sender_name', '')}({cached_item.get('uid', '')})"
            if not reply_text and cached_item.get("text"):
                reply_text = str(cached_item["text"])
            if not reply_imgs and cached_item.get("images"):
                reply_imgs.extend(cached_item["images"])
            for fid in cached_item.get("forward_ids") or []:
                if fid not in reply_forward_ids:
                    reply_forward_ids.append(fid)
        if not reply_imgs and reply_id in _msg_image_cache:
            reply_imgs.extend(_msg_image_cache[reply_id])

    # 3. 若本地尚未拿到被引用消息的文本或图片，主动调用 NapCat get_msg 获取
    if reply_id and (not reply_text or not reply_imgs):
        try:
            msg_param = int(reply_id) if reply_id.lstrip("-").isdigit() else reply_id
            api_data = await asyncio.wait_for(bot.call_api("get_msg", message_id=msg_param), timeout=5.0)
            if isinstance(api_data, dict):
                if not reply_sender:
                    s_info = api_data.get("sender") or {}
                    s_nick = str(s_info.get("card") or s_info.get("nickname") or "").strip()
                    s_uid = str(s_info.get("user_id") or "").strip()
                    reply_sender = f"{s_nick}({s_uid})" if (s_nick and s_uid) else (s_nick or s_uid)
                raw_m = api_data.get("message") or api_data.get("raw_message")
                if raw_m:
                    fetched_imgs = _extract_images_from_segments(raw_m, ignore_stickers=False)
                    if fetched_imgs and not reply_imgs:
                        reply_imgs.extend(fetched_imgs)
                        _msg_image_cache[reply_id] = fetched_imgs
                    ftxt, ffids = _extract_text_and_forwards_from_segments(raw_m)
                    if ftxt and not reply_text:
                        reply_text = ftxt
                    for fid in ffids:
                        if fid not in reply_forward_ids:
                            reply_forward_ids.append(fid)
        except Exception as exc:
            logger.debug(f"调用 get_msg({reply_id}) 跳过: {exc}")

    # 4. 若 get_msg 因 Rkey 或历史消息未返回，且在群聊中，调用 get_group_msg_history 回查最近 25 条群消息
    mentions_img = any(k in plain_text.lower() for k in _VISION_HINT_KEYWORDS)
    if group_id and (reply_id or mentions_img) and (not reply_imgs or not reply_text) and not direct_imgs:
        try:
            hist_resp = await asyncio.wait_for(
                bot.call_api("get_group_msg_history", group_id=int(group_id), count=25),
                timeout=5.0,
            )
            hist_msgs = (hist_resp.get("messages") if isinstance(hist_resp, dict) else None) or []
            if isinstance(hist_msgs, list):
                for hm in reversed(hist_msgs):
                    if not isinstance(hm, dict):
                        continue
                    cand_ids = {str(hm.get(k) or "").strip() for k in ("message_id", "real_id", "message_seq", "id")} - {""}
                    raw_m = hm.get("message") or hm.get("raw_message")
                    hm_imgs = _extract_images_from_segments(raw_m, ignore_stickers=False)
                    hm_txt, hm_fids = _extract_text_and_forwards_from_segments(raw_m)
                    if reply_id and reply_id in cand_ids:
                        if hm_imgs:
                            reply_imgs.extend(hm_imgs)
                        if hm_txt and not reply_text:
                            reply_text = hm_txt
                        for fid in hm_fids:
                            if fid not in reply_forward_ids:
                                reply_forward_ids.append(fid)
                        break
                # 仅当用户未引用纯文字消息、且明确提到“刚才那张图/上面那张图”时，才回溯群内最近图片
                if not reply_imgs and not reply_text and mentions_img:
                    for hm in reversed(hist_msgs):
                        if not isinstance(hm, dict):
                            continue
                        hm_imgs = _extract_images_from_segments(hm.get("message") or hm.get("raw_message"), ignore_stickers=False)
                        if hm_imgs:
                            reply_imgs.extend(hm_imgs)
                            logger.info(f"从 get_group_msg_history 命中群 {group_id} 最近发送的图片 ({len(hm_imgs)} 张)")
                            break
        except Exception as exc:
            logger.debug(f"调用 get_group_msg_history({group_id}) 跳过: {exc}")

    # 5. 展开所有关联的合并转发聊天记录 (forward_id)
    all_forward_ids = list(dict.fromkeys(direct_forward_ids + reply_forward_ids))
    forward_blocks: list[str] = []
    for fid in all_forward_ids[:2]:
        f_txt, f_imgs = await _fetch_forward_messages_text(bot, fid)
        if f_txt:
            forward_blocks.append(f_txt)
        if f_imgs:
            reply_imgs.extend(f_imgs)

    # 6. 收集最终关联图片（当前消息图片 > 被引用消息图片 > 群聊/用户最近发送的图片）
    final_imgs: list[dict[str, str]] = []
    if direct_imgs:
        final_imgs.extend(direct_imgs)
    if reply_imgs:
        for img in reply_imgs:
            if img not in final_imgs:
                final_imgs.append(img)
    # 仅在没有引用纯文字、且用户明确提及“刚才那张图/上面那张图”时，才关联缓存图片（防止纯文字误带图）
    if not final_imgs and not reply_text and not reply_id and mentions_img:
        scope_key = f"{group_id or 'private'}:{uid}"
        recent = _user_last_images.get(scope_key)
        if recent:
            ts, cached_imgs = recent
            if (time.monotonic() - ts) <= 180.0:
                logger.info(f"关联用户 {uid} 最近发送的图片 ({len(cached_imgs)} 张)")
                final_imgs.extend(cached_imgs)
    if not final_imgs and not reply_text and not reply_id and group_id and mentions_img:
        gdq = _group_last_images.get(str(group_id))
        if gdq:
            ts, sender_u, g_imgs = gdq[-1]
            if (time.monotonic() - ts) <= 180.0:
                logger.info(f"关联群 {group_id} 内成员 {sender_u} 最近发送的图片 ({len(g_imgs)} 张)")
                final_imgs.extend(g_imgs)

    # 7. 组装被引用消息 / 合并转发聊天记录的上下文提示文本
    ctx_parts: list[str] = []
    if reply_text or reply_sender or (reply_id and final_imgs and not direct_imgs):
        sender_label = f"（发送者：{reply_sender}）" if reply_sender else ""
        if reply_text:
            ctx_parts.append(f"【用户当前引用/回复的目标消息{sender_label}】：\n「{reply_text}」")
        elif final_imgs and not direct_imgs:
            ctx_parts.append(f"【用户当前引用/回复了{sender_label or '群友'}发送的一张图片（已附带在视觉输入中）】")
    if forward_blocks:
        ctx_parts.append("【合并转发的聊天记录完整内容】：\n" + "\n---\n".join(forward_blocks))

    return "\n\n".join(ctx_parts), final_imgs


def _clean_extracted_url(raw_url: str) -> str:
    """清理从文本或 Markdown 中提取的图片 URL，剥离末尾误吸纳的括号或标点。"""
    u = (raw_url or "").strip().replace("&amp;", "&")
    while u and u[-1] in ").,]}>\"'”’）】。，；！":
        u = u[:-1].strip()
    return u


async def _download_image_cached(url: str, file_hint: str = "") -> Optional[str]:
    """将图片下载到本地缓存；若腾讯 gchat.qpic.cn 因 Rkey 失效报错，自动回退 MD5 公网直链。"""
    try:
        import httpx
    except ImportError:
        return None

    url = _clean_extracted_url(url)
    cache_dir = BOT_ROOT / "data" / "cache" / "images"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key_seed = (file_hint or url or "").encode("utf-8")
        url_hash = hashlib.md5(key_seed).hexdigest()[:16]
        local_path = cache_dir / f"img_{url_hash}.png"
        if local_path.is_file() and local_path.stat().st_size > 100:
            return str(local_path.resolve())
    except Exception as exc:
        logger.warning(f"初始化图片缓存目录异常: {exc}")
        return None

    candidate_urls: list[str] = []
    if url and url.startswith(("http://", "https://")):
        candidate_urls.append(url)

    md5_match = _MD5_HEX_RE.search(file_hint or "") or _MD5_HEX_RE.search(url or "")
    if md5_match:
        md5_upper = md5_match.group(1).upper()
        md5_url = f"https://gchat.qpic.cn/gchatpic_new/0/0-0-{md5_upper}/0"
        if md5_url not in candidate_urls:
            candidate_urls.append(md5_url)

    if not candidate_urls:
        return None

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        async with httpx.AsyncClient(trust_env=False, timeout=25.0, follow_redirects=True, verify=False) as client:
            for target_url in candidate_urls:
                try:
                    resp = await client.get(target_url, headers=headers)
                    if resp.status_code == 200 and resp.content and len(resp.content) > 100:
                        local_path.write_bytes(resp.content)
                        logger.info(
                            f"成功缓存图片到本地: {local_path.name} ({len(resp.content)} bytes) <- {target_url[:65]}"
                        )
                        return str(local_path.resolve())
                    logger.warning(f"下载图片 HTTP {resp.status_code}: {target_url[:80]}")
                except Exception as inner_exc:
                    logger.warning(f"尝试下载图片异常 ({target_url[:60]}): {inner_exc}")
    except Exception as exc:
        logger.warning(f"下载图片缓存异常: {exc}")
    return None


def _file_bytes_to_data_uri(raw: bytes, hint_name: str = "") -> Optional[str]:
    if not raw or len(raw) <= 100:
        return None
    mime = "image/png"
    lower = hint_name.lower()
    if lower.endswith((".jpg", ".jpeg")) or raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif lower.endswith(".webp") or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        mime = "image/webp"
    elif lower.endswith(".gif") or raw[:4] == b"GIF8":
        mime = "image/gif"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _resolve_input_images_b64(bot: Bot, img_infos: list[dict[str, str]]) -> list[str]:
    """将用户发来的 QQ 图片或引用的他人图片解析为 data:image/...;base64,... 列表。"""
    results: list[str] = []
    cache_dir = BOT_ROOT / "data" / "cache" / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)

    for info in img_infos[:4]:
        url = (info.get("url") or "").strip()
        file_id = (info.get("file") or "").strip()
        path_hint = (info.get("path") or "").strip()

        if file_id:
            url_hash = hashlib.md5(file_id.encode("utf-8")).hexdigest()[:16]
            cached_p = cache_dir / f"img_{url_hash}.png"
            if cached_p.is_file() and cached_p.stat().st_size > 100:
                uri = _file_bytes_to_data_uri(cached_p.read_bytes(), file_id)
                if uri:
                    logger.info(f"从本地缓存命中参考图: {cached_p.name}")
                    results.append(uri)
                    continue

        found_local = False
        for candidate_local in (path_hint, file_id):
            if candidate_local and candidate_local.startswith("/"):
                cp = Path(candidate_local)
                if cp.is_file() and cp.stat().st_size > 100:
                    raw = cp.read_bytes()
                    uri = _file_bytes_to_data_uri(raw, cp.name)
                    if uri:
                        logger.info(f"直接从本地路径读取参考图成功: {cp} ({len(raw)} bytes)")
                        results.append(uri)
                        found_local = True
                        break
        if found_local:
            continue

        if file_id:
            for api_name, api_kwargs in (
                ("get_image", {"file": file_id}),
                ("get_file", {"file_id": file_id}),
            ):
                try:
                    resp = await asyncio.wait_for(bot.call_api(api_name, **api_kwargs), timeout=12.0)
                    if not isinstance(resp, dict):
                        continue
                    b64_raw = resp.get("base64")
                    if isinstance(b64_raw, str) and len(b64_raw.strip()) > 100:
                        raw_bytes = base64.b64decode(b64_raw.strip())
                        url_hash = hashlib.md5(file_id.encode("utf-8")).hexdigest()[:16]
                        (cache_dir / f"img_{url_hash}.png").write_bytes(raw_bytes)
                        uri = _file_bytes_to_data_uri(raw_bytes, file_id)
                        if uri:
                            logger.info(f"通过 NapCat {api_name}(base64) 获取参考图成功: {file_id} ({len(raw_bytes)} bytes)")
                            results.append(uri)
                            found_local = True
                            break

                    local_file_str = str(resp.get("file") or "").strip()
                    if local_file_str and local_file_str.startswith("/"):
                        lp = Path(local_file_str)
                        if lp.is_file() and lp.stat().st_size > 100:
                            raw_bytes = lp.read_bytes()
                            url_hash = hashlib.md5(file_id.encode("utf-8")).hexdigest()[:16]
                            (cache_dir / f"img_{url_hash}.png").write_bytes(raw_bytes)
                            uri = _file_bytes_to_data_uri(raw_bytes, lp.name)
                            if uri:
                                logger.info(f"通过 NapCat {api_name} 本地路径读取参考图成功: {lp} ({len(raw_bytes)} bytes)")
                                results.append(uri)
                                found_local = True
                                break

                    resp_url = str(resp.get("url") or "").strip()
                    if resp_url.startswith(("http://", "https://")) and not url:
                        url = resp_url
                except Exception as e:
                    logger.warning(f"调用 NapCat {api_name}({file_id}) 失败: {e}")

        if found_local:
            continue

        local_path = await _download_image_cached(url, file_hint=file_id)
        if local_path:
            p = Path(local_path)
            if p.is_file() and p.stat().st_size > 100:
                uri = _file_bytes_to_data_uri(p.read_bytes(), p.name)
                if uri:
                    results.append(uri)
                    continue

    return results


def _compress_for_qq_send(img_path: Path) -> bytes:
    """若生图原图 PNG 超过 750KB，利用 macOS 原生 sips 转为高质量 JPEG (q=85)，防止 NTQQ sendMsg 上传超时。"""
    raw = img_path.read_bytes()
    if len(raw) <= 750_000:
        return raw
    jpg_path = img_path.with_suffix(".qq_opt.jpg")
    try:
        if not jpg_path.is_file() or jpg_path.stat().st_size <= 100:
            subprocess.run(
                ["/usr/bin/sips", "-s", "format", "jpeg", "-s", "formatOptions", "85", str(img_path), "--out", str(jpg_path)],
                check=True,
                capture_output=True,
                timeout=8.0,
            )
        if jpg_path.is_file() and jpg_path.stat().st_size > 100:
            opt_bytes = jpg_path.read_bytes()
            logger.info(f"已优化大图发送体积: {img_path.name} ({len(raw)} -> {len(opt_bytes)} bytes)")
            return opt_bytes
    except Exception as exc:
        logger.warning(f"sips 压缩图片跳过，使用原图发送: {exc}")
    return raw


async def _get_image_segment(img_url: str) -> Optional[MessageSegment]:
    """优先下载并转为 base64:// 格式发给 NapCat，绕过 macOS QQ 沙盒磁盘权限限制与 OSS 链接失效。"""
    clean_url = _clean_extracted_url(img_url)
    local_file = await _download_image_cached(clean_url)
    if local_file:
        p = Path(local_file)
        if p.is_file() and p.stat().st_size > 0:
            try:
                send_bytes = _compress_for_qq_send(p)
                if not (send_bytes.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                        or (send_bytes.startswith(b"RIFF") and send_bytes[8:12] == b"WEBP")):
                    raise DrawPipelineError("图片地址返回了无效图片数据。")
                b64_str = base64.b64encode(send_bytes).decode("ascii")
                return MessageSegment.image(f"base64://{b64_str}")
            except Exception as e:
                logger.warning(f"图片转 base64 失败: {e}")
    logger.warning(f"图片 URL 无法下载或签名已截断，拦截裸链发送: {clean_url[:90]}")
    return None


async def _render_mixed_message(reply_text: str) -> Message:
    """自动将 Markdown 图片 ![描述](url)、[描述](图片url) 或图片直链转换为 OneBot MessageSegment.image 原图发送。"""
    if not reply_text:
        return Message("")

    msg = Message()
    md_pattern = re.compile(
        r"!\s*\[([^\]]*)\]\(\s*(https?://[^\s\)]+)\)?|\[([^\]]*)\]\(\s*(https?://[^\s\)]+(?:\.(?:png|jpg|jpeg|webp|gif)|dashscope|aliyuncs)[^\s\)]*)\)",
        re.IGNORECASE,
    )
    last_idx = 0
    found_any = False

    for match in md_pattern.finditer(reply_text):
        found_any = True
        start, end = match.span()
        if start > last_idx:
            prefix = reply_text[last_idx:start]
            if prefix:
                msg.append(MessageSegment.text(prefix))
        img_url = _clean_extracted_url(match.group(2) or match.group(4) or "")
        seg = await _get_image_segment(img_url)
        if seg is not None:
            msg.append(seg)
        last_idx = end

    if found_any:
        if last_idx < len(reply_text):
            suffix = reply_text[last_idx:]
            if suffix:
                msg.append(MessageSegment.text(suffix))
        return msg if len(msg) > 0 else Message("博士，图片渲染链接异常，请再试一次哦~")

    url_pattern = re.compile(
        r"(https?://[^\s\)\]\">'\uff09\u3011]+\.(?:png|jpg|jpeg|webp|gif)(?:\?[^\s\)\]\">'\uff09\u3011]*)?)",
        re.IGNORECASE,
    )
    last_idx = 0
    for match in url_pattern.finditer(reply_text):
        found_any = True
        start, end = match.span()
        if start > last_idx:
            prefix = reply_text[last_idx:start]
            if prefix:
                msg.append(MessageSegment.text(prefix))
        img_url = _clean_extracted_url(match.group(1))
        seg = await _get_image_segment(img_url)
        if seg is not None:
            msg.append(seg)
        last_idx = end

    if found_any:
        if last_idx < len(reply_text):
            suffix = reply_text[last_idx:]
            if suffix:
                msg.append(MessageSegment.text(suffix))
        return msg if len(msg) > 0 else Message("博士，图片渲染链接异常，请再试一次哦~")

    return Message(reply_text)


async def _safe_send(bot: Bot, event: MessageEvent, message: str | Message) -> bool:
    """带重试、防重发保护与单换行收敛（消除双 \\n 大段空白）的安全发送。"""
    from .modes import normalize_qq_linebreaks

    if isinstance(message, str):
        message = normalize_qq_linebreaks(message)
    elif isinstance(message, Message):
        for seg in message:
            if seg.type == "text" and isinstance(seg.data.get("text"), str):
                seg.data["text"] = re.sub(r"\n[ \t\u3000]*\n+", "\n", seg.data["text"].replace("\r\n", "\n"))
    for attempt in range(2):
        try:
            async with asyncio.timeout(8):
                await bot.send(event, message)
            return True
        except Exception as e:
            err_str = str(e)
            is_nt_timeout = isinstance(e, TimeoutError) or any(
                k in err_str
                for k in ("Timeout: NTEvent", "onMsgInfoListUpdate", "NodeIKernelMsgService/sendMsg")
            )
            if is_nt_timeout:
                logger.warning(f"QQ 消息回执超时，投递结果未知，不重发正文: {e}")
                return False

            logger.warning(f"发送消息异常 (尝试 {attempt + 1}/2): {e}")
            if attempt == 0:
                await asyncio.sleep(1.5)
    recorder.log("发送消息至 QQ 失败（NapCat/NTKernel 超时）", "ERROR")
    return False


async def _safe_send_voice(bot: Bot, event: MessageEvent, record_b64: str) -> bool:
    # 只发送当前请求的载荷，不能读取其他用户可能刚覆盖的 LAST_AUDIO_META 路径。
    try:
        async with asyncio.timeout(8):
            return await _safe_send(bot, event, MessageSegment.record(record_b64))
    except TimeoutError:
        recorder.log(f"Degradation task={current_task_id.get()} stage=voice_delivery reason=timeout", "WARN")
        return False


async def _resolve_input_files(bot: Bot, event: MessageEvent) -> list[str]:
    """提取当前消息或被引用消息中的语音条 (record)、文档与音视频附件。"""
    msgs_to_scan: list[Any] = []
    orig = getattr(event, "original_message", None) or event.message
    if orig:
        msgs_to_scan.append(orig)
    reply_obj = getattr(event, "reply", None)
    if reply_obj is not None and getattr(reply_obj, "message", None):
        msgs_to_scan.append(reply_obj.message)

    file_paths: list[str] = []
    for msg in msgs_to_scan:
        for seg in msg:
            if seg.type in {"file", "record", "video"}:
                fid = str(seg.data.get("file_id") or seg.data.get("file") or "").strip()
                path_hint = str(seg.data.get("path") or "").strip()
                url_hint = str(seg.data.get("url") or "").strip()

                for candidate_p in (path_hint, fid):
                    if candidate_p and candidate_p.startswith("/") and Path(candidate_p).is_file():
                        if Path(candidate_p).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                            break
                        file_paths.append(candidate_p)
                        break
                else:
                    if seg.type == "record" and fid:
                        got_record = False
                        for fmt in ("wav", "amr", "silk", "mp3"):
                            try:
                                resp = await asyncio.wait_for(
                                    bot.call_api("get_record", file=fid, out_format=fmt), timeout=10.0
                                )
                                if isinstance(resp, dict):
                                    local_f = str(resp.get("file") or resp.get("path") or "").strip()
                                    if local_f and local_f.startswith("/") and Path(local_f).is_file():
                                        logger.info(f"通过 get_record({fmt}) 获取语音本地路径成功: {local_f}")
                                        file_paths.append(local_f)
                                        got_record = True
                                        break
                            except Exception:
                                continue
                        if not got_record and url_hint.startswith(("http://", "https://")):
                            try:
                                import httpx
                                tmp_rec = Path("/tmp") / f"qq_rec_{int(time.time()*1000)}.silk"
                                async with httpx.AsyncClient(trust_env=False, timeout=15.0) as client:
                                    r = await client.get(url_hint)
                                    if r.status_code == 200 and len(r.content) > 50:
                                        tmp_rec.write_bytes(r.content)
                                        file_paths.append(str(tmp_rec))
                            except Exception as exc:
                                logger.warning(f"下载语音 URL 失败: {exc}")
                    elif fid:
                        try:
                            resp = await asyncio.wait_for(bot.call_api("get_file", file_id=fid), timeout=15.0)
                            if isinstance(resp, dict):
                                local_f = str(resp.get("file") or resp.get("path") or "").strip()
                                if local_f and local_f.startswith("/") and Path(local_f).is_file():
                                    if Path(local_f).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                                        file_paths.append(local_f)
                        except Exception as e:
                            logger.warning(f"调用 NapCat get_file({fid}) 失败: {e}")
    return file_paths


agent_msg = on_message(priority=50, block=False)
cmd_mac = on_command("mac", priority=5, block=True)
cmd_computer = on_command("电脑", aliases={"computer"}, priority=4, block=True)


@cmd_mac.handle()
async def handle_mac_command(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    from .device_actuator import mac_actions
    uid = _uid(event)
    if not isinstance(event, PrivateMessageEvent) or not mac_actions.mac.is_authorized_owner(uid):
        return
    parts = args.extract_plain_text().strip().split()
    if len(parts) != 2 or parts[0] not in {'确认', '取消', 'confirm', 'cancel'}:
        await cmd_mac.finish('使用 /mac 确认 动作ID，或 /mac 取消 动作ID；动作详情可在控制台查看。')
    try:
        job = await mac_actions.confirm(uid, parts[1], cancel=parts[0] in {'取消', 'cancel'})
    except (ValueError, PermissionError) as exc:
        await cmd_mac.finish(str(exc))
    await cmd_mac.finish(f"Mac 动作 {job['id']} 状态：{job['status']}。")


@cmd_computer.handle()
async def handle_computer_command(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    from .computer_use_service import computer_use_service

    uid = _uid(event)
    if not isinstance(event, PrivateMessageEvent):
        await cmd_computer.finish("桌面操作仅接受主人私聊指令。")
    text = args.extract_plain_text().strip()
    action, _, job_id = text.partition(" ")
    try:
        if action in {"确认", "confirm"} and job_id.strip():
            reply = await computer_use_service.confirm(uid, job_id.strip())
            await cmd_computer.finish(reply)
        elif action in {"停止", "stop"} and job_id.strip():
            reply = await computer_use_service.stop(uid, job_id.strip())
            await cmd_computer.finish(reply)
        else:
            reply = await computer_use_service.propose_explicit_task(uid, text, bot)
            await cmd_computer.finish(reply)
    except (PermissionError, RuntimeError, ValueError) as exc:
        await cmd_computer.finish(str(exc))
    await cmd_computer.finish(
        f"已接下桌面任务 {job_id}。完成后我会私聊汇报；需要你确认时会给出对应指令。"
    )


_conversation_cleanup_task: Optional[asyncio.Task] = None


@get_driver().on_startup
async def _start_conversation_cleanup():
    global _conversation_cleanup_task
    from .autonomous_agent import autonomous_coordinator

    autonomous_coordinator.set_silence_checker(
        lambda gid, u: _voice_reply_lock.locked() or _amiya_is_silenced(_amiya_silence_scope(gid, u))
    )
    await autonomous_coordinator.startup()

    async def cleanup():
        while True:
            try:
                conversation.store.purge()
            except Exception as exc:
                logger.warning(f"清理过期用户会话失败: {exc}")
            await asyncio.sleep(300)

    _conversation_cleanup_task = asyncio.create_task(cleanup())


@get_driver().on_shutdown
async def _stop_conversation_cleanup():
    from .autonomous_agent import autonomous_coordinator
    from .modes import training_job_manager
    from .sticker_service import amiya_sticker_service

    await autonomous_coordinator.shutdown()
    await training_job_manager.shutdown()
    await amiya_sticker_service.shutdown()
    from .device_actuator import mac_actions
    await mac_actions.shutdown()
    from .computer_use_service import computer_use_service
    await computer_use_service.shutdown()
    if _conversation_cleanup_task:
        _conversation_cleanup_task.cancel()
        try:
            await _conversation_cleanup_task
        except asyncio.CancelledError:
            pass


@agent_msg.handle()
async def handle_agent_message(bot: Bot, event: MessageEvent):
    from .autonomous_agent import autonomous_coordinator
    from .config_manager import ConfigManager
    from .group_chat_manager import (
        NormalQuotaRejectedError,
        QuotaReservation,
        QueuedGroupMessage,
        group_chat_manager,
        split_long_reply_segments,
    )
    from .modes import is_complex_or_long_query, is_training_group, record_group_message_for_training
    from .user_memory_service import user_memory_store

    autonomous_coordinator.set_silence_checker(
        lambda gid, u: _voice_reply_lock.locked() or _amiya_is_silenced(_amiya_silence_scope(gid, u))
    )

    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    uid = _uid(event)
    key = f"{group_id}:{uid}"
    msg_id_str = str(getattr(event, "message_id", "") or "")
    self_id_str = str(getattr(event, "self_id", "") or "")
    sender_nick = _sender_display_name(event)

    # 构造带文本摘要的事件消费键，既防止同条真实消息被普通链路与自主链路重复消费，又兼容单测固定 message_id=700 的不同消息
    event_consume_key = f"{group_id}:{msg_id_str}:{(event.get_plaintext() if hasattr(event, 'get_plaintext') else '')[:24]}" if (group_id and msg_id_str) else ""
    if event_consume_key and isinstance(state, ConfigManager) and group_chat_manager.is_consumed(event_consume_key):
        return

    # 默认始终开启群内全员焦点与上下文跟踪，但与“免艾特触发回复”严格隔离：
    # - 只有明确 @、带名字或引用回复阿米娅时才实时单条回复；
    # - 普通说话绝不因为之前艾特过就按人头免艾特实时秒回，统一走群聊合并批次。
    group_merge_active = bool(
        group_id is not None
        and isinstance(state, ConfigManager)
        and getattr(state, "group_buffer_enabled", False)
    )
    # 严格受控于控制面板的「默认回复模式 (auto_reply_mode)」开关，绝不因开启群合并而强制免艾特乱回！
    free_at_mode = bool(
        group_id is not None
        and getattr(state, "auto_reply_mode", False)
    )
    focus_followup_enabled = False
    parsed = parse_message(
        event,
        character=state.active_character,
        last_interaction=None if group_id is not None else _active_chat_window.get(key),
        now=time.monotonic(),
        detect_draw=llm_service._auto_detect_draw_intent,
        detect_formats=doc_service.detect_requested_formats,
    )
    if group_id is not None:
        parsed.in_continuous_chat = False
    if not bool(getattr(state, "global_active", True)):
        if isinstance(state, ConfigManager):
            autonomous_coordinator.clear_pending_events()
        return

    amiya_interaction = classify_amiya_interaction(
        parsed.clean_text,
        is_at_me=parsed.is_at_me,
        is_private=isinstance(event, PrivateMessageEvent),
        character=state.active_character,
    )
    computer_intent_candidate = False
    if isinstance(event, PrivateMessageEvent):
        from .computer_use_service import computer_use_service

        if await computer_use_service.handle_owner_confirmation(uid, parsed.clean_text, bot):
            return
        intent_event_key = f"private:{uid}:{msg_id_str}" if msg_id_str and msg_id_str != "0" else ""
        computer_intent_candidate = await computer_use_service.observe_owner_input(
            uid, parsed.clean_text, bot, event_key=intent_event_key,
        )
    if amiya_interaction:
        parsed.is_name_wake = True
    silence_scope = _amiya_silence_scope(group_id, uid)
    if group_id is None or _group_allowed(group_id):
        _cache_incoming_images(event, group_id, uid)
        if group_id is not None:
            from .sticker_service import amiya_sticker_service

            amiya_sticker_service.schedule_received_group_emojis(bot, event)

    reply_obj = getattr(event, "reply", None)
    r_txt = ""
    reply_to_msg_id: Optional[str] = None
    reply_to_uid: Optional[str] = None
    is_explicit_reply_to_me = False
    if reply_obj is not None:
        if getattr(reply_obj, "message_id", None) is not None:
            reply_to_msg_id = str(reply_obj.message_id)
        if getattr(reply_obj, "sender", None) is not None and getattr(reply_obj.sender, "user_id", None) is not None:
            reply_to_uid = str(reply_obj.sender.user_id)
            if self_id_str and reply_to_uid == self_id_str:
                is_explicit_reply_to_me = True
                if not parsed.has_other_at:
                    parsed.is_at_me = True
        if getattr(reply_obj, "message", None):
            r_txt, _ = _extract_text_and_forwards_from_segments(reply_obj.message)

    # 默认始终跟踪每个人的群聊对话焦点（record_group_message / record_bot_reply），保证阿米娅随时能接上任何人的话；
    # 同时与免艾特隔离：群合并开启时不把单人焦点直接变成免艾特实时秒回，普通说话统一走群聊合并。
    is_focus_followup = False
    if group_id and _group_allowed(group_id):
        group_chat_manager.attention.record_group_message(
            group_id,
            uid,
            reply_to_user_id=reply_to_uid,
            bot_self_id=self_id_str,
            text=parsed.clean_text,
        )
        if focus_followup_enabled:
            # 若用户在群内明确 [reply] 引用了其他群友（非机器人），且未 @ 阿米娅、未点名唤醒，主动释放焦点并不视为连续跟进
            if reply_to_uid and (not self_id_str or reply_to_uid != self_id_str) and not parsed.is_at_me and not parsed.is_name_wake:
                parsed.in_continuous_chat = False
                _active_chat_window.pop(key, None)
                group_chat_manager.attention.clear_focus(group_id, uid)
            elif not parsed.is_at_me and not parsed.is_name_wake and not is_explicit_reply_to_me:
                is_focus_followup = group_chat_manager.attention.should_followup_in_focus(
                    group_id,
                    uid,
                    has_other_at=parsed.has_other_at,
                    reply_to_user_id=reply_to_uid,
                    bot_self_id=self_id_str,
                    text=parsed.clean_text,
                )
                if is_focus_followup and len(parsed.clean_text) >= 1:
                    parsed.in_continuous_chat = True
                else:
                    parsed.in_continuous_chat = False
                    _active_chat_window.pop(key, None)

    # 强制自动开启白名单群与私聊的实时发言入库与用户画像总结（在 should_reply / 冷却拦截前无条件记录，防打断丢失）
    if (
        (group_id is None or _group_allowed(group_id))
        and isinstance(state, ConfigManager)
        and uid.isdigit()
        and parsed.clean_text
        and not parsed.clean_text.startswith("/")
    ):
        user_memory_store.ingest_realtime_message(
            uid=uid,
            user_text=parsed.clean_text,
            group_id=group_id,
            sender_nickname=sender_nick,
            message_id=msg_id_str,
            reply_to_msg_id=reply_to_msg_id or "",
            character=state.active_character,
            schedule_background_mem0=not state.voice_enabled,
        )

    if group_id and _group_allowed(group_id):
        from .expression_learner import expression_store

        raw_msg_obj = getattr(event, "original_message", None) or getattr(event, "message", None) or []
        img_count = sum(1 for seg in raw_msg_obj if getattr(seg, "type", "") in {"image", "mface"})
        face_count = sum(1 for seg in raw_msg_obj if getattr(seg, "type", "") == "face")
        media_summary = ""
        if img_count > 0:
            media_summary = f"[分享了{img_count}张图片/表情包]"
        elif face_count > 0 and not parsed.clean_text:
            media_summary = f"[发送了{face_count}个QQ表情]"

        if parsed.clean_text and not parsed.clean_text.startswith("/"):
            fb_ev = expression_store.observe_group_reaction(
                group_id,
                uid,
                parsed.clean_text,
                reply_to_bot=is_explicit_reply_to_me,
            )
            if fb_ev and isinstance(state, ConfigManager):
                autonomous_coordinator.record_interaction_feedback(
                    group_id=group_id,
                    uid=uid,
                    feedback_type="negative_reaction" if fb_ev.get("reaction") == "negative" else "positive_reaction",
                    detail=parsed.clean_text,
                    blacklisted_styles=fb_ev.get("blacklisted_styles"),
                )
        if amiya_interaction == "silence" and isinstance(state, ConfigManager):
            autonomous_coordinator.record_interaction_feedback(
                group_id=group_id,
                uid=uid,
                feedback_type="silence_command",
                detail=parsed.clean_text,
            )
        if not _voice_reply_lock.locked() and (parsed.clean_text or media_summary) and not parsed.clean_text.startswith("/"):
            autonomous_coordinator.enqueue_group_event(
                message_id=event_consume_key or msg_id_str,
                group_id=group_id,
                user_id=uid,
                nickname=sender_nick,
                text=parsed.clean_text,
                media_summary=media_summary,
            )
        if parsed.clean_text and not parsed.clean_text.startswith("/"):
            if is_training_group(group_id):
                record_group_message_for_training(
                    group_id=group_id,
                    sender_uid=uid,
                    sender_name=sender_nick,
                    raw_text=parsed.clean_text,
                    reply_to_text=r_txt,
                    reply_to_message_id=reply_to_msg_id,
                    message_id=msg_id_str,
                    bot_self_id=self_id_str,
                )
            elif isinstance(state, ConfigManager):
                expression_store.record_verified_turn_for_learning(
                    group_id=group_id,
                    message_id=msg_id_str,
                    sender_uid=uid,
                    sender_name=sender_nick,
                    text=parsed.clean_text,
                    reply_to_message_id=reply_to_msg_id,
                    reply_to_text=r_txt,
                    bot_self_id=self_id_str,
                )
                # 仅在本地队列中保留最近 60 条供手动触发学习，不再每 12 条群消息自动调用 DeepSeek 消耗额度
                pending_q = expression_store._pending_turns.get(group_id)
                if pending_q is not None and len(pending_q) > 60:
                    del pending_q[:-60]

    if not should_reply(
        parsed,
        global_active=state.global_active,
        is_private=isinstance(event, PrivateMessageEvent),
        group_allowed=group_id is not None and _group_allowed(group_id),
        auto_reply_mode=free_at_mode,
    ):
        return
    if group_id and getattr(state, "group_user_whitelist_enabled", False):
        whitelists = getattr(state, "group_user_whitelists", {}) or {}
        group_wl = set(str(u) for u in whitelists.get(str(group_id), []))
        super_qq = str(state.superuser_qq or "").strip()
        if group_wl and str(uid) not in group_wl and str(uid) != super_qq:
            logger.debug(f"[白名单拦截] 群 {group_id} 用户 {uid} 不在群用户白名单中，跳过响应")
            return
    if _voice_reply_lock.locked():
        _remember_voice_busy_message(uid, group_id, parsed.clean_text)
        if event_consume_key and isinstance(state, ConfigManager):
            group_chat_manager.mark_message_state(
                [event_consume_key], "skipped", group_id=group_id or "", user_id=uid,
                reason="voice_busy_history_recorded",
            )
        return
    from .device_actuator import mac_actions

    async def _device_feedback(job):
        if state.voice_enabled:
            return
        if job['status'] == 'prepared':
            message = f"Mac 动作已准备：{job['summary']}。私聊 /mac 确认 {job['id']} 或到控制台确认；10 分钟后失效。"
        else:
            message = f"Mac 动作状态：{job['status']}。详情请在控制台核对。"
        await bot.call_api('send_private_msg', user_id=int(uid), message=message)

    if amiya_interaction != 'silence' and not _amiya_is_silenced(silence_scope):
        if not computer_intent_candidate:
            mac_actions.schedule(uid, parsed.clean_text, f'{group_id or "private"}:{uid}:{msg_id_str}', feedback=_device_feedback)
        if isinstance(state, ConfigManager) and parsed.clean_text:
            autonomous_coordinator.capture_owner_promise(uid, parsed.clean_text, group_id=group_id)
    silence_was_active = _amiya_is_silenced(silence_scope)
    silence_is_pending = _AMIYA_SILENCE_UNTIL.get(silence_scope) == float("inf")
    if silence_was_active and (amiya_interaction != "wake" or silence_is_pending):
        return
    if silence_was_active:
        _AMIYA_SILENCE_UNTIL.pop(silence_scope, None)
        recorder.log(f"AmiyaSilence explicitly cleared by {uid} in {group_id or 'private'}")

    is_explicit_call = bool(
        parsed.is_at_me
        or parsed.intent == Intent.CONTROL
        or parsed.clean_text.startswith("/")
        or is_explicit_reply_to_me
        or parsed.is_name_wake
        or isinstance(event, PrivateMessageEvent)
    )
    is_quasi_priority_call = bool(
        focus_followup_enabled and not is_explicit_call and (is_focus_followup or parsed.in_continuous_chat)
    )

    # 群聊消息合并与发送配额控制（移植自 Group Chat Plus）
    merged_batch = None
    use_group_merger = bool(
        group_id is not None
        and isinstance(state, ConfigManager)
        and getattr(state, "group_buffer_enabled", False)
        and amiya_interaction != "silence"
        and parsed.intent == Intent.CHAT
        and not parsed.clean_text.startswith("/")
    )

    if amiya_interaction != "silence" and not (silence_was_active and amiya_interaction == "wake") and (
        (not use_group_merger and _in_cooldown(uid))
        or (group_id and not is_explicit_call and not is_quasi_priority_call and not use_group_merger and _in_cooldown(f"group:{group_id}"))
    ):
        return
    if amiya_interaction == "silence":
        _AMIYA_SILENCE_UNTIL[silence_scope] = float("inf")
    if group_id and focus_followup_enabled and is_explicit_call:
        now = time.monotonic()
        for expired in [k for k, ts in _active_chat_window.items() if now - ts > 45]:
            _active_chat_window.pop(expired, None)
        _active_chat_window[key] = now

    if group_id is not None and isinstance(state, ConfigManager):
        group_chat_manager.configure(
            merge_window_sec=float(getattr(state, "group_merge_window_sec", 1.5)),
            max_batch_messages=int(getattr(state, "group_merge_max_messages", 5)),
            max_batch_chars=int(getattr(state, "group_merge_max_chars", 2000)),
            min_interval_sec=float(getattr(state, "group_quota_min_interval_sec", 8.0)),
            max_per_minute=int(getattr(state, "group_quota_max_per_minute", 4)),
            chunk_pace_sec=float(getattr(state, "group_chunk_pace_sec", 1.2)),
        )
        # 非显式召唤的普通群消息严格受控于面板发送配额（最小间隔与每分钟上限）与旁观抑制
        if not is_explicit_call and not is_quasi_priority_call:
            if group_chat_manager.attention.is_bystander_suppressed(group_id):
                if event_consume_key:
                    group_chat_manager.mark_message_state(
                        [event_consume_key],
                        "skipped",
                        group_id=group_id,
                        user_id=uid,
                        reason="bystander_cross_talk_suppressed",
                    )
                return
            if use_group_merger:
                minute_ok, wait_rem, quota_reason = await group_chat_manager.density.get_remaining_normal_cooldown(group_id)
                max_merge_absorb = max(float(getattr(state, "group_merge_window_sec", 1.5)) * 2.5, 12.0)
                if not minute_ok or wait_rem > max_merge_absorb:
                    if event_consume_key:
                        group_chat_manager.mark_message_state(
                            [event_consume_key],
                            "skipped",
                            group_id=group_id,
                            user_id=uid,
                            reason=quota_reason if not minute_ok else f"interval_cooldown({wait_rem}s)",
                        )
                    return
            else:
                can_send_norm, quota_reason = await group_chat_manager.density.can_send_normal(group_id)
                if not can_send_norm:
                    if event_consume_key:
                        group_chat_manager.mark_message_state(
                            [event_consume_key],
                            "skipped",
                            group_id=group_id,
                            user_id=uid,
                            reason=quota_reason,
                        )
                    return
        elif is_explicit_call:
            group_chat_manager.attention.boost(group_id, uid, is_explicit=True)

    task_id = uuid.uuid4().hex
    tracker = recorder.progress_tracker
    tracker.update(task_id, status="pending", stage="等待群调度与用户会话", source=f"{group_id or '私聊'} | {uid}",
                   input_text=parsed.clean_text)

    batch_message_ids: list[str] = []
    if use_group_merger and group_id is not None:
        q_msg = QueuedGroupMessage(
            message_id=event_consume_key or msg_id_str or task_id[:10],
            group_id=group_id,
            user_id=uid,
            nickname=sender_nick,
            text=parsed.clean_text,
            timestamp=time.time(),
            reply_to_message_id=reply_to_msg_id,
            reply_to_user_id=reply_to_uid,
            reply_context=r_txt,
            is_at_me=parsed.is_at_me,
            is_command=parsed.clean_text.startswith("/"),
            is_explicit_reply_to_me=is_explicit_reply_to_me,
            is_name_wake=parsed.is_name_wake,
            is_quasi_priority=is_quasi_priority_call,
            has_other_at=parsed.has_other_at,
            raw_event=event,
            task_id=task_id,
        )
        merged_batch = await group_chat_manager.enqueue_and_collect_batch(q_msg)
        if merged_batch is None:
            # 已被同群窗口的 Leader 批次合并处理，不重复生成回复
            tracker.tasks.pop(task_id, None)
            return
        # 合并窗口等待结束唤醒后，二次校验控制面板开关（防止等待 5s 期间管理员已在面板关掉全局响应或免艾特开关）
        if not bool(getattr(state, "global_active", True)):
            group_chat_manager.mark_message_state(list(merged_batch.message_ids), "skipped", group_id=group_id, user_id=uid, reason="global_disabled_during_merge")
            tracker.tasks.pop(task_id, None)
            return
        if not merged_batch.is_priority_batch and not bool(getattr(state, "auto_reply_mode", False)):
            group_chat_manager.mark_message_state(list(merged_batch.message_ids), "skipped", group_id=group_id, user_id=uid, reason="auto_reply_disabled_during_merge")
            tracker.tasks.pop(task_id, None)
            return
        batch_message_ids = list(merged_batch.message_ids)
        if merged_batch.is_quasi_priority_batch:
            is_quasi_priority_call = True
        elif merged_batch.is_priority_batch:
            is_explicit_call = True
        primary_msg = merged_batch.primary_message
        if primary_msg.text:
            parsed.clean_text = primary_msg.text
        if primary_msg.raw_event is not None and primary_msg.user_id != uid:
            event = primary_msg.raw_event
            uid = primary_msg.user_id
            sender_nick = primary_msg.nickname
        if not isinstance(state, ConfigManager):
            for m_item in merged_batch.messages:
                if m_item.user_id.isdigit() and m_item.text:
                    user_memory_store.record_interaction_sync(
                        uid=m_item.user_id,
                        user_text=m_item.text,
                        group_id=group_id,
                        sender_nickname=m_item.nickname,
                        character=state.active_character,
                        schedule_background_mem0=not state.voice_enabled,
                    )
    else:
        if event_consume_key:
            batch_message_ids = [event_consume_key]
            if isinstance(state, ConfigManager):
                group_chat_manager.mark_message_state(
                    batch_message_ids,
                    "processing",
                    group_id=group_id or "",
                    user_id=uid,
                    task_id=task_id,
                    is_priority=is_explicit_call,
                )
        if not isinstance(state, ConfigManager) and uid.isdigit() and parsed.clean_text:
            user_memory_store.record_interaction_sync(
                uid=uid,
                user_text=parsed.clean_text,
                group_id=group_id,
                sender_nickname=sender_nick,
                character=state.active_character,
                schedule_background_mem0=not state.voice_enabled,
            )

    # 合并窗口结束后再次原子检查独占门；忙碌消息保留历史，不排队追答。
    has_incoming_image = bool(_extract_images_from_msg(getattr(event, "original_message", None) or event.message))
    voice_intent = (
        state.voice_enabled
        and not doc_service.detect_requested_formats(parsed.clean_text)
        and parsed.intent not in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}
        and not _is_draw_intent(parsed.clean_text, has_image=has_incoming_image)
    )
    voice_gate_acquired = False
    if _voice_reply_lock.locked():
        _remember_voice_busy_message(uid, group_id, parsed.clean_text)
        if batch_message_ids and isinstance(state, ConfigManager):
            group_chat_manager.mark_message_state(
                batch_message_ids, "skipped", group_id=group_id or "", user_id=uid,
                reason="voice_busy_history_recorded",
            )
        tracker.tasks.pop(task_id, None)
        return
    if voice_intent:
        await _voice_reply_lock.acquire()
        voice_gate_acquired = True
    voice_mode_turn = bool(
        state.voice_enabled
        and not doc_service.detect_requested_formats(parsed.clean_text)
        and parsed.intent not in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}
        and not _is_draw_intent(parsed.clean_text, has_image=has_incoming_image)
    )

    deferred_segmented: Optional[dict[str, Any]] = None
    with task_scope(task_id):
        try:
            tracker.update(task_id, status="running", stage="解析消息与等待群调度", percent=5)
            if parsed.intent in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}:
                budget = 180
            elif parsed.intent == Intent.DOCUMENT or _is_long_task_or_draw_intent(parsed.clean_text) or is_complex_or_long_query(parsed.clean_text):
                budget = 610
            else:
                budget = max(90, int(CHAT_TTS_TIMEOUT) + 35) if state.voice_enabled else 90
            async with asyncio.timeout(budget):
                deferred_segmented = await _handle_agent_message(
                    bot,
                    event,
                    parsed,
                    amiya_interaction=amiya_interaction,
                    silence_scope=silence_scope,
                    merged_batch=merged_batch,
                    is_explicit_call=is_explicit_call,
                    is_quasi_priority=is_quasi_priority_call,
                    task_id=task_id,
                    batch_message_ids=batch_message_ids,
                )

            # 若属于需要分批公平发送的长文本回答，在已释放 user_turn 与群生成锁的状态下执行分段投递
            if deferred_segmented is not None:
                full_reply = deferred_segmented["reply"]
                personal_query = deferred_segmented["personal_query"]
                sess_ref = deferred_segmented["session"]
                source_desc = deferred_segmented["source_desc"]
                seg_reservation: Optional[QuotaReservation] = deferred_segmented.get("quota_reservation")
                seg_msg_ids: list[str] = deferred_segmented.get("batch_message_ids") or batch_message_ids

                async def _send_one_seg(seg_text: str) -> bool:
                    return await _safe_send(bot, event, seg_text)

                deliv_state = await group_chat_manager.deliver_segmented_fairly(
                    task_id=task_id,
                    group_id=group_id,
                    user_id=uid,
                    full_text=full_reply,
                    send_fn=_send_one_seg,
                    max_segment_chars=180,
                    batch_chunk_size=1,
                    reservation=seg_reservation,
                    message_ids=seg_msg_ids,
                )
                if deliv_state.delivered_segments:
                    recorder.log(
                        f"已分段回复 [{source_desc}] ({len(deliv_state.delivered_segments)}/{len(deliv_state.segments)} 段, status={deliv_state.status}): '{deliv_state.delivered_text[:80]}'"
                    )
                    recorder.record_dialogue(
                        source=source_desc,
                        sender_id=uid,
                        user_text=personal_query,
                        character=state.active_character,
                        reply=deliv_state.delivered_text,
                        voice=False,
                    )
                    # 原子追加已送达内容与待发送余量（不假定用户看过了未送达的后半段）
                    with conversation.use_session(sess_ref, group_id=group_id):
                        conversation.store.remember(
                            sess_ref,
                            personal_query,
                            deliv_state.delivered_text,
                            character=state.active_character,
                            style=getattr(state, "small_talk_style", "pure_persona"),
                            delivery_status=deliv_state.status,
                            pending_segments=deliv_state.pending_segments,
                        )
                    if isinstance(state, ConfigManager):
                        user_memory_store.mark_message_replied(
                            uid=uid,
                            group_id=group_id,
                            message_id=msg_id_str,
                            user_text=parsed.clean_text,
                            bot_reply=deliv_state.delivered_text,
                        )
                    _touch_cooldown(uid)
                    if group_id:
                        _touch_cooldown(f"group:{group_id}")
                        if True:
                            group_chat_manager.attention.record_bot_reply(
                                group_id, uid, was_explicit=is_explicit_call
                            )
                            if is_explicit_call:
                                _active_chat_window[f"{group_id}:{uid}"] = time.monotonic()
                if deliv_state.status != "done":
                    raise MessageDeliveryError("长回答分段发送中途回执未确认，已暂停后续片段重发")

            tracker.update(task_id, status="done", stage="QQ 投递已确认", percent=100)
        except NormalQuotaRejectedError as quota_exc:
            # 普通群聊主动接话配额在并发竞争中被其他任务抢占或同群已有普通生成任务进行中，静默跳过并标记 skipped
            if batch_message_ids and isinstance(state, ConfigManager):
                group_chat_manager.mark_message_state(
                    batch_message_ids,
                    "skipped",
                    group_id=group_id or "",
                    user_id=uid,
                    task_id=task_id,
                    reason=quota_exc.reason,
                )
            tracker.tasks.pop(task_id, None)
            return
        except (MatcherException, ProcessException):
            raise
        except asyncio.CancelledError:
            if batch_message_ids and isinstance(state, ConfigManager):
                for mid in batch_message_ids:
                    if group_chat_manager.get_message_state(mid) not in {"delivered", "uncertain"}:
                        group_chat_manager.mark_message_state(
                            [mid], "failed", group_id=group_id or "", user_id=uid, task_id=task_id, reason="cancelled"
                        )
            tracker.update(task_id, status="error", stage="任务已取消或服务正在重启", percent=100)
            raise
        except Exception as exc:
            if batch_message_ids and isinstance(state, ConfigManager):
                for mid in batch_message_ids:
                    if group_chat_manager.get_message_state(mid) not in {"delivered", "uncertain"}:
                        group_chat_manager.mark_message_state(
                            [mid],
                            "failed",
                            group_id=group_id or "",
                            user_id=uid,
                            task_id=task_id,
                            reason=type(exc).__name__,
                        )
            tracker.update(task_id, status="error", stage=f"处理失败: {type(exc).__name__}", percent=100)
            logger.exception(f"AgentCoreError task={task_id} uid={uid} group={group_id}")
            if voice_mode_turn:
                recorder.log(
                    f"SuppressedVoiceOnlyError task={task_id} scope={group_id or 'private'} type={type(exc).__name__}; no text fallback",
                    "WARN",
                )
            elif group_id is not None or amiya_interaction == "silence":
                recorder.log(f"SuppressedReplyError task={task_id} group={group_id or 'private'} type={type(exc).__name__}", "WARN")
            else:
                char_now = (getattr(state, "active_character", "amiya") or "amiya").strip().lower()
                natural_fallbacks = {
                    "amiya": "唔…刚才网络晃了一下，你刚刚说的什么呀",
                    "dania": "哼，刚才信号闪了一下，你刚刚说的什么呀",
                    "kaltsit": "刚才通讯受到了干扰，你刚刚说的什么",
                }
                natural_msg = natural_fallbacks.get(char_now, "唔…刚才网络晃了一下，你刚刚说的什么呀")
                await _safe_send(bot, event, natural_msg)
        finally:
            if amiya_interaction == "silence" and _AMIYA_SILENCE_UNTIL.get(silence_scope) == float("inf"):
                _AMIYA_SILENCE_UNTIL.pop(silence_scope, None)
            if tracker.tasks.get(task_id, {}).get("status") in {"pending", "running"}:
                tracker.update(task_id, status="error", stage="流程未完成投递", percent=100)
            if voice_gate_acquired:
                _voice_reply_lock.release()


async def _send_exported_documents(bot: Bot, event: MessageEvent, files: list[Path], group_id: Optional[str], uid: str):
    if not files:
        await _safe_send(bot, event, "文档文件生成失败，请稍后重试。")
        return False
    sent = []
    failed = []
    for path in files:
        ok = await doc_service.send_file_to_qq(bot, event, path, group_id=group_id, user_id=uid)
        (sent if ok else failed).append(path.name)
    if sent:
        prefix = (MessageSegment.at(uid) + " ") if group_id else ""
        dest = "到群文件：" if group_id else "为私聊文件："
        await _safe_send(bot, event, prefix + "已上传" + dest + "、".join(sent))
    if failed:
        await _safe_send(bot, event, "文档正文已撰写保存，但 QQ 文件上传失败：" + "、".join(failed) + "。请稍后重试。")
    return bool(sent) and not failed


def _amiya_silence_scope(group_id: Optional[str], uid: str) -> str:
    return f"group:{group_id}" if group_id else f"private:{uid}"


def _amiya_is_silenced(scope: str) -> bool:
    now = time.monotonic()
    for key, deadline in tuple(_AMIYA_SILENCE_UNTIL.items()):
        if deadline != float("inf") and deadline <= now:
            _AMIYA_SILENCE_UNTIL.pop(key, None)
    return scope in _AMIYA_SILENCE_UNTIL


async def _generate_amiya_apology(user_text: str) -> str:
    fallback = "对不起，我刚才打扰到你了。我先安静十秒。"
    system = (
        "你是阿米娅。对方明确表示你的发言打扰到了他，或要求你停止说话。"
        "请真诚承认并简短道歉，只输出一句自然中文；不辩解、不反问、不卖惨、不要求对方原谅，"
        "不使用括号动作、Markdown 或机械称呼。最多30个汉字，可以说明会先安静十秒。"
    )
    try:
        async with asyncio.timeout(4.0):
            raw = await llm_service.chat(system, f"对方刚才说：{(user_text or '')[:160]}", max_tokens=48)
    except Exception as exc:
        logger.warning(f"阿米娅道歉短句生成失败，使用简短道歉: {type(exc).__name__}")
        return fallback

    reply = str(raw or "").replace("\r", "").split("\n", 1)[0].strip()
    reply = re.sub(r"[（(【\[].*?[）)】\]]", "", reply).strip()
    reply = re.sub(r"^[#>*`\s]+|[#>*`\s]+$", "", reply)
    reply = re.sub(r"^(?:博士|阿米娅)[，,：:\s]+", "", reply)
    reply = reply.strip(" \t\"'“”‘’")
    if not reply:
        return fallback
    if len(reply) > 40:
        first_sentence = re.split(r"[。！？!?]", reply, maxsplit=1)[0].strip()
        reply = first_sentence[:40] if first_sentence else reply[:40]
    return reply


async def _send_amiya_apology_and_silence(
    bot: Bot,
    event: MessageEvent,
    user_text: str,
    *,
    scope: str,
    group_id: Optional[str],
    uid: str,
) -> None:
    apology = await _generate_amiya_apology(user_text)
    voice_only = bool(getattr(state, "voice_enabled", False))
    try:
        if voice_only:
            payload = await tts_service.synthesize(apology, timeout=CHAT_TTS_TIMEOUT, latency_mode=True)
            sent = await _safe_send_voice(bot, event, payload)
        else:
            sent = await _safe_send(bot, event, apology)
    finally:
        _AMIYA_SILENCE_UNTIL[scope] = time.monotonic() + _AMIYA_SILENCE_SECONDS
    if not sent:
        recorder.log(f"AmiyaSilence apology delivery failed scope={scope}", "ERROR")
        raise MessageDeliveryError("阿米娅道歉回复未能送达")

    conversation.remember(user_text, apology)
    recorder.record_dialogue(
        source=f"群 {group_id}" if group_id else f"私聊 {uid}",
        sender_id=uid,
        user_text=user_text,
        character="amiya",
        reply=apology,
        voice=voice_only,
    )
    recorder.log(f"AmiyaSilence apology sent; muted for {_AMIYA_SILENCE_SECONDS:g}s scope={scope}")


async def _export_previous_reply(bot: Bot, event: MessageEvent, plain: str, group_id: Optional[str], uid: str):
    session = conversation.current()
    document = session.document if session else None
    last = next((turn for turn in reversed(session.history) if turn.get("assistant")), None) if session else None
    if not document and not last:
        raise DocumentGenerationError("当前没有可导出的对话内容，可能已超过 24 小时未继续对话。请重新发送文档要求或需要导出的正文。")
    content = document["content"] if document else last["assistant"]
    prompt = document["prompt"] if document else last["user"]
    formats = doc_service.detect_requested_formats(plain) or (document.get("formats") if document else None) or doc_service.detect_requested_formats(prompt) or ["docx", "pdf"]
    if (document and document.get("complete") is False) or doc_service.is_placeholder_document(content):
        await _safe_send(bot, event, "上一轮正文尚未完成，我会按原始要求继续生成，完成后导出文件。")
        content = await document_writer.generate(prompt, formats, resume=document if document and document.get("complete") is False else None)
    if not content or doc_service.is_placeholder_document(content):
        raise DocumentGenerationError("模型没有返回完整正文，本次未生成文件。请重试原始文档要求。")
    files = await doc_service.prepare_documents(doc_service.extract_doc_title(prompt, content), content, formats, state.active_character)
    missing = set(formats) - {path.suffix.lstrip(".") for path in files}
    if missing:
        raise DocumentGenerationError("以下格式生成失败：" + "、".join(sorted(missing)) + "；正文已保存，可回复“导出”重试。")
    sent = await _send_exported_documents(bot, event, files, group_id, uid)
    conversation.remember(plain, "文件已上传。" if sent else "文件投递失败，可重试导出。", document={"content": content, "prompt": prompt, "formats": formats, "complete": True})
    _touch_cooldown(uid)
    if group_id:
        _touch_cooldown(f"group:{group_id}")
    if not sent:
        raise DocumentGenerationError()


async def _build_bare_at_group_context(
    bot: Bot,
    group_id: Optional[str],
    uid: str,
    sender_nick: str,
    current_msg_id: str = "",
) -> str:
    """当群友纯艾特机器人（无附带文字）时，提取艾特之前群内大家正在聊的上下文，让机器人顺着话题自然接话，不强制指定回复某个人。"""
    if not group_id:
        return "对方刚刚向你打了个招呼，请用符合你当前人设的口吻自然简短地回应一句。"

    from .user_memory_service import user_memory_store

    gid_str = str(group_id).strip()
    cur_mid = str(current_msg_id or "").strip()
    recent_lines: list[str] = []
    seen_keys: set[str] = set()

    # 1. 优先从实时落盘的 group_chat_logs 与内存群时间轴读取艾特之前的近期发言
    try:
        db_rows = user_memory_store.get_group_chat_history(gid_str, limit=10)
        for r in db_rows:
            mid = str(r.get("message_id") or "").strip()
            if cur_mid and mid == cur_mid:
                continue
            txt = str(r.get("text") or "").strip()
            if not txt or txt.startswith("/"):
                continue
            nick = str(r.get("nickname") or r.get("uid") or "群友").strip()
            dedup_k = f"{r.get('uid')}:{txt}"
            if dedup_k not in seen_keys:
                seen_keys.add(dedup_k)
                recent_lines.append(f"- {nick}: {txt[:80]}")
    except Exception:
        pass

    try:
        dq = local_rag_service._GROUP_TIMELINE.get(gid_str) or []
        for m in dq:
            mid = str(m.get("message_id") or "").strip()
            if cur_mid and mid == cur_mid:
                continue
            txt = str(m.get("text") or "").strip()
            if not txt or txt.startswith("/"):
                continue
            nick = str(m.get("sender_name") or m.get("uid") or "群友").strip()
            dedup_k = f"{m.get('uid')}:{txt}"
            if dedup_k not in seen_keys:
                seen_keys.add(dedup_k)
                recent_lines.append(f"- {nick}: {txt[:80]}")
    except Exception:
        pass

    # 2. 若本地记录不足 2 条，主动向 NapCat 拉取最近 10 条群消息兜底
    if len(recent_lines) < 2:
        try:
            hist_resp = await asyncio.wait_for(
                bot.call_api("get_group_msg_history", group_id=int(gid_str), count=10),
                timeout=3.5,
            )
            hist_msgs = (hist_resp.get("messages") if isinstance(hist_resp, dict) else None) or []
            if isinstance(hist_msgs, list):
                for hm in hist_msgs:
                    if not isinstance(hm, dict):
                        continue
                    mid = str(hm.get("message_id") or hm.get("real_id") or "").strip()
                    if cur_mid and mid == cur_mid:
                        continue
                    hm_txt, _ = _extract_text_and_forwards_from_segments(hm.get("message") or hm.get("raw_message"))
                    hm_txt = (hm_txt or "").strip()
                    if not hm_txt or hm_txt.startswith("/"):
                        continue
                    s_info = hm.get("sender") or {}
                    s_uid = str(s_info.get("user_id") or hm.get("user_id") or "").strip()
                    if s_uid and s_uid == str(getattr(bot, "self_id", "")):
                        continue
                    s_nick = str(s_info.get("card") or s_info.get("nickname") or s_uid or "群友").strip()
                    dedup_k = f"{s_uid}:{hm_txt}"
                    if dedup_k not in seen_keys:
                        seen_keys.add(dedup_k)
                        recent_lines.append(f"- {s_nick}: {hm_txt[:80]}")
        except Exception:
            pass

    recent_lines = recent_lines[-8:]
    if recent_lines:
        return (
            "【群内刚才大家聊到的内容（按时间先后）】：\n"
            + "\n".join(recent_lines)
            + f"\n\n【当前情况】：群友「{sender_nick}」刚刚在群里单纯艾特了你一下（未附带具体问题）。"
            "请根据上面艾特你之前大家正在聊的内容自然接上一句话，不强制指定回复某一个人的需求，能顺着群里的话题自然接上话就行。"
        )
    return f"群友「{sender_nick}」刚刚在群里单纯艾特了你一下，请用符合你当前人设的口吻自然简短地回应一句。"


async def _handle_agent_message(
    bot: Bot,
    event: MessageEvent,
    parsed: ParsedMessage,
    *,
    amiya_interaction: Optional[str] = None,
    silence_scope: Optional[str] = None,
    merged_batch: Any = None,
    session_obj: Any = None,
    is_explicit_call: bool = True,
    is_quasi_priority: bool = False,
    task_id: Optional[str] = None,
    batch_message_ids: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    from .config_manager import ConfigManager
    from .group_chat_manager import QuotaReservation, group_chat_manager, split_long_reply_segments
    from .modes import is_complex_or_long_query
    from .sticker_service import amiya_sticker_service, clear_decision, take_decision

    clear_decision()
    sticker_id: Optional[str] = None

    uid = _uid(event)
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    plain = parsed.clean_text
    source_desc = f"群 {group_id}" if group_id else f"私聊 {uid}"
    eff_task_id = task_id or current_task_id.get() or uuid.uuid4().hex
    msg_ids = list(batch_message_ids or [])

    if amiya_interaction == "silence":
        async with conversation.store.user_turn(uid):
            sess = session_obj or conversation.store.begin(uid)
            with conversation.use_session(sess, group_id=group_id):
                await _send_amiya_apology_and_silence(
                    bot,
                    event,
                    plain,
                    scope=silence_scope or _amiya_silence_scope(group_id, uid),
                    group_id=group_id,
                    uid=uid,
                )
        if msg_ids and isinstance(state, ConfigManager):
            group_chat_manager.mark_message_state(msg_ids, "delivered", group_id=group_id or "", user_id=uid, task_id=eff_task_id)
        return None

    if doc_service.is_export_followup(plain):
        async with conversation.store.user_turn(uid):
            sess = session_obj or conversation.store.begin(uid)
            with conversation.use_session(sess, group_id=group_id):
                await _export_previous_reply(bot, event, plain, group_id, uid)
        if msg_ids and isinstance(state, ConfigManager):
            group_chat_manager.mark_message_state(msg_ids, "delivered", group_id=group_id or "", user_id=uid, task_id=eff_task_id)
        return None

    reply_context_str, ctx_img_infos = await _resolve_reply_and_forward_context(bot, event, group_id, uid, plain)
    parsed.reply_context = reply_context_str
    extra_file_paths = await _resolve_input_files(bot, event)

    image_b64_list: list[str] = []
    if ctx_img_infos:
        image_b64_list = await _resolve_input_images_b64(bot, ctx_img_infos)
        recorder.log(f"已提取关联输入图片 {len(image_b64_list)}/{len(ctx_img_infos)} 张")
    if not image_b64_list and _is_draw_intent(plain) and (
        ctx_img_infos or (getattr(event, "reply", None) and any(word in plain for word in ("差分", "这张", "这个", "重绘", "改图")))
    ):
        raise DrawPipelineError("没有读取到你指定的参考图，请重新发送或引用原图；本次不会替换为本地角色图。")

    # 若存在同群合并的多条消息批次，构建保留各发言人 QQ 号、昵称、时间与引用关系的合并上下文；
    # 但写入个人会话 (conversations.sqlite3) 时仅记录当前主发言人自己的发言 (personal_query_for_history)，防止多人发言混写进单人会话！
    personal_query_for_history = plain
    if merged_batch is not None and len(getattr(merged_batch, "messages", [])) > 1:
        effective_query = merged_batch.format_merged_context()
    else:
        effective_query = plain

    if reply_context_str:
        if effective_query:
            effective_query = f"{reply_context_str}\n\n【用户对上述引用内容的指令/追问】：\n{effective_query}"
        else:
            effective_query = f"{reply_context_str}\n\n请总结或回应上述引用内容。"
        if personal_query_for_history:
            personal_query_for_history = f"{reply_context_str}\n\n【用户对上述引用内容的指令/追问】：\n{personal_query_for_history}"
        else:
            personal_query_for_history = effective_query

    parsed.intent = classify_intent(plain, has_image=bool(image_b64_list),
                                    detect_draw=llm_service._auto_detect_draw_intent,
                                    detect_formats=doc_service.detect_requested_formats)
    if group_id is not None and not is_explicit_call and parsed.intent in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}:
        parsed.intent = Intent.CHAT
    explicit_formats = doc_service.detect_requested_formats(plain)

    # 群优先调度 + 原子配额预占 + 短会话锁：
    # 1. 先在群调度器中原子完成检查额度、预留处理位置与登记预占（等待群调度期间绝不占用用户 user_turn 锁）；
    # 2. 获得群生成槽位后再进入 user_turn(uid) 并读取当前最新 Session，生成完毕立即释放群生成槽位与 user_turn 锁！
    @contextlib.asynccontextmanager
    async def _acquire_gen_and_session():
        if group_id is not None and isinstance(state, ConfigManager):
            async with group_chat_manager.acquire_generation_slot(
                group_id,
                task_id=eff_task_id,
                is_priority=is_explicit_call,
                is_quasi_priority=is_quasi_priority,
                reserve_quota=True,
                auto_release_on_exit=False,
            ) as q_res:
                async with conversation.store.user_turn(uid):
                    sess = conversation.store.begin(uid)
                    with conversation.use_session(sess, group_id=group_id):
                        yield q_res, sess
        else:
            async with conversation.store.user_turn(uid):
                sess = conversation.store.begin(uid)
                with conversation.use_session(sess, group_id=group_id):
                    yield None, sess

    global _tts_synthesizing
    is_group_msg = group_id is not None
    if state.voice_enabled and not explicit_formats and not _is_draw_intent(plain, has_image=bool(image_b64_list)):
        if not effective_query:
            if image_b64_list:
                user_query = "对方分享了一张图片，请像朋友聊天一样用口语自然回应一句。"
            else:
                user_query = await _build_bare_at_group_context(
                    bot, group_id, uid, _sender_display_name(event), str(getattr(event, "message_id", "") or "")
                )
        else:
            user_query = effective_query
        recorder.log(
            f"[语音模式] 接收消息 [{source_desc}] '{user_query[:35]}' (附图={len(image_b64_list)}) -> 正在由角色({state.active_character})生成语音台词..."
        )
        quota_res: Optional[QuotaReservation] = None
        active_sess = None
        async with _acquire_gen_and_session() as (quota_res, active_sess):
            spoken_text = await _dispatch_reply(
                uid=uid,
                group_id=group_id,
                user_text=user_query,
                is_long_task=False,
                is_draw=False,
                image_b64_list=image_b64_list,
                file_paths=extra_file_paths,
                voice_mode_only=True,
            )
            if not spoken_text:
                if quota_res is not None and group_id:
                    await group_chat_manager.density.release_reservation(quota_res, group_id, reason="empty_voice_reply")
                raise LLMProviderError()

        _touch_cooldown(uid)
        if group_id:
            _touch_cooldown(f"group:{group_id}")

        emotion, clean_spoken_text = parse_and_strip_voice_emotion(spoken_text, character=state.active_character)

        _tts_synthesizing = True
        try:
            record_payload = await tts_service.synthesize(
                clean_spoken_text,
                timeout=CHAT_TTS_TIMEOUT,
                truncate_long=True,
                emotion_tag=emotion,
                latency_mode=True,
            )
        except TTSGenerationError as exc:
            recorder.log(f"Degradation task={current_task_id.get()} stage=tts reason={exc}", "WARN")
            record_payload = None
        except BaseException:
            if quota_res is not None and group_id:
                await group_chat_manager.density.release_reservation(quota_res, group_id, reason="tts_aborted")
            raise
        finally:
            _tts_synthesizing = False

        if record_payload:
            if group_id and isinstance(state, ConfigManager):
                sent_ok = await group_chat_manager.density.send_with_pacing(
                    group_id,
                    lambda: _safe_send_voice(bot, event, record_payload),
                    reservation=quota_res,
                    is_priority=bool(is_explicit_call or is_quasi_priority),
                )
            else:
                sent_ok = await _safe_send_voice(bot, event, record_payload)
            if sent_ok:
                wav_saved = tts_service.LAST_AUDIO_META.get("wav_path", "")
                dur_sec = tts_service.LAST_AUDIO_META.get("duration_sec", 0)
                recorder.log(
                    f"[语音模式] 已发送纯语音回复 [{source_desc}] ({dur_sec}s, 情绪={emotion}, 归档: {Path(wav_saved).name if wav_saved else 'mem'}): '{clean_spoken_text[:35]}'"
                )
                recorder.record_dialogue(
                    source=source_desc,
                    sender_id=uid,
                    user_text=personal_query_for_history or user_query,
                    character=state.active_character,
                    reply=clean_spoken_text,
                    voice=True,
                )
                if active_sess is not None:
                    with conversation.use_session(active_sess, group_id=group_id):
                        conversation.store.remember(active_sess, personal_query_for_history or user_query, clean_spoken_text)
                if isinstance(state, ConfigManager):
                    from .user_memory_service import user_memory_store

                    user_memory_store.mark_message_replied(
                        uid=uid,
                        group_id=group_id,
                        message_id=str(getattr(event, "message_id", "") or ""),
                        user_text=plain,
                        bot_reply=clean_spoken_text,
                    )
                if msg_ids and isinstance(state, ConfigManager):
                    group_chat_manager.mark_message_state(msg_ids, "delivered", group_id=group_id or "", user_id=uid, task_id=eff_task_id)
                _touch_cooldown(uid)
                if group_id:
                    _touch_cooldown(f"group:{group_id}")
                    if True:
                        group_chat_manager.attention.record_bot_reply(group_id, uid, was_explicit=is_explicit_call)
                        if is_explicit_call:
                            _active_chat_window[f"{group_id}:{uid}"] = time.monotonic()
                return None
        if quota_res is not None and quota_res.status == "reserved" and group_id:
            await group_chat_manager.density.release_reservation(
                quota_res,
                group_id,
                reason="voice_only_generation_or_delivery_failed",
            )
        raise TTSGenerationError("语音生成或发送失败；语音模式不发送文字替代。")

    if not effective_query and not image_b64_list and not extra_file_paths:
        effective_query = await _build_bare_at_group_context(
            bot, group_id, uid, _sender_display_name(event), str(getattr(event, "message_id", "") or "")
        )
        personal_query_for_history = "[艾特了你，结合群聊前文接话]" if group_id else "[向你打招呼]"
    elif not effective_query and image_b64_list:
        effective_query = "对方在聊天中分享了一张图片，请结合画面内容自然接话回应。"
        personal_query_for_history = effective_query
    elif not effective_query and extra_file_paths:
        if any(Path(p).suffix.lower() in {".wav", ".mp3", ".amr", ".silk", ".m4a", ".ogg"} or "rec" in p.lower() for p in extra_file_paths):
            effective_query = "对方发了一段语音，请解析语音内容并自然回应。"
        else:
            effective_query = "对方发了一份文件，请解析文件内容并自然回应。"
        personal_query_for_history = effective_query

    save_ref_match = _SAVE_REF_ONLY_RE.search(plain) if image_b64_list else None
    is_draw = bool(
        parsed.intent in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}
        and (group_id is None or is_explicit_call)
    )

    is_doc_task = bool(explicit_formats) and not is_draw
    is_long_task = _is_long_task_or_draw_intent(plain, has_image=bool(image_b64_list)) or bool(extra_file_paths) or is_doc_task
    if is_doc_task:
        ack_msg = (MessageSegment.at(uid) + " 已经收到文档要求啦，正在撰写中，请稍候片刻~") if group_id else "已经收到文档要求啦，正在撰写中，请稍候片刻~"
        await _safe_send(bot, event, ack_msg)
        recorder.log(f"已发送文档接单确认 [{source_desc}]")
    elif is_long_task or is_draw:
        ack_text = _get_ack_message(state.active_character, plain, has_ref_img=bool(image_b64_list))
        await _safe_send(bot, event, ack_text)
        recorder.log(f"已发送待机确认 [{source_desc}]: '{ack_text}'")

    if image_b64_list and plain:
        llm_service._maybe_save_character_ref(plain, image_b64_list)
        if save_ref_match and not is_draw:
            cname = save_ref_match.group(1).strip()
            confirmed = await _safe_send(
                bot,
                event,
                f"已经把「{cname}」的参考立绘原型牢牢记住并归档啦！之后只要点名画{cname}，就会自动参考这张设定哦~",
            )
            if not confirmed:
                raise MessageDeliveryError()
            if msg_ids and isinstance(state, ConfigManager):
                group_chat_manager.mark_message_state(msg_ids, "delivered", group_id=group_id or "", user_id=uid, task_id=eff_task_id)
            return None

    recorder.log(
        f"正在处理 [{source_desc}] '{effective_query[:60]}' -> 模式: {state.active_character} "
        f"(生图={is_draw}, 长任务={is_long_task}, 附图={len(image_b64_list)}, 附件={len(extra_file_paths)})"
    )
    recorder.progress_tracker.update(current_task_id.get(), kind="image" if is_draw else "text",
                                     stage="调用模型", percent=15)

    writing_active = True

    async def _doc_30s_heartbeat():
        try:
            await asyncio.sleep(30.0)
            if writing_active:
                hb_msg = (MessageSegment.at(uid) + " 内容篇幅较长，正在深入撰写中，请稍候片刻哦~") if group_id else "内容篇幅较长，正在深入撰写中，请稍候片刻哦~"
                await _safe_send(bot, event, hb_msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    hb_task = asyncio.create_task(_doc_30s_heartbeat()) if is_doc_task else None
    quota_res: Optional[QuotaReservation] = None
    active_sess = None

    try:
        async with _acquire_gen_and_session() as (quota_res, active_sess):
            # 动态防翻旧账机制（不再写死固定秒数）：
            # 1. 若排队等待生成槽位期间，同群合并缓冲区已进入更新的群消息（已被新话题覆盖），直接让位给最新批次；
            # 2. 动态超时阈值随面板设置的「合并窗口秒数」与「普通参与最小间隔」自适应计算。
            if group_id is not None and not is_explicit_call and merged_batch is not None and getattr(merged_batch, "messages", None):
                latest_msg_ts = float(merged_batch.messages[-1].timestamp or 0.0)
                cfg_win = float(getattr(state, "group_merge_window_sec", 1.5) or 1.5)
                cfg_gap = float(getattr(state, "group_quota_min_interval_sec", 4.5) or 4.5)
                max_batch_age = max(cfg_win * 2.5 + 4.0, cfg_gap + 5.0, 10.0)
                newer_pending = bool(group_chat_manager._pending_buffers.get(group_id))
                is_time_stale = bool(latest_msg_ts > 1000000000.0 and (time.time() - latest_msg_ts) > max_batch_age)
                if newer_pending or is_time_stale:
                    drop_reason = "superseded_by_newer_group_messages" if newer_pending else "stale_batch_dropped"
                    if quota_res is not None:
                        await group_chat_manager.density.release_reservation(quota_res, group_id, reason=drop_reason)
                    if msg_ids and isinstance(state, ConfigManager):
                        group_chat_manager.mark_message_state(msg_ids, "skipped", group_id=group_id, user_id=uid, task_id=eff_task_id, reason=drop_reason)
                    return None
            reply = await _dispatch_reply(
                uid=uid, group_id=group_id,
                user_text=(effective_query + doc_service.DOCUMENT_PROMPT if explicit_formats else plain if is_draw else effective_query),
                is_long_task=is_long_task, is_draw=is_draw, image_b64_list=image_b64_list, file_paths=extra_file_paths,
            )
            sticker_id = take_decision()
            if not reply:
                if quota_res is not None and group_id:
                    await group_chat_manager.density.release_reservation(quota_res, group_id, reason="empty_reply")
                if not is_explicit_call and group_id is not None:
                    # 普通无人请求的水群消息允许最终决定不回复，释放预占并标记 skipped
                    if msg_ids and isinstance(state, ConfigManager):
                        group_chat_manager.mark_message_state(msg_ids, "skipped", group_id=group_id, user_id=uid, task_id=eff_task_id, reason="chose_no_reply")
                    return None
                raise DrawPipelineError() if is_draw else LLMProviderError()
            recorder.progress_tracker.update(current_task_id.get(), stage="准备文件或图片投递", percent=85, output_text=reply)

            if explicit_formats and doc_service.is_placeholder_document(reply):
                reply = await _dispatch_reply(
                    uid=uid,
                    group_id=group_id,
                    user_text=effective_query + doc_service.DOCUMENT_PROMPT,
                    is_long_task=True,
                    file_paths=extra_file_paths,
                )
                if not reply or doc_service.is_placeholder_document(reply):
                    if quota_res is not None and group_id:
                        await group_chat_manager.density.release_reservation(quota_res, group_id, reason="doc_placeholder_failed")
                    raise DocumentGenerationError("模型没有返回文档正文，本次未生成或上传文件。请稍后重试。")
    finally:
        writing_active = False
        if hb_task and not hb_task.done():
            hb_task.cancel()
            try:
                await asyncio.shield(asyncio.wait_for(hb_task, timeout=1.0))
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    _touch_cooldown(uid)
    if group_id:
        _touch_cooldown(f"group:{group_id}")

    req_formats = doc_service.detect_requested_formats(plain, reply_len=len(reply), is_long_task=is_long_task)

    # 若属于纯文本长回答或多阶段解释（无需导出文件、非生图、且分段数 > 1），交由外层在已释放 user_turn 与生成锁后执行分批多气泡公平投递！
    if (
        not req_formats
        and not is_draw
        and "![插画]" not in reply
        and not re.search(r"https?://[^\s]+\.(?:png|jpg|jpeg|webp|gif)", reply, re.IGNORECASE)
        and (len(reply) > 150 or (is_complex_or_long_query(plain) and "\n" in reply.strip()))
    ):
        segs = split_long_reply_segments(reply, max_segment_chars=180)
        if len(segs) > 1 and active_sess is not None:
            return {
                "reply": reply,
                "personal_query": personal_query_for_history or effective_query,
                "session": active_sess,
                "source_desc": source_desc,
                "quota_reservation": quota_res,
                "batch_message_ids": msg_ids,
                "is_explicit_call": is_explicit_call,
            }

    try:
        exported_files: list[Path] = []
        if req_formats and not is_draw and "![插画]" not in reply:
            doc_title = doc_service.extract_doc_title(plain, reply)
            exported_files = await doc_service.prepare_documents(
                title=doc_title,
                content=reply,
                formats=req_formats,
                character=state.active_character,
            )
            missing = set(req_formats) - {path.suffix.lstrip(".") for path in exported_files}
            if missing:
                raise DocumentGenerationError("以下格式生成失败：" + "、".join(sorted(missing)) + "；正文已保存，可回复“导出”重试。")
            if exported_files and explicit_formats:
                msg_to_send = Message("正文已生成，正在上传文件：" + "、".join(p.name for p in exported_files))
            elif exported_files and len(reply) >= doc_service.AUTO_FILE_CHAR_THRESHOLD:
                fnames_str = "、".join(f"《{p.name}》" for p in exported_files)
                preview_text = (
                    f"📄 **文档已生成，正在投递**：{fnames_str}（全文共 {len(reply)} 字）\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"【内容导读】：\n{reply[:280].strip()}……\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"文件投递结果稍后单独通知。"
                )
                msg_to_send = Message(preview_text)
            else:
                msg_to_send = await _render_mixed_message(reply)
        else:
            msg_to_send = await _render_mixed_message(reply)

        if is_draw and not any(seg.type == "image" for seg in msg_to_send):
            raise DrawPipelineError("图片生成结果无法下载，本次没有成功交付图片，请稍后重试。")

        if group_id and isinstance(state, ConfigManager):
            sent_ok = await group_chat_manager.density.send_with_pacing(
                group_id,
                lambda: _safe_send(bot, event, msg_to_send),
                reservation=quota_res,
                is_priority=bool(is_explicit_call or is_quasi_priority),
            )
        else:
            sent_ok = await _safe_send(bot, event, msg_to_send)

        if sent_ok:
            if msg_ids and isinstance(state, ConfigManager):
                group_chat_manager.mark_message_state(msg_ids, "delivered", group_id=group_id or "", user_id=uid, task_id=eff_task_id)
            recorder.log(f"已回复 [{source_desc}]: '{reply[:80]}'")
            recorder.record_dialogue(
                source=source_desc,
                sender_id=uid,
                user_text=personal_query_for_history or effective_query,
                character=state.active_character,
                reply=reply,
                voice=False,
            )
            _touch_cooldown(uid)
            if group_id:
                _touch_cooldown(f"group:{group_id}")
                if True:
                    group_chat_manager.attention.record_bot_reply(group_id, uid, was_explicit=is_explicit_call)
                    if is_explicit_call:
                        _active_chat_window[f"{group_id}:{uid}"] = time.monotonic()

            if (
                sticker_id
                and not req_formats
                and not is_draw
                and not is_long_task
                and not image_b64_list
                and not extra_file_paths
            ):
                active_character = (state.active_character or "none").strip().lower()
                amiya_active = active_character in {"amiya", "assistant", "none", "default", ""}

                async def _send_sticker_segment(segment: MessageSegment) -> bool:
                    return await _safe_send(bot, event, segment)

                def _sticker_still_allowed() -> bool:
                    return (
                        bool(getattr(state, "global_active", True))
                        and (state.active_character or "none").strip().lower()
                        in {"amiya", "assistant", "none", "default", ""}
                        and not _amiya_is_silenced(_amiya_silence_scope(group_id, uid))
                    )

                if amiya_active:
                    await amiya_sticker_service.schedule(
                        group_id=group_id,
                        user_id=uid,
                        task_id=eff_task_id,
                        sticker_id=sticker_id,
                        send_segment=_send_sticker_segment,
                        still_allowed=_sticker_still_allowed,
                    )

        file_sent = False
        if req_formats and not is_draw:
            file_sent = await _send_exported_documents(bot, event, exported_files, group_id, uid)
        if sent_ok or file_sent:
            document = {"content": reply, "prompt": plain, "formats": req_formats, "complete": True} if req_formats and not is_draw else None
            if active_sess is not None:
                with conversation.use_session(active_sess, group_id=group_id):
                    conversation.store.remember(
                        active_sess,
                        personal_query_for_history or effective_query,
                        reply,
                        document=document,
                    )
            if isinstance(state, ConfigManager):
                from .user_memory_service import user_memory_store

                user_memory_store.mark_message_replied(
                    uid=uid,
                    group_id=group_id,
                    message_id=str(getattr(event, "message_id", "") or ""),
                    user_text=plain,
                    bot_reply=reply,
                )
        if not sent_ok:
            raise MessageDeliveryError()
        if req_formats and not is_draw and not file_sent and explicit_formats:
            raise DocumentGenerationError()
        return None
    except BaseException:
        if quota_res is not None and quota_res.status == "reserved" and group_id:
            await group_chat_manager.density.release_reservation(quota_res, group_id, reason="post_gen_delivery_error")
        raise


async def _dispatch_reply(
    **kwargs,
) -> Optional[str]:
    prompt = kwargs.get("user_text", "")
    formats = doc_service.detect_requested_formats(llm_service._strip_reply_context_wrapper(prompt))
    if formats and not kwargs.get("voice_mode_only") and not kwargs.get("is_draw") and not kwargs.get("image_b64_list") and not kwargs.get("file_paths"):
        return await document_writer.generate(prompt.removesuffix(doc_service.DOCUMENT_PROMPT), formats)
    with conversation.history_enabled(not kwargs.get("is_draw", False)):
        return await _dispatch_reply_impl(**kwargs)


async def _dispatch_reply_impl(
    *,
    uid: str,
    group_id: Optional[str],
    user_text: str,
    is_long_task: bool = False,
    is_draw: bool = False,
    image_b64_list: Optional[list[str]] = None,
    file_paths: Optional[list[str]] = None,
    voice_mode_only: bool = False,
) -> Optional[str]:
    """模式优先级：闲聊模式(固定人格+群表达参考) > 安慰（非生图） > 干员 > 群画像 > 自定义/默认。"""
    from .modes import build_small_talk_system_prompt, format_small_talk_output, is_complex_or_long_query
    from .device_actuator import mac_actions
    from .autonomous_agent import autonomous_coordinator
    from .config_manager import ConfigManager

    device_instruction = (
        '\n【设备动作状态】本轮设备操作由独立执行器准备，尚无成功回执。只可说明正在准备、可到控制台核对；禁止声称已经放歌、创建提醒或发出微信消息。'
        if mac_actions.relevant(uid, user_text) else ''
    )
    if isinstance(state, ConfigManager):
        promise_hint = autonomous_coordinator.get_active_promises_prompt_hint(uid)
        if promise_hint:
            device_instruction += promise_hint
        owner_work_hint = autonomous_coordinator.get_owner_work_prompt_hint(uid, user_text, group_id=group_id)
        if owner_work_hint:
            device_instruction += owner_work_hint
        rules_hint = autonomous_coordinator.get_learned_rules_prompt_block(group_id=group_id, uid=uid)
        if rules_hint:
            device_instruction += "\n" + rules_hint

    conversation.check_and_fast_release_state(user_text)
    active_char = (state.active_character or "none").strip().lower()
    is_complex = is_complex_or_long_query(user_text) and not voice_mode_only
    allow_amiya_sticker = (
        active_char in {"amiya", "assistant", "none", "default", ""}
        and bool(getattr(state, "amiya_sticker_enabled", True))
        and not voice_mode_only
        and not is_long_task
        and not is_complex
        and not is_draw
        and not image_b64_list
        and not file_paths
    )

    if (getattr(state, "fast_reply_mode", False) and active_char in {"amiya", "kaltsit", "dania"}
            and not is_long_task and not is_complex and not _is_comfort_target(uid)
            and local_rag_service.should_use_fast_engine(user_text, is_draw=is_draw, has_images=bool(image_b64_list), has_files=bool(file_paths))):
        fast_user_text = user_text + (_get_voice_prompt_suffix(active_char) if voice_mode_only else "") + device_instruction
        try:
            fast_reply = await local_rag_service.fast_chat_reply(
                active_char,
                fast_user_text,
                group_id=group_id,
                voice_mode_only=voice_mode_only,
                allow_stickers=allow_amiya_sticker,
            )
        except Exception as exc:
            logger.warning(f"快速路径异常，转回现有模型链路: {type(exc).__name__}: {exc}")
            fast_reply = None
        if fast_reply:
            return fast_reply

    if state.small_talk_mode and not is_draw and not is_long_task and not image_b64_list and not file_paths:
        st_style = getattr(state, "small_talk_style", "pure_persona")
        st_sys = build_small_talk_system_prompt(
            character=state.active_character,
            group_id=group_id,
            style=st_style,
            use_group_training=bool(state.training_mode and st_style == "group_mimic"),
            user_text=user_text,
            uid=uid,
            allow_stickers=allow_amiya_sticker,
            trigger_background_recall=not voice_mode_only,
        )
        if voice_mode_only:
            st_sys += _get_voice_prompt_suffix(state.active_character)
        st_sys += device_instruction
        raw_st = await llm_service.chat(
            st_sys,
            user_text,
            max_tokens=55 if voice_mode_only else (1400 if is_complex else 90),
            enable_web_search=True,
        )
        from .sticker_service import parse_model_reply

        raw_st = parse_model_reply(raw_st, allow_sticker=allow_amiya_sticker)
        if not is_complex:
            st_checked = await llm_service.resolve_and_enforce_situational_state(
                state.active_character or "amiya", user_text, raw_st, voice_mode_only=voice_mode_only
            )
        else:
            st_checked = raw_st
        if voice_mode_only:
            return st_checked
        active_st = conversation.get_active_state()
        return format_small_talk_output(
            st_checked,
            character=state.active_character,
            speech_blocked=bool(active_st and active_st.get("speech_blocked")),
            user_text=user_text,
            allow_long=is_complex,
        )

    if voice_mode_only:
        user_text += _get_voice_prompt_suffix(state.active_character)
        is_draw = False
        is_long_task = False

    max_tok = 55 if voice_mode_only else (1600 if (is_long_task or is_complex) else 320)
    if _is_comfort_target(uid) and not is_draw and not image_b64_list:
        system = _read_preset("comfort") or "你是温柔的情绪安抚助手，回复简短共情。"
        return await llm_service.chat(system, user_text, max_tokens=max_tok, enable_web_search=True)

    character = (state.active_character or "none").strip().lower()
    if character == "assistant":
        return await llm_service.chat_operator(
            "amiya",
            user_text,
            max_tokens=max_tok,
            voice_mode_only=voice_mode_only,
            is_long_task=is_long_task or is_complex,
            is_draw=is_draw,
            image_b64_list=image_b64_list,
            file_paths=file_paths,
            allow_stickers=allow_amiya_sticker,
        )
    if character in {"amiya", "kaltsit", "dania"}:
        return await llm_service.chat_operator(
            character,
            user_text,
            max_tokens=max_tok,
            voice_mode_only=voice_mode_only,
            is_long_task=is_long_task or is_complex,
            is_draw=is_draw,
            image_b64_list=image_b64_list,
            file_paths=file_paths,
            allow_stickers=allow_amiya_sticker,
        )

    if is_draw or image_b64_list or file_paths:
        return await llm_service.chat_operator(
            "amiya",
            user_text,
            max_tokens=max_tok,
            voice_mode_only=voice_mode_only,
            is_long_task=is_long_task or is_complex,
            is_draw=is_draw,
            image_b64_list=image_b64_list,
            file_paths=file_paths,
        )

    if group_id:
        # 群友镜像只借用群聊表达风格，不覆盖阿米娅角色人格
        st_sys = build_small_talk_system_prompt(
            character="amiya",
            group_id=group_id,
            style="group_mimic",
            use_group_training=True,
            user_text=user_text,
            uid=uid,
            allow_stickers=allow_amiya_sticker,
        )
        group_reply = await llm_service.chat(st_sys, user_text, max_tokens=max_tok, enable_web_search=True)
        from .sticker_service import parse_model_reply

        return parse_model_reply(group_reply, allow_sticker=allow_amiya_sticker)

    customs = state.custom_prompts or {}
    custom = customs.get(uid) or customs.get(str(uid))
    if custom:
        system = str(custom)
        return await llm_service.chat(system, user_text, max_tokens=max_tok, enable_web_search=True)

    return await llm_service.chat(
        "你是简洁的 QQ 助手，用一两句中文回答。",
        user_text,
        max_tokens=max_tok,
        enable_web_search=True,
    )


# ---------------------------------------------------------------------------
# Regression v8: 群友互动反馈 —— 戳一戳 (PokeNotifyEvent) 实时生成 (<=12字，3秒防刷冷却)
# ---------------------------------------------------------------------------
from nonebot import on_notice
from nonebot.adapters.onebot.v11 import PokeNotifyEvent

poke_matcher = on_notice(priority=50, block=False)
_poke_cooldown: dict[str, float] = {}


@poke_matcher.handle()
async def handle_poke_notify(bot: Bot, event: PokeNotifyEvent):
    if _voice_reply_lock.locked() or getattr(state, "voice_enabled", False):
        return
    if not getattr(state, "accept_poke", True):
        return
    if not getattr(state, "global_active", True):
        return
    if str(getattr(event, "target_id", "")) != str(getattr(event, "self_id", "")):
        return
    group_id = str(event.group_id) if getattr(event, "group_id", None) is not None else None
    if group_id is not None and not _group_allowed(group_id):
        return
    uid = str(getattr(event, "user_id", "") or "")
    now_mono = time.monotonic()
    if uid and (now_mono - _poke_cooldown.get(uid, 0.0)) < 3.0:
        return
    if uid:
        _poke_cooldown[uid] = now_mono

    char = getattr(state, "active_character", "amiya") or "amiya"
    if char in ("none", "default"):
        char = "amiya"
    from . import local_rag_service
    from .modes import small_talk

    raw_reaction = await local_rag_service.generate_poke_reaction(character=char)
    clean_reaction = small_talk.format_small_talk_output(
        raw_reaction or "唔…耳朵不能乱捏的啦",
        character=char,
        is_question=False,
    )
    if clean_reaction:
        await _safe_send(bot, event, clean_reaction)

