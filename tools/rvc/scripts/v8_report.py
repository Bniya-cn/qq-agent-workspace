import json, os, re, numpy as np
W="/Users/daijinglin/Desktop/qq-agent-workspace"; G=f"{W}/bot/data/test_output/exp_gagged_v8_gen"; O5=f"{W}/bot/data/test_output/exp_gagged_v5_closed"
M=json.load(open(f"{G}/v8_metrics.json")); A={}
for f in ["asr_v8_rvc.json","asr_v8_sovits.json"]:
    if os.path.exists(f"{G}/{f}"): A.update({os.path.basename(k):v for k,v in json.load(open(f"{G}/{f}")).items()})
HUM=set("呜嗚呦哼嗯呃呿喵咪哦喔唔呵嘿唉哎哟噢欸誒呣啪嗷…。，、！？!?,. ~～-|"); CLICK=set("嘟哒嗒都啵波"); OPENA=set("啊阿哈妈嗎吗呀哇啦嗷拉吧爸怕他她它沙杀嘛麻马骂呵嗬")
def asr(f):
    a=A.get(os.path.basename(f))
    if a is None: return "?",""
    t=a.get("text") or ""
    real=[c for c in t if c not in HUM and c not in CLICK and c not in OPENA and not c.isspace() and c!="\ufffd" and not c.isascii()]
    if any(c.isascii() and c.isalpha() for c in t): real.append("EN")
    return ("有实词" if real else ("开口a" if any(c in OPENA for c in t) else "无字"))+("+哒" if any(c in CLICK for c in t) else ""), t[:14]
REF={r["file"]:r for r in M if r["kind"]=="ref"}
def ref_for(r):
    f=r["file"]; ch=r["char"]
    m=re.search(r"(gagged_\d)",f)
    if r["kind"]=="rvc" and m:
        k=[x for x in M if x["kind"]=="rvc" and x["char"]==ch and m.group(1) in x["file"] and "fullidx" in x["file"] and not x["eq"]]
        if k: return k[0]
    return REF[f"{ch}_C_v4_L2_Ab_eqT0.06__before.wav" if ch=="dania" else "amiya_C_rvc_lines_L2_cn008__before.wav"]
for r in M:
    r["asr"],r["asr_text"]=asr(r["file"]) if r["kind"]!="ref" else ("","")
    if r["kind"]=="ref": continue
    b=ref_for(r); r["ref"]=b["file"]; f=[]
    if r["bw"]<8000: f.append("带宽<8k")
    gt=lambda a,c,d: a is not None and c is not None and a>c+d
    if gt(r["F0mod"],b["F0mod"],0.10): f.append("F0调制↑")
    if gt(r["cpp"],b["cpp"],1.5): f.append("CPP↑")
    if r["hnr"] is None or (b["hnr"] is not None and r["hnr"]<b["hnr"]-1.0): f.append("HNR↓/无")
    if gt(r["creak"],(b["creak"] or 0),2): f.append("creak↑")
    if gt(r["jit"],b["jit"],0.3): f.append("jitter↑")
    if r["hvn"] is None or r["hvn"]>-4: f.append("hi_vs_n2>-4")
    if r["lt1200"]<75: f.append("lt1200")
    if r["asr"].startswith(("有实词","开口a","?")): f.append("ASR")
    r["fails"]=f; r["pass"]=not f; r["syl_gain"]=round((r["syl_depth"] or 0)-(b["syl_depth"] or 0),2)
    r["score"]=round(r["res"]+0.5*r["cam"]+0.1*(5-(r["dist"] if r["dist"] is not None else 8))+0.01*max(-10,min(10,r["syl_gain"])),3)
