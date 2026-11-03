"""Remove tiny disconnected islands (spikes/flecks) from an imported AI3D mesh."""
import bpy
import os
from pathlib import Path

OUT_BLEND = Path(os.environ["OUT_BLEND"])
MIN_VERTS = int(os.environ.get("MIN_VERTS", "1000"))

obj = bpy.data.objects.get("AI3D_Character")
if obj is None:
    raise RuntimeError("AI3D_Character not found")

bpy.context.view_layer.objects.active = obj
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.mesh.separate(type="LOOSE")
bpy.ops.object.mode_set(mode="OBJECT")

parts = [o for o in bpy.context.selected_objects if o.type == "MESH"]
keep = [o for o in parts if len(o.data.vertices) >= MIN_VERTS]
drop = [o for o in parts if o not in keep]
print("CLEANUP_PARTS", len(parts), "keep", len(keep), "drop", len(drop),
      "dropped_verts", sum(len(o.data.vertices) for o in drop))
for o in drop:
    bpy.data.objects.remove(o, do_unlink=True)

bpy.ops.object.select_all(action="DESELECT")
for o in keep:
    o.select_set(True)
bpy.context.view_layer.objects.active = keep[0]
if len(keep) > 1:
    bpy.ops.object.join()
merged = bpy.context.object
merged.name = "AI3D_Character"
print("CLEANUP_RESULT", len(merged.data.vertices), len(merged.data.polygons))

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
print("CLEANUP_SAVED", OUT_BLEND)
