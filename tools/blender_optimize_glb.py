"""Decimate AI character meshes for a web-friendly GLB, keeping UVs."""
import bpy
import os

RATIO = float(os.environ.get("RATIO", "0.35"))
OUT_BLEND = os.environ["OUT_BLEND"]
OUT_GLB = os.environ["OUT_GLB"]

total_before = total_after = 0
for obj in bpy.data.objects:
    if obj.type != "MESH" or not obj.name.startswith("AI3D_"):
        continue
    before = len(obj.data.polygons)
    total_before += before
    if before < 20000:
        total_after += before
        continue
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    mod = obj.modifiers.new("Decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = RATIO
    bpy.ops.object.modifier_apply(modifier=mod.name)
    after = len(obj.data.polygons)
    total_after += after
    print("OPT", obj.name, before, "->", after)

print("OPT_TOTAL", total_before, "->", total_after)
bpy.ops.wm.save_as_mainfile(filepath=OUT_BLEND)
bpy.ops.export_scene.gltf(filepath=OUT_GLB, export_format="GLB", use_visible=True, export_apply=True)
print("OPT_GLB", OUT_GLB, os.path.getsize(OUT_GLB))
