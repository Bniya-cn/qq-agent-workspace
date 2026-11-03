# faster-whisper check that RVC outputs contain no recognizable words
import glob, os, json, sys
from faster_whisper import WhisperModel
O="/Users/daijinglin/Desktop/qq-agent-workspace/bot/data/test_output/exp_gagged_v3_rvc"
files=sorted(glob.glob(f"{O}/rvc_*.wav")) if len(sys.argv)<2 else sys.argv[1:]
m=WhisperModel("small", device="cpu", compute_type="int8")
NONLEX=set("呜嗚呦哼嗯啊呃呿喵哦喔唔呵哈嘿唉哎哟噢欸誒呣啪嗷呀哇…。，、！？!?,. ~～-")
lexical=lambda t: any(c not in NONLEX and not c.isspace() for c in t)
OPENA=set("啊阿哈妈嗎吗呀哇啦嗷拉吧爸怕他她它沙杀嘛麻马骂")
res={}
for f in files:
    segs,info=m.transcribe(f, language="zh", beam_size=5, vad_filter=False, condition_on_previous_text=False, temperature=0.0, word_timestamps=True)
    out=[]
    for s in segs:
        words=[w for w in (s.words or []) if w.probability>=0.5]
        out.append({"start":round(s.start,2),"end":round(s.end,2),"text":s.text.strip(),"no_speech_prob":round(s.no_speech_prob,3),"avg_logprob":round(s.avg_logprob,3),"conf_words":[w.word for w in words]})
    flagged=[o for o in out if lexical(o["text"]) and o["conf_words"] and o["no_speech_prob"]<0.6 and o["avg_logprob"]>-1.0]
    opena=[o for o in out if any(c in OPENA for c in o["text"]) and o["no_speech_prob"]<0.6]
    res[os.path.basename(f)]={"segments":out,"flagged_speech":flagged,"open_a_sounds":bool(opena),"verdict":"WORDS_DETECTED" if flagged else "no_words"}
    print(os.path.basename(f), res[os.path.basename(f)]["verdict"], "open_a" if opena else "no_open_a", [o["text"] for o in out], flush=True)
json.dump(res, open(f"{O}/asr_check_v3_rvc.json","w"), ensure_ascii=False, indent=1)
