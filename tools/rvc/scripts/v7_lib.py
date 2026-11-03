# v7: native-SR rendering. LPC/LSF analysis on a 16 kHz COPY only; filters applied at native SR via STFT gains.
import numpy as np, soundfile as sf, librosa, scipy.signal as ss, pyworld as pw
import nasal_lib as N
A_SR=16000; HOPS=0.005
def loadn(p):
    y,sr=sf.read(p,always_2d=True); return y.mean(1).astype(np.float64), sr
def _sm(x,n):
    n=max(1,int(n)); w=np.hanning(n+2)[1:-1]; w/=w.sum(); return np.convolve(x,w,mode="same")
def frame_lsf(y16, order=16):
    F=N.frames(y16); L=[]
    for k in range(len(F)):
        try: a=librosa.lpc(F[k]+1e-7*np.random.randn(N.WIN),order=order); l=N.lpc2lsf(a)
        except Exception: l=None
        L.append(l if (l is not None and len(l)==order and np.all(np.isfinite(l))) else None)
    d=np.mean([x for x in L if x is not None],0); return np.array([x if x is not None else d for x in L])
def env_lsf_cheaptrick(y16, order=16):
    # for high-F0 gag sources: F0-adaptive envelope (analysis only) -> autocorrelation -> LPC -> LSF on the 5 ms grid
    f0,t=pw.harvest(y16,A_SR,f0_floor=70,f0_ceil=1200,frame_period=HOPS*1000); sp=pw.cheaptrick(y16,f0,t,A_SR)
    from scipy.linalg import solve_toeplitz
    L=[]
    for s in sp:
        r=np.fft.irfft(s)[:order+1]; r[0]*=1.0001
        c=solve_toeplitz(r[:order],-r[1:order+1]); L.append(N.lpc2lsf(np.r_[1.0,c]))
    L=np.array([l if len(l)==order else L[0] for l in L]); return L
def fixlsf(Lt):
    Lt=np.sort(np.clip(Lt,1e-3,np.pi-1e-3),axis=1)
    for i in range(1,Lt.shape[1]): Lt[:,i]=np.maximum(Lt[:,i],Lt[:,i-1]+0.01)
    return Lt
def lpc_mag_db(lsf_rows, freqs):
    w=2*np.pi*freqs/A_SR; z=np.exp(-1j*np.outer(np.arange(lsf_rows.shape[1]+1),w))
    out=[]
    for l in lsf_rows: a=N.lsf2lpc(l); out.append(-20*np.log10(np.abs(a@z)+1e-9))
    return np.array(out)
def apply_gain_stft(y, sr, gain_db_fn, nper=None):
    nper=nper or int(2**np.round(np.log2(0.04*sr))); hop=nper//4
    f,t,Z=ss.stft(y,sr,window="hann",nperseg=nper,noverlap=nper-hop,boundary="even",padded=True)
    G=gain_db_fn(f,t)          # (len(t), len(f)) dB
    Z=Z*(10**(G.T/20)); _,z=ss.istft(Z,sr,window="hann",nperseg=nper,noverlap=nper-hop,boundary=True)
    z=z[:len(y)]; return np.r_[z,np.zeros(len(y)-len(z))]
def refilter(y, sr, Lsrc, Ltgt, clip_db=24, t_smooth_frames=3):
    """y * |A_src/A_tgt| applied at native SR; above 7.6 kHz the gain is held at its 7.0-7.6 kHz mean."""
    tg=(np.arange(len(Lsrc))*N.HOP+N.WIN/2)/A_SR
    def fn(f,t):
        fl=np.minimum(f,7600.0); idx=np.clip(np.searchsorted(tg,t),0,len(tg)-1)
        Gs=lpc_mag_db(Lsrc[idx],fl); Gt=lpc_mag_db(Ltgt[idx],fl); G=Gt-Gs
        G=G-np.median(G[:,(f>200)&(f<1000)],axis=1,keepdims=True)*0  # keep absolute; loudness restored later
        G=np.clip(G,-clip_db,clip_db)
        hi=f>7600; G[:,hi]=G[:,((f>7000)&(f<=7600))].mean(1,keepdims=True)
        G=np.apply_along_axis(lambda c: _sm(c,t_smooth_frames),0,G); return G
    return apply_gain_stft(y,sr,fn)
def zero_biquad(sr,f0,bw):
    r=np.exp(-np.pi*bw/sr); th=2*np.pi*f0/sr; b=np.array([1,-2*r*np.cos(th),r*r]); return b/np.sum(b)
def dens(y,sr,a,b):
    f,P=ss.welch(y,sr,nperseg=4096); m=(f>=a)&(f<b); return 10*np.log10(P[m].mean()+1e-20)
def rms_env(y,sr,ms): return np.sqrt(_sm(y**2,sr*ms/1000)+1e-12)
def syllables(y,sr,min_gap_ms=120,prom_db=4):
    hop=int(sr*0.005); e=20*np.log10(rms_env(y,sr,30)[::hop]+1e-9); e=_sm(e,8)
    pk,_=ss.find_peaks(e,prominence=prom_db,distance=int(min_gap_ms/5),height=np.percentile(e,95)-25)
    ons=[]
    for p in pk:
        lo=max(0,p-int(0.25/0.005)); q=lo+int(np.argmin(e[lo:p+1])) if p>lo else p; ons.append((q*hop,p*hop))
    return ons
