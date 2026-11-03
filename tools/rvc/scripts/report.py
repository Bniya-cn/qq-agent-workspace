import json, os, glob, re, statistics as st
W="/Users/daijinglin/Desktop/qq-agent-workspace"; R=f"{W}/tools/rvc"; O=f"{W}/bot/data/test_output/exp_gagged_v3_rvc"; L=f"{R}/applio/logs"
J=lambda p, d=None: json.load(open(p)) if os.path.exists(p) else d
top=J(f"{O}/fidelity_top.json",{}); evf=J(f"{O}/eval_fidelity_joined.json",[]) or J(f"{O}/eval_fidelity.json",[])
evc=J(f"{O}/eval_chain.json",[]); asc=J(f"{O}/asr_chain.json",{}); srcs=J(f"{O}/eval_fidelity_sources.json",[])
oldspk=J(f"{O}/spk_sim_v3_rvc.json",[])
md=["# exp_gagged_v3_rvc 报告：RVC v2 角色音色模型 + 封嘴 gag 纹理模型", ""]
md+=["## 1. 数据审计（身份模型只用角色真实语音）",""]
for ch in ["amiya","dania"]:
    rows=[l.split("\t") for l in open(f"{R}/datasets/{ch}/MANIFEST.tsv").read().splitlines()[1:]]
    inn=[r for r in rows if r[4]=="in_dataset"]; ex=[r for r in rows if r[4]!="in_dataset"]
    md.append(f"- **{ch}**：{len(inn)} 条，{sum(float(r[2]) for r in inn)/60:.2f} 分钟，来源 `{inn[0][3]}`；排除 {len(ex)} 条（喊叫/尖叫高 F0）。清单：`tools/rvc/datasets/{ch}/MANIFEST.tsv`")
md+=["- 不含：gg-test/封嘴源、src_nospeech/stems、任何 TTS/VC 测试输出、clean_* 闷声 TTS、另一角色音频（逐条核对路径，0 违规）。",
     "- 封嘴/闷声素材只进独立的 gag 模型数据集 `tools/rvc/datasets_gag/gag/MANIFEST.tsv`，从未混入身份数据集。方向 (a)（往角色模型里混闷声微调）按用户要求已取消。",""]
md+=["## 2. 模型与训练配置",""]
md+=["- 工具：Applio（RVC v2，HiFi-GAN NSF，contentvec 768，RMVPE），独立 venv `tools/rvc/.venv`（torch 2.14.0，MPS FP32）；底模 f0G40k/f0D40k。",
     "- 40 kHz，batch 8，60 epoch（原定 100，因与另一执行器争用 GPU 每 epoch 达 5–11 分钟，于 epoch 20 断点续训改为 60），每 10 epoch 存小权重；faiss IVF index。"]
for m in ["amiya_gag_rvc","dania_gag_rvc","gag_rvc"]:
    ws=sorted(glob.glob(f"{L}/{m}/{m}_*e_*s.pth"),key=lambda p:int(re.search(r'_(\d+)e_',p).group(1))); ix=glob.glob(f"{L}/{m}/*.index")
    md.append(f"- `{m}`：最终权重 `{ws[-1] if ws else '缺失'}`；index `{ix[0] if ix else '缺失'}`；共 {len(ws)} 个 checkpoint")
