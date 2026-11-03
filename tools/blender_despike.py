"""Delete degenerate spike triangles before Solidify amplifies them.

Image-to-3D meshes often contain a few triangles with edges orders of
magnitude longer than the median. Solidify turns those into visible needles.
"""
import bpy
import bmesh
import os
from pathlib import Path

OUT_BLEND = Path(os.environ["OUT_BLEND"])
MAX_EDGE = float(os.environ.get("MAX_EDGE", "0.012"))

obj = bpy.data.objects.get("AI3D_Character")
if obj is None:
    raise RuntimeError("AI3D_Character not found")

bm = bmesh.new()
bm.from_mesh(obj.data)
edges = [(e, (e.verts[0].co - e.verts[1].co).length) for e in bm.edges]
lengths = sorted(l for _, l in edges)
median = lengths[len(lengths) // 2] if lengths else 0.0
spikes = [e for e, l in edges if l > MAX_EDGE]
faces = set()
for e in spikes:
    faces.update(e.link_faces)
print("DESPIKE median", round(median, 5), "max", round(lengths[-1], 4),
      "spike_edges", len(spikes), "faces", len(faces))
if faces:
    bmesh.ops.delete(bm, geom=list(faces), context="FACES")
    bmesh.ops.holes_fill(bm, edges=bm.edges, sides=0)
bm.to_mesh(obj.data)
bm.free()
print("DESPIKE_RESULT", len(obj.data.vertices), len(obj.data.polygons))

bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
print("DESPIKE_SAVED", OUT_BLEND)
