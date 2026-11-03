"""隔离模型加载，验证 9880 参考缓存的角色、文件版本和容量边界。"""
import ast
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest


class ReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.weights = self.root / "weights.pth"
        self.weights.write_bytes(b"weights")
        self.pipeline = SimpleNamespace(configs=SimpleNamespace(
            t2s_weights_path=str(self.weights), vits_weights_path=str(self.weights), version="v4"),
            prompt_cache={})
        self.computed = []

        def render(req):
            if self.pipeline.prompt_cache.get("ref_audio_path") != req["ref_audio_path"]:
                self.computed.append(req["ref_audio_path"])
            self.pipeline.prompt_cache = {"ref_audio_path": req["ref_audio_path"], "features": [req["ref_audio_path"]]}
            return SimpleNamespace(status_code=200)

        source = Path(__file__).resolve().parents[2] / "gpt-sovits" / "api_v2.py"
        tree = ast.parse(source.read_text())
        names = {"_reference_cache_key", "_copy_reference_cache", "_render_voice_request"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.ns = {"Path": Path, "tts_pipeline": self.pipeline, "REFERENCE_CACHES": OrderedDict(),
                   "VOICE_MODEL_LOCK": threading.Lock(), "_tts_handle_sync": render,
                   "_switch_voice_model": lambda role: None}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), self.ns)

    def request(self, name):
        path = self.root / name
        path.touch(exist_ok=True)
        return {"ref_audio_path": str(path), "prompt_text": "参考台词", "prompt_lang": "zh"}

    def test_switching_reference_restores_features(self):
        a, b = self.request("a.wav"), self.request("b.wav")
        for req in (a, b, a, b):
            self.ns["_render_voice_request"](req)
        self.assertEqual(self.computed, [a["ref_audio_path"], b["ref_audio_path"]])

    def test_model_and_reference_versions_isolate_cache(self):
        a = self.request("a.wav")
        key = self.ns["_reference_cache_key"](a)
        other = self.root / "other.pth"
        other.write_bytes(b"other")
        self.pipeline.configs.vits_weights_path = str(other)
        self.assertNotEqual(key, self.ns["_reference_cache_key"](a))
        key = self.ns["_reference_cache_key"](a)
        Path(a["ref_audio_path"]).write_bytes(b"new reference")
        self.assertNotEqual(key, self.ns["_reference_cache_key"](a))

    def test_cache_capacity_is_bounded(self):
        for index in range(10):
            self.ns["_render_voice_request"](self.request(f"{index}.wav"))
        self.assertEqual(len(self.ns["REFERENCE_CACHES"]), 8)


if __name__ == "__main__":
    unittest.main()
