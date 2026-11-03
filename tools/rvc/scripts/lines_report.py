import json, os, statistics as st
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v3_rvc"; D=f"{O}/lines_v4_amiya"
E=json.load(open(f"{D}/eval.json")); A=json.load(open(f"{D}/asr.json")) if os.path.exists(f"{D}/asr.json") else {}; AI=json.load(open(f"{D}/asr_inputs.json")) if os.path.exists(f"{D}/asr_inputs.json") else {}
ins,outs=E["inputs"],E["outputs"]
for m in list(ins.values())+list(outs.values()): m["f0med_pyin"]=m["f0med"]; m["f0med"]=m.get("f0rm") if m.get("f0rm") is not None else None
_b={}
for b,m in outs.items():  # one per (input,ckpt,ir): keep the transpose whose output F0 is closest to 292 Hz
    k=(m["input"],m["ckpt"],m["ir"]); d=abs((m["f0med"] or 1e4)-292)
    if k not in _b or d<_b[k][0]: _b[k]=(d,b)
NALL=len(outs); outs={b:outs[b] for _,b in _b.values()}
OA=set("啊阿哈呀哇啦嗷吗嘛")
def asr(d,b):  # strict: any >=2 distinct lexical chars = words (even low-confidence); any 啊/哈-type char in transcript = open-a
    a=d.get(b)
    if a is None: return "?"
    if a["words"] or len(a.get("lexical_chars",""))>=2: return "有字"
    if a["open_a"] or any(c in OA for c in a.get("text","")): return "开口a"
    return "无字"
for b,m in outs.items():
    m["asr"]=asr(A,b); m["f0ok"]= m["f0med"] is not None and 285<=m["f0med"]<=300
    m["pass"]= m["f0ok"] and m["asr"]=="无字" and m["lt1200"]>=75 and m.get("env_corr",0)>=0.85
    m["score"]=round(m["res_margin"]+0.5*m["cam_margin"],4)
