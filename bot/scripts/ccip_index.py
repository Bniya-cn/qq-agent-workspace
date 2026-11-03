"""用已经训练好的 CCIP 权重给本地立绘建外观向量。

这里不更新模型参数。新角色要能被比较，只需要把立绘算进索引。
向量存在 bot/data/refs/ccip/，不写入 QQ 用户记忆。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from torch import nn

from ccip_metaformer import Attention, MetaFormer, MlpHead, SepConv

BOT_ROOT = Path(__file__).resolve().parents[1]
CKPT_PATH = BOT_ROOT / "data" / "models" / "ccip-caformer_b36-24.ckpt"
REFS_DIR = BOT_ROOT / "data" / "refs"
INDEX_DIR = REFS_DIR / "ccip"
GAMES = (
    "Arknights",
    "BlueArchive",
    "Endfield",
    "GenshinImpact",
    "HonkaiStarRail",
    "WutheringWaves",
)
IMAGE_SIZE = 384
# CLIP 图像归一化，和 CCIP 训练时的预处理一致。
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
# 模型卡上的同角色差值阈值。只用于召回提示，不是识别概率。
PUBLISHED_DIFF_THRESHOLD = 0.213231
MODEL_ID = "ccip-caformer_b36-24"


class AttentionPool2d(nn.Module):
    """把 12×12 特征图收成一个 768 维外观向量。"""

    def __init__(self, spacial_dim: int, embed_dim: int, num_heads: int, output_dim: int) -> None:
        super().__init__()
        self.positional_embedding = nn.Parameter(
            torch.randn(spacial_dim ** 2 + 1, embed_dim) / embed_dim ** 0.5
        )
        self.k_proj = nn.Linear(embed_dim, embed_dim)
        self.q_proj = nn.Linear(embed_dim, embed_dim)
        self.v_proj = nn.Linear(embed_dim, embed_dim)
        self.c_proj = nn.Linear(embed_dim, output_dim)
        self.num_heads = num_heads

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.flatten(start_dim=2).permute(2, 0, 1)
        x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)
        x = x + self.positional_embedding[:, None, :].to(x.dtype)
        x, _ = torch.nn.functional.multi_head_attention_forward(
            query=x[:1],
            key=x,
            value=x,
            embed_dim_to_check=x.shape[-1],
            num_heads=self.num_heads,
            q_proj_weight=self.q_proj.weight,
            k_proj_weight=self.k_proj.weight,
            v_proj_weight=self.v_proj.weight,
            in_proj_weight=None,
            in_proj_bias=torch.cat([self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]),
            bias_k=None,
            bias_v=None,
            add_zero_attn=False,
            dropout_p=0,
            out_proj_weight=self.c_proj.weight,
            out_proj_bias=self.c_proj.bias,
            use_separate_proj_weight=True,
            training=self.training,
            need_weights=False,
        )
        return x.squeeze(0)


class CCIPBackbone(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.caformer = MetaFormer(
            depths=[3, 12, 18, 3],
            dims=[128, 256, 512, 768],
            token_mixers=[SepConv, SepConv, Attention, Attention],
            head_fn=MlpHead,
            num_classes=2,
            drop_path_rate=0.0,
        )
        self.attnpool = AttentionPool2d(12, 768, 8, 768)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # 分类头是训练时的辅助输出。检索用的是注意力池化后的外观向量。
        for stage_index in range(self.caformer.num_stage):
            x = self.caformer.downsample_layers[stage_index](x)
            x = self.caformer.stages[stage_index](x)
        return self.attnpool(x.permute(0, 3, 1, 2).contiguous())


class _Feature(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.backbone = CCIPBackbone()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.backbone(x)


class _Metrics(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.logit_scale = nn.Parameter(torch.ones([]))


class CCIP(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.feature = _Feature()
        self.metrics = _Metrics()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.feature(x)


def pick_device(name: str = "auto") -> torch.device:
    if name == "cpu":
        return torch.device("cpu")
    if name == "mps":
        return torch.device("mps")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def load_ccip(device: torch.device, ckpt_path: Path = CKPT_PATH) -> CCIP:
    raw = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    if not isinstance(raw, dict):
        raise TypeError(f"CCIP 权重不是 state_dict: {type(raw).__name__}")
    state = {}
    for key, value in raw.items():
        name = str(key)
        if name.startswith("module."):
            name = name[len("module.") :]
        if name.startswith("_orig_mod."):
            name = name[len("_orig_mod.") :]
        state[name] = value
    model = CCIP()
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing or unexpected:
        raise RuntimeError(
            f"CCIP 权重和网络对不上，missing={missing[:8]} unexpected={unexpected[:8]}"
        )
    model.eval()
    return model.to(device)


def to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.getchannel("A"))
        return background
    return image.convert("RGB")


def preprocess_image(image: Image.Image) -> torch.Tensor:
    rgb = to_rgb(image).resize((IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.BILINEAR)
    array = np.asarray(rgb, dtype=np.float32) / 255.0
    array = (array - np.asarray(CLIP_MEAN, dtype=np.float32)) / np.asarray(CLIP_STD, dtype=np.float32)
    return torch.from_numpy(array.transpose(2, 0, 1))


def encode_images(model: CCIP, images: list[Image.Image], device: torch.device) -> np.ndarray:
    batch = torch.stack([preprocess_image(image) for image in images]).to(device)
    with torch.inference_mode():
        features = model(batch)
        features = features / features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
    return features.float().cpu().numpy()


def difference_from_cosine(cosine: float, logit_scale: float) -> float:
    """把余弦相似度换成 CCIP 的差值。相同图接近 0，越不像越大。"""
    scale = float(np.exp(logit_scale))
    logit = scale * float(cosine)
    return (scale - logit) / (scale * 2.0)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_catalog_items(refs_dir: Path, games: tuple[str, ...] = GAMES) -> list[dict]:
    items = []
    for game in games:
        catalog_path = refs_dir / game / "catalog.json"
        if not catalog_path.is_file():
            continue
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        assets = catalog.get("assets") or {}
        for asset_id in sorted(assets):
            asset = assets[asset_id]
            if not isinstance(asset, dict):
                continue
            filename = str(asset.get("filename") or "")
            if not filename:
                continue
            items.append(
                {
                    "game": game,
                    "id": str(asset.get("id") or asset_id),
                    "name": str(asset.get("name") or ""),
                    "aliases": [str(alias) for alias in asset.get("aliases") or [] if str(alias).strip()],
                    "filename": filename,
                    "relpath": f"{game}/{filename}",
                    "catalog_sha256": str(asset.get("sha256") or ""),
                    "status": str(asset.get("status") or ""),
                }
            )
    return items


def _load_existing(index_dir: Path) -> dict[str, np.ndarray]:
    vector_path = index_dir / "vectors.npz"
    meta_path = index_dir / "index.json"
    if not vector_path.is_file() or not meta_path.is_file():
        return {}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    vectors = np.load(vector_path)["vectors"]
    saved = {}
    for row, item in zip(vectors, meta.get("items") or []):
        digest = str(item.get("sha256") or "")
        if digest and row.shape == (768,):
            saved[digest] = np.asarray(row, dtype=np.float32)
    return saved


def _write_index(index_dir: Path, items: list[dict], vectors: np.ndarray, logit_scale: float) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": MODEL_ID,
        "dim": 768,
        "image_size": IMAGE_SIZE,
        "normalized": True,
        "logit_scale": logit_scale,
        "published_difference_threshold": PUBLISHED_DIFF_THRESHOLD,
        "alpha_background": "white",
        "count": len(items),
        "items": items,
    }
    meta_tmp = index_dir / "index.json.part"
    vector_tmp = index_dir / "vectors.part.npz"
    meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    np.savez_compressed(vector_tmp, vectors=vectors.astype(np.float32))
    os.replace(meta_tmp, index_dir / "index.json")
    os.replace(vector_tmp, index_dir / "vectors.npz")


def build_index(
    *,
    refs_dir: Path = REFS_DIR,
    index_dir: Path = INDEX_DIR,
    ckpt_path: Path = CKPT_PATH,
    games: tuple[str, ...] = GAMES,
    limit: int = 0,
    batch_size: int = 4,
    device_name: str = "auto",
) -> dict:
    device = pick_device(device_name)
    model = load_ccip(device, ckpt_path)
    logit_scale = float(model.metrics.logit_scale.detach().float().cpu())
    records = iter_catalog_items(refs_dir, games)
    if limit > 0:
        records = records[:limit]
    cached = {} if limit > 0 else _load_existing(index_dir)
    done_items: list[dict] = []
    done_vectors: list[np.ndarray] = []
    pending: list[dict] = []
    stats = {"encoded": 0, "skipped": 0, "failed": 0, "total": len(records)}

    def flush() -> None:
        if limit > 0 or not done_vectors:
            return
        _write_index(index_dir, done_items, np.stack(done_vectors), logit_scale)

    def take_batch(batch: list[dict]) -> None:
        images = []
        opened = []
        for record in batch:
            path = refs_dir / record["relpath"]
            try:
                with Image.open(path) as image:
                    images.append(image.copy())
                opened.append(record)
            except Exception as exc:
                stats["failed"] += 1
                print(f"[失败] {record['relpath']} {exc}", flush=True)
        if not images:
            return
        try:
            vectors = encode_images(model, images, nonlocal_device[0])
        except RuntimeError as exc:
            if nonlocal_device[0].type != "cpu":
                print(f"[设备] {nonlocal_device[0]} 编码失败，改用 CPU：{exc}", flush=True)
                model.to("cpu")
                nonlocal_device[0] = torch.device("cpu")
                vectors = encode_images(model, images, nonlocal_device[0])
            else:
                raise
        for record, vector in zip(opened, vectors):
            done_items.append(
                {
                    "game": record["game"],
                    "id": record["id"],
                    "name": record["name"],
                    "aliases": record["aliases"],
                    "filename": record["filename"],
                    "relpath": record["relpath"],
                    "sha256": record["sha256"],
                }
            )
            done_vectors.append(np.asarray(vector, dtype=np.float32))
            stats["encoded"] += 1
        finished = len(done_items)
        current = done_items[-1]
        print(
            f"[{finished}/{stats['total']}] {current['game']} {current['name']} "
            f"encoded={stats['encoded']} skipped={stats['skipped']} failed={stats['failed']}",
            flush=True,
        )
        if finished % 20 == 0:
            flush()

    nonlocal_device = [device]
    for record in records:
        path = refs_dir / record["relpath"]
        if not path.is_file():
            stats["failed"] += 1
            print(f"[失败] 缺少文件 {record['relpath']}", flush=True)
            continue
        digest = file_sha256(path)
        record["sha256"] = digest
        cached_vector = cached.get(digest)
        if cached_vector is not None:
            done_items.append(
                {
                    "game": record["game"],
                    "id": record["id"],
                    "name": record["name"],
                    "aliases": record["aliases"],
                    "filename": record["filename"],
                    "relpath": record["relpath"],
                    "sha256": digest,
                }
            )
            done_vectors.append(cached_vector)
            stats["skipped"] += 1
            continue
        pending.append(record)
        if len(pending) >= batch_size:
            take_batch(pending)
            pending = []
    if pending:
        take_batch(pending)
    if done_vectors:
        matrix = np.stack(done_vectors)
        if limit > 0:
            index_dir.mkdir(parents=True, exist_ok=True)
        _write_index(index_dir, done_items, matrix, logit_scale)
    stats["kept"] = len(done_items)
    stats["logit_scale"] = logit_scale
    stats["index_dir"] = str(index_dir)
    return stats


def search_index(query: np.ndarray, index_dir: Path = INDEX_DIR, top_k: int = 5) -> list[dict]:
    meta = json.loads((index_dir / "index.json").read_text(encoding="utf-8"))
    vectors = np.load(index_dir / "vectors.npz")["vectors"].astype(np.float32)
    query = np.asarray(query, dtype=np.float32)
    query = query / max(float(np.linalg.norm(query)), 1e-12)
    scores = vectors @ query
    order = np.argsort(-scores)[: max(1, top_k)]
    logit_scale = float(meta["logit_scale"])
    hits = []
    for index in order:
        item = dict(meta["items"][int(index)])
        cosine = float(scores[int(index)])
        item["cosine"] = cosine
        item["difference"] = difference_from_cosine(cosine, logit_scale)
        hits.append(item)
    return hits
