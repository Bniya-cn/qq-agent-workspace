"""Render close-up front and 3/4 views of the imported AI3D_Character."""
import bpy
import math
import os
from pathlib import Path
from mathutils import Vector

OUT = Path(os.environ["OUT_DIR"])
TAG = os.environ.get("TAG", "closeup")
RENDER_COLOR = os.environ.get("RENDER_COLOR", "1") == "1"
SAMPLES = int(os.environ.get("SAMPLES", "64"))
RES = int(os.environ.get("RES", "900"))

obj = bpy.data.objects.get("AI3D_Character")
if obj is None:
    raise RuntimeError("AI3D_Character not found")

if RENDER_COLOR:
    mat = bpy.data.materials.get("M_AI3D_UV") or bpy.data.materials.get("M_AI3D_Painted")
    if mat is not None:
        obj.data.materials.clear()
        obj.data.materials.append(mat)

corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
center = sum(corners, Vector()) / 8.0
size = max(obj.dimensions)

cam_data = bpy.data.cameras.new("CAM_Closeup")
cam = bpy.data.objects.new("CAM_Closeup", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.data.lens = 60
scene = bpy.context.scene
scene.camera = cam
scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.resolution_percentage = 100
scene.view_settings.exposure += float(os.environ.get("EXPOSURE", "0"))
try:
    scene.eevee.taa_render_samples = SAMPLES
except Exception:
    pass

# front view looks along +Y from -Y; 3/4 rotates 35 degrees around Z
dist = size * 2.35
for name, angle in (("front", 0.0), ("threequarter", 35.0)):
    a = math.radians(angle)
    cam.location = (
        center.x + dist * math.sin(a),
        center.y - dist * math.cos(a),
        center.z + size * 0.10,
    )
    cam.rotation_euler = (center - Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()
    path = OUT / f"{TAG}_{name}.png"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    print("CLOSEUP", name, path, path.stat().st_size)
