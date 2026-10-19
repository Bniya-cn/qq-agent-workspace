"""运行时 config.json 原子读写（Task2）。"""
from __future__ import annotations

import asyncio
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

# src/plugins/agent_core/config_manager.py -> parents[3] == bot/
BOT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = BOT_ROOT / "data" / "config.json"


def ensure_bot_env() -> None:
    """把 bot/.env 与 .env.{ENVIRONMENT} 灌进 os.environ（自定义钥匙不被 NoneBot 默认导出）。"""
    import os
    from dotenv import load_dotenv

    env = (os.getenv("ENVIRONMENT") or "dev").strip() or "dev"
    load_dotenv(BOT_ROOT / ".env", override=True)
    load_dotenv(BOT_ROOT / f".env.{env}", override=True)

DEFAULT_CONFIG: dict[str, Any] = {
    "global_active": False,
    "superuser_qq": "1785381042",
    "active_groups": [],
    "active_character": "none",
    "comfort_mode_targets": [],
    "custom_prompts": {},
    "voice_enabled": False,
    "voice_only_reply": False,
    "voice_mode": "emotion_auto",  # "emotion_auto" | "fixed_ref"
    "voice_ref_name": "default",
    "voice_model": "auto",
    "voice_speed": 1.0,
    "voice_temperature": 0.9,
    "voice_top_p": 0.95,
    "small_talk_mode": False,
    "small_talk_style": "pure_persona",  # "pure_persona" (纯角色5-20字) | "group_mimic" (群友接梗)
    "auto_reply_mode": True,  # 默认回复模式（免触发词自动回复）
    "reply_interval_seconds": 3.0,  # 回复防刷屏冷却秒数
    "training_mode": False,
    "training_groups": [],
    "group_user_whitelist_enabled": False,
    "group_user_whitelists": {},
}

ALLOWED_CHARACTERS = frozenset({"none", "amiya", "kaltsit", "dania", "assistant"})