def mean(rs,k): v=[r[k] for r in rs if r.get(k) is not None]; return round(st.mean(v),3) if v else None
md=["## 4b. 追加：v4 阿米娅拟声 TTS（Ab_eqT*）→ RVC 阿米娅（F0 先移到 285–300 Hz）","",
f"- 输入：`exp_gagged_v4_lines/muffled/amiya_*_Ab_eqT*.wav` + `g3_phrase/amiya_L6_*_Ab_eqT*.wav`，共 {len(ins)} 条（g3splice 拼接版含原 g3 发声人，排除）。原文件只读，输出在 `exp_gagged_v3_rvc/lines_v4_amiya/`。",
"- 移调：逐条 pyin 中位 F0 → transpose = round(12·log2(292/F0))，音高曲线随源、仅整体平移；RVC 参数 protect 0.5、rms_mix 0.25、index_rate 0.5/0.6（≤0.6），checkpoint 60e（及 40e@0.6）。",
f"- F0 校正：首轮按输入 pyin F0 定移调，二轮按输出 pyin F0 修正，三轮按输入 RMVPE F0 重定移调 round(12·log2(292/F0))（共 {NALL} 条推理），每个 输入×配置 取 F0 最接近 292 Hz 的一条进入统计。",
"- 指标：margin = 对阿米娅质心余弦 − 对达妮娅质心余弦（CAM++ 与 Resemblyzer 各一）；<1200Hz 能量占比；包络相关（对输入）；faster-whisper small ASR。score = Res margin + 0.5·CAM++ margin；F0 = RMVPE 中位（RVC 自身的 F0 估计器；pyin 在闷哼上常锁到谐波/上限，不再用于判定）。pass = 输出 F0 在 285–300 Hz 且 ASR 严格无字（≥2 个不同实词字即算有字，含低置信段）、转写中无 啊/哈 类开口音 且 <1200 ≥75% 且包络 ≥0.85。",""]
rows_in=list(ins.values())
md+=["| 组 | 条数 | F0中位 | F0在285–300 | <1200% | >3k% | CAM++阿米娅 | CAM++达妮娅 | CAM++ margin | Res阿米娅 | Res达妮娅 | Res margin | ASR无字 | 通过 |","|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
def grow(name,rs,asrd,keyf):
    nz=sum(1 for r in rs if asr(asrd,keyf(r))=="无字")
    f0ok=sum(1 for r in rs if r["f0med"] and 285<=r["f0med"]<=300)
    md.append(f"| {name} | {len(rs)} | {mean(rs,'f0med')} | {f0ok} | {mean(rs,'lt1200')} | {mean(rs,'gt3k')} | {mean(rs,'cam_amiya')} | {mean(rs,'cam_dania')} | {mean(rs,'cam_margin')} | {mean(rs,'res_amiya')} | {mean(rs,'res_dania')} | {mean(rs,'res_margin')} | {nz} | {sum(1 for r in rs if r.get('pass')) if 'pass' in (rs[0] if rs else {}) else '-'} |")
grow("输入（v4 原样）",rows_in,AI,lambda r:os.path.basename(r["path"]))
for ck,ir in [("60e",0.5),("60e",0.6),("40e",0.6)]:
    rs=[r for r in outs.values() if r["ckpt"]==ck and r["ir"]==ir]
    if rs: grow(f"RVC {ck} ir{ir}",rs,A,lambda r:os.path.basename(r["path"]))
md.append("")
# paired delta
d=[(r["res_margin"]-ins[r["input"]]["res_margin"], r["cam_margin"]-ins[r["input"]]["cam_margin"]) for r in outs.values()]
if d: md.append(f"- 相对输入的平均变化：Res margin {st.mean(x for x,_ in d):+.3f}，CAM++ margin {st.mean(y for _,y in d):+.3f}；margin 转正的输出 Res {sum(1 for r in outs.values() if r['res_margin']>0)}/{len(outs)}，CAM++ {sum(1 for r in outs.values() if r['cam_margin']>0)}/{len(outs)}。")
top=sorted([r for r in outs.values() if r["pass"]],key=lambda r:-r["score"])
seen=set(); best=[]
for r in top:
    k=r["input"].split("_Ab_")[0]
    if k in seen: continue
    seen.add(k); best.append(r)
md+=["",f"通过 gate 的输出 {len(top)}/{len(outs)}；按 score 前 5（同一 台词+seed 只取一条）：","","| # | 文件 | 输入 | ckpt/ir | 移调 | F0 | <1200% | 包络 | CAM++ 阿/达 (margin) | Res 阿/达 (margin) | ASR |","|---|---|---|---|---|---|---|---|---|---|---|"]
for k,r in enumerate(best[:5]):
    a=A.get(os.path.basename(r["path"]),{})
    md.append(f"| {k+1} | `lines_v4_amiya/{os.path.basename(r['path'])}` | {r['input']} | {r['ckpt']}/{r['ir']} | {r['pitch']:+d} | {r['f0med']} | {round(r['lt1200'],1)} | {r['env_corr']} | {r['cam_amiya']}/{r['cam_dania']} ({r['cam_margin']:+.3f}) | {r['res_amiya']}/{r['res_dania']} ({r['res_margin']:+.3f}) | 「{(a.get('text') or '')[:20]}」 |")
if not best: md.append("| - | 无输出通过 gate | | | | | | | | | |")
md+=["","结论（本轮）：","- RVC 阿米娅让 CAM++ margin 平均 +0.05（−0.054 → ≈0），Resemblyzer margin +0.02 但仍为负（≈−0.02）：身份向阿米娅移动，但客观上仍未明显区分于达妮娅；cn008 参考的输入经 RVC 后 CAM++ margin 最高（+0.04~+0.07）。","- 亮度：>3k 由 16% 降到 ≈8.5%，但 <1200 平均由 75.9% 降到 ≈69%（1.2–3k 增加），只有部分条目过 75% 门限；ASR 无字比例也略降（RVC 让部分哼声更像元音）。","- ckpt 60e 优于 40e；index_rate 0.6 比 0.5 通过数略多（13 vs 9），未超过 0.6 上限。","- ASR 使用严格判定；pyin 在闷哼上易锁谐波，F0 判定改用 RMVPE（原 pyin 值保留在 eval.json 的 f0med_pyin/原字段）。","","完整明细：`lines_v4_amiya/eval.json`、`asr.json`、`asr_inputs.json`、`input_f0.json`、`jobs.json`。"]
open(f"{O}/lines_v4_amiya_section.md","w").write("\n".join(md)+"\n")
json.dump(dict(top5=[dict(file=r["path"],**{k:r[k] for k in ['input','ckpt','ir','pitch','f0med','lt1200','env_corr','cam_amiya','cam_dania','cam_margin','res_amiya','res_dania','res_margin','asr','score']}) for r in best[:5]],n_pass=len(top),n=len(outs)),open(f"{D}/summary.json","w"),ensure_ascii=False,indent=1)
print("\n".join(md))
