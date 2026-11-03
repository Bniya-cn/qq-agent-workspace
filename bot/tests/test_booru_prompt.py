"""Illustrious / NoobAI 提示词必须是 Danbooru 标签，而不是口语。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nonebot

nonebot.init()
from src.plugins.agent_core.booru_prompt import build_booru_prompt, nsfw_tags_for


class BooruPromptTests(unittest.TestCase):
    def test_restraint_words_use_danbooru_tags(self):
        tags = nsfw_tags_for("手背后，大腿绳，口球，脸红，害羞，有口水")
        for expected in (
            "arms behind back",
            "bound wrists",
            "bound thighs",
            "ball gag",
            "gagged",
            "blush",
            "shy",
            "saliva",
            "drooling",
            "shibari over clothes",
        ):
            self.assertIn(expected, tags)

    def test_illustrious_order_puts_character_before_clothes(self):
        positive, negative = build_booru_prompt(
            user_text="手背后，大腿绳，口球，脸红，害羞",
            character_tags=["mika_(blue_archive)", "mika_(swimsuit)"],
            feature_tags=["yellow eyes", "pink hair", "single side bun", "hair flower", "pink halo", "low wings"],
            clothing_tags=["white bikini", "frills"],
            scene_tags=["beach", "sitting", "looking at viewer"],
            base="illustrious",
        )
        self.assertTrue(positive.startswith("masterpiece, best quality, very aesthetic, absurdres, newest, 1girl"))
        self.assertNotIn("score_9", positive)
        self.assertLess(positive.index(r"mika_\(blue_archive\)"), positive.index("white bikini"))
        self.assertLess(positive.index("white bikini"), positive.index("arms behind back"))
        self.assertIn("holding", negative)
        self.assertIn("outstretched arms", negative)

    def test_crotch_rope_vibrator_and_fluids(self):
        tags = nsfw_tags_for("股间绳子，跳蛋，液体")
        for expected in ("crotch rope", "egg vibrator", "vibrator", "wet", "pussy juice", "saliva", "sweat"):
            self.assertIn(expected, tags)
        self.assertLess(tags.index("crotch rope"), tags.index("shibari over clothes"))

    def test_plain_scene_does_not_add_restraint_negatives(self):
        _, negative = build_booru_prompt(
            user_text="在办公室工作",
            character_tags=["denia_(wuthering_waves)"],
            scene_tags=["office"],
        )
        self.assertNotIn("holding", negative)
        self.assertNotIn("bondage", _)


if __name__ == "__main__":
    unittest.main()