C=[r for r in M if r["kind"]!="ref" and "fullidx" not in r["file"]]
def short(f): return f.replace("rvc/","").replace("sovits/","")
md=["","## 10. v8：在生成阶段闭口（RVC 真鼻音检索索引 + GPT-SoVITS 真哼声参考；后处理最多轻 EQ）","",
"### 10.1 做法","",
"- **真鼻音段扩充**：所有官方片段都跑了字级 ASR（嗯/哼/恩/唔/呣 + 鼻音声母字），再加声学鼻音检测（浊音、<500 Hz ≥85%、500–1000 ≤6%、hi_vs_n2 ≤ −5 dB、≥40 ms），两者取并集。dania 849 段 / 100.0 s，amiya 623 段 / 62.1 s（目标 1–3 分钟）。`exp_gagged_v6_nasal/genuine_nasal_spans_v8.json`。",
"- **只含鼻音的 RVC 检索索引**：在整段官方片段上提 contentvec（保留上下文），只保留落在鼻音段里的帧。新建 `applio/logs/{ch}_gag_rvc/{ch}_gag_rvc_NASALONLY_v8.index`（dania 4989 向量 IVF127，amiya 3097 向量 IVF79），原有索引没动。",
"- **RVC 推理**：60e 模型。源为 g1、g2、原版 g3，以及 v4_lines L1–5 Ab_eqT0.06。F0 调到 dania 250 / amiya 292 Hz。gag 源跑 ir 0.6/0.75/0.9 × protect 0.33/0.5，台词跑 (0.75, 0.5) 和 (0.9, 0.33)。对照组 `fullidx` = 原完整索引 ir 0.6 / pr 0.5。",
"- **GPT-SoVITS**：参考音频 = 角色自己的真哼声音节 + 最长的鼻音段拼接。dania 6.4 s（4 个哼/嗯 + 7 段鼻音），amiya 6.1 s（8 个嗯 + 6 段鼻音）。prompt_text 每段一个字（哼，嗯，…）。拟声来自 gag_onomatopoeia.text_to_gag，5 句 × 种子 42/7/123。官方语音里真哼声很少（ASR 只找到 dania 5 个、amiya 9 个），所以用鼻音段补足。",
"- **后处理**：每个原始输出另存 `__eq` 版：线性相位 F1 −5 dB + 100 ms 慢情绪包络（±3 dB）。不走 WORLD，不做 LPC，不加组织低通、F0 勾、谷加深，保持原生 SR。","",
"### 10.2 鼻音索引 vs 完整索引（gag 源，原始输出，按角色平均）","","| 角色 | 组 | n | dist | hi_vs_n2 | 500–1000% | <1200 | 带宽 | HNR | CAM++ | Res |","|---|---|---|---|---|---|---|---|---|---|---|"]
for ch in ["dania","amiya"]:
    for lab,fn in [("fullidx ir0.6",lambda f:"fullidx" in f),("nasidx ir0.6",lambda f:"nasidx_ir0.6_" in f),("nasidx ir0.75",lambda f:"nasidx_ir0.75_" in f),("nasidx ir0.9",lambda f:"nasidx_ir0.9_" in f)]:
        rs=[r for r in M if r["kind"]=="rvc" and r["char"]==ch and not r["eq"] and "gagged" in r["file"] and fn(r["file"])]
        if rs:
            av=lambda k: round(float(np.mean([r[k] for r in rs if r[k] is not None])),3)
            md.append(f"| {ch} | {lab} | {len(rs)} | {av('dist')} | {av('hvn')} | {av('e5_10')} | {av('lt1200')} | {av('bw')} | {av('hnr')} | {av('cam')} | {av('res')} |")
