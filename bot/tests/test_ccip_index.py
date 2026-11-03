"""CCIP 权重能对上网络，并且同一张图会检索回自己。"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("ccip_index", SCRIPTS / "ccip_index.py")
ccip_index = importlib.util.module_from_spec(spec)
sys.modules["ccip_index"] = ccip_index
spec.loader.exec_module(ccip_index)


class CcipIndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.device = ccip_index.pick_device("cpu")
        cls.model = ccip_index.load_ccip(cls.device)

    def test_same_image_is_identical_and_retrieves_itself(self) -> None:
        image = Image.new("RGBA", (64, 96), (0, 0, 0, 0))
        pixels = image.load()
        for y in range(20, 80):
            for x in range(16, 48):
                pixels[x, y] = (40, 90, 180, 255)
        first, second = ccip_index.encode_images(self.model, [image, image], self.device)
        cosine = float(np.dot(first, second))
        self.assertGreater(cosine, 0.999)

        refs = Path(tempfile.mkdtemp())
        game = refs / "Endfield"
        game.mkdir()
        image.save(game / "测试.png")
        catalog = {
            "game": "Endfield",
            "assets": {
                "test": {
                    "id": "test",
                    "name": "测试",
                    "aliases": ["测试角色"],
                    "filename": "测试.png",
                    "sha256": "",
                    "status": "ready",
                }
            },
        }
        (game / "catalog.json").write_text(
            __import__("json").dumps(catalog, ensure_ascii=False),
            encoding="utf-8",
        )
        index_dir = refs / "ccip"
        stats = ccip_index.build_index(
            refs_dir=refs,
            index_dir=index_dir,
            games=("Endfield",),
            batch_size=1,
            device_name="cpu",
        )
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["kept"], 1)
        hits = ccip_index.search_index(first, index_dir, top_k=1)
        self.assertEqual(hits[0]["name"], "测试")
        self.assertLess(hits[0]["difference"], 0.01)


if __name__ == "__main__":
    unittest.main()
