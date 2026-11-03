# v8 RVC jobs: g1/g2/ORIGINAL g3 + v4_lines Ab inputs -> character 60e model with the NASAL-ONLY index (and baseline full index). usage: v8_rvc_jobs.py jobs.json
import sys, glob, os, json, math, numpy as np, soundfile as sf, librosa, pyworld as pw
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v8_gen/rvc"; os.makedirs(O,exist_ok=True)
A=f"{W}/tools/rvc/applio/logs"; TGT={"dania":250.0,"amiya":292.0}
GAGS=[f"{W}/gg-test/wav/gagged_{i}.wav" for i in (1,2,3)]   # g3 = ORIGINAL (never the nospeech one)
def f0med(p):
    y,sr=sf.read(p,always_2d=True); y=librosa.resample(y.mean(1),orig_sr=sr,target_sr=16000).astype(np.float64)
    f,_=pw.harvest(y,16000,f0_floor=80,f0_ceil=1100,frame_period=10); v=f[f>0]; return float(np.median(v)) if len(v) else 300.0
jobs=[]; meta={}
for ch in ["dania","amiya"]:
    L=f"{A}/{ch}_gag_rvc"; pth=glob.glob(f"{L}/{ch}_gag_rvc_60e_*s.pth")[0]; NI=f"{L}/{ch}_gag_rvc_NASALONLY_v8.index"; FI=f"{L}/{ch}_gag_rvc.index"
    lines=sorted(glob.glob(f"{W}/bot/data/test_output/exp_gagged_v4_lines/muffled/{ch}_L?_s42_Ab_eqT0.06.wav"))
    for src in GAGS+lines:
        m=f0med(src); t=int(round(12*math.log2(TGT[ch]/m))); stem=os.path.basename(src)[:-4]; meta[f"{ch}|{stem}"]=dict(f0_in=round(m,1),pitch=t)
        grid=[(0.6,0.5),(0.75,0.5),(0.9,0.33),(0.9,0.5),(0.75,0.33),(0.6,0.33)] if "gagged" in src else [(0.75,0.5),(0.9,0.33)]
        for ir,pr in grid:
            jobs.append(dict(src=src,pth=pth,index=NI,pitch=t,ir=ir,pr=pr,rms=0.25,out=f"{O}/{ch}_{stem}__nasidx_ir{ir}_pr{pr}_p{t:+d}.wav"))
        if "gagged" in src: jobs.append(dict(src=src,pth=pth,index=FI,pitch=t,ir=0.6,pr=0.5,rms=0.25,out=f"{O}/{ch}_{stem}__fullidx_ir0.6_pr0.5_p{t:+d}.wav"))
json.dump(jobs,open(sys.argv[1],"w"),indent=0); json.dump(meta,open(f"{O}/input_f0.json","w"),indent=0,ensure_ascii=False); print(len(jobs),"jobs",meta)
