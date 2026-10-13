"""Collect everything Sonnet should see for a job: the target message, its images, linked pages,
text attachments, and (if a bot token is set) the conversation around it.

All of this is untrusted data. It goes into the prompt inside clearly marked tags.
"""
import base64
import html
import io
import ipaddress
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("inputs")

DISCORD_BASE = os.environ.get("DISCORD_API_BASE", "https://discord.com/api/v10")
MAX_IMAGES = 4
CONTEXT_MEDIA = 2        # pictures/GIFs taken from the recent discussion itself
CONTEXT_MEDIA_WINDOW = 6  # ... from at most this many latest messages
MAX_LINKS = 3
MAX_LINK_CHARS = 6000
MAX_FILE_CHARS = 6000
MAX_CONTEXT_MSG_CHARS = 500
MAX_CONTEXT_CHARS = 12000
MAX_DOWNLOAD = 10 * 1024 * 1024
IMAGE_LONG_EDGE = 1568
TEXT_EXT = (".txt", ".md", ".py", ".js", ".ts", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".cs", ".json", ".yaml",
            ".yml", ".toml", ".csv", ".log", ".sql", ".sh", ".html", ".css", ".xml", ".kt", ".swift", ".rb", ".php")
URL_RE = re.compile(r"https?://[^\s<>()\"'`]+")
UA = "Mozilla/5.0 (compatible; explain-bot/1.0; +https://discord.com)"


# ------------------------------------------------------------ safe download
class _Blocked(Exception):
    pass


def _check_host(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise _Blocked("only http/https")
    if os.environ.get("ALLOW_PRIVATE_FETCH") == "1":  # local tests only
        return
    for info in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80)):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise _Blocked(f"address {ip} is not public")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def fetch(url, max_bytes=MAX_DOWNLOAD, timeout=10, headers=None):
    """GET a public URL. Returns (bytes, content_type). Refuses private addresses, also after redirects."""
    _check_host(url)
    req = urllib.request.Request(url, headers={"user-agent": UA, **(headers or {})})
    with _opener.open(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise _Blocked("too large")
        return data, (r.headers.get("content-type") or "").split(";")[0].strip().lower()


def final_url(url, timeout=10):
    """Where a share/short link lands after redirects (each hop is checked like fetch)."""
    _check_host(url)
    req = urllib.request.Request(url, headers={"user-agent": UA})
    with _opener.open(req, timeout=timeout) as r:
        return r.geturl()


# --------------------------------------------------------------- images
def _fingerprint(data):
    """A tiny average hash: equal for the same picture at different sizes or encodings."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        pixels = list(im.convert("L").resize((8, 8)).tobytes())
        avg = sum(pixels) / 64
        return "".join("1" if p > avg else "0" for p in pixels)
    except Exception:
        return hash(data)


VIDEO_TYPES = ("video/mp4", "video/webm", "video/quicktime")
STORYBOARD_FRAMES = 6


def _frames_from_image(data):
    """Evenly spaced frames of an animated GIF/WebP/APNG, or None for a still image."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    n = getattr(im, "n_frames", 1)
    if n < 2:
        return None
    count = min(STORYBOARD_FRAMES, n)
    frames = []
    for k in range(count):
        im.seek(min(n - 1, int((k + 0.5) * n / count)))  # mid-points: skips the black fade-in of frame 0
        frames.append(im.convert("RGB"))
    return frames


def _frames_from_video(data, content_type):
    import subprocess
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "clip" + {"video/webm": ".webm", "video/quicktime": ".mov"}.get(content_type, ".mp4"))
        with open(src, "wb") as f:
            f.write(data)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", src],
                               capture_output=True, text=True, timeout=20)
        duration = float(probe.stdout.strip() or 0)
        if duration <= 0:
            return None
        frames = []
        for k in range(STORYBOARD_FRAMES):
            out = os.path.join(d, f"f{k}.png")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{(k + 0.5) * duration / STORYBOARD_FRAMES:.3f}",
                            "-i", src, "-frames:v", "1", "-vf", "scale=640:-2", out], timeout=30, check=False)
            if os.path.exists(out):
                with Image.open(out) as im:
                    frames.append(im.convert("RGB"))
        return frames or None


