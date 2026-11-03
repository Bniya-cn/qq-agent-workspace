"""Assemble the final lounge with four AI characters."""
import bpy
import os
from pathlib import Path

ROOT = Path("/Users/daijinglin/Desktop/qq-agent-workspace")
SOURCES = [
    ("bigfish", ROOT / "renders/generated/bigfish_rm5_uv.blend", "V3_bigfish_", "AI3D_Bigfish"),
    ("dania", ROOT / "renders/generated/dania_full_solid3_uv.blend", "V3_dania_", "AI3D_Dania"),
    ("kaltsit", ROOT / "renders/generated/kaltsit_full_uv.blend", "V3_kaltsit_", "AI3D_Kaltsit"),
]
OUT_BLEND = ROOT / "renders/prts-lounge-ai-final.blend"
OUT_HERO = ROOT / "renders/prts-lounge-ai-hero.png"
OUT_3Q = ROOT / "renders/prts-lounge-ai-3q.png"
OUT_GLB = ROOT / "renders/prts-lounge-ai-final.glb"

for _tag, _path, prefix, _name in SOURCES:
    for o in bpy.data.objects:
        if o.name.startswith(prefix):
            o.hide_render = True
            o.hide_viewport = True

for tag, path, _prefix, new_name in SOURCES:
    before = set(bpy.data.objects)
    bpy.ops.wm.append(
        directory=str(path) + "/Object/",
        filename="AI3D_Character",
    )
    added = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    if not added:
        raise RuntimeError(f"appended {tag} not found")
    obj = added[0]
    obj.name = new_name
    print("ASSEMBLE_OBJ", tag, obj.name, len(obj.data.vertices))

scene = bpy.context.scene
scene.render.resolution_x = 1280
scene.render.resolution_y = 720
scene.render.resolution_percentage = 100
scene.view_settings.exposure = 0.4
try:
    scene.eevee.taa_render_samples = 64
except Exception:
    pass

# per-pedestal fill lights so the four figures read evenly
from mathutils import Vector
for i, (x, y) in enumerate(((-1.95, 0.12), (-0.65, 0.0), (0.65, 0.0), (1.95, 0.12))):
    d = bpy.data.lights.new(f"FILL_{i}", type="AREA")
    d.energy = 55
    d.size = 1.4
    d.color = (1.0, 0.95, 0.88)
    o = bpy.data.objects.new(f"FILL_{i}", d)
    scene.collection.objects.link(o)
    o.location = (x, y - 1.5, 1.65)
    o.rotation_euler = (Vector((x, y, 0.7)) - o.location).to_track_quat("-Z", "Y").to_euler()

for cam_name, out in (("CAM_Hero", OUT_HERO), ("CAM_ThreeQuarter", OUT_3Q)):
    cam = bpy.data.objects.get(cam_name)
    if cam is None:
        print("ASSEMBLE_NO_CAM", cam_name)
        continue
    scene.camera = cam
    scene.render.filepath = str(out)
    bpy.ops.render.render(write_still=True)
    print("ASSEMBLE_RENDER", cam_name, out, out.stat().st_size)

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
print("ASSEMBLE_SAVED", OUT_BLEND)

# export visibility-respecting GLB
bpy.ops.export_scene.gltf(
    filepath=str(OUT_GLB),
    export_format="GLB",
    use_visible=True,
    export_apply=True,
)
print("ASSEMBLE_GLB", OUT_GLB, OUT_GLB.stat().st_size)
