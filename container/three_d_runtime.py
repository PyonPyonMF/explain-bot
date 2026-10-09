"""Trusted Blender scene setup and frame renderer; executes as the sandbox user."""
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

import bpy
from mathutils import Vector
sys.path.insert(0, str(Path(__file__).resolve().parent))
import three_d_kit as K

TEACHER_POSITION = (-1.25, 0.55, 0)
BOARD_POSITION = (1.0, 1.64, 1.92)


def collection(name):
    col = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(col)
    bpy.context.view_layer.active_layer_collection = bpy.context.view_layer.layer_collection.children[col.name]
    return col


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def camera_shot(kind, teacher):
    """Eye-level close-ups and explanatory inserts, rather than an orthographic overview."""
    camera = bpy.context.scene.camera
    if kind == "teacher" and not teacher:
        kind = "object"
    if kind == "teacher":
        head = Vector(TEACHER_POSITION) + Vector((0, -0.04, 1.50))
        camera.location = head + Vector((0.55, -2.70, 0.05))
        target = head + Vector((0.20, 0.0, -0.12))
        focus, lens, aperture = head, 55, 4.0
    elif kind == "board":
        target = Vector(BOARD_POSITION)
        camera.location = target + Vector((-0.15, -4.7, 0.05))
        focus, lens, aperture = target, 42, 12.0
    else:
        kind = "object"
        target = Vector((0.30, -0.10, 1.36))
        camera.location = target + Vector((1.20, -2.90, 0.65))
        focus, lens, aperture = target, 46, 5.6
    camera.data.type, camera.data.lens = "PERSP", lens
    camera.data.dof.use_dof = bool(bpy.context.scene.get("quality_dof")) and kind != "board"
    camera.data.dof.focus_distance = (camera.location - focus).length
    camera.data.dof.aperture_fstop = aperture
    camera.data.dof.aperture_blades = 6
    bpy.context.scene.display.shading.use_dof = False
    look_at(camera, target)
    return kind


