# Build per-character RVC training sets (copies only; never touches originals)
import os, glob, hashlib, json, sys
import numpy as np, soundfile as sf, librosa
sys.path.insert(0, os.path.dirname(__file__))
from audio_metrics import metrics
W = "/Users/daijinglin/Desktop/qq-agent-workspace"
R = f"{W}/tools/rvc"
src = {
  "amiya": sorted(glob.glob(f"{R}/data_raw/amiya/arkecho/*.wav")),
  "dania": sorted(glob.glob(f"{R}/data_raw/dania/huanx/*.wav")),
}
# local real game refs not in downloaded sets (TTS outputs, gagged clips, clean_* / s01_* nuclei excluded)
src["amiya"] += [f"{W}/gpt-sovits/models/amiya/reference_audios/中文/emotions/【默认】博士整理一下航程信息吧。.wav"]
out_all = {}
only = sys.argv[1:] or list(src)
for ch, files in src.items():
    if ch not in only: continue
    od = f"{R}/datasets/{ch}"; os.makedirs(od, exist_ok=True)
    seen = set(); rows = []; tot = 0.0
    for p in files:
        h = hashlib.md5(open(p,'rb').read()).hexdigest()
        if h in seen: rows.append({"file": os.path.basename(p), "skip": "duplicate"}); continue
        seen.add(h)
        try:
            m = metrics(p, fmin=100 if ch=="dania" else 120, fmax=900)
        except Exception as e:
            rows.append({"file": os.path.basename(p), "skip": f"read_error {e}"}); continue
        y, sr = librosa.load(p, sr=None, mono=True)
        y, _ = librosa.effects.trim(y, top_db=45)
        if len(y) < 0.4*sr: rows.append({"file": os.path.basename(p), "skip": "too_short", **m}); continue
        pk = np.max(np.abs(y)); y = y/pk*0.89 if pk>0 else y
        name = f"{ch}_{len(rows):03d}.wav"
        sf.write(f"{od}/{name}", y, sr, subtype="PCM_16")
        tot += len(y)/sr
        rows.append({"file": os.path.basename(p), "out": name, **m})
    kept = [r for r in rows if "out" in r]
    srs = {}; [srs.__setitem__(r["sr"], srs.get(r["sr"],0)+1) for r in kept]
    summ = {"kept": len(kept), "skipped": len(rows)-len(kept), "minutes": round(tot/60,2), "sample_rates": srs,
            "median_snr_proxy_db": float(np.median([r["snr_proxy_db"] for r in kept])) if kept else None,
            "median_floor_db": float(np.median([r["floor_db"] for r in kept])) if kept else None,
            "median_f0": float(np.nanmedian([r["f0med"] or np.nan for r in kept])) if kept else None,
            "median_lt1200": float(np.median([r["lt1200"] for r in kept])) if kept else None}
    out_all[ch] = {"summary": summ, "files": rows}
    print(ch, json.dumps(summ, ensure_ascii=False, default=float), flush=True)
for ch in out_all: json.dump(out_all[ch], open(f"{R}/datasets/inventory_{ch}.json","w"), ensure_ascii=False, indent=1, default=float)
