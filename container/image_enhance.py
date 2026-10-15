"""Enhance a supplied photo ("джарвис enhance", "улучши фото", "апскейл") with the host Codex image worker.

The worker edits from the photo itself and is told to keep the same picture: composition, subjects, identities,
objects and text, and the original color character unless colorization was asked for. Only the image and that one
option leave the bot; the request text does not. The reply is labeled as a generative reconstruction: added detail
is plausible, not recovered, so it is never presented as a real higher-resolution photo.
"""
import io
import logging
import os
from pathlib import Path
import re

log = logging.getLogger("enhance")
MAX_REFERENCE_SIDE = 3072
STRINGS = {
    "ru": {"progress": "🪄 Улучшаю изображение генерацией через Codex…",
           "label": "🪄 **Улучшено генеративно** (Codex image generation). Чёткость и мелкие детали дорисованы моделью: "
                    "это правдоподобная реконструкция, а не настоящий снимок в высоком разрешении.",
           "colorized": "Цвета тоже подобраны моделью, а не восстановлены.",
           "no_image": "🪄 Прикрепи картинку (или ответь на сообщение с ней) вместе с просьбой — и я её улучшу.",
           "off": "🪄 Улучшение изображений сейчас недоступно: генератор Codex не подключён.",
           "failed": "🪄 Генератор не смог улучшить это изображение{reason}. Попробуй другое фото."},
    "en": {"progress": "🪄 Enhancing the image with Codex image generation…",
           "label": "🪄 **Generatively enhanced** (Codex image generation). Sharpness and fine detail were drawn by the "
                    "model: a plausible reconstruction, not a real higher-resolution photo.",
           "colorized": "The colors were chosen by the model, not recovered.",
           "no_image": "🪄 Attach a picture (or reply to a message with one) together with the request and I'll enhance it.",
           "off": "🪄 Image enhancement is unavailable right now: the Codex generator is not connected.",
           "failed": "🪄 The generator could not enhance this image{reason}. Try another photo."},
}


def strings():
    return STRINGS.get(os.environ.get("BOT_LANG") or "ru", STRINGS["en"])


def colorize_requested(text):
    from render_modes import _request_words
    return bool(re.search(r"\b(?:раскрась(?:те)?|в\s+цвет\w*|цветн\w+|colori[sz]e|in\s+colou?r)\b", _request_words(text)))


def source_photo(job, images, directory):
    """The first supplied image as a JPEG reference for the worker, at native resolution when it was kept."""
    import base64
    from PIL import Image
    if not images:
        return None
    originals = job.get("image_originals") or []
    raw = originals[0] if originals and originals[0] else None
    if raw is None:
        source = images[0].get("source") or {}
        if source.get("type") != "base64":
            return None
        raw = base64.b64decode(source["data"])
    with Image.open(io.BytesIO(raw)) as opened:
        picture = opened.convert("RGB")
    picture.thumbnail((MAX_REFERENCE_SIDE, MAX_REFERENCE_SIDE))
    path = Path(directory) / "enhance-source.jpg"
    picture.save(path, "JPEG", quality=95)
    return {"path": str(path), "width": picture.width, "height": picture.height}


def fit_upload(data, workdir, limit):
    """PNG when it fits the upload limit, else high-quality JPEG, downscaled only as far as needed."""
    from PIL import Image
    with Image.open(io.BytesIO(data)) as produced:
        picture = produced.convert("RGB")
    path = Path(workdir) / "enhanced.png"
    picture.save(path, "PNG", optimize=True)
    if path.stat().st_size <= limit:
        return path, "image/png", picture.width, picture.height
    path = Path(workdir) / "enhanced.jpg"
    for _ in range(8):
        picture.save(path, "JPEG", quality=92)
        size = path.stat().st_size
        if size <= limit:
            break
        scale = max(0.5, (limit / size) ** 0.5 * 0.95)
        picture = picture.resize((max(1, int(picture.width * scale)), max(1, int(picture.height * scale))), Image.LANCZOS)
    return path, "image/jpeg", picture.width, picture.height


def run(job, images, workdir):
    import assets
    import pipeline as P
    S = strings()
    if os.environ.get("IMAGE_ENHANCE", "1") != "1":
        raise P.UserError(S["off"])
    photo = source_photo(job, images, workdir)
    if photo is None:
        raise P.UserError(S["no_image"])
    colorize = colorize_requested(job.get("question") or job.get("text") or "")
    P._safe_text(job, S["progress"])
    try:
        data, result = assets.request_codex("", "", [photo], timeout=int(os.environ.get("IMAGE_ENHANCE_TIMEOUT") or 750),
                                            mode="enhance", options={"colorize": colorize})
    except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
        log.warning("image enhancement unavailable: %s", type(exc).__name__)
        raise P.UserError(S["off"] if "unavailable" in str(exc) else S["failed"].format(reason="")) from None
    if not data:
        reason = assets.clean(result.get("description"), 160)
        raise P.UserError(S["failed"].format(reason=f" ({reason})" if reason else ""))
    out, kind, width, height = fit_upload(data, workdir, int(P.MAX_UPLOAD_MB * 1024 * 1024))
    content = S["label"] + (" " + S["colorized"] if colorize else "")
    content += f"\n-# {photo['width']}×{photo['height']} → {width}×{height}"
    P.discord_file(job, out, "enhanced." + ("png" if kind == "image/png" else "jpg"), kind, content)
    try:  # a follow-up ("а теперь раскрась") can refer to this answer
        import followups
        followups.record_image(job, f"Generatively enhanced version of the supplied photo ({width}x{height})",out.read_bytes())
        followups.remember(job)
    except Exception as exc:
        log.warning("could not save image memory: %s", type(exc).__name__)
    log.info("image enhanced: %dx%d -> %dx%d colorize=%s", photo["width"], photo["height"], width, height, colorize)
