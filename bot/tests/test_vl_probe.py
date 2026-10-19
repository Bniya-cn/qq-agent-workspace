import asyncio
import base64
import os
from pathlib import Path
import unittest
import httpx

BOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BOT_DIR / ".env"
REF_IMG = BOT_DIR / "data" / "refs" / "Touch.png"


def load_env():
    env = {}
    if ENV_FILE.exists():
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


class TestVLModelProbe(unittest.IsolatedAsyncioTestCase):
    async def test_probe_vl_models(self):
        env = load_env()
        api_key = env.get("DASHSCOPE_API_KEY")
        self.assertTrue(api_key, "缺少 DASHSCOPE_API_KEY")

        with open(REF_IMG, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        img_url = f"data:image/png;base64,{b64}"

        candidates = [
            "qwen-vl-plus",
            "qwen3-vl-plus",
            "qwen-vl-max-latest",
            "qwen2.5-vl-72b-instruct",
        ]
        url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        success_model = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for model in candidates:
                payload = {
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": img_url}},
                                {"type": "text", "text": "简要描述图中角色的外貌特征。"},
                            ],
                        }
                    ],
                    "max_tokens": 100,
                }
                resp = await client.post(url, json=payload, headers=headers)
                print(f"\n[PROBE] Model: {model} -> HTTP {resp.status_code}")
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    print(f"[SUCCESS] {model} response: {content[:100]}...")
                    success_model = model
                    break
                else:
                    print(f"[FAIL] {model}: {resp.text[:200]}")

        self.assertIsNotNone(success_model, "所有候选视觉模型均探测失败")
