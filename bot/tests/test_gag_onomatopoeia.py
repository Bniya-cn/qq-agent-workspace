import importlib.util, pathlib, sys
P = pathlib.Path(__file__).resolve().parents[1] / "src/plugins/agent_core/gag_onomatopoeia.py"
spec = importlib.util.spec_from_file_location("gag_onomatopoeia", P)
g = importlib.util.module_from_spec(spec); sys.modules["gag_onomatopoeia"] = g; spec.loader.exec_module(g)

LINES = ["放开我！", "救命", "你是谁", "我不会说的", "博士……快跑"]

def test_split_pinyin():
    assert g.split_pinyin("fang4") == ("f", "ang", 4)
    assert g.split_pinyin("zhi1") == ("zh", "i", 1)
    assert g.split_pinyin("de5")[2] == 5

def test_syllable_count_preserved():
    for s in LINES:
        gl = g.text_to_gag(s)
        n_src = len([c for c in s if g.CJK.match(c)])
        assert len(gl.tokens) == n_src
        assert len([c for c in gl.tts_text if g.CJK.match(c)]) == n_src

def test_only_closed_mouth_chars():
    allowed = set("唔嗯哼呣")
    for s in LINES:
        assert set(c for c in g.text_to_gag(s).tts_text if g.CJK.match(c)) <= allowed

def test_rules():
    t = g.text_to_gag("放开我！").tokens
    assert t[0].unit == "mm"            # f 唇齿 → 唔
    assert t[1].unit == "hng"           # k 舌根 + 重读 → 哼
    assert t[2].stress > 1.4 and t[2].pause_after_ms > 0
    assert [x.tone for x in t] == [4, 1, 3]
    assert g.text_to_gag("你是谁").tokens[0].unit == "nn"

def test_punct_kept():
    assert g.text_to_gag("放开我！").tts_text.endswith("！")
    assert "……" in g.text_to_gag("博士……快跑").tts_text

def test_tone_contours():
    f1 = g.tone_f0_contour(1, 300, 20); f2 = g.tone_f0_contour(2, 300, 20)
    f4 = g.tone_f0_contour(4, 300, 20); f3 = g.tone_f0_contour(3, 300, 21)
    assert max(f1) - min(f1) < 1e-6
    assert f2[-1] > f2[0] and f4[-1] < f4[0]
    assert min(f3) < f3[0] and min(f3) < f3[-1]

def test_plan_f0():
    f0, spans = g.plan_f0(g.text_to_gag("放开我！"), 240)
    assert len(spans) == 3 and len(f0) > spans[-1][1]

def test_g3_phrases():
    r = g.g3_spoken_replacements()
    for p in ("全兵放了", "全兵散了"):
        assert len([c for c in r[p]["tts_text"] if g.CJK.match(c)]) == 4
        assert set(r[p]["tts_text"]) <= set("唔嗯哼呣")
    assert g.replace_spoken_phrases("嗯唔全兵放了嗯") == "嗯唔" + r["全兵放了"]["tts_text"] + "嗯"
