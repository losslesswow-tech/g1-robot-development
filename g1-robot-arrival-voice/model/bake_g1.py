#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bake the official Unitree G1 URDF (g1_29dof_rev_1_0.urdf) into a single
world-space binary STL, with all revolute joints at their zero (home) pose.

Output: g1_model.stl  (binary STL, meters, pelvis at origin, +X forward, +Z up)
"""
import os
import struct
import math
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
URDF = os.path.join(HERE, "g1_29dof_rev_1_0.urdf")
MESH_DIR = os.path.join(HERE, "meshes")
OUT = os.path.join(HERE, "g1_model.stl")


def rpy_to_matrix(r, p, y):
    """URDF convention: R = Rz(yaw) * Ry(pitch) * Rx(roll)."""
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    Rx = [[1, 0, 0], [0, cr, -sr], [0, sr, cr]]
    Ry = [[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]]
    Rz = [[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]
    # Rz * Ry * Rx
    def matmul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return matmul(matmul(Rz, Ry), Rx)


def parse_origin(elem):
    xyz = [0.0, 0.0, 0.0]
    rpy = [0.0, 0.0, 0.0]
    o = elem.find("origin")
    if o is not None:
        if o.get("xyz"):
            xyz = [float(v) for v in o.get("xyz").split()]
        if o.get("rpy"):
            rpy = [float(v) for v in o.get("rpy").split()]
    return xyz, rpy


def read_binary_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    # header 80 + uint32 count
    if len(data) < 84:
        return []
    # try binary first
    n = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + n * 50
    if expected == len(data):
        tris = []
        for i in range(n):
            base = 84 + i * 50
            vals = struct.unpack_from("<9f", data, base + 12)  # 3 vertices * 3
            tris.append(vals)
        return tris
    # ASCII fallback
    txt = data.decode("latin-1", "replace")
    tris = []
    verts = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(verts) == 3:
                tris.append(verts[0] + verts[1] + verts[2])
                verts = []
    return tris


def apply_transform(xyz, rpy, point):
    R = rpy_to_matrix(*rpy)
    return [
        R[0][0] * point[0] + R[0][1] * point[1] + R[0][2] * point[2] + xyz[0],
        R[1][0] * point[0] + R[1][1] * point[1] + R[1][2] * point[2] + xyz[1],
        R[2][0] * point[0] + R[2][1] * point[1] + R[2][2] * point[2] + xyz[2],
    ]


def main():
    tree = ET.parse(URDF)
    root = tree.getroot()

    links = {}
    for link in root.findall("link"):
        links[link.get("name")] = link

    joints = {}
    children = {}  # child -> (parent, joint)
    for joint in root.findall("joint"):
        jname = joint.get("name")
        jtype = joint.get("type")
        parent = joint.find("parent").get("link")
        child = joint.find("child").get("link")
        xyz, rpy = parse_origin(joint)
        joints[jname] = dict(parent=parent, child=child, type=jtype, xyz=xyz, rpy=rpy)
        children[child] = (parent, jname)

    # Build world transform for each link (pelvis = root, identity).
    # T_link maps link-frame point -> pelvis(world) frame.
    transforms = {"pelvis": ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])}  # xyz, rpy (composed)

    def get_transform(link):
        if link in transforms:
            return transforms[link]
        parent, jname = children[link]
        j = joints[jname]
        pxyz, prpy = get_transform(parent)
        # compose: world = T_parent ∘ T_joint
        # T_joint maps child->parent: point_parent = R_j * p + t_j
        # world point = T_parent(T_joint(p))
        # xyz world = R_parent * t_j + t_parent
        R_parent = rpy_to_matrix(*prpy)
        jx, jy, jz = j["xyz"]
        wx = (R_parent[0][0] * jx + R_parent[0][1] * jy + R_parent[0][2] * jz) + pxyz[0]
        wy = (R_parent[1][0] * jx + R_parent[1][1] * jy + R_parent[1][2] * jz) + pxyz[1]
        wz = (R_parent[2][0] * jx + R_parent[2][1] * jy + R_parent[2][2] * jz) + pxyz[2]
        # rpy world = R_parent * R_j  -> convert back to rpy
        Rj = rpy_to_matrix(*j["rpy"])
        Rw = matmul(R_parent, Rj)
        wrpy = matrix_to_rpy(Rw)
        transforms[link] = ([wx, wy, wz], wrpy)
        return transforms[link]

    # gather all meshes
    all_verts = []
    stats = []
    for link in root.findall("link"):
        name = link.get("name")
        visual = link.find("visual")
        if visual is None:
            continue
        geom = visual.find("geometry")
        if geom is None:
            continue
        mesh = geom.find("mesh")
        if mesh is None:
            continue
        fname = mesh.get("filename")
        fname = fname.replace("meshes/", "").replace("package://", "")
        path = os.path.join(MESH_DIR, fname)
        if not os.path.exists(path):
            print("MISSING MESH:", name, fname)
            continue
        viz_xyz, viz_rpy = parse_origin(visual)
        lxyz, lrpy = get_transform(name)
        # link world transform
        tris = read_binary_stl(path)
        if not tris:
            print("EMPTY MESH:", name, fname)
            continue
        count = 0
        for tri in tris:
            # 9 floats: 3 vertices
            for k in range(3):
                p = [tri[k * 3], tri[k * 3 + 1], tri[k * 3 + 2]]
                # visual origin (identity in this urdf, but apply anyway)
                p = apply_transform(viz_xyz, viz_rpy, p)
                # link world transform
                p = apply_transform(lxyz, lrpy, p)
                all_verts.append(p)
                count += 1
        stats.append((name, len(tris)))
        print("OK %-28s %7d tris  %s" % (name, len(tris), fname))

    print("---")
    print("total triangles:", len(all_verts) // 3)
    # compute bounds
    if all_verts:
        xs = [v[0] for v in all_verts]
        ys = [v[1] for v in all_verts]
        zs = [v[2] for v in all_verts]
        print("bounds x: %.3f .. %.3f" % (min(xs), max(xs)))
        print("bounds y: %.3f .. %.3f" % (min(ys), max(ys)))
        print("bounds z: %.3f .. %.3f" % (min(zs), max(zs)))

    # write binary STL
    ntri = len(all_verts) // 3
    with open(OUT, "wb") as f:
        f.write(b"\0" * 80)
        f.write(struct.pack("<I", ntri))
        for i in range(ntri):
            v0, v1, v2 = all_verts[i * 3], all_verts[i * 3 + 1], all_verts[i * 3 + 2]
            # face normal
            e1 = [v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]]
            e2 = [v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]]
            nx = e1[1] * e2[2] - e1[2] * e2[1]
            ny = e1[2] * e2[0] - e1[0] * e2[2]
            nz = e1[0] * e2[1] - e1[1] * e2[0]
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            nx, ny, nz = nx / ln, ny / ln, nz / ln
            f.write(struct.pack("<3f", nx, ny, nz))
            for v in (v0, v1, v2):
                f.write(struct.pack("<3f", v[0], v[1], v[2]))
            f.write(struct.pack("<H", 0))
    size = os.path.getsize(OUT)
    print("wrote %s  (%d bytes, %.2f MB)" % (OUT, size, size / 1e6))


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def matrix_to_rpy(R):
    # R = Rz(y) Ry(p) Rx(r)
    # handle gimbal lock-ish
    sp = -R[2][0]
    if abs(sp) >= 1.0 - 1e-9:
        p = math.copysign(math.pi / 2, sp)
        y = math.atan2(-R[0][1], R[1][1])
        r = 0.0
    else:
        p = math.asin(sp)
        y = math.atan2(R[1][0], R[0][0])
        r = math.atan2(R[2][1], R[2][2])
    return [r, p, y]


if __name__ == "__main__":
    main()
