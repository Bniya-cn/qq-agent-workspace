# v8: nasal-only RVC retrieval index per character from GENUINE official clips only (acoustic nasal-murmur detector + ASR spans).
import os, sys, json, glob
A="/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc/applio"; sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import torch
import numpy as np, nasal_lib as N
os.chdir(A); sys.path.insert(0,A)
from rvc.lib.utils import load_embedding
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O6=f"{W}/bot/data/test_output/exp_gagged_v6_nasal"; R=f"{W}/tools/rvc/datasets"
TS={}
for f in ["genuine_nasal_ts.json","genuine_nasal_ts2.json"]:
    if os.path.exists(f"{O6}/{f}"): TS.update(json.load(open(f"{O6}/{f}")))
model=load_embedding("contentvec").float().eval()
spans={"dania":[],"amiya":[]}; feats={"dania":[],"amiya":[]}
for ch in spans:
    for f in sorted(glob.glob(f"{R}/{ch}/*.wav")):
        y=N.load16(f); F=N.frames(y); f0=N.voiced_mask(y); E,Pw=N.cep_env(F); e=N._db(Pw.sum(1)); act=e>np.percentile(e,95)-28
        tot=Pw[:,(N.FR>=50)&(N.FR<4000)].sum(1); lt5=Pw[:,(N.FR>=50)&(N.FR<500)].sum(1)/tot; e510=Pw[:,(N.FR>=500)&(N.FR<1000)].sum(1)/Pw[:,(N.FR>=50)&(N.FR<1000)].sum(1)
        hvn=E[:,(N.FR>=1300)&(N.FR<2000)].mean(1)-E[:,(N.FR>=700)&(N.FR<1000)].mean(1)
        m=(f0>0)&act&(lt5>=0.85)&(e510<=0.06)&(hvn<=-5)
        t=(np.arange(len(F))*N.HOP+N.WIN/2)/N.SR
        # ASR nasal-initial / hum spans (acoustically relaxed: voiced, lt500>=0.7)
        for h in TS.get(f,{}).get("hits",[]):
            if h["p"]<0.3: continue
            hum=h["c"] in "嗯呣唔哼恩"; w=(t>=h["start"]-0.04)&(t<=(h["end"] if hum else h["start"]+0.12))
            m|=w&(f0>0)&act&(lt5>=0.7)
        # runs >= 40 ms (8 frames), bridge gaps <= 10 ms
        idx=np.where(m)[0]; runs=[]
        if len(idx):
            s=idx[0]; p=idx[0]
            for k in idx[1:]:
                if k-p>2: runs.append((s,p)); s=k
                p=k
            runs.append((s,p))
        runs=[(a,b) for a,b in runs if b-a+1>=8]
        if not runs: continue
        with torch.no_grad():
            fv=model(torch.from_numpy(y.astype(np.float32))[None])["last_hidden_state"][0].numpy()   # (T,768), 20 ms hop
        tf=(np.arange(len(fv))*320+200)/16000.0
        for a,b in runs:
            t0,t1=t[a]-0.0125,t[b]+0.0125; spans[ch].append(dict(file=os.path.basename(f),t0=round(float(t0),3),t1=round(float(t1),3)))
            sel=(tf>=t0)&(tf<=t1); feats[ch].append(fv[sel])
    X=np.concatenate(feats[ch]).astype(np.float32); dur=sum(s["t1"]-s["t0"] for s in spans[ch])
    n=len(X); out="(faiss built separately by v8_build_faiss.py; torch+faiss IVF segfaults in one process)"; np.save(f"{O6}/{ch}_nasal_feats_v8.npy",X)
    print(ch,"spans",len(spans[ch]),"dur_s",round(dur,1),"vectors",n,"->",out,flush=True)
json.dump(spans,open(f"{O6}/genuine_nasal_spans_v8.json","w"),ensure_ascii=False,indent=0)
print("NASAL_INDEX DONE")
