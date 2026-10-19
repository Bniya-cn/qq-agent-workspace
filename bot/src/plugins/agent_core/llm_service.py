"""统一 LLM 适配器：DeepSeek/OpenAI 通用对话 + 百炼干员 RAG + 视觉理解(qwen-vl) + 千问图像(qwen-image-2.1-pro / LoRA)文生图与垫图生图。"""
from __future__ import annotations

import asyncio

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Optional

from nonebot import logger

from .config_manager import ensure_bot_env
from . import conversation_service as conversation
from . import doc_service
from .errors import DrawPipelineError, LLMProviderError
from .history_recorder import recorder, current_task_id

_NO_KEY_MSG = "未配置 DEEPSEEK_API_KEY / OPENAI_API_KEY / DASHSCOPE_API_KEY，暂时无法生成回复。"
_NO_DASHSCOPE_MSG = "未配置 DASHSCOPE_API_KEY，干员模式暂不可用。可先贴群里或写进 bot/.env。"
_DEFAULT_BASE = "https://api.deepseek.com"
_DEFAULT_MODEL_DEEPSEEK = "deepseek-chat"
_DEFAULT_MODEL_OPENAI = "gpt-4o-mini"
_DASHSCOPE_COMPAT = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_DASHSCOPE_APP_BASE = "https://dashscope.aliyuncs.com/api/v1/apps"
_DASHSCOPE_MULTIMODAL_GEN = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
_DASHSCOPE_ASYNC_IMG_GEN = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/image-generation/generation"
)
_DASHSCOPE_TASKS_BASE = "https://dashscope.aliyuncs.com/api/v1/tasks"
_DEFAULT_QWEN = "qwen-plus"
_DEFAULT_QWEN_VL = "qwen3-vl-plus"
_DEFAULT_IMAGE_MODEL = "qwen-image-2.1-pro"

_BOT_ROOT = Path(__file__).resolve().parents[3]
_PRESETS = _BOT_ROOT / "data" / "presets"
_KNOWLEDGE = _BOT_ROOT / "data" / "knowledge"
_REFS_DIR = _BOT_ROOT / "data" / "refs"

_ALIAS_FILE = _KNOWLEDGE / "char_aliases.json"
_REFS_CACHE_FILE = _REFS_DIR / "refs_visual_cache.json"
_REFS_NORM_DIR = _BOT_ROOT / "data" / "cache" / "refs_norm"
_SINGLE_CHAR_NAMES = {"年", "夕", "令", "W", "w"}
_GENERIC_REFERENCE_NAMES = {
    "这个角色", "那个角色", "这位角色", "这个人物", "那个人物", "这位人物",
    "不同姿势", "的不同姿势", "这个", "那个", "这位",
}


def _strip_reply_context_wrapper(text: str) -> str:
    """剥离系统自动拼接的【用户当前引用/回复...】包装头，仅保留用户自己输入的原始指令文字。"""
    s = (text or "").strip()
    marker = "【用户对上述引用内容的指令/追问】："
    if marker in s:
        s = s.split(marker, 1)[1].strip()
    elif s.startswith("【用户当前引用/回复") or s.startswith("【合并转发的聊天记录"):
        return ""
    return s


