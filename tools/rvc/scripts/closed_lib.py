# v5 closed-mouth diagnostics + WORLD closed-mouth resynthesis
import numpy as np, soundfile as sf, librosa, pyworld as pw, scipy.signal as ss
FP=2.5  # WORLD frame period ms
def load(p, sr=None):
    y,s=sf.read(p,always_2d=True); y=y.mean(1).astype(np.float64)
    if sr and s!=sr: y=librosa.resample(y,orig_sr=s,target_sr=sr); s=sr
    return y,s
def world(y,sr):
    f0,t=pw.harvest(y,sr,f0_floor=70,f0_ceil=1000,frame_period=FP); f0=pw.stonemask(y,f0,t,sr)
    sp=pw.cheaptrick(y,f0,t,sr); ap=pw.d4c(y,f0,t,sr); return f0,t,sp,ap
def _db(x): return 10*np.log10(x+1e-12)
def diag(path):
    y,sr=load(path,16000); f0,t,sp,ap=world(y,sr); n=sp.shape[1]; fr=np.linspace(0,sr/2,n)
    hop=int(sr*FP/1000); yp=np.pad(y,(0,800)); rms=np.array([np.sqrt(np.mean(yp[i*hop:i*hop+400]**2)+1e-12) for i in range(len(f0))]); rdb=20*np.log10(rms+1e-9)
    act=rdb>np.percentile(rdb,95)-35; v=(f0>0)&act; out={}
    # --- breathiness: HNR from WORLD ap (power ratio) over 0-5k, per voiced frame
    b=fr<5000; P=sp[:,b]; A=np.clip(ap[:,b],1e-4,1-1e-4)
    hnr=_db((P*(1-A)).sum(1))-_db((P*A).sum(1))
    out["hnr_med_db"]=round(float(np.median(hnr[v])),2) if v.any() else None
    out["ap_mean_0_4k"]=round(float(ap[v][:,fr<4000].mean()),3) if v.any() else None
    e=(sp).sum(1); out["unvoiced_active_energy_pct"]=round(float(e[act&(f0==0)].sum()/(e[act].sum()+1e-12)*100),1)
    # --- transients 1.5-6k bursts (2 ms hop)
    S=np.abs(librosa.stft(y.astype(np.float32),n_fft=256,hop_length=32))**2; f=librosa.fft_frequencies(sr=sr,n_fft=256)
    hb=(f>=1500)&(f<=6000); Eh=_db(S[hb].sum(0)); Et=_db(S.sum(0)); ratio=Eh-Et
    floor=np.percentile(Et,95)-35; vr=np.median(ratio[Et>floor]) if (Et>floor).any() else 0
    w=5  # 10 ms
    rise=Eh-np.array([Eh[max(0,i-w):i+1].min() for i in range(len(Eh))])
    cand=np.where((rise>=10)&(ratio>vr+6)&(Et>floor))[0]; bursts=[]
    for c in cand:
        if not bursts or c-bursts[-1]>15: bursts.append(c)
    dur=len(y)/sr; out["bursts"]=len(bursts); out["bursts_per_s"]=round(len(bursts)/dur,2)
    # onset-locked: HF flux share + HF excess at syllable onsets
    oenv=librosa.onset.onset_strength(y=y.astype(np.float32),sr=sr,hop_length=160); on=librosa.onset.onset_detect(onset_envelope=oenv,sr=sr,hop_length=160,units="samples")
    fl=np.maximum(np.diff(S,axis=1),0); exc=[]; fsh=[]
    for o in on:
        i=o//32; a,bn=max(0,i-3),min(S.shape[1]-1,i+10)
        if Et[a:bn].max()<floor: continue
        exc.append(float(ratio[a:bn].max()-vr)); fsh.append(float(fl[hb][:,a:bn].sum()/(fl[:,a:bn].sum()+1e-12)))
    out["onsets"]=len(exc); out["onset_hf_excess_db_med"]=round(float(np.median(exc)),1) if exc else None
    out["onsets_hf_excess_gt6db"]=int(sum(1 for x in exc if x>6)); out["onset_hf_flux_share_med"]=round(float(np.median(fsh)),3) if fsh else None
    # --- open mouth: LPC F1 (8 kHz, order 10) + 500-1000 share + 800-1200 notch depth
    y8=librosa.resample(y,orig_sr=sr,target_sr=8000); h8=int(8000*FP/1000); F1=[]
    for i in np.where(v)[0][::4]:
        fr8=y8[i*h8:i*h8+240]
        if len(fr8)<240: continue
        fr8=np.append(fr8[0],fr8[1:]-0.5*fr8[:-1])*np.hamming(240)
        try: a=librosa.lpc(fr8.astype(np.float64),order=10)
        except Exception: continue
        r=np.roots(a); r=r[np.imag(r)>0]; fq=np.angle(r)*8000/(2*np.pi); bw=-8000/np.pi*np.log(np.abs(r)+1e-12)
        c=sorted(fq[(fq>150)&(bw<500)])
        if c: F1.append(c[0])
    out["F1_med_hz"]=round(float(np.median(F1)),0) if F1 else None
    if v.any():
        m=sp[v].mean(0); md=_db(m)
        out["e500_1000_of_0_1000_pct"]=round(float(m[(fr>=500)&(fr<1000)].sum()/m[fr<1000].sum()*100),1)
        out["notch_depth_db"]=round(float(md[(fr>=200)&(fr<400)].mean()-md[(fr>=800)&(fr<1200)].min()),1)
        out["lt1200_pct_world"]=round(float(m[fr<1200].sum()/m.sum()*100),1)
    # --- emotion: F0 range/sd semitones, RMS dynamics
    if v.sum()>10:
        st=12*np.log2(f0[v]); out["f0_med"]=round(float(np.median(f0[v])),1)
        out["f0_range_st_p5_95"]=round(float(np.percentile(st,95)-np.percentile(st,5)),2); out["f0_sd_st"]=round(float(st.std()),2)
    ra=rdb[act]; out["rms_dyn_db_p10_95"]=round(float(np.percentile(ra,95)-np.percentile(ra,10)),1)
    out["rms_mod_db_per_10ms_sd"]=round(float(np.std(np.diff(rdb[act][::4]))),2); out["dur"]=round(dur,2); out["voiced_pct"]=round(float(v.sum()/max(act.sum(),1)*100),1)
    return out
