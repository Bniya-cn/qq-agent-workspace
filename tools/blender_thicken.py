"""Thicken a flat image-to-3D result: Solidify -> voxel remesh -> smooth.

Single-view generation often produces plate-like hair/ears. Solidify adds a
back shell, remesh fuses self-intersections, smooth removes the voxel stair-step.
"""
import bpy
import os
from pathlib import Path

import numpy as np

OUT_BLEND = Path(os.environ["OUT_BLEND"])
THICKNESS = float(os.environ.get("THICKNESS", "0.012"))
VOXEL = float(os.environ.get("VOXEL_SIZE", "0.0035"))
SMOOTH_ITERS = int(os.environ.get("SMOOTH_ITERS", "4"))
REF_IMAGE = os.environ.get("REF_IMAGE", "")
USE_SOLIDIFY = os.environ.get("USE_SOLIDIFY", "1") == "1"
PRE_DECIMATE = float(os.environ.get("PRE_DECIMATE", "0"))

obj = bpy.data.objects.get("AI3D_Character")
if obj is None:
    raise RuntimeError("AI3D_Character not found")

bpy.context.view_layer.objects.active = obj
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)

if PRE_DECIMATE > 0:
    dec = obj.modifiers.new("PreDecimate", "DECIMATE")
    dec.decimate_type = "COLLAPSE"
    dec.ratio = PRE_DECIMATE
    bpy.ops.object.modifier_apply(modifier=dec.name)
    print("THICKEN_PREDECIMATE", len(obj.data.vertices), len(obj.data.polygons))

if USE_SOLIDIFY:
    solid = obj.modifiers.new("Solidify", "SOLIDIFY")
    solid.thickness = THICKNESS
    solid.offset = 0.0
    solid.use_even_offset = os.environ.get("EVEN_OFFSET", "1") == "1"
    bpy.ops.object.modifier_apply(modifier=solid.name)
    print("THICKEN_SOLIDIFY", len(obj.data.vertices), len(obj.data.polygons))

remesh = obj.modifiers.new("Remesh", "REMESH")
remesh.mode = "VOXEL"
remesh.voxel_size = VOXEL
remesh.use_smooth_shade = True
bpy.ops.object.modifier_apply(modifier=remesh.name)
print("THICKEN_REMESH", len(obj.data.vertices), len(obj.data.polygons))

sm = obj.modifiers.new("Smooth", "SMOOTH")
sm.factor = 0.5
sm.iterations = SMOOTH_ITERS
bpy.ops.object.modifier_apply(modifier=sm.name)
print("THICKEN_SMOOTH", len(obj.data.vertices), len(obj.data.polygons))

if REF_IMAGE and Path(REF_IMAGE).is_file():
    img = bpy.data.images.load(REF_IMAGE)
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    mask = (px[:, :, :3].min(axis=2) < 0.92) & (px[:, :, 3] > 0.01)
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    r0, r1 = int(rows.min()), int(rows.max())
    c0, c1 = int(cols.min()), int(cols.max())
    world = [obj.matrix_world @ v.co for v in obj.data.vertices]
    xs = [p.x for p in world]; zs = [p.z for p in world]
    x0, x1 = min(xs), max(xs); z0, z1 = min(zs), max(zs)
    layer = obj.data.color_attributes.get("Col") or obj.data.color_attributes.new(
        name="Col", type="BYTE_COLOR", domain="POINT")
    for i, p in enumerate(world):
        u = 0.5 if x1 - x0 < 1e-9 else (p.x - x0) / (x1 - x0)
        v = 0.5 if z1 - z0 < 1e-9 else (p.z - z0) / (z1 - z0)
        col = max(0, min(w - 1, int(round(c0 + u * (c1 - c0)))))
        row = max(0, min(h - 1, int(round(r0 + v * (r1 - r0)))))
        r, g, b, a = px[row, col]
        layer.data[i].color = (float(r), float(g), float(b), 1.0)
    print("THICKEN_RECOLORED", len(world))

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
print("THICKEN_SAVED", OUT_BLEND)