def _clean_ref_filename_stem(stem: str) -> str:
    """清洗 refs/ 下的立绘文件名噪点（如 '立绘_守岸人_01' -> '守岸人'）。"""
    s = re.sub(r"[（(【\[\]][^）)】\]]*[）)】\]]", "", stem or "")
    s = re.sub(
        r"(?:角色图|设定图|人设图|立绘|原画|全身|半身|白底|参考图|ref|char|img|[_\-\s]+\d+$|^\d+[_\-\s]+)",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip(" _-.")


def _normalize_ref_file_to_data_uri(p: Path) -> Optional[str]:
    """将 refs/ 下的立绘通过 macOS sips 归一化为 <=1536px 的标准 RGB JPEG data URI。"""
    import subprocess
    try:
        _REFS_NORM_DIR.mkdir(parents=True, exist_ok=True)
        norm_jpg = _REFS_NORM_DIR / f"{p.stem}.norm.jpg"
        if not norm_jpg.is_file() or norm_jpg.stat().st_mtime < p.stat().st_mtime or norm_jpg.stat().st_size <= 500:
            subprocess.run(
                [
                    "/usr/bin/sips",
                    "-Z",
                    "1536",
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    "90",
                    str(p),
                    "--out",
                    str(norm_jpg),
                ],
                check=True,
                capture_output=True,
                timeout=8.0,
            )
        target_p = norm_jpg if (norm_jpg.is_file() and norm_jpg.stat().st_size > 500) else p
        raw = target_p.read_bytes()
        if len(raw) <= 100:
            return None
        if raw[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as exc:
        logger.warning(f"归一化参考立绘 {p.name} 失败: {exc}")
        return None


def _load_alias_map() -> dict[str, list[str]]:
    """读取 bot/data/knowledge/char_aliases.json 中的角色别称字典，并自动合并 refs/ 目录下的文件名。"""
    base_map: dict[str, list[str]] = {}
    try:
        if _ALIAS_FILE.is_file():
            data = json.loads(_ALIAS_FILE.read_text(encoding="utf-8"))
            raw_aliases = data.get("aliases") or {}
            if isinstance(raw_aliases, dict):
                for k, v in raw_aliases.items():
                    if isinstance(v, list):
                        base_map[str(k)] = [str(x) for x in v if x]
    except Exception as exc:
        logger.warning(f"读取角色别称表失败: {exc}")

    if _REFS_DIR.is_dir():
        for p in sorted(_REFS_DIR.iterdir()):
            if not p.is_file() or p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            cleaned = _clean_ref_filename_stem(p.stem)
            if not cleaned or cleaned in _GENERIC_REFERENCE_NAMES:
                continue
            matched_canon = None
            for canon, alist in base_map.items():
                if cleaned.lower() == canon.lower() or any(cleaned.lower() == a.lower() for a in alist):
                    matched_canon = canon
                    break
            target = matched_canon or cleaned
            cur_list = base_map.get(target, [target])
            for item in (target, p.stem, cleaned):
                if item and item not in _SINGLE_CHAR_NAMES and item not in cur_list:
                    cur_list.append(item)
            base_map[target] = cur_list
    return base_map


def _resolve_canonical_characters(user_prompt: str) -> list[str]:
    """从用户输入中精准识别所有登场角色（支持标准名、外号、别称、英文名及 refs/ 文件名），返回去重后的标准角色名列表。"""
    s = _strip_reply_context_wrapper(user_prompt).lower()
    if not s:
        return []
    alias_map = _load_alias_map()
    matched: list[str] = []
    pairs: list[tuple[str, str]] = []
    for canon, alist in alias_map.items():
        candidates = set(alist)
        if canon not in _SINGLE_CHAR_NAMES:
            candidates.add(canon)
        for a in candidates:
            a_clean = a.strip()
            if not a_clean or a_clean in _SINGLE_CHAR_NAMES:
                continue
            if len(a_clean) >= 2 or a_clean in {"椿", "黍"}:
                pairs.append((a_clean, canon))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)

    for alias_str, canon in pairs:
        if alias_str in _GENERIC_REFERENCE_NAMES or canon in _GENERIC_REFERENCE_NAMES:
            continue
        alower = alias_str.lower()
        if re.fullmatch(r"[a-z0-9_'-]+", alower):
            if not re.search(rf"(?<![a-z0-9]){re.escape(alower)}(?![a-z0-9])", s):
                continue
        elif alower not in s:
            continue
        if canon == "菲比" and "菲比啾比" in matched:
            continue
        if canon not in matched:
            matched.append(canon)
    return matched


def _normalize_prompt_with_aliases(user_prompt: str) -> str:
    """若用户使用了角色外号/别称（如‘二小姐’、‘岸宝’、‘42姐’），自动附注标准角色名。"""
    raw_clean = _strip_reply_context_wrapper(user_prompt)
    canons = _resolve_canonical_characters(raw_clean)
    if not canons:
        return raw_clean
    missing_canons = [c for c in canons if c.lower() not in raw_clean.lower()]
    if not missing_canons:
        return raw_clean
    return f"{raw_clean}（注：登场角色标准名为 {', '.join(missing_canons)}）"


def _resolve_credentials() -> tuple[Optional[str], str, str]:
    """返回 (api_key, base_url, model)。优先 DeepSeek，其次 OpenAI，最后回退百炼 DashScope。"""
    ensure_bot_env()
    ds_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if ds_key:
        base = (os.getenv("DEEPSEEK_BASE_URL") or _DEFAULT_BASE).rstrip("/")
        model = (os.getenv("DEEPSEEK_MODEL") or _DEFAULT_MODEL_DEEPSEEK).strip()
        if "api.deepseek.com" in base.lower() and model.lower() == "deepseek-flash":
            model = "deepseek-chat"
        return ds_key, base, model

    oa_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if oa_key:
        base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        model = (os.getenv("OPENAI_MODEL") or _DEFAULT_MODEL_OPENAI).strip()
        return oa_key, base, model

    qwen_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
    if qwen_key:
        base = _DASHSCOPE_COMPAT.rstrip("/")
        model = (os.getenv("DASHSCOPE_MODEL") or _DEFAULT_QWEN).strip()
        return qwen_key, base, model

    return None, "", ""


def _report_llm_progress(
    kind: str,
    stage: str,
    percent: int,
    *,
    input_text: str = "",
    prompt_text: str = "",
    output_text: str = "",
    extra_info: str = "",
    status: str = "running",
) -> None:
    """向面板实时上报 LLM / 提示词 Agent / 生图模型进度与完整未截断 Prompt。"""
    task_id = current_task_id.get()
    if task_id:
        recorder.progress_tracker.update(
            task_id, kind=kind, stage=stage, percent=min(percent, 95),
            status="running" if status == "done" else status,
            input_text=input_text, prompt_text=prompt_text,
            output_text=output_text, extra_info=extra_info,
        )
    recorder.log(f"[{stage}] {extra_info}")


def _dashscope_key() -> str:
    ensure_bot_env()
    return (os.getenv("DASHSCOPE_API_KEY") or "").strip()


def _operator_app_id(character: str) -> str:
    """百炼应用 ID：DASHSCOPE_APP_ID_AMIYA / KALTSIT，或统一 DASHSCOPE_APP_ID。"""
    specific = (os.getenv(f"DASHSCOPE_APP_ID_{character.upper()}") or "").strip()
    if specific:
        return specific
    return (os.getenv("DASHSCOPE_APP_ID") or "").strip()


def _read_text(path: Path, limit: int = 12000) -> str:
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")[:limit]
    except OSError as exc:
        logger.warning(f"读取失败 {path}: {exc}")
        return ""


def _maybe_save_character_ref(user_prompt: str, image_b64_list: list[str]) -> None:
    """仅在用户明确要求记住角色图时归档，避免把普通图生图指令误识别成角色名。"""
    raw_clean = _strip_reply_context_wrapper(user_prompt)
    if not raw_clean or not image_b64_list:
        return
    m = re.search(
        r"(?:(?:记住\s*)?(?:这是|这个是|这张是)|记住)\s*[「『\"“]?"
        r"([\u4e00-\u9fa5A-Za-z0-9·_-]{2,16}?)"
        r"[」』\"”]?\s*(?:的)?(?:角色图|立绘|设定图|原画|形象|原型)",
        raw_clean,
        re.IGNORECASE,
    )
    if not m:
        return
    char_name = m.group(1).strip()
    stop_words = _GENERIC_REFERENCE_NAMES | {
        "一张", "一幅", "一个", "鸣潮", "原神", "方舟", "明日", "绝区", "崩坏", "星穹",
        "这个角色", "那个角色", "这位角色", "角色", "人物", "少女", "女生", "这幅", "这张",
    }
    if char_name in stop_words or len(char_name) < 2:
        return
    try:
        _REFS_DIR.mkdir(parents=True, exist_ok=True)
        target_path = _REFS_DIR / f"{char_name}.png"
        if target_path.is_file() and target_path.stat().st_size > 1000:
            return
        b64_uri = image_b64_list[0]
        if ";base64," in b64_uri:
            raw = base64.b64decode(b64_uri.split(";base64,", 1)[1])
            if len(raw) > 1000:
                target_path.write_bytes(raw)
                logger.info(f"已自动将用户提供的「{char_name}」参考立绘归档至: {target_path}")
    except Exception as exc:
        logger.warning(f"自动归档角色参考图失败: {exc}")


def _load_local_character_refs(user_prompt: str, max_refs: int = 4) -> list[str]:
    """从 bot/data/refs/ 中自动匹配用户生图指令提到的角色立绘参考图。"""
    raw_clean = _strip_reply_context_wrapper(user_prompt)
    if not raw_clean or not _REFS_DIR.is_dir():
        return []
    matched_files: list[Path] = []
    valid_exts = {".png", ".jpg", ".jpeg", ".webp"}
    try:
        for entry in sorted(_REFS_DIR.iterdir()):
            name_stem = entry.stem.strip()
            cleaned_name = _clean_ref_filename_stem(name_stem)
            if (
                not name_stem
                or name_stem.startswith(".")
                or cleaned_name in _GENERIC_REFERENCE_NAMES
            ):
                continue
            if name_stem.lower() in raw_clean.lower() or (cleaned_name and cleaned_name.lower() in raw_clean.lower()):
                if entry.is_file() and entry.suffix.lower() in valid_exts:
                    matched_files.append(entry)
                elif entry.is_dir():
                    for sub in sorted(entry.iterdir()):
                        if sub.is_file() and sub.suffix.lower() in valid_exts:
                            matched_files.append(sub)
    except OSError as exc:
        logger.warning(f"扫描本地角色参考图目录失败: {exc}")
        return []

    out_b64: list[str] = []
    for p in matched_files[:max_refs]:
        uri = _normalize_ref_file_to_data_uri(p)
        if uri:
            out_b64.append(uri)
            logger.info(f"命中本地归一化角色参考图: {p.name}")
    return out_b64


def _voice_snippets(character: str, limit: int = 12) -> str:
    """从本地语音表抽若干中文台词，注入 system 作 few-shot 口吻。"""
    txt_candidates = [
        _KNOWLEDGE / f"{character}_voice_table.txt",
        _KNOWLEDGE / character / "voice_table.txt",
    ]
    for p in txt_candidates:
        if not p.is_file():
            continue
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError:
            continue
        lines: list[str] = []
        title = ""
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("## "):
                title = s[3:].strip()
                if ". " in title[:6]:
                    title = title.split(". ", 1)[-1]
            elif s.startswith("文本:"):
                text = s[3:].strip()
                if text:
                    lines.append(f"- [{title}] {text}" if title else f"- {text}")
                if len(lines) >= limit:
                    return "\n".join(lines)
        if lines:
            return "\n".join(lines)

    json_candidates = [
        _KNOWLEDGE / f"{character}_voice_table.json",
        _KNOWLEDGE / character / "voice_table.json",
    ]
    data: dict[str, Any] = {}
    for p in json_candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
    voices = data.get("voices") or []
    lines = []
    for v in voices:
        title = (v.get("title") or "").strip()
        text = (v.get("text_zh") or "").strip()
        if not text:
            continue
        lines.append(f"- [{title}] {text}" if title else f"- {text}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def build_operator_system(character: str, *, is_long_task: bool = False) -> str:
    """本地 preset + 档案 + 语音表 → 干员 system。"""
    char = character.strip().lower()
    preset = _read_text(_PRESETS / f"{char}.txt", 4000)
    handbook = _read_text(_KNOWLEDGE / f"{char}_handbook.txt", 1800) or _read_text(
        _KNOWLEDGE / char / "handbook.txt", 1800
    )
    voices = _voice_snippets(char)
    default_intro = (
        "你是《鸣潮》拉海洛的共鸣者「达妮娅」，性格傲娇嘴硬但关心同伴，称呼对方为“你”或“漂泊者”。"
        if char == "dania"
        else f"你是明日方舟干员「{char}」，请保持角色口吻，称呼对方为博士。"
    )
    parts = [preset.strip()] if preset.strip() else [default_intro]
    if handbook.strip():
        parts.append("## 参考档案（本地 PRTS / GameData）\n" + handbook.strip())
    if voices.strip():
        parts.append("## 参考语音口吻\n" + voices.strip())
    if is_long_task:
        parts.append(
            "【重要指令】博士当前向你交代了一项深度撰写/物理或学科教案/分析委托。"
            "请先用一句符合你干员身份的问候开场，随后直接输出结构完整、公式准确（使用标准 LaTeX $...$ 或 $$...$$）、内容详实、层次分明的完整 Markdown 正文"
            "（包含多级标题、教学目标、重难点、具体实验/推导步骤、板书设计及课后分层作业），严禁只回复客套话推脱，也不要声称在云端沙箱生成文件！"
        )
    else:
        parts.append(
            "回复必须 1～3 句口语短句，有温度；禁止说明书口吻和大段设定。"
            "若该用户在历史或本轮对话中对你设定了尚未解除的身体限制或持续处境（如嘴巴被封住/贴胶带只能发出「唔……唔唔！嗯……呜呜……」闷哼声而不能说出任何词汇，"
            "或背着一箱沙子/负重需要全程气喘吁吁「呼……哈……」并持续提及负重处境），则在用户明确解除前必须严格维持该状态！"
        )
    return "\n\n".join(parts)


async def _post_chat_completions(
    *, api_key: str, base_url: str, model: str, system: str, user: str, max_tokens: int
) -> str:
    base_clean = base_url.rstrip("/")
    eff_model = model
    if "api.deepseek.com" in base_clean.lower() and eff_model.lower() == "deepseek-flash":
        eff_model = "deepseek-chat"
    url = f"{base_clean}/chat/completions"
    payload: dict[str, Any] = {
        "model": eff_model,
        "messages": conversation.messages(system or "", user or ""),
        "max_tokens": int(max_tokens),
        "temperature": 0.7,
    }
    model_l = (eff_model or "").lower()
    if "deepseek" in model_l and "flash" in model_l and "api.deepseek.com" not in base_clean.lower():
        payload["thinking"] = {"type": "disabled"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        import httpx
    except ImportError:
        logger.error("httpx 未安装，无法调用 LLM")
        raise LLMProviderError("缺少 httpx 依赖。")

    try:
        timeout = httpx.Timeout(connect=15.0, read=150.0, write=30.0, pool=15.0)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
                raise LLMProviderError()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise LLMProviderError()
            msg = choices[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            if not content:
                raise LLMProviderError()
            return content
    except LLMProviderError:
        raise
    except Exception as exc:
        raise LLMProviderError() from exc


async def chat_with_vision(
    *,
    api_key: str,
    character: str,
    user_text: str,
    image_b64_list: list[str],
    max_tokens: int = 400,
    voice_mode_only: bool = False,
) -> Optional[str]:
    """使用千问 VL (qwen-vl-max) 识别用户发送或引用的图片并以干员口吻回复。"""
    if not api_key or not image_b64_list:
        return None
    try:
        import httpx
    except ImportError:
        return None

    char = (character or "amiya").strip().lower()
    if char == "kaltsit":
        role_desc = "你是罗德岛医疗部凯尔希，请用冷静简练的口吻称呼对方为“博士”，直接根据图片内容完成博士的要求（如判断这是什么图、翻译图中文字、总结信息或点评画面）。"
    elif char == "dania":
        role_desc = "你是《鸣潮》拉海洛共鸣者达妮娅，请用带一点傲娇吐槽又自然的战友口吻称呼对方为“你”或“漂泊者”，直接根据图片内容完成对方的要求（如判断这是什么图、翻译图中文字或回应画面，严禁括号动作描写）。"
    else:
        role_desc = "你是罗德岛领袖阿米娅，请用温柔体贴的口吻称呼对方为“博士”，直接根据图片内容完成博士的要求（如判断这是什么图/什么角色、准确翻译图中文字或总结图中信息）。"

    if voice_mode_only:
        role_desc += (
            "\n【纯语音输出约束】你的回答将直接转成语音条播放：请用自然流畅的中文口语直接说出图片内容或翻译结果，"
            "总字数控制在 195 字以内（约 30 秒语音），严禁包含任何括号动作、Markdown 符号或网址。"
        )

    content_items: list[dict[str, Any]] = []
    for img_b64 in image_b64_list[:4]:
        content_items.append({"type": "image_url", "image_url": {"url": img_b64}})
    raw_q = _strip_reply_context_wrapper(user_text)
    prompt_text = raw_q or "博士给你发了一张图片，请直接看看图片里是什么内容并回应博士。"
    content_items.append({"type": "text", "text": prompt_text})

    url = f"{_DASHSCOPE_COMPAT.rstrip('/')}/chat/completions"
    payload = {
        "model": os.getenv("DASHSCOPE_VL_MODEL") or _DEFAULT_QWEN_VL,
        "messages": conversation.messages(role_desc, content_items),
        "max_tokens": 260 if voice_mode_only else int(max_tokens),
        "temperature": 0.5,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        timeout = httpx.Timeout(connect=15.0, read=65.0, write=30.0, pool=15.0)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 404 and payload.get("model") != "qwen-vl-plus":
                logger.warning(f"Qwen-VL 模型 {payload.get('model')} 不存在(404)，尝试降级回退 qwen-vl-plus...")
                payload["model"] = "qwen-vl-plus"
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning(f"Qwen-VL HTTP {resp.status_code}: {resp.text[:300]}")
                return None
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            msg = choices[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            return content or None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Qwen-VL 视觉调用异常: {exc}")
        return None


_CHARACTER_VISUAL_PRESETS: dict[str, str] = {
    "菲比啾比": (
        "《鸣潮》二创萌系形象「菲比啾比」(Chibi Phoebe)：菲比的超可爱 Q 版大头团子/二头身包子脸萌系少女形态，保留璀璨浅金发、齐刘海与两侧垂落系蓝丝带的长发束、"
        "水汪汪的湛蓝大眼睛、头顶标志性洁白宽檐隐海修会圆礼帽（缀金色星芒与蓝宝石桂冠），脸蛋圆滚滚肉嘟嘟"
    ),
    "菲比": (
        "《鸣潮》(Wuthering Waves) 角色「菲比」(Phoebe)：璀璨浅金发、整齐刘海搭配脸颊两侧柔顺垂落的长发束与双侧长卷发（发尾系蓝色丝带蝴蝶结），"
        "清澈透亮的水蓝色/湛蓝色大眼睛（绝非紫瞳），头戴标志性洁白宽檐隐海修会圣职者圆礼帽（帽檐与帽顶装饰金色星芒与蓝宝石桂冠徽章），气质圣洁温柔乖巧"
    ),
    "大肥鱼": (
        "DeepSeek 拟人化看板娘「蓝色大肥鱼 / DeepSeek娘」（注意：是二次元 Q 版/萝莉鲸鱼女仆美少女，绝对禁止画成真鱼或动物鲸鱼！）："
        "深海蓝渐变至浅青蓝色的蓬松微卷长发，头顶立着一根高高弯曲的呆毛，头戴白色荷叶边女仆发箍，左侧发际系浅蓝色小蝴蝶结；"
        "脑袋两侧长着一对水平微垂的蓝白双色鲸鱼胸鳍状兽耳（耳底带白色波浪纹理），身后裙摆下伸出一条大大的深蓝色鲸鱼尾鳍；"
        "湛蓝色圆润大眼睛，肉嘟嘟鼓起的粉嫩包子脸（常带“ω”猫猫嘴或害羞可爱的表情）；"
        "身穿深藏青色（海军蓝）配纯白荷叶边的长袖古典女仆装，领口系深蓝蝴蝶结，身前纯白荷叶边围裙的右下角印有一只蓝色小鲸鱼图标（DeepSeek Logo），穿白色蕾丝花边短袜与深蓝色圆头玛丽珍皮鞋"
    ),
    "deepseek": (
        "DeepSeek 拟人化看板娘「蓝色大肥鱼 / DeepSeek娘」（注意：是二次元 Q 版/萝莉鲸鱼女仆美少女，绝对禁止画成真鱼或动物鲸鱼！）："
        "深蓝渐变浅青蓝微卷长发、头顶一根弯曲呆毛、白色荷叶边女仆发箍配左侧浅蓝小蝴蝶结；"
        "脑袋两侧长着一对水平微垂的蓝白双色鲸鱼胸鳍兽耳，身后有蓝色大鲸鱼尾鳍；湛蓝大眼、肉嘟嘟鼓起的包子脸“ω”嘴；"
        "身穿深藏青色配纯白荷叶边长袖女仆装，白色围裙右下角印有一只蓝色小鲸鱼 Logo，配白蕾丝短袜与深蓝玛丽珍鞋"
    ),
    "守岸人": (
        "《鸣潮》(Wuthering Waves) 角色「守岸人」(Shorekeeper)：浅蓝色/冰晶色渐变柔顺长发，金色/琥珀色温柔眼眸，"
        "佩戴半透明蓝色蝴蝶与星光质感头纱发饰，气质空灵神圣"
    ),
    "椿": (
        "《鸣潮》(Wuthering Waves) 角色「椿」(Camellya)：纯白长发挑染绯红发梢、双马尾发髻配红色山茶花发饰，绯红色眼眸，神态慵懒魅惑"
    ),
    "今汐": (
        "《鸣潮》(Wuthering Waves) 角色「今汐」(Jinhsi)：雪白及腰长发、精致双马尾与龙角发簪头饰，清冷金色/浅瞳色眼眸，端庄华贵"
    ),
    "长离": (
        "《鸣潮》(Wuthering Waves) 角色「长离」(Changli)：绯红与粉白渐变长发、精致盘发与凤羽发饰，金红色眼眸，成熟妩媚"
    ),
    "珂莱塔": (
        "《鸣潮》(Wuthering Waves) 角色「珂莱塔」(Carlotta)：银白/浅灰粉挑染双马尾长卷发、璀璨红宝石般眼眸，佩戴欧式贵族蝴蝶结发饰，大小姐优雅气质"
    ),
}


def _get_matched_character_keys(user_prompt: str) -> list[str]:
    """返回用户指令中命中的去重角色名列表（结合内置预设与 char_aliases.json 别称映射，杜绝单字误匹配）。"""
    prompt_lower = _strip_reply_context_wrapper(user_prompt).lower()
    used_spans: list[str] = []
    for cname in sorted(_CHARACTER_VISUAL_PRESETS.keys(), key=len, reverse=True):
        if cname in _SINGLE_CHAR_NAMES:
            continue
        clower = cname.lower()
        if clower in prompt_lower:
            if cname == "菲比" and "菲比啾比" in used_spans:
                continue
            if cname == "deepseek" and "大肥鱼" in used_spans:
                continue
            used_spans.append(cname)
    for canon in _resolve_canonical_characters(prompt_lower):
        if canon not in used_spans:
            if canon == "菲比" and "菲比啾比" in used_spans:
                continue
            used_spans.append(canon)
    return used_spans


_MULTI_INTERACTION_WORDS = (
    "两个角色", "两位角色", "两个人", "双人", "催促", "同框", "合影", "合照",
    "互动", "对视", "作为老板", "谈恋爱", "恋爱", "约会", "表白", "牵手",
    "拥抱", "抱在一起", "贴贴", "亲亲", "接吻", "结婚", "婚礼", "情侣", "并肩",
)
_MULTI_REL_RE = re.compile(
    r"(?:和|与|跟|同|向)[\u4e00-\u9fa5A-Za-z0-9]{1,12}(?:一起|谈恋爱|恋爱|约会|表白|贴贴|拥抱|牵手|亲|结婚|合照|合影|同框|互动|打架|喝茶|逛街|玩)",
    re.IGNORECASE,
)


def _is_multi_character_scene(user_prompt: str, has_ref_image: bool = False) -> bool:
    """判断是否为双人/多人同框互动场景：
    1. 文本中直接点名了 >= 2 位角色；
    2. 或者用户附带/引用了一张人物参考图（has_ref_image=True，代表图中角色1），同时在文本中点名了另一位角色（如“大肥鱼”）或提出了双人恋爱/互动要求（如“和大肥鱼谈恋爱”）。
    """
    raw_clean = _strip_reply_context_wrapper(user_prompt)
    keys = _get_matched_character_keys(raw_clean)
    if len(keys) >= 2:
        return True
    s = raw_clean.lower()
    if any(w in s for w in _MULTI_INTERACTION_WORDS) or _MULTI_REL_RE.search(s):
        return True
    if has_ref_image and len(keys) >= 1 and any(w in s for w in ("和", "与", "跟", "向", "一起")):
        return True
    return False


def _build_matched_presets_hint(user_prompt: str, *, has_ref_image: bool = False) -> str:
    """支持同时命中多个角色设定锚点，并在『参考图角色 + 预设角色（如大肥鱼）』双人同框场景下明确区分角色1与角色2。"""
    raw_clean = _strip_reply_context_wrapper(user_prompt)
    used_spans = _get_matched_character_keys(raw_clean)
    if not used_spans:
        return ""
    ref_cache: dict[str, str] = {}
    try:
        if _REFS_CACHE_FILE.is_file():
            ref_cache = json.loads(_REFS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        ref_cache = {}
    matched_entries = []
    is_multi = _is_multi_character_scene(raw_clean, has_ref_image=has_ref_image)
    for idx, cname in enumerate(used_spans):
        desc = _CHARACTER_VISUAL_PRESETS.get(cname) or ref_cache.get(cname) or f"二次元角色「{cname}」（请严格依据输入参考图或官方立绘设定刻画五官、发色与代表性服饰）"
        role_num = (idx + 2) if (has_ref_image and is_multi and len(used_spans) == 1) else (idx + 1)
        matched_entries.append(f"- 【独立角色 {role_num}（{cname}）】：{desc}")

    multi_guard = ""
    if is_multi:
        if has_ref_image and len(used_spans) == 1:
            multi_guard = (
                "【双人恋爱/互动同框最高铁律（严禁只画原图单人！严禁特征缝合！）】：\n"
                f"画面中必须同时画出 2 位完全独立分离的角色（Two distinct characters interacting romantically in the same scene）：\n"
                "1. 【角色 1】是输入参考图中的那位人物（完整保留参考图中人物的发型、发色、瞳色、五官与代表性服饰/头饰特征，但必须改变原图死板的单人站姿）！\n"
                f"2. 【角色 2】是「{used_spans[0]}」（严格按照上方【独立角色 2（{used_spans[0]}）】的二次元人形设定绘制）！\n"
                f"3. 角色 1 与 角色 2（{used_spans[0]}）必须在同一画面中亲密互动（如牵手、相视微笑、依偎约会、谈恋爱甜蜜氛围），绝不能只输出参考图原本的单人画面！"
            )
        elif len(used_spans) >= 2:
            multi_guard = (
                "【双人同框独立角色最高铁律（严禁特征缝合/严禁合成兽！）】：\n"
                f"画面中必须同时画出 {len(used_spans)} 位完全独立、左右分离的角色：\n"
                f"1. 角色 1（{used_spans[0]}）位于画面一侧，角色 2（{used_spans[1]}）位于画面另一侧，两人正在发生互动！\n"
                "2. 严禁把角色 2 的特征穿戴或缝合到角色 1 身上！\n"
            )
    return (
        "【已核验画面登场角色准确外观设定（最高优先级，必须100%按此二次元人形设定刻画）】：\n"
        + "\n".join(matched_entries)
        + "\n"
        + multi_guard
    )


def _reference_refinement_contamination(
    user_prompt: str,
    visual_anchor: str,
    refined_prompt: str,
    *,
    has_ref_image: bool = True,
) -> str:
    """检测提示词 Agent 是否向单张参考图任务中注入了无关本地角色或多人设定。"""
    raw_clean = _strip_reply_context_wrapper(user_prompt)
    if _is_multi_character_scene(raw_clean, has_ref_image=has_ref_image):
        return ""
    source_text = f"{raw_clean}\n{visual_anchor}"
    allowed_names = set(_get_matched_character_keys(source_text))
    injected_names = set(_get_matched_character_keys(refined_prompt)) - allowed_names
    if injected_names:
        return f"出现未由用户或参考图提供的角色：{', '.join(sorted(injected_names))}"

    multi_markers = (
        "双人同框",
        "两位角色",
        "两个角色",
        "角色1",
        "角色 1",
        "角色2",
        "角色 2",
    )
    has_left_right_roles = "左侧角色" in refined_prompt and "右侧角色" in refined_prompt
    if any(marker in refined_prompt for marker in multi_markers) or has_left_right_roles:
        return "单角色参考图任务被扩写成了多人场景"
    return ""


async def extract_character_visual_traits(api_key: str, image_b64: str, user_prompt: str = "") -> str:
    """从参考图中提取角色的头部五官发型特征，并结合多角色预设锚点与百炼提示词 Agent 生成完整指令。"""
    if not api_key or not image_b64:
        return ""
    clean_user_prompt = _strip_reply_context_wrapper(user_prompt)[:300]
    preset_hint = _build_matched_presets_hint(clean_user_prompt, has_ref_image=True)
    is_multi = _is_multi_character_scene(clean_user_prompt, has_ref_image=True)
    vl_res = ""
    try:
        import httpx

        url = f"{_DASHSCOPE_COMPAT.rstrip('/')}/chat/completions"
        task_instruction = (
            "这是一幅【双人恋爱/互动同框】生图任务！请仔细观察参考图中的人物（作为【角色1】），结合上方【角色2】的外观设定，直接编写一段给千问图像生成模型(qwen-image-2.1-pro)的双人同框互动中文生图指令（320字以内，不要寒暄）：\n"
            "1. 【角色1（参考图中的人物）】：精确提炼并描述参考图中人物的性别/少女或少年感、发色、刘海与发型、眼睛瞳色、面部五官、头饰/眼镜/配饰以及服装特征，确保不看原图也能100%还原该角色外观！\n"
            "2. 【角色2（用户点名的互动对象）】：完整保留上方预设中该角色的发色、呆毛、兽耳/尾鳍及服饰特征。\n"
            f"3. 【双人恋爱/互动构图与全新动作】：严格执行用户要求「{clean_user_prompt}」，描绘角色1与角色2在同一画面中面对面或并肩亲密互动（如牵手、拥抱、深情对视、约会谈恋爱，飘落花瓣与柔和暖光），两人左右独立分明，绝不能只画一个人！"
            if is_multi
            else (
                "请你仔细观察参考图，直接编写一段给千问图像编辑模型(qwen-image-2.1-pro)的高精度中文生图指令（240字以内，不要分段寒暄）：\n"
                "1. 【必须保留的头部与画风特征】：精确描述图中角色的发色、刘海、马尾/长发细节、眼睛瞳色、头饰/帽饰，以及清透的日韩二游官方 2D 赛璐珞立绘画风。\n"
                "2. 【保持参考一致】：仅修改用户明确要求的部分，未要求变更的构图、背景、服装与姿态保持原样；差分图不另造角色。\n"
                f"3. 【全新服装与动作场景特写】：严格按照用户的要求「{clean_user_prompt}」，将服装款式、动作、表情神态转化为画面感极强的 2D 二次元插画描述！"
            )
        )
        payload = {
            "model": os.getenv("DASHSCOPE_VL_MODEL") or _DEFAULT_QWEN_VL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_b64}},
                        {
                            "type": "text",
                            "text": f"用户绘图要求：「{clean_user_prompt}」。\n{preset_hint}\n{task_instruction}",
                        },
                    ],
                }
            ],
            "max_tokens": 450,
            "temperature": 0.25,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                choices = resp.json().get("choices") or []
                if choices:
                    vl_res = (choices[0].get("message", {}).get("content") or "").strip()
                    logger.info(f"Qwen-VL 垫图特征提取完成 (is_multi={is_multi}): {vl_res[:110]}...")
                    try:
                        canons = _resolve_canonical_characters(clean_user_prompt)
                        if not is_multi and len(canons) == 1 and canons[0] not in _CHARACTER_VISUAL_PRESETS and vl_res:
                            c_map = json.loads(_REFS_CACHE_FILE.read_text(encoding="utf-8")) if _REFS_CACHE_FILE.is_file() else {}
                            if canons[0] not in c_map:
                                c_map[canons[0]] = vl_res[:260]
                                _REFS_CACHE_FILE.write_text(json.dumps(c_map, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
    except Exception as exc:
        logger.warning(f"提取参考图特征异常: {exc}")

    vl_res = vl_res[:380]

    if not vl_res:
        if is_multi and preset_hint:
            return f"{preset_hint}\n请绘制参考图中的角色与上述角色一起：{clean_user_prompt}"
        return (
            "【参考图约束】：输入参考图是本次任务唯一的角色身份与外观依据。"
            "只绘制参考图中的角色，保留其五官、发色、发型、瞳色、头饰和核心服饰识别特征；"
            f"严格执行用户要求：{clean_user_prompt}"
        )

    if not is_multi and not _get_matched_character_keys(clean_user_prompt):
        logger.info("单角色参考图任务未指定预设角色名，直接采用 Qwen-VL 原图特征结果")
        return vl_res

    prompt_agent_id = (
        os.getenv("DASHSCOPE_PROMPT_AGENT_APP_ID") or "97fcb1dc96504f879c58f3b4c3a3e6d0"
    ).strip()
    if prompt_agent_id:
        vl_clause = f"【已从参考图提取的角色1外观与双人互动草稿】：{vl_res}\n" if vl_res else ""
        divergence_directive = (
            f"{preset_hint}{vl_clause}"
            f"【用户原始绘图需求】：{clean_user_prompt}\n"
            "【提示词深度发散与精细化指令（请输出 350~650 字纯享版生图 Prompt，勿输出寒暄或解释）】：\n"
            "1. 角色外貌锚点：若为双人同框互动（如参考图角色1与大肥鱼谈恋爱），必须同时详细刻画『左侧角色1（来自参考图的全部发色、瞳色、五官、服饰特征）』与『右侧角色2（如大肥鱼的蓝发、呆毛、鲸鱼耳、鲸鱼尾与女仆装）』各自独立分离且亲密互动，严禁漏画任何一人或特征缝合！\n"
            "2. 肢体动态与微表情：刻画两人深情对视、牵手/拥抱/依偎等恋爱感拉满的动作与羞涩微笑表情。\n"
            "3. 场景空间与浪漫光影：补充浪漫背景（如樱花飘落、夕阳暖光、光斑粒子）与景深层次。\n"
            "4. 镜头构图与画风标签：末尾附上日韩二游官方 2D 赛璐珞立绘英文质量标签（Masterpiece 2D anime game character CG, Wuthering Waves & Blue Archive official 2D art style, crisp clean anime lineart, luminous anime cel-shading）。"
        )
        polished = await _call_bailian_app(
            prompt_agent_id,
            api_key,
            divergence_directive,
            max_tokens=1000,
            style="prompt_agent",
            enable_web_search=True,
        )
        if polished and len(polished.strip()) > 25:
            polished_clean = polished.strip()[:850]
            contamination = _reference_refinement_contamination(
                clean_user_prompt,
                vl_res,
                polished_clean,
                has_ref_image=True,
            )
            if contamination:
                logger.warning(f"提示词 Agent 结果与参考图冲突，已丢弃并回退 Qwen-VL：{contamination}")
                return vl_res
            logger.info(f"百炼提示词 Agent ({prompt_agent_id}) 发散润色完成 (len={len(polished_clean)}): {polished_clean[:120]}...")
            _report_llm_progress("image", "2/4 垫图特征提取 + 百炼提示词 Agent 润色完成", 45, input_text=clean_user_prompt, prompt_text=polished_clean, extra_info=f"Agent ID: {prompt_agent_id} | 双人模式={is_multi}")
            return polished_clean
    return vl_res or preset_hint.strip()


async def search_character_visual_sheet(api_key: str, user_prompt: str) -> str:
    """无参考图时，优先调用百炼提示词 Agent（97fcb1dc96504f879c58f3b4c3a3e6d0），失败时回退内置联网二次元主美提示词 Agent。"""
    if not api_key:
        return ""

    clean_user_prompt = _strip_reply_context_wrapper(user_prompt)[:300]
    preset_hint = _build_matched_presets_hint(clean_user_prompt, has_ref_image=False)

    prompt_agent_id = (
        os.getenv("DASHSCOPE_PROMPT_AGENT_APP_ID") or "97fcb1dc96504f879c58f3b4c3a3e6d0"
    ).strip()
    if prompt_agent_id:
        divergence_directive = (
            f"{preset_hint}"
            f"【用户原始绘图需求】：{clean_user_prompt}\n"
            "【提示词深度发散与精细化指令（请输出 350~650 字纯享版生图 Prompt，勿输出寒暄或解释）】：\n"
            "1. 角色外貌锚点：严格遵循已核验角色设定（若无预设则联网核实官方立绘设定）；若涉及双人/多人同框，必须明确『左侧角色A……；右侧角色B……』各自独立分离互动，严禁特征缝合。\n"
            "2. 服饰材质与剪裁细化：在执行用户指定服装的基础上，补充褶皱质感、配饰细节与色彩层次。\n"
            "3. 肢体动态与微表情：刻画生动的眼神互动、面部微表情、手部动作与身体张力。\n"
            "4. 场景空间与光影氛围发散：主动发散符合情境的前景道具、中景互动环境、远景空间纵深，以及戏剧化主光、柔和补光、轮廓高光与光影空气感。\n"
            "5. 镜头构图与画风标签：指明景别与镜头视角，末尾附上：Masterpiece 2D anime game official character CG, Wuthering Waves & Blue Archive official 2D illustration style, crisp clean anime lineart, luminous flat anime cel-shading, expressive anime eyes."
        )
        agent_res = await _call_bailian_app(
            prompt_agent_id,
            api_key,
            divergence_directive,
            max_tokens=1000,
            style="prompt_agent",
            enable_web_search=True,
        )
        if agent_res and len(agent_res.strip()) > 25:
            agent_clean = agent_res.strip()[:850]
            logger.info(f"百炼提示词 Agent ({prompt_agent_id}) 发散生成完成 (len={len(agent_clean)}): {agent_clean[:120]}...")
            _report_llm_progress("image", "2/4 百炼提示词 Agent 发散与精细化完成", 45, input_text=clean_user_prompt, prompt_text=agent_clean, extra_info=f"Agent ID: {prompt_agent_id} | 长度: {len(agent_clean)}字")
            return agent_clean

    system = (
        "你是顶级二次元手游（《鸣潮》《蔚蓝档案》《明日方舟》）主美级生图提示词构建 Agent。\n"
        "请结合联网搜索（若提供了【已核验画面登场角色准确外观设定】则必须100%以核验设定为准），直接输出一段纯享版、高画质的中文+英文标签生图 Prompt（300~500字，严禁输出任何寒暄或解释）：\n"
        "1. 【剥离噪音与纠偏】：将“照片”一律转化为“日韩二游官方2D角色立绘插画”。\n"
        "2. 【双人同框防缝合】：若涉及两位角色，必须明确描述左右两个独立分离的角色同框互动，严禁特征合并。\n"
        "3. 【场景光影与服装动作发散】：严格执行用户要求的服装与互动动作，补充前中远景环境道具、光影色温与微表情细节。\n"
        "4. 【画风锚点】：末尾固定附上：Masterpiece 2D anime game official character illustration, Wuthering Waves & Blue Archive official 2D art style, crisp clean anime lineart, luminous flat anime cel-shading, expressive anime eyes, pure 2D illustration."
    )
    res = await _dashscope_chat_with_search(
        api_key=api_key,
        model=os.getenv("DASHSCOPE_MODEL") or _DEFAULT_QWEN,
        system=system,
        user=f"{preset_hint}请将以下用户请求发散并细化为高精度 2D 二次元生图 Prompt：{clean_user_prompt}",
        max_tokens=600,
    )
    if res and not res.startswith("联网问答"):
        res_clean = res.strip()[:800]
        logger.info(f"内置提示词 Agent 生成完成 (len={len(res_clean)}): {res_clean[:90]}...")
        return res_clean
    return preset_hint.strip()


_draw_failures = 0
_draw_open_until = 0.0


async def generate_image_with_qwen_or_wan(**kwargs) -> str:
    global _draw_failures, _draw_open_until
    if asyncio.get_running_loop().time() < _draw_open_until:
        raise DrawPipelineError("生图服务正在短暂熔断保护中，请半分钟后重试。")
    try:
        async with asyncio.timeout(120):
            result = await _generate_image_with_qwen_or_wan(**kwargs)
        if not result:
            raise DrawPipelineError()
        _draw_failures = 0
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _draw_failures += 1
        if _draw_failures >= 3:
            _draw_open_until = asyncio.get_running_loop().time() + 30
        if isinstance(exc, DrawPipelineError):
            raise
        raise DrawPipelineError("生图服务超时或连接失败，请稍后重试。") from exc


async def _generate_image_with_qwen_or_wan(
    *, api_key: str, user_prompt: str, image_b64_list: Optional[list[str]] = None,
    character: str = "amiya", visual_traits: str = "",
) -> str:
    import httpx

    if not api_key:
        raise DrawPipelineError("未配置生图服务密钥，请先在控制台完成配置。")

    raw_clean_prompt = _strip_reply_context_wrapper(user_prompt)
    refs = (image_b64_list or [])[:3]
    is_multi = _is_multi_character_scene(raw_clean_prompt, has_ref_image=bool(refs))
    lora_model = (os.getenv("DASHSCOPE_LORA_MODEL") or "").strip()
    lora_trigger = (os.getenv("DASHSCOPE_LORA_TRIGGER") or "").strip()
    primary_model = (os.getenv("DASHSCOPE_IMAGE_MODEL") or _DEFAULT_IMAGE_MODEL).strip()

    models_to_try: list[str] = []
    if lora_model:
        models_to_try.append(lora_model)
    for m in (primary_model, "qwen-image-2.1-pro", "qwen-image-3.0-pro"):
        if m and m not in models_to_try:
            models_to_try.append(m)

    trigger_prefix = f"{lora_trigger}, " if lora_trigger else ""
    sanitized_user_prompt = (
        raw_clean_prompt[:300]
        .replace("照片", "二次元立绘插画")
        .replace("相片", "二次元立绘插画")
    )

    # 用户提供的图片始终作为视觉依据，特征提取与失败重试都不能移除原图。
    active_refs = refs

    if active_refs and not is_multi:
        if visual_traits:
            full_prompt = (
                f"{trigger_prefix}{visual_traits}\n"
                f"【垫图全新重绘要求（严禁直接复制原图！）】：保留参考图角色面部五官与发饰特征，仅改变用户明确要求修改的部分；差分图保留其他构图、背景、服装与姿态，严格执行「{sanitized_user_prompt}」绘制全新动作与光影的日韩二游官方 2D 赛璐珞立绘。"
            )
        else:
            full_prompt = (
                f"{trigger_prefix}请保留输入参考图中这位二次元角色的面部五官轮廓、眼睛瞳色、发色刘海与头饰配饰，以及清透的日韩二游 2D 立绘画风。\n"
                f"严格保留参考图未被要求修改的部分，按用户指令修改：{sanitized_user_prompt}。\n"
                "Masterpiece 2D anime game character illustration, Wuthering Waves / Blue Archive official 2D art style, clean lineart, flat anime cel-shading."
            )
    else:
        if visual_traits:
            full_prompt = (
                f"{trigger_prefix}{visual_traits}\n"
                f"【核心画面指令】：{sanitized_user_prompt}。\n"
                "【画风与构图锁定】：纯正日韩二游官方 2D 赛璐珞双人/角色插画（Official 2D anime game character CG, two distinct characters interacting romantically if multi-character, clean crisp anime lineart, luminous flat anime cel shading），严禁三次元真人照片或 3D 塑料建模感。"
            )
        else:
            full_prompt = (
                f"{trigger_prefix}请绘制一张顶级日韩二次元手游官方 2D 立绘画风的角色插画：{sanitized_user_prompt}。\n"
                "严格按照用户描述绘制角色的服装款式、身体动作与光影场景（Official 2D anime game character CG, Wuthering Waves / Blue Archive / Arknights official 2D illustration style, "
                "clean crisp anime lineart, luminous anime cel shading, detailed anime eyes）。"
            )

    final_prompt_text = full_prompt.strip()[:1500]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=4)

    for model_name in models_to_try:
        content_list: list[dict[str, str]] = []
        for b64_uri in active_refs:
            content_list.append({"image": b64_uri})
        content_list.append({"text": final_prompt_text})

        use_extend = False if visual_traits else True

        for attempt in range(2):
            enable_deep_thinking = True if attempt == 0 else False
            read_timeout = 95.0 if enable_deep_thinking else 65.0
            timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=20.0, pool=10.0)

            params: dict[str, Any] = {
                "prompt_extend": use_extend,
                "enable_thinking": enable_deep_thinking,
                "n": 1,
                "watermark": False,
                "size": "1536*2048",
                "negative_prompt": (
                    "三次元, 真人摄影, 写实照片, 2.5D半写实, 油腻厚涂, 3D渲染, 塑料建模感, 手指畸形, 多手指, 五官崩坏, 肢体扭曲, 角色特征缝合, 低分辨率, 模糊, 水印"
                ),
            }
            if use_extend:
                params["prompt_extend_mode"] = "direct"

            payload = {
                "model": model_name,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": content_list,
                        }
                    ]
                },
                "parameters": params,
            }

            try:
                logger.info(
                    f"正在调用生图模型 {model_name} (第 {attempt + 1}/2 次, 传入底图={len(active_refs)}, 双人同框={is_multi}, "
                    f"enable_thinking={enable_deep_thinking}, prompt_len={len(final_prompt_text)})..."
                )
                _report_llm_progress(
                    "image",
                    f"3/4 正在调用 {model_name} 渲染画布 (第 {attempt + 1}/2 次, 双人同框={'是' if is_multi else '否'})...",
                    75,
                    input_text=raw_clean_prompt,
                    prompt_text=final_prompt_text,
                    extra_info=f"模型={model_name} | 底图={len(active_refs)}张 | 双人同框={is_multi} | 最终Prompt={len(final_prompt_text)}字",
                )
                async with httpx.AsyncClient(trust_env=False, timeout=timeout, limits=limits) as client:
                    resp = await client.post(_DASHSCOPE_MULTIMODAL_GEN, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        logger.warning(f"{model_name} 生图 HTTP {resp.status_code}")
                        if resp.status_code in {400, 401, 403}:
                            raise DrawPipelineError("生图请求被服务拒绝或参数不可用，本次未出图，请调整要求或检查配置。")
                        break
                    data = resp.json()
                    choices = (data.get("output") or {}).get("choices") or []
                    for ch in choices:
                        msg_content = (ch.get("message") or {}).get("content") or []
                        for item in msg_content:
                            img_url = item.get("image")
                            if img_url and isinstance(img_url, str):
                                logger.info(
                                    f"{model_name} 生图成功 (is_multi={is_multi}, enable_thinking={enable_deep_thinking}): {img_url}"
                                )
                                _report_llm_progress(
                                    "image",
                                    f"4/4 {model_name} 图像渲染完成！",
                                    100,
                                    input_text=raw_clean_prompt,
                                    prompt_text=final_prompt_text,
                                    output_text=img_url,
                                    extra_info=f"模型={model_name} | 双人同框={is_multi}",
                                    status="done",
                                )
                                if character == "kaltsit":
                                    return f"博士，图像已通过 {model_name} 渲染完毕。\n![插画]({img_url})"
                                if character == "dania":
                                    return f"哼，你要的图我已经盯着 {model_name} 给你画出来啦，快看看满不满意吧！\n![插画]({img_url})"
                                if refs:
                                    return f"博士，阿米娅已经结合参考图特征用 {model_name} 为您绘制好新插画啦！请过目~\n![插画]({img_url})"
                                return f"博士，阿米娅已经用 {model_name} 为您绘制好这幅插画啦！请看看合不合心意~\n![插画]({img_url})"
                    break
            except DrawPipelineError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"{model_name} 生图调用异常 (第 {attempt + 1}/2 次, enable_thinking={enable_deep_thinking}): {exc}"
                )
                if attempt == 0:
                    import asyncio
                    await asyncio.sleep(1.2)
                continue

    raise DrawPipelineError()


