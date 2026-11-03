import bpy
import math
from pathlib import Path
from mathutils import Vector

ROOT = Path("/Users/daijinglin/Desktop/qq-agent-workspace")
OUT = ROOT / "renders/generated"
obj = bpy.data.objects.get("AI3D_Amiya")
if obj is None:
    raise RuntimeError("AI3D_Amiya not found")

# origin at bounding-box center so rotation spins the figure in place
corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
center = sum(corners, Vector()) / 8.0
bpy.context.scene.cursor.location = center
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.origin_set(type="ORIGIN_CURSOR")

# dedicated front camera
cam_data = bpy.data.cameras.new("CAM_AI_Test")
cam = bpy.data.objects.new("CAM_AI_Test", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.location = (center.x, center.y - 2.0, center.z + 0.12)
cam.rotation_euler = (Vector((center.x, center.y, center.z)) - Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()
cam.data.lens = 50
scene = bpy.context.scene
scene.camera = cam
scene.render.resolution_x = 640
scene.render.resolution_y = 640
scene.render.resolution_percentage = 100
try:
    scene.eevee.taa_render_samples = 16
except Exception:
    pass

for angle in (0, 90, 180, 270):
    obj.rotation_euler.z = math.radians(angle)
    path = OUT / f"amiya_rot_{angle}.png"
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    print("ROT", angle, path, path.stat().st_size)
