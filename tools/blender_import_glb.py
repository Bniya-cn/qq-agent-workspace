"""Headless Blender: import a generated GLB, fit it to a pedestal, optionally
paint vertex colors by projecting the reference image from the front."""
import bpy
import json
import math
import os
from pathlib import Path
from mathutils import Vector

import numpy as np

GLB = Path(os.environ["GLB_PATH"])
OUT_BLEND = Path(os.environ["OUT_BLEND"])
OUT_CLAY = Path(os.environ["OUT_CLAY"])
OUT_COLOR = Path(os.environ.get("OUT_COLOR", str(OUT_CLAY.with_name(OUT_CLAY.stem + "_color.png"))))
HIDE_PREFIX = os.environ.get("HIDE_PREFIX", "")
PED_X = float(os.environ.get("PEDESTAL_X", "0"))
PED_Y = float(os.environ.get("PEDESTAL_Y", "0"))
TARGET_H = float(os.environ.get("TARGET_HEIGHT", "0.85"))
ROT_Z = float(os.environ.get("ROT_Z_DEG", "0"))
REF_IMAGE = os.environ.get("REF_IMAGE", "")
COLORIZE = os.environ.get("COLORIZE", "1") == "1"

if HIDE_PREFIX:
    for o in bpy.data.objects:
        if o.name.startswith(HIDE_PREFIX):
            o.hide_render = True
            o.hide_viewport = True

before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=str(GLB))
new = [o for o in bpy.data.objects if o not in before]
meshes = [o for o in new if o.type == "MESH"]
if not meshes:
    raise RuntimeError("No mesh imported")

bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.object
obj.name = "AI3D_Character"
obj.rotation_mode = "XYZ"
if ROT_Z:
    obj.rotation_euler.z = math.radians(ROT_Z)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

# fit height
dims = obj.dimensions
scale = TARGET_H / max(dims.z, 1e-6)
obj.scale = (scale, scale, scale)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

# place on pedestal
min_z = min((obj.matrix_world @ Vector(c)).z for c in obj.bound_box)
obj.location.x += PED_X - obj.location.x
obj.location.y += PED_Y - obj.location.y
obj.location.z += 0.26 - min_z
bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)

if os.environ.get("REMESH", "0") == "1":
    voxel = float(os.environ.get("VOXEL_SIZE", "0.009"))
    mod = obj.modifiers.new("Remesh", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = voxel
    mod.use_smooth_shade = True
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=mod.name)
    sm = obj.modifiers.new("Smooth", "SMOOTH")
    sm.factor = 0.5
    sm.iterations = 6
    bpy.ops.object.modifier_apply(modifier=sm.name)
    print("AI3D_REMESHED", len(obj.data.vertices), len(obj.data.polygons))

info = {
    "vertices": len(obj.data.vertices),
    "faces": len(obj.data.polygons),
    "dimensions": [round(v, 3) for v in obj.dimensions],
    "location": [round(v, 3) for v in obj.location],
    "bounds_z": [round(min((obj.matrix_world @ Vector(c)).z for c in obj.bound_box), 3),
                 round(max((obj.matrix_world @ Vector(c)).z for c in obj.bound_box), 3)],
}
print("AI3D_INFO", json.dumps(info, ensure_ascii=False))

# clay material
clay = bpy.data.materials.get("M_AI3D_Clay") or bpy.data.materials.new("M_AI3D_Clay")
clay.use_nodes = True
bsdf = next((n for n in clay.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
if bsdf:
    bsdf.inputs["Base Color"].default_value = (0.62, 0.58, 0.52, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.55
obj.data.materials.clear()
obj.data.materials.append(clay)

scene = bpy.context.scene
scene.camera = bpy.data.objects.get("CAM_Hero") or scene.camera
scene.render.resolution_x = 1280
scene.render.resolution_y = 720
scene.render.resolution_percentage = 100
try:
    scene.eevee.taa_render_samples = 24
except Exception:
    pass
scene.render.filepath = str(OUT_CLAY)
bpy.ops.render.render(write_still=True)
print("AI3D_CLAY", OUT_CLAY, OUT_CLAY.stat().st_size)

if COLORIZE and REF_IMAGE and Path(REF_IMAGE).is_file():
    img = bpy.data.images.load(REF_IMAGE)
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    # non-white character bounds; Blender image rows are bottom-up
    mask = (px[:, :, :3].min(axis=2) < 0.92) & (px[:, :, 3] > 0.01)
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    r0, r1 = int(rows.min()), int(rows.max())
    c0, c1 = int(cols.min()), int(cols.max())
    print("REF_BOUNDS", c0, c1, r0, r1, "image", w, h)

    world = [obj.matrix_world @ v.co for v in obj.data.vertices]
    xs = [p.x for p in world]; zs = [p.z for p in world]
    x0, x1 = min(xs), max(xs); z0, z1 = min(zs), max(zs)
    color_layer = obj.data.color_attributes.new(name="Col", type="BYTE_COLOR", domain="POINT")
    for i, p in enumerate(world):
        u = 0.5 if x1 - x0 < 1e-9 else (p.x - x0) / (x1 - x0)
        v = 0.5 if z1 - z0 < 1e-9 else (p.z - z0) / (z1 - z0)
        col = int(round(c0 + u * (c1 - c0)))
        row = int(round(r0 + v * (r1 - r0)))
        col = max(0, min(w - 1, col)); row = max(0, min(h - 1, row))
        r, g, b, a = px[row, col]
        color_layer.data[i].color = (float(r), float(g), float(b), 1.0)

    painted = bpy.data.materials.new("M_AI3D_Painted")
    painted.use_nodes = True
    nt = painted.node_tree
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    pb = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    vc = nt.nodes.new("ShaderNodeVertexColor")
    vc.layer_name = "Col"
    nt.links.new(vc.outputs["Color"], pb.inputs["Base Color"])
    pb.inputs["Roughness"].default_value = 0.62
    obj.data.materials.clear()
    obj.data.materials.append(painted)
    scene.render.filepath = str(OUT_COLOR)
    bpy.ops.render.render(write_still=True)
    print("AI3D_COLOR", OUT_COLOR, OUT_COLOR.stat().st_size)

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
print("AI3D_SAVED", OUT_BLEND)
