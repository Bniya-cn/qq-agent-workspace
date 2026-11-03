# buzz / 电流音 diagnostics + vocoder-free closed-mouth path (v4)
import numpy as np, soundfile as sf, librosa, pyworld as pw, scipy.signal as ss
def load(p, sr=None):
    y,s=sf.read(p,always_2d=True); y=y.mean(1).astype(np.float64)
    if sr and s!=sr: y=librosa.resample(y,orig_sr=s,target_sr=sr); s=sr
    return y,s
def _db(x): return 10*np.log10(np.maximum(x,1e-20))
def buzz(path):
    yr,sr0=load(path); clip=float(np.mean(np.abs(yr)>0.99)*100)
    y,sr=load(path,16000)
    f0,t=pw.harvest(y,sr,f0_floor=70,f0_ceil=1000,frame_period=5); v=f0>0
    f0m=float(np.median(f0[v])) if v.sum()>10 else 250.0
    lf=12*np.log2(f0[v]) if v.sum()>10 else np.array([0.0]); d=np.abs(np.diff(lf))
    out=dict(f0_med=round(f0m,1), jitter_st_med=round(float(np.median(d)),3) if len(d) else None,
             jitter_st_p95=round(float(np.percentile(d,95)),3) if len(d) else None, octave_jumps=int((d>9).sum()), clip_pct=round(clip,4))
    # HF (2-6 kHz) envelope modulation spectrum on active region
    sos=ss.butter(6,[2000,6000],btype="band",fs=sr,output="sos"); hb=ss.sosfiltfilt(sos,y)
    env=np.abs(ss.hilbert(hb)); q=8; env=ss.decimate(env,q,ftype="fir"); fe=sr/q  # 2 kHz
    e_full=ss.decimate(np.abs(ss.hilbert(y)),q,ftype="fir"); act=e_full>np.percentile(e_full,95)*10**(-30/20)
    x=env[act]; x=x/(x.mean()+1e-12)-1
    f,P=ss.welch(x,fs=fe,nperseg=min(1024,len(x)))
    band=lambda a,b: P[(f>=a)&(f<b)].sum()
    tot=band(10,900)+1e-20
    out["hf_mod_F0_share"]=round(float(band(0.85*f0m,1.15*f0m)/tot),3)          # pulse-train buzz: HF pulsed at F0
    out["hf_mod_50_200_share"]=round(float(band(50,200)/tot),3)                 # zipper / frame-rate gain modulation
    out["hf_mod_depth"]=round(float(np.sqrt(np.sum(P[(f>=10)&(f<900)])*(f[1]-f[0]))),3)
    # strongest non-harmonic modulation line 150-900 Hz (frame-rate zipper e.g. 400 Hz WORLD 2.5 ms, 312/250 Hz STFT hops)
    pd=_db(P); import scipy.ndimage as nd; base=nd.median_filter(pd,31,mode="nearest"); pr=pd-base
    m=(f>=150)&(f<=900)&np.array([all(abs(fi-k*f0m)>0.1*k*f0m for k in range(1,4)) for fi in f])
    i=np.argmax(np.where(m,pr,-99)); out["mod_line_hz"]=round(float(f[i]),1); out["mod_line_prom_db"]=round(float(pr[i]),1)
    # HF comb: cepstral peak prominence of 2-6 kHz log spectrum at quefrency 1/F0
    S=np.abs(librosa.stft(y.astype(np.float32),n_fft=1024,hop_length=80))+1e-9; fs=librosa.fft_frequencies(sr=sr,n_fft=1024); b=(fs>=2000)&(fs<=6000)
    fr_f0=np.interp(np.arange(S.shape[1])*80/sr,t,f0); cpp=[]
    for j in np.where((fr_f0>0)&(S.sum(0)>np.percentile(S.sum(0),40)))[0][::2]:
        ls=20*np.log10(S[b,j]); ls=ls-np.polyval(np.polyfit(fs[b],ls,1),fs[b]); c=np.abs(np.fft.rfft(ls*np.hanning(len(ls))))
        qf=np.fft.rfftfreq(len(ls),d=fs[1]-fs[0])  # cycles per Hz
        tq=1.0/fr_f0[j]; w=(qf>=0.8*tq)&(qf<=1.25*tq)
        if w.any():
            cd=20*np.log10(c+1e-9); cpp.append(cd[w].max()-np.median(cd[(qf>0.3*tq)&(qf<3*tq)]))
    out["hf_comb_cpp_db"]=round(float(np.median(cpp)),2) if cpp else None
    return out
