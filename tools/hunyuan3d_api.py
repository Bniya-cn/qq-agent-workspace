#!/usr/bin/env python3
"""Minimal Hunyuan3D-2mini API compatible with Blender MCP's /generate contract.

Blender MCP sends JSON {image: base64, texture, octree_resolution,
num_inference_steps, guidance_scale} and expects a GLB binary response.

This server intentionally supports shape generation first. Texture generation
is optional because the official paint pipeline relies on CUDA rasterization.
"""
from __future__ import annotations

import argparse
import base64
import io
import logging
import os
import time

import cv2
import numpy as np
import torch
import trimesh
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image
import uvicorn

from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
from hy3dgen.shapegen.schedulers import ConsistencyFlowMatchEulerDiscreteScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("hunyuan3d_api")


def _mps_safe_set_timesteps(self, num_inference_steps=None, device=None, sigmas=None):
    """MPS 不支持 float64；官方 scheduler 默认把 sigmas 保持为 float64。"""
    self.num_inference_steps = num_inference_steps if num_inference_steps is not None else len(sigmas)
    inference_indices = np.linspace(
        0, self.config.pcm_timesteps, num=self.num_inference_steps, endpoint=False
    )
    inference_indices = np.floor(inference_indices).astype(np.int64)
    inference_indices = torch.from_numpy(inference_indices).long()
    self.sigmas_ = self.sigmas[inference_indices].float()
    timesteps = self.sigmas_ * self.config.num_train_timesteps
    self.timesteps = timesteps.to(device=device, dtype=torch.float32)
    self.sigmas_ = torch.cat([self.sigmas_, torch.ones(1, device=self.sigmas_.device)])
    self._step_index = None
    self._begin_index = None


ConsistencyFlowMatchEulerDiscreteScheduler.set_timesteps = _mps_safe_set_timesteps

app = FastAPI()
PIPELINE = None
DEVICE = "cpu"


def remove_solid_background(image: Image.Image, threshold: int = 45) -> Image.Image:
    """Remove a solid background connected to the image border.

    Works for white, black, or colored studio backgrounds. Only pixels connected
    to the border are removed, so white clothing inside the character remains.
    """
    rgba = np.array(image.convert("RGBA"))
    rgb = rgba[:, :, :3].astype(np.float32)
    h, w = rgb.shape[:2]
    patches = [rgb[0:20, 0:20], rgb[0:20, -20:], rgb[-20:, 0:20], rgb[-20:, -20:]]
    border = np.concatenate([p.reshape(-1, 3) for p in patches], axis=0).mean(axis=0)
    dist = np.sqrt(((rgb - border) ** 2).sum(axis=2))
    mask = (dist < threshold).astype(np.uint8)
    flood = mask.copy()
    fill_mask = np.zeros((h + 2, w + 2), np.uint8)
    for seed in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        if flood[seed[1], seed[0]]:
            cv2.floodFill(flood, fill_mask, seed, 2)
    background = flood == 2
    rgba[background, 3] = 0
    return Image.fromarray(rgba, "RGBA")


def load_pipeline(model_path: str, subfolder: str, device: str):
    logger.info("loading %s / %s on %s", model_path, subfolder, device)
    t0 = time.time()
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        model_path,
        subfolder=subfolder,
        use_safetensors=True,
        device=device,
    )
    try:
        pipe.enable_flashvdm(mc_algo="mc")
        logger.info("flashvdm enabled")
    except Exception as exc:
        logger.warning("flashvdm unavailable on %s: %s", device, exc)
    logger.info("pipeline loaded in %.1fs", time.time() - t0)
    return pipe


@app.get("/health")
async def health():
    return {"status": "ok", "device": DEVICE, "pipeline": PIPELINE is not None}


@app.post("/generate")
async def generate(request: Request):
    params = await request.json()
    if "image" not in params:
        return JSONResponse({"error": "image is required"}, status_code=400)
    try:
        raw = base64.b64decode(params["image"])
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
        image = remove_solid_background(image)

        steps = int(params.get("num_inference_steps", 5))
        guidance = float(params.get("guidance_scale", 5.0))
        octree = int(params.get("octree_resolution", 128))
        seed = int(params.get("seed", 1234))
        logger.info("generate steps=%s guidance=%s octree=%s seed=%s", steps, guidance, octree, seed)

        t0 = time.time()
        try:
            generator = torch.Generator(device=DEVICE).manual_seed(seed)
        except Exception:
            generator = None
        mesh = PIPELINE(
            image=image,
            num_inference_steps=steps,
            guidance_scale=guidance,
            octree_resolution=octree,
            mc_algo="mc",
            generator=generator,
        )[0]
        logger.info("shape generated in %.1fs", time.time() - t0)

        buf = io.BytesIO()
        mesh.export(buf, file_type="glb")
        data = buf.getvalue()
        logger.info("glb bytes=%s", len(data))
        return Response(content=data, media_type="model/gltf-binary")
    except Exception as exc:
        logger.exception("generation failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--model_path", default="tencent/Hunyuan3D-2mini")
    parser.add_argument("--subfolder", default="hunyuan3d-dit-v2-mini-turbo")
    parser.add_argument("--device", default="mps")
    args = parser.parse_args()
    DEVICE = args.device
    PIPELINE = load_pipeline(args.model_path, args.subfolder, args.device)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