def _extract_bailian_text(data: dict[str, Any]) -> str:
    """从百炼 JSON 结构中提取文本。"""
    output = data.get("output") or {}
    text = (
        output.get("text")
        or (output.get("choices") or [{}])[0].get("message", {}).get("content")
    )
    if isinstance(text, str) and text.strip() and text.strip() not in {"stop", "null"}:
        return text.strip()
    for key in ("text", "result", "content"):
        val = output.get(key)
        if isinstance(val, str) and val.strip() and val.strip() not in {"stop", "null"}:
            return val.strip()
    return ""


# 严格限制生图触发词为明确的“创作/绘制/修图/改图”动作，移除裸名词（如“照片/头像/壁纸”）及被动查看词（“看/给/发”），彻底防止引用图片提问时误触发走生图！
_AUTO_DRAW_KEYWORDS = (
    "画一张", "画一幅", "画个", "画幅", "绘图", "生图", "生成图片", "生成一张", "生成一幅", "生成个",
    "作画", "重绘", "改图", "修图", "换装", "二次元化", "手办化", "卡通化", "垫图", "图生图",
    "差分图", "画这个", "变q版", "做成q版", "输出更换后的图", "来一张", "来一幅", "整一张", "搞一张", "弄一张",
)

_AUTO_DRAW_RE = re.compile(
    r"(?:生成|画|绘|重绘|绘制|制作|做|弄|来|整|搞).{0,12}(?:一)?(?:张|幅|个|份).{0,14}(?:图|照片|相片|写真|立绘|插画|肖像|自拍|头像|壁纸|q版|jk|泳装|女仆|旗袍)"
    r"|不知道.{0,10}长什么样.{0,16}(?:生成|画|来|整|搞).{0,8}(?:照片|图|立绘)"
    r"|(?:生成|画|绘|重绘).{0,10}(?:和|与|跟).{1,12}(?:谈恋爱|恋爱|约会|贴贴|拥抱|牵手|结婚|同框|合照|合影).{0,6}(?:的)?(?:图|画|插画|照片)?",
    re.IGNORECASE,
)

