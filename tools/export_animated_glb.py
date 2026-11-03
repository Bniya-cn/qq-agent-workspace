"""Export the lounge as GLB with one NLA track per action, then restore the
clean file. NLA is used only because the glTF exporter skips some layered
actions in ACTIONS mode."""
import bpy
import os

OUT = "/Users/daijinglin/Desktop/qq-agent-workspace/renders/prts-lounge-animated.glb"

JOBS = {
    "AI3D_Character": ["Amiya_Idle_A", "Amiya_Idle_B", "Amiya_Hover"],
    "鸣潮_达妮娅1.4(red ver)_arm": ["Dania_Idle_A", "Dania_Idle_B", "Dania_Hover"],
    "AI3D_Kaltsit": ["Kaltsit_Idle_A", "Kaltsit_Idle_B", "Kaltsit_Hover"],
    "Armature": ["Bigfish_Idle_A", "Bigfish_Idle_B", "Bigfish_Hover"],
}
# hover actions are authored on the root empties
ROOTS = {
    "Amiya": "AI3D_Character",
    "Dania": "AIModelRoot_Dania_Red",
    "Bigfish": "AIModelRoot_DeepSeek",
}


def clear_nla(obj):
    if obj.animation_data:
        obj.animation_data.action = None
        for t in list(obj.animation_data.nla_tracks):
            obj.animation_data.nla_tracks.remove(t)


# hover actions live on empty roots
for name, root_name in ROOTS.items():
    root = bpy.data.objects.get(root_name)
    if root is None:
        continue
    clear_nla(root)
    act = bpy.data.actions.get(f"{name}_Hover")
    if act is None:
        continue
    ad = root.animation_data or root.animation_data_create()
    tr = ad.nla_tracks.new()
    tr.name = f"{name}_Hover"
    st = tr.strips.new(f"{name}_Hover", 0, act)
    if act.slots:
        st.action_slot = act.slots[0]

for obj_name, actions in JOBS.items():
    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        continue
    clear_nla(obj)
    ad = obj.animation_data or obj.animation_data_create()
    for an in actions:
        if an.endswith("_Hover") and obj_name not in ROOTS.values() and obj_name != "AI3D_Kaltsit":
            # armature objects only carry idle tracks; hover is on the root
            continue
        act = bpy.data.actions.get(an)
        if act is None:
            continue
        tr = ad.nla_tracks.new()
        tr.name = an
        st = tr.strips.new(an, 0, act)
        if act.slots:
            st.action_slot = act.slots[0]

bpy.context.scene.frame_set(0)
bpy.ops.export_scene.gltf(
    filepath=OUT,
    export_format="GLB",
    use_visible=True,
    export_apply=False,
    export_animations=True,
    export_animation_mode="NLA_TRACKS",
    export_optimize_animation_size=True,
    export_morph=False,
)
print("GLB_EXPORTED", os.path.getsize(OUT))

# restore clean file
for obj_name in list(JOBS) + list(ROOTS.values()):
    obj = bpy.data.objects.get(obj_name)
    if obj:
        clear_nla(obj)
bpy.ops.wm.save_as_mainfile(filepath=bpy.data.filepath)
print("NLA_RESTORED")