def storyboard(data, content_type):
    """One numbered contact sheet for an animated GIF/sticker or a short video: (jpeg bytes, frames) or None."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        if content_type in VIDEO_TYPES:
            frames = _frames_from_video(data, content_type)
        else:
            frames = _frames_from_image(data)
    except Exception as e:
        log.warning("storyboard skipped (%s): %s", content_type, e)
        return None
    if not frames:
        return None
    cols = 3 if len(frames) > 4 else 2 if len(frames) > 1 else 1
    rows = -(-len(frames) // cols)
    w = min(512, max(f.width for f in frames))  # small GIFs are not padded into large empty tiles
    h = min(512, max(1, round(w * frames[0].height / max(1, frames[0].width))))
    pad = 8
    sheet = Image.new("RGB", (cols * (w + pad) + pad, rows * (h + pad) + pad), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=28)
    except TypeError:
        font = ImageFont.load_default()
    for k, frame in enumerate(frames):
        frame = frame.copy()
        frame.thumbnail((w, h))
        x = pad + (k % cols) * (w + pad) + (w - frame.width) // 2
        y = pad + (k // cols) * (h + pad) + (h - frame.height) // 2
        sheet.paste(frame, (x, y))
        draw.rectangle((x, y, x + 34, y + 34), fill=(0, 0, 0))
        draw.text((x + 9, y + 2), str(k + 1), fill=(255, 220, 80), font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=85)
    return out.getvalue(), len(frames)


def image_block(data, content_type):
    """Resize to the model's long-edge limit and return a base64 image block, or None."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((IMAGE_LONG_EDGE, IMAGE_LONG_EDGE))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(out.getvalue()).decode()}}
    except Exception as e:
        log.warning("image skipped (%s): %s", content_type, e)
        return None