def classroom(spec):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = spec.get("engine", "BLENDER_EEVEE_NEXT")
    if scene.render.engine == "BLENDER_WORKBENCH":
        shade = scene.display.shading
        shade.light, shade.color_type = "STUDIO", "TEXTURE"
        shade.show_shadows, shade.show_cavity = False, True
        shade.cavity_type = "SCREEN"
        shade.background_type, shade.background_color = "WORLD", (0.06, 0.08, 0.12)
        scene.display.render_aa = "FXAA"
    scene.render.resolution_x = spec.get("width", 960)
    scene.render.resolution_y = spec.get("height", 540)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.film_transparent = False
    scene.render.fps = spec.get("fps", 12)
    scene["quality_dof"] = bool(spec.get("quality"))
    scene.render.threads_mode, scene.render.threads = "FIXED", 2
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = spec.get("samples", 16)
    scene.world.color = (0.26, 0.31, 0.38)
    scene.view_settings.view_transform = "Standard"
    if scene.render.engine == "BLENDER_WORKBENCH":
        scene.view_settings.exposure = 0.7
    collection("Classroom")
    # Warm Japanese schoolroom: timber flooring/wainscot, tall windows, chalkboard and wooden desks.
    K.box("Floor foundation", (0, -1.4, -0.09), (8, 8, 0.18), (0.34, 0.21, 0.11))
    for i in range(28):
        tone = i % 4 * 0.025
        K.box("Timber floorboard", (-3.85 + i * 0.28, -1.4, 0.015), (0.274, 8, 0.035),
              (0.46 + tone, 0.29 + tone * 0.6, 0.14 + tone * 0.3), bevel=0.004)
    K.box("Warm plaster", (0.4, 1.88, 1.65), (8, 0.15, 3.3), (0.76, 0.72, 0.59))
    K.box("Timber wainscot", (0.4, 1.78, 0.46), (8, 0.05, 0.92), (0.39, 0.24, 0.12))
    K.box("Wall rail", (0.4, 1.70, 0.94), (8, 0.07, 0.055), (0.23, 0.13, 0.065))
    K.box("Window sill wall", (-2.55, -0.8, 0.40), (0.16, 5.5, 0.80), (0.76, 0.72, 0.59))
    K.box("Window lintel", (-2.55, -0.8, 3.05), (0.16, 5.5, 0.40), (0.76, 0.72, 0.59))
    for y in (-2.55, -1.25, 0.05, 1.35):
        K.box("Window daylight", (-2.57, y, 1.91), (0.035, 1.22, 2.12), (0.69, 0.85, 0.94), bevel=0)
        for edge in (-0.62, 0.62):
            K.box("Window upright", (-2.49, y + edge, 1.91), (0.09, 0.045, 2.16), (0.90, 0.89, 0.80))
        for z in (0.86, 1.91, 2.97):
            K.box("Window crossbar", (-2.49, y, z), (0.09, 1.26, 0.045), (0.90, 0.89, 0.80))
    K.box("Window sill", (-2.40, -0.7, 0.83), (0.36, 5.5, 0.07), (0.50, 0.33, 0.17))
    K.box("Board frame", (1.0, 1.70, 1.92), (3.95, 0.10, 2.00), (0.31, 0.20, 0.10))
    K.box("Board", BOARD_POSITION, (3.8, 0.04, 1.85), (0.035, 0.11, 0.085), bevel=0)
    K.box("Chalk tray", (1.0, 1.48, 0.94), (3.95, 0.23, 0.07), (0.27, 0.18, 0.11))
    K.box("Board eraser", (2.3, 1.45, 1.02), (0.24, 0.11, 0.08), (0.29, 0.19, 0.13))
    for x in (0.1, 0.23, 0.36):
        K.rod("Chalk", (x, 1.42, 1.005), (x + 0.10, 1.42, 1.005), 0.009, (0.90, 0.88, 0.77))
    # A plane facing the camera has predictable UVs for legible board text.
    bpy.ops.mesh.primitive_plane_add(size=2, location=(1.0, 1.61, 1.92), rotation=(math.pi / 2, 0, 0))
    screen = bpy.context.object
    screen.name, screen.scale = "Board content", (1.87, 0.90, 1)
    mat = bpy.data.materials.new("Board display")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    tex = nodes.new("ShaderNodeTexImage")
    emit = nodes.new("ShaderNodeEmission")
    output = nodes.new("ShaderNodeOutputMaterial")
    mat.node_tree.links.new(tex.outputs["Color"], emit.inputs["Color"])
    mat.node_tree.links.new(emit.outputs[0], output.inputs["Surface"])
    nodes.active = tex
    screen.data.materials.append(mat)
    K.box("Demonstration table", (0.3, -0.2, 0.74), (2.5, 1.5, 0.12), (0.52, 0.34, 0.16))
    for x in (-0.65, 1.25):
        for y in (-0.75, 0.35):
            K.box("Table leg", (x, y, 0.35), (0.10, 0.10, 0.70), (0.29, 0.30, 0.29))
    for x in (-1.45, 0.3, 2.05):
        for y in (-2.25, -3.8):
            K.box("Student desk", (x, y, 0.67), (0.95, 0.65, 0.07), (0.58, 0.40, 0.20))
            for dx in (-0.37, 0.37):
                K.box("Desk leg", (x + dx, y, 0.31), (0.045, 0.48, 0.62), (0.32, 0.35, 0.34))
            K.box("Chair seat", (x, y - 0.6, 0.40), (0.48, 0.45, 0.06), (0.51, 0.30, 0.13))
            K.box("Chair back", (x, y - 0.83, 0.69), (0.48, 0.05, 0.37), (0.51, 0.30, 0.13))
    # A clock and noticeboard are background details, not lesson-specific geometry.
    clock = K.rod("Clock face", (3.28, 1.69, 2.75), (3.28, 1.63, 2.75), 0.20, (0.87, 0.86, 0.77))
    K.rod("Clock hour hand", (3.28, 1.59, 2.75), (3.19, 1.59, 2.81), 0.008, (0.12, 0.14, 0.14))
    K.rod("Clock minute hand", (3.28, 1.59, 2.75), (3.38, 1.59, 2.84), 0.005, (0.12, 0.14, 0.14))
    for location, energy, size in [((-2.0, -0.3, 2.5), 150, 3), ((0.5, -3, 2.7), 70, 4)]:
        bpy.ops.object.light_add(type="AREA", location=location)
        light = bpy.context.object
        light.data.energy, light.data.shape, light.data.size = energy, "DISK", size
        light.data.color = (1.0, 0.93, 0.82)
        light.data.use_shadow = bool(spec.get("quality"))
        if hasattr(light.data, "use_shadow_jitter"):
            light.data.use_shadow_jitter = False
        look_at(light, (0, 0, 1))
    bpy.ops.object.camera_add(location=(-0.7, -2.2, 1.6))
    camera = bpy.context.object
    camera.data.type, camera.data.lens = "PERSP", 55
    look_at(camera, (-1.0, 0.55, 1.40))
    scene.camera = camera
    return tex


