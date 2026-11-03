# step 1: genuine nasal-murmur spans + closed/open cue measurements; fits per-character nasal tract (pole LPC + antiformant)
import os, sys, json, glob
sys.path.insert(0,os.path.dirname(__file__))
import numpy as np, nasal_lib as N
W="/Users/daijinglin/Desktop/qq-agent-workspace"; T=f"{W}/bot/data/test_output"; O6=f"{T}/exp_gagged_v6_nasal"; V5=f"{T}/exp_gagged_v5_closed"
TS=json.load(open(f"{O6}/genuine_nasal_ts.json"))
spans={"dania":[],"amiya":[]}; acc={ch:dict(rows=[],vecs=[],P=[]) for ch in spans}; openacc={ch:dict(rows=[],vecs=[]) for ch in spans}
for f,info in TS.items():
    ch=info["char"]; y=N.load16(f); F=N.frames(y); f0=N.voiced_mask(y); E,Pw=N.cep_env(F); e=N._db(Pw.sum(1)); act=e>np.percentile(e,95)-30
    t=(np.arange(len(F))*N.HOP+N.WIN/2)/N.SR; nasal=np.zeros(len(F),bool)
    for h in info["hits"]:
        if h["p"]<0.3: continue
        w=(t>=h["start"]-0.04)&(t<=h["start"]+(h["end"]-h["start"] if h["c"] in "嗯呣唔" else 0.12))
        if not w.any(): continue
        syl=(t>=h["start"])&(t<=h["end"]+0.05); emax=e[syl].max() if syl.any() else e[w].max()
        tot=Pw[:,(N.FR>=50)&(N.FR<4000)].sum(1); lt5=Pw[:,(N.FR>=50)&(N.FR<500)].sum(1)/tot
        sel=w&(f0>0)&act&(lt5>=0.7)&(e<=emax-3 if h["c"] not in "嗯呣唔" else True)
        if sel.sum()>=3:
            nasal|=sel; ks=np.where(sel)[0]; spans[ch].append(dict(file=f,c=h["c"],t0=round(float(t[ks[0]]),3),t1=round(float(t[ks[-1]]),3),n=int(sel.sum())))
    for k in np.where(nasal)[0]:
        acc[ch]["rows"].append(N.cue_frames(E[k],Pw[k],N.lpc_poles(F[k]))); acc[ch]["vecs"].append(N.profile_vec(E[k])); acc[ch]["P"].append(Pw[k]/Pw[k].sum())
    for k in np.where((f0>0)&act&~nasal)[0][::6]:
        openacc[ch]["rows"].append(N.cue_frames(E[k],Pw[k],N.lpc_poles(F[k]))); openacc[ch]["vecs"].append(N.profile_vec(E[k]))
res={}; tract={}
for ch in spans:
    res[f"genuine_nasal_{ch}"]=N.summarize(acc[ch]["rows"],acc[ch]["vecs"]); res[f"genuine_open_{ch}"]=N.summarize(openacc[ch]["rows"],openacc[ch]["vecs"])
    Pm=np.mean(acc[ch]["P"],0); a=N.fit_nasal_lpc(Pm); tract[ch]=dict(a=a.tolist(),af_hz=res[f"genuine_nasal_{ch}"]["AF_med"],N1=res[f"genuine_nasal_{ch}"]["N1_med"],n_frames=len(acc[ch]["rows"]),n_spans=len(spans[ch]))
json.dump(spans,open(f"{O6}/genuine_nasal_spans.json","w"),ensure_ascii=False,indent=0)
json.dump(tract,open(f"{O6}/nasal_tract_fit.json","w"),indent=0)
# references and current outputs
G={"g1_orig":f"{W}/gg-test/wav/gagged_1.wav","g2_orig":f"{W}/gg-test/wav/gagged_2.wav","g3_orig":f"{W}/gg-test/wav/gagged_3.wav"}
for k,p in G.items(): res[k]=N.file_cues(p)
for ch,tag in [("dania","A_rvc_g2_top1"),("dania","C_v4_L2_Ab_eqT0.06"),("amiya","A_rvc_g2_top1"),("amiya","C_rvc_lines_L2_cn008")]:
    for vn in ["before","v4a","v4b","v4c"]:
        p=f"{V5}/{ch}_{tag}__{vn}.wav"; r=N.file_cues(p); r["dist_to_nasal"]=N.profile_dist(r["profile"],res[f"genuine_nasal_{ch}"]["profile"]); r["dist_to_open"]=N.profile_dist(r["profile"],res[f"genuine_open_{ch}"]["profile"]); res[f"{ch}_{tag}__{vn}"]=r
for ch in spans:
    for k in [f"genuine_nasal_{ch}",f"genuine_open_{ch}","g1_orig","g2_orig","g3_orig"]:
        res[k].setdefault(f"dist_to_nasal_{ch}",N.profile_dist(res[k]["profile"],res[f"genuine_nasal_{ch}"]["profile"]))
json.dump(res,open(f"{O6}/closed_cues_step1.json","w"),ensure_ascii=False,indent=0)
K=["n_frames","N1_med","N1bw_med","F2_med","F2_std_hz","F2_prom_db","AF_med","AF_depth_db","hi_vs_n2","n2_vs_dip","lt500","e5_10","tilt"]
print("name\t"+"\t".join(K)+"\tdist_nasal")
for k,r in res.items():
    d=r.get("dist_to_nasal",r.get("dist_to_nasal_"+("amiya" if "amiya" in k else "dania")))
    print(k[:44]+"\t"+"\t".join(str(r.get(x,"")) for x in K)+f"\t{d}")
print({ch:(tract[ch]["n_spans"],tract[ch]["n_frames"],tract[ch]["af_hz"],tract[ch]["N1"]) for ch in tract})
