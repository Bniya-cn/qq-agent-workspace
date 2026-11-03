"""Final animation pass: natural base pose + two looping idles per character.

Base poses measured from render tests:
  Dania:   腕.L/R local Z = +/-90deg drops the T-pose arms.
  Kaltsit: c_arm_ik.l/r local Y = -/+1.2 rad drops the T-pose arms.
  Amiya:   static mesh, animated at object level.
  Bigfish: VRM rig already in natural A-pose.
"""
import bpy
import math

FPS = 24
LOOP_A = 96
LOOP_B = 120
DEG = math.pi / 180.0

AM = bpy.data.objects.get("AI3D_Character")
DANIA = bpy.data.objects.get("鸣潮_达妮娅1.4(red ver)_arm")
KAL = bpy.data.objects.get("rig_D")
FISH = bpy.data.objects.get("Armature")

ROOTS = {
    "Amiya": AM,
    "Dania": bpy.data.objects.get("AIModelRoot_Dania_Red") or DANIA,
    "Kaltsit": bpy.data.objects.get("AIModelRoot_Kaltsit") or KAL,
    "Bigfish": bpy.data.objects.get("AIModelRoot_DeepSeek") or FISH,
}


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


def bind(obj, name):
    act = bpy.data.actions.get(name)
    obj.animation_data.action = act
    if act.slots:
        obj.animation_data.action_slot = act.slots[0]
    return act


def base_dania(extra_spine=0.0, extra_neck=0.0, extra_head=0.0, sway=0.0):
    for side, sign in (("L", 1), ("R", -1)):
        pb = DANIA.pose.bones.get(f"腕.{side}")
        if pb:
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, 0.0, 1.57 * sign)
        pe = DANIA.pose.bones.get(f"ひじ.{side}")
        if pe:
            pe.rotation_mode = "XYZ"
            pe.rotation_euler = (0.10, 0.0, 0.0)
    sp = DANIA.pose.bones.get("上半身")
    if sp:
        sp.rotation_mode = "XYZ"
        sp.rotation_euler = (extra_spine, 0.0, sway)
    nk = DANIA.pose.bones.get("首")
    if nk:
        nk.rotation_mode = "XYZ"
        nk.rotation_euler = (extra_neck, 0.0, 0.0)
    hd = DANIA.pose.bones.get("頭")
    if hd:
        hd.rotation_mode = "XYZ"
        hd.rotation_euler = (extra_head, 0.0, 0.0)


def base_kal(extra_spine=0.0, extra_neck=0.0, extra_head=0.0, sway=0.0, head_turn=0.0):
    for side, sign in (("l", 1), ("r", -1)):
        pb = KAL.pose.bones.get(f"c_arm_ik.{side}")
        if pb:
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0.0, -0.35 * sign, 0.0)
    sp = KAL.pose.bones.get("c_spine_02.x") or KAL.pose.bones.get("spine_02.x")
    if sp:
        sp.rotation_mode = "XYZ"
        sp.rotation_euler = (extra_spine, 0.0, sway)
    nk = KAL.pose.bones.get("c_neck.x") or KAL.pose.bones.get("neck.x")
    if nk:
        nk.rotation_mode = "XYZ"
        nk.rotation_euler = (extra_neck, 0.0, 0.0)
    hd = KAL.pose.bones.get("c_head.x") or KAL.pose.bones.get("head.x")
    if hd:
        hd.rotation_mode = "XYZ"
        hd.rotation_euler = (extra_head, head_turn, 0.0)


ANIM_BONES = {
    "Dania": ("腕.L", "腕.R", "ひじ.L", "ひじ.R", "上半身", "首", "頭", "手首.L", "手首.R"),
    "Kaltsit": ("c_arm_ik.l", "c_arm_ik.r", "c_spine_02.x", "c_neck.x", "c_head.x"),
    "Bigfish": ("hips", "chest", "head", "hair_root", "upper_arm.L", "upper_arm.R"),
}


def key_pose_bones(arm, frame, bone_names):
    """Only key the bones the rig actually animates; keying all 800 bones
    would bloat the exported GLB into hundreds of megabytes."""
    for name in bone_names:
        pb = arm.pose.bones.get(name)
        if pb is None:
            continue
        pb.keyframe_insert("rotation_euler", frame=frame)
        pb.keyframe_insert("location", frame=frame)


def key_obj(obj, frame):
    obj.keyframe_insert("location", frame=frame)
    obj.keyframe_insert("rotation_euler", frame=frame)
    obj.keyframe_insert("scale", frame=frame)


# ------------------------------------------------------------------ Amiya
if AM:
    AM.rotation_mode = "XYZ"
    new_action(AM, "Amiya_Idle_A")
    for f in range(0, LOOP_A + 1, 12):
        t = f / LOOP_A * 2 * math.pi
        AM.rotation_euler = (0, 0, 3.2 * DEG * math.sin(t + math.pi / 3))
        s = 1.0 + 0.014 * math.sin(t + math.pi / 2)
        AM.scale = (s, s, s)
        key_obj(AM, f)

    new_action(AM, "Amiya_Idle_B")
    for f in range(0, LOOP_B + 1, 10):
        t = f / LOOP_B * 2 * math.pi
        AM.rotation_euler = (-3.5 * DEG * abs(math.sin(t + math.pi / 2)), 0, 7.0 * DEG * math.sin(t))
        AM.scale = (1, 1, 1)
        key_obj(AM, f)