def lecture_avatar_material(source):
    """A lightweight toon material preserving the VRM's base texture, color and alpha."""
    extension = getattr(source, "vrm_addon_extension", None)
    if not extension or not extension.mtoon1.enabled:
        return source
    existing = bpy.data.materials.get(source.name + " lecture")
    if existing:
        return existing
    gltf = extension.mtoon1
    factor = tuple(gltf.pbr_metallic_roughness.base_color_factor)
    image = gltf.pbr_metallic_roughness.base_color_texture.index.source
    material = bpy.data.materials.new(source.name + " lecture")
    material.diffuse_color = factor
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emit = nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = (*factor[:3], 1)
    alpha = factor[3]
    texture = None
    if image:
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.blend_type = "MULTIPLY"
        multiply.inputs[0].default_value = 1
        multiply.inputs[2].default_value = (*factor[:3], 1)
        material.node_tree.links.new(texture.outputs["Color"], multiply.inputs[1])
        material.node_tree.links.new(multiply.outputs[0], emit.inputs["Color"])
        nodes.active = texture
    if gltf.alpha_mode == "OPAQUE":
        material.node_tree.links.new(emit.outputs[0], output.inputs["Surface"])
    else:
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        mix = nodes.new("ShaderNodeMixShader")
        mix.inputs[0].default_value = alpha
        if texture:
            amount = nodes.new("ShaderNodeMath"); amount.operation = "MULTIPLY"
            amount.inputs[1].default_value = alpha
            material.node_tree.links.new(texture.outputs["Alpha"], amount.inputs[0])
            material.node_tree.links.new(amount.outputs[0], mix.inputs[0])
        material.node_tree.links.new(transparent.outputs[0], mix.inputs[1])
        material.node_tree.links.new(emit.outputs[0], mix.inputs[2])
        material.node_tree.links.new(mix.outputs[0], output.inputs["Surface"])
        material.surface_render_method = "BLENDED"
    return material


def remove_invisible_workbench_faces(obj):
    """Workbench ignores alpha=0; omit only faces that the VRM declares fully invisible.

    Keep every vertex so skin weights and expression shape-key indices remain unchanged.
    The source VRM file is never modified.
    """
    hidden = set()
    for index, material in enumerate(obj.data.materials):
        extension = getattr(material, "vrm_addon_extension", None) if material else None
        if extension and extension.mtoon1.enabled:
            gltf = extension.mtoon1
            if gltf.alpha_mode != "OPAQUE" and gltf.pbr_metallic_roughness.base_color_factor[3] <= 0.000001:
                hidden.add(index)
    if not hidden:
        return
    import bmesh
    count = len(obj.data.vertices)
    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    bmesh.ops.delete(mesh, geom=[face for face in mesh.faces if face.material_index in hidden], context="FACES_ONLY")
    mesh.to_mesh(obj.data)
    mesh.free()
    assert len(obj.data.vertices) == count, "transparent-face cleanup must preserve rig vertices"


