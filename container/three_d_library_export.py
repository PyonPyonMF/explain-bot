"""Export declared demonstration objects as standalone GLBs and object-only thumbnails."""
import hashlib
import json
from pathlib import Path
import re

import bpy
from mathutils import Matrix, Vector

import three_d_kit as K


def object_preview(collection, path, center):
    scene = bpy.data.scenes.new("Library preview")
    scene.collection.children.link(collection)
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = 384
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "TEXTURE"
    scene.display.shading.show_shadows = False
    scene.display.shading.show_cavity = True
    scene.display.shading.background_type = "VIEWPORT"
    scene.display.shading.background_color = (0.07, 0.09, 0.13)
    scene.display.render_aa = "FXAA"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = 1
    camera = bpy.data.objects.new("Library camera", bpy.data.cameras.new("Library camera"))
    scene.collection.objects.link(camera)
    camera.location = Vector(center) + Vector((2, -3, 1.8))
    camera.rotation_euler = (Vector(center) - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.type, camera.data.ortho_scale = "ORTHO", 1.55
    scene.camera = camera
    scene.render.filepath = str(path)
    bpy.ops.render.render(scene=scene.name, write_still=True)
    bpy.data.objects.remove(camera, do_unlink=True)
    bpy.data.scenes.remove(scene)


def fingerprint(objects):
    """Ignore exporter timestamps/namespaces; include actual geometry, hierarchy and materials."""
    digest = hashlib.sha256()
    def number(value):
        return round(float(value), 6) or 0.0
    def add(value):
        digest.update(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())
    for obj in sorted(objects, key=lambda o: o.get("library_part", "")):
        add([obj.get("library_part"), obj.type, obj.parent.get("library_part", "root") if obj.parent else None])
        if obj.type != "MESH":
            add([number(v) for v in obj.matrix_world.translation])
            continue
        add([[number(v) for v in obj.matrix_world @ vert.co] for vert in obj.data.vertices])
        faces = []
        for face in obj.data.polygons:
            vertices = list(face.vertices)
            start = vertices.index(min(vertices))
            loops = list(face.loop_indices)
            loops = loops[start:] + loops[:start]
            uv = [[[number(v) for v in layer.data[loop].uv] for loop in loops] for layer in obj.data.uv_layers]
            faces.append([vertices[start:] + vertices[:start], face.material_index, face.use_smooth, uv])
        add(sorted(faces))
        for mat in obj.data.materials:
            if not mat:
                continue
            add([number(v) for v in mat.diffuse_color])
            if mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == "BSDF_PRINCIPLED":
                        for name in ("Base Color", "Metallic", "Roughness", "Alpha", "Emission Color", "Emission Strength"):
                            if name in node.inputs:
                                value = node.inputs[name].default_value
                                add([name, [number(v) for v in value] if hasattr(value, "__len__") else number(value)])
                    if node.type == "TEX_IMAGE" and node.image:
                        image = node.image
                        if image.packed_file:
                            digest.update(bytes(image.packed_file.data))
                        elif image.filepath and Path(bpy.path.abspath(image.filepath)).is_file():
                            digest.update(Path(bpy.path.abspath(image.filepath)).read_bytes())
    return digest.hexdigest()


def export_candidate(candidate, out, index):
    key = str(candidate.get("key") or "").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", key):
        raise ValueError("invalid reusable object key")
    allowed = set(K._DEMO_COLLECTION.all_objects)
    originals = [o for o in candidate["objects"] if o in allowed and o.type in ("MESH", "CURVE", "SURFACE", "EMPTY")]
    if not originals or len(originals) > 150:
        raise ValueError("invalid reusable object size")
    bpy.context.view_layer.update()
    transforms = {obj:obj.matrix_world.copy() for obj in originals}
    depsgraph = bpy.context.evaluated_depsgraph_get()
    col = bpy.data.collections.new("Library export " + key)
    bpy.context.scene.collection.children.link(col)
    root = bpy.data.objects.new(key + " root", None)
    col.objects.link(root)
    copies = {}
    for obj in originals:
        data = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), depsgraph=depsgraph) if obj.type != "EMPTY" else None
        copy = bpy.data.objects.new(obj.name, data)
        copy["library_part"] = obj.get("library_part") or obj.name
        copy.matrix_world = obj.matrix_world.copy()
        col.objects.link(copy)
        copies[obj] = copy
    if not any(o.type == "MESH" for o in copies.values()):
        raise ValueError("reusable object has no mesh")
    if sum(len(o.data.vertices) for o in copies.values() if o.type == "MESH") > 500_000:
        raise ValueError("reusable object has too many vertices")
    for obj in originals:
        copy = copies[obj]
        copy.parent = copies.get(obj.parent, root)
        copy.matrix_parent_inverse = Matrix.Identity(4)
        copy.matrix_basis = transforms[obj.parent].inverted() @ transforms[obj] if obj.parent in copies else transforms[obj]
        copy.hide_render = False
    points = [transforms[original] @ vertex.co for original, obj in copies.items() if obj.type == "MESH" for vertex in obj.data.vertices]
    low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    scale = 1 / max(max(high - low), 0.001)
    root.scale = (scale,) * 3
    root.location = -Vector(((low.x + high.x)/2, (low.y + high.y)/2, low.z)) * scale
    bpy.context.view_layer.update()
    model_path = out / f"object-{index}.glb"
    preview_path = out / f"object-{index}.png"
    digest = fingerprint(list(copies.values()))
    bpy.ops.object.select_all(action="DESELECT")
    for obj in [root, *copies.values()]:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = root
    result = bpy.ops.export_scene.gltf(filepath=str(model_path), export_format="GLB", use_selection=True,
        export_extras=True, export_animations=False, export_cameras=False, export_lights=False, export_apply=False)
    if result != {"FINISHED"}:
        raise RuntimeError("object GLB export failed")
    object_preview(col, preview_path, (0, 0, (high.z-low.z)*scale/2))
    sources = [K._MODELS[asset_id] for asset_id in {o.get("library_origin_id") for o in originals}
               if asset_id in K._MODELS]
    metadata = {"key":key, "title":str(candidate.get("title") or key)[:120],
                "description":str(candidate.get("description") or "")[:1200], "tags":candidate.get("tags", [])[:30],
                "file":model_path.name, "preview":preview_path.name, "fingerprint":digest,
                "parts":[o["library_part"] for o in copies.values()],
                "credit":"; ".join(dict.fromkeys(s.get("credit", "") for s in sources if s.get("credit"))),
                "source_url":next((s.get("source_url") for s in sources if s.get("source_url")), ""),
                "license":"; ".join(dict.fromkeys(s.get("license", "") for s in sources if s.get("license"))) or "generated",
                "bounds":[round(v*scale, 6) for v in high-low]}
    for obj in [*copies.values(), root]:
        bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(col)
    return metadata
