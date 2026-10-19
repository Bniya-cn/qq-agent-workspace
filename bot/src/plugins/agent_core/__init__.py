"""Task2–3 后端核心：配置持久化、本地控制 API、全局拦截与模式分发（支持看图问答、引用消息/合并转发解析、24h QQ号级上下文、垫图生图、文档导出与 30s 高清语音模式）。"""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import json
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
        "管理指令: /on /off /mode amiya|kaltsit|dania|none；/group add|del <id>；/status；/refs"
    ),
)

from nonebot.exception import MatcherException, ProcessException
from .service_registry import services, state, recorder
from . import controller_api
from .config_manager import BOT_ROOT, CONFIG_PATH, ALLOWED_CHARACTERS
from .errors import AgentCoreError, DocumentGenerationError, DrawPipelineError, TTSGenerationError, LLMProviderError, MessageDeliveryError
from .message_pipeline import ParsedMessage, Intent, parse_message, should_reply, classify_intent
from .history_recorder import task_scope, current_task_id

conversation = services.conversation
document_writer = services.writer
doc_service = services.documents
llm_service = services.llm
tts_service = services.tts
controller_api.setup(services)

PRESETS_DIR = BOT_ROOT / "data" / "presets"
GROUPS_DIR = BOT_ROOT / "data" / "groups"
REFS_DIR = BOT_ROOT / "data" / "refs"
COOLDOWN_SECONDS = 3.0
CHAT_TTS_TIMEOUT = 100.0
_active_chat_window: dict[str, float] = {}  # group:uid -> 上次互动时间戳（支持60s免@连续对话）

# 消息冷却（支持 uid 与 group:group_id 双重防刷屏）
_cooldown: dict[str, float] = {}

# 最近消息图片与全文缓存：支持跨消息引用看图、引用他人消息总结及合并转发解析
_msg_image_cache: collections.OrderedDict[str, list[dict[str, str]]] = collections.OrderedDict()
_msg_full_cache: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
_user_last_images: dict[str, tuple[float, list[dict[str, str]]]] = {}
_group_last_images: dict[str, collections.deque[tuple[float, str, list[dict[str, str]]]]] = {}

_ADMIN_CMDS = frozenset({"on", "off", "mode", "group", "status", "refs"})
_MD5_HEX_RE = re.compile(r"([0-9a-fA-F]{32})")

_VOICE_MODE_PROMPT_SUFFIX = (
    "\n\n【当前处于纯语音回复模式（最高优先级）】\n"
    "你的文字输出将直接送入 GPT-SoVITS 语音合成引擎生成语音条发给对方，不会展示文字或图片：\n"
    "1. 请保持你的角色性格与称呼，直接用自然流畅、简短精炼的中文口语回答对方（日常闲聊严格控制在 15 至 30 字以内，一到两句话即可，严禁长篇大论或铺垫废话）。\n"
    "2. 严禁输出任何括号动作或神态描写（如（微笑）、(叹气)、*抱住*、【动作】），所有情绪与语气体现在发出的口语音节中。\n"
    "3. 严禁输出 Markdown 符号、代码块、分点序号、网址链接或图片语法；标点仅使用自然的逗号、句号、问号、感叹号与省略号，确保气口自然。"
)


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
    interval = float(getattr(state, "reply_interval_seconds", 3.0) or 3.0)
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
    state.global_active = False
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
            return "收到博士的参考立绘啦！阿米娅正在对照原图细节为您精心绘制，请稍微等我一下哦~"
        return "收到博士的指示啦！阿米娅这就为您构思画面，正在全力绘制中，请稍微等我一下哦~"
    return "好的博士！关于这项委托阿米娅这就为您深入整理，请稍微等我一下哦~"


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


_conversation_cleanup_task: Optional[asyncio.Task] = None


@get_driver().on_startup
async def _start_conversation_cleanup():
    global _conversation_cleanup_task

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
    if _conversation_cleanup_task:
        _conversation_cleanup_task.cancel()
        try:
            await _conversation_cleanup_task
        except asyncio.CancelledError:
            pass


