# v8: build GPT-SoVITS reference = concatenated GENUINE hum syllables (嗯/哼/唔/呣/恩) from official clips (native SR). rvc venv.
import os, json, glob, numpy as np, soundfile as sf, librosa
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O6=f"{W}/bot/data/test_output/exp_gagged_v6_nasal"; O=f"{W}/bot/data/test_output/exp_gagged_v8_gen/sovits_ref"; os.makedirs(O,exist_ok=True)
TS={}
for f in ["genuine_nasal_ts.json","genuine_nasal_ts2.json"]: TS.update(json.load(open(f"{O6}/{f}")))
SP=json.load(open(f"{O6}/genuine_nasal_spans_v8.json")) if os.path.exists(f"{O6}/genuine_nasal_spans_v8.json") else {}
info={}
for ch in ["dania","amiya"]:
    hums=[(h["p"],f,h) for f,v in TS.items() if v["char"]==ch for h in v["hits"] if h["c"] in "嗯呣唔哼恩" and h["p"]>=0.3]
    hums.sort(key=lambda x:-x[0]); segs=[]; sr0=None; tot=0; used=[]
    for p,f,h in hums:
        y,sr=sf.read(f,always_2d=True); y=y.mean(1); sr0=sr0 or sr
        if sr!=sr0: y=librosa.resample(y,orig_sr=sr,target_sr=sr0)
        a=int(max(0,h["start"]-0.03)*sr0); b=int((h["end"]+0.06)*sr0); s=y[a:b]
        if len(s)<0.12*sr0: continue
        fd=int(0.01*sr0); s=s.copy(); s[:fd]*=np.linspace(0,1,fd); s[-fd:]*=np.linspace(1,0,fd)
        segs.append(s); used.append(dict(file=os.path.basename(f),c=h["c"],start=h["start"],end=h["end"],kind="asr_hum")); tot+=len(s)/sr0+0.12
        if tot>=6.5: break
    if tot<4.0:   # top up with the longest acoustic nasal-murmur runs
        runs=sorted(SP.get(ch,[]),key=lambda s:-(s["t1"]-s["t0"]))
        for r in runs:
            if r["t1"]-r["t0"]<0.15: break
            f=f"{W}/tools/rvc/datasets/{ch}/{r['file']}"; y,sr=sf.read(f,always_2d=True); y=y.mean(1)
            if sr0 is None: sr0=sr
            if sr!=sr0: y=librosa.resample(y,orig_sr=sr,target_sr=sr0)
            s=y[int(r["t0"]*sr0):int(r["t1"]*sr0)].copy(); fd=int(0.01*sr0); s[:fd]*=np.linspace(0,1,fd); s[-fd:]*=np.linspace(1,0,fd)
            segs.append(s); used.append(dict(file=r["file"],t0=r["t0"],t1=r["t1"],kind="acoustic_murmur")); tot+=len(s)/sr0+0.12
            if tot>=6.0: break
    gap=np.zeros(int(0.12*sr0)); z=np.concatenate([np.r_[s,gap] for s in segs]); z=z/(np.max(np.abs(z))+1e-9)*0.9
    out=f"{O}/{ch}_hum_ref_v8.wav"; assert not os.path.exists(out); sf.write(out,z.astype(np.float32),sr0)
    info[ch]=dict(file=out,dur=round(len(z)/sr0,2),n_asr_hum=sum(u["kind"]=="asr_hum" for u in used),n_murmur=sum(u["kind"]!="asr_hum" for u in used),segments=used)
    print(ch,info[ch]["dur"],"s",info[ch]["n_asr_hum"],"hums",info[ch]["n_murmur"],"murmurs",[u.get("c","m") for u in used])
json.dump(info,open(f"{O}/hum_ref_v8.json","w"),ensure_ascii=False,indent=0)
