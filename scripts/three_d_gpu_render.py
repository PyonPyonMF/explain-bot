"""Trusted local Blender entrypoint. Only native scene animation is accepted."""
import json
from pathlib import Path
import sys
import bpy

config = json.loads(Path(sys.argv[sys.argv.index("--") + 1]).read_text())
scene = bpy.context.scene
for text in list(bpy.data.texts):
    bpy.data.texts.remove(text)
scene.render.engine = "BLENDER_EEVEE" if bpy.app.version >= (5, 0, 0) else "BLENDER_EEVEE_NEXT"
scene.render.threads_mode = "AUTO"
scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
scene.render.resolution_percentage = 100
scene.render.fps = int(config["fps"])
scene.frame_start, scene.frame_end = 1, int(config["frames"])
if hasattr(scene, "eevee"):
    if hasattr(scene.eevee, "taa_render_samples"):
        scene.eevee.taa_render_samples = 32
    if hasattr(scene.eevee, "taa_samples"):
        scene.eevee.taa_samples = 32
if hasattr(scene.render.image_settings, "media_type"):
    scene.render.image_settings.media_type = "VIDEO"
scene.render.image_settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.ffmpeg.constant_rate_factor = "HIGH"
scene.render.ffmpeg.ffmpeg_preset = "GOOD"
scene.render.ffmpeg.audio_codec = "NONE"
scene.render.filepath = config["output"]
bpy.ops.render.render(animation=True)
