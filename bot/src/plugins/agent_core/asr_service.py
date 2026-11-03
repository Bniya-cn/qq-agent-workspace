"""本机 Apple Silicon 语音识别服务。"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Optional

_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo-q4"
_ASR_LOCK = threading.Lock()
_TRANSCRIPTION_PROMPT = "阿米娅、凯尔希、达妮娅、博士、罗德岛、明日方舟。"


def _transcribe_sync(audio_path: str) -> str:
    path = Path(audio_path)
    if not path.is_file():
        raise FileNotFoundError(f"语音文件不存在: {path.name}")

    try:
        import mlx_whisper
    except ImportError as exc:
        raise RuntimeError("本地 ASR 依赖未安装，请在 bot/.venv 安装 mlx-whisper。") from exc

    language = (os.getenv("ASR_LANGUAGE") or "zh").strip().lower()
    if language in {"", "auto", "detect"}:
        language = None
    model = (os.getenv("ASR_MODEL") or _DEFAULT_MODEL).strip()
    with _ASR_LOCK:
        result = mlx_whisper.transcribe(
            str(path),
            path_or_hf_repo=model,
            language=language,
            initial_prompt=_TRANSCRIPTION_PROMPT,
            temperature=0.0,
            condition_on_previous_text=False,
            verbose=False,
        )
    return str((result or {}).get("text") or "").strip()


async def transcribe_audio(audio_path: str) -> Optional[str]:
    """在线程中运行 MLX Whisper，避免推理阻塞 NoneBot 的异步事件循环。"""
    text = await asyncio.to_thread(_transcribe_sync, audio_path)
    return text or None
