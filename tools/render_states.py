"""Render default / Amiya-selected / Dania-selected states for animation QA."""
import bpy

OUT = "/Users/daijinglin/Desktop/qq-agent-workspace/renders/anim-preview/"
ROOT_BASE = {
    "AI3D_Character": 0.0,
    "AIModelRoot_Dania_Red": 0.26,
    "AIModelRoot_Kaltsit": 0.291,
    "AIModelRoot_DeepSeek": 0.259,
}
IDLE0 = {
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

scene = bpy.context.scene
scene.camera = bpy.data.objects["CAM_Hero"]
scene.render.resolution_x = 1280
scene.render.resolution_y = 720
scene.render.resolution_percentage = 100
try:
    scene.eevee.taa_render_samples = 48
except Exception:
    pass


def play(obj_name, act_name, frame):
    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        return
    ad = obj.animation_data or obj.animation_data_create()
    act = bpy.data.actions.get(act_name)
    ad.action = act
    if act and act.slots:
        ad.action_slot = act.slots[0]
    scene.frame_set(frame)
    bpy.context.view_layer.update()


def root_pose(z):
    obj = bpy.data.objects.get("AI3D_Character")
    if obj:
        obj.location.z = z


def reset_roots():
    # detach hover actions so they cannot re-evaluate the stored height
    for name, z in ROOT_BASE.items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        if obj.animation_data:
            obj.animation_data.action = None
        obj.location.z = z


AMBIENT = ("FILL_0", "FILL_1", "FILL_2", "FILL_3", "L_Key_Warm", "L_Fill_Cool", "L_Ceiling_Practical", "L_Ceiling_Practical")
_ambient_base = {}


def set_lights(selected=None):
    for name in AMBIENT:
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        if name not in _ambient_base:
            _ambient_base[name] = obj.data.energy
        obj.data.energy = _ambient_base[name] * (0.25 if selected else 1.0)
    for name, (energy, color, size) in SPOTS.items():
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        if selected and name.endswith(selected):
            obj.data.energy = 900
            obj.data.color = HOLY
            obj.data.spot_size = 0.38
        else:
            obj.data.energy = 8 if selected else energy
            obj.data.color = color
            obj.data.spot_size = size


# ---- default ----
for obj_name, act_name in IDLE0.items():
    play(obj_name, act_name, 0)
reset_roots()
set_lights(None)
scene.render.filepath = OUT + "state_default.png"
bpy.ops.render.render(write_still=True)
print("STATE_DEFAULT")

# ---- Amiya selected ----
play("AI3D_Character", "Amiya_Hover", 30)
bpy.data.objects["AI3D_Character"].location.z = 0.108
set_lights("amiya")
scene.render.filepath = OUT + "state_amiya.png"
bpy.ops.render.render(write_still=True)
print("STATE_AMIYA")

# ---- Dania selected ----
reset_roots()
play("AIModelRoot_Dania_Red", "Dania_Hover", 30)
bpy.data.objects["AIModelRoot_Dania_Red"].location.z = 0.368
set_lights("dania")
scene.render.filepath = OUT + "state_dania.png"
bpy.ops.render.render(write_still=True)
print("STATE_DANIA")

# ---- Kaltsit selected ----
reset_roots()
play("AIModelRoot_Kaltsit", "Kaltsit_Hover", 30)
bpy.data.objects["AIModelRoot_Kaltsit"].location.z = 0.399
set_lights("kaltsit")
scene.render.filepath = OUT + "state_kaltsit.png"
bpy.ops.render.render(write_still=True)
print("STATE_KALTSIT")

# ---- Bigfish selected ----
reset_roots()
play("AIModelRoot_DeepSeek", "Bigfish_Hover", 30)
bpy.data.objects["AIModelRoot_DeepSeek"].location.z = 0.367
set_lights("bigfish")
scene.render.filepath = OUT + "state_bigfish.png"
bpy.ops.render.render(write_still=True)
print("STATE_BIGFISH")
