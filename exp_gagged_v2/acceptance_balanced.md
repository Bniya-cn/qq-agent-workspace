# 封嘴语音 balanced 轮 A/B 验收（参照/对照，非主线）

- 日期：2026-10-20（America/Chicago；报告写入约 00:30 本机时间）
- 目录：`bot/data/test_output/exp_gagged_v2/vc_final_balanced/`（主验 `*_envdyn_final.wav`）
- 排名表：`metrics_balanced.tsv` / `metrics_balanced.json`
- 辅材：`asr_balanced.json`、`_emb_rmvpe_balanced.json`、`_post_balanced.json`、`balanced_refs/`
- 自算：`_self_metrics_balanced_accept.json`（gpt-sovits/.venv；Peak/RMS/频带/质心/1.85s 局部能量）
- 对照：g12 首选 `vc_final_g12/`（g3v2）、旧 GPT purified（`exp_ref3_audit/`）、timbre 过强档（`vc_final_timbre/` / `metrics_timbre.tsv`）、`acceptance_g12.md` / `acceptance_fixed.md`
- 源（本轮）：`src_nospeech/gagged_3_bs_vocals_nospeech_v3.wav`（**g3v3**）
- 参考锚：`balanced_refs/dania_bal_hf027_hf010a.wav`、`amiya_bal_CN013_CN009a.wav`（已排除高 F0 波动/笑声等过激参考）

> **定位（协作方 2026-10-20 补充，必须遵守）**  
> 1. 用户已决定**停用去人声版 g3**；之后统一用 **g1 + g2 + 原版 g3**。  
> 2. 本轮 balanced（基于 g3v3）验收**照常完成**，但定位为 **参照 / 对照**，**不是**主线推荐接线对象。  
> 3. 新主线将输出到 `exp_gagged_v2/vc_final_g123_orig/`；**那一轮出来后再验，才是主线**。  
> → 下文「首选」仅指本参照轮内的听感/客观优选，**不要**写成「下一版默认接入」。

---

## 0. 结论摘要

| 问题 | 判定 |
| --- | --- |
| 平衡点是否找对？ | **达妮娅：基本找对（参照轮内）。** `bal_s-4_cfg0.6` 相对 g3v2 更靠角色、相对 timbre 高 cfg 不过激。**阿米娅：半对。** 闭口/无开元音好于过强档，但 campplus 仍偏源；且 bal 参考仍含亮锚 CN_013。 |
| 本轮推荐试听（参照） | 达妮娅：`vc_dania_g3v3_bal_s-4_cfg0.6_st40_envdyn_final.wav`；阿米娅：`vc_amiya_g3v3_bal_s-3_cfg0.5_st40_envdyn_final.wav`（听感/频谱优先于 campplus 排名）。 |
| 是否接线 / 是否作下一版默认？ | **否。balanced 仅参照。** 不改生产、不接 `vc_gagged`。主线等 `vc_final_g123_orig/`（g1+g2+原版 g3）出样后再验。 |
| 用户反馈① g3v2 太贴源 | 达妮娅 bal s-4：campplus/resemblyzer 均为 char>src（diff +0.084 / +0.127）→ 相对 g3v2「贴源」抱怨有客观改善。阿米娅 campplus 仍 char&lt;src；resemblyzer s-3 为 char>src。 |
| 用户反馈② 加音色强度后崩/喘/喊 | cfg≥0.7 再现开元音或伪字（达妮娅 cfg0.7「呵」gate 失败；cfg0.8「嘖」；阿米娅 cfg0.6「呵」）。**本轮焦点 cfg0.5–0.6 无开元音、无 gate 失败** → 相对过强档，过激风险明显下降。 |
| campplus vs resemblyzer（阿米娅） | **闷哼/封嘴场景采信 resemblyzer 方向 + 频谱/f0 交叉验证；campplus 单独排序对阿米娅易误导。** 详见 §6。 |

**五项总评（参照轮内，envdyn 焦点）**