# ------------- closed-mouth resynthesis -------------
def nasal_target_db(fr, roll=14.0, notch=18.0, nasal2k=3.0):
    # nasal murmur: pole ~270 Hz, antiresonance ~1 kHz, steep fall above 1.2 kHz, weak nasal pole ~2.2 kHz
    g=lambda c,bw,a: a*np.exp(-0.5*((fr-c)/bw)**2)
    return g(270,90,12)-g(1000,180,notch)+g(2200,250,nasal2k)-np.clip((fr-1200)/1000,0,None)*roll
def closed(path, out, emo=0.0, ap_scale=0.2, f1_target=290.0, alpha_lo=0.6, alpha_hi=1.0, attack_ms=25, uv_gain=0.12, roll=14.0, notch=18.0, warp_on=True, env_on=True, hf_onset_db=12.0, env_mode='morph', d_mid=6.0, boost=3.0, hf_lim_db=None, post_onset=False, hf_excess_db=4.0, attack_post_ms=20.0, nasal2k=3.0, lowmid_db=0.0, pres_db=0.0, whine_notch=False):
    y,sr=load(path); f0,t,sp,ap=world(y,sr); n=sp.shape[1]; fr=np.linspace(0,sr/2,n); v=f0>0
    lsp=np.log(sp+1e-16)
    # 1) F1 warp toward ~290 Hz (envelope frequency warp below 1.5 kHz, identity above)
    d=diag(path); F1=d.get("F1_med_hz") or f1_target; r=float(np.clip(F1/f1_target,1.0,1.8))
    if warp_on and r>1.02:
        wmap=np.where(fr<1500, fr*(r-(r-1)*np.clip(fr/1500,0,1)), fr)  # source freq read for each target freq
        lsp=np.stack([np.interp(wmap,fr,row) for row in lsp])
    # 2) envelope toward nasal murmur: replace smoothed shape, keep fine structure; weaker below 1 kHz to keep timbre cues
    T=nasal_target_db(fr,roll,notch,nasal2k)*np.log(10)/10
    sm=ss.savgol_filter(lsp,  max(5,(int(n*600/(sr/2))//2)*2+1), 2, axis=1)  # ~600 Hz smoothing
    fine=lsp-sm; ref=sm[:,fr<1000].mean(1,keepdims=True); shape=sm-ref
    alpha=np.where(fr<1000,alpha_lo,alpha_hi)
    if env_on and env_mode=='morph': lsp=ref+(1-alpha)*shape+alpha*T+fine*np.where(fr<1200,1.0,0.5)
    if env_on and env_mode=='eq':  # additive closed-mouth EQ: keeps the speaker's own envelope shape
        g=lambda c,bw,a: a*np.exp(-0.5*((fr-c)/bw)**2)
        G=g(270,90,boost)-g(750,200,d_mid)-g(1050,200,notch)-np.clip((fr-1500)/1000,0,None)*roll
        lsp=lsp+G[None,:]*np.log(10)/10
    if lowmid_db or pres_db:  # body (200-400 Hz) + gentle presence (1-2 kHz), below the 3 kHz shelf
        gg_=lambda c,bw,a: a*np.exp(-0.5*((fr-c)/bw)**2)
        lsp=lsp+(gg_(300,110,lowmid_db)+gg_(1500,350,pres_db))[None,:]*np.log(10)/10
    sp2=np.exp(lsp)
    # 3) periodicity: lower aperiodicity on voiced frames; attenuate unvoiced (breath / burst) frames
    ap2=ap.copy(); ap2[v]=np.clip(ap[v]*ap_scale,0.001,1)
    sp2[~v]*=uv_gain
    # 4) onset transient suppression: per-frame attack smoothing + extra HF cut in first 30 ms of each voiced run
    vv=np.r_[False,v]; starts=np.where(vv[1:]&~vv[:-1])[0]; A=int(attack_ms/FP); hf=fr>1200
    gain=np.ones(len(f0))
    for s in starts:
        k=np.arange(min(A,len(f0)-s)); gain[s+k]=np.minimum(gain[s+k],0.5-0.5*np.cos(np.pi*(k+1)/(A+1)))
        for j in range(min(int(30/FP),len(f0)-s)): sp2[s+j,hf]*=10**(-hf_onset_db*(1-j/(30/FP))/10)
    # 5) emotion (optional): expand F0 excursions + RMS dynamics within limits
    f02=f0.copy()
    if emo>0 and v.sum()>10:
        lf=np.log2(f0[v]); m=np.median(lf); ex=m+(lf-m)*(1+emo)
        ex=np.clip(ex,m-7/12,m+5/12)  # max -7/+5 semitones from median: no wailing
        f02[v]=2**ex
        e=np.log(sp2.sum(1)+1e-12); me=np.median(e[v]); g=np.clip(np.exp((e-me)*emo*0.5),10**(-6/10),10**(4/10)); sp2*=g[:,None]
    if hf_lim_db is not None:  # HF transient limiter: clamp 1.5-6 kHz band energy to (37 ms running median + lim) -> removes 哒/嘟 bursts
        import scipy.ndimage as nd
        hb=(fr>=1500)&(fr<=6000); E=10*np.log10(sp2[:,hb].sum(1)+1e-20); Es=nd.median_filter(E,15,mode='nearest')
        ex=np.clip(E-Es-hf_lim_db,0,None); sp2[:,hb]*=(10**(-ex/10))[:,None]
    sp2*=(gain**2)[:,None]
    z=pw.synthesize(f02,np.ascontiguousarray(sp2),np.ascontiguousarray(ap2),sr,FP)
    if post_onset: z=onset_post(z,sr,hf_excess_db,attack_post_ms)
    if whine_notch: z,_=notch_whine(z,sr,f0med=float(np.median(f02[f02>0])) if (f02>0).any() else None)
    z=z/(np.max(np.abs(z))+1e-9)*0.89; sf.write(out,z.astype(np.float32),sr); return dict(f1_in=F1,warp=round(r,3))

def onset_post(z, sr, hf_excess_db=4.0, attack_ms=20.0):
    """Transient suppressor: (1) clamp 1.5-6 kHz share to (voiced median + hf_excess_db) per STFT frame -> burst/哒 attenuation;
    (2) attack follower: envelope may rise only with a ~attack_ms time constant (smooths 哒/嘟 attacks into 嗯 onsets)."""
    import scipy.ndimage as nd
    n_fft=1024 if sr>24000 else 512; hop=n_fft//8
    Z=librosa.stft(z.astype(np.float32),n_fft=n_fft,hop_length=hop); P=np.abs(Z)**2; f=librosa.fft_frequencies(sr=sr,n_fft=n_fft)
    hb=(f>=1500)&(f<=6000); Eh=_db(P[hb].sum(0)); Et=_db(P.sum(0)); r=Eh-Et; act=Et>np.percentile(Et,95)-35
    vr=np.median(r[act]) if act.any() else np.median(r)
    ex=np.clip(r-(vr+hf_excess_db),0,None); ex=nd.maximum_filter1d(ex,3)
    g=np.ones_like(P); g[hb]=10**(-ex/20)[None,:]; z2=librosa.istft(Z*g,hop_length=hop,length=len(z))
    # attack follower on 1 ms RMS envelope
    h=max(1,int(sr/1000)); n=len(z2)//h; e=np.sqrt(np.mean(z2[:n*h].reshape(n,h)**2,1)+1e-12)
    a=np.exp(-1.0/max(attack_ms,1)); fo=np.zeros(n); prev=e[0]
    for i in range(n):
        prev = e[i] if e[i]<prev else a*prev+(1-a)*e[i]; fo[i]=prev
    gg=np.clip(fo/e,0,1); gg=np.repeat(gg,h); gg=np.r_[gg,np.ones(len(z2)-len(gg))]; gg=nd.uniform_filter1d(gg,h*3)
    return z2*gg

def ltas(y, sr, n_fft=4096):
    S=np.abs(librosa.stft(y.astype(np.float32),n_fft=n_fft,hop_length=n_fft//4))**2; e=S.sum(0); act=e>e.max()*10**(-3.5)
    return librosa.fft_frequencies(sr=sr,n_fft=n_fft), S[:,act].mean(1)
def whine_peaks(y, sr, fmin=400, fmax=7000, prom_db=6.0):
    """narrow stationary peaks in the LTAS: prominence over a ~250 Hz median baseline, Q = f / -3 dB bandwidth"""
    import scipy.ndimage as nd
    f,P=ltas(y,sr); d=_db(P); w=max(3,int(250/(f[1]-f[0]))|1); base=nd.median_filter(d,w,mode='nearest'); pr=d-base
    pk,_=ss.find_peaks(pr,height=prom_db); out=[]
    for i in pk:
        if not (fmin<=f[i]<=fmax): continue
        lo=i; hi=i
        while lo>0 and d[lo]>d[i]-3: lo-=1
        while hi<len(d)-1 and d[hi]>d[i]-3: hi+=1
        bw=max(f[hi]-f[lo],f[1]-f[0]); out.append(dict(f=round(float(f[i]),1),prom_db=round(float(pr[i]),1),Q=round(float(f[i]/bw),1)))
    return sorted(out,key=lambda x:-x["prom_db"])
def notch_whine(z, sr, prom_db=6.0, qmin=6.0, f0med=None):
    # skip peaks that are just low harmonics of the (stable) F0 - notching those would hollow the voice, not remove a whine
    def is_harm(f): return f0med is not None and any(abs(f-k*f0med)<0.08*k*f0med for k in range(1,7))
    pk=[p for p in whine_peaks(z,sr,prom_db=prom_db) if p["Q"]>=qmin and not is_harm(p["f"])][:3]
    for p in pk:
        b,a=ss.iirnotch(p["f"],min(p["Q"],30),fs=sr); z=ss.filtfilt(b,a,z)
    return z,pk
def richness(path):
    """spectral tilt, band shares, whine peaks, harmonic count on voiced frames"""
    y,sr=load(path,16000); f,P=ltas(y,sr); tot=P[(f>=50)].sum()
    sh=lambda a,b: round(float(P[(f>=a)&(f<b)].sum()/tot*100),1)
    m=(f>=100)&(f<=5000); sl=np.polyfit(np.log2(f[m]),_db(P[m]),1)[0]
    f0,t=pw.harvest(y,sr,f0_floor=70,f0_ceil=1000,frame_period=10); sp=pw.cheaptrick(y,f0,t,sr); fr=np.linspace(0,sr/2,sp.shape[1]); hc=[]
    S=np.abs(librosa.stft(y.astype(np.float32),n_fft=1024,hop_length=160))**2; fs=librosa.fft_frequencies(sr=sr,n_fft=1024)
    for i in np.where(f0>0)[0][::3]:
        if i>=S.shape[1]: break
        col=_db(S[:,i]); ks=np.arange(1,int(5000/f0[i])+1); amp=np.array([col[np.argmin(np.abs(fs-k*f0[i]))] for k in ks])
        # a harmonic counts if within 30 dB of the strongest one and >= 6 dB above the inter-harmonic valley
        val=np.array([col[np.argmin(np.abs(fs-(k+0.5)*f0[i]))] for k in ks])
        hc.append(int(((amp>amp.max()-30)&(amp-val>=6)).sum()))
    wp=whine_peaks(y,sr)
    return dict(tilt_db_per_oct=round(float(sl),2), e100_500=sh(100,500), e500_1000=sh(500,1000), e1000_2500=sh(1000,2500), e2500_5000=sh(2500,5000),
                harmonics_med=float(np.median(hc)) if hc else None, whine_top=wp[:2], whine_max_prom_db=wp[0]["prom_db"] if wp else 0.0, whine_max_Q=max([p["Q"] for p in wp[:3]],default=0.0))
