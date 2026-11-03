"""Render one procedural V3 character on transparent background as a clean
image-to-3D reference."""
import bpy
import colorsys
import os
from pathlib import Path

PREFIX = os.environ["CHAR_PREFIX"]
CENTER_X = float(os.environ["CENTER_X"])
CENTER_Z = float(os.environ.get("CENTER_Z", "0.685"))
ORTHO = float(os.environ.get("ORTHO_SCALE", "1.0"))
OUT = Path(os.environ["OUT_PNG"])
VIVID = os.environ.get("VIVID", "0") == "1"
SAT_BOOST = float(os.environ.get("SAT_BOOST", "1.9"))
VAL_BOOST = float(os.environ.get("VAL_BOOST", "1.1"))

if VIVID:
    mat_prefix = PREFIX.replace("V3_", "M3_")
    for mat in bpy.data.materials:
        if not mat.name.startswith(mat_prefix) or not mat.use_nodes:
            continue
        bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            continue
        r, g, b, a = bsdf.inputs["Base Color"].default_value
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        s = min(1.0, s * SAT_BOOST)
        v = min(1.0, v * VAL_BOOST)
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        bsdf.inputs["Base Color"].default_value = (r, g, b, a)
        print("VIVID_MAT", mat.name, round(s, 2), round(v, 2))

scene = bpy.context.scene
for o in bpy.data.objects:
    if o.type == "MESH" and not o.name.startswith(PREFIX):
        o.hide_render = True

cam_data = bpy.data.cameras.new("CAM_RefGen")
cam_data.type = "ORTHO"
cam_data.ortho_scale = ORTHO
cam = bpy.data.objects.new("CAM_RefGen", cam_data)
scene.collection.objects.link(cam)
cam.location = (CENTER_X, -3.0, CENTER_Z)
cam.rotation_euler = (1.5708, 0.0, 0.0)  # look along +Y
scene.camera = cam

scene.render.film_transparent = True
scene.render.resolution_x = 1024
scene.render.resolution_y = 1024
scene.render.resolution_percentage = 100
try:
    scene.eevee.taa_render_samples = 32
except Exception:
    pass
scene.render.filepath = str(OUT.with_suffix(""))
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
bpy.ops.render.render(write_still=True)

# Blender writes <name>.png for a path without extension in some versions
produced = OUT if OUT.exists() else OUT.with_name(OUT.stem + ".png")
print("REFGEN", produced, produced.stat().st_size)
