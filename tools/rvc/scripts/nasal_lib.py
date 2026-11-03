# closed-mouth (nasal murmur) cue measurement + v6 residual-excited nasal re-filtering (no vocoder)
import numpy as np, soundfile as sf, librosa, pyworld as pw, scipy.signal as ss
SR=16000; WIN=400; HOP=80; P=16
def load16(p):
    y,s=sf.read(p,always_2d=True); y=y.mean(1).astype(np.float64)
    return librosa.resample(y,orig_sr=s,target_sr=SR) if s!=SR else y
def _db(x): return 10*np.log10(np.maximum(x,1e-20))
NF=1024; FR=np.fft.rfftfreq(NF,1/SR)
def frames(y):
    n=1+max(0,(len(y)-WIN)//HOP); idx=np.arange(WIN)[None,:]+HOP*np.arange(n)[:,None]; return y[idx]*np.hanning(WIN)[None,:]
def cep_env(fr, lifter=40):
    S=np.abs(np.fft.rfft(fr,NF,axis=-1))**2+1e-12; c=np.fft.irfft(np.log(S),axis=-1); c[...,lifter:NF-lifter]=0
    return np.real(np.fft.rfft(c,axis=-1))*10/np.log(10), S   # dB envelope, power
def voiced_mask(y):
    f0,t=pw.harvest(y,SR,f0_floor=70,f0_ceil=1000,frame_period=HOP/SR*1000); n=len(frames(y)); f0=np.r_[f0,np.zeros(max(0,n-len(f0)))][:n]; return f0
def lpc_poles(fr, order=12):
    try: a=librosa.lpc(fr.astype(np.float64),order=order)
    except Exception: return []
    r=np.roots(a); r=r[np.imag(r)>0]; f=np.angle(r)*SR/(2*np.pi); b=-SR/np.pi*np.log(np.abs(r)+1e-12)
    o=np.argsort(f); return [(float(f[i]),float(b[i])) for i in o if 90<f[i]<SR/2-100]
def cue_frames(E, Pw, poles):
    """per-frame closed/open cues from cepstral envelope E (dB) and LPC poles"""
    def band(a,b): return E[(FR>=a)&(FR<b)]
    n1=[p for p in poles if 150<=p[0]<=500]; N1f,N1b=(n1[0] if n1 else (np.nan,np.nan))
    # 'F2' = strongest envelope peak 600-1800 Hz; prominence over the deepest dip between N1 and it
    m=(FR>=600)&(FR<=1800); i=np.argmax(np.where(m,E,-1e9)); F2f=FR[i]
    lo=(FR>=400)&(FR<F2f); dip=E[lo].min() if lo.any() else E[i]
    F2prom=float(E[i]-dip)
    # antiformant: deepest dip 700-1400 relative to mean of 200-400 and 1500-2500 envelope
    # antiformant: deepest dip 1100-2200 Hz below the N2 hump (700-1000) -- genuine nasal zero sits at ~1.4-1.7 kHz
    am=(FR>=1100)&(FR<=2200); j=np.argmin(np.where(am,E,1e9)); ref=band(700,1000).mean(); AFdepth=float(ref-E[j])
    hi_vs_n2=float(band(1300,2000).mean()-band(700,1000).mean()); n2_vs_dip=float(band(700,1000).mean()-band(480,620).mean())
    tot=Pw[(FR>=50)&(FR<4000)].sum(); lt500=float(Pw[(FR>=50)&(FR<500)].sum()/tot); e5_10=float(Pw[(FR>=500)&(FR<1000)].sum()/Pw[(FR>=50)&(FR<1000)].sum())
    mm=(FR>=100)&(FR<=4000); tilt=float(np.polyfit(np.log2(FR[mm]),E[mm],1)[0])
    return dict(N1=N1f,N1bw=N1b,F2=float(F2f),F2prom=F2prom,AF=float(FR[j]),AFdepth=AFdepth,hi_vs_n2=hi_vs_n2,n2_vs_dip=n2_vs_dip,lt500=lt500,e5_10=e5_10,tilt=tilt)
def profile_vec(E): # normalized envelope 100-4000 Hz on a log grid (for distance)
    g=np.geomspace(100,4000,48); v=np.interp(g,FR,E); return v-v.mean()
def file_cues(path, mask_fn=None):
    y=load16(path); F=frames(y); f0=voiced_mask(y); E,Pw=cep_env(F); e=_db(Pw.sum(1)); act=e>np.percentile(e,95)-30
    sel=(f0>0)&act if mask_fn is None else mask_fn(y,f0,E,Pw,act)
    rows=[]; vecs=[]
    for k in np.where(sel)[0]:
        c=cue_frames(E[k],Pw[k],lpc_poles(F[k])); rows.append(c); vecs.append(profile_vec(E[k]))
    return summarize(rows,vecs)
def summarize(rows, vecs):
    if not rows: return None
    g=lambda k: np.array([r[k] for r in rows],float)
    F2=g("F2"); out=dict(n_frames=len(rows), N1_med=round(float(np.nanmedian(g("N1"))),1), N1bw_med=round(float(np.nanmedian(g("N1bw"))),1),
        F2_med=round(float(np.median(F2)),1), F2_std_hz=round(float(np.std(F2)),1), F2_prom_db=round(float(np.median(g("F2prom"))),2),
        AF_med=round(float(np.median(g("AF"))),1), AF_depth_db=round(float(np.median(g("AFdepth"))),2), lt500=round(float(np.median(g("lt500"))*100),1),
        e5_10=round(float(np.median(g("e5_10"))*100),1),hi_vs_n2=round(float(np.median(g("hi_vs_n2"))),2),n2_vs_dip=round(float(np.median(g("n2_vs_dip"))),2), tilt=round(float(np.median(g("tilt"))),2))
    out["profile"]=np.mean(vecs,0).round(2).tolist(); return out
def profile_dist(a,b): return round(float(np.sqrt(np.mean((np.array(a)-np.array(b))**2))),2)
# ---------------- v6: residual-excited closed-tract re-filtering ----------------
def lpc2lsf(a):
    p=len(a)-1; A=np.r_[a,0]; P_=A+A[::-1]; Q=A-A[::-1]
    rp=np.angle(np.roots(P_)); rq=np.angle(np.roots(Q)); w=np.sort(np.r_[rp[(rp>1e-6)&(rp<np.pi-1e-6)],rq[(rq>1e-6)&(rq<np.pi-1e-6)]]); return w[:p]
def lsf2lpc(w):
    p=len(w); wp=w[0::2]; wq=w[1::2]
    P_=np.array([1.0]); Q=np.array([1.0])
    for x in wp: P_=np.convolve(P_,[1,-2*np.cos(x),1])
    for x in wq: Q=np.convolve(Q,[1,-2*np.cos(x),1])
    P_=np.convolve(P_,[1,1]); Q=np.convolve(Q,[1,-1]); a=0.5*(P_+Q); return a[:p+1]
def fit_nasal_lpc(power_spec_mean, order=P):
    r=np.fft.irfft(power_spec_mean)[:order+1]
    from scipy.linalg import solve_toeplitz
    c=solve_toeplitz(r[:order],-r[1:order+1]); return np.r_[1.0,c]
def _smooth(x,n):
    n=max(1,int(n)); w=np.hanning(n+2)[1:-1]; w/=w.sum(); return np.convolve(x,w,mode="same")
def v6(path, out, nasal_a, af_hz=1000.0, af_bw=150.0, w_nasal=0.75, lsf_smooth_ms=25, f2_freeze_ms=120, lp_hz=2000.0, emo=0.2, mix_path=None, mix=0.0, air=0.0):
    y=load16(path); n=len(y); nfr=1+(n-WIN)//HOP
    F=frames(y); A=[]
    for k in range(nfr):
        try: a=librosa.lpc(F[k]+1e-7*np.random.randn(WIN),order=P)
        except Exception: a=np.r_[1.0,np.zeros(P)]
        A.append(lpc2lsf(a) if np.all(np.isfinite(a)) else None)
    good=[x for x in A if x is not None and len(x)==P]; dflt=np.mean(good,0)
    L=np.array([x if (x is not None and len(x)==P) else dflt for x in A])
    # smooth frame LSFs (>=20 ms) and freeze slow vowel-colour movement (long smoothing) before blending to nasal tract
    k1=max(1,int(lsf_smooth_ms/5)); k2=max(1,int(f2_freeze_ms/5))
    Ls=np.stack([_smooth(L[:,i],k1) for i in range(P)],1); Lf=np.stack([_smooth(L[:,i],k2) for i in range(P)],1)
    Ln=lpc2lsf(nasal_a)
    Lt=np.sort((1-w_nasal)*Lf+w_nasal*Ln[None,:],axis=1); Lt=np.clip(Lt,1e-3,np.pi-1e-3)
    for i in range(1,P): Lt[:,i]=np.maximum(Lt[:,i],Lt[:,i-1]+0.01)
    # sample-rate filtering with per-hop coefficients (LSF interpolated per hop -> stable, smooth)
    res=np.zeros(n); outy=np.zeros(n); zi_inv=np.zeros(P); zi_syn=np.zeros(P)
    for k in range(nfr):
        s=k*HOP+WIN//2-HOP//2; e=min(n,s+HOP)
        if k==0: s=0
        if k==nfr-1: e=n
        ai=lsf2lpc(Ls[k]); at=lsf2lpc(Lt[k])
        seg,zi_inv=ss.lfilter(ai,[1.0],y[s:e],zi=zi_inv); res[s:e]=seg
        o,zi_syn=ss.lfilter([1.0],at,seg,zi=zi_syn); outy[s:e]=o
    # labial-nasal antiformant (zero pair) + tissue/tape low-pass
    r=np.exp(-np.pi*af_bw/SR); th=2*np.pi*af_hz/SR; b=np.array([1,-2*r*np.cos(th),r*r]); b=b/np.sum(b)*1.0
    outy=ss.lfilter(b,[1.0],outy); sos=ss.butter(2,lp_hz,fs=SR,output="sos"); outy=ss.sosfiltfilt(sos,outy)
    # restore original slow loudness (30 ms) then v4 emotion expansion (100 ms RMS, +-4 dB)
    ri=np.sqrt(_smooth(y**2,SR*0.03)+1e-12); ro=np.sqrt(_smooth(outy**2,SR*0.03)+1e-12); g=_smooth(np.clip(ri/ro,0,30),SR*0.03); outy=outy*g
    if air>0:  # soft nasal-air noise at high-effort onsets
        rr=20*np.log10(np.sqrt(_smooth(y**2,SR*0.02)+1e-12)); d=rr-np.r_[np.full(int(SR*0.05),rr[0]),rr[:-int(SR*0.05)]]
        on=(d>8)&(rr>np.percentile(rr,90)-12); env=_smooth(on.astype(float),SR*0.06); nz=ss.sosfiltfilt(ss.butter(2,[300,1500],btype="band",fs=SR,output="sos"),np.random.randn(n))
        outy=outy+air*env*nz*ri
    if emo>0:
        rr=np.sqrt(_smooth(outy**2,SR*0.10)+1e-12); act=rr>np.percentile(rr,95)*10**(-30/20); med=np.median(rr[act])
        ge=np.clip((rr/med)**emo,10**(-4/20),10**(4/20)); outy=outy*_smooth(ge,SR*0.05)
    if mix_path and mix>0:
        m=load16(mix_path)[:n]; m=np.r_[m,np.zeros(n-len(m))]
        outy=outy/(np.sqrt(np.mean(outy**2))+1e-9); m=m/(np.sqrt(np.mean(m**2))+1e-9); outy=(1-mix)*outy+mix*m
    outy=outy/(np.max(np.abs(outy))+1e-9)*0.89; sf.write(out,outy.astype(np.float32),SR)
