"""Create two looping idle actions per lounge character plus selection hover.

Each loop starts and ends on the same pose so it can be played by an
AnimationMixer with LoopRepeat. Hover is a separate one-shot action so the web
layer can trigger it on selection.
"""
import bpy
import math

FPS = 24
LOOP_A = 96   # 4.0s
LOOP_B = 120  # 5.0s

DEG = math.pi / 180.0


def ensure_action(obj, name):
    if obj.animation_data is None:
        obj.animation_data_create()
    old = bpy.data.actions.get(name)
    if old:
        bpy.data.actions.remove(old)
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    obj.animation_data.action = act
    return act


def sine_keys(frames, length, amplitude, phase=0.0):
    """Return (frame, value) pairs over one full sine cycle."""
    out = []
    for f in frames:
        t = (f / length) * 2.0 * math.pi + phase
        out.append((f, math.sin(t) * amplitude))
    return out


def key_obj(obj, data_path, index, pairs):
    for f, v in pairs:
        if data_path == "location":
            obj.location[index] = v
        elif data_path == "rotation_euler":
            obj.rotation_euler[index] = v
        elif data_path == "scale":
            obj.scale[index] = v
        obj.keyframe_insert(data_path=data_path, index=index, frame=f)


def bone_rot(arm, bone_name, frame, euler):
    pb = arm.pose.bones.get(bone_name)
    if pb is None:
        print("MISSING_BONE", arm.name, bone_name)
        return
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = euler
    pb.keyframe_insert("rotation_euler", frame=frame)


def bone_loc(arm, bone_name, frame, loc):
    pb = arm.pose.bones.get(bone_name)
    if pb is None:
        print("MISSING_BONE", arm.name, bone_name)
        return
    pb.location = loc
    pb.keyframe_insert("location", frame=frame)


def clear_pose(arm):
    for pb in arm.pose.bones:
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = (0, 0, 0)
        pb.location = (0, 0, 0)
        pb.scale = (1, 1, 1)


bpy.context.scene.render.fps = FPS
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end = LOOP_B

AM = bpy.data.objects.get("AI3D_Character")
DANIA_ARM = bpy.data.objects.get("鸣潮_达妮娅1.4(red ver)_arm")
KAL_ARM = bpy.data.objects.get("rig_D")
FISH_ARM = bpy.data.objects.get("Armature")

ROOTS = {
    "Amiya": AM,
    "Dania": bpy.data.objects.get("AIModelRoot_Dania_Red") or DANIA_ARM,
    "Kaltsit": bpy.data.objects.get("AIModelRoot_Kaltsit") or KAL_ARM,
    "Bigfish": bpy.data.objects.get("AIModelRoot_DeepSeek") or FISH_ARM,
}
for k, v in ROOTS.items():
    if v is None:
        print("MISSING_ROOT", k)
    else:
        print("ROOT", k, v.name, tuple(round(x, 3) for x in v.location))

# ---------------------------------------------------------------- Amiya
if AM:
    ensure_action(AM, "Amiya_Idle_A")
    frames = range(0, LOOP_A + 1, 12)
    for f, v in sine_keys(frames, LOOP_A, 0.012):
        key_obj(AM, "location", 2, [(f, 0.0 + v)])
    for f, v in sine_keys(frames, LOOP_A, 1.5 * DEG, phase=math.pi / 3):
        key_obj(AM, "rotation_euler", 2, [(f, v)])
    for f, v in sine_keys(frames, LOOP_A, 0.006, phase=math.pi / 2):
        key_obj(AM, "scale", 0, [(f, 1.0 + v)])
        key_obj(AM, "scale", 1, [(f, 1.0 + v)])
        key_obj(AM, "scale", 2, [(f, 1.0 + v)])

    ensure_action(AM, "Amiya_Idle_B")
    frames = range(0, LOOP_B + 1, 10)
    for f, v in sine_keys(frames, LOOP_B, 0.008):
        key_obj(AM, "location", 2, [(f, 0.0 + v)])
    for f, v in sine_keys(frames, LOOP_B, 4.0 * DEG):
        key_obj(AM, "rotation_euler", 2, [(f, v)])
    for f, v in sine_keys(frames, LOOP_B, 1.8 * DEG, phase=math.pi / 2):
        key_obj(AM, "rotation_euler", 0, [(f, -abs(v))])

