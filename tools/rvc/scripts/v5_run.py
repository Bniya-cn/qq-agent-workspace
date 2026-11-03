# v5 closed-mouth A/B: before copies + closed / closed_emo afters, diagnostics, similarity, env/F0 follow
import os, sys, json, glob, random, shutil
sys.path.insert(0,os.path.dirname(__file__)); sys.path.insert(0,"/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
import torch, torchaudio  # torch first
import numpy as np, soundfile as sf, librosa, pyworld as pw
import closed_lib as C
from audio_metrics import metrics
from modules.campplus.DTDNN import CAMPPlus
from resemblyzer import VoiceEncoder, preprocess_wav
W="/Users/daijinglin/Desktop/qq-agent-workspace"; T=f"{W}/bot/data/test_output"; O=f"{T}/exp_gagged_v5_closed"; R=f"{W}/tools/rvc"
F=f"{T}/exp_gagged_v3_rvc/fidelity"; V=f"{T}/exp_gagged_v4_lines"; L=f"{T}/exp_gagged_v3_rvc/lines_v4_amiya"; S2=f"{T}/exp_gagged_v2"
IN={"dania":[("A_rvc_g2_top1",f"{F}/rvc_dania_g2_40e_p-13_ir0.6_pr0.5_rms0.25.wav"),("B_rvc_g2_dp0",f"{F}/rvc_dania_g2_40e_p-16_ir0.6_pr0.5_rms0.25.wav"),("C_v4_L2_Ab_eqT0.06",f"{V}/muffled/dania_L2_s42_Ab_eqT0.06.wav")],
    "amiya":[("A_rvc_g2_top1",f"{F}/rvc_amiya_g2_40e_p-12_ir0.4_pr0.33_rms0.25.wav"),("B_seedvc_g2bs_final",f"{S2}/vc_final_g123_orig/vc_amiya_g2bs_bal_s-12_cfg0.5_st40_envdyn_final.wav"),("C_rvc_lines_L2_cn008",f"{L}/rvc_amiya_L2_s7_Ab_eqT0.06_cn008__60e_p-1_ir0.6_pr0.5_rms0.25.wav")]}
VAR={"closed":dict(emo=0.0),"closed_emo":dict(emo=0.25)}
torch.set_num_threads(4); VE=VoiceEncoder('cpu',verbose=False)
cm=CAMPPlus(feat_dim=80,embedding_size=192); cm.load_state_dict(torch.load(f"{W}/tools/seed-vc/checkpoints/campplus_cn_common.bin",map_location="cpu")); cm.eval()
def l16(p): y,s=sf.read(p,always_2d=True); y=y.mean(1).astype(np.float32); return librosa.resample(y,orig_sr=s,target_sr=16000) if s!=16000 else y
def emb(y):
    f=torchaudio.compliance.kaldi.fbank(torch.tensor(y)[None],num_mel_bins=80,dither=0,sample_frequency=16000); f=f-f.mean(0,keepdim=True)
    with torch.no_grad(): return torch.nn.functional.normalize(cm(f[None])[0],dim=0).numpy()
def remb(y):
    w=preprocess_wav(y,source_sr=16000)
    if len(w)<8000: w=y/(np.max(np.abs(y))+1e-9)
    e=VE.embed_utterance(w); return e/np.linalg.norm(e)
random.seed(0); cent={}; rc={}
for ch in ["amiya","dania"]:
    fs=[f for f in sorted(glob.glob(f"{R}/datasets/{ch}/*.wav")) if sf.info(f).duration>=3.0]; ys=[l16(f) for f in random.sample(fs,min(30,len(fs)))]
    c=np.mean([emb(y) for y in ys],0); cent[ch]=c/np.linalg.norm(c); r=np.mean([remb(y) for y in ys],0); rc[ch]=r/np.linalg.norm(r)
def env(y): r=librosa.feature.rms(y=y,frame_length=640,hop_length=160)[0]; return np.convolve(r,np.ones(5)/5,mode="same")
def f0c(y): f,t=pw.harvest(y.astype(np.float64),16000,f0_floor=70,f0_ceil=1000,frame_period=10); return f
rows=[]
for ch,lst in IN.items():
    oc="dania" if ch=="amiya" else "amiya"
    for tag,src in lst:
        before=f"{O}/{ch}_{tag}__before.wav"
        if not os.path.exists(before): shutil.copy2(src,before)
        outs=[("before",before,{})]
        for vn,kw in VAR.items():
            p=f"{O}/{ch}_{tag}__{vn}.wav"; info={}
            if not os.path.exists(p): info=C.closed(src,p,**kw)
            outs.append((vn,p,info))
        yb=l16(before); eb=env(yb); fb=f0c(yb)
        for vn,p,info in outs:
            y=l16(p); m=metrics(p,fmin=100,fmax=900); d=C.diag(p); e=emb(y); r=remb(y); ev=env(y); k=min(len(ev),len(eb)); fa=f0c(y); n=min(len(fa),len(fb)); vv=(fa[:n]>0)&(fb[:n]>0)
            row=dict(char=ch,input=tag,variant=vn,file=os.path.basename(p),src=src,**{f"m_{a}":m[a] for a in ["lt1200","gt3k","cenP","peak_db"]},**d,
                cam_char=round(float(e@cent[ch]),3),cam_other=round(float(e@cent[oc]),3),res_char=round(float(r@rc[ch]),3),res_other=round(float(r@rc[oc]),3),
                env_corr_vs_before=round(float(np.corrcoef(ev[:k],eb[:k])[0,1]),3),
                f0_contour_corr_vs_before=round(float(np.corrcoef(np.log(fa[:n][vv]),np.log(fb[:n][vv]))[0,1]),3) if vv.sum()>10 else None, **info)
            row["cam_margin"]=round(row["cam_char"]-row["cam_other"],3); row["res_margin"]=round(row["res_char"]-row["res_other"],3)
            rows.append(row); print(row["file"],row["hnr_med_db"],row["bursts_per_s"],row["F1_med_hz"],row["e500_1000_of_0_1000_pct"],row["cam_char"],row["res_char"],flush=True)
json.dump(rows,open(f"{O}/ab_metrics.json","w"),ensure_ascii=False,indent=0,default=float)