@agent_msg.handle()
async def handle_agent_message(bot: Bot, event: MessageEvent):
    from .modes import is_training_group, record_group_message_for_training

    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    uid = _uid(event)
    key = f"{group_id}:{uid}"
    parsed = parse_message(
        event, character=state.active_character, last_interaction=_active_chat_window.get(key),
        now=time.monotonic(), detect_draw=llm_service._auto_detect_draw_intent,
        detect_formats=doc_service.detect_requested_formats,
    )
    if group_id is None or _group_allowed(group_id):
        _cache_incoming_images(event, group_id, uid)
    if group_id and is_training_group(group_id) and parsed.clean_text and not parsed.clean_text.startswith("/"):
        reply_obj = getattr(event, "reply", None)
        r_txt = ""
        if reply_obj is not None and getattr(reply_obj, "message", None):
            r_txt, _ = _extract_text_and_forwards_from_segments(reply_obj.message)
        record_group_message_for_training(
            group_id=group_id,
            sender_uid=uid,
            sender_name=_sender_display_name(event),
            raw_text=parsed.clean_text,
            reply_to_text=r_txt,
        )
    if not should_reply(
        parsed,
        global_active=state.global_active,
        is_private=isinstance(event, PrivateMessageEvent),
        group_allowed=group_id is not None and _group_allowed(group_id),
        auto_reply_mode=bool(getattr(state, "auto_reply_mode", True)),
    ):
        return
    if group_id and getattr(state, "group_user_whitelist_enabled", False):
        whitelists = getattr(state, "group_user_whitelists", {}) or {}
        group_wl = set(str(u) for u in whitelists.get(str(group_id), []))
        super_qq = str(state.superuser_qq or "").strip()
        if group_wl and str(uid) not in group_wl and str(uid) != super_qq:
            logger.debug(f"[白名单拦截] 群 {group_id} 用户 {uid} 不在群用户白名单中，跳过响应")
            return
    if _in_cooldown(uid) or (group_id and _in_cooldown(f"group:{group_id}")):
        return
    if group_id:
        now = time.monotonic()
        for expired in [k for k, ts in _active_chat_window.items() if now - ts > 60]:
            _active_chat_window.pop(expired, None)
        _active_chat_window[key] = now

    task_id = uuid.uuid4().hex
    tracker = recorder.progress_tracker
    tracker.update(task_id, status="pending", stage="等待用户会话", source=f"{group_id or '私聊'} | {uid}",
                   input_text=parsed.clean_text)
    with task_scope(task_id):
        try:
            async with conversation.store.user_turn(uid):
                tracker.update(task_id, status="running", stage="解析消息与上下文", percent=5)
                session = conversation.store.begin(uid)
                with conversation.use_session(session, group_id=group_id):
                    if parsed.intent in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}:
                        budget = 180
                    elif parsed.intent == Intent.DOCUMENT or _is_long_task_or_draw_intent(parsed.clean_text):
                        budget = 610
                    else:
                        budget = max(90, int(CHAT_TTS_TIMEOUT) + 35) if state.voice_enabled else 90
                    async with asyncio.timeout(budget):
                        await _handle_agent_message(bot, event, parsed)
                tracker.update(task_id, status="done", stage="QQ 投递已确认", percent=100)
        except (MatcherException, ProcessException):
            # NoneBot 的结束、跳过等控制异常由框架接管，不能包装为模型故障。
            raise
        except asyncio.CancelledError:
            tracker.update(task_id, status="error", stage="任务已取消或服务正在重启", percent=100)
            raise
        except Exception as exc:
            tracker.update(task_id, status="error", stage=f"处理失败: {type(exc).__name__}", percent=100)
            logger.exception(f"AgentCoreError task={task_id} uid={uid} group={group_id}")
            if isinstance(exc, AgentCoreError):
                message = str(exc)
            elif isinstance(exc, TimeoutError):
                session = conversation.current()
                cur_doc = session.document if session else None
                has_valid_draft = (
                    cur_doc is not None
                    and cur_doc.get("prompt") == parsed.clean_text
                    and cur_doc.get("complete") is False
                    and len(str(cur_doc.get("content") or "").strip()) >= 50
                )
                if parsed.intent in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}:
                    message = "生图请求超时，本次没有成功交付图片，请稍后重试。"
                elif has_valid_draft:
                    message = "文档本次生成超时，已为您保存阶段性草稿，可回复“导出”继续续写。"
                else:
                    message = "处理请求超时，请稍后重试。"
            else:
                message = AgentCoreError.user_message
            if not await _safe_send(bot, event, message):
                recorder.log(f"DeliveryFailed task={task_id} 错误提示也未获 QQ 投递确认", "ERROR")
        finally:
            if tracker.tasks.get(task_id, {}).get("status") in {"pending", "running"}:
                tracker.update(task_id, status="error", stage="流程未完成投递", percent=100)


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