# -------------------------------------------------------------- discord
def _discord_get(path):
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        return None
    req = urllib.request.Request(DISCORD_BASE + path, headers={"authorization": f"Bot {token}",
                                                                "user-agent": "DiscordBot (explain-bot, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning("discord GET %s -> %s (bot not in this server, or no Read Message History?)", path, e.code)
    except Exception as e:
        log.warning("discord GET %s failed: %s", path, e)
    return None


def _author(m):
    a = m.get("author") or {}
    return a.get("global_name") or a.get("username") or "?"


GIF_URL = re.compile(r"https?://(?:www\.)?(?:tenor\.com/(?:[a-z]{2}(?:-[A-Z]{2})?/)?view/|giphy\.com/gifs/"
                     r"|klipy\.com/(?:gifs|clips|stickers|ai-gifs|static-memes)/)([^\s/?#]+)")
GIF_MEDIA_HOSTS = {"static.klipy.com": "Klipy", "media.tenor.com": "Tenor", "c.tenor.com": "Tenor",
                   "media.giphy.com": "Giphy", "i.giphy.com": "Giphy"}
DISCORD_PROXY = re.compile(r"^https?://(?:images-ext-\d+|media)\.discordapp\.net/external/[^/]+/(https?)/(.+)$")


def unproxy(url):
    """Discord's media proxy wraps the original address: .../external/<sig>/https/static.klipy.com/x.mp4."""
    m = DISCORD_PROXY.match(url or "")
    return f"{m.group(1)}://{m.group(2)}" if m else url


def gif_host(url):
    """'Klipy' for a direct GIF/video file on a GIF site (also behind Discord's proxy), else None."""
    host = (urllib.parse.urlsplit(unproxy(url)).hostname or "").lower()
    return GIF_MEDIA_HOSTS.get(host) or next((v for k, v in GIF_MEDIA_HOSTS.items() if host.endswith("." + k)), None)


def gif_title(url):
    """'https://tenor.com/view/tony-soprano-stare-gif-123' -> 'tony soprano stare' (the uploader's own title)."""
    m = GIF_URL.match(url or "")
    if not m:
        return None
    words = [w for w in m.group(1).split("-") if w and not w.isdigit() and not re.fullmatch(r"[A-Za-z0-9]{12,}", w)]
    if words and words[-1].lower() == "gif":
        words.pop()
    return " ".join(words) or "untitled"


def _gif_text(text, keep_url=False):
    """Name GIF links the way people see them in Discord: [GIF: title] instead of an address."""
    text = GIF_URL.sub(lambda g: f"[GIF: {gif_title(g.group(0))}]" + (f" {g.group(0)}" if keep_url else ""), text)
    return URL_RE.sub(lambda u: (f"[GIF from {gif_host(u.group(0))}]" + (f" {u.group(0)}" if keep_url else ""))
                      if gif_host(u.group(0)) else u.group(0), text)


def _one_line(m, limit=MAX_CONTEXT_MSG_CHARS):
    text = _gif_text((m.get("content") or "").strip())
    for e in m.get("embeds") or []:
        if gif_title(e.get("url")) or e.get("type") == "gifv":
            content = m.get("content") or ""
            if not GIF_URL.search(content) and not any(gif_host(u) for u in URL_RE.findall(content)):
                provider = (e.get("provider") or {}).get("name")
                text += f"\n[GIF from {provider}]" if provider else "\n[GIF]"
            continue  # otherwise the content already shows it as [GIF: ...]
        bits = [e.get("title"), e.get("description"), e.get("url")]
        text += "\n" + " — ".join(b for b in bits if b)
    names = [a.get("filename") for a in m.get("attachments") or [] if a.get("filename")]
    if names:
        text += "\n[attachments: " + ", ".join(names) + "]"
    stickers = [s.get("name") for s in m.get("sticker_items") or [] if s.get("name")]
    if stickers:
        text += "\n[sticker: " + ", ".join(stickers) + "]"
    text = text.strip() or "[no text]"
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    ts = (m.get("timestamp") or "")[11:16]
    return (f"[{ts}] " if ts else "") + f"{_author(m)}: {text}"


def _history(channel_id, before=None, limit=10):
    if not channel_id or limit <= 0:
        return []
    q = f"?limit={min(50, limit)}" + (f"&before={before}" if before else "")
    msgs = _discord_get(f"/channels/{channel_id}/messages{q}") or []
    return list(reversed(msgs))  # oldest first


def _reply_chain(msg, channel_id):
    """Follow at most four ancestors, only inside the channel the user invoked us in."""
    seen, parents = {str(msg.get("id"))}, []
    for _ in range(4):
        reference = msg.get("message_reference") or {}
        ref_id = reference.get("message_id")
        if (not ref_id or str(ref_id) in seen or reference.get("type") == 1
                or str(reference.get("channel_id") or channel_id) != str(channel_id)):
            break
        seen.add(str(ref_id))
        parent = msg.get("referenced_message") or _discord_get(f"/channels/{channel_id}/messages/{ref_id}")
        if not parent:
            break
        parents.append(parent)
        msg = parent
    return list(reversed(parents))


# ------------------------------------------------------- reaction media
def _stickers(m):
    """Discord stickers as (url, type, info). Lottie stickers have no raster form; their name is in the text."""
    for st in m.get("sticker_items") or []:
        fmt, sid = int(st.get("format_type") or 1), st.get("id")
        if not sid or fmt == 3:
            continue
        ext = "gif" if fmt == 4 else "png"
        yield (f"https://media.discordapp.net/stickers/{sid}.{ext}", f"image/{ext}",
               {"caption": f"Discord sticker \"{st.get('name') or 'sticker'}\""})


def _embed_media(e):
    """Best media of one embed as (url, type, info): the GIF's video first, then its image or thumbnail."""
    title = gif_title(e.get("url"))
    provider = (e.get("provider") or {}).get("name") or gif_host((e.get("video") or {}).get("url"))
    gif_like = e.get("type") == "gifv" or title or gif_host((e.get("video") or {}).get("url"))
    if gif_like:
        caption = "GIF" + (f" from {provider}" if provider else "") + (f" titled \"{title}\"" if title and title != "untitled" else "")
    else:
        caption = e.get("title") or "Link preview image from the Discord message"
    info = {"caption": caption, "credit": provider or "Референс из сообщения", "source_url": e.get("url") or ""}
    video = (e.get("video") or {}).get("url")
    if video and gif_like:
        yield video, "video/mp4", info
    for key in ("image", "thumbnail"):
        u = (e.get(key) or {}).get("url")
        if u:
            yield u, "", info


# --------------------------------------------------------------- build
class AttachmentUnavailable(Exception):
    def __init__(self, name, reason):
        self.name = str(name or "file")[:120]
        self.reason = reason
        super().__init__(reason)


def _is_document(attachment):
    ctype = (attachment.get("content_type") or "").lower()
    name = (attachment.get("filename") or "").lower()
    return not (ctype.startswith(("image/", "video/")) or name.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".gif", ".apng", ".mp4", ".webm", ".mov")))