_VISION_QA_EXCLUSIONS = (
    "这是啥", "这是什么", "这图是", "这张图是", "图里是", "图中是", "写的是", "写的什么", "写着什么",
    "翻译", "总结", "解释", "什么意思", "啥意思", "看得到", "看到吗", "看得见", "能看到", "是谁", "哪来的", "出处",
)


def _auto_detect_draw_intent(text: str, has_image: bool = False) -> bool:
    """精准识别用户是否要求【生成/重绘图片】：
    1. 自动剥离【用户当前引用/回复...发送的一张图片】系统包装头，绝不因包装头含“发送的一张图片”误判为生图；
    2. 当用户引用/附带图片提问（如“这是啥图”、“翻译一下”、“总结”）时，严格返回 False（走 Qwen-VL 视觉问答）；
    3. 仅当用户明确发出“生成一张...的图”、“画一张...”、“把她换成/改成...”等创作指令时才返回 True。
    """
    raw_clean = _strip_reply_context_wrapper(text).lower()
    if not raw_clean:
        return False
    if raw_clean.startswith(("/draw", "/workflow")):
        return True
    if any(k in raw_clean for k in _AUTO_DRAW_KEYWORDS):
        return True
    if _AUTO_DRAW_RE.search(raw_clean):
        return True
    if any(q in raw_clean for q in _VISION_QA_EXCLUSIONS):
        return False
    if has_image and re.search(
        r"(?:把(?:她|他|它|这|图|衣服|服装|动作|姿势|背景|头发|风格)|换成|改成|变成|转为|画成|重绘成|加上|去掉)",
        raw_clean,
    ):
        return True
    return False


