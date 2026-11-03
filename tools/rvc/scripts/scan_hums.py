# find existing character-voiced muffled TTS hums (closed-mouth) produced earlier by GPT-SoVITS gagged chain
import glob, os, json, sys
sys.path.insert(0, os.path.dirname(__file__))
from audio_metrics import metrics
W="/Users/daijinglin/Desktop/qq-agent-workspace"; G=f"{W}/gpt-sovits"
cands={"amiya": glob.glob(f"{G}/output/tts_*阿米娅.wav")+glob.glob(f"{G}/output/amiya_tape*.wav")+[f"{G}/models/amiya/refs/nuclei_bank/clean_001529.wav",f"{G}/models/amiya/refs/nuclei_bank/clean_001713.wav"],
       "dania": glob.glob(f"{G}/output/tts_*达妮娅.wav")+glob.glob(f"{G}/output/dania_*tape*.wav")+glob.glob(f"{G}/models/dania/refs/nuclei_bank/s01_*.wav")}
res={}
for ch,fs in cands.items():
    rows=[]
    for f in sorted(set(fs)):
        try: m=metrics(f, fmin=100, fmax=900)
        except Exception as e: continue
        m["file"]=f; rows.append(m)
    res[ch]=rows
    sel=[r for r in rows if r["lt1200"]>=80 and r["gt3k"]<=5 and (r["f0med"] or 0)<450 and r["dur"]>=1.0]
    print(ch, len(rows), "candidates;", len(sel), "muffled-looking", round(sum(r["dur"] for r in sel),1), "s")
json.dump(res, open(f"{W}/tools/rvc/datasets/hum_scan.json","w"), ensure_ascii=False, indent=1, default=float)
