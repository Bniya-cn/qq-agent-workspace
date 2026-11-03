import os, sys, json, glob, random
sys.path.insert(0,os.path.dirname(__file__)); sys.path.insert(0,"/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
import torch, torchaudio
import numpy as np, soundfile as sf, librosa
import closed_lib as C
from modules.campplus.DTDNN import CAMPPlus
from resemblyzer import VoiceEncoder, preprocess_wav
W="/Users/daijinglin/Desktop/qq-agent-workspace"; R=f"{W}/tools/rvc"; os.makedirs("/tmp/v5sweep",exist_ok=True)
torch.set_num_threads(4); VE=VoiceEncoder('cpu',verbose=False)
cm=CAMPPlus(feat_dim=80,embedding_size=192); cm.load_state_dict(torch.load(f"{W}/tools/seed-vc/checkpoints/campplus_cn_common.bin",map_location="cpu")); cm.eval()
def l16(p): y,s=sf.read(p,always_2d=True); y=y.mean(1).astype(np.float32); return librosa.resample(y,orig_sr=s,target_sr=16000) if s!=16000 else y
def emb(y):
    f=torchaudio.compliance.kaldi.fbank(torch.tensor(y)[None],num_mel_bins=80,dither=0,sample_frequency=16000); f=f-f.mean(0,keepdim=True)
    with torch.no_grad(): return torch.nn.functional.normalize(cm(f[None])[0],dim=0).numpy()
def remb(y):
    w=preprocess_wav(y,source_sr=16000); w=w if len(w)>=8000 else y/(np.max(np.abs(y))+1e-9); e=VE.embed_utterance(w); return e/np.linalg.norm(e)
random.seed(0); cent={}; rc={}
for ch in ["amiya","dania"]:
    fs=[f for f in sorted(glob.glob(f"{R}/datasets/{ch}/*.wav")) if sf.info(f).duration>=3.0]; ys=[l16(f) for f in random.sample(fs,min(30,len(fs)))]
    c=np.mean([emb(y) for y in ys],0); cent[ch]=c/np.linalg.norm(c); r=np.mean([remb(y) for y in ys],0); rc[ch]=r/np.linalg.norm(r)
FILES=json.loads(sys.argv[1]); CFG=json.loads(sys.argv[2])
for ch,src in FILES:
    for name,kw in CFG.items():
        p=f"/tmp/v5sweep/{ch}_{name}.wav"
        if name=="orig": p=src
        else: C.closed(src,p,**kw)
        y=l16(p); d=C.diag(p); e=emb(y); r=remb(y); oc="dania" if ch=="amiya" else "amiya"
        print(ch,name,"cam",round(float(e@cent[ch]),3),round(float(e@cent[oc]),3),"res",round(float(r@rc[ch]),3),"hnr",d["hnr_med_db"],"ap",d["ap_mean_0_4k"],"burst",d["bursts_per_s"],"onsetHF",d["onset_hf_excess_db_med"],"F1",d["F1_med_hz"],"e5-10",d["e500_1000_of_0_1000_pct"],"notch",d["notch_depth_db"],"lt1200",d["lt1200_pct_world"],flush=True)
