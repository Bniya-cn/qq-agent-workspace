# CAM++ (funasr campplus_cn_common) speaker cosine similarity; run with tools/seed-vc/.venv from tools/seed-vc
import torch, torchaudio, soundfile as sf, glob, os, json, sys, random
sys.path.insert(0, "/Users/daijinglin/Desktop/qq-agent-workspace/tools/seed-vc")
from modules.campplus.DTDNN import CAMPPlus
torch.set_num_threads(4)
W="/Users/daijinglin/Desktop/qq-agent-workspace"; R=f"{W}/tools/rvc"; O=f"{W}/bot/data/test_output/exp_gagged_v3_rvc"
E=f"{W}/bot/data/test_output/exp_gagged_v2"
m=CAMPPlus(feat_dim=80, embedding_size=192); m.load_state_dict(torch.load(f"{W}/tools/seed-vc/checkpoints/campplus_cn_common.bin", map_location="cpu")); m.eval()
def emb(p):
    y,sr=sf.read(p, always_2d=True); y=torch.tensor(y.mean(1),dtype=torch.float32)[None]
    if sr!=16000: y=torchaudio.functional.resample(y,sr,16000)
    f=torchaudio.compliance.kaldi.fbank(y,num_mel_bins=80,dither=0,sample_frequency=16000); f=f-f.mean(0,keepdim=True)
    with torch.no_grad(): e=m(f[None])[0]
    return torch.nn.functional.normalize(e,dim=0)
random.seed(0); cent={}
for ch in ["amiya","dania"]:
    fs=[f for f in sorted(glob.glob(f"{R}/datasets/{ch}/*.wav")) if sf.info(f).duration>=3.0]
    fs=random.sample(fs,min(30,len(fs)))
    cent[ch]=torch.nn.functional.normalize(torch.stack([emb(f) for f in fs]).mean(0),dim=0)
print("char centroid cos amiya-dania:", round(float(cent["amiya"]@cent["dania"]),3))
S=f"{E}/src_nospeech"
srcs={"1":f"{S}/gagged_1_bs_vocals_nospeech.wav","2":f"{S}/gagged_2_bs_vocals_nospeech.wav","3":f"{S}/gagged_3_bs_vocals_nospeech_v2.wav"}
se={k:emb(v) for k,v in srcs.items()}
rows=[]
for k in srcs: rows.append({"file":os.path.basename(srcs[k]),"vs_amiya":round(float(se[k]@cent["amiya"]),3),"vs_dania":round(float(se[k]@cent["dania"]),3),"vs_source":1.0})
outs=sorted(glob.glob(f"{O}/rvc_*.wav"))
# prior Seed-VC best candidates for comparison (gagged3 source)
outs+= [p for p in [f"{E}/vc_final_fixed/vc_dania_seed_g3_s-3_final.wav", f"{E}/vc_final_amiya_ref2/vc_amiya_seed_g3_ref2_CN027_s-2_final.wav"] if os.path.exists(p)]
for p in outs:
    b=os.path.basename(p); ch="amiya" if "amiya" in b else "dania"
    n=b.split("_g")[1][0] if "rvc_" in b else "3"
    e=emb(p); other="dania" if ch=="amiya" else "amiya"
    rows.append({"file":b,"char":ch,"vs_char":round(float(e@cent[ch]),3),"vs_other_char":round(float(e@cent[other]),3),"vs_source":round(float(e@se[n]),3)})
json.dump(rows, open(f"{O}/spk_sim_v3_rvc.json","w"), ensure_ascii=False, indent=1)
for r in rows: print(r)
