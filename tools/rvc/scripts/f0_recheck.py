# re-measure F0 with a narrow pyin range (150-500 Hz) to avoid octave/harmonic errors on muffled hums; adds f0r/voiced_r to eval.json
import json, numpy as np, soundfile as sf, librosa, sys
from concurrent.futures import ProcessPoolExecutor
P="/Users/daijinglin/Desktop/qq-agent-workspace/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya/eval.json"
def f0(p):
    y,sr=sf.read(p,always_2d=True); y=librosa.resample(y.mean(1).astype(np.float32),orig_sr=sr,target_sr=16000)
    f,v,_=librosa.pyin(y,fmin=150,fmax=500,sr=16000,frame_length=1024)
    return (round(float(np.nanmedian(f[v])),1) if v.sum()>=5 else None, round(float(v.mean())*100,1))
if __name__=="__main__":
    E=json.load(open(P)); items=[(k,b,m["path"]) for k in ("inputs","outputs") for b,m in E[k].items() if "f0r" not in m]
    with ProcessPoolExecutor(4) as ex:
        for (k,b,_),(a,v) in zip(items, ex.map(f0,[p for *_,p in items])): E[k][b]["f0r"]=a; E[k][b]["voiced_r"]=v
    json.dump(E,open(P,"w"),ensure_ascii=False,indent=0,default=float); print("done",len(items))
