import json
import urllib.request
import urllib.error

url = "http://127.0.0.1:9880/tts"
payload = {
    "text": "唔……唔唔！嗯呜……唔？唔！唔呜呜！",
    "text_lang": "zh",
    "voice_role": "达妮娅",
    "ref_audio_path": "/Users/daijinglin/Desktop/qq-agent-workspace/gpt-sovits/models/dania/refs/gagged_3.wav",
    "prompt_text": "唔！唔呜唔！唔唔！",
    "prompt_lang": "zh",
    "aux_ref_audio_paths": [
        "/Users/daijinglin/Desktop/qq-agent-workspace/gpt-sovits/models/dania/refs/hf_025.wav",
        "/Users/daijinglin/Desktop/qq-agent-workspace/gpt-sovits/models/dania/refs/hf_025_anchor1.wav",
        "/Users/daijinglin/Desktop/qq-agent-workspace/gpt-sovits/models/dania/refs/hf_025_anchor2.wav"
    ],
    "speed_factor": 1.14,
    "temperature": 0.78,
    "top_p": 0.85,
    "fragment_interval": 0.08,
    "media_type": "wav",
    "text_split_method": "cut5",
    "top_k": 12,
    "sample_steps": 8,
    "seed": -1,
    "repetition_penalty": 1.45
}

data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

print("Sending request to 9880...")
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read()
        print("Success! HTTP Status:", resp.status)
        print("Received audio bytes:", len(content))
        with open("/Users/daijinglin/Desktop/qq-agent-workspace/bot/data/test_dania_gagged3.wav", "wb") as f:
            f.write(content)
        print("Saved to bot/data/test_dania_gagged3.wav")
except urllib.error.HTTPError as e:
    print("HTTPError Status:", e.code)
    print("Error Body:", e.read().decode("utf-8", errors="replace"))
except Exception as e:
    print("General Error:", type(e), e)
