import json, glob, sys, os
from concurrent.futures import ProcessPoolExecutor
from closed_lib import diag
W="/Users/daijinglin/Desktop/qq-agent-workspace"; T=f"{W}/bot/data/test_output"; F=f"{T}/exp_gagged_v3_rvc/fidelity"; C=f"{T}/exp_gagged_v3_rvc/chain"; V=f"{T}/exp_gagged_v4_lines"; L=f"{T}/exp_gagged_v3_rvc/lines_v4_amiya"; S2=f"{T}/exp_gagged_v2"
D={"src":[("g1_orig",f"{W}/gg-test/wav/gagged_1.wav"),("g2_orig",f"{W}/gg-test/wav/gagged_2.wav"),("g3_orig",f"{W}/gg-test/wav/gagged_3.wav"),
          ("g1_nospeech(RVC入)",f"{S2}/src_nospeech/gagged_1_bs_vocals_nospeech.wav"),("g2_nospeech(RVC入)",f"{S2}/src_nospeech/gagged_2_bs_vocals_nospeech.wav"),("g2_bs_vocals(SeedVC入)",f"{S2}/src_purified/gagged_2_bs_vocals.wav")],
 "dania":[("rvc_fid_g1",f"{F}/rvc_dania_g1_40e_p-16_ir0.6_pr0.5_rms0.25.wav"),("rvc_fid_g2",f"{F}/rvc_dania_g2_40e_p-13_ir0.6_pr0.5_rms0.25.wav"),("rvc_fid_g3",f"{F}/rvc_dania_g3_40e_p-5_ir0.6_pr0.5_rms0.25.wav"),
          ("rvc_fid_g2_dp0",f"{F}/rvc_dania_g2_40e_p-16_ir0.6_pr0.5_rms0.25.wav"),("chainA_g2(+gag模型)",f"{C}/rvcA_dania_g2_top1_40e_dp3_ir0.6_pr0.5_rms0.25.wav"),
          ("v4_tts_real_L2",f"{V}/tts_raw/dania_L2_real_s42.wav"),("v4_tts_ono_L2",f"{V}/tts_ono/dania_L2_ono_s42.wav"),("v4_L2_Ab_eqT0.06",f"{V}/muffled/dania_L2_s42_Ab_eqT0.06.wav")],
 "amiya":[("rvc_fid_g1",f"{F}/rvc_amiya_g1_40e_p-15_ir0.4_pr0.33_rms0.25.wav"),("rvc_fid_g2",f"{F}/rvc_amiya_g2_40e_p-12_ir0.4_pr0.33_rms0.25.wav"),("rvc_fid_g3",f"{F}/rvc_amiya_g3_40e_p-4_ir0.4_pr0.33_rms0.25.wav"),
          ("chainA_g2(+gag模型)",f"{C}/rvcA_amiya_g2_top1_40e_dp0_ir0.4_pr0.33_rms0.25.wav"),
          ("seedvc_g2bs_raw",f"{S2}/vc_raw_g123_orig/vc_amiya_g2bs_bal_s-12_cfg0.5_st40.wav"),("seedvc_g2bs_final",f"{S2}/vc_final_g123_orig/vc_amiya_g2bs_bal_s-12_cfg0.5_st40_envdyn_final.wav"),
          ("v4_tts_ono_L2_cn008",f"{V}/tts_ono_r2/amiya_L2_onocn008_s7.wav"),("v4_L2_Ab_eqT0.06_cn008",f"{V}/muffled/amiya_L2_s7_Ab_eqT0.06_cn008.wav"),
          ("rvc_lines_L2(测试句)",f"{L}/rvc_amiya_L2_s7_Ab_eqT0.06_cn008__60e_p-1_ir0.6_pr0.5_rms0.25.wav"),("rvc_lines_L3(测试句)",f"{L}/rvc_amiya_L3_s7_Ab_eqT0.06_cn008__60e_p-1_ir0.6_pr0.5_rms0.25.wav"),("rvc_lines_L6(测试句)",f"{L}/rvc_amiya_L6_s7_Ab_eqT0.06__60e_p+1_ir0.6_pr0.5_rms0.25.wav")]}
for ch in ["amiya","dania"]:
    D[f"clean_{ch}"]=[(f"clean_{os.path.basename(p)}",p) for p in sorted(glob.glob(f"{W}/tools/rvc/datasets/{ch}/*.wav"))[5:45:8]]
if __name__=="__main__":
    items=[(g,n,p) for g,l in D.items() for n,p in l]
    miss=[p for *_,p in items if not os.path.exists(p)]; assert not miss, miss
    with ProcessPoolExecutor(4) as ex: res=list(ex.map(diag,[p for *_,p in items]))
    out=[dict(group=g,name=n,path=p,**r) for (g,n,p),r in zip(items,res)]
    json.dump(out,open(sys.argv[1],"w"),ensure_ascii=False,indent=0)
    K=["hnr_med_db","ap_mean_0_4k","unvoiced_active_energy_pct","bursts_per_s","onsets_hf_excess_gt6db","onsets","onset_hf_excess_db_med","F1_med_hz","e500_1000_of_0_1000_pct","notch_depth_db","f0_med","f0_range_st_p5_95","rms_dyn_db_p10_95","rms_mod_db_per_10ms_sd"]
    print("group\tname\t"+"\t".join(K))
    for r in out: print(r["group"]+"\t"+r["name"][:28]+"\t"+"\t".join(str(r.get(k)) for k in K))