tl=f"{R}/logs/train_all.log"
if os.path.exists(tl): md.append("- 训练结束记录：" + " / ".join(open(tl).read().split("\n")))
md+=["","## 3. 源素材基线",""," | 源 | <1200Hz% | >3k% | 质心 | F0中位 | CAM++对阿米娅 | CAM++对达妮娅 | Res对阿米娅 | Res对达妮娅 |","|---|---|---|---|---|---|---|---|---|"]
for s in srcs: md.append(f"| {s['file']} | {s['lt1200']} | {s['gt3k']} | {s['cenP']} | {s['f0med']} | {s['spk_vs_amiya']} | {s['spk_vs_dania']} | {s.get('res_vs_amiya')} | {s.get('res_vs_dania')} |")
md+=["","推理源：g1/g2 = exp_gagged_v2/src_nospeech/gagged_{1,2}_bs_vocals_nospeech.wav；g3 = 原始 gg-test/wav/gagged_3.wav（用户指出 nospeech 版 g3 去台词效果差，不再使用）。","注：gagged_1/2 源 RMVPE F0 中位约 693/593 Hz（尖叫式），只降 1–2 半音会保留尖、哭腔；因此主推“目标移调”（g1/g2/g3：阿米娅 -15/-12/-4，达妮娅 -19/-16/-8，音高曲线仍跟随源，仅整体平移）。",""]
md+=["## 4. 方向 (2) 角色保真度网格：每角色前 3",
     "约束：<1200Hz ≥75%、包络相关 ≥0.85、ASR 无字且无开口 a/ha 音；音色强度 index_rate 上限 0.6（平衡档，不推极端）。排序：先通过数（满分 3 源），再按 score：阿米娅 = resemblyzer 对角色余弦 − F0 偏离 288Hz 惩罚（0.5/倍频程）− >3k 亮度惩罚 − <1200 不足惩罚；达妮娅 = CAM++ 对角色余弦。角色参考 = 30 条干净角色语音的嵌入质心。",""]
