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
from html.parser import HTMLParser

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


# --------------------------------------------------------------- images
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


# ---------------------------------------------------------------- pages
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip, self.title, self._in_title, self.meta = [], 0, "", False, {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:description", "description", "twitter:description") and a.get("content"):
                self.meta[key] = a["content"]
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div", "pre"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.parts.append(data)


def page_text(url):
    data, ctype = fetch(url, max_bytes=3 * 1024 * 1024)
    if ctype.startswith("image/"):
        return None, image_block(data, ctype)
    if ctype not in ("text/html", "application/xhtml+xml", "text/plain", "") and not ctype.startswith("text/"):
        return f"[{url}: content type {ctype}, not read]", None
    text = data.decode("utf-8", errors="replace")
    if ctype == "text/plain":
        return text[:MAX_LINK_CHARS], None
    p = _TextExtractor()
    p.feed(text)
    body = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    head = [p.title.strip()] + [p.meta[k] for k in ("og:title", "og:description", "description") if k in p.meta]
    out = "\n".join(dict.fromkeys(h for h in head if h)) + "\n\n" + body
    return html.unescape(out)[:MAX_LINK_CHARS], None


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
                blk = image_block(data, ctype)
                if blk:
                    images.append(blk)
            elif name.lower().endswith(TEXT_EXT) and (a.get("size") or 0) < 500_000:
                data, _ = fetch(url, max_bytes=500_000)
                texts.append(f"<attachment name=\"{name}\">\n{data.decode('utf-8', 'replace')[:MAX_FILE_CHARS]}\n</attachment>")
            else:
                texts.append(f"<attachment name=\"{name}\" type=\"{ctype or '?'}\">[not read]</attachment>")
        except Exception as e:
            log.warning("attachment %s skipped: %s", name, e)
    for e in embeds:  # images in link previews / image embeds
        for key in ("image", "thumbnail"):
            u = (e.get(key) or {}).get("url")
            if u and len(images) < MAX_IMAGES:
                try:
                    data, ct = fetch(u)
                    blk = image_block(data, ct)
                    if blk:
                        images.append(blk)
                    break
                except Exception as ex:
                    log.warning("embed image skipped: %s", ex)

    # 4. linked pages (from the post, or from the query)
    source = "\n".join(m.get("content") or "" for m in sources) + "\n" + (job.get("question") or "") + "\n" + (job.get("text") or "")
    urls = list(dict.fromkeys(u.rstrip(".,;:!?)»") for u in URL_RE.findall(source)))[:MAX_LINKS]
    for u in urls:
        try:
            txt, img = page_text(u)
            if img and len(images) < MAX_IMAGES:
                images.append(img)
            if txt:
                texts.append(f"<linked_page url=\"{u}\">\n{txt}\n</linked_page>")
        except Exception as e:
            log.warning("link %s skipped: %s", u, e)
            texts.append(f"<linked_page url=\"{u}\">[could not open: {type(e).__name__}]</linked_page>")

    return images, texts, notes
