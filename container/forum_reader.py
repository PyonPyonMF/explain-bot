"""Bounded public forum navigation with attributed posts, quotes and current signatures."""
import html
import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

MAX_PAGES = 10
MAX_BYTES = 2_000_000
MAX_PAGE_CHARS = 16_000
DROP_PARAMS = {"s", "sid", "session", "session_id", "auth_key", "csrfkey", "hl"}


def canonical(url, base=""):
    value = str(url)
    # BeautifulSoup already decoded attributes. A second unescape corrupts '&section' into '§ion'.
    if "&amp;" in value or "&#" in value:
        value = html.unescape(value)
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(base, value))
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in DROP_PARAMS]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", urllib.parse.urlencode(pairs), parsed.fragment))


def _text(node, limit=2500):
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()[:limit] if node else ""


def _next(soup, url):
    candidates = []
    for anchor in soup.select('a[rel="next"], .pagination a, .ipsPagination a'):
        link = canonical(anchor.get("href", ""), url)
        if link and urllib.parse.urlsplit(link).hostname == urllib.parse.urlsplit(url).hostname and link != url:
            candidates.append({"url": link, "label": _text(anchor, 40)})
    return list({p["url"]: p for p in candidates}.values())[:8]


def parse_page(raw, url):
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.select("script,style,noscript"):
        node.decompose()
    title = _text(soup.select_one("h1") or soup.select_one(".ipsType_pagetitle") or soup.title, 180)
    site_title = _text(soup.title, 250)
    sections = list(dict.fromkeys(_text(a, 100) for a in soup.select(".breadcrumb a, .ipsBreadcrumb a") if _text(a, 100)))
    visible = _text(soup, 5000)
    if re.search(r"(?:не имеете прав|необходимо войти|слишком часто|подождите.*поиск|permission denied|sign in to search|rate limit)", visible, re.I):
        return {"kind": "forum_unavailable", "url": url, "title": title,
                "notice": "The page requires access or limits searches. Do not bypass it or interpret this as absence of information."}
    posts = []
    for block in soup.select(".post_block, article[data-post-id], .cPost")[:40]:
        body_node = block.select_one(".post.entry-content, [itemprop='commentText'], .cPost_contentWrap .ipsType_richText, .cooked")
        if not body_node:
            continue
        author = _text(block.select_one(".author.vcard, [itemprop='creator'], .ipsType_sectionHead a, .names .username"), 100)
        date = block.select_one("abbr.published, time[datetime]")
        published = (date.get("title") or date.get("datetime") or _text(date, 100)) if date else ""
        bookmark = block.select_one("a[rel='bookmark'], a[itemprop='replyToUrl']")
        link = canonical(bookmark.get("href"), url) if bookmark else url
        body = BeautifulSoup(str(body_node), "html.parser")
        quotes = []
        for quote in list(body.select("blockquote, div.blockquote")):
            if quote.parent is None:
                continue
            citation = quote.find_previous_sibling("p", class_="citation")
            quotes.append({"attribution": _text(citation, 150), "text": _text(quote, 1500)})
            quote.decompose()
            if citation:
                citation.decompose()
        for node in body.select(".signature, .edit, .edit_reason, .post_controls"):
            node.decompose()
        signature = _text(block.select_one(".signature"), 700)
        member_title = _text(block.select_one(".member_title"), 200)
        posts.append({"url": link, "author": author, "date": published, "text": _text(body, 4500),
                      "quotes": quotes[:3], "current_signature": signature, "current_member_title": member_title})
    if posts:
        return {"kind": "forum_topic", "url": url, "title": title, "site_title": site_title, "sections": sections,
                "posts": posts, "pages": _next(soup, url),
                "notice": "Only this page was read. Quotes belong to their cited authors, not necessarily the post author. "
                          "Signatures/member titles can change and are not evidence of their state on an old post's date. "
                          "Forum opinions or accusations are not established facts."}
    # A public profile: retain the handle/title and on-forum navigation, not contact/location fields.
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    if "showuser" in params:
        name = _text(soup.select_one("#profile_name, .ipsType_pagetitle, h1"), 120)
        signature = _text(soup.select_one("#profile_signature, .signature"), 1000)
        return {"kind": "forum_profile", "url": url, "title": title, "public_handle": name,
                "current_signature": signature,
                "notice": "This is a public forum persona, not a real-world identity. No contact or location fields were retained."}
    matches = []
    for anchor in soup.select('a[href*="showtopic="], a[href*="showuser="], a[href*="/topic/"], a[href*="/t/"]'):
        link = canonical(anchor.get("href"), url)
        label = _text(anchor, 160)
        if not link or not label or label in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "»", "→"):
            continue
        if urllib.parse.urlsplit(link).hostname != urllib.parse.urlsplit(url).hostname:
            continue
        args = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
        if args.get("view") == "getlastpost":
            continue
        matches.append({"url": link, "title": label, "kind": "profile" if "showuser" in args else "topic"})
    unique = {}
    for match in matches:
        args = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(match["url"]).query))
        identity = (match["kind"], args.get("showtopic") or args.get("showuser") or match["url"])
        unique.setdefault(identity, match)
    visible = _text(soup, 5000)
    blocked = bool(re.search(r"(?:не имеете прав|необходимо войти|слишком часто|подождите.*поиск|permission denied|sign in to search|rate limit)", visible, re.I))
    return {"kind": "forum_search" if "search" in url else "forum_index", "url": url, "title": title,
            "matches": list(unique.values())[:50], "pages": _next(soup, url), "blocked": blocked,
            "notice": "Search/index links are navigation, not evidence for claims. Open relevant topics. "
                      "An empty or limited search does not establish that something never existed."}


