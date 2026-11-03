"""Project the reference image onto the mesh as an image-texture UV map.

Vertex colors are limited by vertex density (a 900px face gets a few thousand
vertices), which makes eyes and trim look blotchy. A UV map samples the
reference per-pixel on the GPU instead.
"""
import bpy
import os
from pathlib import Path

import numpy as np

OUT_BLEND = Path(os.environ["OUT_BLEND"])
REF_IMAGE = os.environ["REF_IMAGE"]

obj = bpy.data.objects.get("AI3D_Character")
if obj is None:
    raise RuntimeError("AI3D_Character not found")

img = bpy.data.images.load(REF_IMAGE)
w, h = img.size
px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)

# REF_BBOX_PIL="x0,y0,x1,y1" is a top-down PIL box; Blender pixel rows are
# bottom-up, so convert the vertical range here.
bbox_env = os.environ.get("REF_BBOX_PIL", "")
if bbox_env:
    x0p, y0p, x1p, y1p = [int(v) for v in bbox_env.split(",")]
    c0, c1 = x0p, x1p
    r0, r1 = h - 1 - y1p, h - 1 - y0p
else:
    mask = (px[:, :, :3].min(axis=2) < 0.92) & (px[:, :, 3] > 0.01)
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    r0, r1 = int(rows.min()), int(rows.max())
    c0, c1 = int(cols.min()), int(cols.max())
print("UV_REF_BOUNDS", c0, c1, r0, r1, "image", w, h)

me = obj.data
uv = me.uv_layers.get("UVMap") or me.uv_layers.new(name="UVMap")

world = [obj.matrix_world @ v.co for v in me.vertices]
xs = [p.x for p in world]; zs = [p.z for p in world]
x0, x1 = min(xs), max(xs); z0, z1 = min(zs), max(zs)
span_x = max(x1 - x0, 1e-9)
span_z = max(z1 - z0, 1e-9)

uvs = []
for p in world:
    u = (p.x - x0) / span_x
    v = (p.z - z0) / span_z
    uu = (c0 + u * (c1 - c0)) / w
    vv = (r0 + v * (r1 - r0)) / h
    uvs.append((uu, vv))

for loop in me.loops:
    uv.data[loop.index].uv = uvs[loop.vertex_index]

mat = bpy.data.materials.new("M_AI3D_UV")
mat.use_nodes = True
nt = mat.node_tree
pb = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
tex = nt.nodes.new("ShaderNodeTexImage")
tex.image = img
tex.interpolation = "Linear"
tex.extension = "EXTEND"
nt.links.new(tex.outputs["Color"], pb.inputs["Base Color"])
pb.inputs["Roughness"].default_value = 0.62
me.materials.clear()
me.materials.append(mat)

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
print("UV_PAINTED", len(me.vertices), "verts", len(me.loops), "loops", OUT_BLEND)