def import_teacher(path):
    if not path or not Path(path).is_file():
        return None
    sys.path.insert(0, "/opt/blender-addons")
    bpy.ops.preferences.addon_enable(module="io_scene_vrm")
    collection("Teacher")
    before = set(bpy.data.objects)
    engine = bpy.context.scene.render.engine
    bpy.context.scene.render.engine = "BLENDER_EEVEE_NEXT"
    try:
        result = bpy.ops.import_scene.vrm(filepath=path)
    finally:
        bpy.context.scene.render.engine = engine
    if result != {"FINISHED"}:
        raise RuntimeError("VRM import did not complete")
    objects = list(set(bpy.data.objects) - before)
    if engine == "BLENDER_WORKBENCH":
        for obj in objects:
            if obj.type == "MESH":
                remove_invisible_workbench_faces(obj)
    if engine == "BLENDER_EEVEE_NEXT":
        for obj in objects:
            if obj.type != "MESH":
                continue
            for slot in obj.material_slots:
                if slot.material:
                    slot.material = lecture_avatar_material(slot.material)
            for modifier in list(obj.modifiers):
                if modifier.type == "NODES" and "outline" in modifier.name.casefold():
                    obj.modifiers.remove(modifier)
    # Workbench can show the original avatar textures when the base-color image is active.
    for obj in objects:
        if obj.type != "MESH":
            continue
        for mat in obj.data.materials:
            if not mat or not mat.use_nodes:
                continue
            extension = getattr(mat, "vrm_addon_extension", None)
            image = extension.mtoon1.pbr_metallic_roughness.base_color_texture.index.source if extension else None
            if image:
                node = next((n for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image == image), None)
                if node is None:
                    node = mat.node_tree.nodes.new("ShaderNodeTexImage")
                    node.image = image
                mat.node_tree.nodes.active = node
    root = K.normalize(objects, TEACHER_POSITION, height=1.7)
    root.rotation_euler.z = 0.22
    armature = next((o for o in objects if o.type == "ARMATURE"), None)
    bpy.context.view_layer.update()
    teacher = {"objects": objects, "root": root, "armature": armature, "targets": [], "mouth_binds": [], "blink_binds": []}
    if armature:
        ext = armature.data.vrm_addon_extension
        bones = ext.vrm1.humanoid.human_bones if ext.spec_version == "1.0" else None
        for side in ("left", "right"):
            if bones:
                upper_name = getattr(bones, side + "_upper_arm").node.bone_name
                lower_name = getattr(bones, side + "_lower_arm").node.bone_name
            else:
                mapping = {b.bone: b.node.bone_name for b in ext.vrm0.humanoid.human_bones}
                upper_name, lower_name = mapping.get(side + "UpperArm"), mapping.get(side + "LowerArm")
            upper, lower = armature.pose.bones.get(upper_name or ""), armature.pose.bones.get(lower_name or "")
            if not upper or not lower:
                continue
            shoulder = armature.matrix_world @ upper.head
            elbow = armature.matrix_world @ upper.tail
            wrist = armature.matrix_world @ lower.tail
            length = (elbow - shoulder).length + (wrist - elbow).length
            target = bpy.data.objects.new(side + " hand target", None)
            bpy.context.collection.objects.link(target)
            rest = shoulder + Vector((0.08 if shoulder.x > TEACHER_POSITION[0] else -0.08, -0.12, -1)).normalized() * length * 0.93
            point = shoulder + (Vector((0.6, 1.6, 1.85)) - shoulder).normalized() * length * 0.95
            target.location = rest
            constraint = lower.constraints.new("IK")
            constraint.target, constraint.chain_count, constraint.use_stretch = target, 2, False
            teacher["targets"].append((target, rest, point, shoulder.x))
        if bones:
            for name, destination in (("aa", "mouth_binds"), ("blink", "blink_binds")):
                expression = getattr(ext.vrm1.expressions.preset, name)
                for bind in expression.morph_target_binds:
                    obj = bpy.data.objects.get(bind.node.mesh_object_name)
                    index = int(bind.index) if str(bind.index).isdigit() else -1
                    if obj and obj.type == "MESH" and obj.data.shape_keys and 0 <= index < len(obj.data.shape_keys.key_blocks):
                        teacher[destination].append((obj.data.shape_keys.key_blocks[index], bind.weight))
    return teacher


