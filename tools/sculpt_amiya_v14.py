"""Q 版阿米娅 r14：手办级几何重建。

在 prts-lounge-final.blend 上运行，隐藏旧 V3_amiya_* 对象，
生成 V4_amiya_* 部件，输出 prts-lounge-r14.blend 与特写渲染。
"""
import bpy
import bmesh
import math
from mathutils import Vector, Matrix

ROOT = "/Users/daijinglin/Desktop/qq-agent-workspace"
OUT_BLEND = f"{ROOT}/renders/prts-lounge-r14.blend"

BASE = Vector((-1.95, 0.12, 0.0))       # 灯台中心
FLOOR_Z = 0.26                           # 灯台顶面
HEAD_C = Vector((-1.95, 0.12, 0.925))   # 头部中心
FACE_Y = -1.0                            # 面朝 -Y

# ---------- 通用工具 ----------

def clean_scene_prefix(prefix):
    for o in list(bpy.data.objects):
        if o.name.startswith(prefix):
            bpy.data.objects.remove(o, do_unlink=True)


def new_mesh_obj(name, bm, mat=None):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    if mat:
        ob.data.materials.append(mat)
    return ob


def shade_smooth(ob):
    for p in ob.data.polygons:
        p.use_smooth = True


def add_subsurf(ob, levels=2):
    m = ob.modifiers.new("Subsurf", "SUBSURF")
    m.levels = levels
    m.render_levels = levels
    return m


def mat_principled(name, color, rough=0.55, metal=0.0, sss=0.0, sheen=0.0):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metal
    if sss > 0:
        bsdf.inputs["Subsurface Weight"].default_value = sss
        bsdf.inputs["Subsurface Radius"].default_value = (0.12, 0.05, 0.03)
    if sheen > 0:
        bsdf.inputs["Sheen Weight"].default_value = sheen
    return m


def curve_obj(name, points, radius_fn, bevel_res=6, mat=None, taper=None):
    """points: 世界坐标列表；radius_fn(t) -> 半径，t 从 0 到 1。"""
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    cu.resolution_u = 12
    cu.bevel_depth = 1.0
    cu.bevel_resolution = bevel_res
    cu.use_fill_caps = True
    sp = cu.splines.new("BEZIER")
    sp.bezier_points.add(len(points) - 1)
    for i, p in enumerate(points):
        bp = sp.bezier_points[i]
        bp.co = p
        bp.handle_left_type = "AUTO"
        bp.handle_right_type = "AUTO"
        t = i / max(len(points) - 1, 1)
        bp.radius = radius_fn(t)
    ob = bpy.data.objects.new(name, cu)
    bpy.context.collection.objects.link(ob)
    if mat:
        ob.data.materials.append(mat)
    return ob


def convert_to_mesh(ob):
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.convert(target="MESH")
    ob = bpy.context.object
    ob.select_set(False)
    recalc_normals(ob)
    return ob


def recalc_normals(ob):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(ob.data)
    bm.free()


# ---------- 材质 ----------

M_SKIN = mat_principled("M4_amiya_skin", (0.86, 0.62, 0.48), rough=0.58, sss=0.08)
M_HAIR = mat_principled("M4_amiya_hair", (0.070, 0.034, 0.017), rough=0.48, sheen=0.06)
M_HAIR_DARK = mat_principled("M4_amiya_hair_dark", (0.038, 0.018, 0.009), rough=0.52)
M_EAR_IN = mat_principled("M4_amiya_ear_inner", (0.82, 0.55, 0.55), rough=0.55, sss=0.15)
M_COAT = mat_principled("M4_amiya_coat", (0.055, 0.10, 0.19), rough=0.62, sheen=0.35)
M_COAT_LIGHT = mat_principled("M4_amiya_coat_light", (0.09, 0.16, 0.29), rough=0.6, sheen=0.3)
M_SHIRT = mat_principled("M4_amiya_shirt", (0.90, 0.90, 0.88), rough=0.65, sheen=0.2)
M_TIE_DARK = mat_principled("M4_amiya_tie_dark", (0.05, 0.05, 0.055), rough=0.6)
M_TIE_LIGHT = mat_principled("M4_amiya_tie_light", (0.82, 0.82, 0.80), rough=0.6)
M_SKIRT = mat_principled("M4_amiya_skirt", (0.10, 0.10, 0.11), rough=0.7, sheen=0.25)
M_BOOT = mat_principled("M4_amiya_boot", (0.13, 0.07, 0.04), rough=0.42)
M_BOOT_SOLE = mat_principled("M4_amiya_boot_sole", (0.045, 0.04, 0.035), rough=0.8)
M_SOCK = mat_principled("M4_amiya_sock", (0.10, 0.10, 0.11), rough=0.75, sheen=0.2)
M_METAL = mat_principled("M4_amiya_metal", (0.66, 0.46, 0.16), rough=0.28, metal=1.0)
M_EYE_WHITE = mat_principled("M4_amiya_eye_white", (0.96, 0.96, 0.95), rough=0.22)
M_IRIS = mat_principled("M4_amiya_iris", (0.20, 0.50, 0.72), rough=0.18)
M_PUPIL = mat_principled("M4_amiya_pupil", (0.02, 0.05, 0.09), rough=0.15)
M_HILITE = mat_principled("M4_amiya_hilite", (1.0, 1.0, 1.0), rough=0.05)
M_LASH = mat_principled("M4_amiya_lash", (0.03, 0.02, 0.02), rough=0.5)
M_BLUSH = mat_principled("M4_amiya_blush", (0.95, 0.62, 0.60), rough=0.6)
M_GREEN = mat_principled("M4_amiya_green", (0.35, 0.75, 0.30), rough=0.5)