async def _call_bailian_app(
    app_id: str,
    api_key: str,
    prompt: str,
    *,
    max_tokens: int = 800,
    style: str = "amiya",
    enable_web_search: bool | None = None,
    is_long_task: bool = False,
    is_draw: bool = False,
    extra_visual_traits: str = "",
    voice_mode_only: bool = False,
) -> Optional[str]:
    """阿里百炼应用 completion（支持 SSE 流式保活，防止网关空闲断连与云端沙箱抢跑文件导出）。"""
    url = f"{_DASHSCOPE_APP_BASE}/{app_id}/completion"
    if is_draw:
        tokens_limit = 3500
        traits_hint = f"\n【角色头部与五官设定】：{extra_visual_traits}\n" if extra_visual_traits else ""
        styled = (
            "【角色与生图委托】你是罗德岛领袖阿米娅。博士现在委托你绘制一幅角色插画。\n"
            f"{traits_hint}"
            "【生图铁律】：\n"
            "1. 严格按照博士要求的【服装款式】与【身体姿势动作】编写生图 Prompt，严禁使用角色默认原皮服装覆盖博士指定的服装！\n"
            "2. 强制日韩二游官方 2D 赛璐珞/清透立绘画风：加入『Official 2D anime game character illustration, Wuthering Waves / Blue Archive official art style, crisp clean anime lineart, flat anime cel shading, expressive anime eyes』。\n"
            "3. 立即调用生图工具并直接输出完整 Markdown 图片链接。\n"
            f"博士：{prompt}"
        )
    elif is_long_task:
        tokens_limit = 4000
        styled = (
            "【角色与工作委托】你是罗德岛领袖阿米娅。请以专业干练态度认真完成博士交代的深度研究/物理或学科教案/分析/创作委托。\n"
            "【严禁口头推脱与严禁云端沙箱建文件】：\n"
            "1. 严禁回复“阿米娅这就去办”等客套推脱！必须在本次回复中直接输出层次分明、公式准确（使用标准 LaTeX $...$ 或 $$...$$）、细节充实的完整 Markdown 正文（包含多级标题、教学目标、重难点、具体实验与教学步骤、板书设计及课后作业）。\n"
            "2. 严禁在云端代码沙箱中尝试生成 .docx/.pdf 文件或提供下载链接！本地系统会自动把你输出的 Markdown 正文打包为 Word/PDF 文件并上传给博士。\n"
            f"博士：{prompt}"
        )
    elif style == "amiya":
        tokens_limit = max(int(max_tokens), 800)
        styled = (
            "【回复】用阿米娅口吻对博士说，1～3句自然口语短句，有温度；"
            "主动结合上文线索与博士刚才说的话做机敏联想。禁止说明书/百科长段。若使用了联网结果，用阿米娅口吻转述，勿自称助手。\n"
            "【持续状态优先】：若博士在本轮或上文中设定了尚未解除的身体限制或负重处境（如嘴巴被封住/贴胶带则在取下前严禁说出任何词汇，只能输出「唔……唔唔！嗯……呜呜……唔？」；"
            "如背着一箱沙子/重物则在放下前即使聊其他话题也必须全程喘息「呼……哈……」并提及负重感受），必须严格遵守！\n"
            f"博士：{prompt}"
        )
    elif style == "kaltsit":
        tokens_limit = max(int(max_tokens), 800)
        styled = (
            "【回复】用凯尔希口吻对博士说，1～3句简短冷静的口语，可带一点毒舌；"
            "主动结合上文线索与博士刚才说的话做机敏联想。禁止说明书/百科长段。若使用了联网结果，用凯尔希口吻转述，勿自称助手。\n"
            "【持续状态优先】：若博士在本轮或上文中设定了尚未解除的身体限制或负重处境（如封嘴则在取下前只能发出「唔……嗯……」闷哼声，如负重则在放下前持续体现负重状态），必须严格维持！\n"
            f"博士：{prompt}"
        )
    elif style == "dania":
        tokens_limit = max(int(max_tokens), 800)
        styled = (
            "【回复】用《鸣潮》达妮娅口吻对漂泊者说，1～3句自然口语短句，带一点傲娇嘴硬但关心同伴；"
            "主动结合上文线索与漂泊者刚才说的话做机敏联想。严禁括号动作描写，禁止说明书/百科长段。若使用了联网结果，用达妮娅口吻转述，勿自称助手。\n"
            "【持续状态优先】：若对方在本轮或上文中设定了尚未解除的身体限制或负重处境（如嘴巴被封住在取下前只能输出「唔……唔唔！嗯……」，如背着一箱沙子在放下前全程喘息并提及负重），必须严格维持！\n"
            f"漂泊者：{prompt}"
        )
    else:
        tokens_limit = max(int(max_tokens), 800)
        styled = prompt or ""

    if voice_mode_only:
        tokens_limit = 260

    is_prompt_agent = style == "prompt_agent"
    if not is_prompt_agent and not is_draw:
        styled = conversation.app_prompt(styled)
    params: dict[str, Any] = {
        "max_tokens": tokens_limit,
        "has_thoughts": True if is_prompt_agent else False,
        "incremental_output": False,
    }
    if enable_web_search is not None:
        params["enable_web_search"] = bool(enable_web_search)
    payload = {
        "input": {"prompt": styled},
        "parameters": params,
        "debug": {},
    }
    try:
        import httpx
    except ImportError:
        return "缺少 httpx 依赖，请在 bot/.venv 中 pip install httpx。"

    sse_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-DashScope-SSE": "enable",
    }
    latest_text = ""
    try:
        read_limit = 45.0 if is_prompt_agent else (180.0 if is_long_task else 120.0)
        timeout = httpx.Timeout(connect=10.0, read=read_limit, write=20.0, pool=10.0)
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=4)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout, limits=limits) as client:
            async with client.stream("POST", url, json=payload, headers=sse_headers) as resp:
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="ignore")
                    logger.warning(f"百炼 App SSE HTTP {resp.status_code}: {err_body[:400]}")
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
                            piece = _extract_bailian_text(pkt)
                            if piece:
                                latest_text = piece
                        except json.JSONDecodeError:
                            continue
                else:
                    body_bytes = await resp.aread()
                    data = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    latest_text = _extract_bailian_text(data)
        if latest_text.strip():
            if is_long_task and "沙箱的文件目录在本次任务中被回收" in latest_text and len(latest_text.strip()) < 300:
                logger.warning("百炼返回云端沙箱目录回收致歉且无正文，自动切换至千问/DeepSeek 直出完整教案正文")
                return None
            return latest_text.strip()
        logger.warning("百炼返回为空（SSE 流结束未解析到文本）")
        return None
    except Exception as exc:  # noqa: BLE001
        if latest_text and len(latest_text.strip()) > 60:
            logger.info(f"百炼流在中途断开但已获取有效内容 ({len(latest_text)} chars)，直接采用")
            return latest_text.strip()
        logger.warning(f"百炼调用异常 ({app_id[:8]}...): {exc}")
        return None


