"""Render a close-up looping clip for one character."""
import bpy
import os
from mathutils import Vector

CHAR = os.environ["CHAR"]
CLIP = os.environ["CLIP"]
OUT = os.environ["OUT_DIR"]
FRAMES = int(os.environ.get("FRAMES", "48"))

TARGET = {
    "Amiya": ("AI3D_Character", -1.95, 0.12, 0.55, 1.9),
    "Dania": ("鸣潮_达妮娅1.4(red ver)_arm", -0.65, 0.0, 0.72, 2.4),
    "Kaltsit": ("AI3D_Kaltsit", 0.65, 0.0, 0.72, 2.4),
    "Bigfish": ("Armature", 1.95, 0.12, 0.62, 2.0),
}

obj_name, x, y, cz, dist = TARGET[CHAR]
obj = bpy.data.objects.get(obj_name)
ad = obj.animation_data or obj.animation_data_create()
act = bpy.data.actions.get(CLIP)
ad.action = act
if act and act.slots:
    ad.action_slot = act.slots[0]

scene = bpy.context.scene
cam_data = bpy.data.cameras.new("CAM_CharAnim")
cam = bpy.data.objects.new("CAM_CharAnim", cam_data)
scene.collection.objects.link(cam)
cam.data.lens = 60
cam.location = (x, y - dist, cz + 0.35)
cam.rotation_euler = (Vector((x, y, cz)) - Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()
scene.camera = cam
scene.render.resolution_x = 640
scene.render.resolution_y = 640
scene.render.resolution_percentage = 100
scene.render.fps = 24
try:
    scene.eevee.taa_render_samples = 16
except Exception:
    pass

os.makedirs(OUT, exist_ok=True)
for f in range(FRAMES):
    scene.frame_set(f)
    bpy.context.view_layer.update()
    scene.render.filepath = os.path.join(OUT, f"frame_{f:04d}.png")
    bpy.ops.render.render(write_still=True)
print("DONE", CHAR, CLIP, FRAMES)
