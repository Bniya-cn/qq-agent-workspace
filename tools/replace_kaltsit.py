"""Swap the incomplete native Kaltsit for the clothed AI-generated model."""
import bpy
import math
from mathutils import Vector

AI_BLEND = "/Users/daijinglin/Desktop/qq-agent-workspace/renders/generated/kaltsit_full_uv.blend"
TARGET_X, TARGET_Y = 0.65, 0.0
TARGET_H = 0.85
BASE_Z = 0.26
DEG = math.pi / 180.0

# hide the native Kaltsit parts
col = bpy.data.collections.get("SOURCE_Kaltsit_Native_BLEND")
hidden = 0
if col:
    for o in col.objects:
        if o.type == "MESH":
            o.hide_render = True
            o.hide_viewport = True
            hidden += 1
print("NATIVE_HIDDEN", hidden)

# append the AI model
before = set(bpy.data.objects)
bpy.ops.wm.append(directory=AI_BLEND + "/Object/", filename="AI3D_Character")
added = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
if not added:
    raise RuntimeError("AI Kaltsit not found")
obj = added[0]
obj.name = "AI3D_Kaltsit"
obj.rotation_mode = "XYZ"
bpy.context.view_layer.objects.active = obj
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

# fit height and place on the pedestal
dims = obj.dimensions
scale = TARGET_H / max(dims.z, 1e-6)
obj.scale = (scale, scale, scale)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
bb = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
min_z = min(p.z for p in bb)
cx = (min(p.x for p in bb) + max(p.x for p in bb)) / 2
cy = (min(p.y for p in bb) + max(p.y for p in bb)) / 2
obj.location.x += TARGET_X - cx
obj.location.y += TARGET_Y - cy
obj.location.z += BASE_Z - min_z
bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)
print("AI_KALTSIT", len(obj.data.vertices), tuple(round(v, 3) for v in obj.dimensions), tuple(round(v, 3) for v in obj.location))

# object-level idle actions
scene = bpy.context.scene
scene.render.fps = 24


def new_action(name):
    if obj.animation_data is None:
        obj.animation_data_create()
    old = bpy.data.actions.get(name)
    if old:
        bpy.data.actions.remove(old)
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    obj.animation_data.action = act
    return act


def key(frame):
    obj.keyframe_insert("rotation_euler", frame=frame)
    obj.keyframe_insert("scale", frame=frame)


for name, length, amp_deg, amp_scale, phase in (
    ("Kaltsit_Idle_A", 96, 1.6, 0.006, 0.0),
    ("Kaltsit_Idle_B", 120, 4.5, 0.0, 0.0),
):
    new_action(name)
    for f in range(0, length + 1, 12):
        t = f / length * 2 * math.pi
        obj.rotation_euler = (0, 0, amp_deg * DEG * math.sin(t + phase))
        s = 1.0 + amp_scale * math.sin(t + math.pi / 2)
        obj.scale = (s, s, s)
        key(f)

# hover on the object itself (replaces the old root action)
new_action("Kaltsit_Hover")
for f, dz in ((0, 0.0), (6, 0.012), (24, 0.10), (30, 0.108)):
    obj.location.z = BASE_Z + dz
    obj.keyframe_insert("location", index=2, frame=f)
obj.location.z = BASE_Z
obj.animation_data.action = None

bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
print("KALTSIT_REPLACED")
