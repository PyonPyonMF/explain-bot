"""Collect everything Sonnet should see for a job: the target message, its images, linked pages,
text attachments, and (if a bot token is set) the conversation around it.

All of this is untrusted data. It goes into the prompt inside clearly marked tags.
"""
import base64
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
        pixels = list(im.convert("L").resize((8, 8)).getdata())
        avg = sum(pixels) / 64
        return "".join("1" if p > avg else "0" for p in pixels)
    except Exception:
        return hash(data)


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


def _one_line(m, limit=MAX_CONTEXT_MSG_CHARS):
    text = (m.get("content") or "").strip()
    for e in m.get("embeds") or []:
        bits = [e.get("title"), e.get("description"), e.get("url")]
        text += "\n" + " — ".join(b for b in bits if b)
    names = [a.get("filename") for a in m.get("attachments") or [] if a.get("filename")]
    if names:
        text += "\n[attachments: " + ", ".join(names) + "]"
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


# --------------------------------------------------------------- build
def collect(job):
    """Return (content_blocks, notes). content_blocks go into the user message; images come first."""
    images, texts, notes = [], [], []
    msg = job.get("message") or {}
    channel_id = job.get("channel_id")

    # 1. conversation context (needs DISCORD_BOT_TOKEN, bot in the server, Message Content intent)
    n_ctx = max(0, min(50, int(job.get("context_messages", 10 if job.get("kind") == "message" else 0) or 0)))
    history, parents = [], []
    if job.get("kind") == "message" and msg.get("id"):
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
    if job.get("kind") == "message":
        body = (msg.get("content") or "").strip()
        emb = []
        for e in msg.get("embeds") or []:
            emb.append(" — ".join(b for b in [e.get("title"), e.get("description"), e.get("url")] if b))
        if emb:
            body += "\n[embeds]\n" + "\n".join(emb)
        texts.append(f"<post author=\"{_author(msg)}\">\n{body or '[no text]'}\n</post>")

    # 3. attachments: also retain media from the question and the quoted reply chain.
    # Keep the same total bounds as one post, with the target taking priority.
    # job["image_meta"][i] says where images[i] came from, for captions and source credits.
    meta, seen = [], set()
    job["image_meta"] = meta

    def add_image(data, ctype, info):
        if len(images) >= MAX_IMAGES:
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

    from_message = {"caption": "Reference supplied in the Discord conversation", "credit": "Референс из сообщения"}
    sources = [msg, job.get("request_message") or {}, *reversed(parents)]
    attachments = [a for source in sources for a in source.get("attachments") or []][:10]
    embeds = [e for source in sources for e in source.get("embeds") or []][:5]
    for a in attachments:
        ctype = (a.get("content_type") or "").lower()
        name = a.get("filename") or "file"
        url = a.get("url") or a.get("proxy_url")
        if not url:
            continue
        try:
            if ctype.startswith("image/") and len(images) < MAX_IMAGES:
                data, _ = fetch(url)
                add_image(data, ctype, from_message)
            elif name.lower().endswith(TEXT_EXT) and (a.get("size") or 0) < 500_000:
                data, _ = fetch(url, max_bytes=500_000)
                texts.append(f"<attachment name=\"{name}\">\n{data.decode('utf-8', 'replace')[:MAX_FILE_CHARS]}\n</attachment>")
            else:
                texts.append(f"<attachment name=\"{name}\" type=\"{ctype or '?'}\">[not read]</attachment>")
        except Exception as e:
            log.warning("attachment %s skipped: %s", name, e)

    # 4. linked pages (from the post, or from the query). Site readers return the post/article itself,
    # so "what does this mean + link" explains the content behind the link, not the URL.
    import links
    source = "\n".join(m.get("content") or "" for m in sources) + "\n" + (job.get("question") or "") + "\n" + (job.get("text") or "")
    queue = list(dict.fromkeys(u.rstrip(".,;:!?)»") for u in URL_RE.findall(source)))[:MAX_LINKS]
    opened = set()
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
            add_image(page["image_data"], "image/*", page["image_meta"])
        for ref in page.get("images") or []:
            if len(images) >= MAX_IMAGES:
                break
            try:
                data, ct = fetch(ref["url"])
                add_image(data, ct, {k: ref.get(k, "") for k in ("caption", "credit", "source_url")})
            except Exception as ex:
                log.warning("linked image skipped: %s", ex)
        attached = len(images) - n_before
        note = f" attached_images=\"{attached}\"" if attached else ""
        if page.get("text"):
            texts.append(f"<linked_page url=\"{u}\" kind=\"{page.get('kind', 'web_page')}\"{note}>\n{page['text']}\n</linked_page>")
        queue += [x.rstrip(".,;:!?)»") for x in page.get("links") or []][:2]
        log.info("link %s read as %s (%d chars, %d images)", u, page.get("kind"), len(page.get("text") or ""), attached)

    for e in embeds:  # Discord link previews / image embeds fill the remaining image slots
        for key in ("image", "thumbnail"):
            u = (e.get(key) or {}).get("url")
            if u and len(images) < MAX_IMAGES:
                try:
                    data, ct = fetch(u)
                    add_image(data, ct, {"caption": e.get("title") or "Link preview image from the Discord message",
                                         "credit": (e.get("provider") or {}).get("name") or "Референс из сообщения",
                                         "source_url": e.get("url") or ""})
                    break
                except Exception as ex:
                    log.warning("embed image skipped: %s", ex)

    return images, texts, notes