def bounded(result, focus=""):
    result = dict(result)
    if result.get("posts"):
        posts = result["posts"]
        terms = set(re.findall(r"[\w-]{3,}", focus.lower()))
        scores = [sum(term in json.dumps(post, ensure_ascii=False).lower() for term in terms) for post in posts]
        indices = sorted(range(len(posts)), key=lambda i: (-scores[i], i))[:10] if any(scores) else list(range(min(10, len(posts))))
        indices = list(dict.fromkeys(indices + [0]))
        selected = {}
        for index in indices:
            post = posts[index]
            if len(json.dumps(list(selected.values()) + [post], ensure_ascii=False)) > MAX_PAGE_CHARS:
                continue
            selected[index] = post
        result["posts"] = [selected[i] for i in sorted(selected)]
        result["posts_on_page"] = len(posts)
        result["partial"] = len(selected) < len(posts)
    return result


class Session:
    def __init__(self, allowed_urls=(), max_pages=MAX_PAGES, deadline=None):
        from inputs import _SafeRedirect
        self.allowed = set()
        self.authorize(allowed_urls)
        self.max_pages, self.used = max_pages, 0
        self.deadline = deadline or time.monotonic() + 90
        self.cache = {}
        self.opener = urllib.request.build_opener(_SafeRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def authorize(self, urls):
        for url in urls:
            clean = canonical(url)
            if clean:
                self.allowed.add(urllib.parse.urlsplit(clean).hostname)

    def fetch(self, url):
        from inputs import _check_host, UA
        url = canonical(url)
        if not url or urllib.parse.urlsplit(url).hostname not in self.allowed:
            raise ValueError("Forum domain must come from the user's source or an observed public web-search result")
        if url in self.cache:
            return self.cache[url]
        if self.used >= self.max_pages or time.monotonic() >= self.deadline:
            raise ValueError("Forum reading budget reached; do not claim to have checked the whole forum")
        _check_host(url)
        self.used += 1
        request = urllib.request.Request(url, headers={"user-agent": UA})
        with self.opener.open(request, timeout=max(1, min(15, self.deadline-time.monotonic()))) as response:
            final = canonical(response.geturl())
            if not final or urllib.parse.urlsplit(final).hostname not in self.allowed:
                raise ValueError("Forum redirected outside the authorized source domain")
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Forum page exceeds reading size limit")
        self.cache[url] = raw
        return raw

    def read(self, url, focus=""):
        url = canonical(url)
        if not url:
            raise ValueError("Invalid forum URL")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if any(str(params.get(k, "")).lower() in {"delete", "del", "logout", "login", "register", "moderate", "unsubscribe", "track"}
               for k in ("do", "act", "action")):
            raise ValueError("Only public read/search forum operations are allowed")
        return bounded(parse_page(self.fetch(url), url), focus)

    def search(self, base_url, query, kind="posts"):
        base_url = canonical(base_url)
        if not base_url:
            raise ValueError("Invalid forum URL")
        parsed = urllib.parse.urlsplit(base_url)
        root = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        raw = self.fetch(root)
        soup = BeautifulSoup(raw, "html.parser")
        form = next((f for f in soup.find_all("form", action=True) if "module=search" in f["action"]), None)
        anchor = soup.select_one('a[href*="module=search"][href*="search_in=forums"]') or soup.select_one('a[href*="module=search"]')
        action = canonical(form["action"] if form else anchor["href"], root) if form or anchor else None
        if action:
            target = urllib.parse.urlsplit(action)
            params = dict(urllib.parse.parse_qsl(target.query))
            params.update(app="core", module="search", do="search", fromMainBar="1", search_term=str(query)[:180],
                          search_app="members" if kind == "members" else "forums")
            url = urllib.parse.urlunsplit((target.scheme, target.netloc, target.path, urllib.parse.urlencode(params), ""))
            result = self.read(url, query)
            if result.get("matches"):
                wanted = "profile" if kind == "members" else "topic"
                result["matches"] = [m for m in result["matches"] if m["kind"] == wanted][:18]
            result.update(query=query, search_kind=kind)
            return result
        return {"kind": "unsupported_forum_search", "url": root,
                "notice": "No supported public forum search form. Use site-scoped web search and forum_read for topic URLs; do not bypass login."}


TOOLS = [
    {"name": "forum_search", "description": "Search a public forum using its own search form, including old posts or public member handles not indexed well on the web. The domain must be supplied by the user or observed in web search. Guest access only; no login bypass. Open returned topics with forum_read.",
     "input_schema": {"type": "object", "properties": {"base_url": {"type": "string"}, "query": {"type": "string"},
                      "kind": {"type": "string", "enum": ["posts", "members"]}}, "required": ["base_url", "query"]}},
    {"name": "forum_read", "description": "Read a public forum topic page, preserving post authors, dates, quotations, and separate current signatures. Follow returned pagination to inspect more pages; pass exact handles/terms as focus to prioritize relevant posts. Does not search an entire forum or reveal private profiles.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "focus": {"type": "string"}}, "required": ["url"]}},
]
