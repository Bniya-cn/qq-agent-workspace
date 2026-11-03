import json, os
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v5_closed"
M=json.load(open(f"{O}/v4_metrics.json")); A={}
for f in ["asr_v4.json","asr_v3.json","asr_v1v2.json","asr.json"]:
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
    r["asr"],r["asr_text"]=asr(r["file"]); b=B[(r["char"],r["input"])]
    fails=[]
    if r["lt1200"]<75: fails.append("lt1200")
    if r["variant"]!="before" and r["env_corr"]<0.85: fails.append("env")
    if r["asr"].startswith(("有实词","开口a")): fails.append("asr")
    if r["variant"]!="before" and r["e5_10"]>10: fails.append("e5-10>10%")
    if r["variant"]!="before" and r["bz_hf_comb_cpp_db"]>b["bz_hf_comb_cpp_db"]+2: fails.append("buzz(cpp)")
    r["fails"]=fails; r["pass"]=not fails; r["score"]=round(r["res_char"]+0.5*r["cam_char"],3)
md=["","## 7. v4：无声码器闭口路径（修「电流/电音」）","",
"### 7.1 电流音成因（实测，HF=2–6 kHz）","",
"指标：`F0调制占比` = HF 包络调制谱中落在 F0±15% 的能量占比（脉冲串激励 → HF 被 F0 周期性打点 = 嗡/电流）；`HF梳状CPP` = 2–6 kHz 对数谱在 1/F0 倒谱处的峰值显著度（越高越像整齐梳齿 = 电子味）；`50–200Hz调制` = 帧率增益调制（zipper）；`调制线` = 非谐波最强调制谱线；`跳变p95` = 相邻 5 ms F0 半音差 95 分位；`倍频跳` = >9 半音跳变数。","",
"| 文件 | F0调制占比 | HF梳状CPP dB | 50–200Hz调制 | 调制线 Hz / dB | 跳变p95 st | 倍频跳 | 削波% |","|---|---|---|---|---|---|---|---|"]
for ch,tag in [("dania","C_v4_L2_Ab_eqT0.06"),("amiya","C_rvc_lines_L2_cn008"),("dania","A_rvc_g2_top1"),("amiya","A_rvc_g2_top1")]:
    for vn in ["before","world_noedit","closed_emo","v3a","v3b","v1mild","v4a","v4b","v4c"]:
        r=next((x for x in M if x["char"]==ch and x["input"]==tag and x["variant"]==vn),None)
        if r: md.append(f"| {ch}_{tag} {vn} | {r['bz_hf_mod_F0_share']} | {r['bz_hf_comb_cpp_db']} | {r['bz_hf_mod_50_200_share']} | {r['bz_mod_line_hz']} / {r['bz_mod_line_prom_db']} | {r['bz_jitter_st_p95']} | {r['bz_octave_jumps']} | {r['bz_clip_pct']} |")
