# usage: make_jobs.py <stage> <out_jobs.json>   stage: fidelity | chain
import sys, os, glob, json, re
W="/Users/daijinglin/Desktop/qq-agent-workspace"; L=f"{W}/tools/rvc/applio/logs"
S=f"{W}/bot/data/test_output/exp_gagged_v2/src_nospeech"; O=f"{W}/bot/data/test_output/exp_gagged_v3_rvc"
SRC={1:f"{S}/gagged_1_bs_vocals_nospeech.wav",2:f"{S}/gagged_2_bs_vocals_nospeech.wav",3:"/Users/daijinglin/Desktop/qq-agent-workspace/gg-test/wav/gagged_3.wav"}
# target transposes: source RMVPE median F0 g1 693 / g2 593 (src_nospeech) / g3 352 Hz (original gg-test/wav/gagged_3.wav) -> amiya ~290 Hz, dania ~230 Hz
TGT={"amiya":{1:-15,2:-12,3:-4},"dania":{1:-19,2:-16,3:-8}}
def ckpts(m, want):
    fs=glob.glob(f"{L}/{m}/{m}_*e_*s.pth"); by={int(re.search(r"_(\d+)e_",f).group(1)):f for f in fs}
    out=[]
    for w in want:
        if not by: break
        e=min(by,key=lambda k:abs(k-w)); 
        if by[e] not in out: out.append(by[e])
    return out
def index(m): return glob.glob(f"{L}/{m}/*.index")[0]
def tag(p): return re.search(r"_(\d+e)_",p).group(1)
stage=sys.argv[1]; jobs=[]
if stage=="fidelity":
    os.makedirs(f"{O}/fidelity",exist_ok=True)
    for ch in ["amiya","dania"]:
        m=f"{ch}_gag_rvc"
        for pth in ckpts(m,[20,40,60]):
            for n,src in SRC.items():
                for dp in [0,3]:
                    p=TGT[ch][n]+dp
                    for ir in [0.4,0.5,0.6]:  # timbre strength capped at 0.6 (user: balanced, no extremes)
                        for pr in [0.33,0.5]:
                            for rms in [0.0,0.25]:
                                jobs.append(dict(src=src,pth=pth,index=index(m),pitch=p,ir=ir,pr=pr,rms=rms,
                                  out=f"{O}/fidelity/rvc_{ch}_g{n}_{tag(pth)}_p{p}_ir{ir}_pr{pr}_rms{rms}.wav"))
elif stage=="chain":
    top=json.load(open(f"{O}/fidelity_top.json")); gm="gag_rvc"
    gp=ckpts(gm,[10000])[0]; gi=index(gm); os.makedirs(f"{O}/chain/tmp",exist_ok=True)
    for ch,cfgs in top.items():
        m=f"{ch}_gag_rvc"
        for k,c in enumerate(cfgs[:3]):
            pth=ckpts(m,[int(c["epoch"][:-1])])[0]
            for n,src in SRC.items():
                p=TGT[ch][n]+c["dp"]; base=f"top{k+1}_{c['epoch']}_dp{c['dp']}_ir{c['ir']}_pr{c['pr']}_rms{c['rms']}"
                # A: char -> gag (texture pass, pitch 0)
                mid=f"{O}/fidelity/rvc_{ch}_g{n}_{c['epoch']}_p{p}_ir{c['ir']}_pr{c['pr']}_rms{c['rms']}.wav"
                jobs.append(dict(src=mid,pth=gp,index=gi,pitch=0,ir=0.3,pr=0.5,rms=0.25,out=f"{O}/chain/rvcA_{ch}_g{n}_{base}.wav"))
                # B: gag -> char
                g=f"{O}/chain/tmp/gagpass_g{n}.wav"
                jobs.append(dict(src=src,pth=gp,index=gi,pitch=0,ir=0.5,pr=0.5,rms=0.25,out=g))
                jobs.append(dict(src=g,pth=pth,index=index(m),pitch=p,ir=c["ir"],pr=c["pr"],rms=c["rms"],out=f"{O}/chain/rvcB_{ch}_g{n}_{base}.wav"))
json.dump(jobs,open(sys.argv[2],"w"),ensure_ascii=False,indent=0); print(stage,len(jobs),"jobs")