async def chat(system: str, user: str, max_tokens: int = 150) -> str:
    """通用异步对话（安慰 / 群画像 / 默认）；无 key 时返回短中文说明。"""
    api_key, base_url, model = _resolve_credentials()
    if not api_key:
        raise LLMProviderError(_NO_KEY_MSG)
    return await _post_chat_completions(
        api_key=api_key,
        base_url=base_url,
        model=model,
        system=system,
        user=user,
        max_tokens=max_tokens,
    )


_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".amr", ".silk", ".pcm", ".opus"}


def _ensure_wav_for_bailian(audio_path: str) -> Path:
    """若输入音频为 QQ 原始 SILK v3 (.amr / .silk)，自动调用本机 pilk 解码为标准 24kHz WAV 文件供阿米娅 Agent 解析。"""
    p = Path(audio_path)
    if not p.is_file():
        return p
    try:
        head = p.read_bytes()[:16]
        if b"SILK_V3" in head:
            import pilk
            out_wav = Path("/tmp") / f"{p.stem}_decoded.wav"
            pilk.silk_to_wav(str(p), str(out_wav), rate=24000)
            if out_wav.is_file() and out_wav.stat().st_size > 100:
                logger.info(f"已通过 pilk 将 QQ 语音 {p.name} 解码为标准 WAV: {out_wav.name} ({out_wav.stat().st_size} bytes)")
                return out_wav
    except Exception as exc:
        logger.warning(f"SILK 解码 WAV 跳过，使用原文件 {p.name}: {exc}")
    return p


