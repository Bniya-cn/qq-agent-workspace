"""Bigger, more readable idle motions.

- Kaltsit is now a static mesh: object-level breathing + look-around.
- Dania gets a hair-touch gesture on Idle B.
- Bigfish gets a visible arm sway on Idle B.
"""
import bpy
import math

DEG = math.pi / 180.0
LOOP_A = 96
LOOP_B = 120

KAL = bpy.data.objects.get("AI3D_Kaltsit")
DANIA = bpy.data.objects.get("鸣潮_达妮娅1.4(red ver)_arm")
FISH = bpy.data.objects.get("Armature")


def new_action(obj, name):
    if obj.animation_data is None:
        obj.animation_data_create()
    old = bpy.data.actions.get(name)
    if old:
        bpy.data.actions.remove(old)
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    obj.animation_data.action = act
    return act


# ---------------------------------------------------------------- Kaltsit
if KAL:
    KAL.rotation_mode = "XYZ"
    new_action(KAL, "Kaltsit_Idle_A")
    for f in range(0, LOOP_A + 1, 12):
        t = f / LOOP_A * 2 * math.pi
        KAL.rotation_euler = (0, 0, 2.5 * DEG * math.sin(t))
        s = 1.0 + 0.010 * math.sin(t + math.pi / 2)
        KAL.scale = (s, s, s)
        KAL.keyframe_insert("rotation_euler", frame=f)
        KAL.keyframe_insert("scale", frame=f)

    new_action(KAL, "Kaltsit_Idle_B")
    for f in range(0, LOOP_B + 1, 10):
        t = f / LOOP_B * 2 * math.pi
        # look left/right plus a small nod, clearly readable in close-up
        KAL.rotation_euler = (
            -3.0 * DEG * abs(math.sin(t + math.pi / 2)),
            0,
            6.5 * DEG * math.sin(t),
        )
        KAL.scale = (1, 1, 1)
        KAL.keyframe_insert("rotation_euler", frame=f)
        KAL.keyframe_insert("scale", frame=f)
    KAL.animation_data.action = None

# ---------------------------------------------------------------- Dania
if DANIA:
    for pb in DANIA.pose.bones:
        pb.rotation_mode = "XYZ"
    new_action(DANIA, "Dania_Idle_B")
    for f in range(0, LOOP_B + 1, 10):
        t = f / LOOP_B * 2 * math.pi
        for side, sign in (("L", 1), ("R", -1)):
            pb = DANIA.pose.bones.get(f"腕.{side}")
            if pb:
                pb.rotation_euler = (0.0, 0.0, (1.57 + 0.10 * math.sin(t)) * sign)
            pe = DANIA.pose.bones.get(f"ひじ.{side}")
            if pe:
                pe.rotation_euler = (0.10 + 0.25 * (1 + math.sin(t)) / 2, 0, 0)
        sp = DANIA.pose.bones.get("上半身")
        if sp:
            sp.rotation_euler = (0.5 * DEG * math.sin(t), 0, 3.5 * DEG * math.sin(t))
        nk = DANIA.pose.bones.get("首")
        if nk:
            nk.rotation_euler = (0, 0, 0)
        hd = DANIA.pose.bones.get("頭")
        if hd:
            hd.rotation_euler = (0, 5.0 * DEG * math.sin(t + math.pi / 4), 0)
        for name in ("腕.L", "腕.R", "ひじ.L", "ひじ.R", "上半身", "首", "頭"):
            pb = DANIA.pose.bones.get(name)
            if pb:
                pb.keyframe_insert("rotation_euler", frame=f)
    DANIA.animation_data.action = None

# ---------------------------------------------------------------- Bigfish
if FISH:
    for pb in FISH.pose.bones:
        pb.rotation_mode = "XYZ"
    new_action(FISH, "Bigfish_Idle_B")
    for f in range(0, LOOP_B + 1, 10):
        t = f / LOOP_B * 2 * math.pi
        for pb in FISH.pose.bones:
            pb.rotation_euler = (0, 0, 0)
            pb.location = (0, 0, 0)
        chest = FISH.pose.bones.get("chest")
        if chest:
            chest.rotation_euler = (2.5 * DEG * math.sin(t), 0, 1.5 * DEG * math.sin(t))
        head = FISH.pose.bones.get("head")
        if head:
            head.rotation_euler = (0, 0, 5.0 * DEG * math.sin(t))
        # one arm waves up and down, the other stays relaxed
        ua = FISH.pose.bones.get("upper_arm.L")
        if ua:
            ua.rotation_euler = (0, 0, (12.0 * DEG) * (0.5 + 0.5 * math.sin(t)))
        ua2 = FISH.pose.bones.get("upper_arm.R")
        if ua2:
            ua2.rotation_euler = (0, 0, -4.0 * DEG * math.sin(t))
        for name in ("chest", "head", "upper_arm.L", "upper_arm.R"):
            pb = FISH.pose.bones.get(name)
            if pb:
                pb.keyframe_insert("rotation_euler", frame=f)
    FISH.animation_data.action = None

bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
print("ENHANCED", [a.name for a in bpy.data.actions if a.name.startswith(("Kaltsit","Dania","Bigfish"))])
