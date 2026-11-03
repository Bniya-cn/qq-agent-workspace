# v4 amiya Ab_eqT onomatopoeia TTS -> RVC amiya, per-file transpose so output F0 median ~292 Hz (285-300). usage: lines_jobs.py out.json
import sys, glob, os, json, math, numpy as np, soundfile as sf, librosa
W="/Users/daijinglin/Desktop/qq-agent-workspace"; V=f"{W}/bot/data/test_output/exp_gagged_v4_lines"; O=f"{W}/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya"
L=f"{W}/tools/rvc/applio/logs/amiya_gag_rvc"; IDX=glob.glob(f"{L}/*.index")[0]
CK={"60e":glob.glob(f"{L}/amiya_gag_rvc_60e_*s.pth")[0],"40e":glob.glob(f"{L}/amiya_gag_rvc_40e_*s.pth")[0]}
ins=sorted(glob.glob(f"{V}/muffled/amiya_*_Ab_eqT*.wav"))+sorted(f for f in glob.glob(f"{V}/g3_phrase/amiya_L6_*_Ab_eqT*.wav"))  # g3splice excluded (contains original g3 speaker)
os.makedirs(O,exist_ok=True); jobs=[]; f0s={}
for p in ins:
    y,sr=sf.read(p,always_2d=True); y=librosa.resample(y.mean(1).astype(np.float32),orig_sr=sr,target_sr=16000)
    f0,vf,_=librosa.pyin(y,fmin=100,fmax=900,sr=16000,frame_length=1024); m=float(np.nanmedian(f0)) if np.any(vf) else 270.0
    t=round(12*math.log2(292/m)); f0s[os.path.basename(p)]=dict(f0_in=round(m,1),pitch=t)
    stem=os.path.basename(p)[:-4]
    for ck,ir in [("60e",0.5),("60e",0.6),("40e",0.6)]:
        jobs.append(dict(src=p,pth=CK[ck],index=IDX,pitch=t,ir=ir,pr=0.5,rms=0.25,out=f"{O}/rvc_{stem}__{ck}_p{t:+d}_ir{ir}_pr0.5_rms0.25.wav"))
json.dump(jobs,open(sys.argv[1],"w"),indent=0); json.dump(f0s,open(f"{O}/input_f0.json","w"),indent=0); print(len(ins),"inputs",len(jobs),"jobs")
