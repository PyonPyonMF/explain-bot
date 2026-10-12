"""Site-aware link readers; every network call is mocked."""
import json
import unittest
from unittest.mock import patch

import links

FX = {"code": 200, "tweet": {
    "url": "https://x.com/garrytan/status/2109410082769482174", "text": "Startups win by shipping. https://example.com/essay",
    "author": {"name": "Garry Tan", "screen_name": "garrytan"}, "created_at": "Mon Oct 12 09:00:00 +0000 2026",
    "likes": 1200, "retweets": 80, "replies": 15, "views": 90000,
    "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "altText": "A chart"},
                      {"type": "video", "url": "https://video.twimg.com/v.mp4", "thumbnail_url": "https://pbs.twimg.com/thumb.jpg", "duration": 75}]},
    "quote": {"text": "Quoted words", "author": {"name": "Paul", "screen_name": "paulg"}, "url": "https://x.com/paulg/status/1"},
    "community_note": {"text": "Context added by readers"},
}}


def reply(payload, ctype="application/json"):
    return (json.dumps(payload).encode() if not isinstance(payload, bytes) else payload), ctype


class XTests(unittest.TestCase):
    def test_mirrors_and_original_hosts_are_recognised(self):
        for url in ("https://fixvx.com/garrytan/status/2109410082769482174", "https://x.com/garrytan/status/2109410082769482174?s=20",
                    "https://vxtwitter.com/garrytan/status/2109410082769482174", "https://mobile.twitter.com/garrytan/status/2109410082769482174",
                    "https://fixupx.com/i/status/2109410082769482174", "https://nitter.privacydev.net/garrytan/status/2109410082769482174"):
            self.assertIsNotNone(links._x_match(url), url)
        self.assertIsNone(links._x_match("https://x.com/garrytan"))
        self.assertIsNone(links._x_match("https://example.com/garrytan/status/2109410082769482174"))

    def test_fixvx_link_reads_the_post_not_the_url(self):
        with patch.object(links, "fetch", return_value=reply(FX)) as fetch:
            page = links.read_link("https://fixvx.com/garrytan/status/2109410082769482174")
        self.assertEqual(fetch.call_args[0][0], "https://api.fxtwitter.com/garrytan/status/2109410082769482174")
        self.assertEqual(page["kind"], "x_post")
        for expected in ("Garry Tan (@garrytan)", "Startups win by shipping.", "alt text: A chart", "video, 1:15",
                         "Quoted post by Paul (@paulg)", "Quoted words", "Context added by readers", "likes 1 200"):
            self.assertIn(expected, page["text"])
        self.assertEqual([i["url"] for i in page["images"]], ["https://pbs.twimg.com/media/a.jpg", "https://pbs.twimg.com/thumb.jpg"])
        self.assertEqual(page["links"], ["https://example.com/essay"])

    def test_second_mirror_then_generic_preview_when_apis_fail(self):
        vx = {"text": "From vx", "user_name": "Garry Tan", "user_screen_name": "garrytan",
              "media_extended": [{"type": "image", "url": "https://pbs.twimg.com/media/b.jpg"}]}
        def fetch(url, **kw):
            if "fxtwitter" in url:
                raise OSError("down")
            return reply(vx)
        with patch.object(links, "fetch", side_effect=fetch):
            page = links.read_link("https://x.com/garrytan/status/2109410082769482174")
        self.assertIn("From vx", page["text"])
        self.assertEqual(page["images"][0]["url"], "https://pbs.twimg.com/media/b.jpg")

        preview = b'<html><head><meta property="og:title" content="Garry Tan on X"><meta property="og:description" content="The post text"></head><body></body></html>'
        def fetch2(url, **kw):
            if "api." in url:
                raise OSError("down")
            return preview, "text/html"
        with patch.object(links, "fetch", side_effect=fetch2):
            page = links.read_link("https://fixvx.com/garrytan/status/2109410082769482174")
        self.assertEqual(page["kind"], "web_page")
        self.assertIn("The post text", page["text"])


