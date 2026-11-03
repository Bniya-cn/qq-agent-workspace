import json, os
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v5_closed"; O6=f"{W}/bot/data/test_output/exp_gagged_v6_nasal"
M=json.load(open(f"{O}/v6_metrics.json")); S1=json.load(open(f"{O6}/closed_cues_step1.json")); A={}
for f in ["asr_v6.json","asr_v4.json","asr_v3.json","asr_v1v2.json","asr.json"]:
    if os.path.exists(f"{O}/{f}"):
        for k,v in json.load(open(f"{O}/{f}")).items(): A.setdefault(k,v)
HUM=set("呜嗚呦哼嗯呃呿喵咪哦喔唔呵嘿唉哎哟噢欸誒呣啪嗷…。，、！？!?,. ~～-|"); CLICK=set("嘟哒嗒都啵波"); OPENA=set("啊阿哈妈嗎吗呀哇啦嗷拉吧爸怕他她它沙杀嘛麻马骂呵嗬")
def asr(f):
    a=A.get(f)
    if a is None: return "?",""
    t=a.get("text") or ""
    real=[c for c in t if c not in HUM and c not in CLICK and c not in OPENA and not c.isspace() and c!="\ufffd" and not c.isascii()]
    if any(c.isascii() and c.isalpha() for c in t): real.append("EN")
    s="有实词" if real else ("开口a" if any(c in OPENA for c in t) else "无字")
    return s+("+哒" if any(c in CLICK for c in t) else ""), t[:16]
B={(r["char"],r["input"]):r for r in M if r["variant"]=="before"}
for r in M:
    r["asr"],r["asr_text"]=asr(r["file"]); b=B[(r["char"],r["input"])]; f=[]
    if r["lt1200"]<75: f.append("lt1200")
    if r["variant"]!="before" and r["env_corr"]<0.85: f.append("env")
    if r["asr"].startswith(("有实词","开口a")): f.append("asr")
    if r["variant"]!="before" and r["e5_10"]>10: f.append("e5-10>10%")
    if r["variant"]!="before" and r["bz_hf_comb_cpp_db"]>b["bz_hf_comb_cpp_db"]+2: f.append("buzz(cpp)")
    if r["variant"]!="before" and r["bz_hf_mod_F0_share"]>b["bz_hf_mod_F0_share"]+0.15: f.append("buzz(F0mod)")
    if r["variant"]!="before" and r["nc_hi_vs_n2"]>-4: f.append("口腔F2残留(hi_vs_n2>-4)")
    if r["variant"]!="before" and r.get("rms_dyn") is not None and b.get("rms_dyn") is not None and abs(r["rms_dyn"]-b["rms_dyn"])>4: f.append("情绪RMS")
    r["fails"]=f; r["pass"]=not f; r["score"]=round(r["res_char"]+0.5*r["cam_char"]+0.1*(5-r["nc_dist"]),3)
gn={ch:S1[f"genuine_nasal_{ch}"] for ch in ["dania","amiya"]}
md=["","## 8. v6：残差激励 + 角色自身鼻音声道重滤波（修「刻意发呜」、无闭口感）","",
"### 8.1 真·闭口线索测量（step 1）","",
f"- 真鼻音段：faster-whisper 字级时间戳，在官方角色语音（各 40 条）里找鼻音声母字（嗯/吗/们/你/呢/没…），再用声学条件细化（浊音、50–500 Hz 占比 ≥70%、低于该音节峰值 3 dB 以上）。dania 51 段 / 471 帧，amiya 36 段 / 415 帧。数据：`exp_gagged_v6_nasal/genuine_nasal_spans.json`、`closed_cues_step1.json`。",
"- 指标：N1 = LPC 极点 150–500 Hz（频率 / 带宽）。`hi_vs_n2` = 倒谱包络 1.3–2 kHz 均值减 0.7–1 kHz 均值（真鼻音这里是反共振峰谷）。`AF深度` = 0.7–1 kHz 均值减 1.1–2.2 kHz 最低点。`F2sd` = 600–1800 Hz 最强峰的帧间标准差。`dist` = 100–4000 Hz 对数频率包络与该角色真鼻音平均轮廓的 RMS 距离（dB，越小越像真闭口鼻音）。","",
"| 来源 | N1 Hz / 带宽 | F2峰 Hz (sd) | hi_vs_n2 dB | AF深度 dB | <500% | 500–1000% | tilt | dist |","|---|---|---|---|---|---|---|---|---|"]
for k in ["genuine_nasal_dania","genuine_open_dania","genuine_nasal_amiya","genuine_open_amiya","g1_orig","g2_orig","g3_orig"]+[k for k in S1 if "__" in k]:
    r=S1[k]; d=r.get("dist_to_nasal",r.get("dist_to_nasal_"+("amiya" if "amiya" in k else "dania")))
    md.append(f"| {k} | {r['N1_med']} / {r['N1bw_med']} | {r['F2_med']} ({r['F2_std_hz']}) | {r['hi_vs_n2']} | {r['AF_depth_db']} | {r['lt500']} | {r['e5_10']} | {r['tilt']} | {d} |")
