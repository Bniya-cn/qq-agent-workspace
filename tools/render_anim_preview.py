"""Render animation preview sequences (idle loop and selection transition)."""
import bpy
import os

MODE = os.environ.get("MODE", "idle")
OUT = os.environ.get("OUT_DIR", "/tmp/anim_frames/idle/")
FRAMES = int(os.environ.get("FRAMES", "72"))

ROOT_BASE = {
    "AI3D_Character": 0.0,
    "AIModelRoot_Dania_Red": 0.26,
    "AIModelRoot_Kaltsit": 0.291,
    "AIModelRoot_DeepSeek": 0.259,
}
IDLE = {
    "AI3D_Character": "Amiya_Idle_A",
    "鸣潮_达妮娅1.4(red ver)_arm": "Dania_Idle_A",
    "AI3D_Kaltsit": "Kaltsit_Idle_A",
    "Armature": "Bigfish_Idle_A",
}
SPOTS = {
    "L3_Spot_amiya": (120, (0.30, 0.62, 0.92), 0.73),
    "L3_Spot_dania": (55, (0.78, 0.50, 0.68), 0.73),
    "L3_Spot_kaltsit": (55, (0.50, 0.75, 0.55), 0.73),
    "L3_Spot_bigfish": (60, (0.28, 0.62, 0.90), 0.73),
}
HOLY = (1.0, 0.96, 0.84)
AMBIENT = ("FILL_0", "FILL_1", "FILL_2", "FILL_3", "L_Key_Warm", "L_Fill_Cool", "L_Ceiling_Practical")
_ambient_base = {}


def play(obj_name, act_name, frame=0):
    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        return
    ad = obj.animation_data or obj.animation_data_create()
    act = bpy.data.actions.get(act_name)
    ad.action = act
    if act and act.slots:
        ad.action_slot = act.slots[0]


def dim_ambient(factor):
    for name in AMBIENT:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        if name not in _ambient_base:
            _ambient_base[name] = obj.data.energy
        obj.data.energy = _ambient_base[name] * factor


scene = bpy.context.scene
scene.camera = bpy.data.objects["CAM_Hero"]
scene.render.resolution_x = 960
scene.render.resolution_y = 540
scene.render.resolution_percentage = 100
scene.render.fps = 24
try:
    scene.eevee.taa_render_samples = 16
except Exception:
    pass

for name, z in ROOT_BASE.items():
    obj = bpy.data.objects.get(name)
    if obj:
        if obj.animation_data:
            obj.animation_data.action = None
        obj.location.z = z
for obj_name, act_name in IDLE.items():
    play(obj_name, act_name)

if MODE == "select":
    # Amiya selection: hover up while holy light takes over
    play("AI3D_Character", "Amiya_Hover")
    dim_ambient(0.05)
    for name, (energy, color, size) in SPOTS.items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        if name.endswith("amiya"):
            obj.data.energy = 900
            obj.data.color = HOLY
            obj.data.spot_size = 0.38
        else:
            obj.data.energy = 8
            obj.data.color = color
            obj.data.spot_size = size

os.makedirs(OUT, exist_ok=True)
for f in range(0, FRAMES):
    scene.frame_set(f)
    bpy.context.view_layer.update()
    scene.render.filepath = os.path.join(OUT, f"frame_{f:04d}.png")
    bpy.ops.render.render(write_still=True)
    if f % 12 == 0:
        print("RENDERED", MODE, f)
print("DONE", MODE, FRAMES)
