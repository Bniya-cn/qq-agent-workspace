import sys,os,json; sys.path.insert(0,os.path.dirname(__file__)); import tv_lib as T
O="/Users/daijinglin/Desktop/qq-agent-workspace/bot/data/test_output/exp_gagged_v5_closed"; res={}
for ch,tag in [("dania","A_rvc_g2_top1"),("dania","C_v4_L2_Ab_eqT0.06"),("amiya","A_rvc_g2_top1"),("amiya","C_rvc_lines_L2_cn008")]:
    for vn in ["before","v4b","v4c","v6a","v6c","v6e"]:
        p=f"{O}/{ch}_{tag}__{vn}.wav"
        if os.path.exists(p): r=T.tv(p); res[f"{ch}_{tag}__{vn}"]=r; print(f"{ch[:2]}_{tag[:1]}_{vn}",{k:v for k,v in r.items()})
json.dump(res,open("/Users/daijinglin/Desktop/qq-agent-workspace/bot/data/test_output/exp_gagged_v6_nasal/oldtv_diag.json","w"),indent=0)