def collect(job):
    """Return (content_blocks, notes). content_blocks go into the user message; images come first."""
    images, texts, notes = [], [], []
    msg = job.get("message") or {}
    channel_id = job.get("channel_id")
    # A document attached to this request takes precedence over a quoted post and ambient chat.
    # If it is unreadable, fail before research/rendering rather than inventing a replacement topic.
    document_source = next((s for s in (job.get("request_message") or {}, msg)
                            if any(_is_document(a) for a in s.get("attachments") or [])), None)
    job["primary_document"] = document_source is not None

    # 1. conversation context (needs DISCORD_BOT_TOKEN, bot in the server, Message Content intent)
    n_ctx = max(0, min(50, int(job.get("context_messages", 10 if job.get("kind") == "message" else 0) or 0)))
    if document_source is not None:
        n_ctx = 0
    history, parents = [], []
    if job.get("kind") == "message" and msg.get("id") and document_source is None:
        parents = _reply_chain(msg, channel_id)
        if parents:
            texts.append("<replied_to oldest_first=\"true\">\n" + "\n".join(_one_line(m, 1500) for m in parents) + "\n</replied_to>")
        history = _history(channel_id, before=msg["id"], limit=n_ctx)
    anchor = job.get("context_before_id")
    if n_ctx > 0 and (job.get("kind") != "message" or (anchor and str(anchor) != str(msg.get("id")))):
        # Freeze the boundary at the mention, even if rendering starts much later.
        history += _history(channel_id, before=anchor, limit=n_ctx)
        if not history:
            notes.append("context")
    unique = {}
    for item in history:
        key = str(item.get("id"))
        if key == str(msg.get("id")) or (job.get("bot_user_id") and str((item.get("author") or {}).get("id")) == job["bot_user_id"]):
            continue
        unique[key] = item
    ctx_lines = [_one_line(m) for m in unique.values()]
    if ctx_lines:
        ctx = "\n".join(ctx_lines)[-MAX_CONTEXT_CHARS:]
        texts.append(f"<conversation_before oldest_first=\"true\">\n{ctx}\n</conversation_before>")

    # 2. the target message itself
    if job.get("kind") == "message" and (document_source is None or document_source is msg):
        body = _gif_text((msg.get("content") or "").strip(), keep_url=True)
        emb = []
        for e in msg.get("embeds") or []:
            if e.get("type") == "gifv" or gif_title(e.get("url")):
                continue
            emb.append(" — ".join(b for b in [e.get("title"), e.get("description"), e.get("url")] if b))
        if emb:
            body += "\n[embeds]\n" + "\n".join(emb)
        stickers = [st.get("name") for st in msg.get("sticker_items") or [] if st.get("name")]
        if stickers:
            body += "\n[sticker: " + ", ".join(stickers) + "]"
        texts.append(f"<post author=\"{_author(msg)}\">\n{body or '[no text]'}\n</post>")

    # 3. attachments: also retain media from the question and the quoted reply chain.
    # Keep the same total bounds as one post, with the target taking priority.
    # job["image_meta"][i] says where images[i] came from, for captions and source credits.
    meta, seen = [], set()
    job["image_meta"] = meta

    def add_image(data, ctype, info):
        """Stills go in as they are; GIFs, animated stickers and short videos become one numbered storyboard."""
        if len(images) >= MAX_IMAGES:
            return False
        ctype = (ctype or "").lower()
        board = storyboard(data, ctype) if ctype in VIDEO_TYPES or ctype.startswith("image/") else None
        if board:
            what = "video" if ctype in VIDEO_TYPES else "animation"
            info = {**info, "animated": board[1],
                    "caption": f"{info.get('caption') or 'Animated media'} — storyboard of {board[1]} frames of the "
                               f"{what} in time order (numbered, left to right, top to bottom)"}
            data, ctype = board[0], "image/jpeg"
        elif ctype in VIDEO_TYPES:
            return False
        key = _fingerprint(data)
        if key in seen:  # the same picture via a link preview and the link's own media
            return True
        blk = image_block(data, ctype)
        if blk:
            seen.add(key)
            images.append(blk)
            meta.append(info)
        return bool(blk)

    def fetch_media(url, ctype_hint, info, max_bytes=MAX_DOWNLOAD):
        try:
            data, ct = fetch(url, max_bytes=max_bytes)
            return add_image(data, ct or ctype_hint, info)
        except Exception as ex:
            log.warning("media %s skipped: %s", (url or "")[:80], ex)
            return False

    from_message = {"caption": "Reference supplied in the Discord conversation", "credit": "Референс из сообщения"}
    sources = [msg, job.get("request_message") or {}, *reversed(parents)]
    if document_source is not None:
        sources = [document_source]
    attachments = [a for source in sources for a in source.get("attachments") or []][:10]
    embeds = [e for source in sources for e in source.get("embeds") or []][:5]
    document_chars = 0
    for a in attachments:
        ctype = (a.get("content_type") or "").lower()
        name = a.get("filename") or "file"
        url = a.get("url") or a.get("proxy_url")
        if not url:
            if document_source is not None and _is_document(a):
                raise AttachmentUnavailable(name, "download")
            continue
        try:
            if ctype == "application/pdf" or name.lower().endswith(".pdf"):
                from pdf_inputs import MAX_CHARS, PDFUnreadable, read_pdf
                if (a.get("size") or 0) > MAX_DOWNLOAD:
                    raise AttachmentUnavailable(name, "too_large")
                data, _ = fetch(url)
                try:
                    pdf = read_pdf(data)
                except PDFUnreadable as exc:
                    raise AttachmentUnavailable(name, exc.reason) from exc
                document_chars += len(pdf["text"])
                if document_chars > MAX_CHARS:
                    raise AttachmentUnavailable(name, "pdf_limit")
                texts.append(f'<attachment name="{html.escape(name, quote=True)}" type="application/pdf" '
                             f'pages="{pdf["pages"]}">\n{pdf["text"]}\n</attachment>')
                for page, preview in pdf["previews"]:
                    add_image(preview, "image/jpeg", {"caption": f"{name}: PDF page {page}/{pdf['pages']}",
                                                       "credit": "Страница приложенного PDF"})
                log.info("PDF input: %d pages, %d text characters, %d previews", pdf["pages"], len(pdf["text"]), len(pdf["previews"]))
            elif ctype.startswith("image/") and len(images) < MAX_IMAGES:
                data, _ = fetch(url)
                add_image(data, ctype, from_message)
            elif ctype in VIDEO_TYPES and (a.get("size") or 0) <= MAX_DOWNLOAD and len(images) < MAX_IMAGES:
                data, _ = fetch(url)
                shown = add_image(data, ctype, {**from_message, "caption": f"Video {name} from the Discord conversation"})
                texts.append(f"<attachment name=\"{name}\" type=\"{ctype}\">"
                             f"{'[video: frames are attached as a storyboard image; the sound is not available]' if shown else '[not read]'}</attachment>")
            elif name.lower().endswith(TEXT_EXT) and (a.get("size") or 0) < 500_000:
                data, _ = fetch(url, max_bytes=500_000)
                if document_source is not None and not data.strip():
                    raise AttachmentUnavailable(name, "empty")
                texts.append(f"<attachment name=\"{name}\">\n{data.decode('utf-8', 'replace')[:MAX_FILE_CHARS]}\n</attachment>")
            else:
                if document_source is not None and _is_document(a):
                    raise AttachmentUnavailable(name, "unsupported")
                texts.append(f"<attachment name=\"{name}\" type=\"{ctype or '?'}\">[not read]</attachment>")
        except AttachmentUnavailable:
            raise
        except Exception as e:
            log.warning("attachment read failed: %s", type(e).__name__)
            if document_source is not None and _is_document(a):
                raise AttachmentUnavailable(name, "download") from e
    for source in sources:
        for url, ctype, extra in _stickers(source):
            fetch_media(url, ctype, {**from_message, **extra})

    # 4. linked pages (from the post, or from the query). Site readers return the post/article itself,
    # so "what does this mean + link" explains the content behind the link, not the URL.
    import links
    source = "\n".join(m.get("content") or "" for m in sources) + "\n" + (job.get("question") or "") + "\n" + (job.get("text") or "")
    queue = list(dict.fromkeys(u.rstrip(".,;:!?)»") for u in URL_RE.findall(source)))[:MAX_LINKS]
    opened, with_media = set(), set()
    while queue and len(opened) < MAX_LINKS + 1:  # one extra slot for a link found inside a linked post
        u = queue.pop(0)
        if u in opened:
            continue
        opened.add(u)
        try:
            page = links.read_link(u)
        except Exception as e:
            log.warning("link %s skipped: %s", u, e)
            texts.append(f"<linked_page url=\"{u}\">[could not open: {type(e).__name__}]</linked_page>")
            continue
        n_before = len(images)
        if page.get("image_data"):
            add_image(page["image_data"], page.get("image_type") or "image/*", page["image_meta"])
        for ref in page.get("images") or []:
            if len(images) >= MAX_IMAGES:
                break
            fetch_media(ref["url"], ref.get("type") or "", {k: ref.get(k, "") for k in ("caption", "credit", "source_url")})
        attached = len(images) - n_before
        if attached:
            with_media.add(u)
        note = f" attached_images=\"{attached}\"" if attached else ""
        if page.get("text"):
            texts.append(f"<linked_page url=\"{u}\" kind=\"{page.get('kind', 'web_page')}\"{note}>\n{page['text']}\n</linked_page>")
        queue += [x.rstrip(".,;:!?)»") for x in page.get("links") or []][:2]
        log.info("link %s read as %s (%d chars, %d images)", u, page.get("kind"), len(page.get("text") or ""), attached)

    for e in embeds:  # Discord link previews, image embeds and GIFs fill the remaining image slots
        if e.get("url") in with_media:
            continue
        for url, ctype, extra in _embed_media(e):
            if len(images) >= MAX_IMAGES or fetch_media(url, ctype, {**from_message, **extra}):
                break

    # 5. pictures, GIFs and stickers posted in the latest messages: "what did he mean by this?" needs to see them
    taken = 0
    for m in ([] if document_source is not None else list(unique.values())[::-1][:CONTEXT_MEDIA_WINDOW]):
        if taken >= CONTEXT_MEDIA or len(images) >= MAX_IMAGES:
            break
        ts = (m.get("timestamp") or "")[11:16]
        posted = {"caption": f"Posted by {_author(m)}" + (f" at [{ts}]" if ts else "") + " in the conversation",
                  "credit": "Референс из сообщения"}
        candidates = list(_stickers(m)) + [(a.get("url"), (a.get("content_type") or "").lower(), {})
                                           for a in m.get("attachments") or []
                                           if (a.get("content_type") or "").lower().startswith("image/")
                                           or ((a.get("content_type") or "").lower() in VIDEO_TYPES and (a.get("size") or 0) <= MAX_DOWNLOAD)]
        for e in m.get("embeds") or []:
            candidates += list(_embed_media(e))[:1]
        for url, ctype, extra in candidates:
            if url and fetch_media(url, ctype, {**posted, **extra, "caption": posted["caption"] + (
                    f"; {extra['caption']}" if extra.get("caption") else "")}):
                taken += 1
                break

    if meta:
        listing = "\n".join(f"{k}. {info.get('caption') or 'image'}" for k, info in enumerate(meta, 1))
        texts.append(f"<attached_images order=\"same as the images above\">\n{listing}\n</attached_images>")
    return images, texts, notes