md+=["",
"**结论：电流音 = WORLD 脉冲串激励 + 被压低的非周期度。**",
"- before（RVC / Seed-VC / TTS 原样）本身不嗡：F0 调制占比 0.09–0.33，HF 梳状 CPP 4.4–6.6 dB。",
"- 只做 WORLD 分析/重合成不改任何参数（world_noedit），就已经变嗡：F0 调制升到 0.23–0.33，CPP 升 1–3 dB。",
"- 再压 ap（v0 ×0.2，v3 ×0.6–0.8 但还叠了包络替换），F0 调制到 0.58–0.81，CPP 到 8–22 dB：高频被 F0 整齐地一下下打点，就是「电流/电音」。v3 把 ap 调回后，电流比闷感更突出。v1mild（加性 EQ，但仍过 WORLD）在中间：0.32–0.51 / 10–14 dB。",
"- 排除的候选：帧率 zipper（50–200 Hz 调制在 v0/v3 反而更低，0.00–0.06）；F0 倍频错误（v0/v3 为 0–2 次，before 也有 0–2 次）；削波（全部 0%）；跳变 p95 只比 before 高约 0.3–0.5 半音，量级小，不是主因。",
"- 所以修法是彻底不走声码器：v4 直接在原波形上做线性相位滤波。","",
"### 7.2 v4 做法（不重合成，所有增益 ≥30–50 ms 平滑）","",
"- 静态线性相位 FIR（2047/4095 taps）做鼻音闭口塑形：800 Hz F1 区高斯衰减（另在 1.1 kHz 衰减一半），300 Hz 处加 body，1.5 kHz 以上按倍频程缓降并封顶。",
"- v4b 另加自适应：按 50 ms 平滑的 500–1000 Hz 占比，在弱/强两版 FIR 输出之间平滑交叉淡化（不逐帧切换）。",
"- 起音：1.5 kHz 互补线性相位分频，HF 快/慢包络比的瞬态整形（增益平滑 30 ms），加 20 ms 跟随的宽带起音软化（增益平滑 30 ms）。",
"- 情绪：只用 100 ms RMS 包络扩展（±4 dB，增益平滑 50 ms）。",
"","| 变体 | F1 区衰减 dB | body dB | HF 斜率 dB/oct（封顶） | 1.6k 存在感 | 瞬态 k | emo | 自适应 |","|---|---|---|---|---|---|---|---|",
"| v4a | −8 | +2 | −2（−10） | 0 | 0.5 | 0.20 | 否 |","| v4b | −10（强档 −16） | +3 | −3（−12） | 0 | 0.7 | 0.25 | 是 |","| v4c | −6 | +2 | −1.5（−8） | +1 | 0.5 | 0.15 | 否 |","",
"### 7.3 A/B 结果（门：<1200 ≥75%、包络 ≥0.85、实词 ASR 门、无开口 a、500–1000 ≤10%、HF CPP 不比 before 高 2 dB 以上；score = Res + 0.5·CAM++，均对角色）",""]
for ch in ["dania","amiya"]:
    for tag in sorted(set(r["input"] for r in M if r["char"]==ch)):
        md+=[f"#### {ch} / {tag}","","| 变体 | F0调制 | CPP | <1200 | 500–1000% | F1 | onset HF dB | CAM++ 角/他 | Res 角/他 | 包络 | ASR | 门 | score |","|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in [x for x in M if x["char"]==ch and x["input"]==tag]:
            md.append(f"| {r['variant']} | {r['bz_hf_mod_F0_share']} | {r['bz_hf_comb_cpp_db']} | {r['lt1200']:.1f} | {r['e5_10']} | {r['F1']} | {r.get('onset_hf')} | {r['cam_char']}/{r['cam_other']} | {r['res_char']}/{r['res_other']} | {r['env_corr']} | {r['asr']}「{r['asr_text']}」 | {'通过' if r['pass'] else '×'+','.join(r['fails'])} | {r['score']} |")
        md.append("")
top={}
for ch in ["dania","amiya"]:
    c=[r for r in M if r["char"]==ch and r["variant"].startswith("v4")]; c.sort(key=lambda r:(not r["pass"],-r["score"])); top[ch]=c
md+=["### 7.4 每角色前 2（v4）",""]
for ch,rs in top.items():
    for i,r in enumerate(rs[:2],1):
        md.append(f"- **{ch} #{i}** `{r['file']}`：score {r['score']}，CAM++ {r['cam_char']}，Res {r['res_char']}，F0调制 {r['bz_hf_mod_F0_share']}，CPP {r['bz_hf_comb_cpp_db']} dB，<1200 {r['lt1200']:.1f}%，500–1000 {r['e5_10']}%，ASR {r['asr']}「{r['asr_text']}」，{'通过' if r['pass'] else '×'+','.join(r['fails'])}")
    cb=[r for r in rs if r["input"].startswith("C_")][:1]
    if cb: r=cb[0]; md.append(f"  - {ch} C 输入最佳：`{r['file']}`（score {r['score']}，CAM++ {r['cam_char']}，Res {r['res_char']}，{'通过' if r['pass'] else '×'+','.join(r['fails'])}）")
md+=["","- 新文件：`*__world_noedit.wav`（诊断用）和 `*__v4a/b/c.wav`。没有覆盖任何已有文件。明细：`v4_metrics.json`、`asr_v4.json`。脚本：`tools/rvc/scripts/buzz_lib.py`、`v5_v4.py`。",
"- 注意：v4 只是滤波，不能把已经张嘴的共振峰「合上」，只能把它压下去。A 输入的 500–1000 占比已降到 0.8–7.4%，但 F1 估计没有明显下移。"]
p=f"{O}/report.md"; s=open(p).read()
if "\n## 7. v4" in s: s=s[:s.index("\n## 7. v4")]
open(p,"w").write(s+"\n".join(md)+"\n")
for ch,rs in top.items():
    for r in rs: print(ch,r["file"],r["score"],r["pass"],r["fails"],"cam",r["cam_char"],"res",r["res_char"],"F0mod",r["bz_hf_mod_F0_share"],"cpp",r["bz_hf_comb_cpp_db"],"lt",r["lt1200"],"e510",r["e5_10"],"F1",r["F1"],"env",r["env_corr"],r["asr"],r["asr_text"])