md+=["","### 10.3 全部候选（门：带宽 ≥8k、F0调制 ≤ ref+0.10、CPP ≤ ref+1.5、HNR ≥ ref−1、creak ≤ ref+2%、jitter ≤ ref+0.3 st、hi_vs_n2 ≤ −4、<1200 ≥75%、实词 ASR 门。ref = 同源 fullidx（gag 源）或该角色 C 输入 before（台词/SoVITS）。score = Res + 0.5·CAM++ + 0.1·(5−dist) + 0.01·音节深度增益）",""]
for ch in ["dania","amiya"]:
    rs=sorted([r for r in C if r["char"]==ch],key=lambda r:(not r["pass"],-r["score"]))
    md+=[f"#### {ch}（{len(rs)} 个，按门/分排序，列前 25）","","| 文件 | 带宽 | F0调制 | CPP | HNR | creak% | jitter | dist | hi_vs_n2 | <1200 | 音节深度（增益） | F0中位 | F0范围 | RMS动态 | CAM++ | Res | ASR | 门 | score |","|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rs[:25]:
        md.append(f"| {short(r['file'])} | {r['bw']} | {r['F0mod']} | {r['cpp']} | {r['hnr']} | {r['creak']} | {r['jit']} | {r['dist']} | {r['hvn']} | {r['lt1200']} | {r['syl_depth']} ({r['syl_gain']:+}) | {r['f0_med']} | {r['f0_range']} | {r['rms_dyn']} | {r['cam']} | {r['res']} | {r['asr']}「{r['asr_text']}」 | {'通过' if r['pass'] else '×'+','.join(r['fails'])} | {r['score']} |")
    md.append(f"- 通过数：{sum(r['pass'] for r in rs)}/{len(rs)}（RVC {sum(r['pass'] for r in rs if r['kind']=='rvc')}，SoVITS {sum(r['pass'] for r in rs if r['kind']=='sovits')}）")
    md.append("")
top={ch:sorted([r for r in C if r["char"]==ch],key=lambda r:(not r["pass"],-r["score"])) for ch in ["dania","amiya"]}
md+=["### 10.4 每角色前 3（v8）",""]
for ch,rs in top.items():
    for i,r in enumerate(rs[:3],1):
        md.append(f"- **{ch} #{i}** `{short(r['file'])}`：score {r['score']}，带宽 {r['bw']}，HNR {r['hnr']}，dist {r['dist']}，hi_vs_n2 {r['hvn']}，<1200 {r['lt1200']}%，音节深度 {r['syl_depth']}，CAM++ {r['cam']}，Res {r['res']}，F0调制 {r['F0mod']}，CPP {r['cpp']}，ASR {r['asr']}「{r['asr_text']}」，{'通过' if r['pass'] else '×'+','.join(r['fails'])}")
    sv=[r for r in rs if r["kind"]=="sovits"][:1]
    if sv: r=sv[0]; md.append(f"  - {ch} SoVITS 最佳：`{short(r['file'])}`（score {r['score']}，dist {r['dist']}，CAM++ {r['cam']}，Res {r['res']}，ASR「{r['asr_text']}」，{'通过' if r['pass'] else '×'+','.join(r['fails'])}）")
md+=["","- 文件：`bot/data/test_output/exp_gagged_v8_gen/{rvc,sovits}/`（`__eq` = 轻 EQ 版）。明细：`v8_metrics.json`、`asr_v8_rvc.json`、`asr_v8_sovits.json`。脚本：`tools/rvc/scripts/v8_*.py`。没有覆盖任何已有文件。"]
p=f"{O5}/report.md"; s=open(p).read()
if "\n## 10. v8" in s: s=s[:s.index("\n## 10. v8")]
open(p,"w").write(s+"\n".join(md)+"\n")
for ch,rs in top.items():
    print(ch,"pass",sum(r["pass"] for r in rs),"/",len(rs))
    for r in rs[:8]: print(" ",short(r["file"]),r["score"],r["pass"],r["fails"],"bw",r["bw"],"hnr",r["hnr"],"cr",r["creak"],"jit",r["jit"],"dist",r["dist"],"hvn",r["hvn"],"lt",r["lt1200"],"syl",r["syl_depth"],"cam",r["cam"],"res",r["res"],"F0m",r["F0mod"],"cpp",r["cpp"],"f0",r["f0_med"],"f0r",r["f0_range"],r["asr"],r["asr_text"])
    for r in [x for x in rs if x["kind"]=="sovits"][:3]: print(" SV",short(r["file"]),r["score"],r["pass"],r["fails"],"dist",r["dist"],"hvn",r["hvn"],"cam",r["cam"],"res",r["res"],r["asr_text"])
