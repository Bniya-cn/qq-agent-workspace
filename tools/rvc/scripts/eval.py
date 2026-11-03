# metrics + envelope corr + CAM++ speaker cosine for all outputs in given dirs. usage: eval.py out.json dir1 [dir2..]
import sys, os, glob, json, re, random
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0,"/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
import torch, torchaudio, numpy as np, soundfile as sf, librosa
from audio_metrics import metrics
from modules.campplus.DTDNN import CAMPPlus
from resemblyzer import VoiceEncoder, preprocess_wav
VE=VoiceEncoder('cpu', verbose=False)
def remb(y):
    w=preprocess_wav(y, source_sr=16000)
    if len(w)<8000: w=y/ (np.max(np.abs(y))+1e-9)
    return VE.embed_utterance(w)
torch.set_num_threads(4)
W="/Users/daijinglin/Desktop/qq-agent-workspace"; R=f"{W}/tools/rvc"
S=f"{W}/bot/data/test_output/exp_gagged_v2/src_nospeech"
SRC={"1":f"{S}/gagged_1_bs_vocals_nospeech.wav","2":f"{S}/gagged_2_bs_vocals_nospeech.wav","3":"/Users/daijinglin/Desktop/qq-agent-workspace/gg-test/wav/gagged_3.wav"}
cm=CAMPPlus(feat_dim=80, embedding_size=192); cm.load_state_dict(torch.load(f"{W}/tools/seed-vc/checkpoints/campplus_cn_common.bin", map_location="cpu")); cm.eval()
def load16(p):
    y,sr=sf.read(p, always_2d=True); y=y.mean(1).astype(np.float32)
    return librosa.resample(y,orig_sr=sr,target_sr=16000) if sr!=16000 else y
def emb(y):
    f=torchaudio.compliance.kaldi.fbank(torch.tensor(y)[None],num_mel_bins=80,dither=0,sample_frequency=16000); f=f-f.mean(0,keepdim=True)
    with torch.no_grad(): return torch.nn.functional.normalize(cm(f[None])[0],dim=0)
def env(y):
    r=librosa.feature.rms(y=y,frame_length=640,hop_length=160)[0]; return np.convolve(r,np.ones(5)/5,mode="same")
random.seed(0); cent={}; rcent={}
for ch in ["amiya","dania"]:
    fs=[f for f in sorted(glob.glob(f"{R}/datasets/{ch}/*.wav")) if sf.info(f).duration>=3.0]
    pick=random.sample(fs,min(30,len(fs))); ys=[load16(f) for f in pick]
    cent[ch]=torch.nn.functional.normalize(torch.stack([emb(y) for y in ys]).mean(0),dim=0)
    rc=np.mean([remb(y) for y in ys],0); rcent[ch]=rc/np.linalg.norm(rc)
sy={k:load16(v) for k,v in SRC.items()}; se={k:emb(v) for k,v in sy.items()}; rse={k:remb(v) for k,v in sy.items()}; senv={k:env(v) for k,v in sy.items()}
out=sys.argv[1]; old={r["file"]:r for r in json.load(open(out))} if os.path.exists(out) else {}
rows=[]
for d in sys.argv[2:]:
    for p in sorted(glob.glob(f"{d}/rvc*.wav")):
        b=os.path.basename(p)
        if b in old and 'res_vs_char' in old[b]: rows.append(old[b]); continue
        ch="amiya" if "_amiya_" in b else "dania"; n=re.search(r"_g(\d)_",b).group(1)
        m=metrics(p, fmin=100, fmax=900); y=load16(p); e=emb(y); re_=remb(y); oc='dania' if ch=='amiya' else 'amiya'; ev=env(y); k=min(len(ev),len(senv[n]))
        m.update(file=b, path=p, char=ch, src=n, env_corr=round(float(np.corrcoef(ev[:k],senv[n][:k])[0,1]),3),
                 spk_vs_char=round(float(e@cent[ch]),3), spk_vs_other=round(float(e@cent["dania" if ch=="amiya" else "amiya"]),3), spk_vs_source=round(float(e@se[n]),3),
                 res_vs_char=round(float(re_@rcent[ch]),3), res_vs_other=round(float(re_@rcent[oc]),3), res_vs_source=round(float(re_@rse[n]),3))
        rows.append(m); print(b, m["lt1200"], m["env_corr"], m["spk_vs_char"], m["spk_vs_source"], flush=True)
json.dump(rows, open(out,"w"), ensure_ascii=False, indent=0, default=float)
src_rows=[]
for k,v in SRC.items():
    m=metrics(v,fmin=100,fmax=900); m.update(file=os.path.basename(v), spk_vs_amiya=round(float(se[k]@cent["amiya"]),3), spk_vs_dania=round(float(se[k]@cent["dania"]),3), res_vs_amiya=round(float(rse[k]@rcent['amiya']),3), res_vs_dania=round(float(rse[k]@rcent['dania']),3)); src_rows.append(m)
json.dump(src_rows, open(out.replace(".json","_sources.json"),"w"), ensure_ascii=False, indent=0, default=float)
