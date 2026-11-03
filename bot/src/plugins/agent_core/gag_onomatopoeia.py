"""台词 → 可识别的「堵嘴拟声」映射（独立模块，纯标准库，pypinyin 可选）。

设计原则（参考同人写作/日系擬音/ASMR 的 gag-speak 惯例）：
  1. 音节数不变：一个汉字 → 一个闭口拟声音节（R2D2 原则：先写真台词，再"闷掉"）。
  2. 声调不变：每个音节保留原字声调，由 F0 塑形（tone_f0_contour）或原台词 F0 移植实现。
  3. 重音/标点不变：！→ 末音节加重 + 用"哼"类爆发鼻音；？→ 末音节升调；……→ 拖长 + 停顿。
  4. 声母决定"闭口鼻音的颜色"（读者靠首辅音猜词）：
       双唇/唇齿 b p m f       → 唔 (mm, 双唇闭合)
       舌尖 d t n l            → 嗯 (nn, 舌尖抵龈)
       舌根 g k h              → 哼 (hng, 喉/软腭)；非重读时退为 嗯
       舌面/卷舌/平舌 j q x zh ch sh r z c s → 嗯 (n 化 + 鼻气)
       零声母 y w / 元音开头    → 由韵母决定
  5. 韵母决定"嘴型残影"：开口/圆唇韵母 a o e ai ao ou an ang ong → 唔；齐齿 i/ü/en/eng/in → 嗯；
     开口元音重读时可展开为"唔呃"式双段（仅用于 display 文本，合成仍保持 1 音节 1 拟声）。
  6. 避免"呜/哇/啊"等开口字（会被 TTS/听众读成哭腔或喊叫）。

主要函数：
  syllabify(text)            -> list[Syllable]  （拼音、声母、韵母、声调、标点重音）
  map_syllable(syl)          -> GagToken
  text_to_gag(text)          -> GagLine（tts_text / display / roman / tokens）
  tone_f0_contour(tone, base_f0, n, ...) -> list[float]  （声调 F0 模板，供 DSP 塑形）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

try:  # pypinyin 可选；缺失时用内置小字典 + 中性回退
    from pypinyin import pinyin as _py_pinyin, Style as _PyStyle  # type: ignore
except Exception:  # pragma: no cover
    _py_pinyin = None
    _PyStyle = None

INITIALS = ("zh", "ch", "sh", "b", "p", "m", "f", "d", "t", "n", "l", "g", "k", "h",
            "j", "q", "x", "r", "z", "c", "s", "y", "w")
LABIAL = {"b", "p", "m", "f"}
ALVEOLAR = {"d", "t", "n", "l"}
VELAR = {"g", "k", "h"}
SIBILANT = {"j", "q", "x", "zh", "ch", "sh", "r", "z", "c", "s"}
OPEN_FINALS = ("a", "o", "e", "ai", "ao", "ou", "an", "ang", "ong", "ua", "uo", "uai", "uan", "uang", "ia", "iao", "ian", "iang", "er")
CJK = re.compile(r"[\u4e00-\u9fff]")
PUNCT_EXCL = set("！!")
PUNCT_Q = set("？?")
PUNCT_PAUSE = set("，,、；;：:")
PUNCT_END = set("。.")

# 闭口拟声字库：char, pinyin(带调号数字), roman(英文 gag-speak), kana(日式)
GAG_UNITS = {
    "mm": ("唔", "wu2", "mm", "んむ"),
    "nn": ("嗯", "en1", "nn", "んん"),
    "hng": ("哼", "heng1", "hmph", "んぐ"),
    "mf": ("呣", "m2", "mf", "むぅ"),
}
# 4 个声调下各拟声的"带调读法"（给 TTS 文本时用于提示声调；实际声调仍以 F0 塑形为准）
TONE_MARK = {1: "ˉ", 2: "ˊ", 3: "ˇ", 4: "ˋ", 5: ""}

_FALLBACK_PY = {  # 测试台词的内置回退（无 pypinyin 时保证可用）
    "放": "fang4", "开": "kai1", "我": "wo3", "救": "jiu4", "命": "ming4", "你": "ni3",
    "是": "shi4", "谁": "shei2", "不": "bu4", "会": "hui4", "说": "shuo1", "的": "de5",
    "博": "bo2", "士": "shi4", "快": "kuai4", "跑": "pao3",
}


@dataclass
class Syllable:
    char: str
    pinyin: str          # 如 fang4
    initial: str         # 如 f；零声母为 ""
    final: str           # 如 ang
    tone: int            # 1-4，轻声 5
    stress: float = 1.0  # 1.0 普通；>1 重读
    boundary: str = ""   # 该音节后的标点："!" "?" "," "..." "." 或 ""
    phrase_final: bool = False


@dataclass
class GagToken:
    syllable: Syllable
    unit: str            # mm / nn / hng / mf
    char: str            # 合成用拟声字
    roman: str
    kana: str
    tone: int
    stress: float
    duration_scale: float = 1.0
    pause_after_ms: int = 0


@dataclass
class GagLine:
    source: str
    tokens: list = field(default_factory=list)
    tts_text: str = ""     # 喂给角色 TTS 的拟声文本（保留标点）
    display: str = ""      # 给用户看的中文拟声（含重读拖长）
    roman: str = ""        # 英文 gag-speak
    kana: str = ""         # 日式擬音

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def split_pinyin(py: str) -> tuple[str, str, int]:
    """'fang4' -> ('f','ang',4)；'er2' -> ('', 'er', 2)；无数字视为轻声 5。"""
    m = re.match(r"^([a-zü]+?)([1-5]?)$", py.lower().replace("v", "ü"))
    if not m:
        return "", py, 5
    body, tone = m.group(1), int(m.group(2) or 5)
    for ini in INITIALS:
        if body.startswith(ini) and len(body) > len(ini):
            fin = body[len(ini):]
            if ini == "y":
                fin = "i" + fin if not fin.startswith(("i", "u")) else fin
            elif ini == "w":
                fin = "u" + fin if not fin.startswith("u") else fin
            return ini, fin, tone
    return "", body, tone


def _char_pinyin(chars: list[str]) -> list[str]:
    if _py_pinyin is not None:
        res = _py_pinyin("".join(chars), style=_PyStyle.TONE3, neutral_tone_with_five=True, errors="ignore")
        out = [r[0] for r in res]
        if len(out) == len(chars):
            return out
    return [_FALLBACK_PY.get(c, "en5") for c in chars]


def syllabify(text: str) -> list[Syllable]:
    """拆音节并标注标点边界与重读。"""
    chars: list[str] = []
    bounds: list[str] = []
    i = 0
    t = text.replace("…", "...").replace("——", ",")
    while i < len(t):
        c = t[i]
        if CJK.match(c):
            chars.append(c)
            bounds.append("")
        elif chars:
            if t.startswith("...", i):
                bounds[-1] = "..."
                i += 3
                continue
            if c in PUNCT_EXCL:
                bounds[-1] = "!"
            elif c in PUNCT_Q:
                bounds[-1] = "?"
            elif c in PUNCT_PAUSE and not bounds[-1]:
                bounds[-1] = ","
            elif c in PUNCT_END and not bounds[-1]:
                bounds[-1] = "."
        i += 1
    pys = _char_pinyin(chars)
    sylls: list[Syllable] = []
    for c, py, b in zip(chars, pys, bounds):
        ini, fin, tone = split_pinyin(py)
        sylls.append(Syllable(c, py, ini, fin, tone, 1.0, b))
    # 短语末与重读
    for k, s in enumerate(sylls):
        nxt_b = k == len(sylls) - 1 or bool(s.boundary)
        s.phrase_final = nxt_b
        if s.boundary == "!":
            s.stress = 1.6
            if k > 0 and not sylls[k - 1].boundary:
                sylls[k - 1].stress = max(sylls[k - 1].stress, 1.25)
        elif s.boundary == "?":
            s.stress = 1.3
        elif s.tone == 4:
            s.stress = max(s.stress, 1.1)
        elif s.tone == 5:
            s.stress = 0.75
    return sylls


def map_syllable(s: Syllable) -> GagToken:
    """按声母/韵母/重读选择闭口拟声。"""
    open_final = s.final in OPEN_FINALS or s.final.startswith(("a", "o"))
    if s.initial in LABIAL:
        unit = "mm"
    elif s.initial in VELAR:
        unit = "hng" if s.stress >= 1.1 else "nn"
    elif s.initial in ALVEOLAR:
        unit = "nn"
    elif s.initial in SIBILANT:
        unit = "nn" if not open_final else "mm"
    else:  # 零声母 / y / w
        unit = "mm" if (open_final or s.initial == "w" or s.final.startswith("u")) else "nn"
    if s.tone == 5 and unit != "mm":
        unit = "mf" if s.initial in LABIAL else "nn"
    ch, _py, roman, kana = GAG_UNITS[unit]
    dur = 1.0
    pause = 0
    if s.boundary == "!":
        dur, pause = 1.35, 220
    elif s.boundary == "?":
        dur, pause = 1.25, 220
    elif s.boundary == "...":
        dur, pause = 1.6, 420
    elif s.boundary in (",", "."):
        dur, pause = 1.1, 180
    elif s.tone == 5:
        dur = 0.7
    return GagToken(s, unit, ch, roman, kana, s.tone, s.stress, dur, pause)


def _display_unit(tok: GagToken) -> str:
    ch = tok.char
    open_final = tok.syllable.final in OPEN_FINALS
    if tok.stress >= 1.5:
        ch = ch + ("呃" if open_final and tok.unit == "mm" else "") + "——"
    elif tok.syllable.boundary == "...":
        ch = ch + "……"
    return ch


def text_to_gag(text: str) -> GagLine:
    """主入口：台词 → GagLine。tts_text 保持 1 字 1 拟声 + 原标点。"""
    sylls = syllabify(text)
    toks = [map_syllable(s) for s in sylls]
    tts_parts, disp, roman, kana = [], [], [], []
    punct_map = {"!": "！", "?": "？", "...": "……", ",": "，", ".": "。", "": ""}
    for t in toks:
        b = t.syllable.boundary
        tts_parts.append(t.char + punct_map[b])
        disp.append(_display_unit(t) + ("" if t.stress >= 1.5 or b == "..." else punct_map[b]) + ("！" if t.stress >= 1.5 and b == "!" else ""))
        r = t.roman
        if t.stress >= 1.5:
            r = r[0].upper() + r[1:] + r[-1] * 2
        roman.append(r + ("-" if not b else {"!": "!", "?": "?", "...": "...", ",": ",", ".": "."}[b] + " "))
        kana.append(t.kana + ("ーっ" if t.stress >= 1.5 else "") + {"!": "！", "?": "？", "...": "……", ",": "、", ".": "。", "": ""}[b])
    return GagLine(text, toks, "".join(tts_parts), "".join(disp), "".join(roman).strip(" -"), "".join(kana))


def tone_f0_contour(tone: int, base_f0: float, n: int, span_st: float = 4.0, stress: float = 1.0) -> list[float]:
    """普通话声调 F0 模板（五度标记法近似）：返回 n 帧 Hz。
    1 阴平 55 平高；2 阳平 35 上升；3 上声 214 降升（快语中常为 21 低降）；4 去声 51 高降；5 轻声 短平略降。
    span_st：调域（半音）；stress>1 放大调域并抬高起点。"""
    if n <= 0:
        return []
    shapes = {1: (5, 5, 5), 2: (3, 4, 5), 3: (2, 1, 3), 4: (5, 3, 1), 5: (3, 2.6, 2.3)}
    a, b, c = shapes.get(tone, shapes[5])
    span = span_st * (stress if stress > 1 else 1.0)
    out = []
    for k in range(n):
        x = k / max(1, n - 1)
        # 二次插值三点
        lvl = a * (1 - x) * (1 - 2 * x) + b * 4 * x * (1 - x) + c * x * (2 * x - 1) if n > 2 else a + (c - a) * x
        st = (lvl - 3.0) / 4.0 * span  # 五度 1..5 → [-span/2, +span/2]
        out.append(base_f0 * (2.0 ** (st / 12.0)))
    return out


def plan_f0(line: GagLine, base_f0: float, frame_ms: float = 5.0, syl_ms: float = 230.0, span_st: float = 4.0):
    """无原台词 F0 时，按音节/声调/重读/停顿生成整句 F0 轨（0 表示静音）。返回 (f0_list, syllable_spans)。"""
    f0: list[float] = []
    spans = []
    decl = 0.0
    for i, t in enumerate(line.tokens):
        n = max(4, int(syl_ms * t.duration_scale / frame_ms))
        b = base_f0 * (2 ** (-decl / 12.0)) * (2 ** ((t.stress - 1.0) * 3.0 / 12.0))
        seg = tone_f0_contour(t.tone, b, n, span_st, t.stress)
        if t.syllable.boundary == "?":
            seg = [v * (2 ** (3.0 * k / max(1, n - 1) / 12.0)) for k, v in enumerate(seg)]
        spans.append((len(f0), len(f0) + n))
        f0.extend(seg)
        f0.extend([0.0] * int(t.pause_after_ms / frame_ms))
        decl += 0.35  # 句内下倾
        if t.syllable.boundary:
            decl = 0.0
    return f0, spans


# ---- 原版 gg-test/wav/gagged_3.wav 里混入的真台词（ASR 识别为「全兵放了」/「全兵散了」，0–1.1s 与 1.85–2.55s 两处）----
G3_SPOKEN_PHRASES = ("全兵放了", "全兵散了")
G3_SPOKEN_SEGMENTS_S = ((0.0, 1.10), (1.85, 2.55))


def gag_phrase(text: str) -> dict:
    """任意一句真台词 → 闭口拟声（tts_text/display/roman/kana + 每音节 unit/tone）。"""
    g = text_to_gag(text)
    return dict(source=text, tts_text=g.tts_text, display=g.display, roman=g.roman, kana=g.kana,
                units=[t.unit for t in g.tokens], tones=[t.tone for t in g.tokens])


def g3_spoken_replacements() -> dict:
    """g3 真台词两种识别结果各自的拟声替换（音节数、声调保持）。"""
    return {p: gag_phrase(p) for p in G3_SPOKEN_PHRASES}


def replace_spoken_phrases(text: str, phrases=G3_SPOKEN_PHRASES) -> str:
    """把文本里出现的已知真台词替换成拟声（其余字符不动）。"""
    for p in phrases:
        if p in text:
            text = text.replace(p, text_to_gag(p).tts_text)
    return text


if __name__ == "__main__":  # 简易自检
    for s in ["放开我！", "救命", "你是谁", "我不会说的", "博士……快跑"]:
        g = text_to_gag(s)
        print(s, "|", g.tts_text, "|", g.display, "|", g.roman, "|", g.kana)
    print(g3_spoken_replacements())
