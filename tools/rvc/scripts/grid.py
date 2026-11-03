# In-process RVC grid inference (resumable). usage: grid.py jobs.json
import sys, os, json
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK","1")
A="/Users/daijinglin/Desktop/qq-agent-workspace/tools/rvc/applio"; os.chdir(A); sys.path.insert(0,A)
from rvc.infer.infer import VoiceConverter
from core import _base_infer_kwargs
vc=VoiceConverter()
jobs=json.load(open(sys.argv[1])); done=0
for j in jobs:
    if os.path.exists(j["out"]) and os.path.getsize(j["out"])>1000: continue
    kw=_base_infer_kwargs(pitch=j["pitch"], index_rate=j["ir"], volume_envelope=j["rms"], protect=j["pr"], f0_method="rmvpe",
        pth_path=j["pth"], index_path=j["index"], export_format="WAV", embedder_model="contentvec")
    kw["audio_input_path"]=j["src"]; kw["audio_output_path"]=j["out"]
    vc.convert_audio(**kw); done+=1
    print("OK", os.path.basename(j["out"]), flush=True)
print("GRID_DONE", done, flush=True)
