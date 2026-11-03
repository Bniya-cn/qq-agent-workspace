import numpy as np, soundfile as sf, librosa
def metrics(path, fmin=80, fmax=1000):
    y, sr = sf.read(path, always_2d=True); y = y.mean(1).astype(np.float32)
    dur = len(y)/sr
    peak = 20*np.log10(np.max(np.abs(y))+1e-9)
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))**2
    f = librosa.fft_frequencies(sr=sr, n_fft=2048)
    tot = S.sum()+1e-12
    lt1200 = S[f<1200].sum()/tot*100; gt3k = S[f>3000].sum()/tot*100
    b1 = S[(f>=1200)&(f<3000)].sum()/tot*100
    cenP = float((f[:,None]*S).sum()/tot)
    fe = S.sum(0); act = fe > fe.max()*1e-3
    cen_frames = (f[:,None]*S).sum(0)/(fe+1e-12)
    cenF = float(np.mean(cen_frames[act])) if act.any() else 0.0
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    rdb = 20*np.log10(rms+1e-9)
    floor = float(np.percentile(rdb, 10)); loud = float(np.percentile(rdb, 95))
    try:
        y16 = librosa.resample(y, orig_sr=sr, target_sr=16000)
        f0, vf, _ = librosa.pyin(y16, fmin=fmin, fmax=fmax, sr=16000, frame_length=1024)
        f0med = float(np.nanmedian(f0)) if np.any(vf) else float('nan'); vr = float(np.mean(vf))*100
    except Exception:
        f0med, vr = float('nan'), float('nan')
    return dict(dur=round(dur,2), sr=sr, peak_db=round(peak,2), lt1200=round(lt1200,1), b1200_3k=round(b1,1), gt3k=round(gt3k,1),
                cenP=round(cenP), cenF=round(cenF), f0med=round(f0med,1) if f0med==f0med else None, voiced_pct=round(vr,1) if vr==vr else None,
                floor_db=round(floor,1), loud95_db=round(loud,1), snr_proxy_db=round(loud-floor,1))
if __name__ == "__main__":
    import sys, json
    for p in sys.argv[1:]: print(json.dumps({"file": p, **metrics(p)}, ensure_ascii=False))
