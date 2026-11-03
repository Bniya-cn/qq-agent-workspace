# native-SR quality metrics: effective bandwidth, notch count, HF floor, hop-rate modulation, syllabic modulation, nasal level
import numpy as np, soundfile as sf, scipy.signal as ss
def loadn(p):
    y,sr=sf.read(p,always_2d=True); return y.mean(1).astype(np.float64), sr
def ltas(y,sr,nfft=4096):
    f,P=ss.welch(y,sr,nperseg=nfft,noverlap=nfft*3//4); return f,10*np.log10(P+1e-20)
def octsmooth(f,L,frac=6):
    out=np.empty_like(L); Pl=10**(L/10)
    for i,fc in enumerate(f):
        if fc<=0: out[i]=L[i]; continue
        m=(f>=fc*2**(-0.5/frac))&(f<=fc*2**(0.5/frac)); out[i]=10*np.log10(Pl[m].mean())
    return out
def tv(p):
    y,sr=loadn(p); f,L=ltas(y,sr); S=octsmooth(f,L,6); m=(f>=100)&(f<=sr/2*0.98)
    pk=S[(f>=100)&(f<=4000)].max()
    above=np.where(m&(S>=pk-50))[0]; bw50=float(f[above.max()])
    P=10**(L/10); c=np.cumsum(P*(f>=50)); roll99=float(f[np.searchsorted(c,0.99*c[-1])]); roll999=float(f[np.searchsorted(c,0.999*c[-1])])
    def dens(a,b): mm=(f>=a)&(f<b); return 10*np.log10(P[mm].mean()+1e-20)
    hf_floor=dens(4000,min(8000,sr/2))-dens(100,1000); hf_8k=(dens(8000,min(14000,sr/2*0.95))-dens(100,1000)) if sr>17000 else None
    # notches: local minima of 1/6-oct LTAS 200-6000 with >=8 dB depth vs max within +-1/2 octave on both sides
    nn=0; nlist=[]
    idx=np.where((f>=200)&(f<=min(6000,sr/2*0.9)))[0]
    for i in idx[1:-1]:
        if S[i]<=S[i-1] and S[i]<=S[i+1]:
            l=(f>=f[i]/2**0.5)&(f<f[i]); r=(f>f[i])&(f<=f[i]*2**0.5)
            if l.any() and r.any() and min(S[l].max(),S[r].max())-S[i]>=8: nn+=1; nlist.append(int(f[i]))
    # hop-rate (200 Hz) / frame modulation in 1-4 kHz envelope
    b=ss.butter(4,[1000,min(4000,sr/2*0.9)],btype="band",fs=sr,output="sos"); e=np.abs(ss.hilbert(ss.sosfilt(b,y)))
    fe,Pe=ss.welch(e-e.mean(),sr,nperseg=8192); tot=Pe[(fe>20)&(fe<1000)].sum()
    hop200=float(Pe[(fe>190)&(fe<210)].sum()/tot)
    # syllabic modulation depth: std of 2-6 Hz band of dB envelope (active), and 2-6 Hz share
    hop=int(sr*0.005); fr=np.lib.stride_tricks.sliding_window_view(y,int(sr*0.02))[::hop]; r=np.sqrt((fr**2).mean(1)+1e-12); rd=20*np.log10(r)
    act=rd>np.percentile(rd,95)-35; fs_e=sr/hop
    bb=ss.butter(2,[2,6],btype="band",fs=fs_e,output="sos"); m26=ss.sosfiltfilt(bb,rd-rd[act].mean()*0)  # dB envelope band
    syl_depth=float(np.std(m26[act])*2*np.sqrt(2))  # ~peak-to-peak dB of a 2-6 Hz sinusoid
    fm,Pm=ss.welch(r-r.mean(),fs_e,nperseg=min(512,len(r))); syl_share=float(Pm[(fm>=2)&(fm<=6)].sum()/Pm[(fm>=0.5)&(fm<=20)].sum())
    nasal_lvl=dens(200,400)-dens(500,2000)
    return dict(sr=sr,bw50=round(bw50),roll99=round(roll99),roll999=round(roll999),hf_floor_db=round(hf_floor,1),hf_8k_db=None if hf_8k is None else round(hf_8k,1),
                notches=nn,notch_hz=nlist[:8],hop200_mod=round(hop200,3),syl_depth_db=round(syl_depth,2),syl_share=round(syl_share,3),nasal_lvl_db=round(nasal_lvl,1))