| 文件 | 1辨识 | 2闭口 | 3无元音字 | 4无拼接 | 5无过激 | 总评 |
| --- | --- | --- | --- | --- | --- | --- |
| 达妮娅 s-4 cfg0.6 ★ | ✅ char>src 双口径 | ✅ lt1200 92.7% | ✅ 哼哼 | ✅ 1.85s Δ≈+4dB 温和 | ✅ 无开元音 | **参照轮首选** |
| 达妮娅 s-3 cfg0.6 | ⚠️ campplus 几乎贴平 | ✅ 94.5% | ✅ 哼哼 | ✅ | ✅ | 参照备选 |
| 阿米娅 s-3 cfg0.5 ★ | ⚠️ rz 靠角色 / cp 仍贴源 | ✅ 87.3% | ✅ 呜呜 | ✅ Δ≈+1.9dB | ✅ | **参照轮首选（听感口径）** |
| 阿米娅 s-2 cfg0.5 | ⚠️ 两口径均弱/贴源 | ⚠️ 82.7% 略亮 | ✅ 呜呜 | ✅ | ✅ f0 偏高 318 | 参照备选（campplus 榜首） |

---

## 1. 样本清单

### 1.1 本轮重点（envdyn）

| 角色 | 标签 | 文件 |
| --- | --- | --- |
| 达妮娅 | ★参照首选 | `vc_final_balanced/vc_dania_g3v3_bal_s-4_cfg0.6_st40_envdyn_final.wav` |
| 达妮娅 | 参照对比 | `vc_final_balanced/vc_dania_g3v3_bal_s-3_cfg0.6_st40_envdyn_final.wav` |
| 阿米娅 | ★参照首选（听感/rz） | `vc_final_balanced/vc_amiya_g3v3_bal_s-3_cfg0.5_st40_envdyn_final.wav` |
| 阿米娅 | 参照对比（cp 榜首） | `vc_final_balanced/vc_amiya_g3v3_bal_s-2_cfg0.5_st40_envdyn_final.wav` |

### 1.2 对照

| 用途 | 路径 |
| --- | --- |
| g3v2 达妮娅首选 | `vc_final_g12/vc_dania_seed_g3v2_s-3_final.wav` |
| g3v2 阿米娅首选 | `vc_final_g12/vc_amiya_seed_g3v2_CN027_s-3_final.wav` |
| 旧 GPT 达妮娅 | `exp_ref3_audit/vc_dania_gagged3_purified.wav` |
| 旧 GPT 阿米娅 | `exp_ref3_audit/vc_amiya_gagged3_purified.wav` |
| 原素材 | `gg-test/wav/gagged_3.wav` |
| 本轮源 g3v3 | `src_nospeech/gagged_3_bs_vocals_nospeech_v3.wav` |
| 过强档（情绪崩） | `vc_final_timbre/*cfg1.0*`, `*cfg1.5*`, `*cfg2.0*`, `*auto_s0*` 等 |
| **未来主线（未出样）** | `vc_final_g123_orig/`（g1+g2+**原版** g3） |

`mainonly` 变体：env 相关门控大量失败（`band12_3k_over3dB` 高、env_corr 低），**不参与本轮首选**；仅作后处理对照。

---

## 2. 客观指标表

### 2.1 metrics_balanced.tsv（摘录，envdyn）

| 样本 | rank | cos_char_cp | cos_src_cp | diff_cp | cos_char_rz | cos_src_rz | diff_rz | lt1200% | >3k% | cP / cF | f0_med | ASR | gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |
| **Dania s-4 cfg0.6** | 1 | **0.438** | 0.354 | **+0.084** | **0.757** | 0.630 | **+0.127** | **92.7** | 0.5 | 572 / 1870 | **280** | 哼哼 | True |
| Dania s-3 cfg0.6 | 2 | 0.353 | 0.342 | +0.012 | 0.667 | 0.627 | +0.040 | 94.5 | 0.9 | 557 / 2196 | 300 | 哼哼 | True |
| Dania s-3 cfg0.8 | 3 | 0.451 | 0.450 | +0.001 | 0.676 | 0.676 | ~0 | 95.2 | 0.3 | 490 / 1984 | 299 | 嘖… | True |
| Dania s-3 cfg0.7 | 12 | 0.330 | 0.329 | ~0 | 0.716 | 0.614 | +0.102 | 93.9 | 0.9 | 527 / 2157 | 301 | **呵呵** | **False** |
| Amiya s-2 cfg0.5 | 1 | 0.424 | **0.471** | **-0.047** | 0.701 | 0.695 | +0.006 | 82.7 | 2.8 | 809 / 2912 | **318** | 呜呜 | True |
| **Amiya s-3 cfg0.5** | 2 | **0.456** | 0.540 | **-0.084** | **0.725** | 0.641 | **+0.084** | **87.3** | 3.3 | 679 / 2702 | **294** | 呜呜 | True |
| Amiya s-2 cfg0.6 | 11 | 0.442 | 0.485 | -0.042 | 0.706 | 0.621 | +0.086 | 85.6 | 4.2 | 746 / 2695 | 316 | **呵呵** | **False** |