class ConfigManager:
    """线程/协程安全的配置状态；属性读写 + get_state()/save()。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CONFIG_PATH
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = deepcopy(DEFAULT_CONFIG)
        self._load_sync()

    # ---- 属性读写 ----
    @property
    def global_active(self) -> bool:
        return bool(self._data.get("global_active", False))

    @global_active.setter
    def global_active(self, value: bool) -> None:
        self._data["global_active"] = bool(value)

    @property
    def superuser_qq(self) -> str:
        return str(self._data.get("superuser_qq", ""))

    @superuser_qq.setter
    def superuser_qq(self, value: str) -> None:
        self._data["superuser_qq"] = str(value)

    @property
    def active_groups(self) -> list:
        return list(self._data.get("active_groups") or [])

    @active_groups.setter
    def active_groups(self, value: list) -> None:
        self._data["active_groups"] = list(value)

    @property
    def active_character(self) -> str:
        return str(self._data.get("active_character", "none"))

    @active_character.setter
    def active_character(self, value: str) -> None:
        self._data["active_character"] = str(value)

    @property
    def comfort_mode_targets(self) -> list:
        return list(self._data.get("comfort_mode_targets") or [])

    @comfort_mode_targets.setter
    def comfort_mode_targets(self, value: list) -> None:
        self._data["comfort_mode_targets"] = list(value)

    @property
    def custom_prompts(self) -> dict:
        return dict(self._data.get("custom_prompts") or {})

    @custom_prompts.setter
    def custom_prompts(self, value: dict) -> None:
        self._data["custom_prompts"] = dict(value)

    @property
    def voice_enabled(self) -> bool:
        return bool(self._data.get("voice_enabled", False))

    @voice_enabled.setter
    def voice_enabled(self, value: bool) -> None:
        self._data["voice_enabled"] = bool(value)

    @property
    def voice_only_reply(self) -> bool:
        return bool(self._data.get("voice_only_reply", False))

    @voice_only_reply.setter
    def voice_only_reply(self, value: bool) -> None:
        self._data["voice_only_reply"] = bool(value)

    @property
    def voice_mode(self) -> str:
        return str(self._data.get("voice_mode") or "emotion_auto")

    @voice_mode.setter
    def voice_mode(self, value: str) -> None:
        self._data["voice_mode"] = str(value)

    @property
    def voice_ref_name(self) -> str:
        return str(self._data.get("voice_ref_name") or "default")

    @voice_ref_name.setter
    def voice_ref_name(self, value: str) -> None:
        self._data["voice_ref_name"] = str(value)

    @property
    def voice_model(self) -> str:
        return str(self._data.get("voice_model") or "auto")

    @voice_model.setter
    def voice_model(self, value: str) -> None:
        self._data["voice_model"] = str(value)

    @property
    def voice_speed(self) -> float:
        try:
            return float(self._data.get("voice_speed", 1.0))
        except (TypeError, ValueError):
            return 1.0

    @voice_speed.setter
    def voice_speed(self, value: float) -> None:
        self._data["voice_speed"] = round(max(0.6, min(1.6, float(value))), 2)

    @property
    def voice_temperature(self) -> float:
        try:
            return float(self._data.get("voice_temperature", 0.9))
        except (TypeError, ValueError):
            return 0.9

    @voice_temperature.setter
    def voice_temperature(self, value: float) -> None:
        self._data["voice_temperature"] = round(max(0.1, min(1.5, float(value))), 2)

    @property
    def voice_top_p(self) -> float:
        try:
            return float(self._data.get("voice_top_p", 0.95))
        except (TypeError, ValueError):
            return 0.95

    @voice_top_p.setter
    def voice_top_p(self, value: float) -> None:
        self._data["voice_top_p"] = round(max(0.1, min(1.0, float(value))), 2)

    @property
    def small_talk_mode(self) -> bool:
        return bool(self._data.get("small_talk_mode", False))

    @small_talk_mode.setter
    def small_talk_mode(self, value: bool) -> None:
        self._data["small_talk_mode"] = bool(value)

    @property
    def small_talk_style(self) -> str:
        return str(self._data.get("small_talk_style", "pure_persona"))

    @small_talk_style.setter
    def small_talk_style(self, value: str) -> None:
        val = str(value or "pure_persona").strip().lower()
        self._data["small_talk_style"] = val if val in {"pure_persona", "group_mimic"} else "pure_persona"

    @property
    def auto_reply_mode(self) -> bool:
        return bool(self._data.get("auto_reply_mode", True))

    @auto_reply_mode.setter
    def auto_reply_mode(self, value: bool) -> None:
        self._data["auto_reply_mode"] = bool(value)

    @property
    def reply_interval_seconds(self) -> float:
        try:
            return float(self._data.get("reply_interval_seconds", 3.0))
        except (TypeError, ValueError):
            return 3.0

    @reply_interval_seconds.setter
    def reply_interval_seconds(self, value: float) -> None:
        self._data["reply_interval_seconds"] = max(0.5, min(30.0, float(value)))

    @property
    def training_mode(self) -> bool:
        return bool(self._data.get("training_mode", False))

    @training_mode.setter
    def training_mode(self, value: bool) -> None:
        self._data["training_mode"] = bool(value)

    @property
    def training_groups(self) -> list:
        return [str(g) for g in (self._data.get("training_groups") or [])]

    @training_groups.setter
    def training_groups(self, value: list) -> None:
        self._data["training_groups"] = [str(g) for g in (value or [])]

    @property
    def group_user_whitelist_enabled(self) -> bool:
        return bool(self._data.get("group_user_whitelist_enabled", False))

    @group_user_whitelist_enabled.setter
    def group_user_whitelist_enabled(self, value: bool) -> None:
        self._data["group_user_whitelist_enabled"] = bool(value)

    @property
    def group_user_whitelists(self) -> dict[str, list[str]]:
        val = self._data.get("group_user_whitelists")
        if isinstance(val, dict):
            return {str(k): [str(u) for u in v] for k, v in val.items() if isinstance(v, list)}
        return {}

    @group_user_whitelists.setter
    def group_user_whitelists(self, value: dict[str, list[str]]) -> None:
        if isinstance(value, dict):
            self._data["group_user_whitelists"] = {
                str(k): [str(u) for u in v] for k, v in value.items() if isinstance(v, list)
            }
        else:
            self._data["group_user_whitelists"] = {}

    def get_state(self) -> dict[str, Any]:
        """返回当前配置深拷贝。"""
        return deepcopy(self._data)

    def _merge_defaults(self, raw: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(DEFAULT_CONFIG)
        for key, val in raw.items():
            merged[key] = val
        for key, default in DEFAULT_CONFIG.items():
            if key not in raw:
                merged[key] = deepcopy(default)
        return merged

    def _load_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._data = deepcopy(DEFAULT_CONFIG)
            self._write_atomic(self._data)
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except (json.JSONDecodeError, OSError):
            raw = {}
        self._data = self._merge_defaults(raw)
        # 仅补缺省键时回写，不覆盖已有值
        missing = [k for k in DEFAULT_CONFIG if k not in raw]
        if missing:
            self._write_atomic(self._data)

    def _write_atomic(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=".config_",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
            tmp_path.replace(self._path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    async def save(self) -> None:
        async with self._lock:
            self._write_atomic(self._data)

    async def reload(self) -> dict[str, Any]:
        async with self._lock:
            self._load_sync()
            return self.get_state()


# 模块级单例，供 controller_api / 后续中间件共用
state = ConfigManager()
