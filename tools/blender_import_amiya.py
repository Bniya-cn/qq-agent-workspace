"""Headless Blender: import generated Amiya, fit to pedestal, render a test view."""
import bpy
import json
from pathlib import Path
from mathutils import Vector

ROOT = Path("/Users/daijinglin/Desktop/qq-agent-workspace")
GLB = ROOT / "renders/generated/amiya.glb"
OUT_BLEND = ROOT / "renders/generated/amiya_import.blend"
OUT_PNG = ROOT / "renders/generated/amiya_preview.png"

# Hide the primitive Amiya so the generated mesh is isolated.
for o in bpy.data.objects:
    if o.name.startswith("V3_amiya_"):
        o.hide_render = True
        o.hide_viewport = True

before = set(bpy.data.objects)
bpy.ops.import_scene.gltf(filepath=str(GLB))
new = [o for o in bpy.data.objects if o not in before]
meshes = [o for o in new if o.type == "MESH"]

if not meshes:
    raise RuntimeError("No mesh imported")

# Join all imported meshes into one object for stable scaling.
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.object
obj.name = "AI3D_Amiya"
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

# Fit height to 0.85 m (body height above the 0.26 m pedestal).
dims = obj.dimensions
target_height = 0.85
scale = target_height / max(dims.z, 1e-6)
obj.scale = (scale, scale, scale)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

# Place on the Amiya pedestal.
min_z = min((obj.matrix_world @ Vector(c)).z for c in obj.bound_box)
obj.location.x += -1.95 - obj.location.x
obj.location.y += 0.12 - obj.location.y
obj.location.z += 0.26 - min_z
bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)

# Neutral clay material for the shape test.
mat = bpy.data.materials.get("M_AI3D_Clay") or bpy.data.materials.new("M_AI3D_Clay")
mat.use_nodes = True
bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
if bsdf:
    bsdf.inputs["Base Color"].default_value = (0.62, 0.58, 0.52, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.55
obj.data.materials.clear()
obj.data.materials.append(mat)

info = {
    "name": obj.name,
    "vertices": len(obj.data.vertices),
    "faces": len(obj.data.polygons),
    "dimensions": [round(v, 3) for v in obj.dimensions],
    "location": [round(v, 3) for v in obj.location],
    "bounds_z": [round(min((obj.matrix_world @ Vector(c)).z for c in obj.bound_box), 3),
                 round(max((obj.matrix_world @ Vector(c)).z for c in obj.bound_box), 3)],
}
print("AI3D_INFO", json.dumps(info, ensure_ascii=False))

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))

scene = bpy.context.scene
scene.camera = bpy.data.objects.get("CAM_Hero") or scene.camera
scene.render.resolution_x = 1280
scene.render.resolution_y = 720
scene.render.resolution_percentage = 100
try:
    scene.eevee.taa_render_samples = 24
except Exception:
    pass
scene.render.filepath = str(OUT_PNG)
bpy.ops.render.render(write_still=True)
print("AI3D_RENDER", OUT_PNG, OUT_PNG.stat().st_size if OUT_PNG.exists() else 0)
