# v3 variants from the v0 closed_emo recipe (less dull/thin). never overwrites.
import os, sys, json, glob, random
sys.path.insert(0,os.path.dirname(__file__)); sys.path.insert(0,"/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
import torch, torchaudio
import numpy as np, soundfile as sf, librosa, pyworld as pw
import closed_lib as C
from audio_metrics import metrics
from modules.campplus.DTDNN import CAMPPlus
from resemblyzer import VoiceEncoder, preprocess_wav
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v5_closed"; R=f"{W}/tools/rvc"
INP=[("dania","C_v4_L2_Ab_eqT0.06"),("amiya","C_rvc_lines_L2_cn008"),("dania","A_rvc_g2_top1"),("amiya","A_rvc_g2_top1")]
BASE=dict(emo=0.25,env_mode="morph",warp_on=True,attack_ms=25,hf_onset_db=12.0,nasal2k=0.0,whine_notch=True)
VAR={"v3a":dict(alpha_lo=.5,alpha_hi=.5,roll=8,notch=14,ap_scale=.7,uv_gain=.5,lowmid_db=2,pres_db=2),
     "v3b":dict(alpha_lo=.4,alpha_hi=.4,roll=6,notch=12,ap_scale=.8,uv_gain=.5,lowmid_db=3,pres_db=2),
     "v3c":dict(alpha_lo=.6,alpha_hi=.6,roll=10,notch=16,ap_scale=.6,uv_gain=.5,lowmid_db=2,pres_db=3),
     "v3d":dict(alpha_lo=.5,alpha_hi=.6,roll=6,notch=12,ap_scale=.7,uv_gain=.5,lowmid_db=4,pres_db=1,post_onset=True,hf_excess_db=4,attack_post_ms=20)}
torch.set_num_threads(4); VE=VoiceEncoder('cpu',verbose=False)
cm=CAMPPlus(feat_dim=80,embedding_size=192); cm.load_state_dict(torch.load(f"{W}/tools/seed-vc/checkpoints/campplus_cn_common.bin",map_location="cpu")); cm.eval()
def l16(p):
    y,s=sf.read(p,always_2d=True); y=y.mean(1).astype(np.float32); return librosa.resample(y,orig_sr=s,target_sr=16000) if s!=16000 else y
def emb(y):
    f=torchaudio.compliance.kaldi.fbank(torch.tensor(y)[None],num_mel_bins=80,dither=0,sample_frequency=16000); f=f-f.mean(0,keepdim=True)
    with torch.no_grad(): return torch.nn.functional.normalize(cm(f[None])[0],dim=0).numpy()
def remb(y):
    w=preprocess_wav(y,source_sr=16000); w=w if len(w)>=8000 else y/(np.max(np.abs(y))+1e-9); e=VE.embed_utterance(w); return e/np.linalg.norm(e)
def env(y): r=librosa.feature.rms(y=y,frame_length=640,hop_length=160)[0]; return np.convolve(r,np.ones(5)/5,mode="same")
def f0c(y): f,_=pw.harvest(y.astype(np.float64),16000,f0_floor=70,f0_ceil=1000,frame_period=10); return f
random.seed(0); cent={}; rc={}
for ch in ["amiya","dania"]:
    fs=[f for f in sorted(glob.glob(f"{R}/datasets/{ch}/*.wav")) if sf.info(f).duration>=3.0]; ys=[l16(f) for f in random.sample(fs,min(30,len(fs)))]
    c=np.mean([emb(y) for y in ys],0); cent[ch]=c/np.linalg.norm(c); r=np.mean([remb(y) for y in ys],0); rc[ch]=r/np.linalg.norm(r)
rows=[]
for ch,tag in INP:
    oc="dania" if ch=="amiya" else "amiya"; before=f"{O}/{ch}_{tag}__before.wav"
    files=[("before",before),("v0_closed_emo",f"{O}/{ch}_{tag}__closed_emo.wav")]
    for vn,kw in VAR.items():
        p=f"{O}/{ch}_{tag}__{vn}.wav"
        if not os.path.exists(p): C.closed(before,p,**{**BASE,**kw})
        files.append((vn,p))
    yb=l16(before); eb=env(yb); fb=f0c(yb)
    for vn,p in files:
        y=l16(p); m=metrics(p,fmin=100,fmax=900); d=C.diag(p); rr=C.richness(p); e=emb(y); r=remb(y); ev=env(y); k=min(len(ev),len(eb)); fa=f0c(y); n=min(len(fa),len(fb)); vv=(fa[:n]>0)&(fb[:n]>0)
        row=dict(char=ch,input=tag,variant=vn,file=os.path.basename(p),lt1200=m["lt1200"],gt3k=m["gt3k"],hnr=d["hnr_med_db"],ap=d["ap_mean_0_4k"],F1=d["F1_med_hz"],e5_10=d["e500_1000_of_0_1000_pct"],
                 bursts=d["bursts_per_s"],f0=d.get("f0_med"),f0_range=d.get("f0_range_st_p5_95"),rms_dyn=d.get("rms_dyn_db_p10_95"),**rr,
                 cam_char=round(float(e@cent[ch]),3),cam_other=round(float(e@cent[oc]),3),res_char=round(float(r@rc[ch]),3),res_other=round(float(r@rc[oc]),3),
                 env_corr=round(float(np.corrcoef(ev[:k],eb[:k])[0,1]),3),f0_corr=round(float(np.corrcoef(np.log(fa[:n][vv]),np.log(fb[:n][vv]))[0,1]),3) if vv.sum()>10 else None)
        rows.append(row); print(row["file"],"lt",row["lt1200"],"tilt",row["tilt_db_per_oct"],"2.5-5k",row["e2500_5000"],"1-2.5k",row["e1000_2500"],"whine",row["whine_max_prom_db"],"ap",row["ap"],"cam",row["cam_char"],"res",row["res_char"],"env",row["env_corr"],flush=True)
json.dump(rows,open(f"{O}/v3_metrics.json","w"),ensure_ascii=False,indent=0,default=float); print("WROTE",len(rows))