# ---------------------------------------------------------------- Dania
if DANIA_ARM:
    clear_pose(DANIA_ARM)
    ensure_action(DANIA_ARM, "Dania_Idle_A")
    frames = range(0, LOOP_A + 1, 12)
    for f, v in sine_keys(frames, LOOP_A, 1.6 * DEG):
        bone_rot(DANIA_ARM, "上半身", f, (v, 0, 0))
        bone_rot(DANIA_ARM, "首", f, (v * 0.8, 0, 0))
        bone_rot(DANIA_ARM, "頭", f, (v * 0.6, 0, 0))
    for f, v in sine_keys(frames, LOOP_A, 0.004, phase=math.pi / 2):
        bone_loc(DANIA_ARM, "センター", f, (0, 0, v))

    clear_pose(DANIA_ARM)
    ensure_action(DANIA_ARM, "Dania_Idle_B")
    frames = range(0, LOOP_B + 1, 10)
    for f, v in sine_keys(frames, LOOP_B, 2.4 * DEG):
        bone_rot(DANIA_ARM, "上半身", f, (0, 0, v))
        bone_rot(DANIA_ARM, "頭", f, (0, v * 2.0, 0))
    for f, v in sine_keys(frames, LOOP_B, 5.0 * DEG, phase=math.pi / 2):
        bone_rot(DANIA_ARM, "手首.L", f, (v, 0, 0))
        bone_rot(DANIA_ARM, "手首.R", f, (v, 0, 0))

# ---------------------------------------------------------------- Kaltsit
if KAL_ARM:
    clear_pose(KAL_ARM)
    ensure_action(KAL_ARM, "Kaltsit_Idle_A")
    frames = range(0, LOOP_A + 1, 12)
    for f, v in sine_keys(frames, LOOP_A, 1.0 * DEG):
        bone_rot(KAL_ARM, "spine_02.x", f, (v, 0, 0))
        bone_rot(KAL_ARM, "neck.x", f, (v * 1.5, 0, 0))
        bone_rot(KAL_ARM, "head.x", f, (v * 1.2, 0, 0))

    clear_pose(KAL_ARM)
    ensure_action(KAL_ARM, "Kaltsit_Idle_B")
    frames = range(0, LOOP_B + 1, 10)
    for f, v in sine_keys(frames, LOOP_B, 1.6 * DEG):
        bone_rot(KAL_ARM, "spine_03.x", f, (0, 0, v))
        bone_rot(KAL_ARM, "head.x", f, (0, v * 2.2, 0))
    for f, v in sine_keys(frames, LOOP_B, 2.0 * DEG, phase=math.pi / 2):
        bone_rot(KAL_ARM, "shoulder.l", f, (0, 0, v))

# ---------------------------------------------------------------- Bigfish
if FISH_ARM:
    clear_pose(FISH_ARM)
    ensure_action(FISH_ARM, "Bigfish_Idle_A")
    frames = range(0, LOOP_A + 1, 12)
    for f, v in sine_keys(frames, LOOP_A, 0.004):
        bone_loc(FISH_ARM, "hips", f, (0, v, 0))
    for f, v in sine_keys(frames, LOOP_A, 1.2 * DEG, phase=math.pi / 2):
        bone_rot(FISH_ARM, "chest", f, (v, 0, 0))
        bone_rot(FISH_ARM, "head", f, (0, 0, v * 1.6))
    for f, v in sine_keys(frames, LOOP_A, 1.5 * DEG, phase=math.pi / 3):
        bone_rot(FISH_ARM, "hair_root", f, (0, 0, v))

    clear_pose(FISH_ARM)
    ensure_action(FISH_ARM, "Bigfish_Idle_B")
    frames = range(0, LOOP_B + 1, 10)
    for f, v in sine_keys(frames, LOOP_B, 2.0 * DEG):
        bone_rot(FISH_ARM, "chest", f, (v * 0.8, 0, v * 0.6))
        bone_rot(FISH_ARM, "head", f, (0, 0, v * 2.2))
    for f, v in sine_keys(frames, LOOP_B, 6.0 * DEG, phase=math.pi / 2):
        bone_rot(FISH_ARM, "upper_arm.L", f, (0, 0, v))
        bone_rot(FISH_ARM, "upper_arm.R", f, (0, 0, -v))

# ---------------------------------------------------------------- Hover
for name, root in ROOTS.items():
    if root is None:
        continue
    ensure_action(root, f"{name}_Hover")
    base = root.location.z
    key_obj(root, "location", 2, [(0, base), (6, base + 0.012), (24, base + 0.10), (30, base + 0.108)])

# leave a neutral action assigned so the saved file opens on idle A
if AM:
    AM.animation_data.action = bpy.data.actions.get("Amiya_Idle_A")

bpy.context.scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
print("ANIM_DONE actions", [a.name for a in bpy.data.actions])
