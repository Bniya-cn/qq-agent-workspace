import sys, glob, os, json
sys.path.insert(0, os.path.dirname(__file__))
from audio_metrics import metrics
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v3_rvc"
S=f"{W}/bot/data/test_output/exp_gagged_v2/src_nospeech"
files=sorted(glob.glob(f"{S}/gagged_[12]_bs_vocals_nospeech.wav"))+[f"{S}/gagged_3_bs_vocals_nospeech_v2.wav"]+sorted(glob.glob(f"{O}/rvc_*.wav"))
import numpy as np, soundfile as sf, librosa
SRC={"1":f"{S}/gagged_1_bs_vocals_nospeech.wav","2":f"{S}/gagged_2_bs_vocals_nospeech.wav","3":f"{S}/gagged_3_bs_vocals_nospeech_v2.wav"}
def env(p):
    y,sr=sf.read(p,always_2d=True); y=librosa.resample(y.mean(1).astype(np.float32),orig_sr=sr,target_sr=16000)
    r=librosa.feature.rms(y=y,frame_length=640,hop_length=160)[0]
    return np.convolve(r,np.ones(5)/5,mode="same")
def envcorr(a,b):
    ea,eb=env(a),env(b); n=min(len(ea),len(eb)); return float(np.corrcoef(ea[:n],eb[:n])[0,1])
rows=[]
for f in files:
    if not os.path.exists(f): continue
    m=metrics(f, fmin=100, fmax=900); m["file"]=os.path.basename(f)
    b=m["file"]; m["env_corr"]=round(envcorr(f,SRC[b.split("_g")[1][0]]),3) if b.startswith("rvc_") else 1.0
    rows.append(m)
keys=["file","dur","lt1200","b1200_3k","gt3k","cenP","cenF","f0med","voiced_pct","peak_db","env_corr"]
with open(f"{O}/metrics_v3_rvc.tsv","w") as fh:
    fh.write("\t".join(keys)+"\n")
    for r in rows: fh.write("\t".join(str(r.get(k)) for k in keys)+"\n")
json.dump(rows, open(f"{O}/metrics_v3_rvc.json","w"), ensure_ascii=False, indent=1, default=float)
for r in rows: print("\t".join(str(r.get(k)) for k in keys))
