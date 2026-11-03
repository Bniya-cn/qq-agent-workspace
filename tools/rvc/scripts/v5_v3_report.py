import json, os
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v5_closed"
M=json.load(open(f"{O}/v3_metrics.json")); A=json.load(open(f"{O}/asr_v3.json")); G=json.load(open(f"{O}/v3_diag_mosquito.json"))
HUM=set("呜嗚呦哼嗯呃呿喵咪哦喔唔呵嘿唉哎哟噢欸誒呣啪嗷…。，、！？!?,. ~～-|"); CLICK=set("嘟哒嗒都啵波"); OPENA=set("啊阿哈妈嗎吗呀哇啦嗷拉吧爸怕他她它沙杀嘛麻马骂呵嗬")
def asr(f):  # strict, recomputed from transcript text: any real char -> 有实词; any 啊/哈-type char (any confidence) -> 开口a; 嘟/哒/啵 -> +哒
    a=A.get(f)
    if a is None: return "?",""
    t=a.get("text") or ""
    real=[c for c in t if c not in HUM and c not in CLICK and c not in OPENA and not c.isspace() and c!="\ufffd" and not c.isascii()]
    s="有实词" if real else ("开口a" if any(c in OPENA for c in t) else "无字")
    return s+("+哒" if any(c in CLICK for c in t) else ""), t[:16]
for r in M:
    r["asr"],r["asr_text"]=asr(r["file"])
    fails=[]
    if r["lt1200"]<75: fails.append("lt1200")
    if r["variant"]!="before" and r["env_corr"]<0.85: fails.append("env")
    if r["asr"].startswith(("有实词","开口a")): fails.append("asr")
    if r["variant"]!="before" and r["e5_10"]>10: fails.append("e5-10>10%")
    r["fails"]=fails; r["pass"]=not fails; r["score"]=round(r["res_char"]+0.5*r["cam_char"],3)
md=["","## 6. v3：修复 v0 closed_emo 的「闷 / 薄 / 蚊子声」","",
"### 6.1 蚊子声成因（实测）","",
"| 文件 | 频谱倾斜 dB/oct | 100–500% | 500–1000% | 1–2.5k% | 2.5–5k% | 最强窄峰 | ap | HNR |","|---|---|---|---|---|---|---|---|---|"]
for k in ["dania_C_v4_L2_Ab_eqT0.06__before.wav","dania_C_v4_L2_Ab_eqT0.06__closed_emo.wav","amiya_C_rvc_lines_L2_cn008__before.wav","amiya_C_rvc_lines_L2_cn008__closed_emo.wav"]:
    g=G[k]; w=g["whine_top"][0] if g["whine_top"] else {}
    md.append(f"| {k} | {g['tilt_db_per_oct']} | {g['e100_500']} | {g['e500_1000']} | {g['e1000_2500']} | {g['e2500_5000']} | {w.get('f')} Hz, +{w.get('prom_db')} dB, Q {w.get('Q')} | {g['ap']} | {g['hnr']} |")
for ch in ["dania","amiya"]:
    g=G[f"genuine_{ch}_mean5"]; md.append(f"| 真人 {ch}（5 条均值） | {g['tilt_db_per_oct']} | {g['e100_500']} | {g['e500_1000']} | {g['e1000_2500']} | {g['e2500_5000']} | 窄峰 +{g['whine_max_prom_db']} dB | – | – |")
