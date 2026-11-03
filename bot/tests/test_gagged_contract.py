# -*- coding: utf-8 -*-
"""
封嘴语音回归与 MRTE test=1 调用链契约测试（可在 /usr/bin/python3 标准库环境下直接运行）
"""
import array
import ast
import importlib.util
import io
import unittest
import wave
from pathlib import Path

ROOT = Path("/Users/daijinglin/Desktop/qq-agent-workspace")
AUDIO_DSP_PATH = ROOT / "bot/src/plugins/agent_core/audio_dsp.py"
MODELS_PY_PATH = ROOT / "gpt-sovits/GPT_SoVITS/module/models.py"
MRTE_PY_PATH = ROOT / "gpt-sovits/GPT_SoVITS/module/mrte_model.py"
TTS_PY_PATH = ROOT / "gpt-sovits/GPT_SoVITS/TTS_infer_pack/TTS.py"
API_V2_PATH = ROOT / "gpt-sovits/api_v2.py"


def _load_audio_dsp():
    spec = importlib.util.spec_from_file_location("audio_dsp_test", AUDIO_DSP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGaggedContractAndMRTEChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ad = _load_audio_dsp()
        cls.ref3_bytes = (ROOT / "gg-test/wav/gagged_3.wav").read_bytes()

    def test_01_join_whimper_units_accepts_pause_durations_and_pause_sec(self):
        """验证 tts_service.py:540 传入 pause_durations=pause_list 不再抛出 TypeError。"""
        u1 = array.array("h", [1200, -1200] * 4000)
        u2 = array.array("h", [800, -800] * 4000)
        wav_bytes = self.ad.join_whimper_units_to_wav([u1, u2], 32000, pause_durations=[0.21, 0.20])
        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            self.assertEqual(wf.getframerate(), 32000)
            expected_frames = len(u1) + len(u2) + int(32000 * 0.21)
            self.assertEqual(wf.getnframes(), expected_frames)

    def test_02_shape_whimper_unit_pcm_contract(self):
        """验证 tts_service.py:499 调用 shape_whimper_unit_pcm 的完整关键字参数契约与返回字段。"""
        pcm, sr, meta = self.ad.shape_whimper_unit_pcm(
            self.ref3_bytes,
            style="protest",
            min_sec=1.0,
            max_sec=1.6,
            target_sec=1.28,
            base_f0=276.0,
            mark_offset_ratio=0.42,
            pitch_undulation=1.0,
        )
        self.assertEqual(sr, 32000)
        self.assertGreater(len(pcm), 0)
        self.assertLessEqual(len(pcm), int(32000 * 1.6))
        self.assertIn("shaped_duration_sec", meta)
        self.assertIn("median_f0_hz", meta)
        self.assertIn("avg_zcr_hz", meta)
        self.assertGreater(meta["median_f0_hz"], 180.0)

    def test_03_normal_tts_enhance_and_cap_untouched(self):
        """确认普通角色语音 (muffled=False) 的增强与归一化路径不受影响。"""
        enh_wav, pcm_24k, silk_rate = self.ad.enhance_and_cap_wav_bytes(
            self.ref3_bytes, max_duration_sec=30.0, muffled=False
        )
        self.assertTrue(enh_wav.startswith(b"RIFF"))
        self.assertEqual(silk_rate, 24000)
        self.assertGreater(len(pcm_24k), 0)

    def test_04_mrte_test1_forwarding_in_models_py(self):
        """AST 级验证 models.py 中 TextEncoder.forward、SynthesizerTrn.decode、SynthesizerTrnV3.decode_encp 的 test 参数透传。"""
        src = MODELS_PY_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)

        found_text_enc = False
        found_trn_decode = False
        found_v3_decode_encp = False

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "TextEncoder":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "forward":
                        arg_names = [a.arg for a in item.args.args]
                        self.assertIn("test", arg_names)
                        # Confirm self.mrte(...) receives keyword test=test
                        for sub in ast.walk(item):
                            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "mrte":
                                kw_names = [kw.arg for kw in sub.keywords]
                                self.assertIn("test", kw_names, "TextEncoder.forward must pass test=test to self.mrte")
                                found_text_enc = True

            elif isinstance(node, ast.ClassDef) and node.name == "SynthesizerTrn":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "decode":
                        arg_names = [a.arg for a in item.args.args]
                        self.assertIn("test", arg_names, "SynthesizerTrn.decode must accept test parameter")
                        for sub in ast.walk(item):
                            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "enc_p":
                                kw_names = [kw.arg for kw in sub.keywords]
                                self.assertIn("test", kw_names, "SynthesizerTrn.decode must pass test=test to self.enc_p")
                                found_trn_decode = True

            elif isinstance(node, ast.ClassDef) and node.name == "SynthesizerTrnV3":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "decode_encp":
                        arg_names = [a.arg for a in item.args.args]
                        self.assertIn("test", arg_names, "SynthesizerTrnV3.decode_encp must accept test parameter")
                        for sub in ast.walk(item):
                            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "enc_p":
                                kw_names = [kw.arg for kw in sub.keywords]
                                self.assertIn("test", kw_names, "SynthesizerTrnV3.decode_encp must pass test=test to self.enc_p")
                                found_v3_decode_encp = True

        self.assertTrue(found_text_enc, "TextEncoder.forward -> self.mrte(test=test) verified")
        self.assertTrue(found_trn_decode, "SynthesizerTrn.decode -> self.enc_p(test=test) verified")
        self.assertTrue(found_v3_decode_encp, "SynthesizerTrnV3.decode_encp -> self.enc_p(test=test) verified")

    def test_05_readonly_gg_test_assets_intact(self):
        """验证原始影视剧素材与 1 级解码 WAV 的 SHA-256 未被篡改。"""
        import hashlib
        expected = {
            "gg-test/素材1.mp3": "55ee8d51885eaf30",
            "gg-test/素材2.mp3": "717572b542408946",
            "gg-test/素材3.mp3": "a2e251bf82832c6d",
            "gg-test/wav/gagged_1.wav": "7f402313a640d8cb",
            "gg-test/wav/gagged_2.wav": "203f9010b5a6aac6",
            "gg-test/wav/gagged_3.wav": "eb0af0f7aec8e74c",
        }
        for rel, prefix in expected.items():
            p = ROOT / rel
            self.assertTrue(p.is_file(), f"Missing {rel}")
            sha = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            self.assertEqual(sha, prefix, f"Hash mismatch on {rel}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