async def parse_voice_via_amiya_agent(
    api_key: str,
    audio_paths: list[str],
    user_prompt: str = "",
    target_character: str = "amiya",
    voice_mode_only: bool = False,
) -> Optional[str]:
    """将用户发送或引用的语音文件上传至百炼临时 OSS，交由阿米娅 Agent 处理，并提供 qwen-omni-turbo 自动转写双保险。"""
    if not api_key or not audio_paths:
        return None
    from . import workflow_service

    amiya_app_id = _operator_app_id("amiya")
    oss_urls: list[str] = []
    local_wavs: list[Path] = []

    for ap in audio_paths[:2]:
        wav_p = _ensure_wav_for_bailian(ap)
        if not wav_p.is_file():
            continue
        local_wavs.append(wav_p)
        oss_u = await workflow_service.upload_file_to_bailian_temp_oss(
            api_key, wav_p, model_name="qwen-plus"
        )
        if oss_u:
            oss_urls.append(oss_u)

    asr_transcript = ""
    if local_wavs:
        try:
            import httpx
            wav_bytes = local_wavs[0].read_bytes()
            b64_audio = base64.b64encode(wav_bytes).decode("ascii")
            fmt = local_wavs[0].suffix.lower().lstrip(".") or "wav"
            url = f"{_DASHSCOPE_COMPAT.rstrip('/')}/chat/completions"
            payload = {
                "model": "qwen-omni-turbo",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:;base64,{b64_audio}",
                                    "format": fmt if fmt in {"wav", "mp3"} else "wav",
                                },
                            },
                            {"type": "text", "text": "请一字不差地将这段语音转写为中文文本，只输出转写后的原话内容，不要任何多余解释。"},
                        ],
                    }
                ],
                "max_tokens": 600,
            }
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            async with httpx.AsyncClient(trust_env=False, timeout=25.0) as client:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 200:
                    choices = r.json().get("choices") or []
                    if choices:
                        asr_transcript = (choices[0].get("message", {}).get("content") or "").strip()
                        if asr_transcript:
                            logger.info(f"语音转写辅助提取完成: {asr_transcript}")
        except Exception as exc:
            logger.debug(f"qwen-omni-turbo 辅助转写跳过，直接走阿米娅 Agent 语音解析: {exc}")

    if amiya_app_id:
        try:
            import httpx
            url = f"{_DASHSCOPE_APP_BASE}/{amiya_app_id}/completion"
            asr_hint = f"\n【语音内容识别文本】：{asr_transcript}\n" if asr_transcript else ""
            req_text = _strip_reply_context_wrapper(user_prompt) or "请解析这段语音的内容并回应。"
            prompt_for_amiya = conversation.app_prompt(
                f"博士向你发送了一段语音消息。{asr_hint}"
                f"【博士的附加要求】：{req_text}"
            )
            inp: dict[str, Any] = {"prompt": prompt_for_amiya}
            if oss_urls:
                inp["file_list"] = oss_urls
                inp["biz_params"] = {
                    "file_urls": oss_urls,
                    "audio_url": oss_urls[0],
                    "files": oss_urls,
                }
            payload = {
                "input": inp,
                "parameters": {
                    "max_tokens": 260 if voice_mode_only else 3500,
                    "has_thoughts": False,
                    "incremental_output": False,
                },
            }
            sse_headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "X-DashScope-SSE": "enable",
            }
            if oss_urls:
                sse_headers["X-DashScope-OssResourceResolve"] = "enable"

            latest_text = ""
            timeout = httpx.Timeout(connect=10.0, read=90.0, write=20.0, pool=10.0)
            async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
                async with client.stream("POST", url, json=payload, headers=sse_headers) as resp:
                    if resp.status_code < 400:
                        async for line in resp.aiter_lines():
                            s = line.strip()
                            if not s.startswith("data:"):
                                continue
                            raw_json = s[5:].strip()
                            if not raw_json or raw_json == "[DONE]":
                                continue
                            try:
                                pkt = json.loads(raw_json)
                                piece = _extract_bailian_text(pkt)
                                if piece:
                                    latest_text = piece
                            except Exception:
                                continue
            if latest_text.strip():
                logger.info(f"阿米娅 Agent 语音解析回复完成 (len={len(latest_text)})")
                if voice_mode_only:
                    return latest_text.strip()
                if asr_transcript and "【语音原话】" not in latest_text:
                    return f"🎙️ **语音识别内容**：「{asr_transcript}」\n\n{latest_text.strip()}"
                return latest_text.strip()
        except Exception as exc:
            logger.warning(f"调用阿米娅 Agent 解析语音异常: {exc}")

    if asr_transcript:
        return asr_transcript if voice_mode_only else f"🎙️ **语音识别内容**：「{asr_transcript}」"
    return None