md+=["",
"**结论：** 蚊子声不是低中频不够，而是能量过度集中：",
"- v0 的 100–500 Hz 已占 84–88%（真人 50–67%），2.5–5 kHz 只剩 0.1–0.2%（真人达妮娅 2%、阿米娅 17%），倾斜 −9.4 dB/oct（真人 −4.5 / −1.1）。原因是整包络替换 + 1.2 kHz 以上 −14 dB/kHz 滚降。",
"- ap 被压到 0.03–0.06（真人 0.34–0.50），几乎是纯周期波，没有气息和颗粒感。",
"- 最强窄峰在 ~600–690 Hz、Q 30–44、+13–17 dB，正好是 **2×F0**。F0 稳定时，就剩基频加二次谐波一个尖峰，听起来像正弦哨音，也就是「蚊子」。",
"- 这个峰是谐波、随 F0 移动，所以不能用固定陷波去掉（会掏空声音）。脚本里的 whine notch 加了谐波保护，只削不在 k×F0 上的固定峰；本轮两条都没有触发固定峰陷波。真正的解法是把高频和 ap 补回来、把能量摊开。","",
"### 6.2 v3 变体（以 v0 closed_emo 为基础：morph + F1 warp + emo 0.25；去掉 2.2 kHz 人工鼻峰）","",
"| 变体 | 新包络占比 alpha（原包络保留） | 1.2k 以上滚降 | 凹陷 | ap× | 清音帧增益 | 200–400 Hz | 1–2 kHz | 其他 |","|---|---|---|---|---|---|---|---|---|",
"| v0 closed_emo | 0.6 低频 / 1.0 高频（低频保留 40%，高频 0） | 14 dB/kHz | 18 | 0.2 | 0.12 | 0 | 0 | 2.2k 鼻峰 +3 |",
"| v3a | 0.5（保留 50%） | 8 | 14 | 0.7 | 0.5 | +2 | +2 | – |",
"| v3b | 0.4（保留 60%） | 6 | 12 | 0.8 | 0.5 | +3 | +2 | – |",
"| v3c | 0.6（保留 40%） | 10 | 16 | 0.6 | 0.5 | +2 | +3 | – |",
"| v3d | 0.5 / 0.6 | 6 | 12 | 0.7 | 0.5 | +4 | +1 | + 起音瞬态抑制 |","",
"### 6.3 结果（门：<1200 ≥75%、包络 ≥0.85、实词 ASR 门、无开口 a、500–1000 ≤10%；score = Res对角色 + 0.5·CAM++对角色）",""]
for ch in ["dania","amiya"]:
    for tag in sorted(set(r["input"] for r in M if r["char"]==ch)):
        md+=[f"#### {ch} / {tag}","","| 变体 | <1200 | 倾斜 | 1–2.5k% | 2.5–5k% | 窄峰dB | ap | HNR | F1 | 500–1000% | F0跨度 st | CAM++ 角/他 | Res 角/他 | 包络 | ASR | 门 | score |","|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in [x for x in M if x["char"]==ch and x["input"]==tag]:
            md.append(f"| {r['variant']} | {r['lt1200']:.1f} | {r['tilt_db_per_oct']} | {r['e1000_2500']} | {r['e2500_5000']} | {r['whine_max_prom_db']} | {r['ap']} | {r['hnr']} | {r['F1']} | {r['e5_10']} | {r['f0_range']} | {r['cam_char']}/{r['cam_other']} | {r['res_char']}/{r['res_other']} | {r['env_corr']} | {r['asr']}「{r['asr_text']}」 | {'通过' if r['pass'] else '×'+','.join(r['fails'])} | {r['score']} |")
        md.append("")
top={}
for ch in ["dania","amiya"]:
    c=[r for r in M if r["char"]==ch and r["variant"].startswith("v3")]
    c.sort(key=lambda r:(not r["pass"],-r["score"])); top[ch]=c[:2]
md+=["### 6.4 每角色前 2",""]
bestC={}
for ch in ["dania","amiya"]:
    c=[r for r in M if r["char"]==ch and r["variant"].startswith("v3") and r["input"].startswith("C_")]
    c.sort(key=lambda r:(not r["pass"],-r["score"])); bestC[ch]=c[:2]
for ch,rs in top.items():
    for i,r in enumerate(rs,1):
        md.append(f"- **{ch} #{i}** `{r['file']}`：score {r['score']}，CAM++ {r['cam_char']}，Res {r['res_char']}，<1200 {r['lt1200']:.1f}%，500–1000 {r['e5_10']}%，倾斜 {r['tilt_db_per_oct']}，2.5–5k {r['e2500_5000']}%，ap {r['ap']}，ASR {r['asr']}，{'通过' if r['pass'] else '×'+','.join(r['fails'])}")
md+=["","用户偏好的 C 输入（closed_emo 基础）前 2："]
for ch,rs in bestC.items():
    for i,r in enumerate(rs,1):
        md.append(f"- **{ch} C #{i}** `{r['file']}`：score {r['score']}，CAM++ {r['cam_char']}，Res {r['res_char']}，<1200 {r['lt1200']:.1f}%，500–1000 {r['e5_10']}%，倾斜 {r['tilt_db_per_oct']}，2.5–5k {r['e2500_5000']}%，ap {r['ap']}，ASR {r['asr']}「{r['asr_text']}」，{'通过' if r['pass'] else '×'+','.join(r['fails'])}")
md+=["","- ASR 门在本报告里按转写文本严格重算：任意实字算有字，任意 啊/哈 类字算开口 a（不论置信度）；嘟/哒/啵 只标 +哒（唇/舌起音），不算实词。","- 所有 v3 文件都是新名字（`__v3a`…`__v3d`），v0、v1、v2 均未覆盖。明细：`v3_metrics.json`、`asr_v3.json`、`v3_diag_mosquito.json`。"]
p=f"{O}/report.md"; s=open(p).read()
if "## 6. v3" in s: s=s[:s.index("\n## 6. v3")]
open(p,"w").write(s+"\n".join(md)+"\n")
json.dump({k:[{x:r[x] for x in ["file","score","pass","fails","cam_char","res_char","lt1200","e5_10","tilt_db_per_oct","e2500_5000","ap","asr"]} for r in v] for k,v in top.items()},open(f"{O}/v3_top2.json","w"),ensure_ascii=False,indent=1)
for ch,rs in top.items():
    for r in rs: print("TOP",ch, r["file"], r["score"], r["pass"], r["fails"], r["cam_char"], r["res_char"], r["lt1200"], r["e5_10"], r["asr"], r["asr_text"])
for ch,rs in bestC.items():
    for r in rs: print("C",ch,r["file"],r["score"],r["pass"],r["fails"],r["cam_char"],r["res_char"],r["lt1200"],r["e5_10"],r["tilt_db_per_oct"],r["e2500_5000"],r["ap"],r["asr"],r["asr_text"])
print("--- all v3 gate:")
for r in M:
    if r["variant"].startswith(("v3","v0")): print(r["file"], r["pass"], r["fails"], r["asr"], r["asr_text"], r["score"])