def hum_shape(x, sr, ons, swell_db=3.0, push_db=4.0, n1_db=3.0, dip_k=0.6, dip_floor_db=-10, attack_ms=60, n1_hz=(180,450)):
    n=len(x); curve=np.zeros(n)
    for q,p in ons:
        a=int(sr*attack_ms/1000); d=int(sr*0.18); s=max(0,p-a)
        seg=np.r_[0.5-0.5*np.cos(np.linspace(0,np.pi,a)), 0.5+0.5*np.cos(np.linspace(0,np.pi,d))]
        e=min(n,s+len(seg)); curve[s:e]=np.maximum(curve[s:e],seg[:e-s])
    nb=ss.sosfiltfilt(ss.butter(2,list(n1_hz),btype="band",fs=sr,output="sos"),x)
    gN=10**((n1_db+push_db*curve)/20)-1; x=x+gN*nb                  # nasal formant level + nasal push in each attack
    x=x*10**(swell_db*curve/20)                                     # per-syllable pressure swell
    es=rms_env(x,sr,30); et=rms_env(x,sr,300); g=np.clip((es/et)**dip_k,10**(dip_floor_db/20),10**(3/20))
    return x*_sm(g,sr*0.02)                                          # deeper dips between hums (no gating: floor)
def f0_scoop(x, sr, ons, st=-1.5, down_ms=80, back_ms=170):
    n=len(x); d=np.zeros(n)  # read-pointer offset in samples; r = 1 + d'
    for q,p in ons:
        a=int(sr*down_ms/1000); b=int(sr*back_ms/1000); r0=2**(st/12)-1
        dp=np.r_[np.linspace(r0,0,a), np.full(b, -r0*a/2/b)]
        s=q; e=min(n,s+len(dp)); d[s:e]+=np.cumsum(dp[:e-s])
    t=np.arange(n)+d; return np.interp(t,np.arange(n),x)
def air_floor(low_proc, y_orig, sr, target_db=-40, split=4000):
    sos=ss.butter(2,split,fs=sr,output="sos"); lo=ss.sosfiltfilt(sos,low_proc); hp_p=low_proc-lo
    hp_o=y_orig-ss.sosfiltfilt(sos,y_orig)
    ep=rms_env(low_proc,sr,20); eo=rms_env(y_orig,sr,20); hp_o=hp_o*np.clip(ep/(eo+1e-9),0,4)   # orig air follows processed loudness
    h=0.5*hp_p/(np.sqrt(np.mean(hp_p**2))+1e-12)+0.5*hp_o/(np.sqrt(np.mean(hp_o**2))+1e-12)
    top=min(8000,sr/2*0.95); ref=dens(lo,sr,100,1000); Tp=10**((ref+target_db)/10); Plo=10**(dens(lo,sr,4000,top)/10); Ph=10**(dens(h,sr,4000,top)/10)
    g=np.sqrt(max(Tp-Plo,0.1*Tp)/Ph); return lo+g*h
def emo_expand(x,sr,emo=0.2):
    rr=rms_env(x,sr,100); act=rr>np.percentile(rr,95)*10**(-30/20); med=np.median(rr[act])
    return x*_sm(np.clip((rr/med)**emo,10**(-4/20),10**(4/20)),sr*0.05)
def render(path, out, nasal_a, af_hz, w_nasal=0.6, f2_freeze_ms=120, af_bw=400, lp_hz=3500, air_db=-40, mix_path=None, mix=0.0,
           gag_path=None, w_gag=0.0, hum=None, scoop_st=-1.5, emo=0.2):
    y,sr=loadn(path); y16=librosa.resample(y,orig_sr=sr,target_sr=A_SR)
    Lf=frame_lsf(y16); k1=5; Ls=np.stack([_sm(Lf[:,i],k1) for i in range(16)],1)
    Ln=N.lpc2lsf(np.array(nasal_a))
    if gag_path:
        g,gs=loadn(gag_path); g16=librosa.resample(g,orig_sr=gs,target_sr=A_SR); Lg=env_lsf_cheaptrick(g16)
        Lg=np.stack([_sm(Lg[:,i],5) for i in range(16)],1); n=len(Ls); Lg=np.r_[Lg,np.repeat(Lg[-1:],max(0,n-len(Lg)),0)][:n]
        Lt=w_gag*Lg+(1-w_gag)*Ln[None,:]
    else:
        k2=int(f2_freeze_ms/5); Lfz=np.stack([_sm(Lf[:,i],k2) for i in range(16)],1); Lt=(1-w_nasal)*Lfz+w_nasal*Ln[None,:]
    Lt=fixlsf(Lt); x=refilter(y,sr,Ls,Lt)
    x=ss.lfilter(zero_biquad(sr,af_hz,af_bw),[1.0],x)
    x=ss.sosfilt(ss.butter(1,lp_hz,fs=sr,output="sos"),x)            # light 1st-order tissue LP (causal, -6 dB/oct)
    ri=rms_env(y,sr,30); ro=rms_env(x,sr,30); x=x*_sm(np.clip(ri/ro,0,30),sr*0.03)
    if emo>0: x=emo_expand(x,sr,emo)
    if mix_path and mix>0:
        m,ms=loadn(mix_path); m=librosa.resample(m,orig_sr=ms,target_sr=sr) if ms!=sr else m; m=np.r_[m,np.zeros(max(0,len(x)-len(m)))][:len(x)]
        x=(1-mix)*x/(np.sqrt(np.mean(x**2))+1e-9)+mix*m/(np.sqrt(np.mean(m**2))+1e-9)
    ons=syllables(x,sr)
    if hum: x=hum_shape(x,sr,ons,**hum)
    x=air_floor(x,y,sr,target_db=air_db)
    if scoop_st: x=f0_scoop(x,sr,ons,st=scoop_st)
    x=x/(np.max(np.abs(x))+1e-9)*0.89; sf.write(out,x.astype(np.float32),sr); return dict(sr=sr,n_syll=len(ons))
