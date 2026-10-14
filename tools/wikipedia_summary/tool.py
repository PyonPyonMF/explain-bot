"""Lead summary of one Wikipedia article through the public REST API."""
import urllib.parse


def run(args, ctx):
    language = args["language"]
    title = " ".join(args["title"].split()).replace(" ", "_")
    url = f"https://{language}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title, safe='')}?redirect=true"
    response = ctx.fetch(url)
    if response.status == 404:
        ctx.fail(f"not_found: no {language} Wikipedia article titled {args['title']!r}")
    if not response.ok:
        ctx.fail(f"unavailable: Wikipedia answered HTTP {response.status}")
    page = response.json()
    extract = " ".join(str(page.get("extract") or "").split())
    if len(extract) > 1500:
        extract = extract[:1500].rsplit(" ", 1)[0] + "…"
    link = ((page.get("content_urls") or {}).get("desktop") or {}).get("page") or ""
    return {"title": str(page.get("title") or args["title"])[:200], "description": str(page.get("description") or "")[:300],
            "extract": extract, "url": link if link.startswith(f"https://{language}.wikipedia.org/") else "",
            "disambiguation": page.get("type") == "disambiguation"}
