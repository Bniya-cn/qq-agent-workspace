# join eval + asr, apply constraints, rank configs. usage: select_top.py eval.json asr.json prefix(rvc_|rvcA_|rvcB_) out_top.json
import json, sys, re, collections
ev=json.load(open(sys.argv[1])); asr=json.load(open(sys.argv[2])); pre=sys.argv[3]
def cfg(b):
    m=re.match(r"rvc_(\w+?)_g(\d)_(\d+e)_p(-?\d+)_ir([\d.]+)_pr([\d.]+)_rms([\d.]+)\.wav",b)
    return m.groups() if m else None
TGT={"amiya":{1:-15,2:-12,3:-4},"dania":{1:-19,2:-16,3:-8}}
groups=collections.defaultdict(list)
for r in ev:
    if not r["file"].startswith(pre): continue
    a=asr.get(r["file"],{}); r["asr_words"]=a.get("words"); r["asr_open_a"]=a.get("open_a"); r["asr_text"]=a.get("text")
    r["pass"]=bool(r["lt1200"]>=75 and r["env_corr"]>=0.85 and a and not a["words"] and not a["open_a"])
    if pre=="rvc_":
        c=cfg(r["file"]); ch,n,ep,p,ir,pr,rms=c; dp=int(p)-TGT[ch][int(n)]
        groups[(ch,ep,dp,ir,pr,rms)].append(r)
import math
def score(ch, rs):
    if ch=="amiya":  # user: rank amiya by resemblyzer + spectrum/F0
        pen=0
        for r in rs:
            f0=r.get("f0med") or 288; pen+=0.5*abs(math.log2(f0/288))+0.01*max(0,r["gt3k"]-3)+0.005*max(0,75-r["lt1200"])
        return round(sum(r["res_vs_char"] for r in rs)/len(rs)-pen/len(rs),4)
    return round(sum(r["spk_vs_char"] for r in rs)/len(rs),4)   # dania: CAM++
top={}
for ch in ["amiya","dania"]:
    cands=[]
    for (c,ep,dp,ir,pr,rms),rs in groups.items():
        if c!=ch or len(rs)<3: continue
        npass=sum(r["pass"] for r in rs)
        cands.append(dict(score=score(ch,rs),res_vs_char=round(sum(r['res_vs_char'] for r in rs)/3,3),res_vs_source=round(sum(r['res_vs_source'] for r in rs)/3,3),f0med=round(sum((r.get('f0med') or 0) for r in rs)/3,1),char=ch,epoch=ep,dp=dp,ir=float(ir),pr=float(pr),rms=float(rms),n_pass=npass,
            spk_vs_char=round(sum(r["spk_vs_char"] for r in rs)/3,3), spk_vs_source=round(sum(r["spk_vs_source"] for r in rs)/3,3),
            lt1200=round(sum(r["lt1200"] for r in rs)/3,1), env_corr=round(sum(r["env_corr"] for r in rs)/3,3),
            files=[r["file"] for r in sorted(rs,key=lambda r:r["src"])], per_src=[{k:r.get(k) for k in ["src","lt1200","gt3k","env_corr","f0med","spk_vs_char","spk_vs_source","res_vs_char","res_vs_source","asr_text","pass"]} for r in sorted(rs,key=lambda r:r["src"])]))
    cands.sort(key=lambda c:(-c["n_pass"],-c["score"]))
    top[ch]=cands[:10]
json.dump(top, open(sys.argv[4],"w"), ensure_ascii=False, indent=1)
json.dump(ev, open(sys.argv[1].replace(".json","_joined.json"),"w"), ensure_ascii=False, indent=0, default=float)
for ch in top:
    for c in top[ch][:3]: print(ch, c["score"], c["res_vs_char"], c["epoch"], c["dp"], c["ir"], c["pr"], c["rms"], c["n_pass"], c["spk_vs_char"], c["spk_vs_source"], c["lt1200"], c["env_corr"])
