"""Render a mid-loop pose per character/action pair for animation QA."""
import bpy
from mathutils import Vector

OUT = "/Users/daijinglin/Desktop/qq-agent-workspace/renders/anim-preview/"

JOBS = [
    ("Amiya", "AI3D_Character", -1.95, 0.12, ["Amiya_Idle_A", "Amiya_Idle_B"], [24, 30]),
    ("Dania", "鸣潮_达妮娅1.4(red ver)_arm", -0.65, 0.0, ["Dania_Idle_A", "Dania_Idle_B"], [24, 30]),
    ("Kaltsit", "rig_D", 0.65, 0.0, ["Kaltsit_Idle_A", "Kaltsit_Idle_B"], [24, 30]),
    ("Bigfish", "Armature", 1.95, 0.12, ["Bigfish_Idle_A", "Bigfish_Idle_B"], [24, 30]),
]

scene = bpy.context.scene
scene.render.resolution_x = 640
scene.render.resolution_y = 640
scene.render.resolution_percentage = 100
try:
    scene.eevee.taa_render_samples = 24
except Exception:
    pass

cam_data = bpy.data.cameras.new("CAM_AnimQA")
cam = bpy.data.objects.new("CAM_AnimQA", cam_data)
scene.collection.objects.link(cam)
cam.data.lens = 55
scene.camera = cam

for tag, obj_name, x, y, actions, frames in JOBS:
    obj = bpy.data.objects.get(obj_name)
    if obj is None or obj.animation_data is None:
        print("SKIP", tag)
        continue
    target = Vector((x, y, 0.72))
    cam.location = (x, y - 2.1, 1.0)
    cam.rotation_euler = (target - Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()
    for act_name, frame in zip(actions, frames):
        act = bpy.data.actions.get(act_name)
        if act is None:
            print("MISSING_ACTION", act_name)
            continue
        obj.animation_data.action = act
        # Blender 4.4+ layered actions only evaluate when a slot is bound.
        if act.slots:
            obj.animation_data.action_slot = act.slots[0]
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        path = f"{OUT}{tag}_{act_name}_f{frame}.png"
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        print("QA", tag, act_name, path)
