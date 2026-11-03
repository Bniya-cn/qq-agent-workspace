import json, os, math
O="/Users/daijinglin/Desktop/qq-agent-workspace/bot/data/test_output/exp_gagged_v3_rvc/lines_v4_amiya"
E=json.load(open(f"{O}/eval.json")); J={os.path.basename(j["out"]):j for j in json.load(open(f"{O}/jobs.json"))}; fx=[]
for b,m in E["outputs"].items():
    f=m["f0med"]
    if b not in J or f is None or 285<=f<=300: continue
    j=dict(J[b]); np_=j["pitch"]+round(12*math.log2(292/f))
    if np_==j["pitch"]: np_=j["pitch"]+(1 if f<285 else -1)
    j["pitch"]=np_; j["out"]=j["out"].replace(f"_p{J[b]['pitch']:+d}_",f"_p{np_:+d}_")
    if j["out"]!=J[b]["out"]: fx.append(j)
json.dump(fx,open(f"{O}/jobs_fix.json","w"),indent=0); print(len(fx),"fix jobs")
