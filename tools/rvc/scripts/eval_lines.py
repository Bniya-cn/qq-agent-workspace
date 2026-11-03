# eval v4->RVC amiya outputs + their inputs. usage: eval_lines.py out.json
import sys, os, glob, json, re, random
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0,"/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
import torch, torchaudio, numpy as np, soundfile as sf, librosa
from audio_metrics import metrics
from modules.campplus.DTDNN import CAMPPlus
from resemblyzer import VoiceEncoder, preprocess_wav
torch.set_num_threads(4); VE=VoiceEncoder('cpu', verbose=False)
W="/Users/daijinglin/Desktop/qq-agent-workspace"; R=f"{W}/tools/rvc"; O=f"{W}/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya"; V=f"{W}/bot/data/test_output/exp_gagged_v4_lines"
cm=CAMPPlus(feat_dim=80, embedding_size=192); cm.load_state_dict(torch.load(f"{W}/tools/seed-vc/checkpoints/campplus_cn_common.bin", map_location="cpu")); cm.eval()
def load16(p):
    y,sr=sf.read(p, always_2d=True); y=y.mean(1).astype(np.float32)
    return librosa.resample(y,orig_sr=sr,target_sr=16000) if sr!=16000 else y
def emb(y):
    f=torchaudio.compliance.kaldi.fbank(torch.tensor(y)[None],num_mel_bins=80,dither=0,sample_frequency=16000); f=f-f.mean(0,keepdim=True)
    with torch.no_grad(): return torch.nn.functional.normalize(cm(f[None])[0],dim=0)
def remb(y):
    w=preprocess_wav(y, source_sr=16000)
    if len(w)<8000: w=y/(np.max(np.abs(y))+1e-9)
    e=VE.embed_utterance(w); return e/np.linalg.norm(e)
def env(y):
    r=librosa.feature.rms(y=y,frame_length=640,hop_length=160)[0]; return np.convolve(r,np.ones(5)/5,mode="same")
random.seed(0); cent={}; rcent={}
for ch in ["amiya","dania"]:
    fs=[f for f in sorted(glob.glob(f"{R}/datasets/{ch}/*.wav")) if sf.info(f).duration>=3.0]
    ys=[load16(f) for f in random.sample(fs,min(30,len(fs)))]
    cent[ch]=torch.nn.functional.normalize(torch.stack([emb(y) for y in ys]).mean(0),dim=0)
    rc=np.mean([remb(y) for y in ys],0); rcent[ch]=rc/np.linalg.norm(rc)
def ev(p, ref=None):
    m=metrics(p,fmin=100,fmax=900); y=load16(p); e=emb(y); r=remb(y)
    m.update(cam_amiya=round(float(e@cent["amiya"]),3), cam_dania=round(float(e@cent["dania"]),3), res_amiya=round(float(r@rcent["amiya"]),3), res_dania=round(float(r@rcent["dania"]),3))
    m["cam_margin"]=round(m["cam_amiya"]-m["cam_dania"],3); m["res_margin"]=round(m["res_amiya"]-m["res_dania"],3)
    if ref is not None:
        a=env(y); b=env(load16(ref)); k=min(len(a),len(b)); m["env_corr"]=round(float(np.corrcoef(a[:k],b[:k])[0,1]),3); m["cam_vs_input"]=round(float(e@emb(load16(ref))),3)
    return m
jl=json.load(open(f"{O}/jobs.json"))
for x in ("jobs_fix.json","jobs_rmvpe.json"):
    if os.path.exists(f"{O}/{x}"): jl+=json.load(open(f"{O}/{x}"))
jobs={os.path.basename(j["out"]):j for j in jl}
out=sys.argv[1]; old=J=json.load(open(out)) if os.path.exists(out) else {"inputs":{},"outputs":{}}
for j in jobs.values():
    b=os.path.basename(j["src"])
    if b not in old["inputs"]: old["inputs"][b]=dict(ev(j["src"]),path=j["src"]); print("IN",b,flush=True)
for b,j in jobs.items():
    p=j["out"]
    if b in old["outputs"] or not os.path.exists(p): continue
    m=ev(p,j["src"]); m.update(path=p,input=os.path.basename(j["src"]),pitch=j["pitch"],ir=j["ir"],ckpt=re.search(r"__(\d+e)_",b).group(1)); old["outputs"][b]=m; print("OUT",b,m["f0med"],m["lt1200"],m["cam_margin"],m["res_margin"],flush=True)
json.dump(old,open(out,"w"),ensure_ascii=False,indent=0,default=float)
