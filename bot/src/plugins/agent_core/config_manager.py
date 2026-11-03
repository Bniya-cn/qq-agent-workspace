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
    "accept_poke": True,
    "superuser_qq": "1785381042",
    "active_groups": [],
    "active_character": "none",
    "comfort_mode_targets": [],
    "custom_prompts": {},
    "voice_enabled": False,
    "voice_only_reply": False,
    "voice_mode": "emotion_auto",  # "emotion_auto" | "fixed_ref"
    "voice_emotion_director_enabled": False,
    "voice_ref_name": "default",
    "voice_model": "auto",
    "voice_speed": 1.0,
    "voice_temperature": 0.9,
    "voice_top_p": 0.95,
    "amiya_sticker_enabled": True,
    "amiya_sticker_min_delay_sec": 1.0,
    "amiya_sticker_max_delay_sec": 20.0,
    "small_talk_mode": False,
    "small_talk_style": "pure_persona",  # "pure_persona" (纯角色) | "group_mimic" (借用群表达风格，固定角色优先)
    "auto_reply_mode": True,  # 默认回复模式（免触发词自动回复）
    "reply_interval_seconds": 1.5,  # 回复防刷屏冷却秒数
    "training_mode": False,
    "training_groups": [],
    "group_user_whitelist_enabled": False,
    "group_user_whitelists": {},
    "fast_reply_mode": True,  # 极速云端回复模式（本地缓存 + 预热连接池直连极速模型）
    "vector_engine_mode": "local",  # "local" | "dashscope" | "hybrid"
    "group_quote_reply": True,  # 群聊多人连续发言自动携带引用回复气泡
    "natural_persona_mode": True,  # 启用自然鲜活少女群聊口吻
    "chat_1v1_enabled": True,  # 1v1 独立模式按钮
    "group_buffer_enabled": True,  # 群聊合并模式按钮
    "parallel_sync_async_enabled": False,  # 兼容旧字段；同 QQ 仍使用整轮串行锁
    "group_buffer_window_ms": 1500,  # 群聊合并窗口毫秒数（默认 1500ms = 1.5s）
    "group_merge_window_sec": 1.5,  # 群聊合并窗口秒数（默认 1.5 秒）
    "group_merge_max_messages": 5,  # 单批最多合并消息数（默认 5 条）
    "group_merge_max_chars": 2000,  # 单批最多合并字符数（默认 2000 字）
    "group_quota_min_interval_sec": 8.0,  # 普通主动参与最小发送间隔（默认每 8 秒最多 1 条）
    "group_quota_max_per_minute": 4,  # 普通主动参与每分钟发送上限（默认每分钟最多 4 条）
    "group_chunk_pace_sec": 1.2,  # 长回答分段发送气泡节奏间隔（秒）
    "autonomous_group_chat_enabled": False,  # 自主参与/发起群聊开关
    "autonomous_qzone_enabled": False,  # 自主发布 QQ 空间开关
    "autonomous_qzone_max_per_day": 2,  # 自主空间每日上限（默认 2 条）
    "autonomous_qzone_min_interval_hours": 6.0,  # 自主空间最小间隔小时数（默认 6 小时）
    "autonomous_quiet_start_hour": 23,  # 安静时段开始小时（默认 23:00）
    "autonomous_quiet_end_hour": 8,  # 安静时段结束小时（默认 08:00）
    "daily_9am_enabled": False,
    "daily_schedule_armed": False,  # 每日早上 9:00 定时任务总开关
    "daily_qzone_enabled": True,  # 每日早上 9:00 自动发 QQ 空间开工动态
    "daily_morning_group_enabled": True,  # 每日早上 9:00 自动在白名单群发送早安语录
    "daily_schedule_time": "09:00",  # 每日定时触发时间 (HH:MM)
    "image_gen_route": "qwen",  # "qwen" (阿里百炼 Qwen 生图 API) | "friend_comfy" (朋友的 ComfyUI 模型)
    "friend_comfy_workflow": "sdxl",  # "sdxl" (novaAnimeXL + IPAdapter) | "krea2" (Krea 2 文生图) | "auto"
    "friend_comfy_url": "http://node.tail3189e0.ts.net:8100",
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
    def accept_poke(self) -> bool:
        return bool(self._data.get("accept_poke", True))

    @accept_poke.setter
    def accept_poke(self, value: bool) -> None:
        self._data["accept_poke"] = bool(value)

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
    def voice_emotion_director_enabled(self) -> bool:
        return bool(self._data.get("voice_emotion_director_enabled", False))

    @voice_emotion_director_enabled.setter
    def voice_emotion_director_enabled(self, value: bool) -> None:
        self._data["voice_emotion_director_enabled"] = bool(value)

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
    def voice_temperature(self, value: bool | float) -> None:
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
    def amiya_sticker_enabled(self) -> bool:
        return bool(self._data.get("amiya_sticker_enabled", True))

    @amiya_sticker_enabled.setter
    def amiya_sticker_enabled(self, value: bool) -> None:
        self._data["amiya_sticker_enabled"] = bool(value)

    @property
    def amiya_sticker_min_delay_sec(self) -> float:
        try:
            return max(1.0, min(20.0, float(self._data.get("amiya_sticker_min_delay_sec", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    @amiya_sticker_min_delay_sec.setter
    def amiya_sticker_min_delay_sec(self, value: float) -> None:
        self._data["amiya_sticker_min_delay_sec"] = round(max(1.0, min(20.0, float(value))), 2)

    @property
    def amiya_sticker_max_delay_sec(self) -> float:
        try:
            return max(1.0, min(20.0, float(self._data.get("amiya_sticker_max_delay_sec", 20.0))))
        except (TypeError, ValueError):
            return 20.0

    @amiya_sticker_max_delay_sec.setter
    def amiya_sticker_max_delay_sec(self, value: float) -> None:
        self._data["amiya_sticker_max_delay_sec"] = round(max(1.0, min(20.0, float(value))), 2)

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
            return float(self._data.get("reply_interval_seconds", 1.5))
        except (TypeError, ValueError):
            return 1.5

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

    @property
    def fast_reply_mode(self) -> bool:
        return bool(self._data.get("fast_reply_mode", True))

    @fast_reply_mode.setter
    def fast_reply_mode(self, value: bool) -> None:
        self._data["fast_reply_mode"] = bool(value)

    @property
    def vector_engine_mode(self) -> str:
        val = str(self._data.get("vector_engine_mode", "local")).strip().lower()
        return val if val in {"local", "dashscope", "hybrid"} else "local"

    @vector_engine_mode.setter
    def vector_engine_mode(self, value: str) -> None:
        val = str(value or "local").strip().lower()
        self._data["vector_engine_mode"] = val if val in {"local", "dashscope", "hybrid"} else "local"

    @property
    def group_quote_reply(self) -> bool:
        return bool(self._data.get("group_quote_reply", True))

    @group_quote_reply.setter
    def group_quote_reply(self, value: bool) -> None:
        self._data["group_quote_reply"] = bool(value)

    @property
    def natural_persona_mode(self) -> bool:
        return bool(self._data.get("natural_persona_mode", True))

    @natural_persona_mode.setter
    def natural_persona_mode(self, value: bool) -> None:
        self._data["natural_persona_mode"] = bool(value)

    @property
    def chat_1v1_enabled(self) -> bool:
        return bool(self._data.get("chat_1v1_enabled", True))

    @chat_1v1_enabled.setter
    def chat_1v1_enabled(self, value: bool) -> None:
        self._data["chat_1v1_enabled"] = bool(value)

    @property
    def group_buffer_enabled(self) -> bool:
        return bool(self._data.get("group_buffer_enabled", True))

    @group_buffer_enabled.setter
    def group_buffer_enabled(self, value: bool) -> None:
        self._data["group_buffer_enabled"] = bool(value)

    @property
    def parallel_sync_async_enabled(self) -> bool:
        return bool(self._data.get("parallel_sync_async_enabled", False))

    @parallel_sync_async_enabled.setter
    def parallel_sync_async_enabled(self, value: bool) -> None:
        self._data["parallel_sync_async_enabled"] = bool(value)

    @property
    def group_buffer_window_ms(self) -> int:
        try:
            return max(100, min(5000, int(self._data.get("group_buffer_window_ms", 1500))))
        except (TypeError, ValueError):
            return 1500

    @group_buffer_window_ms.setter
    def group_buffer_window_ms(self, value: int) -> None:
        try:
            ms = max(100, min(5000, int(value)))
            self._data["group_buffer_window_ms"] = ms
            self._data["group_merge_window_sec"] = round(ms / 1000.0, 2)
        except (TypeError, ValueError):
            self._data["group_buffer_window_ms"] = 1500

    @property
    def group_merge_window_sec(self) -> float:
        try:
            return max(0.0, min(10.0, float(self._data.get("group_merge_window_sec", 1.5))))
        except (TypeError, ValueError):
            return 1.5

    @group_merge_window_sec.setter
    def group_merge_window_sec(self, value: float) -> None:
        sec = round(max(0.0, min(10.0, float(value))), 2)
        self._data["group_merge_window_sec"] = sec
        self._data["group_buffer_window_ms"] = int(sec * 1000)

    @property
    def group_merge_max_messages(self) -> int:
        try:
            return max(1, min(20, int(self._data.get("group_merge_max_messages", 5))))
        except (TypeError, ValueError):
            return 5

    @group_merge_max_messages.setter
    def group_merge_max_messages(self, value: int) -> None:
        self._data["group_merge_max_messages"] = max(1, min(20, int(value)))

    @property
    def group_merge_max_chars(self) -> int:
        try:
            return max(200, min(8000, int(self._data.get("group_merge_max_chars", 2000))))
        except (TypeError, ValueError):
            return 2000

    @group_merge_max_chars.setter
    def group_merge_max_chars(self, value: int) -> None:
        self._data["group_merge_max_chars"] = max(200, min(8000, int(value)))

    @property
    def group_quota_min_interval_sec(self) -> float:
        try:
            return max(0.5, min(120.0, float(self._data.get("group_quota_min_interval_sec", 8.0))))
        except (TypeError, ValueError):
            return 8.0

    @group_quota_min_interval_sec.setter
    def group_quota_min_interval_sec(self, value: float) -> None:
        self._data["group_quota_min_interval_sec"] = round(max(0.5, min(120.0, float(value))), 2)

    @property
    def group_quota_max_per_minute(self) -> int:
        try:
            return max(1, min(120, int(self._data.get("group_quota_max_per_minute", 4))))
        except (TypeError, ValueError):
            return 4

    @group_quota_max_per_minute.setter
    def group_quota_max_per_minute(self, value: int) -> None:
        self._data["group_quota_max_per_minute"] = max(1, min(120, int(value)))

    @property
    def group_chunk_pace_sec(self) -> float:
        try:
            return max(0.0, min(10.0, float(self._data.get("group_chunk_pace_sec", 1.2))))
        except (TypeError, ValueError):
            return 1.2

    @group_chunk_pace_sec.setter
    def group_chunk_pace_sec(self, value: float) -> None:
        self._data["group_chunk_pace_sec"] = round(max(0.0, min(10.0, float(value))), 2)

    @property
    def autonomous_group_chat_enabled(self) -> bool:
        return bool(self._data.get("autonomous_group_chat_enabled", False))

    @autonomous_group_chat_enabled.setter
    def autonomous_group_chat_enabled(self, value: bool) -> None:
        self._data["autonomous_group_chat_enabled"] = bool(value)

    @property
    def autonomous_qzone_enabled(self) -> bool:
        return bool(self._data.get("autonomous_qzone_enabled", False))

    @autonomous_qzone_enabled.setter
    def autonomous_qzone_enabled(self, value: bool) -> None:
        self._data["autonomous_qzone_enabled"] = bool(value)

    @property
    def autonomous_qzone_max_per_day(self) -> int:
        try:
            return max(0, min(10, int(self._data.get("autonomous_qzone_max_per_day", 2))))
        except (TypeError, ValueError):
            return 2

    @autonomous_qzone_max_per_day.setter
    def autonomous_qzone_max_per_day(self, value: int) -> None:
        self._data["autonomous_qzone_max_per_day"] = max(0, min(10, int(value)))

    @property
    def autonomous_qzone_min_interval_hours(self) -> float:
        try:
            return max(0.5, min(48.0, float(self._data.get("autonomous_qzone_min_interval_hours", 6.0))))
        except (TypeError, ValueError):
            return 6.0

    @autonomous_qzone_min_interval_hours.setter
    def autonomous_qzone_min_interval_hours(self, value: float) -> None:
        self._data["autonomous_qzone_min_interval_hours"] = round(max(0.5, min(48.0, float(value))), 2)

    @property
    def autonomous_quiet_start_hour(self) -> int:
        try:
            return max(0, min(23, int(self._data.get("autonomous_quiet_start_hour", 23))))
        except (TypeError, ValueError):
            return 23

    @autonomous_quiet_start_hour.setter
    def autonomous_quiet_start_hour(self, value: int) -> None:
        self._data["autonomous_quiet_start_hour"] = max(0, min(23, int(value)))

    @property
    def autonomous_quiet_end_hour(self) -> int:
        try:
            return max(0, min(23, int(self._data.get("autonomous_quiet_end_hour", 8))))
        except (TypeError, ValueError):
            return 8

    @autonomous_quiet_end_hour.setter
    def autonomous_quiet_end_hour(self, value: int) -> None:
        self._data["autonomous_quiet_end_hour"] = max(0, min(23, int(value)))

    @property
    def daily_schedule_armed(self) -> bool:
        return bool(self._data.get("daily_schedule_armed", False))

    @daily_schedule_armed.setter
    def daily_schedule_armed(self, value: bool) -> None:
        self._data["daily_schedule_armed"] = bool(value)

    @property
    def daily_9am_enabled(self) -> bool:
        return bool(self._data.get("daily_9am_enabled", True))

    @daily_9am_enabled.setter
    def daily_9am_enabled(self, value: bool) -> None:
        self._data["daily_9am_enabled"] = bool(value)

    @property
    def daily_qzone_enabled(self) -> bool:
        return bool(self._data.get("daily_qzone_enabled", True))

    @daily_qzone_enabled.setter
    def daily_qzone_enabled(self, value: bool) -> None:
        self._data["daily_qzone_enabled"] = bool(value)

    @property
    def daily_morning_group_enabled(self) -> bool:
        return bool(self._data.get("daily_morning_group_enabled", True))

    @daily_morning_group_enabled.setter
    def daily_morning_group_enabled(self, value: bool) -> None:
        self._data["daily_morning_group_enabled"] = bool(value)

    @property
    def daily_schedule_time(self) -> str:
        val = str(self._data.get("daily_schedule_time") or "09:00").strip()
        return val if len(val) == 5 and ":" in val else "09:00"

    @daily_schedule_time.setter
    def daily_schedule_time(self, value: str) -> None:
        val = str(value or "09:00").strip()
        self._data["daily_schedule_time"] = val if len(val) == 5 and ":" in val else "09:00"

    @property
    def image_gen_route(self) -> str:
        val = str(self._data.get("image_gen_route") or "qwen").strip().lower()
        if val in {"friend_comfy", "comfy", "comfyui", "friend"}:
            return "friend_comfy"
        return "qwen"

    @image_gen_route.setter
    def image_gen_route(self, value: str) -> None:
        val = str(value or "qwen").strip().lower()
        self._data["image_gen_route"] = "friend_comfy" if val in {"friend_comfy", "comfy", "comfyui", "friend"} else "qwen"

    @property
    def friend_comfy_workflow(self) -> str:
        val = str(self._data.get("friend_comfy_workflow") or "sdxl").strip().lower()
        return val if val in {"sdxl", "krea2", "auto"} else "sdxl"

    @friend_comfy_workflow.setter
    def friend_comfy_workflow(self, value: str) -> None:
        val = str(value or "sdxl").strip().lower()
        self._data["friend_comfy_workflow"] = val if val in {"sdxl", "krea2", "auto"} else "sdxl"

    @property
    def friend_comfy_url(self) -> str:
        val = str(self._data.get("friend_comfy_url") or "http://node.tail3189e0.ts.net:8100").strip().rstrip("/")
        return val or "http://node.tail3189e0.ts.net:8100"

    @friend_comfy_url.setter
    def friend_comfy_url(self, value: str) -> None:
        val = str(value or "http://node.tail3189e0.ts.net:8100").strip().rstrip("/")
        self._data["friend_comfy_url"] = val or "http://node.tail3189e0.ts.net:8100"

    def get_group_user_whitelist(self, group_id: str) -> list[str]:
        gid = str(group_id).strip()
        wl = self.group_user_whitelists
        return [str(u) for u in wl.get(gid, [])]

    async def set_group_user_whitelist(self, group_id: str, users: list[str]) -> None:
        gid = str(group_id).strip()
        wl = dict(self.group_user_whitelists)
        wl[gid] = [str(u).strip() for u in users if str(u).strip()]
        self.group_user_whitelists = wl
        await self.save()

    async def set_group_user_whitelist_enabled(self, enabled: bool) -> None:
        self.group_user_whitelist_enabled = bool(enabled)
        await self.save()

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