排序规则（官方 meta）：gate = `env_corr_lin≥0.85` 且 ASR 无开元音字；通过后按 **campplus (cos_char−cos_src)** 排序。

### 2.2 自算频带 / 能量（与官方 lt1200 一致）

| 样本 | Peak/RMS | lt400% | 400–800% | lt1200% | 1.2–3k% | >3k% | cP | 1.85s ΔdB |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Dania bal s-4 | -1.0 / -17.9 | 66.0 | 15.7 | 92.7 | 6.7 | 0.5 | 572 | +4.1 |
| Dania bal s-3 | -1.0 / -15.9 | 70.8 | 6.1 | 94.6 | 4.5 | 0.9 | 561 | +3.4 |
| Amiya bal s-2 | -1.0 / -17.5 | 56.1 | 15.6 | 82.8 | 14.4 | 2.8 | 805 | +3.3 |
| Amiya bal s-3 | -1.0 / -16.8 | 72.2 | 9.8 | 87.3 | 9.3 | 3.4 | 680 | +1.9 |
| Dania g3v2 s-3 | -1.0 / -16.1 | 69.5 | 6.9 | 86.9 | **12.2** | 0.9 | 608 | -11.0（能量谷） |
| Amiya g3v2 s-3 | -1.0 / -14.4 | 77.8 | 4.9 | 86.1 | 12.3 | 1.6 | 613 | -8.7（能量谷） |
| 旧 GPT Dania | -1.4 / -17.3 | 88.1 | 7.5 | 99.7 | **0.3** | 0.0 | 349 | n/a（尾静） |
| 旧 GPT Amiya | -1.4 / -17.0 | 52.2 | 35.5 | 98.2 | 1.8 | 0.0 | 501 | n/a |
| src g3v3 | -7.3 / -23.8 | 42.4 | 30.1 | 75.9 | 23.6 | 0.4 | 834 | +1.3 |
| bal_ref Amiya | -1.0 / -16.3 | 42.6 | 6.1 | **54.6** | 14.5 | **30.8** | **2433** | — |

说明：达妮娅 bal 中频 1.2–3k（4.5–6.7%）低于 g3v2 的 ~12%，但仍远高于旧 GPT 的 ~0.3%（旧版「虚弱」）；结合更高的 char 相似度与更低 f0，判为「略闷但角色更靠、不虚弱回退」。

---

## 3. 五项逐条（重点四文件）

### 3.1 达妮娅 `bal_s-4_cfg0.6` ★

1. **角色辨识度**：✅ campplus/resemblyzer 均 char>src，且为达妮娅全表最强 diff。f0 280 落入角色区，明显低于源 ~360。  
2. **闭口**：✅ lt1200 92.7%，>3k 0.5%，cP 572。  
3. **无清晰元音字词**：✅ ASR「哼哼哼哼」，无开元音字符。  
4. **无节拍/拼接**：✅ 1.85s 局部 Δ≈+4 dB、env jump 极小；非 g12 s-4 那种 24 dB 硬切。  
5. **无过激**：✅ 相对 timbre cfg≥1 / concat 的「呵/啊」失败，本档安静；envdyn 对 1.2–3k 过冲帧仅 0.2%。

### 3.2 达妮娅 `bal_s-3_cfg0.6`

1. ⚠️ campplus diff 仅 +0.012（几乎贴源）；rz +0.040 弱于 s-4。  
2. ✅ 闭口更好（94.5%）。  
3. ✅ 「哼哼」。  
4. ✅ 接缝温和。  
5. ✅ 无过激。  
→ **客观可过，但角色拉力不如 s-4** → 参照备选。

### 3.3 阿米娅 `bal_s-3_cfg0.5` ★（听感口径）

