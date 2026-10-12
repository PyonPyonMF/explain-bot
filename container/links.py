"""Read what is behind a link the way a person sees it, not the HTML shell a bot gets.

X/Twitter and its embed mirrors (fixvx, fxtwitter, vxtwitter, ...) render posts with JavaScript, so a plain
download returns an empty page and the model ends up explaining the URL itself. Known sites therefore go
through their public JSON/embed endpoints; everything else uses a readable-text extractor.

read_link(url) returns {"kind", "text", "images": [{"url", "caption", "credit"}], "links": [...]}.
All of it is untrusted data.
"""
import html
import json
import re
import urllib.parse
from html.parser import HTMLParser

from inputs import MAX_LINK_CHARS, fetch, final_url

DISCORDBOT_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
MAX_JSON = 4 * 1024 * 1024
MAX_ARTICLE_CHARS = 40000  # long-reads are the whole point of the link; about 10k tokens
MAX_ARTICLE_IMAGES = 3
URL_RE = re.compile(r"https?://[^\s<>()\"'`]+")


def _host(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return re.sub(r"^(www|mobile|m|d|g|old|new|np|music)\.", "", host)


def _json(url, **kwargs):
    raw, _ = fetch(url, max_bytes=MAX_JSON, timeout=12, **kwargs)
    return json.loads(raw)


def _clip(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(n):
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else None


# --------------------------------------------------------------- X / Twitter
X_HOSTS = {"x.com", "twitter.com", "fxtwitter.com", "fixupx.com", "fixvx.com", "vxtwitter.com", "twittpr.com",
           "xcancel.com", "nitter.net", "nitter.poast.org"}
X_PATH = re.compile(r"^/(?:(?P<user>[A-Za-z0-9_]{1,15})|i(?:/web)?)/status(?:es)?/(?P<id>\d{5,25})")


def _x_match(url):
    host = _host(url)
    if host not in X_HOSTS and not host.startswith("nitter."):
        return None
    m = X_PATH.match(urllib.parse.urlsplit(url).path)
    return (m.group("user") or "i", m.group("id")) if m else None


def _article(article):
    """Render an X Article (Draft.js blocks) as readable text and list its pictures in reading order."""
    content = article.get("content") or {}
    entity_map = content.get("entityMap") or {}
    if isinstance(entity_map, list):
        entity_map = {str(e.get("key")): e.get("value") or {} for e in entity_map}
    media = {}
    for m in article.get("media_entities") or []:
        info = m.get("media_info") or {}
        url = info.get("original_img_url") or (info.get("preview_image") or {}).get("original_img_url")
        if url:
            media[str(m.get("media_id"))] = ("animated image" if info.get("__typename") == "ApiGif" else "image", url)
    lines, images, number = [], [], 0
    prefix = {"header-one": "# ", "header-two": "## ", "header-three": "### ", "unordered-list-item": "- ",
              "blockquote": "> "}
    for block in content.get("blocks") or []:
        kind, text = block.get("type"), (block.get("text") or "").rstrip()
        entities = [entity_map.get(str(r.get("key"))) or {} for r in block.get("entityRanges") or []]
        if kind == "atomic":
            for entity in entities:
                data = entity.get("data") or {}
                if entity.get("type") == "MEDIA":
                    for item in data.get("mediaItems") or []:
                        what, url = media.get(str(item.get("mediaId")), ("image", None))
                        number += 1
                        caption = (data.get("caption") or "").strip()
                        lines.append(f"[{what.capitalize()} {number}" + (f": {caption}" if caption else "") + "]")
                        if url:
                            images.append({"url": url, "number": number, "caption": caption, "what": what})
                elif entity.get("type") == "MARKDOWN" and data.get("markdown"):
                    lines.append(data["markdown"].strip())
                elif entity.get("type") == "TWEET" and data.get("tweetId"):
                    lines.append(f"[Embedded post: https://x.com/i/status/{data['tweetId']}]")
                elif entity.get("type") == "DIVIDER":
                    lines.append("---")
            continue
        if not text.strip():
            continue
        if kind == "ordered-list-item":
            line = "1. " + text
        elif kind == "code-block":
            line = "```\n" + text + "\n```"
        else:
            line = prefix.get(kind, "") + text
        urls = [(e.get("data") or {}).get("url") for e in entities if e.get("type") == "LINK"]
        if any(urls):
            line += " (links: " + ", ".join(u for u in urls if u) + ")"
        lines.append(line)
    body = "\n".join(lines).strip()
    if len(body) > MAX_ARTICLE_CHARS:
        body = body[:MAX_ARTICLE_CHARS] + f"\n[Article cut here: {MAX_ARTICLE_CHARS} of {len(body)} characters shown]"
    cover = (article.get("cover_media") or {}).get("media_info") or {}
    return body, images, cover.get("original_img_url")


def _fx_post(t, depth=0):
    """Normalise one FxTwitter post (also used for quoted posts)."""
    author = t.get("author") or {}
    media = []
    for item in (t.get("media") or {}).get("all") or []:
        kind = item.get("type") or "photo"
        url = item.get("url") if kind == "photo" else item.get("thumbnail_url")
        media.append({"type": kind, "url": url, "alt": item.get("altText") or "", "duration": item.get("duration")})
    poll = [(c.get("label"), c.get("percentage")) for c in (t.get("poll") or {}).get("choices") or []]
    note = t.get("community_note")
    article = t.get("article") or {}
    body, article_images, cover = _article(article) if article.get("content") else ("", [], None)
    return {
        "name": author.get("name"), "handle": author.get("screen_name"), "date": t.get("created_at"),
        "text": t.get("text") or (t.get("raw_text") or {}).get("text") or "",
        "reply_to": t.get("replying_to"), "media": media, "poll": poll, "url": t.get("url"),
        "note": note.get("text") if isinstance(note, dict) else note if isinstance(note, str) else None,
        "article": {"title": article.get("title") or "", "preview": article.get("preview_text") or "",
                    "body": body, "images": article_images, "cover": cover} if article else None,
        "stats": {"likes": t.get("likes"), "reposts": t.get("retweets"), "replies": t.get("replies"),
                  "views": t.get("views")},
        "quote": _fx_post(t["quote"], depth + 1) if depth == 0 and isinstance(t.get("quote"), dict) else None,
    }


def _vx_post(t, depth=0):
    media = []
    for item in t.get("media_extended") or []:
        kind = item.get("type") or "image"
        kind = "photo" if kind == "image" else kind
        url = item.get("url") if kind == "photo" else item.get("thumbnail_url")
        seconds = (item.get("duration_millis") or 0) / 1000 or None
        media.append({"type": kind, "url": url, "alt": item.get("altText") or "", "duration": seconds})
    return {
        "name": t.get("user_name"), "handle": t.get("user_screen_name"), "date": t.get("date"),
        "text": t.get("text") or "", "reply_to": t.get("replyingTo"), "media": media, "poll": [], "note": None,
        "url": t.get("tweetURL"), "article": None,
        "stats": {"likes": t.get("likes"), "reposts": t.get("retweets"), "replies": t.get("replies"), "views": None},
        "quote": _vx_post(t["qrt"], depth + 1) if depth == 0 and isinstance(t.get("qrt"), dict) else None,
    }


def _x_text(post, label="X (Twitter) post"):
    who = " ".join(v for v in (post["name"], f"(@{post['handle']})" if post["handle"] else None) if v)
    lines = [f"{label} by {who or 'unknown author'}" + (f", {post['date']}" if post["date"] else "")]
    if post["reply_to"]:
        lines.append(f"In reply to @{post['reply_to']}")
    article = post["article"]
    text = post["text"].strip()
    if article:  # the post text is then only links to the article itself
        text = " ".join(w for w in text.split() if "/i/article/" not in w)
    lines.append("Text:\n" + (text or "[no text]"))
    if article and article["body"]:
        attached = min(len(article["images"]), MAX_ARTICLE_IMAGES)
        lines.append(f"<x_article title=\"{article['title']}\">\n# {article['title']}\n\n{article['body']}\n</x_article>"
                     + (f"\nThe first {attached} article image(s) are attached." if attached else ""))
    elif article:
        lines.append("Attached article (only its preview is available): "
                     + " — ".join(v for v in (article["title"], article["preview"]) if v))
    for k, item in enumerate(post["media"], 1):
        bits = [item["type"]]
        if item["duration"]:
            bits.append(f"{int(item['duration']) // 60}:{int(item['duration']) % 60:02d}")
        if item["alt"]:
            bits.append(f"alt text: {item['alt']}")
        if item["type"] != "photo":
            bits.append("only its preview frame is attached")
        lines.append(f"Media {k}: " + ", ".join(bits))
    if post["poll"]:
        lines.append("Poll: " + "; ".join(f"{label} — {pct}%" for label, pct in post["poll"]))
    if post["note"]:
        lines.append("Community note (added by X readers): " + post["note"])
    stats = [f"{name} {_count(v)}" for name, v in post["stats"].items() if _count(v)]
    if stats:
        lines.append("Engagement: " + ", ".join(stats))
    if post["quote"]:
        lines.append("<quoted_post>\n" + _x_text(post["quote"], "Quoted post") + "\n</quoted_post>")
    return "\n".join(lines)


def _x_images(post, source_url):
    out = []
    article, credit = post["article"], f"@{post['handle']} (X)" if post["handle"] else "X post"
    if article:  # the article's own diagrams explain more than its cover
        for image in article["images"][:MAX_ARTICLE_IMAGES]:
            caption = f"{image['what'].capitalize()} {image['number']} of the X article \"{article['title']}\""
            out.append({"url": image["url"], "caption": caption + (f": {image['caption']}" if image["caption"] else ""),
                        "credit": credit, "source_url": post["url"] or source_url})
        if article["cover"]:
            out.append({"url": article["cover"], "caption": f"Cover of the X article \"{article['title']}\"",
                        "credit": credit, "source_url": post["url"] or source_url})
    for p in (post, post["quote"]) if post["quote"] else (post,):
        credit = f"@{p['handle']} (X)" if p["handle"] else "X post"
        for item in p["media"]:
            if item["url"]:
                what = "Photo" if item["type"] == "photo" else "Video frame"
                out.append({"url": item["url"], "caption": f"{what} from the linked X post by {credit}",
                            "credit": credit, "source_url": p["url"] or source_url})
    return out


def read_x(url):
    match = _x_match(url)
    if not match:
        return None
    user, status = match
    post = None
    try:
        data = _json(f"https://api.fxtwitter.com/{user}/status/{status}")
        if isinstance(data.get("tweet"), dict):
            post = _fx_post(data["tweet"])
    except Exception:
        post = None
    if post is None:  # second, independent mirror
        post = _vx_post(_json(f"https://api.vxtwitter.com/{user}/status/{status}"))
    post["url"] = post["url"] or f"https://x.com/{user}/status/{status}"
    links = URL_RE.findall(post["text"]) + (URL_RE.findall(post["quote"]["text"]) if post["quote"] else [])
    return {"kind": "x_post", "text": _x_text(post), "images": _x_images(post, url),
            "links": [u for u in links if _host(u) not in X_HOSTS and _host(u) != "t.co"]}


# ------------------------------------------------------------------ Telegram
class _TelegramParser(HTMLParser):
    VOID = {"br", "img", "hr", "wbr", "input", "meta", "link", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.owner, self.images, self._depth, self._target = [], [], [], 0, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if self._target:
            self._depth += tag not in self.VOID
            if tag == "br":
                self.text.append("\n")
            return
        if "tgme_widget_message_text" in cls and tag == "div":
            self._target, self._depth = "text", 1
        elif "tgme_widget_message_owner_name" in cls:
            self._target, self._depth = "owner", 1
        for marker in ("tgme_widget_message_photo_wrap", "tgme_widget_message_video_thumb", "link_preview_image"):
            if marker in cls:
                m = re.search(r"background-image:url\('([^']+)'\)", a.get("style") or "")
                if m:
                    self.images.append(("photo" if "photo" in marker else "preview", m.group(1)))

    def handle_endtag(self, tag):
        if self._target and tag not in self.VOID:
            self._depth -= 1
            if self._depth <= 0:
                self._target = None

    def handle_data(self, data):
        if self._target == "text":
            self.text.append(data)
        elif self._target == "owner":
            self.owner.append(data)


def read_telegram(url):
    if _host(url) not in ("t.me", "telegram.me"):
        return None
    m = re.match(r"^/(?:s/)?([A-Za-z0-9_]{4,32})/(\d+)", urllib.parse.urlsplit(url).path)
    if not m:
        return None
    channel, post_id = m.groups()
    raw, _ = fetch(f"https://t.me/{channel}/{post_id}?embed=1&mode=tme", max_bytes=MAX_JSON, timeout=12)
    p = _TelegramParser()
    p.feed(raw.decode("utf-8", "replace"))
    text = re.sub(r"\n{3,}", "\n\n", "".join(p.text)).strip()
    if not text and not p.images:
        return None
    owner = "".join(p.owner).strip() or channel
    source = f"https://t.me/{channel}/{post_id}"
    images = [{"url": u, "caption": f"{'Photo' if kind == 'photo' else 'Preview'} from the Telegram post in {owner}",
               "credit": f"Telegram: {owner}", "source_url": source} for kind, u in p.images]
    return {"kind": "telegram_post", "images": images, "links": URL_RE.findall(text),
            "text": f"Telegram post in {owner} (t.me/{channel})\nText:\n{text or '[no text]'}"
                    + (f"\nMedia: {len(p.images)} image(s)" if p.images else "")}


# -------------------------------------------------------------------- Reddit
REDDIT_HOSTS = {"reddit.com", "redd.it", "rxddit.com", "vxreddit.com"}


def read_reddit(url):
    if _host(url) not in REDDIT_HOSTS:
        return None
    path = urllib.parse.urlsplit(url).path
    if _host(url) == "redd.it" or re.match(r"^/r/[^/]+/s/", path):
        path = urllib.parse.urlsplit(final_url(url)).path  # share links redirect to the post
    m = re.search(r"/comments/([a-z0-9]{3,12})", path)
    if not m:
        return None
    data = _json(f"https://www.reddit.com/comments/{m.group(1)}.json?limit=6&depth=1&raw_json=1")
    post = data[0]["data"]["children"][0]["data"]
    lines = [f"Reddit post in r/{post.get('subreddit')} by u/{post.get('author')}, score {post.get('score')}",
             f"Title: {post.get('title')}"]
    if post.get("selftext"):
        lines.append("Text:\n" + _clip(post["selftext"], 3000))
    if post.get("url") and not post.get("is_self"):
        lines.append(f"Linked: {post['url']}")
    comments = [c["data"] for c in data[1]["data"]["children"] if c.get("kind") == "t1"][:5]
    if comments:
        lines.append("Top comments:\n" + "\n".join(
            f"- u/{c.get('author')} ({c.get('score')}): {_clip(c.get('body'), 400)}" for c in comments))
    images, source = [], "https://www.reddit.com" + (post.get("permalink") or "")
    for item in (post.get("media_metadata") or {}).values():
        u = ((item or {}).get("s") or {}).get("u")
        if u:
            images.append(u)
    for item in (post.get("preview") or {}).get("images") or []:
        images.append(((item or {}).get("source") or {}).get("url"))
    if post.get("post_hint") == "image" and post.get("url"):
        images.insert(0, post["url"])
    credit = f"u/{post.get('author')} (Reddit)"
    return {"kind": "reddit_post", "text": "\n".join(lines),
            "images": [{"url": u, "caption": f"Image from the Reddit post by {credit}", "credit": credit,
                        "source_url": source} for u in dict.fromkeys(i for i in images if i)],
            "links": [post["url"]] if post.get("url") and not post.get("is_self") and post.get("post_hint") != "image" else []}


# ------------------------------------------------------------------- YouTube
def _youtube_id(url):
    p = urllib.parse.urlsplit(url)
    host = _host(url)
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        vid = urllib.parse.parse_qs(p.query).get("v", [""])[0]
        m = re.match(r"^/(?:shorts|live|embed|v)/([^/?#]+)", p.path)
        vid = vid or (m.group(1) if m else "")
    else:
        return None
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or "") else None


def read_youtube(url):
    vid = _youtube_id(url)
    if not vid:
        return None
    watch = f"https://www.youtube.com/watch?v={vid}"
    meta = _json("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(watch, safe=""))
    lines = [f"YouTube video: {meta.get('title')}", f"Channel: {meta.get('author_name')}"]
    try:  # description and length live in the page's player JSON; the consent cookie avoids the EU interstitial
        raw, _ = fetch(watch, max_bytes=4 * 1024 * 1024, timeout=12,
                       headers={"accept-language": "en", "cookie": "CONSENT=YES+1; SOCS=CAI"})
        page = raw.decode("utf-8", "replace")
        m = re.search(r'"lengthSeconds":"(\d+)"', page)
        if m:
            lines.append(f"Length: {int(m.group(1)) // 60}:{int(m.group(1)) % 60:02d}")
        m = re.search(r'"shortDescription":("(?:[^"\\]|\\.)*")', page)
        if m:
            lines.append("Description:\n" + _clip(json.loads(m.group(1)), 3000))
    except Exception:
        pass
    lines.append("The video itself is not transcribed: only its title, description and thumbnail are available.")
    credit = f"{meta.get('author_name') or 'YouTube'} (YouTube)"
    return {"kind": "youtube_video", "text": "\n".join(lines), "links": [],
            "images": [{"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "caption": "Thumbnail of the linked YouTube video",
                        "credit": credit, "source_url": watch}]}


# ------------------------------------------------------------------- generic
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "template"}
    MAIN = {"article", "main"}
    META = ("og:title", "og:description", "description", "twitter:description", "og:site_name",
            "og:image", "twitter:image", "og:image:alt", "og:video", "og:video:secure_url", "og:video:url", "keywords")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.main, self.skip, self.in_main = [], [], 0, 0
        self.title, self._in_title, self.meta = "", False, {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.MAIN:
            self.in_main += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in self.META and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div", "pre", "blockquote"):
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.MAIN and self.in_main:
            self.in_main -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self._emit(data)

    def _emit(self, s):
        self.parts.append(s)
        if self.in_main:
            self.main.append(s)


def _squash(parts):
    body = re.sub(r"[ \t\r\f\v]+", " ", "".join(parts))
    return re.sub(r"\n\s*\n+", "\n", body).strip()


def _parse_html(raw):
    p = _TextExtractor()
    p.feed(raw.decode("utf-8", errors="replace"))
    main = _squash(p.main)
    body = main if len(main) > 400 else _squash(p.parts)  # prefer the article body over menus and sidebars
    return p, body


def read_generic(url):
    data, ctype = fetch(url, max_bytes=3 * 1024 * 1024)
    if ctype.startswith("image/"):
        return {"kind": "image", "text": "", "links": [], "image_data": data, "image_type": ctype,
                "images": [], "image_meta": {"caption": "Image from the shared link", "credit": _host(url), "source_url": url}}
    if ctype not in ("text/html", "application/xhtml+xml", "") and not ctype.startswith("text/"):
        return {"kind": "file", "text": f"[{url}: content type {ctype}, not read]", "images": [], "links": []}
    if ctype == "text/plain":
        return {"kind": "text", "text": data.decode("utf-8", errors="replace")[:MAX_LINK_CHARS], "images": [], "links": []}
    p, body = _parse_html(data)
    if len(body) < 300:
        # A JavaScript shell. Many sites serve the same preview Discord shows to its own crawler.
        try:
            data2, ctype2 = fetch(url, max_bytes=3 * 1024 * 1024, headers={"user-agent": DISCORDBOT_UA})
            if ctype2 in ("text/html", "application/xhtml+xml", ""):
                p2, body2 = _parse_html(data2)
                if len(body2) + len(" ".join(p2.meta.values())) > len(body) + len(" ".join(p.meta.values())):
                    p, body = p2, body2
        except Exception:
            pass
    head = [p.title.strip()] + [p.meta[k] for k in ("og:site_name", "og:title", "og:description", "description",
                                                    "twitter:description") if k in p.meta]
    text = html.unescape("\n".join(dict.fromkeys(h.strip() for h in head if h and h.strip())) + "\n\n" + body).strip()
    if len(body) < 300:
        text += "\n[Only the page preview could be read; the page itself is rendered by JavaScript.]"
    images = []
    image = p.meta.get("og:image") or p.meta.get("twitter:image")
    if image:
        image = urllib.parse.urljoin(url, html.unescape(image))
        images.append({"url": image, "caption": p.meta.get("og:image:alt") or f"Preview image of {_host(url)}",
                       "credit": p.meta.get("og:site_name") or _host(url), "source_url": url})
    return {"kind": "web_page", "text": text[:MAX_LINK_CHARS], "images": images, "links": []}


# ---------------------------------------------------------------- GIF sites
def read_gif(url):
    """Tenor/Giphy pages: the uploader's title and tags say which film/meme a reaction GIF comes from."""
    from inputs import gif_title
    title = gif_title(url)
    if not title:
        return None
    site = "Tenor" if "tenor.com" in url else "Giphy"
    lines, images = [f"Reaction GIF on {site}" + (f", titled \"{title}\"" if title != "untitled" else "")], []
    try:
        raw, _ = fetch(url, max_bytes=3 * 1024 * 1024)
        p, _ = _parse_html(raw)
        if p.meta.get("og:title"):
            lines.append("Page title: " + p.meta["og:title"])
        if p.meta.get("og:description"):
            lines.append("Description: " + p.meta["og:description"])
        if p.meta.get("keywords"):
            lines.append("Tags: " + p.meta["keywords"][:300])
        media = (p.meta.get("og:video:secure_url") or p.meta.get("og:video") or p.meta.get("og:video:url")
                 or p.meta.get("og:image"))
        if media:
            media = urllib.parse.urljoin(url, html.unescape(media))
            images.append({"url": media, "caption": f"GIF from {site}" + (f" titled \"{title}\"" if title != "untitled" else ""),
                           "credit": site, "source_url": url, "type": "video/mp4" if ".mp4" in media else ""})
    except Exception:
        pass  # the title from the address still says what the GIF is; the Discord embed brings the frames
    lines.append("This is a reaction GIF: its frames are attached as a storyboard when available.")
    return {"kind": "gif", "text": "\n".join(lines), "images": images, "links": []}


READERS = (read_x, read_telegram, read_reddit, read_youtube, read_gif)


def read_link(url):
    """Site-specific reader first; any failure there falls back to the generic page reader."""
    for reader in READERS:
        try:
            result = reader(url)
        except Exception:
            result = None
        if result:
            limit = MAX_ARTICLE_CHARS + MAX_LINK_CHARS if "<x_article" in result["text"] else MAX_LINK_CHARS
            result["text"] = result["text"][:limit]
            return result
    return read_generic(url)


