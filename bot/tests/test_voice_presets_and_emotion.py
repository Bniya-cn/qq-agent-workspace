from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot
nonebot.init()

from src.plugins.agent_core.voice_presets import (
    SAFE_FALLBACK_REPLIES,
    VOICE_CANONICAL_PRESETS,
    parse_and_strip_voice_emotion,
    resolve_character_id,
)
from src.plugins.agent_core.tts_service import (
    _VOICE_CHECK_CACHE,
    _fast_local_delivery_select,
    check_voice_services,
)
from src.plugins.agent_core import tts_service


class TestVoicePresetsAndEmotion(unittest.IsolatedAsyncioTestCase):
    """测试统一角色解析、单一真源情绪映射及状态机标签清洗器（标准 unittest 版，零第三方测试框架依赖）。"""

    def test_resolve_character_id_valid_and_case_insensitive(self):
        self.assertEqual(resolve_character_id("阿米娅"), "amiya")
        self.assertEqual(resolve_character_id("达妮娅"), "dania")
        self.assertEqual(resolve_character_id("凯尔希"), "kaltsit")
        self.assertEqual(resolve_character_id("amiya"), "amiya")
        self.assertEqual(resolve_character_id("Amiya"), "amiya")
        self.assertEqual(resolve_character_id("dania"), "dania")
        self.assertEqual(resolve_character_id("daniya"), "dania")
        self.assertEqual(resolve_character_id("Daniya"), "dania")
        self.assertEqual(resolve_character_id("kaltsit"), "kaltsit")
        self.assertEqual(resolve_character_id("Kal'tsit"), "kaltsit")
        self.assertEqual(resolve_character_id("kal'tsit"), "kaltsit")

    def test_resolve_character_id_rejects_unknown(self):
        with self.assertRaises(ValueError):
            resolve_character_id("unknown_role")
        with self.assertRaises(ValueError):
            resolve_character_id("陈")
        with self.assertRaises(ValueError):
            resolve_character_id("")

    def test_parse_and_strip_voice_emotion_basic_and_chinese_brackets(self):
        tag, text = parse_and_strip_voice_emotion("[开心]你好呀，博士！", character="amiya")
        self.assertEqual(tag, "开心")
        self.assertEqual(text, "你好呀，博士！")

        tag, text = parse_and_strip_voice_emotion("【温柔】博士，休息一下吧。", character="amiya")
        self.assertEqual(tag, "温柔")
        self.assertEqual(text, "博士，休息一下吧。")

    def test_parse_and_strip_voice_emotion_role_whitelist(self):
        tag, text = parse_and_strip_voice_emotion("[开朗]今天天气真好！", character="dania")
        self.assertEqual(tag, "开朗")
        self.assertEqual(text, "今天天气真好！")

        tag, text = parse_and_strip_voice_emotion("[调皮]略略略，抓不到我吧！", character="dania")
        self.assertEqual(tag, "调皮")
        self.assertEqual(text, "略略略，抓不到我吧！")

        tag, text = parse_and_strip_voice_emotion("[冷静]局势尚在可控范围内。", character="kaltsit")
        self.assertEqual(tag, "冷静")
        self.assertEqual(text, "局势尚在可控范围内。")

        tag, text = parse_and_strip_voice_emotion("[疲惫]今天的例会就到这里吧。", character="kaltsit")
        self.assertEqual(tag, "疲惫")
        self.assertEqual(text, "今天的例会就到这里吧。")

    def test_parse_and_strip_voice_emotion_unknown_tag_stripping(self):
        tag, text = parse_and_strip_voice_emotion("[超开心]今天收到了新礼物！", character="amiya")
        self.assertEqual(tag, "")
        self.assertEqual(text, "今天收到了新礼物！")

        tag, text = parse_and_strip_voice_emotion("[开朗]今天天气真好！", character="amiya")
        self.assertEqual(tag, "")
        self.assertEqual(text, "今天天气真好！")

    def test_parse_and_strip_voice_emotion_multiple_consecutive_tags(self):
        tag, text = parse_and_strip_voice_emotion("[开心][温柔] 博士早上好！", character="amiya")
        self.assertEqual(tag, "开心")
        self.assertEqual(text, "博士早上好！")

        tag, text = parse_and_strip_voice_emotion("[未知][开心] 博士早上好！", character="amiya")
        self.assertEqual(tag, "开心")
        self.assertEqual(text, "博士早上好！")

    def test_parse_and_strip_voice_emotion_bracket_protection(self):
        tag, text = parse_and_strip_voice_emotion("[开心】你好", character="amiya")
        self.assertEqual(tag, "")
        self.assertEqual(text, "[开心】你好")

        tag, text = parse_and_strip_voice_emotion("[开心]关于【罗德岛】制药的决策文档[已批准]", character="amiya")
        self.assertEqual(tag, "开心")
        self.assertEqual(text, "关于【罗德岛】制药的决策文档[已批准]")

    def test_parse_and_strip_voice_emotion_empty_body_fallback(self):
        tag, text = parse_and_strip_voice_emotion("[开心]", character="amiya")
        self.assertEqual(tag, "开心")
        self.assertEqual(text, SAFE_FALLBACK_REPLIES["amiya"])

        tag, text = parse_and_strip_voice_emotion("[冷静]   ... ", character="kaltsit")
        self.assertEqual(tag, "冷静")
        self.assertEqual(text, SAFE_FALLBACK_REPLIES["kaltsit"])

    def test_canonical_presets_structure(self):
        for cid in ("amiya", "dania", "kaltsit"):
            self.assertIn(cid, VOICE_CANONICAL_PRESETS)
            preset = VOICE_CANONICAL_PRESETS[cid]
            self.assertIn("default_ref", preset)
            self.assertIn("tags", preset)
            self.assertGreaterEqual(len(preset["tags"]), 1)

        self.assertEqual(VOICE_CANONICAL_PRESETS["dania"]["default_ref"], "hf_025")
        self.assertEqual(VOICE_CANONICAL_PRESETS["kaltsit"]["default_ref"], "default")

    def test_fast_local_delivery_select_flag_off_and_on(self):
        with patch.object(tts_service, "state", SimpleNamespace(voice_emotion_director_enabled=False)):
            with patch.dict(os.environ, {"VOICE_EMOTION_ROUTING_ENABLED": "false"}):
                candidates = [{"name": "hf_025"}, {"name": "hf_024"}]
                res = _fast_local_delivery_select("你好呀", "dania", candidates, "hf_025", emotion_tag="开朗")
                self.assertEqual(res["ref_name"], "hf_025")
                self.assertEqual(res["decision_source"], "neutral_baseline_legacy")

            with patch.dict(os.environ, {"VOICE_EMOTION_ROUTING_ENABLED": "true"}):
                candidates = [
                    {"name": "CN_001_normal"},
                    {"name": "CN_008_happy"},
                    {"name": "CN_031_sad"},
                ]
                res = _fast_local_delivery_select("你好呀", "amiya", candidates, "CN_001_normal", emotion_tag="开心")
                self.assertEqual(res["ref_name"], "CN_008_happy")
                self.assertEqual(res["decision_source"], "canonical_emotion_preset")

    async def test_check_voice_services_ttl_cache(self):
        _VOICE_CHECK_CACHE["expires_at"] = 0.0
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.get", AsyncMock(return_value=mock_resp)) as mock_get:
            res1 = await check_voice_services()
            self.assertTrue(res1["tts_9880_online"])
            call_count = mock_get.call_count

            res2 = await check_voice_services()
            self.assertTrue(res2["tts_9880_online"])
            self.assertEqual(mock_get.call_count, call_count)


if __name__ == "__main__":
    unittest.main()
