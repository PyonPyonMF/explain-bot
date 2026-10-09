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


def collection(name):
    col = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(col)
    bpy.context.view_layer.active_layer_collection = bpy.context.view_layer.layer_collection.children[col.name]
    return col


def look_at(obj, target):
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()


def classroom(spec):
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.render.engine = spec.get("engine", "BLENDER_EEVEE_NEXT")
    if scene.render.engine == "BLENDER_WORKBENCH":
        shade = scene.display.shading
        shade.light, shade.color_type = "STUDIO", "TEXTURE"
        shade.show_shadows, shade.show_cavity = False, True
        shade.cavity_type = "BOTH"
        shade.background_type, shade.background_color = "WORLD", (0.06, 0.08, 0.12)
        scene.display.render_aa = "FXAA"
    scene.render.resolution_x = spec.get("width", 960)
    scene.render.resolution_y = spec.get("height", 540)
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.film_transparent = False
    scene.render.fps = spec.get("fps", 12)
    scene.render.threads_mode, scene.render.threads = "FIXED", 2
    if hasattr(scene, "eevee"):
        scene.eevee.taa_render_samples = spec.get("samples", 16)
    scene.world.color = (0.06, 0.07, 0.10)
    scene.view_settings.view_transform = "Standard"
    if scene.render.engine == "BLENDER_WORKBENCH":
        scene.view_settings.exposure = 1.0
    collection("Classroom")
    K.box("Floor", (0, 0, -0.10), (8, 7, 0.2), (0.12, 0.15, 0.21))
    K.box("Back wall", (0, 1.65, 1.6), (8, 0.15, 3.3), (0.08, 0.12, 0.19))
    K.box("Board frame", (0, 1.49, 1.94), (3.95, 0.10, 2.04), (0.42, 0.32, 0.20))
    board = K.box("Board", (0, 1.42, 1.94), (3.8, 0.04, 1.9), (0.01, 0.04, 0.05), bevel=0)
    # A plane facing the camera has predictable UVs for legible board text.
    bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 1.385, 1.94), rotation=(math.pi / 2, 0, 0))
    screen = bpy.context.object
    screen.name, screen.scale = "Board content", (1.87, 0.925, 1)
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
    K.box("Demonstration table", (0.3, -0.2, 0.74), (2.5, 1.5, 0.12), (0.30, 0.36, 0.46))
    for x in (-0.65, 1.25):
        for y in (-0.75, 0.35):
            K.box("Table leg", (x, y, 0.35), (0.10, 0.10, 0.70), (0.14, 0.18, 0.24))
    for location, energy, size in [((1, -3, 5), 450, 4), ((-3, 0, 3.5), 220, 3)]:
        bpy.ops.object.light_add(type="AREA", location=location)
        light = bpy.context.object
        light.data.energy, light.data.shape, light.data.size = energy, "DISK", size
        if hasattr(light.data, "use_shadow_jitter"):
            light.data.use_shadow_jitter = False
        look_at(light, (0, 0, 1))
    bpy.ops.object.camera_add(location=(3.0, -7.4, 3.15))
    camera = bpy.context.object
    camera.data.type, camera.data.ortho_scale = "ORTHO", 6.7
    look_at(camera, (0, 0.35, 1.55))
    scene.camera = camera
    return tex


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
    root = K.normalize(objects, (-1.8, 0, 0), height=1.7)
    root.rotation_euler.z = 0.35
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
            rest = shoulder + Vector((0.08 if shoulder.x > -1.8 else -0.08, -0.12, -1)).normalized() * length * 0.93
            point = shoulder + (Vector((0.1, 1.4, 2.0)) - shoulder).normalized() * length * 0.95
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
    teacher["root"].rotation_euler.z = 0.35 + 0.025 * math.sin(t * 1.1)
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


def main():
    spec = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
    out = Path(spec["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    result = {"scenes": [], "avatar_loaded": False}
    try:
        board_tex = classroom(spec)
        K._MODELS.update({m["id"]: m for m in spec.get("models", [])})
        teacher = import_teacher(spec.get("avatar"))
        result["avatar_loaded"] = bool(teacher)
        builders = load_code(spec["code"])
        if len(builders) != len(spec["scenes"]):
            raise ValueError("SCENES count must match narration scenes")
        demo = None
        for index in spec.get("only", list(range(len(builders)))):
            clear_demo(demo)
            demo = collection("Demonstration")
            scene = spec["scenes"][index]
            board_tex.image = bpy.data.images.load(scene["board"], check_existing=True)
            animate = builders[index]()
            if len(demo.all_objects) > 250:
                raise ValueError("too many demonstration objects; use fewer objects or batched meshes")
            if animate is not None and not callable(animate):
                raise ValueError("each scene builder must return an animation function or None")
            frames = []
            count = max(1, round(scene["duration"] * spec["fps"]))
            def update(t):
                if animate:
                    animate(t, scene["duration"])
                envelope = scene.get("mouth", [])
                mouth = envelope[min(int(t * spec["fps"]), len(envelope) - 1)] if envelope else 0
                animate_teacher(teacher, t, mouth)
            started = time.monotonic()
            if spec["mode"] == "test":
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
                                     "demo_objects": len(demo.all_objects), "frame_count": count})
    except Exception:
        result["error"] = traceback.format_exc()[-2000:]
    (out / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