# ---------- 头部 ----------

def make_head():
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=40, radius=1.0)
    for v in bm.verts:
        x, y, z = v.co
        # Q 版头：略扁、脸颊饱满、下巴收窄
        sx, sy, sz = 0.152, 0.150, 0.148
        v.co = Vector((x * sx, y * sy, z * sz))
        # 下巴收窄（Q 版保留圆脸颊，只轻微收）
        if v.co.z < -0.02:
            t = min((-v.co.z - 0.02) / 0.13, 1.0)
            v.co.x *= (1.0 - 0.18 * t * t)
            v.co.y *= (1.0 - 0.12 * t * t)
        # 额头略平
        if v.co.z > 0.06:
            v.co.y *= 0.96
        # 眼窝：局部向内凹，制造面部结构
        for ec in (Vector((-0.060, -0.128, 0.014)), Vector((0.060, -0.128, 0.014))):
            d = (v.co - ec).length
            if d < 0.058:
                w = (1.0 - d / 0.058) ** 2
                v.co.y += 0.009 * w
        # 下巴轻微前凸
        if -0.145 < v.co.z < -0.085 and v.co.y < -0.06:
            w = math.exp(-((v.co.z + 0.115) ** 2) / 0.0006)
            v.co.y -= 0.006 * w
        # 颧骨饱满
        for cc in (Vector((-0.095, -0.085, -0.020)), Vector((0.095, -0.085, -0.020))):
            d = (v.co - cc).length
            if d < 0.065:
                w = (1.0 - d / 0.065) ** 2
                v.co.y -= 0.004 * w
    me = bpy.data.meshes.new("V4_amiya_Head")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("V4_amiya_Head", me)
    bpy.context.collection.objects.link(ob)
    ob.location = HEAD_C
    ob.data.materials.append(M_SKIN)
    shade_smooth(ob)
    add_subsurf(ob, 1)
    return ob