# ------------------------------------------------------------------ Dania
if DANIA:
    new_action(DANIA, "Dania_Idle_A")
    for f in range(0, LOOP_A + 1, 12):
        t = f / LOOP_A * 2 * math.pi
        base_dania(extra_spine=3.2 * DEG * math.sin(t), extra_neck=2.6 * DEG * math.sin(t), extra_head=2.0 * DEG * math.sin(t))
        key_pose_bones(DANIA, f, ANIM_BONES["Dania"])

    new_action(DANIA, "Dania_Idle_B")
    for f in range(0, LOOP_B + 1, 10):
        t = f / LOOP_B * 2 * math.pi
        base_dania(sway=4.5 * DEG * math.sin(t), extra_head=0.0)
        for side in ("L", "R"):
            pb = DANIA.pose.bones.get(f"手首.{side}")
            if pb:
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = (9.0 * DEG * math.sin(t + math.pi / 2), 0, 0)
        key_pose_bones(DANIA, f, ANIM_BONES["Dania"])

# ------------------------------------------------------------------ Kaltsit
if KAL:
    new_action(KAL, "Kaltsit_Idle_A")
    for f in range(0, LOOP_A + 1, 12):
        t = f / LOOP_A * 2 * math.pi
        base_kal(extra_spine=2.2 * DEG * math.sin(t), extra_neck=3.0 * DEG * math.sin(t), extra_head=2.4 * DEG * math.sin(t))
        key_pose_bones(KAL, f, ANIM_BONES["Kaltsit"])

    new_action(KAL, "Kaltsit_Idle_B")
    for f in range(0, LOOP_B + 1, 10):
        t = f / LOOP_B * 2 * math.pi
        base_kal(sway=3.2 * DEG * math.sin(t), head_turn=6.0 * DEG * math.sin(t))
        key_pose_bones(KAL, f, ANIM_BONES["Kaltsit"])

# ------------------------------------------------------------------ Bigfish
if FISH:
    new_action(FISH, "Bigfish_Idle_A")
    for f in range(0, LOOP_A + 1, 12):
        t = f / LOOP_A * 2 * math.pi
        for pb in FISH.pose.bones:
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0, 0, 0)
            pb.location = (0, 0, 0)
        hips = FISH.pose.bones.get("hips")
        if hips:
            hips.location = (0, 0.009 * math.sin(t), 0)
        chest = FISH.pose.bones.get("chest")
        if chest:
            chest.rotation_euler = (2.4 * DEG * math.sin(t + math.pi / 2), 0, 0)
        head = FISH.pose.bones.get("head")
        if head:
            head.rotation_euler = (0, 0, 3.6 * DEG * math.sin(t + math.pi / 2))
        hair = FISH.pose.bones.get("hair_root")
        if hair:
            hair.rotation_euler = (0, 0, 3.2 * DEG * math.sin(t + math.pi / 3))
        key_pose_bones(FISH, f, ANIM_BONES["Bigfish"])

    new_action(FISH, "Bigfish_Idle_B")
    for f in range(0, LOOP_B + 1, 10):
        t = f / LOOP_B * 2 * math.pi
        for pb in FISH.pose.bones:
            pb.rotation_mode = "XYZ"
            pb.rotation_euler = (0, 0, 0)
            pb.location = (0, 0, 0)
        chest = FISH.pose.bones.get("chest")
        if chest:
            chest.rotation_euler = (3.2 * DEG * math.sin(t), 0, 2.4 * DEG * math.sin(t))
        head = FISH.pose.bones.get("head")
        if head:
            head.rotation_euler = (0, 0, 7.0 * DEG * math.sin(t))
        for side, sign in (("L", 1), ("R", -1)):
            ua = FISH.pose.bones.get(f"upper_arm.{side}")
            if ua:
                ua.rotation_euler = (0, 0, sign * 10.0 * DEG * math.sin(t + math.pi / 2))
        key_pose_bones(FISH, f, ANIM_BONES["Bigfish"])

# ------------------------------------------------------------------ Hover
BASE_Z = {"Amiya": 0.0, "Dania": 0.26, "Kaltsit": 0.291, "Bigfish": 0.259}
for name, root in ROOTS.items():
    if root is None:
        continue
    new_action(root, f"{name}_Hover")
    base_z = BASE_Z[name]
    root.location.z = base_z
    for f, dz in ((0, 0.0), (6, 0.012), (24, 0.10), (30, 0.108)):
        root.location.z = base_z + dz
        root.keyframe_insert("location", index=2, frame=f)
    root.location.z = base_z

# restore a neutral state
bpy.context.scene.render.fps = FPS
bpy.context.scene.frame_start = 0
bpy.context.scene.frame_end = LOOP_B
bpy.context.scene.frame_set(0)
if AM:
    bind(AM, "Amiya_Idle_A")
bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
print("ANIM_V2_DONE", [a.name for a in bpy.data.actions if a.name != "T-Pose"])