md+=["","（g1/g2 的 F0 本身高于 500 Hz，<500/500–1000 占比不可比，只作参考；g3_orig 的 dist 是对 dania 轮廓。）","",
"**v4 为什么听起来像刻意发「呜」：**",
"1. v4 把 500–1200 Hz 整段挖空（500–1000 只剩 1–8%），剩下约 300 Hz 的 F1 和一个孤立的 1.5–1.8 kHz 峰（hi_vs_n2 = −2.9 至 +10.2 dB）。这正是圆唇口腔元音（呜/淤）的谱形。真鼻音在 1.4–2 kHz 恰好是反共振峰谷（hi_vs_n2 = −6 至 −10 dB），dania 的谷约在 1.45 kHz，amiya 约 1.55 kHz。",
"2. 反共振峰放错位置：v4 的凹陷在 0.9–1.1 kHz，而真鼻音的零点在约 1.4–1.7 kHz。",
"3. N2 被抹掉：真鼻音在 750–900 Hz 保留一个弱 N2 小鼓包，v4 把它一起压没了。",
"4. 口腔 F2 仍在走动：600–1800 Hz 最强峰 sd 245–515 Hz，听感是在「发元音」。另外 v4b 的 N1 过窄（29–52 Hz，真鼻音 52–76 Hz），共鸣太亮。",
"5. 低频占比（<500 Hz 82–97%）早就够了，所以问题不是「低频不够」。","",
"### 8.2 v6 做法（不走声码器）","",
"- 逐帧（25 ms 窗、5 ms 跳）LPC-16 求残差：逆滤波系数用 LSF 插值，并做 25 ms Hann 平滑，保证稳定、不断裂。",
"- 合成声道 = 帧 LSF 经 120 ms 长平滑（冻结 F2 走动）后，与该角色**自己的真鼻音平均声道**（真鼻音帧平均功率谱拟合 LPC-16）在 LSF 域按权重 w 混合，逐 hop 更新、有状态滤波。",
"- 再加唇鼻音零点：dania 1450 Hz，amiya 1550 Hz（来自真鼻音平均轮廓的谷），带宽 300–400 Hz。之后接 2 阶组织低通，并按 30 ms 包络恢复原响度。",
"- 情绪：沿用 v4 的 100 ms RMS 扩展（±4 dB，emo 0.2）。v6d 在高力度起音处另加 300–1500 Hz 软鼻气噪声。各变体按 RMS 归一后混入 0–30% 该输入的最佳 v4。","",
"| 变体 | 鼻音声道权重 w | 零点带宽 Hz | 低通 Hz | 混 v4 | 鼻气 |","|---|---|---|---|---|---|",
"| v6a | 0.70 | 350 | 2000 | 15% | – |","| v6b | 0.85 | 300 | 1800 | 0 | – |","| v6c | 0.60 | 400 | 2500 | 30% | – |","| v6d | 0.70 | 350 | 2000 | 15% | 0.08 |","| v6e | 0.50 | 400 | 2500 | 30% | – |","",
"### 8.3 A/B（门：<1200 ≥75%、包络 ≥0.85、实词 ASR 门、500–1000 ≤10%、CPP ≤ before+2 dB、F0调制 ≤ before+0.15、hi_vs_n2 ≤ −4 dB、RMS 动态与 before 相差 ≤4 dB；score = Res + 0.5·CAM++ + 0.1·(5 − dist)）",""]
for ch in ["dania","amiya"]:
    for tag in sorted(set(r["input"] for r in M if r["char"]==ch)):
        md+=[f"#### {ch} / {tag}","","| 变体 | dist | hi_vs_n2 | F2sd | AF深度 | N1/bw | F0调制 | CPP | <1200 | 500–1000% | F0范围 st | RMS动态 dB | CAM++ 角/他 | Res 角/他 | 包络 | ASR | 门 | score |","|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in [x for x in M if x["char"]==ch and x["input"]==tag]:
            md.append(f"| {r['variant']} | {r['nc_dist']} | {r['nc_hi_vs_n2']} | {r['nc_F2_std_hz']} | {r['nc_AF_depth_db']} | {r['nc_N1_med']}/{r['nc_N1bw_med']} | {r['bz_hf_mod_F0_share']} | {r['bz_hf_comb_cpp_db']} | {r['lt1200']:.1f} | {r['e5_10']} | {r.get('f0_range')} | {r.get('rms_dyn')} | {r['cam_char']}/{r['cam_other']} | {r['res_char']}/{r['res_other']} | {r['env_corr']} | {r['asr']}「{r['asr_text']}」 | {'通过' if r['pass'] else '×'+','.join(r['fails'])} | {r['score']} |")
        md.append("")
top={}
for ch in ["dania","amiya"]:
    c=[r for r in M if r["char"]==ch and r["variant"].startswith("v6")]; c.sort(key=lambda r:(not r["pass"],-r["score"])); top[ch]=c
md+=["### 8.4 每角色前 2（v6）",""]
for ch,rs in top.items():
    for i,r in enumerate(rs[:2],1):
        md.append(f"- **{ch} #{i}** `{r['file']}`：score {r['score']}，dist {r['nc_dist']}（真鼻音 0、真开口 {S1['genuine_open_'+ch].get('dist_to_nasal_'+ch)}），hi_vs_n2 {r['nc_hi_vs_n2']} dB，F2sd {r['nc_F2_std_hz']} Hz，CAM++ {r['cam_char']}，Res {r['res_char']}，F0调制 {r['bz_hf_mod_F0_share']}，CPP {r['bz_hf_comb_cpp_db']}，<1200 {r['lt1200']:.1f}%，ASR {r['asr']}「{r['asr_text']}」，{'通过' if r['pass'] else '×'+','.join(r['fails'])}")
md+=["","- 新文件：`exp_gagged_v5_closed/*__v6a..e.wav`（16 kHz）。没有覆盖任何已有文件。明细：`v6_metrics.json`、`asr_v6.json`。脚本：`tools/rvc/scripts/nasal_lib.py`、`v6_step1.py`、`v5_v6.py`、`v5_v6_report.py`。",
"- 取舍：鼻音声道权重 w 越高越闭口（dist、hi_vs_n2 越好），但 CAM++ 掉得越多（w = 0.85 时 amiya 的 CAM++ 接近 0）。混入 v4 与 w ≤ 0.6 能保住大部分身份。"]
p=f"{O}/report.md"; s=open(p).read()
if "\n## 8. v6" in s: s=s[:s.index("\n## 8. v6")]
open(p,"w").write(s+"\n".join(md)+"\n")
for ch,rs in top.items():
    for r in rs: print(ch,r["file"],r["score"],r["pass"],r["fails"],"dist",r["nc_dist"],"hvn",r["nc_hi_vs_n2"],"F2sd",r["nc_F2_std_hz"],"cam",r["cam_char"],"res",r["res_char"],"F0mod",r["bz_hf_mod_F0_share"],"cpp",r["bz_hf_comb_cpp_db"],"lt",r["lt1200"],"e510",r["e5_10"],"f0r",r.get("f0_range"),"rms",r.get("rms_dyn"),r["asr"],r["asr_text"])
for k,b in B.items(): print("before",k,"F0mod",b["bz_hf_mod_F0_share"],"cpp",b["bz_hf_comb_cpp_db"],"f0r",b.get("f0_range"),"rms",b.get("rms_dyn"),"cam",b["cam_char"],"res",b["res_char"])
