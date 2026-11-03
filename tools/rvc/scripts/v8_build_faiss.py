# build nasal-only faiss index (no torch in this process). never overwrites.
import os, numpy as np, faiss
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O6=f"{W}/bot/data/test_output/exp_gagged_v6_nasal"; A=f"{W}/tools/rvc/applio/logs"
for ch in ["dania","amiya"]:
    X=np.load(f"{O6}/{ch}_nasal_feats_v8.npy").astype(np.float32); n=len(X)
    out=f"{A}/{ch}_gag_rvc/{ch}_gag_rvc_NASALONLY_v8.index"; assert not os.path.exists(out), out
    if n<39*8: idx=faiss.IndexFlatL2(768); idx.add(X); kind="Flat"
    else:
        nl=max(1,min(int(16*np.sqrt(n)),n//39)); idx=faiss.index_factory(768,f"IVF{nl},Flat"); faiss.extract_index_ivf(idx).nprobe=1; idx.train(X); idx.add(X); kind=f"IVF{nl}"
    faiss.write_index(idx,out); print(ch,n,"vectors",round(n*0.02,1),"s",kind,out)
