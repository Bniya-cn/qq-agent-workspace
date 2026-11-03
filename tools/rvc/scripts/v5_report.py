import json, os, statistics as st
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v5_closed"
M=json.load(open(f"{O}/ab_v1v2_metrics.json")); A=json.load(open(f"{O}/asr_v1v2.json")) if os.path.exists(f"{O}/asr_v1v2.json") else {}
PA=json.load(open(f"{O}/diag_partA.json")) if os.path.exists(f"{O}/diag_partA.json") else []
def asr(b):
    a=A.get(b)
    if a is None: return "?","?"
    # REAL-WORD gate: real_words OR open_a fail; click_onset noted
    if a.get("real_words") or a.get("words"): st_="有实词"
    elif a.get("open_a"): st_="开口a"
    else: st_="无字"
    note=""
    if a.get("click_onset"): note="+哒/嘟"
    return st_+note, (a.get("text") or "")[:24]
def gate(r, asr_st):
    fails=[]
    if r["lt1200"]<75: fails.append("lt1200")
    if r.get("env_corr_vs_before",1)<0.85 and r["variant"]!="before": fails.append("env")
    if r.get("f0_contour_corr_vs_before") is not None and r["f0_contour_corr_vs_before"]<0.9 and r["variant"]!="before": fails.append("f0curve")
    if asr_st.startswith("有实词") or asr_st.startswith("开口a"): fails.append("asr")
    return not fails, fails
md=["# exp_gagged_v5_closed：闭口重合成 A/B + Part A 诊断","","生成时间：见文件 mtime。v0（`__closed` / `__closed_emo`）已作废——整体包络替换导致 CAM++ 角色相似度崩到 ~0.1，仅保留作对照，**不要听、不计入结论**。正式交付是 `__v1mild` / `__v2medium`。","","## 0. 处理说明","",
"- WORLD 分析保留 F0 与时长；加性闭口 EQ（在角色自身包络上叠鼻音凹陷，不整体替换）；降低有声帧非周期度；衰减清音帧；WORLD 域起音渐入 + 音节起始 HF 衰减；输出端瞬态抑制（HF 占比钳位 + ~20 ms 起音跟随）。",
"- **v1mild**：notch 6 / mid 3 / ap×0.35 / uv 0.3 / post_onset；偏保身份。",
"- **v2medium**：notch 10 / mid 6 / ap×0.2 / uv 0.15 / post_onset / emo 0.2（F0/RMS 适度放大，±7/5 半音封顶）；偏闭口+情绪。",
"- 输入：达妮娅 = RVC g2 top1、RVC g2 无 +3 半音、v4 L2 Ab_eqT0.06；阿米娅 = RVC g2 top1、Seed-VC g2bs final、RVC 测试句 L2 cn008。原件只读，before 为副本。",
"- ASR 门改为 **实词门**：任一真实汉字词符即失败（允许 呜/哼/嗯/唔 等拟声；单独重复的 嘟/哒/都 记为 click_onset 但不算实词）。`asr_dir.py` 与 `exp_gagged_v2/merge_g123_orig.py` 已同步硬化。","",
"## 1. Part A 诊断摘要（详见 diag_partA.json）","",
"| 问题 | 主因阶段 | 关键数字 |",
"|---|---|---|",
"| 线驱动 TTS / 测试句张嘴、情绪弱 | 音轨本身 + RVC 再开一点 | Ab_eqT 入 F1≈319、e500–1000≈4%；过 RVC 后 F1 356–424、e5–10 9–22%。F0 跨度：g 源 11–13 st，拟声 TTS 5–9 st，RVC 测试句 3–5 st |",
"| 达妮娅漏气 +「达/哒哒」 | RVC 达妮娅引入（训练语料辅音重） | ap 0–4k：源 0.18 → RVC 0.32。起始 HF 超出：g2 4.0→5.8 dB，g3 3.4→8.2 dB。达妮娅真人语料起始 HF>6 dB 极多 |",
"| 阿米娅 gag 级仍张嘴 | gag 模型链（chain A） | 仅 RVC：e5–10 23.8%；+gag：61.4%、F1 534 Hz。Seed-VC g2bs 最贴近鼻音哼（e5–10 4.3%） |",
"| 测试句像「哼唧」 | F0 偏高（达妮娅 +3）+ 漏气 + F0 跨度塌缩 | 达妮娅 top F0 272–284 Hz vs 不加 +3 的 238 Hz；HNR 测试句 15–18 dB |",
"","","## 2. A/B 指标（五门 + 新指标）","",
"五门：lt1200≥75%、包络相关≥0.85（相对 before）、F0 曲线相关≥0.9、ASR 无实词且无开口 a。新指标：HNR、ap、bursts/s、F1、e500–1000%、CAM++/Resemblyzer 对角色。","",]
by={}
for r in M:
    by.setdefault((r["char"],r["input"]),{}).setdefault(r["variant"],r)