async def chat_operator(
    character: str,
    user: str,
    max_tokens: int = 150,
    is_long_task: bool = False,
    is_draw: bool = False,
    image_b64_list: Optional[list[str]] = None,
    file_paths: Optional[list[str]] = None,
    voice_mode_only: bool = False,
) -> str:
    """干员模式：支持百炼多模态工作流自动上传分发、看图问答、qwen-image-2.1-pro 生图及长任务断连自动兜底。"""
    char = (character or "").strip().lower()
    if char not in {"amiya", "kaltsit", "dania"}:
        return "未知角色，仅支持 amiya / kaltsit / dania。"

    ds_key = _dashscope_key()
    app_id = _operator_app_id(char)
    images = list(image_b64_list or [])
    extra_files = list(file_paths or [])
    raw_user_cmd = _strip_reply_context_wrapper(user or "")

    # 仅针对用户自己输入的原始指令检测生图意图（绝不把【用户当前引用...发送的一张图片】包装头误判为生图！）
    if (
        not voice_mode_only
        and not is_draw
        and not doc_service.detect_requested_formats(raw_user_cmd)
        and _auto_detect_draw_intent(raw_user_cmd, has_image=bool(images))
    ):
        is_draw = True
        logger.info(f"chat_operator 自动识别生图意图成功: '{raw_user_cmd[:35]}'")

    if is_draw and images and raw_user_cmd:
        _maybe_save_character_ref(raw_user_cmd, images)

    if is_draw and not images and raw_user_cmd:
        matched_keys = _get_matched_character_keys(raw_user_cmd)
        if _is_multi_character_scene(raw_user_cmd, has_ref_image=False) or len(matched_keys) >= 2:
            logger.info(
                f"检测到多角色同框场景 ({matched_keys})，采用双人独立文字设定锚点 + 纯文生图 (T2I) 防缝合与防超时模式"
            )
            images = []
        else:
            images = _load_local_character_refs(raw_user_cmd)[:1]

    # A-Voice) 若附件中包含 QQ 语音条或音频文件，优先交由阿米娅 Agent 的语音解析能力处理
    audio_files = [
        f for f in extra_files
        if Path(f).suffix.lower() in _AUDIO_EXTS or "record" in f.lower() or "silk" in f.lower() or "amr" in f.lower()
    ]
    if audio_files and ds_key:
        logger.info(f"检测到语音/音频输入 ({len(audio_files)} 个)，正在交由阿米娅 Agent 解析语音...")
        voice_reply = await parse_voice_via_amiya_agent(
            api_key=ds_key,
            audio_paths=audio_files,
            user_prompt=user or "",
            target_character=char,
            voice_mode_only=voice_mode_only,
        )
        if voice_reply:
            return voice_reply

    # A0) 百炼工作流双向分发
    use_workflow = not voice_mode_only and (
        bool(extra_files)
        or raw_user_cmd.lower().startswith("/workflow")
        or (os.getenv("DASHSCOPE_USE_WORKFLOW") or "").strip() in {"1", "true", "yes"}
    )
    if use_workflow and ds_key:
        from . import workflow_service

        clean_prompt = re.sub(r"^/workflow\s*", "", raw_user_cmd, flags=re.IGNORECASE)
        all_sources: list[str | Path] = []
        all_sources.extend(extra_files)
        all_sources.extend(images)
        wf_reply = await workflow_service.run_bailian_workflow(
            api_key=ds_key,
            user_prompt=clean_prompt or raw_user_cmd,
            file_sources=all_sources,
            task_type="draw" if is_draw else "qa",
            character=char,
        )
        if wf_reply:
            if is_draw and not re.search(r"!\\[.*?\\]\\(https?://", wf_reply):
                raise DrawPipelineError("绘图工作流未返回图片，本次未出图。")
            return wf_reply
        logger.warning("百炼工作流返回为空，自动回退直连模型...")

    # A1) 看图对话（含引用他人图片询问“这是啥图”、要求翻译、看图解答，默认不走生图，直接走 Qwen-VL 视觉理解）
    if images and not is_draw and ds_key:
        vision_reply = await chat_with_vision(
            api_key=ds_key,
            character=char,
            user_text=user or "",
            image_b64_list=images,
            max_tokens=260 if voice_mode_only else (1500 if is_long_task else 500),
            voice_mode_only=voice_mode_only,
        )
        if vision_reply:
            return vision_reply

    # A2) 生图委托：直接调用 qwen-image-2.1-pro（支持“引用角色A的图 + 生成和大肥鱼谈恋爱的图”双人互动生图）
    extra_traits = ""
    if is_draw and ds_key:
        user_norm = _normalize_prompt_with_aliases(raw_user_cmd)
        if images:
            extra_traits = await extract_character_visual_traits(ds_key, images[0], user_norm)
        else:
            extra_traits = await search_character_visual_sheet(ds_key, user_norm)

        qwen_img_reply = await generate_image_with_qwen_or_wan(
            api_key=ds_key,
            user_prompt=user_norm,
            image_b64_list=images,
            character=char,
            visual_traits=extra_traits,
        )
        if qwen_img_reply:
            return qwen_img_reply
        logger.warning("is_draw=True 但生图模型均未返回有效图片，拦截纯文本幻觉回退")
        raise DrawPipelineError()

    if is_draw:
        raise DrawPipelineError("生图服务未配置，本次没有生成图片。")

    # B) 百炼应用（Agent 2.0：SSE 流式调用）
    if ds_key and app_id:
        bailian_reply = await _call_bailian_app(
            app_id,
            ds_key,
            user or "",
            max_tokens=260 if voice_mode_only else (4000 if (is_long_task or is_draw) else max(int(max_tokens), 800)),
            voice_mode_only=voice_mode_only,
            style=char,
            enable_web_search=True,
            is_long_task=is_long_task,
            is_draw=is_draw,
            extra_visual_traits=extra_traits,
        )
        if bailian_reply:
            if not voice_mode_only and not is_draw and ("dashscope-result" in bailian_reply or "![" in bailian_reply):
                logger.info("检测到百炼应用触发了内置生图链接，自动转交 qwen-image-2.1-pro 渲染...")
                traits_fallback = await search_character_visual_sheet(ds_key, raw_user_cmd)
                qwen_override = await generate_image_with_qwen_or_wan(
                    api_key=ds_key,
                    user_prompt=raw_user_cmd,
                    image_b64_list=images,
                    character=char,
                    visual_traits=traits_fallback,
                )
                if qwen_override:
                    return qwen_override
            if not is_draw and not is_long_task:
                bailian_reply = await resolve_and_enforce_situational_state(
                    char, raw_user_cmd, bailian_reply, voice_mode_only=voice_mode_only
                )
            return bailian_reply
        logger.warning("百炼 App 返回为空或异常，触发自动回退模型兜底...")

    # C) 自动兜底 1：DashScope OpenAI 兼容接口（千问 qwen-plus + 联网搜索 + 本地干员人设 RAG）
    system = build_operator_system(char, is_long_task=is_long_task)
    target_tokens = 260 if voice_mode_only else (4000 if is_long_task else int(max_tokens))
    if ds_key:
        model = (os.getenv("DASHSCOPE_MODEL") or _DEFAULT_QWEN).strip()
        qwen_reply = await _dashscope_chat_with_search(
            api_key=ds_key,
            model=model,
            system=system,
            user=user or "",
            max_tokens=target_tokens,
        )
        if qwen_reply and not qwen_reply.startswith("联网问答"):
            if not is_draw and not is_long_task:
                qwen_reply = await resolve_and_enforce_situational_state(
                    char, raw_user_cmd, qwen_reply, voice_mode_only=voice_mode_only
                )
            return qwen_reply

    # D) 自动兜底 2：DeepSeek / OpenAI + 本地干员人设 RAG
    api_key, base_url, model = _resolve_credentials()
    if api_key:
        logger.info("干员模式：使用通用 LLM (DeepSeek/OpenAI) 兜底生成回复")
        ds_reply = await _post_chat_completions(
            api_key=api_key,
            base_url=base_url,
            model=model,
            system=system,
            user=user or "",
            max_tokens=target_tokens,
        )
        if not is_draw and not is_long_task:
            ds_reply = await resolve_and_enforce_situational_state(
                char, raw_user_cmd, ds_reply, voice_mode_only=voice_mode_only
            )
        return ds_reply

    raise LLMProviderError(_NO_DASHSCOPE_MSG)


from .situational_state import (
    _SITUATIONAL_STATE_CUES,
    is_muffled_gagged_text as _is_pure_muffled_text,
    resolve_and_enforce_situational_state as _resolve_situational_state_impl,
)


async def resolve_and_enforce_situational_state(
    character: str,
    raw_user_text: str,
    draft_reply: str,
    *,
    voice_mode_only: bool = False,
) -> str:
    """委托独立分包 situational_state.py 执行按 QQ 号隔离的持续状态分辨与闭口鼻音修正。"""
    return await _resolve_situational_state_impl(
        character,
        raw_user_text,
        draft_reply,
        resolve_credentials_fn=_resolve_credentials,
        voice_mode_only=voice_mode_only,
    )


_ASSISTANT_SYSTEM = """你是简洁的中文助手，在 QQ 里回答问题。
优先依据检索/联网结果；没有依据时明确说不确定。
回答短一些（通常三四句内），给可执行建议；涉及抽卡/版本信息请注明时效与来源不确定性。
不要扮演明日方舟干员，不要用阿米娅口吻。"""


def _assistant_app_id() -> str:
    return (os.getenv("DASHSCOPE_APP_ID_ASSISTANT") or "").strip()


def looks_like_web_intent(text: str) -> bool:
    """粗意图：时事/他游/攻略/需联网。"""
    t = (text or "").strip().lower()
    if not t:
        return False
    keys = [
        "鸣潮", "原神", "绝区零", "抽卡", "up池", "下个版本", "下版本",
        "新角色", "攻略", "强度", "毕业", "天气", "新闻", "股价", "比分", "赛程",
        "搜索", "查一下", "联网", "最新", "官网", "机票",
        "wuthering", "wuwa", "genshin", "version", "banner",
    ]
    lore = ["阿米娅", "凯尔希", "罗德岛", "源石", "整合运动", "特蕾西娅", "达妮娅", "拉海洛", "阿列夫一"]
    if any(k in t for k in lore) and not any(k in t for k in ("鸣潮", "原神", "绝区零", "抽卡", "下版本")):
        return False
    return any(k in t for k in keys)


async def _dashscope_chat_with_search(
    *, api_key: str, model: str, system: str, user: str, max_tokens: int
) -> str:
    """DashScope OpenAI 兼容接口 + enable_search（千问联网）。"""
    url = f"{_DASHSCOPE_COMPAT.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": conversation.messages(system, user or ""),
        "max_tokens": int(max_tokens),
        "temperature": 0.5,
        "enable_search": True,
        "search_options": {"forced_search": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        import httpx
    except ImportError:
        return "缺少 httpx 依赖，请在 bot/.venv 中 pip install httpx。"
    try:
        timeout = httpx.Timeout(connect=15.0, read=150.0, write=30.0, pool=15.0)
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.warning(f"千问联网 HTTP {resp.status_code}: {resp.text[:400]}")
                return f"联网问答调用失败（HTTP {resp.status_code}）。"
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return "联网问答返回为空。"
            msg = choices[0].get("message") or {}
            content = (msg.get("content") or "").strip()
            return content or "联网问答返回为空。"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"千问联网调用异常: {exc}")
        return "联网问答调用异常，请稍后重试。"


async def chat_assistant(user: str, max_tokens: int = 260) -> str:
    """助手分支（非干员人设）：优先百炼助手 App，否则 DashScope 千问 + 强制联网，再退回 DeepSeek。"""
    ds_key = _dashscope_key()
    aid = _assistant_app_id()
    if ds_key and aid:
        reply = await _call_bailian_app(
            aid,
            ds_key,
            user or "",
            max_tokens=max_tokens,
            style="plain",
            enable_web_search=True,
        )
        if reply:
            return reply

    if ds_key:
        model = (os.getenv("DASHSCOPE_MODEL") or _DEFAULT_QWEN).strip()
        return await _dashscope_chat_with_search(
            api_key=ds_key,
            model=model,
            system=_ASSISTANT_SYSTEM,
            user=user or "",
            max_tokens=max_tokens,
        )

    api_key, base_url, model = _resolve_credentials()
    if api_key:
        return await _post_chat_completions(
            api_key=api_key,
            base_url=base_url,
            model=model,
            system=_ASSISTANT_SYSTEM,
            user=user or "",
            max_tokens=max_tokens,
        )
    raise LLMProviderError(_NO_DASHSCOPE_MSG)