# ---------------- vocoder-free closed-mouth path ----------------
def _fir(sr, pts, ntaps=2047):
    fq=np.array([p[0] for p in pts])/(sr/2); g=10**(np.array([p[1] for p in pts])/20)
    fq[0]=0; fq[-1]=1; return ss.firwin2(ntaps,fq,g)
def eq_curve(sr, f1_cut=-8, body=2, hf_slope=-2, hf_cap=-10, pres=0.0):
    fr=np.linspace(0,sr/2,2049); g=lambda c,bw,a: a*np.exp(-0.5*((fr-c)/bw)**2)
    G=g(300,120,body)+g(800,260,f1_cut)+g(1100,180,f1_cut*0.5)+g(1600,300,pres)
    hf=np.clip(hf_slope*np.log2(np.maximum(fr,1500)/1500),hf_cap,0); G=G+hf
    return list(zip(fr,G))
def _smooth(x, n): 
    n=max(1,int(n)); w=np.hanning(n+2)[1:-1]; w/=w.sum(); return np.convolve(x,w,mode="same")
def v4(path, out, f1_cut=-8, body=2, hf_slope=-2, hf_cap=-10, pres=0.0, trans_k=0.5, emo=0.2, adapt=False, f1_cut_strong=-14):
    y,sr=load(path); N=len(y); ntaps=2047 if sr<=24000 else 4095
    def filt(h): return ss.fftconvolve(y,h,mode="same")
    yeq=filt(_fir(sr,eq_curve(sr,f1_cut,body,hf_slope,hf_cap,pres),ntaps))
    if adapt:  # smooth crossfade toward a stronger F1 cut where the frame is more 'open' (500-1000 share high)
        ys=filt(_fir(sr,eq_curve(sr,f1_cut_strong,body,hf_slope,hf_cap,pres),ntaps))
        sos=ss.butter(4,[500,1000],btype="band",fs=sr,output="sos"); sosl=ss.butter(4,1000,fs=sr,output="sos")
        h=int(sr*0.005); em=_smooth(ss.sosfiltfilt(sos,y)**2,sr*0.05); el=_smooth(ss.sosfiltfilt(sosl,y)**2,sr*0.05)
        share=em/(el+1e-12); w=np.clip((share-0.10)/0.25,0,1); w=_smooth(w,sr*0.05)
        yeq=(1-w)*yeq+w*ys
    # transient shaper on HF band via complementary linear-phase split (LP + (delta-LP) = identity)
    hl=ss.firwin(ntaps,1500,fs=sr); lo=ss.fftconvolve(yeq,hl,mode="same"); hi=yeq-lo
    fast=np.sqrt(_smooth(hi**2,sr*0.004)+1e-12); slow=np.sqrt(_smooth(hi**2,sr*0.040)+1e-12)
    g=np.clip((slow/fast)**trans_k,0.25,1.0); g=_smooth(g,sr*0.030)  # >=30 ms gain smoothing, no frame switching
    # broadband attack softening (slow follower, gain smoothed 30 ms)
    yy=lo+hi*g
    e=np.sqrt(_smooth(yy**2,sr*0.005)+1e-12); a=np.exp(-1/(sr*0.020)); fo=ss.lfilter([1-a],[1,-a],e)
    ga=np.clip(np.minimum(1.0,np.maximum(fo,e*0.35)/e),0.35,1.0); ga=_smooth(ga,sr*0.030); yy=yy*ga
    # emotion: slow RMS envelope expansion only (100 ms env, 50 ms gain smoothing, +-4 dB)
    if emo>0:
        r=np.sqrt(_smooth(yy**2,sr*0.10)+1e-12); act=r>np.percentile(r,95)*10**(-30/20); med=np.median(r[act])
        ge=np.clip((r/med)**emo,10**(-4/20),10**(4/20)); ge=_smooth(ge,sr*0.05); yy=yy*ge
    yy=yy/(np.max(np.abs(yy))+1e-9)*0.89; sf.write(out,yy.astype(np.float32),sr)