for ch in ["dania","amiya"]:
    md+=[f"### {ch}",""]
    md+=["| 输入 | 变体 | lt1200 | 包络 | F0曲线 | HNR | ap | bursts/s | onsetHF | F1 | e5–10% | 凹陷dB | CAM++角/他 | Res角/他 | ASR | 五门 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for tag in [t for t,_ in {"dania":[("A_rvc_g2_top1",0),("B_rvc_g2_dp0",0),("C_v4_L2_Ab_eqT0.06",0)],"amiya":[("A_rvc_g2_top1",0),("B_seedvc_g2bs_final",0),("C_rvc_lines_L2_cn008",0)]}[ch]]:
        for vn in ["before","v1mild","v2medium"]:
            r=by[(ch,tag)][vn]; st_,tx=asr(r["file"]); ok,fails=gate(r,st_)
            md.append(f"| {tag} | {vn} | {r['lt1200']:.1f} | {r.get('env_corr_vs_before')} | {r.get('f0_contour_corr_vs_before')} | {r['hnr_med_db']} | {r['ap_mean_0_4k']} | {r['bursts_per_s']} | {r.get('onset_hf_excess_db_med')} | {r['F1_med_hz']} | {r['e500_1000_of_0_1000_pct']} | {r.get('notch_depth_db')} | {r['cam_char']}/{r['cam_other']} ({r['cam_margin']:+.3f}) | {r['res_char']}/{r['res_other']} ({r['res_margin']:+.3f}) | {st_}「{tx}」 | {'通过' if ok else '失败:'+','.join(fails)} |")
    md.append("")
# deltas
md+=["## 3. 相对 before 的平均变化（v1mild / v2medium）","",
"| 角色 | 变体 | ΔHNR | Δap | Δbursts/s | ΔF1 | Δe5–10 | ΔCAM++角 | ΔRes角 | 身份保留？ |",
"|---|---|---|---|---|---|---|---|---|---|"]
for ch in ["dania","amiya"]:
    for vn in ["v1mild","v2medium"]:
        pairs=[]
        for tag in by:
            if tag[0]!=ch: continue
            b,a=by[tag]["before"],by[tag][vn]
            pairs.append((a["hnr_med_db"]-b["hnr_med_db"], a["ap_mean_0_4k"]-b["ap_mean_0_4k"], a["bursts_per_s"]-b["bursts_per_s"],
                          (a["F1_med_hz"] or 0)-(b["F1_med_hz"] or 0), a["e500_1000_of_0_1000_pct"]-b["e500_1000_of_0_1000_pct"],
                          a["cam_char"]-b["cam_char"], a["res_char"]-b["res_char"], a["cam_char"], a["res_char"]))
        if not pairs: continue
        def m(i): return round(st.mean(p[i] for p in pairs),3)
        keep="是" if (m(7)>=0.45 and m(8)>=0.70) else ("部分（Res 尚可）" if m(8)>=0.70 else "否（身份掉太多）")
        md.append(f"| {ch} | {vn} | {m(0):+.2f} | {m(1):+.3f} | {m(2):+.2f} | {m(3):+.0f} | {m(4):+.1f} | {m(5):+.3f} | {m(6):+.3f} | {keep} |")
md+=["","","## 4. 文件清单（听这些）","",
"目录：`bot/data/test_output/exp_gagged_v5_closed/`","","| 角色 | 输入 | before | v1mild | v2medium |",
"|---|---|---|---|---|"]
for ch,tags in [("dania",["A_rvc_g2_top1","B_rvc_g2_dp0","C_v4_L2_Ab_eqT0.06"]),("amiya",["A_rvc_g2_top1","B_seedvc_g2bs_final","C_rvc_lines_L2_cn008"])]:
    for t in tags:
        md.append(f"| {ch} | {t} | `{ch}_{t}__before.wav` | `{ch}_{t}__v1mild.wav` | `{ch}_{t}__v2medium.wav` |")
md+=["","","## 5. 结论与注意","",
"- **达妮娅**：v1mild / v2medium 都能明显压低 e5–10 与 ap、略降 bursts；Resemblyzer 身份基本保住（ΔRes 约 −0.01~−0.02）；CAM++ 有下降但仍远高于角色他者差。推荐先听 v1mild。",
"- **阿米娅**：闭口指标改善，但 CAM++ 对角色常掉到接近/低于对达妮娅（margin 转负）；Resemblyzer mild 仍约 0.75–0.80。阿米娅若要保身份，优先 v1mild，或对人耳挑 Seed-VC g2bs 的 before（本身已较鼻音）。",
"- **哒/嘟**：post_onset 把 bursts/s 大约减半，未清零；ASR 里若仍见「嘟嘟」会标 click_onset。要再压需更强 attack 或改训练数据。",
"- **v0 作废**：`*__closed.wav` / `*__closed_emo.wav` 为整包络替换实验，身份崩坏，仅作消融对照。",
"- 规则：g1+g2+原始 g3；未覆写原件；未改 tts_service.py；未宽匹配 kill。",
"- 明细：`ab_v1v2_metrics.json`、`asr_v1v2.json`、`diag_partA.json`。"]
open(f"{O}/report.md","w").write("\n".join(md)+"\n"); print("report written", f"{O}/report.md")
