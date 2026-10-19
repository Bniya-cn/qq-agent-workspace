"""GPT-SoVITS api_v2 语音合成服务（声学 DSP 与 SILK 编码已分包至 audio_dsp.py，支持分单元封嘴呜咽合成、单元内短时情绪起伏与原声声色锁定）。
失败抛出 TTSGenerationError，由分发入口降级为文字。
"""
from __future__ import annotations

import array
import asyncio
import base64
import json
import math
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from nonebot import logger
from .config_manager import state
from .errors import TTSGenerationError
from .history_recorder import recorder, current_task_id
from .voice_director_adapter import select_delivery
from .audio_dsp import (
    MAX_VOICE_DURATION_SEC,
    NAPCAT_ADDON_PATH,
    analyze_wav_acoustics,
    build_whimper_unit_specs,
    enhance_and_cap_wav_bytes as _enhance_and_cap_wav_bytes,
    fallback_node_silk_convert,
    is_muffled_gagged_text,
    join_whimper_units_to_wav,
    normalize_gagged_role,
    shape_whimper_unit_pcm,
    to_nasal_whimper_text,
    wav_bytes_to_silk_bytes as _wav_bytes_to_silk_bytes,
    wav_duration_seconds as _wav_duration_seconds,
)

BOT_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_ROOT = BOT_ROOT.parent
GSV_ROOT = WORKSPACE_ROOT / "gpt-sovits"
OUTPUT_DIR = GSV_ROOT / "output"
OUTPUTS_DIR = GSV_ROOT / "outputs"
ROLES_FILE = GSV_ROOT / "voice_roles.json"

TTS_URL = os.getenv("TTS_URL", "http://127.0.0.1:9880/tts")
VOICE_SAFE_MAX_CHARS = 30
MAX_CHARS = 150
SHORT_SYNC_THRESHOLD = 95
FIRST_SENTENCE_MAX = 80
TTS_DEFAULT_TIMEOUT = 600.0
TTS_CIRCUIT_FAILURES = 10
TTS_CIRCUIT_COOLDOWN = 10.0

# 每個呜咽单元独立挑选不同 seed，使第 1 单元与第 2/3 单元具有截然不同的语调起伏
_GAGGED_UNIT_SEEDS = {
    "dania": [8, 15, 16, 18, 2, 23, 31],
    "amiya": [40, 47, 17, 69, 29, 53],
}

_TTS_MPS_LOCK = asyncio.Lock()
_failures = 0
_open_until = 0.0
LAST_AUDIO_META: dict[str, Any] = {}


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
    """提取开篇第一句完整语义（不超过 max_len 字）进行快速合成。"""
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


def _smart_trim_for_tts(text: str, max_chars: int = MAX_CHARS) -> str:
    """智能截断超长文本至安全字数上限（保留完整句意，适配最长 30 秒语音）。"""
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


