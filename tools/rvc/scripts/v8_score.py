# v8 scoring: raw generation outputs + light-EQ (__eq) versions. (rvc venv; torch first)
import os, sys, json, glob, random
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0,"/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
import torch, torchaudio
import numpy as np, soundfile as sf, librosa, pyworld as pw, scipy.signal as ss
import closed_lib as C, buzz_lib as B, nasal_lib as N, tv_lib as TV
from audio_metrics import metrics
from modules.campplus.DTDNN import CAMPPlus
from resemblyzer import VoiceEncoder, preprocess_wav
W="/Users/daijinglin/Desktop/qq-agent-workspace"; G=f"{W}/bot/data/test_output/exp_gagged_v8_gen"; R=f"{W}/tools/rvc"; V5=f"{W}/bot/data/test_output/exp_gagged_v5_closed"
S1=json.load(open(f"{W}/bot/data/test_output/exp_gagged_v6_nasal/closed_cues_step1.json"))
def light_eq(p,out,f1_cut=-5,emo=0.15):   # v4-style gentle linear-phase EQ (F1 cut <=6 dB) + slow 100 ms RMS emotion envelope; native SR
    y,sr=B.load(p); h=B._fir(sr,B.eq_curve(sr,f1_cut,0,0,0,0),2047 if sr<=24000 else 4095); z=ss.fftconvolve(y,h,mode="same")
    rr=np.sqrt(B._smooth(z**2,sr*0.10)+1e-12); act=rr>np.percentile(rr,95)*10**(-30/20); med=np.median(rr[act])
    z=z*B._smooth(np.clip((rr/med)**emo,10**(-3/20),10**(3/20)),sr*0.05); z=z/(np.max(np.abs(z))+1e-9)*0.89; sf.write(out,z.astype(np.float32),sr)
torch.set_num_threads(4); VE=VoiceEncoder('cpu',verbose=False)
cm=CAMPPlus(feat_dim=80,embedding_size=192); cm.load_state_dict(torch.load(f"{W}/tools/seed-vc/checkpoints/campplus_cn_common.bin",map_location="cpu")); cm.eval()
def l16(p):
    y,s=sf.read(p,always_2d=True); y=y.mean(1).astype(np.float32); return librosa.resample(y,orig_sr=s,target_sr=16000) if s!=16000 else y
def emb(y):
    f=torchaudio.compliance.kaldi.fbank(torch.tensor(y)[None],num_mel_bins=80,dither=0,sample_frequency=16000); f=f-f.mean(0,keepdim=True)
    with torch.no_grad(): return torch.nn.functional.normalize(cm(f[None])[0],dim=0).numpy()
def remb(y):
    w=preprocess_wav(y,source_sr=16000); w=w if len(w)>=8000 else y/(np.max(np.abs(y))+1e-9); e=VE.embed_utterance(w); return e/np.linalg.norm(e)
def creak(y):
    f,_=pw.harvest(y.astype(np.float64),16000,f0_floor=60,f0_ceil=1100,frame_period=5); v=f[f>0]
    if len(v)<20: return None,None
    med=np.median(v); return round(float(np.mean(v<0.6*med)*100),2), round(float(med),1)
random.seed(0); cent={}; rc={}
for ch in ["amiya","dania"]:
    fs=[f for f in sorted(glob.glob(f"{R}/datasets/{ch}/*.wav")) if sf.info(f).duration>=3.0]; ys=[l16(f) for f in random.sample(fs,min(30,len(fs)))]
    c=np.mean([emb(y) for y in ys],0); cent[ch]=c/np.linalg.norm(c); r=np.mean([remb(y) for y in ys],0); rc[ch]=r/np.linalg.norm(r)
raw=[p for p in sorted(glob.glob(f"{G}/rvc/*.wav"))+sorted(glob.glob(f"{G}/sovits/*.wav")) if not p.endswith("__eq.wav")]
for p in raw:
    q=p[:-4]+"__eq.wav"
    if not os.path.exists(q): light_eq(p,q)
refs={"dania":[f"{V5}/dania_A_rvc_g2_top1__before.wav",f"{V5}/dania_C_v4_L2_Ab_eqT0.06__before.wav",f"{V5}/dania_A_rvc_g2_top1__v4b.wav"],
      "amiya":[f"{V5}/amiya_A_rvc_g2_top1__before.wav",f"{V5}/amiya_C_rvc_lines_L2_cn008__before.wav",f"{V5}/amiya_A_rvc_g2_top1__v4c.wav"]}
files=[(ch,"ref",p) for ch in refs for p in refs[ch]]
for p in raw:
    k="rvc" if "/rvc/" in p else "sovits"; ch=os.path.basename(p).split("_")[0]; files+=[(ch,k,p),(ch,k,p[:-4]+"__eq.wav")]
rows=[]
for ch,kind,p in files:
    y=l16(p); oc="dania" if ch=="amiya" else "amiya"
    m=metrics(p,fmin=100,fmax=900); d=C.diag(p); bz=B.buzz(p); tv=TV.tv(p); nc=N.file_cues(p); cr,f0m=creak(y)
    row=dict(char=ch,kind=kind,file=os.path.relpath(p,G) if p.startswith(G) else os.path.basename(p),eq=p.endswith("__eq.wav"),
             bw=tv["bw50"],roll999=tv["roll999"],hf_floor=tv["hf_floor_db"],syl_depth=tv["syl_depth_db"],nasal_lvl=tv["nasal_lvl_db"],
             F0mod=bz["hf_mod_F0_share"],cpp=bz["hf_comb_cpp_db"],jit=bz["jitter_st_p95"],octj=bz["octave_jumps"],hnr=d.get("hnr_med_db"),creak=cr,f0_med=f0m,
             dist=N.profile_dist(nc["profile"],S1[f"genuine_nasal_{ch}"]["profile"]) if nc else None,hvn=nc["hi_vs_n2"] if nc else None,F2sd=nc["F2_std_hz"] if nc else None,
             lt1200=round(float(m["lt1200"]),1),e5_10=d.get("e500_1000_of_0_1000_pct"),f0_range=d.get("f0_range_st_p5_95"),rms_dyn=d.get("rms_dyn_db_p10_95"),
             cam=round(float(emb(y)@cent[ch]),3),cam_o=round(float(emb(y)@cent[oc]),3),res=round(float(remb(y)@rc[ch]),3),res_o=round(float(remb(y)@rc[oc]),3),dur=round(len(y)/16000,2))
    rows.append(row); print(row["file"][-58:],"bw",row["bw"],"F0m",row["F0mod"],"cpp",row["cpp"],"hnr",row["hnr"],"cr",row["creak"],"jit",row["jit"],"dist",row["dist"],"hvn",row["hvn"],"lt",row["lt1200"],"syl",row["syl_depth"],"cam",row["cam"],"res",row["res"],"f0",row["f0_med"],flush=True)
json.dump(rows,open(f"{G}/v8_metrics.json","w"),ensure_ascii=False,indent=0,default=float); print("V8 SCORE DONE",len(rows))
