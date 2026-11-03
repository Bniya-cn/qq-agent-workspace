# asr-venv: transcribe the hum reference -> matching prompt_text (fallback: 嗯 per segment)
import json
from faster_whisper import WhisperModel
W="/Users/daijinglin/Desktop/qq-agent-workspace"; O=f"{W}/bot/data/test_output/exp_gagged_v8_gen/sovits_ref"
info=json.load(open(f"{O}/hum_ref_v8.json")); m=WhisperModel("small",device="cpu",compute_type="int8",cpu_threads=4); pt={}
for ch,v in info.items():
    segs,_=m.transcribe(v["file"],language="zh",beam_size=5,condition_on_previous_text=False,initial_prompt="嗯，哼，嗯嗯。")
    t="".join(s.text for s in segs).strip()
    fb="，".join((u.get("c") or "嗯") for u in v["segments"])+"。"
    pt[ch]=t if t else fb; print(ch,"asr:",t,"| fallback:",fb)
json.dump(pt,open(f"{O}/prompt_text_v8.json","w"),ensure_ascii=False,indent=0)
