# asr-venv: char-level timestamps for nasal-initial syllables in GENUINE character clips (datasets only). usage: nasal_ts.py out.json
import glob, json, os, sys
from faster_whisper import WhisperModel
R="/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc/datasets"
NAS=set("嗯嗯呣唔吗嘛妈马们么没每美门梦米面民明名母木目慢忙猫你呢那哪能年您男难内女牛怒脑闹拿宁弄农娘")
out=sys.argv[1]; res=json.load(open(out)) if os.path.exists(out) else {}
m=WhisperModel("small",device="cpu",compute_type="int8",cpu_threads=4)
for ch in ["dania","amiya"]:
    for f in sorted(glob.glob(f"{R}/{ch}/*.wav"))[::2][:40]:
        if f in res: continue
        segs,_=m.transcribe(f,language="zh",beam_size=5,word_timestamps=True,condition_on_previous_text=False,vad_filter=False)
        hits=[]
        for s in segs:
            for w in (s.words or []):
                t=w.word.strip(); n=len(t)
                if not n: continue
                d=(w.end-w.start)/n
                for i,c in enumerate(t):
                    if c in NAS: hits.append(dict(c=c,start=round(w.start+i*d,3),end=round(w.start+(i+1)*d,3),p=round(w.probability,3)))
        res[f]=dict(char=ch,hits=hits); print(os.path.basename(f),len(hits),flush=True)
        if len(res)%10==0: json.dump(res,open(out,"w"),ensure_ascii=False,indent=0)
json.dump(res,open(out,"w"),ensure_ascii=False,indent=0); print("NAS_TS DONE")