def _save_and_convert_audio(
    wav_bytes: bytes,
    text: str,
    role_name: str,
    delivery_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """将合成出的 WAV 做温和清晰度增强与响度归一化后落盘归档，并预转码为 24kHz SILK 格式。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    is_muffled = bool((delivery_meta or {}).get("muffled_gag")) or is_muffled_gagged_text(text)
    try:
        enhanced_wav, pcm_24k, silk_rate = _enhance_and_cap_wav_bytes(
            wav_bytes,
            max_duration_sec=MAX_VOICE_DURATION_SEC,
            muffled=is_muffled,
        )
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
            f"已预压制 24kHz 高清 SILK 语音条: {silk_path.name} ({len(silk_bytes)} bytes, duration={duration_sec}s, muffled={is_muffled})"
        )
    else:
        raw_silk = fallback_node_silk_convert(wav_path, silk_path)
        if raw_silk:
            silk_bytes = raw_silk
            latest_silk.write_bytes(raw_silk)

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
        "muffled_gag": is_muffled,
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

    aliases = {"amiya": "阿米娅", "dania": "达妮娅", "kaltsit": "凯尔希", "assistant": "阿米娅", "none": "阿米娅"}
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


def _build_role_timbre_aux_refs(role_name: str, info: dict[str, Any]) -> list[str]:
    """为封嘴模式构建锁定角色原本声色（且基频温润低气声、不触发警报高音）的辅助参考音频列表。"""
    aux_ref_paths: list[str] = []
    if role_name == "达妮娅":
        # 融合达妮娅低基频低气声的 default (F0=262Hz, ZCR=1618Hz) 与 hf_025 (F0=307Hz)，压制喘气感并锁定达妮娅声线
        for p_cand in (
            str(info.get("ref_audio") or ""),
            "models/dania/refs/hf_027.wav",
            "models/dania/refs/hf_025.wav",
            "models/dania/refs/hf_025_anchor1.wav",
        ):
            rp = _resolve_audio_path(p_cand)
            if rp and Path(rp).is_file():
                aux_ref_paths.append(rp)
    elif role_name == "阿米娅":
        # 阿米娅 v4 的 mel2/ge 取自 aux_ref_audio_paths 最后一项；使用平静温和的 default (F0=269Hz) 作为末尾锚点，彻底消除 CN_013 高音导致的 500Hz 防空警报感
        for p_cand in (
            "models/amiya/refs/CN_031.wav",
            str(info.get("ref_audio") or ""),
        ):
            rp = _resolve_audio_path(p_cand)
            if rp and Path(rp).is_file():
                aux_ref_paths.append(rp)
    return aux_ref_paths


async def _select_voice_delivery(
    text: str,
    role_name: str,
    info: dict[str, Any],
    force_ref_name: str = "",
    *,
    gagged_variant: str = "A",
) -> dict[str, Any]:
    candidates = _voice_candidates(info)
    by_name = {ref["name"]: ref for ref in candidates}
    neutral = str(info.get("neutral_ref") or "default")
    if neutral not in by_name:
        neutral = "default"
    if not by_name or neutral not in by_name:
        raise ValueError(f"{role_name} 缺少有效参考音频")
    baseline = info.get("inference", {})
    is_muffled = is_muffled_gagged_text(text)
    role_key = normalize_gagged_role(role_name)
    muffled_temp, muffled_top_p = (0.80, 0.86) if role_key == "dania" else (0.76, 0.84)
    decision = {
        "ref_name": neutral,
        "speed": 1.0,
        "temperature": muffled_temp if is_muffled else min(0.82, float(getattr(state, "voice_temperature", 0.78) or 0.78)),
        "top_p": muffled_top_p if is_muffled else min(0.88, float(getattr(state, "voice_top_p", 0.85) or 0.85)),
        "pause": 0.20 if is_muffled else baseline.get("fragment_interval", 0.2),
        "decision_source": "neutral_baseline",
        "reasoning": "分单元合成1~2s完整呜呜+换气隔断+单元内起伏" if is_muffled else "清晰饱满日常基准声线",
    }
    manual_ref = (force_ref_name or "").strip()
    if manual_ref:
        if manual_ref not in by_name:
            norm_wanted = manual_ref.replace("_", "").replace("-", "").lower()
            matched_key = next(
                (k for k in by_name if k.replace("_", "").replace("-", "").lower() == norm_wanted),
                None,
            )
            if matched_key:
                manual_ref = matched_key
            else:
                raise ValueError(f"{role_name} 不存在参考音频 {manual_ref}")
        if is_muffled and not manual_ref.startswith("gagged_"):
            gagged_pick = "gagged_2" if gagged_variant.upper() == "B" else "gagged_3"
            if gagged_pick in by_name:
                manual_ref = gagged_pick
        decision.update(ref_name=manual_ref, decision_source="manual_preview", reasoning="用户指定试听参考")
    elif is_muffled:
        wanted_ref = "gagged_2" if gagged_variant.upper() == "B" else "gagged_3"
        gagged_pick = wanted_ref if wanted_ref in by_name else next((k for k in ("gagged_3", "gagged_2", "gagged_1") if k in by_name), neutral)
        decision.update(
            ref_name=gagged_pick,
            decision_source=f"gagged_multi_unit_{gagged_variant.upper()}",
            reasoning=f"分单元呜咽合成(variant={gagged_variant.upper()}, ref={gagged_pick})+原声声色锚定",
        )
    else:
        normal_candidates = [c for c in candidates if not str(c.get("name") or "").startswith("gagged_")] or candidates
        selected = await select_delivery(
            text, str(info.get("voice_style") or "结合台词内容自然选择情绪与语调，吐字清楚。"),
            role_name, normal_candidates,
        )
        decision.update(selected)

    def bounded(key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(decision.get(key, default))
            return min(high, max(low, value)) if math.isfinite(value) else default
        except (TypeError, ValueError):
            return default

    ref = by_name[decision["ref_name"]]
    aux_ref_paths = _build_role_timbre_aux_refs(role_name, info) if is_muffled else []

    params = {
        "ref_audio_path": _resolve_audio_path(ref["audio"]),
        "prompt_text": ref["text"],
        "prompt_lang": ref.get("lang", info.get("prompt_lang", "zh")),
        "aux_ref_audio_paths": aux_ref_paths,
        "speed_factor": round(min(1.25, max(0.88, state.voice_speed * bounded("speed", 1.0, 0.95, 1.12))), 2),
        "temperature": round(bounded("temperature", muffled_temp if is_muffled else 0.78, 0.68, 0.85), 2),
        "top_p": round(bounded("top_p", muffled_top_p if is_muffled else 0.85, 0.75, 0.88), 2),
        "fragment_interval": 0.18 if is_muffled else round(bounded("pause", 0.2, 0.16, 0.30), 2),
    }
    source = str(decision.get("decision_source") or "neutral_baseline")
    if is_muffled:
        polished = to_nasal_whimper_text(text, role_name)
    else:
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
        "muffled_gag": is_muffled,
    }
    logger.info(
        f"声音导演: model={role_name}, source={source}, ref={ref['name']}, "
        f"speed={params['speed_factor']}, temp={params['temperature']}, top_p={params['top_p']}, muffled={is_muffled}"
    )
    return {"params": params, "meta": meta, "text": polished, "by_name": by_name}


async def _synthesize_gagged_multi_unit(
    client: httpx.AsyncClient,
    role: str,
    info: dict[str, Any],
    delivery: dict[str, Any],
    raw_text: str,
    *,
    gagged_variant: str = "A",
) -> tuple[bytes, dict[str, Any]]:
    """分单元合成封嘴语音：
    将整条封嘴语音拆成 2~3 个独立的 1.0~1.6s『呜咽单元』，每个单元使用不同 seed、不同声调组合与情绪语调单独合成，
    经客观基频(F0)/过零率(ZCR)筛选与单元内双峰起伏调制后，在单元之间插入 0.18~0.25s 换气隔断拼接成完整音频。
    """
    role_key = normalize_gagged_role(role)
    variant = (gagged_variant or "A").strip().upper()
    num_units = 3 if variant == "C" else 2
    unit_specs = build_whimper_unit_specs(raw_text, role=role, num_units=num_units, variant=variant)

    is_v4 = str(info.get("version") or "").lower() == "v4"
    default_steps = 6 if is_v4 else int(info.get("inference", {}).get("sample_steps", 8))
    base_params = dict(delivery["params"])
    by_name = delivery.get("by_name") or {}

    seed_pool = list(_GAGGED_UNIT_SEEDS.get(role_key, [8, 15, 16, 40, 47]))
    if variant == "B":
        # 方案 B 从不同起始 seed 采样并增强单元内音高起伏
        seed_pool = seed_pool[1:] + seed_pool[:1]
        undul_strength = 1.25
        pause_list = [0.23, 0.21]
    elif variant == "C":
        seed_pool = seed_pool[2:] + seed_pool[:2]
        undul_strength = 1.10
        pause_list = [0.19, 0.21]
    else:
        undul_strength = 1.00
        pause_list = [0.21, 0.20]

    unit_pcms: list[array.array] = []
    unit_debug_list: list[dict[str, Any]] = []
    sample_rate = 48000 if is_v4 else 32000
    seed_cursor = 0

    for u_idx, u_spec in enumerate(unit_specs):
        u_text = u_spec["text"]
        u_style = u_spec["style"]
        u_min = float(u_spec["min_sec"])
        u_max = float(u_spec["max_sec"])

        # 不同单元可切换 gagged_3 / gagged_2 参考以增加前后单元语调反差
        u_params = dict(base_params)
        if u_style == "question" and "gagged_2" in by_name and variant in {"B", "C"}:
            r2 = by_name["gagged_2"]
            u_params["ref_audio_path"] = _resolve_audio_path(r2["audio"])
            u_params["prompt_text"] = r2["text"]

        # 单元间语速微差：抗议单元紧凑有力，委屈疑问单元略微放缓
        if role_key == "amiya":
            u_speed = 1.06 if u_style == "protest" else (0.98 if u_style == "question" else 1.08)
        else:
            u_speed = 0.98 if u_style == "protest" else (0.92 if u_style == "question" else 1.02)
        u_params["speed_factor"] = u_speed

        best_unit_pcm = array.array("h")
        best_sr = sample_rate
        best_score = float("inf")
        best_meta: dict[str, Any] = {}

        # 每个单元最多尝试 2 个不同 seed，客观筛选出基频适中（非防空警报）、过零率低（非喘气声）且时长在 0.95~1.7s 的单元
        for try_idx in range(2):
            cur_seed = seed_pool[(seed_cursor + try_idx) % len(seed_pool)]
            payload = {
                "text": u_text,
                "text_lang": "zh",
                **u_params,
                "voice_role": role,
                "media_type": "wav",
                "text_split_method": "cut0",
                "top_k": 18,
                "sample_steps": min(10, default_steps),
                "seed": cur_seed,
                "parallel_infer": False,
                "repetition_penalty": 1.22,
            }
            resp = await client.post(TTS_URL, json=payload)
            if resp.status_code >= 400 or len(resp.content) < 44 or not resp.content.startswith(b"RIFF"):
                continue

            u_pcm, u_sr, u_ac = shape_whimper_unit_pcm(
                resp.content,
                style=u_style,
                min_sec=u_min,
                max_sec=u_max,
                target_sec=float(u_spec.get("target_sec") or 1.28),
                base_f0=float(u_spec.get("base_f0") or (276.0 if role_key == "dania" else 288.0)),
                mark_offset_ratio=round((u_idx * 0.42) % 0.85, 2),
                pitch_undulation=undul_strength,
            )
            shaped_dur = u_ac.get("shaped_duration_sec", 0.0)
            raw_f0 = u_ac.get("median_f0_hz", 340.0)
            raw_zcr = u_ac.get("avg_zcr_hz", 1600.0)

            # 评分函数（越小越优）：惩罚时长过短(<0.85s)、高音警报(raw_f0>370Hz)与气声喘息(raw_zcr>1850Hz)
            dur_penalty = 0.0 if (0.90 <= shaped_dur <= u_max + 0.1) else abs(shaped_dur - 1.25) * 4.0
            f0_penalty = max(0.0, (raw_f0 - 355.0) / 40.0)
            zcr_penalty = max(0.0, (raw_zcr - 1800.0) / 300.0)
            score = dur_penalty + f0_penalty + zcr_penalty

            logger.info(
                f"封嘴单元[{u_idx + 1}/{len(unit_specs)}] 候选(seed={cur_seed}): "
                f"text='{u_text}' style={u_style} shaped_dur={shaped_dur:.2f}s raw_f0={raw_f0:.1f}Hz zcr={raw_zcr:.0f}Hz score={score:.2f}"
            )
            if len(u_pcm) > 0 and score < best_score:
                best_score = score
                best_unit_pcm = u_pcm
                best_sr = u_sr
                best_meta = {"unit": u_idx + 1, "seed": cur_seed, "text": u_text, "style": u_style, **u_ac}
            if score <= 0.65 and shaped_dur >= 0.95:
                break

        seed_cursor += 2
        if len(best_unit_pcm) > 0:
            unit_pcms.append(best_unit_pcm)
            sample_rate = best_sr
            unit_debug_list.append(best_meta)

    if not unit_pcms:
        raise TTSGenerationError("封嘴分单元合成未生成有效音频。")

    joined_wav = join_whimper_units_to_wav(unit_pcms, sample_rate, pause_durations=pause_list)
    extra_meta = {
        "gagged_variant": variant,
        "unit_count": len(unit_pcms),
        "pause_durations_sec": pause_list[: max(0, len(unit_pcms) - 1)],
        "units": unit_debug_list,
    }
    return joined_wav, extra_meta


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


async def check_voice_services() -> dict[str, bool]:
    """检测 9880 (GPT-SoVITS API) 与 9876 (Voice Studio Pro) 是否在线。"""
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
    return {"tts_9880_online": tts_ok, "studio_9876_online": studio_ok}


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
    prefer_wav: bool = False,
    gagged_variant: str = "A",
) -> str:
    """整个语音阶段共用一个预算，包括排锁、AI 情绪选择、分单元封嘴合成（或常规合成）、归档与编码。"""
    global _failures, _open_until
    if time.monotonic() < _open_until:
        raise TTSGenerationError("语音熔断保护中，改用文字。")
    try:
        raw_to = float(timeout if timeout is not None else TTS_DEFAULT_TIMEOUT)
        eff_timeout = raw_to if (math.isfinite(raw_to) and raw_to > 0) else TTS_DEFAULT_TIMEOUT
        async with asyncio.timeout(eff_timeout):
            async with _TTS_MPS_LOCK:
                role = resolve_voice_role(role_name)
                cleaned = _smart_trim_for_tts(text, VOICE_SAFE_MAX_CHARS if truncate_long else MAX_CHARS)
                if not cleaned or not re.search(r"[\w\u4e00-\u9fff]", cleaned):
                    raise TTSGenerationError("没有可播放的语音文本。")
                is_muffled_in = is_muffled_gagged_text(cleaned)
                if is_muffled_in:
                    cleaned = to_nasal_whimper_text(cleaned, role)
                info = _load_role_refs(role)
                delivery = await _select_voice_delivery(
                    cleaned, role, info, force_ref_name, gagged_variant=gagged_variant
                )
                cleaned = _smart_trim_for_tts(delivery["text"], VOICE_SAFE_MAX_CHARS if truncate_long else MAX_CHARS)
                if not cleaned:
                    raise TTSGenerationError("声音导演返回了空台词。")
                params = delivery["params"]
                is_v4 = str(info.get("version") or "").lower() == "v4"
                is_muffled_now = bool(delivery["meta"].get("muffled_gag")) or is_muffled_in or is_muffled_gagged_text(cleaned)
                default_steps = 4 if is_v4 else int(info.get("inference", {}).get("sample_steps", 8))

                _report_tts_progress("正在合成语音", 45, input_text=cleaned)
                async with httpx.AsyncClient(
                    trust_env=False,
                    timeout=httpx.Timeout(eff_timeout, connect=min(2.0, eff_timeout)),
                ) as client:
                    if is_muffled_now:
                        best_content, gagged_extra = await _synthesize_gagged_multi_unit(
                            client, role, info, delivery, text, gagged_variant=gagged_variant
                        )
                        delivery["meta"].update(gagged_extra)
                    else:
                        payload = {
                            "text": cleaned,
                            "text_lang": "zh",
                            **params,
                            "voice_role": role,
                            "media_type": "wav",
                            "text_split_method": os.getenv("TTS_SPLIT", "cut2"),
                            "top_k": int(info.get("inference", {}).get("top_k", 12)),
                            "sample_steps": min(10, default_steps),
                            "seed": -1,
                            "repetition_penalty": float(info.get("inference", {}).get("repetition_penalty", 1.35)),
                        }
                        response = await client.post(TTS_URL, json=payload)
                        if response.status_code == 400 and re.search(r"[a-zA-Z]", cleaned):
                            fallback_clean = re.sub(r"[a-zA-Z0-9]+", "", cleaned).strip("，, ")
                            if fallback_clean and re.search(r"[\u4e00-\u9fff]", fallback_clean):
                                logger.warning(f"TTS 400 异常，检测到混合英文，尝试纯中文降级重试: {fallback_clean[:30]}")
                                payload["text"] = fallback_clean
                                response = await client.post(TTS_URL, json=payload)
                        if response.status_code >= 400:
                            err_detail = response.text[:200]
                            logger.error(f"TTS 9880 接口异常 HTTP {response.status_code}: {err_detail}")
                            raise TTSGenerationError(f"TTS 9880 接口返回错误 HTTP {response.status_code}: {err_detail}")
                        response.raise_for_status()
                        best_content = response.content

                if len(best_content) < 44 or not best_content.startswith(b"RIFF") or best_content[8:12] != b"WAVE":
                    raise TTSGenerationError("语音服务未返回有效 WAV。")
                saved = await asyncio.to_thread(_save_and_convert_audio, best_content, cleaned, role, delivery["meta"])
                if not prefer_wav and (not saved["is_silk"] or not saved["send_bytes"].startswith((b"\x02#!SILK_V3", b"#!SILK_V3"))):
                    raise TTSGenerationError("SILK 编码失败，改用文字。")
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
        logger.warning(f"Degradation stage=tts type={type(exc).__name__} failures={_failures} detail={exc}")
        if isinstance(exc, TTSGenerationError):
            raise
        raise TTSGenerationError(f"TTS合成异常: {type(exc).__name__} - {exc}") from exc
