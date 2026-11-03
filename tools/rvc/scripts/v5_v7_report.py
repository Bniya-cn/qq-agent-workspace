import json, os
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v5_closed"; O6=f"{W}/bot/data/test_output/exp_gagged_v6_nasal"
M=json.load(open(f"{O}/v7_metrics.json")); D=json.load(open(f"{O6}/oldtv_diag.json")); A={}
for f in ["asr_v7.json","asr_v6.json","asr_v4.json","asr_v3.json","asr_v1v2.json","asr.json"]:
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
    r["asr"],r["asr_text"]=asr(r["file"]); b=B[(r["char"],r["input"])]; f=[]; v=r["variant"]!="before"
    if r["lt1200"]<75: f.append("lt1200")
    if v and r["env_corr"]<0.75: f.append("env")
    if r["asr"].startswith(("有实词","开口a")): f.append("asr")
    if v and r["e5_10"]>10: f.append("e5-10>10%")
    if v and r["bz_hf_comb_cpp_db"]>b["bz_hf_comb_cpp_db"]+2: f.append("buzz(cpp)")
    if v and r["bz_hf_mod_F0_share"]>b["bz_hf_mod_F0_share"]+0.15: f.append("buzz(F0mod)")
    if v and r["nc_hi_vs_n2"]>-4: f.append("口腔F2(hi_vs_n2>-4)")
    if v and r["tv_bw50"]<8000: f.append("老电视(BW<8k)")
    if v and r.get("rms_dyn") is not None and b.get("rms_dyn") is not None and abs(r["rms_dyn"]-b["rms_dyn"])>8: f.append("情绪RMS")
    r["syl_gain"]=round(r["tv_syl_depth_db"]-b["tv_syl_depth_db"],2); r["nas_gain"]=round(r["tv_nasal_lvl_db"]-b["tv_nasal_lvl_db"],1)
    r["fails"]=f; r["pass"]=not f
    r["score"]=round(r["res_char"]+0.5*r["cam_char"]+0.1*(5-r["nc_dist"])+0.01*max(-10,min(r["syl_gain"],10))+0.005*max(-15,min(r["nas_gain"],15)),3)
