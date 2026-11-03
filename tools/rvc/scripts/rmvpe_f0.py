# RMVPE (same F0 estimator RVC uses) median F0 for inputs/outputs; adds f0rm/voiced_rm to eval.json; writes jobs_rmvpe.json for missing ideal-transpose outputs
import sys, os, json, math, glob, numpy as np, soundfile as sf, librosa
A="/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc/applio"; sys.path.insert(0,A)
from rvc.lib.predictors.RMVPE import RMVPE0Predictor
O="/Users/daijinglin/Desktop/qq-agent-workspace/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya"; P=f"{O}/eval.json"
M=RMVPE0Predictor(f"{A}/rvc/models/predictors/rmvpe.pt", device="cpu")
def f0(p):
    y,sr=sf.read(p,always_2d=True); y=librosa.resample(y.mean(1).astype(np.float32),orig_sr=sr,target_sr=16000)
    f=M.infer_from_audio(y,thred=0.03); v=f[f>0]
    return (round(float(np.median(v)),1) if len(v)>=5 else None, round(len(v)/max(len(f),1)*100,1))
E=json.load(open(P))
for k in ("inputs","outputs"):
    for b,m in E[k].items():
        if "f0rm" not in m: m["f0rm"],m["voiced_rm"]=f0(m["path"])
# also any newly rendered outputs not yet evaluated are handled by eval_lines later
json.dump(E,open(P,"w"),ensure_ascii=False,indent=0,default=float)
jl=json.load(open(f"{O}/jobs.json"))+(json.load(open(f"{O}/jobs_fix.json")) if os.path.exists(f"{O}/jobs_fix.json") else [])
have={os.path.basename(j["out"]) for j in jl}; base={}
for j in json.load(open(f"{O}/jobs.json")): base.setdefault(os.path.basename(j["src"]),[]).append(j)
new=[]; ideal={}
for b,js in base.items():
    fi=E["inputs"][b]["f0rm"]
    if fi is None: continue
    p=round(12*math.log2(292/fi)); ideal[b]=p
    for j in js:
        o=j["out"].replace(f"_p{j['pitch']:+d}_",f"_p{p:+d}_")
        if os.path.basename(o) not in have: nj=dict(j); nj["pitch"]=p; nj["out"]=o; new.append(nj); have.add(os.path.basename(o))
json.dump(new,open(f"{O}/jobs_rmvpe.json","w"),indent=0); json.dump(ideal,open(f"{O}/ideal_pitch_rmvpe.json","w"),indent=0)
print("inputs rmvpe:",{b:E["inputs"][b]["f0rm"] for b in list(E["inputs"])[:8]}); print(len(new),"new jobs")