def make_eyes():
    """大眼：眼白、虹膜、瞳孔、双高光、上睫毛、腮红。"""
    objs = []
    for side, sx in (("L", -1), ("R", 1)):
        ex = HEAD_C.x + sx * 0.062
        ey = HEAD_C.y - 0.128
        ez = HEAD_C.z + 0.012
        # 眼白：扁球嵌入面部
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=20, radius=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.036, v.co.y * 0.017, v.co.z * 0.046))
        ob = new_mesh_obj(f"V4_amiya_EyeWhite_{side}", bm, M_EYE_WHITE)
        ob.location = (ex, ey, ez)
        shade_smooth(ob)
        objs.append(ob)
        # 虹膜
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.025, v.co.y * 0.010, v.co.z * 0.034))
        ob = new_mesh_obj(f"V4_amiya_Iris_{side}", bm, M_IRIS)
        ob.location = (ex, ey - 0.013, ez - 0.002)
        shade_smooth(ob)
        objs.append(ob)
        # 瞳孔
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=12, radius=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.015, v.co.y * 0.008, v.co.z * 0.024))
        ob = new_mesh_obj(f"V4_amiya_Pupil_{side}", bm, M_PUPIL)
        ob.location = (ex, ey - 0.020, ez - 0.003)
        shade_smooth(ob)
        objs.append(ob)
        # 高光（左上大、右下小）
        for tag, dx, dz, r in (("A", -0.008, 0.012, 0.0085), ("B", 0.010, -0.014, 0.0045)):
            bm = bmesh.new()
            bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=r)
            ob = new_mesh_obj(f"V4_amiya_Hilite_{side}{tag}", bm, M_HILITE)
            ob.location = (ex + dx, ey - 0.026, ez + dz)
            shade_smooth(ob)
            objs.append(ob)
        # 上睫毛：沿眼上缘的粗曲线
        pts = []
        for i in range(9):
            t = i / 8.0
            ang = math.radians(168 - 156 * t)  # 沿上眼睑从左到右
            px = ex + math.cos(ang) * 0.044
            pz = ez + math.sin(ang) * 0.048
            pts.append(Vector((px, ey - 0.020, pz)))
        lash = curve_obj(f"V4_amiya_Lash_{side}", pts, lambda t: 0.0022 * (0.55 + 0.45 * math.sin(math.pi * t)), mat=M_LASH)
        lash = convert_to_mesh(lash)
        objs.append(lash)
        # 腮红
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=12, radius=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.030, v.co.y * 0.006, v.co.z * 0.018))
        ob = new_mesh_obj(f"V4_amiya_Blush_{side}", bm, M_BLUSH)
        ob.location = (ex + sx * 0.012, ey + 0.006, ez - 0.044)
        shade_smooth(ob)
        objs.append(ob)
    # 鼻子：极小凸点
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=1.0)
    for v in bm.verts:
        v.co = Vector((v.co.x * 0.007, v.co.y * 0.005, v.co.z * 0.005))
    nose = new_mesh_obj("V4_amiya_Nose", bm, M_SKIN)
    nose.location = (HEAD_C.x, HEAD_C.y - 0.148, HEAD_C.z - 0.030)
    shade_smooth(nose)
    objs.append(nose)
    # 嘴：一条微弯凹线
    pts = [Vector((HEAD_C.x - 0.012 + 0.006 * i, HEAD_C.y - 0.152, HEAD_C.z - 0.055 - 0.0030 * math.sin(math.pi * i / 4))) for i in range(5)]
    mouth = curve_obj("V4_amiya_Mouth", pts, lambda t: 0.0028, mat=M_LASH)
    mouth = convert_to_mesh(mouth)
    objs.append(mouth)
    return objs


# ---------- 兔耳 ----------

def make_ears():
    objs = []
    for side, sx in (("L", -1), ("R", 1)):
        base = Vector((HEAD_C.x + sx * 0.040, HEAD_C.y + 0.020, HEAD_C.z + 0.145))
        mid = Vector((HEAD_C.x + sx * 0.078, HEAD_C.y + 0.012, HEAD_C.z + 0.275))
        tip = Vector((HEAD_C.x + sx * 0.108, HEAD_C.y - 0.005, HEAD_C.z + 0.400))
        pts = [base, mid, tip]
        ear = curve_obj(
            f"V4_amiya_Ear_{side}", pts,
            lambda t: 0.040 * (1 - t) ** 0.75 + 0.004,
            bevel_res=8, mat=M_HAIR,
        )
        ear = convert_to_mesh(ear)
        shade_smooth(ear)
        add_subsurf(ear, 1)
        objs.append(ear)
        # 内耳：稍小的浅粉曲面
        inner_pts = [
            Vector((base.x - sx * 0.003, base.y - 0.036, base.z + 0.012)),
            Vector((mid.x - sx * 0.002, mid.y - 0.034, mid.z)),
            Vector((tip.x - sx * 0.002, tip.y - 0.020, tip.z - 0.022)),
        ]
        inner = curve_obj(
            f"V4_amiya_EarInner_{side}", inner_pts,
            lambda t: 0.016 * (1 - t) ** 0.8 + 0.0015,
            bevel_res=6, mat=M_EAR_IN,
        )
        inner = convert_to_mesh(inner)
        shade_smooth(inner)
        objs.append(inner)
    return objs


# ---------- 头发 ----------

