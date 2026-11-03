"""GPT-SoVITS api_v2 语音合成服务，支持本地归档与原生 24kHz SILK 预转码。
失败抛出 TTSGenerationError；语音模式由分发入口记录失败并保持静默。
"""
from __future__ import annotations

import array
import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Optional

import httpx
from nonebot import logger
from .config_manager import state
from .errors import TTSGenerationError
from .history_recorder import recorder, current_task_id
from .voice_director_adapter import select_delivery
from .voice_presets import VOICE_CANONICAL_PRESETS, resolve_character_id

BOT_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = BOT_ROOT.parent
GSV_ROOT = WORKSPACE_ROOT / "gpt-sovits"
OUTPUT_DIR = GSV_ROOT / "output"
OUTPUTS_DIR = GSV_ROOT / "outputs"
ROLES_FILE = GSV_ROOT / "voice_roles.json"

NAPCAT_ADDON_PATH = (
    WORKSPACE_ROOT / "napcat" / "shell" / "native" / "ffmpeg" / "ffmpegAddon.darwin.arm64.node"
)

TTS_URL = os.getenv("TTS_URL", "http://127.0.0.1:9880/tts")
MAX_VOICE_DURATION_SEC = 30.0
VOICE_SAFE_MAX_CHARS = 95
MAX_CHARS = 195
SHORT_SYNC_THRESHOLD = 95
FIRST_SENTENCE_MAX = 80
TTS_DEFAULT_TIMEOUT = 45.0
TTS_CIRCUIT_FAILURES = 10
TTS_CIRCUIT_COOLDOWN = 10.0

_TTS_MPS_LOCK = asyncio.Lock()
_failures = 0
_open_until = 0.0
LAST_AUDIO_META: dict[str, Any] = {}
_VOICE_CHECK_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "expires_at": 0.0,
    "res": {"tts_9880_online": False, "studio_9876_online": False},
}


def _resolve_audio_path(p: str) -> str:
    """将 voice_roles.json 中的相对路径统一解析为绝对路径。"""
    if not p:
        return ""
    path_obj = Path(p)
    if path_obj.is_absolute():
        return str(path_obj)
    return str((GSV_ROOT / path_obj).resolve())


def _clean(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)", "", text)
    text = re.sub(r"!\s*\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    for _ in range(8):
        cleaned = re.sub(r"[（(【\[][^（）()【】\[\]]*[）)】\]]", "", text)
        if cleaned == text:
            break
        text = cleaned
    text = re.sub(r"(?<!\*)\*(?!\*)[^*\n]+\*(?!\*)", "", text)
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u20E3]", "", text)
    text = re.sub(r"(?m)^\s*(?:[-+>] |\d+[.)、]\s*)", "", text)
    text = re.sub(r"[*#`>_~{}\[\]（）()【】]", "", text)
    text = re.sub(r"\s+", "，", text.strip())
    text = re.sub(r"[，,]{2,}", "，", text)
    text = re.sub(r"\.{3,}|…{2,}", "…", text)
    return text.strip("，,、 ")


def _extract_first_sentence(text: str, max_len: int = FIRST_SENTENCE_MAX) -> str:
    s = text.strip()
    if len(s) <= max_len:
        return s
    parts = re.split(r"(?<=[。！？!?；;\n])", s)
    first = (parts[0] if parts else s).strip()
    if first and len(first) <= max_len:
        return first
    sub_parts = re.split(r"(?<=[，,、：:])", first or s)
    acc = ""
    for sp in sub_parts:
        if len(acc) + len(sp) <= max_len:
            acc += sp
        else:
            break
    if acc.strip():
        return acc.strip().rstrip("，,、：:")[: max_len - 1] + "。"
    return s[: max_len - 1].rstrip("，,、：:") + "。"


