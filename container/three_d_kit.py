"""Small Blender modelling API for explanatory objects, diagrams and mechanisms.

This module is imported inside Blender, never in the Discord process.
"""
import math
from pathlib import Path

import bpy
from mathutils import Vector

BLUE = (0.10, 0.48, 0.95)
CYAN = (0.10, 0.85, 0.95)
ORANGE = (1.0, 0.42, 0.10)
YELLOW = (1.0, 0.82, 0.15)
WHITE = (0.92, 0.95, 1.0)
GREEN = (0.18, 0.75, 0.45)
RED = (0.92, 0.13, 0.19)
_MODELS = {}


def material(name, color, metallic=0.0, roughness=0.45):
    key = name + str(tuple(color))
    mat = bpy.data.materials.get(key)
    if mat:
        return mat
    mat = bpy.data.materials.new(key)
    mat.diffuse_color = (*color[:3], color[3] if len(color) > 3 else 1)
    mat.use_nodes = True
    node = mat.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = mat.diffuse_color
    node.inputs["Metallic"].default_value = metallic
    node.inputs["Roughness"].default_value = roughness
    return mat


def finish(obj, name, color, smooth=False):
    obj.name = name
    obj.data.materials.append(material(name + " material", color))
    if smooth and obj.type == "MESH":
        for face in obj.data.polygons:
            face.use_smooth = True
    return obj


def box(name, location=(0, 0, 0), size=(1, 1, 1), color=BLUE, bevel=0.025):
    bpy.ops.mesh.primitive_cube_add(size=1, location=location)
    obj = finish(bpy.context.object, name, color)
    obj.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if bevel:
        mod = obj.modifiers.new("Soft edges", "BEVEL")
        mod.width, mod.segments = bevel, 2
        obj.modifiers.new("Weighted normals", "WEIGHTED_NORMAL")
    return obj


def sphere(name, location=(0, 0, 0), radius=0.2, color=ORANGE):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=radius, location=location)
    return finish(bpy.context.object, name, color, True)


def rod(name, start, end, radius=0.025, color=WHITE):
    start, end = Vector(start), Vector(end)
    direction = end - start
    bpy.ops.mesh.primitive_cylinder_add(vertices=20, radius=radius, depth=max(direction.length, 0.001), location=(start + end) / 2)
    obj = finish(bpy.context.object, name, color, True)
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = direction.to_track_quat("Z", "Y")
    return obj


def arrow(name, start, end, radius=0.025, color=YELLOW):
    direction = Vector(end) - Vector(start)
    length = max(direction.length, 0.001)
    unit = direction.normalized()
    tip_len = min(length * 0.25, radius * 5)
    stem = rod(name, start, Vector(end) - unit * tip_len, radius, color)
    bpy.ops.mesh.primitive_cone_add(vertices=20, radius1=radius * 2.6, radius2=0,
                                    depth=tip_len, location=Vector(end) - unit * tip_len / 2)
    tip = finish(bpy.context.object, name + " tip", color, True)
    tip.rotation_mode = "QUATERNION"
    tip.rotation_quaternion = direction.to_track_quat("Z", "Y")
    tip.parent = stem
    tip.matrix_parent_inverse = stem.matrix_world.inverted()
    return stem


def curve(name, points, radius=0.015, color=CYAN, closed=False):
    data = bpy.data.curves.new(name, "CURVE")
    data.dimensions = "3D"
    data.bevel_depth, data.bevel_resolution = radius, 2
    spline = data.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for vertex, point in zip(spline.points, points):
        vertex.co = (*point, 1)
    spline.use_cyclic_u = closed
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    return finish(obj, name, color)


def torus(name, location=(0, 0, 0), major=0.5, minor=0.05, color=BLUE):
    bpy.ops.mesh.primitive_torus_add(major_segments=48, minor_segments=12, location=location,
                                   major_radius=major, minor_radius=minor)
    return finish(bpy.context.object, name, color, True)


def mesh(name, vertices, faces, color=BLUE, smooth=False):
    data = bpy.data.meshes.new(name)
    data.from_pydata(vertices, [], faces)
    data.update()
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    return finish(obj, name, color, smooth)


def label(text, location, size=0.11, color=WHITE):
    data = bpy.data.curves.new("Label", "FONT")
    data.body, data.size, data.align_x = text, size, "CENTER"
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if Path(font).exists():
        data.font = bpy.data.fonts.load(font, check_existing=True)
    obj = bpy.data.objects.new("Label", data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (math.pi / 2, 0, 0)
    return finish(obj, "Label", color)


def normalize(objects, location, height=None, max_size=None):
    meshes = [obj for obj in objects if obj.type == "MESH"]
    if not meshes:
        raise ValueError("model has no mesh")
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    low = Vector(tuple(min(point[i] for point in points) for i in range(3)))
    high = Vector(tuple(max(point[i] for point in points) for i in range(3)))
    root = bpy.data.objects.new("Model root", None)
    bpy.context.collection.objects.link(root)
    selected = set(objects)
    for obj in objects:
        if obj.parent not in selected:
            world = obj.matrix_world.copy()
            obj.parent = root
            obj.matrix_world = world
    scale = height / max(high.z - low.z, 0.001) if height else max_size / max(max(high - low), 0.001)
    center = Vector(((low.x + high.x) / 2, (low.y + high.y) / 2, low.z))
    root.scale = (scale,) * 3
    root.location = Vector(location) - center * scale
    return root


def model(asset_id, location=(0, 0, 0.85), size=1.1):
    """Import an approved operator-supplied GLB/GLTF model, preserving real geometry and materials."""
    if asset_id not in _MODELS:
        raise ValueError("unknown 3D model: " + asset_id)
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=_MODELS[asset_id]["path"])
    objects = list(set(bpy.data.objects) - before)
    return normalize(objects, location, max_size=size)