class OtherSiteTests(unittest.TestCase):
    def test_telegram_post_text_owner_and_photo(self):
        embed = (b'<div class="tgme_widget_message_owner_name"><span>Science News</span></div>'
                 b'<a class="tgme_widget_message_photo_wrap blured" style="width:100px;background-image:url(\'https://cdn4.telesco.pe/file/p.jpg\')"></a>'
                 b'<div class="tgme_widget_message_text js-message_text" dir="auto">Line one<br/><b>bold</b> two</div>')
        with patch.object(links, "fetch", return_value=(embed, "text/html")) as fetch:
            page = links.read_link("https://t.me/sciencenews/123")
        self.assertIn("?embed=1", fetch.call_args[0][0])
        self.assertIn("Science News", page["text"])
        self.assertIn("Line one\nbold two", page["text"])
        self.assertEqual(page["images"][0]["url"], "https://cdn4.telesco.pe/file/p.jpg")

    def test_youtube_ids(self):
        for url in ("https://youtu.be/dQw4w9WgXcQ?t=3", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=x",
                    "https://youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/live/dQw4w9WgXcQ"):
            self.assertEqual(links._youtube_id(url), "dQw4w9WgXcQ", url)
        self.assertIsNone(links._youtube_id("https://www.youtube.com/@channel"))

    def test_youtube_title_description_and_thumbnail(self):
        page = b'..."lengthSeconds":"212"...,"shortDescription":"Line\\nmore",...'
        def fetch(url, **kw):
            return reply({"title": "A video", "author_name": "Chan"}) if "oembed" in url else (page, "text/html")
        with patch.object(links, "fetch", side_effect=fetch):
            result = links.read_link("https://youtu.be/dQw4w9WgXcQ")
        self.assertIn("A video", result["text"])
        self.assertIn("3:32", result["text"])
        self.assertIn("Line\nmore", result["text"])
        self.assertIn("hqdefault", result["images"][0]["url"])

    def test_reddit_post_and_top_comments(self):
        data = [{"data": {"children": [{"data": {"subreddit": "physics", "author": "op", "score": 10, "title": "Why?",
                                                  "selftext": "Body", "is_self": True, "permalink": "/r/physics/comments/abc123/why/"}}]}},
                {"data": {"children": [{"kind": "t1", "data": {"author": "smart", "score": 5, "body": "Because"}}]}}]
        with patch.object(links, "fetch", return_value=reply(data)) as fetch:
            page = links.read_link("https://old.reddit.com/r/physics/comments/abc123/why/")
        self.assertIn("/comments/abc123.json", fetch.call_args[0][0])
        self.assertIn("u/smart (5): Because", page["text"])

    def test_generic_page_prefers_article_body_and_og_image(self):
        body = "Real article sentence. " * 30
        doc = (f'<html><head><title>T</title><meta property="og:image" content="/img/cover.jpg"></head><body>'
               f'<div class="menu">Home About</div><article><p>{body}</p></article></body></html>').encode()
        with patch.object(links, "fetch", return_value=(doc, "text/html")):
            page = links.read_link("https://example.com/news/1")
        self.assertIn("Real article sentence.", page["text"])
        self.assertNotIn("Home About", page["text"])
        self.assertEqual(page["images"][0]["url"], "https://example.com/img/cover.jpg")

    def test_javascript_shell_is_retried_as_a_link_preview_crawler(self):
        shell = b"<html><head><title>App</title></head><body><div id=root></div></body></html>"
        rich = b'<html><head><meta property="og:description" content="Preview text a person would see"></head></html>'
        def fetch(url, headers=None, **kw):
            return (rich if headers and "Discordbot" in headers.get("user-agent", "") else shell), "text/html"
        with patch.object(links, "fetch", side_effect=fetch):
            page = links.read_link("https://app.example.com/p/1")
        self.assertIn("Preview text a person would see", page["text"])
        self.assertIn("rendered by JavaScript", page["text"])


if __name__ == "__main__":
    unittest.main()