async def _export_previous_reply(bot: Bot, event: MessageEvent, plain: str, group_id: Optional[str], uid: str):
    session = conversation.current()
    document = session.document if session else None
    last = session.history[-1] if session and session.history else None
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


async def _handle_agent_message(bot: Bot, event: MessageEvent, parsed: ParsedMessage):
    uid = _uid(event)
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    plain = parsed.clean_text
    source_desc = f"群 {group_id}" if group_id else f"私聊 {uid}"

    if doc_service.is_export_followup(plain):
        await _export_previous_reply(bot, event, plain, group_id, uid)
        return

    # 统一在进入分支前解析【被引用回复的消息/图片】与【合并转发聊天记录】
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

    effective_query = plain
    if reply_context_str:
        if effective_query:
            effective_query = f"{reply_context_str}\n\n【用户对上述引用内容的指令/追问】：\n{effective_query}"
        else:
            effective_query = f"{reply_context_str}\n\n请总结或回应上述引用内容。"

    parsed.intent = classify_intent(plain, has_image=bool(image_b64_list),
                                    detect_draw=llm_service._auto_detect_draw_intent,
                                    detect_formats=doc_service.detect_requested_formats)
    explicit_formats = doc_service.detect_requested_formats(plain)

    # 若开启了纯语音模式（且当前并非明确要求导出 docx/pdf 文件）
    if state.voice_enabled and not explicit_formats and not _is_draw_intent(plain, has_image=bool(image_b64_list)):
        user_query = effective_query or (
            "对方给你发了一张图片，请直接用口语说说图片里的内容。" if image_b64_list else "对方刚刚向你打了个招呼，请用你的角色口吻自然地回应一句。"
        )
        recorder.log(
            f"[语音模式] 接收消息 [{source_desc}] '{user_query[:35]}' (附图={len(image_b64_list)}) -> 正在由角色({state.active_character})生成语音台词..."
        )
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
            raise LLMProviderError()

        _touch_cooldown(uid)
        if group_id:
            _touch_cooldown(f"group:{group_id}")
        try:
            record_payload = await tts_service.synthesize(
                spoken_text, timeout=CHAT_TTS_TIMEOUT, truncate_long=True,
            )
        except TTSGenerationError as exc:
            recorder.log(f"Degradation task={current_task_id.get()} stage=tts reason={exc}", "WARN")
            record_payload = None
        if record_payload:
            sent_ok = await _safe_send_voice(bot, event, record_payload)
            if sent_ok:
                wav_saved = tts_service.LAST_AUDIO_META.get("wav_path", "")
                dur_sec = tts_service.LAST_AUDIO_META.get("duration_sec", 0)
                recorder.log(
                    f"[语音模式] 已发送纯语音回复 [{source_desc}] ({dur_sec}s, 归档: {Path(wav_saved).name if wav_saved else 'mem'}): '{spoken_text[:35]}'"
                )
                recorder.record_dialogue(
                    source=source_desc,
                    sender_id=uid,
                    user_text=user_query,
                    character=state.active_character,
                    reply=spoken_text,
                    voice=True,
                )
                conversation.remember(user_query, spoken_text)
                _touch_cooldown(uid)
                if group_id:
                    _touch_cooldown(f"group:{group_id}")
                return

        fallback_clean = tts_service._clean(spoken_text) or spoken_text
        fallback_sent = await _safe_send(bot, event, fallback_clean)
        if not fallback_sent:
            raise MessageDeliveryError()
        if fallback_sent:
            conversation.remember(user_query, fallback_clean)
        recorder.log(f"Degradation task={current_task_id.get()} stage=voice_to_text source={source_desc}", "WARN")
        recorder.record_dialogue(
            source=source_desc,
            sender_id=uid,
            user_text=user_query,
            character=state.active_character,
            reply=fallback_clean,
            voice=False,
        )
        _touch_cooldown(uid)
        if group_id:
            _touch_cooldown(f"group:{group_id}")
        return

    if not effective_query and not image_b64_list and not extra_file_paths:
        raise LLMProviderError("请发送具体问题、图片或文件。")
    if not effective_query and image_b64_list:
        effective_query = "博士给你发了一张图片，请看看图片内容并回应博士。"
    elif not effective_query and extra_file_paths:
        if any(Path(p).suffix.lower() in {".wav", ".mp3", ".amr", ".silk", ".m4a", ".ogg"} or "rec" in p.lower() for p in extra_file_paths):
            effective_query = "博士给你发了一段语音，请解析语音内容并回应博士。"
        else:
            effective_query = "博士给你发了一份文件，请解析文件内容并回应博士。"

    save_ref_match = _SAVE_REF_ONLY_RE.search(plain) if image_b64_list else None
    is_draw = parsed.intent in {Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE}

    is_doc_task = bool(explicit_formats) and not is_draw
    is_long_task = _is_long_task_or_draw_intent(plain, has_image=bool(image_b64_list)) or bool(extra_file_paths) or is_doc_task
    if is_doc_task:
        ack_msg = (MessageSegment.at(uid) + " 博士，阿米娅已经收到您的要求啦，正在为您撰写，请稍候片刻~") if group_id else "博士，阿米娅已经收到您的要求啦，正在为您撰写，请稍候片刻~"
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
                f"博士，阿米娅已经把「{cname}」的参考立绘原型牢牢记住并归档啦！之后只要点名画{cname}，就会自动参考这张设定哦~",
            )
            if not confirmed:
                raise MessageDeliveryError()
            return

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
                hb_msg = (MessageSegment.at(uid) + " 博士，内容篇幅较长，正在深入撰写中，请稍候片刻哦~") if group_id else "博士，内容篇幅较长，正在深入撰写中，请稍候片刻哦~"
                await _safe_send(bot, event, hb_msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    hb_task = asyncio.create_task(_doc_30s_heartbeat()) if is_doc_task else None

    try:
        reply = await _dispatch_reply(
            uid=uid, group_id=group_id,
            user_text=(effective_query + doc_service.DOCUMENT_PROMPT if explicit_formats else plain if is_draw else effective_query),
            is_long_task=is_long_task, is_draw=is_draw, image_b64_list=image_b64_list, file_paths=extra_file_paths,
        )
        if not reply:
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

    # === 文档导出与超长文本自动转文件投递 (PDF / Word .docx / Markdown .md) ===
    req_formats = doc_service.detect_requested_formats(plain, reply_len=len(reply), is_long_task=is_long_task)
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
    sent_ok = await _safe_send(bot, event, msg_to_send)
    if sent_ok:
        recorder.log(f"已回复 [{source_desc}]: '{reply[:80]}'")
        recorder.record_dialogue(
            source=source_desc,
            sender_id=uid,
            user_text=effective_query,
            character=state.active_character,
            reply=reply,
            voice=False,
        )
        _touch_cooldown(uid)
        if group_id:
            _touch_cooldown(f"group:{group_id}")

    file_sent = False
    if req_formats and not is_draw:
        file_sent = await _send_exported_documents(bot, event, exported_files, group_id, uid)
    if sent_ok or file_sent:
        document = {"content": reply, "prompt": plain, "formats": req_formats, "complete": True} if req_formats and not is_draw else None
        conversation.remember(effective_query, reply, document=document)
    if not sent_ok:
        raise MessageDeliveryError()
    if req_formats and not is_draw and not file_sent and explicit_formats:
        raise DocumentGenerationError()
    return


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
    """模式优先级：闲聊模式(路易斯风) > 安慰（非生图） > 干员 > 群画像 > 自定义/默认。"""
    from .modes import build_small_talk_system_prompt, format_small_talk_output, normalize_qq_linebreaks

    conversation.check_and_fast_release_state(user_text)
    if state.small_talk_mode and not is_draw and not is_long_task and not image_b64_list and not file_paths:
        st_style = getattr(state, "small_talk_style", "pure_persona")
        st_sys = build_small_talk_system_prompt(
            character=state.active_character,
            group_id=group_id,
            style=st_style,
            use_group_training=bool(state.training_mode and st_style == "group_mimic"),
        )
        raw_st = await llm_service.chat(st_sys, user_text, max_tokens=65)
        st_checked = await llm_service.resolve_and_enforce_situational_state(
            state.active_character or "amiya", user_text, raw_st, voice_mode_only=voice_mode_only
        )
        active_st = conversation.get_active_state()
        return format_small_talk_output(
            st_checked,
            character=state.active_character,
            speech_blocked=bool(active_st and active_st.get("speech_blocked")),
        )

    if voice_mode_only:
        user_text += _VOICE_MODE_PROMPT_SUFFIX
        is_draw = False
        is_long_task = False

    max_tok = 260 if voice_mode_only else 220
    if _is_comfort_target(uid) and not is_draw and not image_b64_list:
        system = _read_preset("comfort") or "你是温柔的情绪安抚助手，回复简短共情。"
        return await llm_service.chat(system, user_text, max_tokens=max_tok)

    character = (state.active_character or "none").strip().lower()
    if character == "assistant":
        return await llm_service.chat_operator(
            "amiya",
            user_text,
            max_tokens=max_tok,
            voice_mode_only=voice_mode_only,
            is_long_task=is_long_task,
            is_draw=is_draw,
            image_b64_list=image_b64_list,
            file_paths=file_paths,
        )
    if character in {"amiya", "kaltsit", "dania"}:
        return await llm_service.chat_operator(
            character,
            user_text,
            max_tokens=max_tok,
            voice_mode_only=voice_mode_only,
            is_long_task=is_long_task,
            is_draw=is_draw,
            image_b64_list=image_b64_list,
            file_paths=file_paths,
        )

    if is_draw or image_b64_list or file_paths:
        return await llm_service.chat_operator(
            "amiya",
            user_text,
            max_tokens=max_tok,
            voice_mode_only=voice_mode_only,
            is_long_task=is_long_task,
            is_draw=is_draw,
            image_b64_list=image_b64_list,
            file_paths=file_paths,
        )

    if group_id:
        profile = _load_group_profile(group_id)
        if profile is not None:
            system = str(profile.get("system_prompt") or "").strip()
            examples = profile.get("few_shot_examples") or []
            if system:
                if isinstance(examples, list) and examples:
                    ex_lines = ["\n\n【本群典型对话示例（请严格模仿该语气与黑话风格）】："]
                    for item in examples[:6]:
                        if isinstance(item, dict):
                            r = "群友" if item.get("role") == "user" else "你"
                            ex_lines.append(f"{r}：{item.get('content', '')}")
                    system = system + "\n".join(ex_lines)
                return await llm_service.chat(system, user_text, max_tokens=max_tok)
            return "群画像待接（已有 JSON 但缺 system_prompt）"

    customs = state.custom_prompts or {}
    custom = customs.get(uid) or customs.get(str(uid))
    if custom:
        system = str(custom)
        return await llm_service.chat(system, user_text, max_tokens=max_tok)

    if group_id:
        return await llm_service.chat_operator(
            "amiya",
            user_text,
            max_tokens=max_tok,
            voice_mode_only=voice_mode_only,
            is_long_task=is_long_task,
            is_draw=is_draw,
        )
    return await llm_service.chat(
        "你是简洁的 QQ 助手，用一两句中文回答。",
        user_text,
        max_tokens=max_tok,
    )