for ch,cs in top.items():
    md+=[f"### {ch}","","| # | ckpt | 相对目标移调 | index_rate | protect | rms_mix | 通过/3 | score | CAM++对角色 | CAM++对源 | Resemblyzer对角色 | Resemblyzer对源 | F0中位 | <1200% | 包络相关 |","|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k,c in enumerate(cs[:3]): md.append(f"| {k+1} | {c['epoch']} | {c['dp']:+d} | {c['ir']} | {c['pr']} | {c['rms']} | {c['n_pass']} | {c['score']} | {c['spk_vs_char']} | {c['spk_vs_source']} | {c['res_vs_char']} | {c['res_vs_source']} | {c['f0med']} | {c['lt1200']} | {c['env_corr']} |")
    md.append("")
    for k,c in enumerate(cs[:3]):
        md.append(f"- top{k+1} 文件：")
        for f,p in zip(c["files"],c["per_src"]): md.append(f"  - `fidelity/{f}` — g{p['src']}: <1200 {p['lt1200']}%, >3k {p['gt3k']}%, env {p['env_corr']}, F0 {p['f0med']}, CAM++ 角色/源 {p['spk_vs_char']}/{p['spk_vs_source']}, Resemblyzer 角色/源 {p['res_vs_char']}/{p['res_vs_source']}, ASR「{(p['asr_text'] or '')[:30]}」, {'通过' if p['pass'] else '未通过'}")
    md.append("")
pas=[r for r in evf if r.get("pass")]
md.append(f"网格总计 {len(evf)} 条，通过全部约束 {len(pas)} 条。完整明细：`eval_fidelity_joined.json`、`asr_fidelity.json`。")
seed=[r for r in oldspk if "seed" in r.get("file","")]
if seed: md+=["","对照（此前 Seed-VC 最佳，g3 源）："]+[f"- {r['file']}: 对角色 {r['vs_char']}，对源 {r['vs_source']}" for r in seed]
md+=["","## 5. 方向 (1b) 独立 gag 纹理模型（第二级/前置级）",
     "- 数据：gagged_1/gagged_2（src_nospeech 去台词版）+ 原始 gg-test/wav/gagged_3.wav + OpenGameArt CC0「Ghostly Humming」（清单 datasets_gag/gag/MANIFEST.tsv）。200 epoch，batch 4，底模 f0G40k。",
     "- A = 角色模型输出 → gag 模型（pitch 0，index 0.3）；B = 源 → gag 模型 → 角色模型（同 top 参数）。",""]
if evc:
    md+=["| 文件 | <1200% | 包络 | CAM++对角色 | CAM++对源 | Resemblyzer对角色 | ASR | 无A/B时同参数CAM++对角色 |","|---|---|---|---|---|---|---|---|"]
    base={r["file"]:r for r in evf}
    for r in sorted(evc,key=lambda r:r["file"]):
        a=asc.get(r["file"],{}); mm=re.match(r"rvc[AB]_(\w+?)_g(\d)_top\d_(\d+e)_dp(-?\d+)_ir([\d.]+)_pr([\d.]+)_rms([\d.]+)\.wav",r["file"]); ref=""
        if mm:
            ch,n,ep,dp,ir,pr,rms=mm.groups(); T={"amiya":{1:-15,2:-12,3:-4},"dania":{1:-19,2:-16,3:-8}}[ch][int(n)]+int(dp)
            b=base.get(f"rvc_{ch}_g{n}_{ep}_p{T}_ir{ir}_pr{pr}_rms{rms}.wav"); ref=b["spk_vs_char"] if b else ""
        md.append(f"| chain/{r['file']} | {r['lt1200']} | {r['env_corr']} | {r['spk_vs_char']} | {r['spk_vs_source']} | {r.get('res_vs_char')} | {'有字' if a.get('words') else ('开口a' if a.get('open_a') else '无字')} | {ref} |")
    for pre in ["rvcA_","rvcB_"]:
        rs=[r for r in evc if r["file"].startswith(pre)]
        if rs: md.append(f"\n- {pre[:-1]} 平均：对角色 {st.mean(r['spk_vs_char'] for r in rs):.3f}，<1200 {st.mean(r['lt1200'] for r in rs):.1f}%，包络 {st.mean(r['env_corr'] for r in rs):.3f}")
    tops=[r for r in evf if any(r["file"] in c["files"] for cs in top.values() for c in cs[:3])]
    if tops: md.append(f"- 无 gag 级（top3 本身）平均：对角色 {st.mean(r['spk_vs_char'] for r in tops):.3f}，<1200 {st.mean(r['lt1200'] for r in tops):.1f}%，包络 {st.mean(r['env_corr'] for r in tops):.3f}")
else: md.append("（gag 链结果缺失，见 tools/rvc/logs/）")
LS=f"{O}/lines_v4_amiya_section.md"
if os.path.exists(LS): md+=[""]+open(LS).read().splitlines()
md+=["","## 6. 重做记录","- 早期 10e 探针（index 0.75/1.0、-1/-2 半音，以及 index 0.4/0.6 部分网格）用了 gagged_3 nospeech_v2 → 已归档到 `_superseded_g3nospeech_10e/`，不计入结论；正式网格全部用原始 gagged_3 重跑。","- gag 模型数据集最初为 g2 + g3_nospeech_v2 + CC0 哼唱（未开训），已按要求改为 g1+g2+原始 g3+CC0 哼唱后才训练。","- 达妮娅数据集剔除 13 条高 F0 尖叫/喊叫后重新预处理（训练前完成）。","","## 7. 说明","- Resemblyzer（英文训练）与 CAM++ 对闷声的角色区分度都有限，两角色干净质心彼此也很接近；分数只作相对排序。","- CAM++ 对闷声的判别力有限（两角色干净语音质心互相余弦约 0.84），余弦只作相对排序，最终以人耳为准。",
     "- 训练集中阿米娅/达妮娅仍有少量战斗语音（短促发力声），已剔除高 F0 尖叫/喊叫；若试听仍有情绪外溢，可再剔除全部战斗语音重训。",
     "- 未修改 tts_service.py；原素材未覆写。","- 运维说明：早期曾执行过一次 `pkill -9 -f multiprocessing.spawn` 清理卡死的 Applio 子进程，该模式可能误杀同机其他执行器的 multiprocessing 子进程；之后不再使用宽匹配 kill。"]
open(f"{O}/report.md","w").write("\n".join(md)+"\n"); print("report written")
