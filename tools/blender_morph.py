"""Morphological closing for fragmented image-to-3D meshes.

Dilate along normals so nearby shell fragments touch, voxel-remesh to weld
them, then erode back so the silhouette keeps its original size.
"""
import bpy
import os
from pathlib import Path

OUT_BLEND = Path(os.environ["OUT_BLEND"])
DILATE = float(os.environ.get("DILATE", "0.006"))
ERODE = float(os.environ.get("ERODE", "0.004"))
VOXEL_D = float(os.environ.get("VOXEL_DILATE", "0.004"))
VOXEL_E = float(os.environ.get("VOXEL_ERODE", "0.003"))
SMOOTH_ITERS = int(os.environ.get("SMOOTH_ITERS", "4"))

obj = bpy.data.objects.get("AI3D_Character")
if obj is None:
    raise RuntimeError("AI3D_Character not found")
bpy.context.view_layer.objects.active = obj
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)


def apply_displace(strength: float) -> None:
    mod = obj.modifiers.new("Displace", "DISPLACE")
    mod.direction = "NORMAL"
    mod.strength = strength
    mod.mid_level = 0.0
    bpy.ops.object.modifier_apply(modifier=mod.name)


def apply_remesh(voxel: float) -> None:
    mod = obj.modifiers.new("Remesh", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = voxel
    mod.use_smooth_shade = True
    bpy.ops.object.modifier_apply(modifier=mod.name)


apply_displace(DILATE)
print("MORPH_DILATE", len(obj.data.vertices))
apply_remesh(VOXEL_D)
print("MORPH_REMESH_D", len(obj.data.vertices))
apply_displace(-ERODE)
print("MORPH_ERODE", len(obj.data.vertices))
apply_remesh(VOXEL_E)
print("MORPH_REMESH_E", len(obj.data.vertices))

sm = obj.modifiers.new("Smooth", "SMOOTH")
sm.factor = 0.5
sm.iterations = SMOOTH_ITERS
bpy.ops.object.modifier_apply(modifier=sm.name)
print("MORPH_SMOOTH", len(obj.data.vertices), len(obj.data.polygons))

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
print("MORPH_SAVED", OUT_BLEND)