1. ⚠️ **矛盾区**：rz diff **+0.084**（靠角色）；cp diff **-0.084**（仍更像源）。采信见 §6 → 辨识度记条件通过。  
2. ✅ lt1200 87.3%（优于 s-2 的 82.7% 与 fixed/CN_013 的 72–75%）；>3k 3.3% 可接受。  
3. ✅ 「呜呜」，无开元音。  
4. ✅ 1.85s Δ≈+1.9 dB。  
5. ✅ 无呵/啊；相对 timbre cfg1.0/1.5（呵呵 / 开元音 FAIL）明显更稳。f0 294 低于 s-2 的 318。

### 3.4 阿米娅 `bal_s-2_cfg0.5`（campplus 榜首）

1. ⚠️ cp 仍负 diff；rz 几乎贴平（+0.006）。  
2. ⚠️ 82.7% / cF 官方 2912 —— 相对本轮 s-3 更亮。  
3. ✅ 「呜呜」。  
4. ✅ 接缝 OK。  
5. ✅ 无开元音，但 f0 318 更近源兴奋区。  
→ 作对照；**不升为听感首选**。

---

## 4. 「角色偏离 vs 过激」平衡专项

用户上轮两点：

1. **g3v2 太贴近源素材，偏离角色原声**  
2. **加大音色强度 → 情绪过激、声音崩、喘气/叫喊回来**

| 轴 | g3v2 首选 | balanced 焦点 cfg0.5–0.6 | timbre / 高 cfg |
| --- | --- | --- | --- |
| 角色靠拢（达妮娅） | 用户听感「贴源」；本轮无同表 emb，但 bal s-4 双口径 char>src 最强 | **改善**（s-4） | 部分 cfg 高 cos_char，但常伴随开元音 |
| 角色靠拢（阿米娅） | 客观闭口已可，用户仍嫌贴源 | rz 显示 s-3 靠角色；**cp 仍贴源** | cfg1.5 cos_char_cp 可到 0.54，但 ASR「呵呵」FAIL |
| 过激/崩/开元音 | g12 首选无开元音 | **焦点档无开元音** | cfg0.7+ / auto：呵、啊、啧 等 |
| 闭口 vs 尖 | g3v2 阿米娅已不「过尖」 | 达妮娅更闷；阿米娅 s-3 仍可用，参考仍偏亮 | 部分 auto 回落到 lt1200~73% |

**结论（平衡点）**  
- **达妮娅**：`s-4 @ cfg0.6 + 安静 bal 参考` ≈ 参照轮内正确平衡点（更靠角色、不过激）。  
- **阿米娅**：`s-3 @ cfg0.5` 是过激轴上的正确落点，但角色轴受 **bal 参考仍含 CN_013（gt3k≈31%）** 与 campplus 口径限制，**不能宣称「已完全离开贴源」**。  
- **禁止**在本参照结论里把上述文件写成下一版默认接入；主线改走 `vc_final_g123_orig/`。

---

## 5. 首选 / 备选取舍（仅参照轮）

| 角色 | 参照选用 | 理由 |
| --- | --- | --- |
| 达妮娅 | **s-4 cfg0.6 envdyn** | 双 embedding 角色差最大；闭口优秀；无过激；中频高于旧 GPT |
| 达妮娅 | 备选 s-3 cfg0.6 | 更闷、角色差弱 |
| 阿米娅 | **s-3 cfg0.5 envdyn** | 更暗、f0 更稳、rz 角色差最好；避免 cp 榜把 s-2 推成听感主 |
| 阿米娅 | 备选 s-2 cfg0.5 | 仅 campplus 排序第一，频谱/音高略差 |

试听顺序（参照，非正式接入）：`dania s-4` → `amiya s-3` → g3v2 双首选 → 旧 GPT → `amiya s-2` / `dania s-3` →（反例）timbre cfg1.5 / bal cfg0.7。

---

## 6. 阿米娅 campplus vs resemblyzer：采信结论

**现象（焦点档）**

| 口径 | s-2 cfg0.5 | s-3 cfg0.5 |
| --- | --- | --- |
| campplus cos_char / cos_src / diff | 0.424 / 0.471 / **−0.047** | 0.456 / 0.540 / **−0.084** |
| resemblyzer cos_char / cos_src / diff | 0.701 / 0.695 / **+0.006** | 0.725 / 0.641 / **+0.084** |

两者对 s-3 **方向相反**：cp 说更像源，rz 说更像角色。官方排名采 campplus → 把 s-2 排到阿米娅第 1。