def animate_teacher(teacher, t, mouth):
    if not teacher:
        return
    teacher["root"].rotation_euler.z = 0.22 + 0.015 * math.sin(t * 1.1)
    pointing_x = max((item[3] for item in teacher["targets"]), default=0)
    for target, rest, point, x in teacher["targets"]:
        blend = 0.90 + 0.06 * math.sin(t * 0.6) if x == pointing_x else 0
        target.location = rest.lerp(point, blend)
    for key, weight in teacher["mouth_binds"]:
        key.value = 0.75 * mouth * weight
    for key, weight in teacher["blink_binds"]:
        key.value = max(0, 1 - abs((t % 3.8) - 3.6) / 0.10) * weight
    for obj in teacher["objects"]:
        if obj.type != "MESH" or not obj.data.shape_keys:
            continue
        for key in obj.data.shape_keys.key_blocks:
            name = key.name.casefold()
            if name in ("aa", "a", "fcl_mth_a", "mouthopen", "mouth_open"):
                key.value = 0.75 * mouth
            elif name in ("blink", "fcl_eye_close"):
                key.value = max(0, 1 - abs((t % 3.8) - 3.6) / 0.10)


def load_code(path):
    module_spec = importlib.util.spec_from_file_location("lesson_3d", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return list(module.SCENES)


def clear_demo(col):
    if col:
        for obj in list(col.all_objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(col)


def bake_animation(update, count, fps, out, index):
    """Package native keyframes and textures; no generated Python runs on the personal PC."""
    import array
    import hashlib
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, count
    # Each lesson shot is a separate self-contained file.
    for obj in bpy.data.objects:
        obj.animation_data_clear()
        if obj.type == "MESH" and obj.data.shape_keys:
            obj.data.shape_keys.animation_data_clear()
    for mat in bpy.data.materials:
        if mat.node_tree:
            mat.node_tree.animation_data_clear()
    meshes = {obj.data for obj in K._DEMO_COLLECTION.all_objects if obj.type == "MESH"}
    def geometry_hash(mesh):
        coords = array.array("f", [0]) * (len(mesh.vertices) * 3)
        mesh.vertices.foreach_get("co", coords)
        return (len(mesh.polygons), len(mesh.edges), hashlib.sha256(coords.tobytes()).digest())
    original = {mesh: geometry_hash(mesh) for mesh in meshes}
    objects = set(bpy.data.objects)
    for frame in range(1, count + 1):
        scene.frame_set(frame)
        update((frame - 1) / fps)
        if set(bpy.data.objects) != objects or any(geometry_hash(m) != original[m] for m in meshes):
            raise ValueError("Remote rendering needs stable geometry; use native object/part transforms")
        for obj in objects:
            for prop in ("location", "rotation_euler", "rotation_quaternion", "scale", "hide_render"):
                obj.keyframe_insert(data_path=prop, frame=frame)
            if obj.type == "ARMATURE":
                for bone in obj.pose.bones:
                    for prop in ("location", "rotation_euler", "rotation_quaternion", "scale"):
                        bone.keyframe_insert(data_path=prop, frame=frame)
            if obj.type == "MESH" and obj.data.shape_keys:
                for key in obj.data.shape_keys.key_blocks:
                    key.keyframe_insert(data_path="value", frame=frame)
        for mat in bpy.data.materials:
            if mat.node_tree:
                for node in mat.node_tree.nodes:
                    for socket in node.inputs:
                        if not socket.is_linked and socket.type in ("VALUE", "RGBA", "VECTOR"):
                            socket.keyframe_insert(data_path="default_value", frame=frame)
    scene.frame_set(1)
    bpy.ops.file.pack_all()
    # Text blocks and Python drivers are not part of the rendering contract.
    for block in list(bpy.data.texts):
        bpy.data.texts.remove(block)
    for obj in bpy.data.objects:
        if obj.animation_data:
            for driver in list(obj.animation_data.drivers):
                obj.driver_remove(driver.data_path, driver.array_index)
    path = out / f"scene_{index}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), check_existing=False, compress=True)
    return path.name


def main():
    spec = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    out = Path(spec["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    result = {"scenes": [], "avatar_loaded": False, "candidate_keys": [], "candidates": []}
    try:
        capture = spec["mode"] == "library"
        if capture:
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
            board_tex, teacher = None, None
        else:
            board_tex = classroom(spec)
        K._MODELS.update({m["id"]: m for m in spec.get("models", [])})
        if not capture:
            teacher = import_teacher(spec.get("avatar"))
        result["avatar_loaded"] = bool(teacher)
        builders = load_code(spec["code"])
        if len(builders) != len(spec["scenes"]):
            raise ValueError("SCENES count must match narration scenes")
        demo = None
        for index in spec.get("only", list(range(len(builders)))):
            clear_demo(demo)
            demo = collection("Demonstration")
            K._DEMO_COLLECTION = demo
            K._CANDIDATES.clear()
            scene = spec["scenes"][index]
            if board_tex:
                board_tex.image = bpy.data.images.load(scene["board"], check_existing=True)
            animate = builders[index]()
            if len(demo.all_objects) > 250:
                raise ValueError("too many demonstration objects; use fewer objects or batched meshes")
            if animate is not None and not callable(animate):
                raise ValueError("each scene builder must return an animation function or None")
            result["candidate_keys"] = sorted(set(result["candidate_keys"]) | {c["key"] for c in K._CANDIDATES})
            if capture:
                from three_d_library_export import export_candidate
                saved_keys = {c["key"] for c in result["candidates"]}
                for candidate in K._CANDIDATES:
                    if candidate["key"] not in saved_keys and len(result["candidates"]) < 4:
                        result["candidates"].append(export_candidate(candidate, out, len(result["candidates"])))
                        saved_keys.add(candidate["key"])
                continue
            shot = camera_shot(scene.get("shot") or ("teacher" if index == 0 else "object"), teacher)
            frames = []
            count = max(1, round(scene["duration"] * spec["fps"]))
            def update(t):
                if animate:
                    animate(t, scene["duration"])
                envelope = scene.get("mouth", [])
                mouth = envelope[min(int(t * spec["fps"]), len(envelope) - 1)] if envelope else 0
                animate_teacher(teacher, t, mouth)
            started = time.monotonic()
            if spec["mode"] == "bake":
                blend = bake_animation(update, count, spec["fps"], out, index)
            elif spec["mode"] == "test":
                update(scene["duration"] * 0.45)
                filename = f"scene_{index}_00000.png"
                bpy.context.scene.render.filepath = str(out / filename)
                bpy.ops.render.render(write_still=True)
                frames.append(str(out / filename))
                count = 1
            else:
                render_scene = bpy.context.scene
                render_scene.frame_start, render_scene.frame_end = 1, count
                render_scene.render.filepath = str(out / f"scene_{index}_#####")
                def frame_change(current_scene, *args):
                    update((current_scene.frame_current - 1) / spec["fps"])
                bpy.app.handlers.frame_change_pre.append(frame_change)
                try:
                    # Keep one render session so Eevee can reuse shaders and geometry across frames.
                    bpy.ops.render.render(animation=True)
                finally:
                    bpy.app.handlers.frame_change_pre.remove(frame_change)
            result["scenes"].append({"index": index, "frames": frames, "seconds": time.monotonic() - started,
                                     "demo_objects": len(demo.all_objects), "frame_count": count,
                                     "shot":shot, "camera_type":bpy.context.scene.camera.data.type})
            if spec["mode"] == "bake":
                result["scenes"][-1]["blend"] = blend
        result["used_models"] = sorted(K._USED_MODELS)
    except Exception:
        result["error"] = traceback.format_exc()[-2000:]
    (out / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