def make_hair():
    objs = []
    # 发帽：半球壳，覆盖头骨后上方
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=40, radius=1.0)
    # 前部只保留头顶，后部保留到后脑；发际线边缘由刘海覆盖
    kill = [v for v in bm.verts if (v.co.y < 0 and v.co.z < 0.08) or (v.co.z < -0.12)]
    bmesh.ops.delete(bm, geom=kill, context="VERTS")
    for v in bm.verts:
        v.co = Vector((v.co.x * 0.160, v.co.y * 0.158, v.co.z * 0.156))
        # 后脑更蓬
        if v.co.y > 0:
            v.co.y *= 1.10
    ob = new_mesh_obj("V4_amiya_HairCap", bm, M_HAIR)
    ob.location = HEAD_C
    shade_smooth(ob)
    add_subsurf(ob, 1)
    objs.append(ob)

    # 刘海：7 片独立锥形发片，互相重叠，贴合额头
    def make_lock(name, cx, width, z_tip):
        bm = bmesh.new()
        nz = 10
        rows = []
        for j in range(nz):
            t = j / (nz - 1)
            half = width * 0.5 * (1.0 - 0.92 * t ** 1.15)
            z = 1.042 + (z_tip - 1.042) * t
            row = []
            for i in range(5):
                tx = (i / 4.0 - 0.5) * 2.0
                x = cx + tx * half
                denom = max(0.05, 1.0 - (x / 0.166) ** 2)
                y = HEAD_C.y - 0.164 * math.sqrt(denom) - 0.009 * math.sin(t * math.pi)
                row.append(bm.verts.new(Vector((HEAD_C.x + x, y, z))))
            rows.append(row)
        for j in range(nz - 1):
            for i in range(4):
                bm.faces.new((rows[j][i], rows[j][i + 1], rows[j + 1][i + 1], rows[j + 1][i]))
        ob = new_mesh_obj(name, bm, M_HAIR)
        sol = ob.modifiers.new("Solidify", "SOLIDIFY")
        sol.thickness = 0.009
        sol.offset = 0.0
        shade_smooth(ob)
        add_subsurf(ob, 1)
        return ob

    for i in range(7):
        t = i / 6.0
        cx = (t - 0.5) * 0.225
        z_tip = 0.918 + 0.030 * abs(t - 0.5) * 2.0 + 0.004 * math.sin(i * 1.7)
        objs.append(make_lock(f"V4_amiya_Bang_{i}", cx, 0.058, z_tip))

    # 侧发：两束长发垂到肩下
    for side, sx in (("L", -1), ("R", 1)):
        pts = [
            Vector((HEAD_C.x + sx * 0.140, HEAD_C.y - 0.045, HEAD_C.z + 0.020)),
            Vector((HEAD_C.x + sx * 0.165, HEAD_C.y - 0.030, HEAD_C.z - 0.120)),
            Vector((HEAD_C.x + sx * 0.150, HEAD_C.y + 0.000, HEAD_C.z - 0.235)),
        ]
        strand = curve_obj(
            f"V4_amiya_SideHair_{side}", pts,
            lambda t: 0.026 * (1 - t) ** 0.6 + 0.002,
            bevel_res=8, mat=M_HAIR,
        )
        strand = convert_to_mesh(strand)
        shade_smooth(strand)
        objs.append(strand)

    # 后发：大块发片，带发丝凹槽
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=12, y_segments=10, size=1.0)
    for v in bm.verts:
        x, y = v.co.x, v.co.y
        v.co = Vector((
            HEAD_C.x + x * 0.135,
            HEAD_C.y + 0.135 + y * 0.035 + 0.02 * math.cos(x * 3.0),
            HEAD_C.z + 0.05 - (y + 1) * 0.14 + 0.008 * math.sin(x * 8.0),
        ))
    back = new_mesh_obj("V4_amiya_HairBack", bm, M_HAIR)
    shade_smooth(back)
    add_subsurf(back, 1)
    objs.append(back)

    # 发梢细节：6 束细发丝
    for i in range(6):
        sx = -1 if i % 2 == 0 else 1
        px = HEAD_C.x + sx * (0.03 + 0.025 * (i // 2))
        pts = [
            Vector((px, HEAD_C.y + 0.160, HEAD_C.z + 0.02)),
            Vector((px + sx * 0.01, HEAD_C.y + 0.175, HEAD_C.z - 0.12)),
            Vector((px + sx * 0.02, HEAD_C.y + 0.165, HEAD_C.z - 0.22)),
        ]
        strand = curve_obj(
            f"V4_amiya_BackStrand_{i}", pts,
            lambda t: 0.012 * (1 - t) ** 0.7 + 0.001,
            bevel_res=5, mat=M_HAIR_DARK,
        )
        strand = convert_to_mesh(strand)
        shade_smooth(strand)
        objs.append(strand)
    return objs


# ---------- 身体 ----------

def make_body():
    objs = []
    # 躯干：纺锤形，收腰
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=28, radius=1.0)
    for v in bm.verts:
        x, y, z = v.co
        # z 从 -1 到 1 映射到躯干高度 0.30
        hz = 0.53 + (z + 1) * 0.15
        # 腰部收窄
        waist = 1.0 - 0.18 * math.exp(-((z + 0.15) ** 2) / 0.12)
        w = 0.105 * waist * (0.85 + 0.15 * (1 - abs(z)))
        v.co = Vector((x * w, y * w * 0.72, hz))
    torso = new_mesh_obj("V4_amiya_Torso", bm, M_SHIRT)
    shade_smooth(torso)
    add_subsurf(torso, 1)
    objs.append(torso)

    # 脖子
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=0.046, radius2=0.040, depth=0.06)
    for v in bm.verts:
        v.co.z += 0.815
    neck = new_mesh_obj("V4_amiya_Neck", bm, M_SKIN)
    shade_smooth(neck)
    objs.append(neck)

    # 手臂：从肩膀到手腕的曲线
    for side, sx in (("L", -1), ("R", 1)):
        pts = [
            Vector((HEAD_C.x + sx * 0.085, HEAD_C.y + 0.005, 0.790)),
            Vector((HEAD_C.x + sx * 0.125, HEAD_C.y - 0.005, 0.680)),
            Vector((HEAD_C.x + sx * 0.115, HEAD_C.y - 0.010, 0.575)),
        ]
        arm = curve_obj(
            f"V4_amiya_Arm_{side}", pts,
            lambda t: 0.038 * (1 - 0.25 * t),
            bevel_res=8, mat=M_COAT_LIGHT,
        )
        arm = convert_to_mesh(arm)
        shade_smooth(arm)
        objs.append(arm)
        # 手：Q 版圆手 + 拇指
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=16, radius=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.044, v.co.y * 0.040, v.co.z * 0.048))
        hand = new_mesh_obj(f"V4_amiya_Hand_{side}", bm, M_SKIN)
        hand.location = (HEAD_C.x + sx * 0.122, HEAD_C.y - 0.012, 0.545)
        shade_smooth(hand)
        objs.append(hand)
        # 拇指
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.016, v.co.y * 0.014, v.co.z * 0.022))
        thumb = new_mesh_obj(f"V4_amiya_Thumb_{side}", bm, M_SKIN)
        thumb.location = (HEAD_C.x + sx * 0.095, HEAD_C.y - 0.030, 0.552)
        shade_smooth(thumb)
        objs.append(thumb)
    return objs