def _smart_trim_for_tts(text: str, max_chars: int = VOICE_SAFE_MAX_CHARS) -> str:
    s = _clean(text)
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    sentences = re.split(r"(?<=[。！？!?；;…])", s)
    acc = ""
    for sent in sentences:
        if not sent:
            continue
        if len(acc) + len(sent) <= max_chars:
            acc += sent
        else:
            break
    if len(acc.strip()) >= 12:
        return acc.strip()
    return _extract_first_sentence(s, max_len=max_chars)


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return round(max(1.0, frames / float(rate)), 2)
    except Exception:
        pass
    return round(max(1.0, len(wav_bytes) / 64000.0), 2)


def _enhance_and_cap_wav_bytes(
    wav_bytes: bytes,
    max_duration_sec: float = MAX_VOICE_DURATION_SEC,
) -> tuple[bytes, bytes, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        nchannels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        raw_frames = wf.readframes(wf.getnframes())

    if sampwidth != 2 or not raw_frames or framerate <= 0:
        return wav_bytes, b"", 24000

    samples = array.array("h")
    samples.frombytes(raw_frames)
    if nchannels == 2:
        mono = array.array(
            "h",
            (int((samples[i] + samples[i + 1]) // 2) for i in range(0, len(samples) - 1, 2)),
        )
    elif nchannels > 2:
        mono = array.array("h", (samples[i] for i in range(0, len(samples), nchannels)))
    else:
        mono = samples

    if not mono:
        return wav_bytes, b"", 24000

    max_samples = int(framerate * max_duration_sec)
    if len(mono) > max_samples:
        mono = mono[:max_samples]
        fade_len = min(int(framerate * 0.12), len(mono) // 4)
        for idx in range(fade_len):
            scale = float(fade_len - idx) / float(fade_len)
            pos = len(mono) - fade_len + idx
            mono[pos] = int(mono[pos] * scale)

    n_len = len(mono)
    mean_dc = int(sum(mono) / n_len)
    clarified = array.array("f", [0.0] * n_len)
    prev_x = 0.0
    peak = 1.0
    sum_sq = 0.0
    pre_emph = 0.18
    for i, val in enumerate(mono):
        x = float(val - mean_dc)
        y = x + pre_emph * (x - prev_x)
        prev_x = x
        clarified[i] = y
        abs_y = abs(y)
        if abs_y > peak:
            peak = abs_y
        sum_sq += y * y

    rms = math.sqrt(sum_sq / float(n_len)) if n_len > 0 else 1.0
    target_peak = 28500.0
    target_rms = 5400.0
    gain = min(target_peak / max(peak, 1.0), target_rms / max(rms, 1.0), 3.8)
    if gain < 1.15 and peak < 24000.0:
        gain = min(26500.0 / max(peak, 1.0), 2.4)

    out_mono = array.array("h", [0] * n_len)
    limit_ceiling = 31800.0
    for i, y in enumerate(clarified):
        v = y * gain
        if v > 26000.0:
            v = 26000.0 + (limit_ceiling - 26000.0) * math.tanh((v - 26000.0) / 8000.0)
        elif v < -26000.0:
            v = -26000.0 - (limit_ceiling - 26000.0) * math.tanh((-v - 26000.0) / 8000.0)
        out_mono[i] = int(max(-32767, min(32767, round(v))))

    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf_out:
        wf_out.setnchannels(1)
        wf_out.setsampwidth(2)
        wf_out.setframerate(framerate)
        wf_out.writeframes(out_mono.tobytes())
    enhanced_wav = wav_buf.getvalue()

    target_silk_rate = 24000
    if framerate == target_silk_rate:
        pcm_24k = out_mono.tobytes()
    else:
        ratio = float(framerate) / float(target_silk_rate)
        out_24k_len = max(1, int(n_len / ratio))
        arr_24k = array.array("h", [0] * out_24k_len)
        max_idx = n_len - 1
        for j in range(out_24k_len):
            src_pos = j * ratio
            idx0 = int(src_pos)
            if idx0 >= max_idx:
                arr_24k[j] = out_mono[max_idx]
            else:
                frac = src_pos - idx0
                s0 = out_mono[idx0]
                s1 = out_mono[idx0 + 1]
                arr_24k[j] = int(s0 + frac * (s1 - s0))
        pcm_24k = arr_24k.tobytes()

    return enhanced_wav, pcm_24k, target_silk_rate


def _wav_bytes_to_silk_bytes(wav_bytes: bytes, pcm_24k_bytes: bytes = b"", silk_rate: int = 24000) -> Optional[bytes]:
    if not wav_bytes or len(wav_bytes) < 64:
        return None
    try:
        import pilk
    except ImportError:
        return None

    pcm_path = ""
    silk_path = ""
    try:
        if not pcm_24k_bytes:
            _, pcm_24k_bytes, silk_rate = _enhance_and_cap_wav_bytes(wav_bytes)
        if not pcm_24k_bytes:
            return None

        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as fp_pcm:
            fp_pcm.write(pcm_24k_bytes)
            pcm_path = fp_pcm.name
        silk_path = pcm_path + ".silk"

        pilk.encode(pcm_path, silk_path, pcm_rate=silk_rate, tencent=True)
        p_silk = Path(silk_path)
        if p_silk.is_file() and p_silk.stat().st_size > 10:
            return p_silk.read_bytes()
    except Exception as exc:
        logger.warning(f"WAV 转 SILK v3 失败，回退原始 WAV: {exc}")
    finally:
        for p in (pcm_path, silk_path):
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
    return None


def _save_and_convert_audio(
    wav_bytes: bytes,
    text: str,
    role_name: str,
    delivery_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        enhanced_wav, pcm_24k, silk_rate = _enhance_and_cap_wav_bytes(wav_bytes, max_duration_sec=MAX_VOICE_DURATION_SEC)
    except Exception as exc:
        raise TTSGenerationError("音频格式校验或处理失败。") from exc

    ts_str = time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1_000_000_000:09d}"
    role_slug = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5]", "", role_name or "voice") or "voice"
    wav_filename = f"tts_{ts_str}_{role_slug}.wav"
    wav_path = OUTPUT_DIR / wav_filename
    latest_wav = OUTPUT_DIR / "latest.wav"
    outputs_latest_wav = OUTPUTS_DIR / "latest.wav"

    wav_path.write_bytes(enhanced_wav)
    latest_wav.write_bytes(enhanced_wav)
    try:
        outputs_latest_wav.write_bytes(enhanced_wav)
    except Exception:
        pass

    duration_sec = min(MAX_VOICE_DURATION_SEC, _wav_duration_seconds(enhanced_wav))
    silk_path = OUTPUT_DIR / f"tts_{ts_str}_{role_slug}.silk"
    latest_silk = OUTPUT_DIR / "latest.silk"
    silk_bytes: Optional[bytes] = _wav_bytes_to_silk_bytes(enhanced_wav, pcm_24k, silk_rate)

    if silk_bytes:
        silk_path.write_bytes(silk_bytes)
        latest_silk.write_bytes(silk_bytes)
        logger.info(
            f"已预压制 24kHz 高清 SILK 语音条: {silk_path.name} ({len(silk_bytes)} bytes, duration={duration_sec}s)"
        )
    elif NAPCAT_ADDON_PATH.is_file():
        node_bins = [
            shutil.which("node"),
            "/opt/homebrew/bin/node",
            "/usr/local/bin/node",
        ]
        node_bin = next((b for b in node_bins if b and Path(b).is_file()), None)
        if node_bin:
            js_code = (
                "const { createRequire } = require('module');"
                f"const addon = require({json.dumps(str(NAPCAT_ADDON_PATH.resolve()))});"
                f"Promise.resolve(addon.convertToNTSilkTct({json.dumps(str(wav_path.resolve()))}, {json.dumps(str(silk_path.resolve()))}))"
                ".then(() => process.exit(0)).catch(e => { console.error(e); process.exit(1); });"
            )
            try:
                subprocess.run(
                    [node_bin, "-e", js_code],
                    capture_output=True,
                    timeout=6.0,
                    check=False,
                )
                if silk_path.is_file() and silk_path.stat().st_size > 50:
                    raw_silk = silk_path.read_bytes()
                    if b"#!SILK" in raw_silk[:16]:
                        silk_bytes = raw_silk
                        latest_silk.write_bytes(raw_silk)
            except Exception as exc:
                logger.debug(f"Node 预转码 SILK 跳过: {exc}")

    try:
        archived_wavs = sorted(OUTPUT_DIR.glob("tts_*.wav"), key=lambda p: p.stat().st_mtime)
        while len(archived_wavs) > 100:
            old_w = archived_wavs.pop(0)
            old_s = old_w.with_suffix(".silk")
            old_w.unlink(missing_ok=True)
            old_s.unlink(missing_ok=True)
    except Exception:
        pass

    meta = {
        **(delivery_meta or {}),
        "timestamp": ts_str,
        "role": role_name,
        "text": text,
        "wav_path": str(wav_path.resolve()),
        "latest_wav_path": str(latest_wav.resolve()),
        "wav_bytes": len(enhanced_wav),
        "silk_path": str(silk_path.resolve()) if silk_bytes else "",
        "silk_bytes": len(silk_bytes) if silk_bytes else 0,
        "duration_sec": duration_sec,
    }
    LAST_AUDIO_META.clear()
    LAST_AUDIO_META.update(meta)
    try:
        (OUTPUT_DIR / "latest_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    return {
        "meta": meta,
        "send_bytes": silk_bytes if silk_bytes else enhanced_wav,
        "wav_bytes": enhanced_wav,
        "is_silk": bool(silk_bytes),
    }


def _registered_roles() -> dict[str, Any]:
    data = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def list_voice_models() -> list[dict[str, Any]]:
    result = []
    for name, info in _registered_roles().items():
        ready = all(Path(_resolve_audio_path(str(info.get(k) or ""))).is_file() for k in ("gpt", "sovits", "ref_audio"))
        result.append({"id": name, "name": name, "version": info.get("version", ""), "ready": ready})
    return result


def resolve_voice_role(role_name: Optional[str] = None) -> str:
    from .config_manager import state

    aliases = {"amiya": "阿米娅", "dania": "达妮娅", "daniya": "达妮娅", "kaltsit": "凯尔希", "assistant": "阿米娅", "none": "阿米娅"}
    wanted = role_name or getattr(state, "voice_model", "auto")
    if wanted == "auto":
        wanted = state.active_character
    wanted = aliases.get(wanted, wanted)
    ready = {x["id"] for x in list_voice_models() if x["ready"]}
    if wanted not in ready:
        raise ValueError(f"角色 {wanted} 尚无可用的语音模型，请在控制台选择已安装模型")
    return wanted


def _load_role_refs(role_name: str) -> dict[str, Any]:
    return _registered_roles().get(role_name, {})


def _voice_candidates(info: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = [
        {
            "name": "default",
            "audio": info["ref_audio"],
            "text": info["prompt_text"],
            "lang": info.get("prompt_lang", "zh"),
            "desc": info.get("ref_desc", "中性日常"),
            "tags": info.get("ref_tags", []),
        }
    ]
    for ref in info.get("refs", []):
        candidates.append({**ref, "name": Path(ref["audio"]).stem})
    return [ref for ref in candidates if Path(_resolve_audio_path(ref["audio"])).is_file()]


def _fast_local_delivery_select(
    text: str,
    role_name: str,
    candidates: list[dict],
    default_ref: str,
    emotion_tag: str = "",
) -> dict[str, Any]:
    """单一真源本地情绪路由选择器（受 VOICE_EMOTION_ROUTING_ENABLED 与 state.voice_emotion_director_enabled 控制）。"""
    routing_enabled = (
        os.getenv("VOICE_EMOTION_ROUTING_ENABLED", "false").lower() in ("1", "true", "yes")
        or bool(getattr(state, "voice_emotion_director_enabled", False))
    )
    if not routing_enabled or not emotion_tag:
        return {
            "ref_name": default_ref,
            "speed": 1.0,
            "pause": 0.15,
            "decision_source": "neutral_baseline_legacy",
            "reasoning": f"保持默认基准: {default_ref}",
        }
    try:
        cid = resolve_character_id(role_name)
    except ValueError:
        cid = "amiya"
    spec = VOICE_CANONICAL_PRESETS.get(cid, VOICE_CANONICAL_PRESETS["amiya"])
    target_prefix = spec.get("tags", {}).get(emotion_tag, default_ref)
    cand_names = [str(c.get("name") or "") for c in candidates if isinstance(c, dict)]
    matched = default_ref
    if target_prefix in cand_names:
        matched = target_prefix
    else:
        for cn in cand_names:
            if cn.startswith(target_prefix):
                matched = cn
                break
    return {
        "ref_name": matched,
        "speed": 1.0,
        "pause": 0.18,
        "decision_source": "canonical_emotion_preset",
        "reasoning": f"命中单一真源情绪预设 [{emotion_tag}] -> {matched}",
    }


async def _select_voice_delivery(
    text: str,
    role_name: str,
    info: dict[str, Any],
    force_ref_name: str = "",
    emotion_tag: str = "",
) -> dict[str, Any]:
    candidates = _voice_candidates(info)
    by_name = {ref["name"]: ref for ref in candidates}
    neutral = str(info.get("neutral_ref") or "default")
    if neutral not in by_name:
        neutral = "default"
    if not by_name or neutral not in by_name:
        raise ValueError(f"{role_name} 缺少有效参考音频")
    baseline = info.get("inference", {})
    decision = {
        "ref_name": neutral,
        "speed": 1.0,
        "temperature": min(0.82, float(getattr(state, "voice_temperature", 0.78) or 0.78)),
        "top_p": min(0.88, float(getattr(state, "voice_top_p", 0.85) or 0.85)),
        "pause": baseline.get("fragment_interval", 0.2),
        "decision_source": "neutral_baseline",
        "reasoning": "清晰饱满日常基准声线",
    }
    manual_ref = (force_ref_name or "").strip()
    if manual_ref:
        if manual_ref not in by_name:
            raise ValueError(f"{role_name} 不存在参考音频 {manual_ref}")
        decision.update(ref_name=manual_ref, decision_source="manual_preview", reasoning="用户指定试听参考")
    elif emotion_tag:
        local_sel = _fast_local_delivery_select(text, role_name, candidates, neutral, emotion_tag=emotion_tag)
        if local_sel.get("ref_name") in by_name:
            decision.update(local_sel)
    elif not getattr(state, "voice_emotion_director_enabled", False):
        decision.update(
            ref_name=neutral,
            decision_source="fast_local_rule",
            reasoning="本地快速规则基准声线",
        )
    else:
        selected = await select_delivery(
            text, str(info.get("voice_style") or "结合台词内容自然选择情绪与语调，吐字清楚。"),
            role_name, candidates,
        )
        decision.update(selected)

    def bounded(key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(decision.get(key, default))
            return min(high, max(low, value)) if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default

    ref = by_name[decision["ref_name"]]
    params = {
        "ref_audio_path": _resolve_audio_path(ref["audio"]),
        "prompt_text": ref["text"],
        "prompt_lang": ref.get("lang", info.get("prompt_lang", "zh")),
        "aux_ref_audio_paths": [],
        "speed_factor": round(min(1.25, max(0.88, state.voice_speed * bounded("speed", 1.0, 0.95, 1.12))), 2),
        "temperature": round(bounded("temperature", 0.78, 0.68, 0.85), 2),
        "top_p": round(bounded("top_p", 0.85, 0.75, 0.88), 2),
        "fragment_interval": round(bounded("pause", 0.2, 0.16, 0.30), 2),
    }
    source = str(decision.get("decision_source") or "neutral_baseline")
    polished = _clean(str(decision.get("polished_text") or text))
    if len(polished) > MAX_CHARS or re.sub(r"[\W_]", "", polished) != re.sub(r"[\W_]", "", text):
        polished = text
    meta = {
        "voice_model": role_name,
        "reference": ref["name"],
        "reference_path": params["ref_audio_path"],
        "emotion_source": source,
        "emotion_reason": str(decision.get("reasoning") or "")[:160],
        "speed_factor": params["speed_factor"],
        "temperature": params["temperature"],
        "top_p": params["top_p"],
    }
    logger.info(
        f"声音导演: model={role_name}, source={source}, ref={ref['name']}, "
        f"speed={params['speed_factor']}, temp={params['temperature']}, top_p={params['top_p']}"
    )
    return {"params": params, "meta": meta, "text": polished}


def list_voice_refs(role_name: Optional[str] = None) -> list[dict[str, Any]]:
    try:
        role = resolve_voice_role(role_name)
        refs = _voice_candidates(_load_role_refs(role))
        return [
            {
                "id": r["name"],
                "name": r["name"],
                "character": role,
                "label": str(r.get("desc") or r["name"]),
                "text": r["text"],
                "prompt_text": r["text"],
                "prompt_lang": r.get("lang", "zh"),
                "exists": True,
                "is_default": r["name"] == "default",
            }
            for r in refs
        ]
    except (ValueError, OSError, json.JSONDecodeError):
        return []


async def check_voice_services(force_refresh: bool = False) -> dict[str, bool]:
    """检测 9880 (GPT-SoVITS API) 与 9876 (Voice Studio Pro) 是否在线（带 5.0 秒 TTL 内存缓存）。"""
    now = time.monotonic()
    if not force_refresh and now < float(_VOICE_CHECK_CACHE.get("expires_at", 0.0)):
        return dict(_VOICE_CHECK_CACHE["res"])
    tts_ok = False
    studio_ok = False
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1.2) as client:
            try:
                r1 = await client.get("http://127.0.0.1:9880/docs")
                tts_ok = r1.status_code < 500
            except Exception:
                tts_ok = False
            try:
                r2 = await client.get("http://127.0.0.1:9876/")
                studio_ok = r2.status_code < 500
            except Exception:
                studio_ok = False
    except Exception:
        pass
    res = {"tts_9880_online": tts_ok, "studio_9876_online": studio_ok}
    _VOICE_CHECK_CACHE["ts"] = now
    _VOICE_CHECK_CACHE["expires_at"] = now + 5.0
    _VOICE_CHECK_CACHE["res"] = res
    return dict(res)


def _report_tts_progress(
    stage: str,
    percent: int,
    *,
    input_text: str = "",
    prompt_text: str = "",
    output_text: str = "",
    extra_info: str = "",
    status: str = "running",
) -> None:
    task_id = current_task_id.get()
    if task_id:
        recorder.progress_tracker.update(
            task_id, kind="voice", stage=stage, percent=min(percent, 95),
            status="running" if status == "done" else status,
            input_text=input_text, prompt_text=prompt_text,
            output_text=output_text, extra_info=extra_info,
        )
    recorder.log(f"[TTS {stage}] {extra_info}")


async def synthesize(
    text: str,
    role_name: Optional[str] = None,
    *,
    timeout: float = TTS_DEFAULT_TIMEOUT,
    truncate_long: bool = True,
    force_ref_name: str = "",
    emotion_tag: str = "",
    prefer_wav: bool = False,
    latency_mode: bool = False,
) -> str:
    """整个语音阶段共用一个预算，包括排锁、AI 情绪选择、请求、归档与编码。"""
    global _failures, _open_until
    if time.monotonic() < _open_until:
        raise TTSGenerationError("语音熔断保护中，本轮语音未生成。")
    started = time.perf_counter()
    try:
        eff_timeout = TTS_DEFAULT_TIMEOUT if float(timeout) == 8.0 else max(0.01, float(timeout))
        async with asyncio.timeout(eff_timeout):
            async with _TTS_MPS_LOCK:
                lock_acquired = time.perf_counter()
                role = resolve_voice_role(role_name)
                cleaned = _smart_trim_for_tts(text, VOICE_SAFE_MAX_CHARS if truncate_long else MAX_CHARS)
                if not cleaned or not re.search(r"[\w\u4e00-\u9fff]", cleaned):
                    raise TTSGenerationError("没有可播放的语音文本。")
                info = _load_role_refs(role)
                delivery = await _select_voice_delivery(cleaned, role, info, force_ref_name, emotion_tag=emotion_tag)
                delivery_selected = time.perf_counter()
                cleaned = _smart_trim_for_tts(delivery["text"], VOICE_SAFE_MAX_CHARS if truncate_long else MAX_CHARS)
                if not cleaned:
                    raise TTSGenerationError("声音导演返回了空台词。")
                params = delivery["params"]
                use_cut0 = latency_mode or (
                    os.getenv("VOICE_ADAPTIVE_CUT0_ENABLED", "false").lower() in ("1", "true", "yes")
                    and len(cleaned) <= 22
                )
                role_inference = info.get("inference", {})
                payload = {
                    "text": cleaned,
                    "text_lang": "zh",
                    **params,
                    "voice_role": role,
                    "media_type": "wav",
                    "text_split_method": "cut0" if use_cut0 else os.getenv("TTS_SPLIT", "cut5"),
                    # MPS 下多句并行会争抢 GPU，实测串行比并行快 2~7 倍，必须保持关闭。
                    "parallel_infer": False,
                    "top_k": min(8, int(role_inference.get("top_k", 12))) if latency_mode else int(role_inference.get("top_k", 12)),
                    "sample_steps": min(4, int(role_inference.get("sample_steps", 8))) if latency_mode else min(10, int(role_inference.get("sample_steps", 8))),
                    "seed": -1,
                    "repetition_penalty": float(info.get("inference", {}).get("repetition_penalty", 1.35)),
                }
                _report_tts_progress("正在合成语音", 45, input_text=cleaned)
                async with httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(eff_timeout, connect=min(2.0, eff_timeout))) as client:
                    response = await client.post(TTS_URL, json=payload)
                    response.raise_for_status()
                audio_received = time.perf_counter()
                if len(response.content) < 44 or not response.content.startswith(b"RIFF") or response.content[8:12] != b"WAVE":
                    raise TTSGenerationError("语音服务未返回有效 WAV。")
                saved = await asyncio.to_thread(_save_and_convert_audio, response.content, cleaned, role, delivery["meta"])
                logger.info(
                    f"TTS阶段耗时 role={role} lock={lock_acquired-started:.3f}s "
                    f"director={delivery_selected-lock_acquired:.3f}s infer={audio_received-delivery_selected:.3f}s "
                    f"encode={time.perf_counter()-audio_received:.3f}s total={time.perf_counter()-started:.3f}s"
                )
                if not prefer_wav and (not saved["is_silk"] or not saved["send_bytes"].startswith((b"\x02#!SILK_V3", b"#!SILK_V3"))):
                    raise TTSGenerationError("SILK 编码失败，语音未能发送。")
                result = saved["wav_bytes"] if prefer_wav else saved["send_bytes"]
                if not result:
                    raise TTSGenerationError("音频载荷为空。")
                _failures = 0
                _report_tts_progress("音频已生成，等待投递", 90, output_text=saved["meta"]["wav_path"])
                return "base64://" + base64.b64encode(result).decode("ascii")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _failures += 1
        if _failures >= TTS_CIRCUIT_FAILURES:
            _open_until = time.monotonic() + TTS_CIRCUIT_COOLDOWN
        logger.warning(f"Degradation stage=tts type={type(exc).__name__} failures={_failures}")
        if isinstance(exc, TTSGenerationError):
            raise
        raise TTSGenerationError() from exc
