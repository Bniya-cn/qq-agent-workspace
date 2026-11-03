# faster-whisper small (tools/asr-venv) word / open-vowel check, resumable.
# REAL-WORD gate: reject any real Chinese lexical token (not only open vowels).
# usage: asr_dir.py out.json dir1 [dir2..]
# ASR_GLOB env overrides the default "rvc*.wav" pattern.
import glob, os, json, sys
from faster_whisper import WhisperModel
# Allowed nonverbal / gag syllables + punctuation. Everything else is a real lexical char.
HUM=set("呜嗚呦哼嗯呃呿喵咪哦喔唔呵嘿唉哎哟噢欸誒呣啪嗷…。，、！？!?,. ~～-")
# Whisper often hallucinates these as repeated plosive onsets on muffled hums; alone/repeated they are NOT counted as real words,
# but they still flag click_onset so the 哒/嘟 problem stays visible.
CLICK_HUM=set("嘟哒嗒都啵波")
OPENA=set("啊阿哈妈嗎吗呀哇啦嗷拉吧爸怕他她它沙杀嘛麻马骂呵嗬")
NONLEX=HUM|CLICK_HUM|OPENA  # for lexical_chars display we still list non-hum
out=sys.argv[1]; res=json.load(open(out)) if os.path.exists(out) else {}
m=WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4)
def lex_chars(t):
    return set(c for c in t if c not in HUM and c not in CLICK_HUM and c not in OPENA and not c.isspace() and c!='\ufffd' and not c.isascii())
for d in sys.argv[2:]:
    for f in sorted(glob.glob(f"{d}/"+os.environ.get("ASR_GLOB","rvc*.wav"))):
        b=os.path.basename(f)
        if b in res and "real_words" in res[b]: continue
        segs,_=m.transcribe(f, language="zh", beam_size=5, vad_filter=False, condition_on_previous_text=False, temperature=0.0, word_timestamps=True)
        segs=[dict(text=s.text.strip(), nsp=round(s.no_speech_prob,3), lp=round(s.avg_logprob,3), cw=[w.word for w in (s.words or []) if w.probability>=0.5]) for s in segs]
        all_lex=set().union(*[lex_chars(s["text"]) for s in segs]) if segs else set()
        # confident real word: any high-prob word token containing a real lexical char
        conf_real=[s for s in segs if s["cw"] and s["nsp"]<0.7 and s["lp"]>-1.2 and any(lex_chars(w) for w in s["cw"])]
        # real_words: ANY distinct real lexical char (>=1), or any confident real token. Pure click-hum hallucinations alone do not count.
        real=bool(all_lex) or bool(conf_real)
        # legacy "words" kept for older reports: >=2 distinct lex OR confident real
        words=bool(len(all_lex)>=2 or conf_real)
        opena=[s for s in segs if any(c in OPENA for c in s["text"]) and s["nsp"]<0.7 and s["lp"]>-1.2]
        click=[s for s in segs if any(c in CLICK_HUM for c in s["text"])]
        res[b]=dict(text=" | ".join(s["text"] for s in segs)[:200], words=words, real_words=real, open_a=bool(opena),
                    click_onset=bool(click), lexical_chars="".join(sorted(all_lex)),
                    click_chars="".join(sorted(set(c for s in segs for c in s["text"] if c in CLICK_HUM))))
        print(b, res[b], flush=True)
        if len(res)%10==0: json.dump(res, open(out,"w"), ensure_ascii=False, indent=0)
json.dump(res, open(out,"w"), ensure_ascii=False, indent=0)