# ---------- 服装 ----------

def make_outfit():
    objs = []
    # 衬衫：躯干外壳（白色），比躯干略大
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=28, radius=1.0)
    for v in bm.verts:
        x, y, z = v.co
        hz = 0.555 + (z + 1) * 0.135
        waist = 1.0 - 0.16 * math.exp(-((z + 0.15) ** 2) / 0.12)
        w = 0.112 * waist * (0.85 + 0.15 * (1 - abs(z)))
        v.co = Vector((x * w, y * w * 0.74, hz))
    shirt = new_mesh_obj("V4_amiya_Shirt", bm, M_SHIRT)
    shade_smooth(shirt)
    add_subsurf(shirt, 1)
    objs.append(shirt)

    # 领带：连续条带，黑白相间
    for i in range(7):
        z0 = 0.768 - i * 0.022
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.020, v.co.y * 0.008, v.co.z * 0.0115))
        tie = new_mesh_obj(f"V4_amiya_Tie_{i}", bm, M_TIE_DARK if i % 2 == 0 else M_TIE_LIGHT)
        tie.location = (HEAD_C.x, HEAD_C.y - 0.094, z0)
        objs.append(tie)
    # 领带结
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co = Vector((v.co.x * 0.020, v.co.y * 0.012, v.co.z * 0.014))
    knot = new_mesh_obj("V4_amiya_TieKnot", bm, M_TIE_DARK)
    knot.location = (HEAD_C.x, HEAD_C.y - 0.098, 0.783)
    objs.append(knot)

    # 外套：左右两片垂坠曲面 + 后片，带 Solidify 和褶皱
    def coat_panel(name, sx):
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=10, y_segments=14, size=1.0)
        for v in bm.verts:
            gx, gy = v.co.x, v.co.y  # -1..1
            t = (gy + 1) * 0.5        # 0 上 1 下
            width = 0.038 + 0.028 * t
            px = HEAD_C.x + sx * (0.062 + gx * width)
            py = HEAD_C.y - 0.006 + 0.024 * t - 0.030 * gx * gx
            pz = 0.790 - t * 0.250
            # 布料褶皱
            fold = 0.005 * math.sin(gx * 9.0 + t * 3.0) * t
            py += fold
            px += 0.003 * math.sin(t * 7.0 + gx * 2.0) * t
            v.co = Vector((px, py, pz))
        ob = new_mesh_obj(name, bm, M_COAT)
        sol = ob.modifiers.new("Solidify", "SOLIDIFY")
        sol.thickness = 0.008
        sol.offset = 0.0
        shade_smooth(ob)
        add_subsurf(ob, 1)
        return ob

    objs.append(coat_panel("V4_amiya_Coat_L", -1))
    objs.append(coat_panel("V4_amiya_Coat_R", 1))

    # 外套后片
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=14, y_segments=14, size=1.0)
    for v in bm.verts:
        gx, gy = v.co.x, v.co.y
        t = (gy + 1) * 0.5
        v.co = Vector((
            HEAD_C.x + gx * (0.120 + 0.030 * t),
            HEAD_C.y + 0.055 + 0.020 * t,
            0.800 - t * 0.330,
        ))
    back = new_mesh_obj("V4_amiya_CoatBack", bm, M_COAT)
    sol = back.modifiers.new("Solidify", "SOLIDIFY")
    sol.thickness = 0.008
    shade_smooth(back)
    add_subsurf(back, 1)
    objs.append(back)

    # 翻领：两条从领口向下翻的曲面
    for side, sx in (("L", -1), ("R", 1)):
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=4, y_segments=6, size=1.0)
        for v in bm.verts:
            gx, gy = v.co.x, v.co.y
            t = (gy + 1) * 0.5
            v.co = Vector((
                HEAD_C.x + sx * (0.034 + gx * 0.016) + sx * t * 0.012,
                HEAD_C.y - 0.066 - t * 0.008,
                0.805 - t * 0.065,
            ))
        lapel = new_mesh_obj(f"V4_amiya_Lapel_{side}", bm, M_COAT_LIGHT)
        sol = lapel.modifiers.new("Solidify", "SOLIDIFY")
        sol.thickness = 0.006
        shade_smooth(lapel)
        objs.append(lapel)

    # 腰带
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=32, radius1=0.098, radius2=0.098, depth=0.022)
    for v in bm.verts:
        v.co.z += 0.545
    belt = new_mesh_obj("V4_amiya_Belt", bm, M_TIE_DARK)
    shade_smooth(belt)
    objs.append(belt)

    # 绿色挂绳
    pts = [
        Vector((HEAD_C.x - 0.070, HEAD_C.y - 0.060, 0.560)),
        Vector((HEAD_C.x, HEAD_C.y - 0.085, 0.545)),
        Vector((HEAD_C.x + 0.070, HEAD_C.y - 0.060, 0.560)),
    ]
    cord = curve_obj("V4_amiya_GreenCord", pts, lambda t: 0.0045, mat=M_GREEN)
    cord = convert_to_mesh(cord)
    shade_smooth(cord)
    objs.append(cord)

    # 裙子：圆台 + 百褶
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=False, segments=64, radius1=0.118, radius2=0.175, depth=0.160)
    for v in bm.verts:
        ang = math.atan2(v.co.y, v.co.x)
        pleat = 1.0 + 0.045 * math.cos(ang * 24)
        v.co.x *= pleat
        v.co.y *= pleat
        # 下摆波浪
        if v.co.z < -0.055:
            v.co.z += 0.014 * math.sin(ang * 12)
        v.co.z += 0.480
    skirt = new_mesh_obj("V4_amiya_Skirt", bm, M_SKIRT)
    sol = skirt.modifiers.new("Solidify", "SOLIDIFY")
    sol.thickness = 0.006
    shade_smooth(skirt)
    add_subsurf(skirt, 1)
    objs.append(skirt)
    return objs