**采信：以 resemblyzer 的方向 + lt1200 / 质心 / f0 交叉验证为主；campplus 作辅，不单独定阿米娅听感主。**

理由（均基于已读数字，不虚构）：

1. **任务域不匹配**：campplus（FunASR）面向清晰说话人识别；封嘴闷哼的包络/底噪更接近 **gagged 源**，易把「还像源」判成高 cos_src（本轮阿米娅 envdyn 普遍 cos_src_cp 0.47–0.54）。  
2. **held-out 角色均值偏「干声对白」**：meta 中阿米娅 held-out 含 CN_008 / CN_031 / CN_032 等；与闷哼目标域差距大，抬高「像角色清晰说话」的门槛，压低 cos_char_cp。  
3. **与听感代理一致的是 rz + 频谱**：s-3 相对 s-2 → lt1200↑（87 vs 83）、cP↓、f0↓（294 vs 318）、rz diff↑；这些与「更靠角色且更闭口」同向，与 campplus「s-2 更好」反向。  
4. **campplus 可能骗人的方式**：对低通/闷哼不敏感于音色身份、却敏感于与源共享的低频能量结构 → **diff 为负并不等于「没转音色」**，只说明「相对 held-out 干声，仍更像 gagged 源」。  
5. **达妮娅无此矛盾**：双口径同向且 s-4 双赢 → 该角色可继续参考 campplus 排序。

**实操**：阿米娅表内若 cp 与 rz 打架 → **听 s-3（rz+频谱）**；写报告时标明「cp 榜首 ≠ 听感首选」。

---

## 7. 是否接线 / 与主线关系

| 项 | 决定 |
| --- | --- |
| 生产 / `vc_gagged` 接线 | **不接。** 未改任何生产代码。 |
| balanced 是否「下一版默认」 | **否。** 本轮 = **参照/对照**（g3v3 去词源上的 quiet-ref + 中低 cfg 实验）。 |
| 用户策略变更 | **停用去人声版 g3**；之后 **g1 + g2 + 原版 g3**。 |
| 真正主线 | 等 `exp_gagged_v2/vc_final_g123_orig/` 出样 → **再开一轮验收** 才谈推荐与接线。 |
| 本报告用途 | 沉淀「安静参考 + cfg0.5–0.6」有效、以及阿米娅 embedding 口径教训，供 g123_orig 复用。 |

---

## 8. 给「你好」的摘要（可直接转发）

1. `acceptance_balanced.md` 已写（真实目录 + 镜像各一份）。  
2. **定位：balanced = 参照/对照，不是主线；不接线、不作下一版默认。** 主线等 `vc_final_g123_orig/`（g1+g2+原版 g3）。  
3. **平衡点**：达妮娅 `bal_s-4_cfg0.6` 参照轮内找对（更靠角色、不过激）；阿米娅 `bal_s-3_cfg0.5` 过激轴 OK，角色轴 rz 改善但 cp 仍贴源。  
4. 相对用户两点反馈：贴源 → 达妮娅双口径改善；过激 → 焦点 cfg 无开元音，cfg≥0.7 再现呵/啧。  
5. **阿米娅采信 resemblyzer(+频谱/f0)**，campplus 单独排序易把更亮的 s-2 推成榜首。  
6. 参照试听：`dania s-4_cfg0.6_envdyn`、`amiya s-3_cfg0.5_envdyn`；勿当接入候选。  

---

## 9. 缺失 / 限制

- 未做真人耳机听感（用户已睡）；结论为客观 + ASR + 与历史轮对比。  
- g12 表无同套 campplus/resemblyzer 列，g3v2「贴源」主要依用户听感反馈 + 本轮 emb 相对改善推断。  
- `seam_check` 无 balanced 专用条目；1.85s 用自算局部能量，未跑完整 seam 脚本。  
- 阿米娅 bal 参考 `CN013_CN009a` 自身 gt3k≈30.8%，亮度风险仍在（与 fixed 轮 CN_013 教训同源）。  
- 镜像目录 `qq-agent-workspace/exp_gagged_v2/` 仅存报告副本（按历史 README_PATH）；wav/指标仍以 `bot/data/test_output/exp_gagged_v2/` 为准。  
- **`vc_final_g123_orig/` 尚未验收**（主线未到）。  
- 未改生产代码、未接线。
