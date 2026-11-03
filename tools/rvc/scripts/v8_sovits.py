# v8: GPT-SoVITS (running api_v2 server) with genuine-hum references; onomatopoeia from gag_onomatopoeia.py; 5 lines x 3 seeds. stdlib only.
import json, sys, time, urllib.request, pathlib, importlib.util
W=pathlib.Path("/Users/daijinglin/Desktop/qq-agent-workspace")
spec=importlib.util.spec_from_file_location("g",W/"bot/src/plugins/agent_core/gag_onomatopoeia.py"); g=importlib.util.module_from_spec(spec); sys.modules["g"]=g; spec.loader.exec_module(g)
O=W/"bot/data/test_output/exp_gagged_v8_gen/sovits"; O.mkdir(parents=True,exist_ok=True)
REF=json.loads((W/"bot/data/test_output/exp_gagged_v8_gen/sovits_ref/hum_ref_v8.json").read_text()); PT=json.loads((W/"bot/data/test_output/exp_gagged_v8_gen/sovits_ref/prompt_text_v8b.json").read_text())
LINES=["放开我！","救命","你是谁","我不会说的","博士……快跑"]; SEEDS=[42,7,123]; ROLE={"dania":"达妮娅","amiya":"阿米娅"}
man=[]
for ch in ["dania","amiya"]:
    for i,line in enumerate(LINES):
        t=g.text_to_gag(line).tts_text
        for s in SEEDS:
            f=O/f"{ch}_L{i+1}_humref_s{s}.wav"; man.append(dict(char=ch,line=line,ono=t,seed=s,file=str(f)))
            if f.exists() and f.stat().st_size>1000: continue
            p=dict(voice_role=ROLE[ch],ref_audio_path=REF[ch]["file"],prompt_text=PT[ch],prompt_lang="zh",text=t,text_lang="zh",media_type="wav",text_split_method="cut0",
                   top_k=12,top_p=0.85,temperature=0.75,sample_steps=8,seed=s,repetition_penalty=1.35,speed_factor=1.0,aux_ref_audio_paths=[])
            r=urllib.request.Request("http://127.0.0.1:9880/tts",data=json.dumps(p).encode(),headers={"Content-Type":"application/json"}); t0=time.time()
            try: f.write_bytes(urllib.request.urlopen(r,timeout=300).read()); print("ok",f.name,t,round(time.time()-t0,1),flush=True)
            except Exception as e: print("FAIL",f.name,e,getattr(e,"read",lambda:b"")()[:200],flush=True)
(O/"manifest.json").write_text(json.dumps(man,ensure_ascii=False,indent=1)); print("SOVITS DONE")