# ---------- 腿与靴 ----------

def make_legs_and_boots():
    objs = []
    for side, sx in (("L", -1), ("R", 1)):
        lx = HEAD_C.x + sx * 0.052
        # 大腿/小腿：Q 版短粗腿
        bm = bmesh.new()
        bmesh.ops.create_cone(bm, cap_ends=True, segments=20, radius1=0.042, radius2=0.038, depth=0.135)
        for v in bm.verts:
            v.co.z += 0.365
        leg = new_mesh_obj(f"V4_amiya_Leg_{side}", bm, M_SKIN)
        leg.location = (lx, HEAD_C.y - 0.005, 0)
        shade_smooth(leg)
        objs.append(leg)
        # 袜子
        bm = bmesh.new()
        bmesh.ops.create_cone(bm, cap_ends=True, segments=20, radius1=0.040, radius2=0.038, depth=0.045)
        for v in bm.verts:
            v.co.z += 0.333
        sock = new_mesh_obj(f"V4_amiya_Sock_{side}", bm, M_SOCK)
        sock.location = (lx, HEAD_C.y - 0.005, 0)
        shade_smooth(sock)
        objs.append(sock)
        # 靴筒
        bm = bmesh.new()
        bmesh.ops.create_cone(bm, cap_ends=True, segments=24, radius1=0.050, radius2=0.046, depth=0.110)
        for v in bm.verts:
            v.co.z += 0.300
        boot = new_mesh_obj(f"V4_amiya_Boot_{side}", bm, M_BOOT)
        boot.location = (lx, HEAD_C.y - 0.005, 0)
        shade_smooth(boot)
        objs.append(boot)
        # 鞋头：前伸的椭球
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=16, radius=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.050, v.co.y * 0.080, v.co.z * 0.040))
        toe = new_mesh_obj(f"V4_amiya_Toe_{side}", bm, M_BOOT)
        toe.location = (lx, HEAD_C.y - 0.050, 0.290)
        shade_smooth(toe)
        objs.append(toe)
        # 鞋底
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        for v in bm.verts:
            v.co = Vector((v.co.x * 0.052, v.co.y * 0.088, v.co.z * 0.012))
        sole = new_mesh_obj(f"V4_amiya_Sole_{side}", bm, M_BOOT_SOLE)
        sole.location = (lx, HEAD_C.y - 0.042, 0.263)
        objs.append(sole)
        # 鞋带：三条横线
        for i in range(3):
            z = 0.300 + i * 0.020
            pts = [
                Vector((lx - 0.036, HEAD_C.y - 0.070, z)),
                Vector((lx, HEAD_C.y - 0.080, z + 0.002)),
                Vector((lx + 0.036, HEAD_C.y - 0.070, z)),
            ]
            lace = curve_obj(f"V4_amiya_Lace_{side}{i}", pts, lambda t: 0.0032, mat=M_BOOT_SOLE)
            lace = convert_to_mesh(lace)
            shade_smooth(lace)
            objs.append(lace)
        # 金属扣
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=0.007)
        buckle = new_mesh_obj(f"V4_amiya_Buckle_{side}", bm, M_METAL)
        buckle.location = (lx + sx * 0.042, HEAD_C.y - 0.058, 0.345)
        shade_smooth(buckle)
        objs.append(buckle)
    return objs


# ---------- 执行 ----------

def main():
    clean_scene_prefix("V4_amiya_")
    for o in bpy.data.objects:
        if o.name.startswith("V3_amiya_"):
            o.hide_render = True
            o.hide_viewport = True

    parts = []
    parts.append(make_head())
    parts += make_eyes()
    parts += make_ears()
    parts += make_hair()
    parts += make_body()
    parts += make_outfit()
    parts += make_legs_and_boots()

    # 统一修正法线，避免曲线倒角或删面后出现内翻法线
    for o in parts:
        if o.type == "MESH":
            recalc_normals(o)

    # 统一收进任务集合
    coll = bpy.data.collections.get("AIBP_prts-lounge-blockout-20261020-170834-7aad23")
    if coll:
        for o in parts:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            coll.objects.link(o)

    bpy.ops.wm.save_as_mainfile(filepath=OUT_BLEND)
    print(f"AI3D_SAVED {OUT_BLEND}")
    print(f"AI3D_PARTS {len(parts)}")


main()
