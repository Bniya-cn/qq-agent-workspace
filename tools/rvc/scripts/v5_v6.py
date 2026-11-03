# v6: residual-excited closed-tract nasal re-filtering (no vocoder). never overwrites.
import os, sys, json, glob, random
sys.path.insert(0,os.path.dirname(__file__)); sys.path.insert(0,"/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
import torch, torchaudio
import numpy as np, soundfile as sf, librosa, pyworld as pw
import closed_lib as C, buzz_lib as B, nasal_lib as N
from audio_metrics import metrics
from modules.campplus.DTDNN import CAMPPlus
from resemblyzer import VoiceEncoder, preprocess_wav
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v5_closed"; R=f"{W}/tools/rvc"
INP=[("dania","C_v4_L2_Ab_eqT0.06"),("amiya","C_rvc_lines_L2_cn008"),("dania","A_rvc_g2_top1"),("amiya","A_rvc_g2_top1")]
V6={"v6a":dict(w_nasal=0.7,af_bw=350,lp_hz=2000,mix=0.15),
    "v6b":dict(w_nasal=0.85,af_bw=300,lp_hz=1800,mix=0.0),
    "v6c":dict(w_nasal=0.6,af_bw=400,lp_hz=2500,mix=0.3),
    "v6d":dict(w_nasal=0.7,af_bw=350,lp_hz=2000,mix=0.15,air=0.08),
    "v6e":dict(w_nasal=0.5,af_bw=400,lp_hz=2500,mix=0.3)}
AFHZ={"dania":1450.0,"amiya":1550.0}  # genuine nasal mean-profile valley
BEST4={"dania_A_rvc_g2_top1":"v4b","dania_C_v4_L2_Ab_eqT0.06":"v4c","amiya_A_rvc_g2_top1":"v4c","amiya_C_rvc_lines_L2_cn008":"v4c"}
TR=json.load(open(f"{W}/bot/data/test_output/exp_gagged_v6_nasal/nasal_tract_fit.json")); S1=json.load(open(f"{W}/bot/data/test_output/exp_gagged_v6_nasal/closed_cues_step1.json"))
STAGES=["before","v4a","v4b","v4c","v6a","v6b","v6c","v6d","v6e"]
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
    for vn,kw in V6.items():
        p=f"{O}/{ch}_{tag}__{vn}.wav"
        if not os.path.exists(p): N.v6(before,p,np.array(TR[ch]["a"]),af_hz=AFHZ[ch],mix_path=f"{O}/{ch}_{tag}__{BEST4[ch+'_'+tag]}.wav",emo=0.2,**kw)
    yb=l16(before); eb=env(yb); fb=f0c(yb)
    for vn in STAGES:
        p=f"{O}/{ch}_{tag}__{vn}.wav"
        if not os.path.exists(p): continue
        y=l16(p); m=metrics(p,fmin=100,fmax=900); d=C.diag(p); bz=B.buzz(p); e=emb(y); r=remb(y); ev=env(y); k=min(len(ev),len(eb)); fa=f0c(y); n=min(len(fa),len(fb)); vv=(fa[:n]>0)&(fb[:n]>0)
        row=dict(char=ch,input=tag,variant=vn,file=os.path.basename(p),lt1200=m["lt1200"],gt3k=m["gt3k"],hnr=d["hnr_med_db"],ap=d["ap_mean_0_4k"],F1=d["F1_med_hz"],e5_10=d["e500_1000_of_0_1000_pct"],
                 bursts=d["bursts_per_s"],onset_hf=d.get("onset_hf_excess_db_med"),f0_range=d.get("f0_range_st_p5_95"),rms_dyn=d.get("rms_dyn_db_p10_95"),**{f"bz_{a}":b for a,b in bz.items()},
                 cam_char=round(float(e@cent[ch]),3),cam_other=round(float(e@cent[oc]),3),res_char=round(float(r@rc[ch]),3),res_other=round(float(r@rc[oc]),3),
                 **{('nc_'+k2):v2 for k2,v2 in N.file_cues(p).items() if k2!='profile'},nc_dist=N.profile_dist(N.file_cues(p)['profile'],S1['genuine_nasal_'+ch]['profile']),env_corr=round(float(np.corrcoef(ev[:k],eb[:k])[0,1]),3),f0_corr=round(float(np.corrcoef(np.log(fa[:n][vv]),np.log(fb[:n][vv]))[0,1]),3) if vv.sum()>10 else None)
        rows.append(row); print(row["file"],"F0mod",row["bz_hf_mod_F0_share"],"cpp",row["bz_hf_comb_cpp_db"],"z50",row["bz_hf_mod_50_200_share"],"line",row["bz_mod_line_hz"],row["bz_mod_line_prom_db"],"jit",row["bz_jitter_st_p95"],"oct",row["bz_octave_jumps"],"dist",row["nc_dist"],"F2sd",row["nc_F2_std_hz"],"AF",row["nc_AF_depth_db"],"lt",row["lt1200"],"e510",row["e5_10"],"cam",row["cam_char"],"res",row["res_char"],flush=True)
json.dump(rows,open(f"{O}/v6_metrics.json","w"),ensure_ascii=False,indent=0,default=float); print("WROTE",len(rows))