md=["","## 9. v7：原生采样率 + 加重「呜呜嗯嗯」+ 交叉合成（修 v6「老电视」）","","### 9.1 「老电视」成因（实测，原生 SR，`exp_gagged_v6_nasal/oldtv_diag.json`）","",
"| 文件 | SR | 有效带宽 Hz（LTAS ≥ 峰−50 dB） | 99% / 99.9% 能量滚降 Hz | 4–8 kHz 底噪 dB（相对 0.1–1 kHz） | >8 kHz dB | 凹口数 | 200 Hz hop 调制 |","|---|---|---|---|---|---|---|---|"]
for k,r in D.items(): md.append(f"| {k} | {r['sr']} | {r['bw50']} | {r['roll99']} / {r['roll999']} | {r['hf_floor_db']} | {r['hf_8k_db']} | {r['notches']} | {r['hop200_mod']} |")
md+=["","**成因：** 主要是 v6 的 2 阶组织低通（2–2.5 kHz）。它用 sosfiltfilt 双向跑，等效 4 阶 −24 dB/oct，再叠上 1.45/1.55 kHz 零点，把有效内容压进约 200–2500 Hz：v6 的 99% 能量滚降只有 0.9–2.4 kHz，99.9% 滚降 2.1–4.7 kHz（before 是 5.7–12.4 kHz）。这是典型的老收音机 / 老电视带通。16 kHz 输出又把 8 kHz 以上的空气全部切掉（before 那里还有 −26 至 −39 dB）。凹口数和 hop 调制都不比 before 高，所以 LPC 残差梳状和帧滤波交叉淡化不是主因。","",
"### 9.2 v7 做法","",
"- **整条链路原生 SR**（40k/32k/44.1k）：LPC-16/LSF 只在 16 kHz 副本上分析，得到的 |A_frame/A_target| 增益（帧间平滑约 24 ms、限幅 ±24 dB）在原生 SR 上用 STFT（约 40 ms 窗，1/4 跳）施加；7.6 kHz 以上增益取 7–7.6 kHz 的均值。零点和低通都在原生 SR 上做。",
"- **组织低通改轻**：1 阶 3–3.5 kHz（−6 dB/oct，单向）。",
"- **高频空气底**：4 kHz 二阶互补分频。>4 kHz 由处理后高频与原始高频（跟随处理后响度）各半组成，按功率求解，使 4–8 kHz 密度相对 0.1–1 kHz 达到 −35 / −40 / −45 dB。不砍砖墙。",
"- **加重哼声**：按 30 ms 包络找音节核（突出度 ≥4 dB，间隔 ≥120 ms）。每个音节做以下三件事：",
"  - 40–80 ms 升余弦起音 + 180 ms 回落的压力鼓起（+3/+4 dB）；",
"  - 同一曲线控制 180–450 Hz 鼻腔共振推挤（+4/+6 dB），外加静态 N1 抬升 +3/+4 dB；",
"  - 用 30 ms / 300 ms 包络比的幂（k = 0.6/0.8，下限 −10/−12 dB，不门控）加深音节间谷。",
"  - 起音处做 F0 下勾（−1.5/−2 半音，80 ms 回正，之后约 +0.3 半音补偿；读指针变速，总时长不变），模拟用力的「嗯↑」。",
"- **v7d/e 交叉合成**：角色输出的 LPC 残差（角色的源、音高、身份），换到「时间对齐的 gag 源包络（d 45% / e 65%）+ 角色真鼻音包络」上。g1/g2 的 F0 是 590–690 Hz，所以 gag 包络用 CheapTrick（仅分析）→ 自相关 → LPC-16。对齐已检查：A 输入与 Seed-VC g1bs/g2bs final 相对 gag 源滞后 0 ms，包络相关 0.95–0.995。gag 只在渲染时当滤波器用，不进任何角色模型训练。","",
"| 变体 | 路径 | 鼻音 w / gag 权重 | 混 v4 | 低通 | 空气底 | 哼声 | F0 勾 |","|---|---|---|---|---|---|---|---|",
"| v7a | v6c 原生 SR | w 0.6 | 30% | 1 阶 3.5k | −40 | 中（+3/+4/+3, k0.6, 60 ms） | −1.5 st |",
"| v7b | 同上，哼声更重 | w 0.6 | 30% | 1 阶 3k | −35 | 强（+4/+6/+4, k0.8, 50 ms） | −2 st |",
"| v7c | 更闭口 | w 0.7 | 20% | 1 阶 3k | −45 | 强 | −2 st |",
"| v7d | 交叉合成 | gag 0.45 + 真鼻音 0.55 | 0 | 1 阶 3.5k | −40 | 中 | −1.5 st |",
"| v7e | 交叉合成 | gag 0.65 + 真鼻音 0.35 | 0 | 1 阶 3k | −40 | 强 | −2 st |",
"| v7f | 修正轮：v7a + 更宽带 | w 0.6 | 30% | 1 阶 4k | −35 | 中偏强（N1 +2, push +5, swell +3.5, k0.7） | −1.5 st |",
"| v7g | 修正轮：强哼 + 保身份 | w 0.6 | 40% | 1 阶 4k | −35 | 强（N1 +2） | −2 st |","",
"### 9.3 A/B（门：<1200 ≥75%、包络 ≥0.75（用户要求加深音节谷，原 0.85 放宽）、实词 ASR、500–1000 ≤10%、CPP ≤ before+2、F0调制 ≤ before+0.15、hi_vs_n2 ≤ −4、有效带宽 ≥8 kHz、RMS 动态变化 ≤8 dB；score = Res + 0.5·CAM++ + 0.1·(5−dist) + 0.01·音节深度增益 + 0.005·鼻共振增益）",""]
for ch in ["dania","amiya"]:
    for tag in [t for t in dict.fromkeys(r["input"] for r in M if r["char"]==ch)]:
        md+=[f"#### {ch} / {tag}","","| 变体 | SR | 带宽 | 99.9%滚降 | HF底 | F0调制 | CPP | dist | hi_vs_n2 | 音节深度 dB（增益） | 鼻共振 dB（增益） | <1200 | F0范围 | RMS动态 | CAM++ | Res | ASR | 门 | score |","|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in [x for x in M if x["char"]==ch and x["input"]==tag]:
            md.append(f"| {r['variant']} | {r['tv_sr']} | {r['tv_bw50']} | {r['tv_roll999']} | {r['tv_hf_floor_db']} | {r['bz_hf_mod_F0_share']} | {r['bz_hf_comb_cpp_db']} | {r['nc_dist']} | {r['nc_hi_vs_n2']} | {r['tv_syl_depth_db']} ({r['syl_gain']:+}) | {r['tv_nasal_lvl_db']} ({r['nas_gain']:+}) | {r['lt1200']:.1f} | {r.get('f0_range')} | {r.get('rms_dyn')} | {r['cam_char']} | {r['res_char']} | {r['asr']}「{r['asr_text']}」 | {'通过' if r['pass'] else '×'+','.join(r['fails'])} | {r['score']} |")
        md.append("")
top={}
for ch in ["dania","amiya"]:
    c=[r for r in M if r["char"]==ch and r["variant"].startswith("v7")]; c.sort(key=lambda r:(not r["pass"],-r["score"])); top[ch]=c
md+=["### 9.4 每角色前 2（v7）",""]
for ch,rs in top.items():
    for i,r in enumerate(rs[:2],1):
        md.append(f"- **{ch} #{i}** `{r['file']}`：score {r['score']}，SR {r['tv_sr']}，带宽 {r['tv_bw50']} Hz，HF底 {r['tv_hf_floor_db']} dB，dist {r['nc_dist']}，hi_vs_n2 {r['nc_hi_vs_n2']}，音节深度 {r['tv_syl_depth_db']} dB（{r['syl_gain']:+}），鼻共振 {r['nas_gain']:+} dB，CAM++ {r['cam_char']}，Res {r['res_char']}，F0调制 {r['bz_hf_mod_F0_share']}，CPP {r['bz_hf_comb_cpp_db']}，<1200 {r['lt1200']:.1f}%，ASR {r['asr']}「{r['asr_text']}」，{'通过' if r['pass'] else '×'+','.join(r['fails'])}")
md+=["","- 新文件：`exp_gagged_v5_closed/*__v7a..e.wav`（原生 SR）。另有 Seed-VC 输入副本 `dania_S2_seedvc_g2bs_s-13__before.wav`、`dania_S1_seedvc_g1bs_s-16__before.wav`、`amiya_S1_seedvc_g1bs_s-13__before.wav`（amiya g2bs 用已有的 `amiya_B_seedvc_g2bs_final__before.wav`）。没有覆盖任何已有文件。",
"- 注意：「有效带宽」（峰−50 dB）对 4–8 kHz 底噪很敏感。−35 dB 底噪的变体在 6.9k 与 11.7k 之间跳（N1/鼻腔推挤抬高了峰值），所以 v7a/v7f/v7g 的部分「老电视」判定是临界值，要结合 99.9% 滚降（v7 为 4–7 kHz，v6 为 2–4.7 kHz，before 为 6–12 kHz）和 HF 底一起看。",
"- 交叉合成（v7d/e）结论：g2 输入上 dist 3.7–5.9，比 v7a 差，CAM++ 也更低；g1bs 输入直接失败（500–1000 >10%、dist 约 10）。原因是 g1/g2 的 F0 达 590–690 Hz，包络只在 ≥600 Hz 谐波处被采样，而 g1 本身也不是闭口声道形状（step 1 中 g1 离真鼻音 dist = 27）。所以不建议继续这条路，除非换用低 F0 的 gag 材料（如 g3 原版）。",
"- 身份：v7 的 CAM++ 普遍比 v6 低约 0.07–0.12（N1 抬升 + F0 勾 + 谷加深都会改变 speaker embedding）。",
"- 明细：`v7_metrics.json`、`asr_v7.json`。脚本：`tools/rvc/scripts/tv_lib.py`、`v7_lib.py`、`v7_diag.py`、`v5_v7.py`、`v5_v7_report.py`。"]
p=f"{O}/report.md"; s=open(p).read()
if "\n## 9. v7" in s: s=s[:s.index("\n## 9. v7")]
open(p,"w").write(s+"\n".join(md)+"\n")
for ch,rs in top.items():
    for r in rs: print(ch,r["file"],r["score"],r["pass"],r["fails"],"bw",r["tv_bw50"],"hf",r["tv_hf_floor_db"],"dist",r["nc_dist"],"hvn",r["nc_hi_vs_n2"],"syl",r["tv_syl_depth_db"],r["syl_gain"],"nas",r["nas_gain"],"cam",r["cam_char"],"res",r["res_char"],"F0m",r["bz_hf_mod_F0_share"],"cpp",r["bz_hf_comb_cpp_db"],"lt",round(r["lt1200"],1),"f0r",r.get("f0_range"),"rms",r.get("rms_dyn"),r["asr"],r["asr_text"])
for k,b in B.items(): print("before",k,"bw",b["tv_bw50"],"syl",b["tv_syl_depth_db"],"cam",b["cam_char"],"res",b["res_char"],"f0r",b.get("f0_range"),"rms",b.get("rms_dyn"),"F0m",b["bz_hf_mod_F0_share"],b["asr_text"] if "asr_text" in b else "")
