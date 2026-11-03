#!/usr/bin/env python3
"""Call the local Hunyuan3D /generate API and save a GLB."""
from __future__ import annotations

import argparse
import base64
import time
from pathlib import Path

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("output")
    parser.add_argument("--steps", type=int, default=5)
    parser.add_argument("--octree", type=int, default=128)
    parser.add_argument("--guidance", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--url", default="http://127.0.0.1:8081/generate")
    args = parser.parse_args()

    image_b64 = base64.b64encode(Path(args.image).read_bytes()).decode("ascii")
    payload = {
        "image": image_b64,
        "texture": False,
        "octree_resolution": args.octree,
        "num_inference_steps": args.steps,
        "guidance_scale": args.guidance,
        "seed": args.seed,
    }
    t0 = time.time()
    response = requests.post(args.url, json=payload, timeout=3600)
    cost = round(time.time() - t0, 1)
    print(f"status={response.status_code} cost={cost}s bytes={len(response.content)}")
    if response.status_code != 200:
        print(response.text[:1200])
        raise SystemExit(1)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(response.content)
    print(f"saved {output}")


if __name__ == "__main__":
    main()
