#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
from huggingface_hub import hf_hub_download

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
repo = "tencent/Hunyuan3D-2mini"
files = [
    "hunyuan3d-dit-v2-mini-turbo/config.yaml",
    "hunyuan3d-dit-v2-mini-turbo/model.fp16.safetensors",
    "hunyuan3d-vae-v2-mini-turbo/config.yaml",
    "hunyuan3d-vae-v2-mini-turbo/model.fp16.safetensors",
]
base = Path.home() / ".cache" / "hy3dgen" / repo
for rel in files:
    print(f"FETCH {rel}", flush=True)
    path = hf_hub_download(repo_id=repo, filename=rel, endpoint=os.environ["HF_ENDPOINT"])
    dst = base / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)
    print(f"SAVED {dst} {dst.stat().st_size} bytes", flush=True)
print("ALL_DONE", flush=True)
