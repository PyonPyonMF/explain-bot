"""Reaction GIFs, stickers and videos: storyboards, titles, context media, and the prompt rule; no network."""
import io
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import inputs
import interactions
import links
import pipeline


def gif(frames=12, first_black=True):
    out, ims = io.BytesIO(), []
    for k in range(frames):
        ims.append(Image.new("RGB", (160, 90), (0, 0, 0) if first_black and k == 0 else (10 + k * 18, 80, 200 - k * 12)))
    ims[0].save(out, "GIF", save_all=True, append_images=ims[1:], duration=60, loop=0)
    return out.getvalue()


TENOR = "https://tenor.com/view/tony-soprano-stare-sopranos-gif-25648932"
GIF_EMBED = {"type": "gifv", "url": TENOR, "provider": {"name": "Tenor"},
             "thumbnail": {"url": "https://media.tenor.com/abc/AAAAe/stare.png"},
             "video": {"url": "https://media.tenor.com/abc/AAAPo/stare.mp4"}}


def message(mid, author, content="", **extra):
    return {"id": str(mid), "content": content, "author": {"id": str(mid), "username": author},
            "timestamp": "2026-10-12T00:07:00+00:00", **extra}


class StoryboardTests(unittest.TestCase):
    def test_gif_becomes_numbered_frames_and_skips_the_black_first_frame(self):
        sheet, count = inputs.storyboard(gif(), "image/gif")
        self.assertEqual(count, 6)
        with Image.open(io.BytesIO(sheet)) as im:
            self.assertGreater(max(im.crop((20, 20, 60, 60)).convert("L").tobytes()), 30)  # tile 1 is not the black frame

    def test_still_image_is_left_alone(self):
        out = io.BytesIO()
        Image.new("RGB", (100, 100), "red").save(out, "PNG")
        self.assertIsNone(inputs.storyboard(out.getvalue(), "image/png"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_short_video_storyboard(self):
        with tempfile.TemporaryDirectory() as d:
            clip = f"{d}/c.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=10", clip], check=True)
            with open(clip, "rb") as f:
                self.assertEqual(inputs.storyboard(f.read(), "video/mp4")[1], 6)

    def test_gif_titles_from_tenor_and_giphy_addresses(self):
        self.assertEqual(inputs.gif_title(TENOR), "tony soprano stare sopranos")
        self.assertEqual(inputs.gif_title("https://giphy.com/gifs/thumbs-up-3o7abKhOpu0NwenH3O"), "thumbs up")
        self.assertIsNone(inputs.gif_title("https://example.com/view/a-gif-1"))


class ConversationTests(unittest.TestCase):
    def test_what_did_he_mean_sees_the_gif_and_what_it_answered(self):
        """@bot что он имел ввиду — after someone answered a message with a Tenor GIF."""
        claim = message(1, "Analyst", "Итого ~5 часов реального использования, дальше повербанк ничего не даст.")
        reaction = message(2, "Richard Cruise", TENOR, embeds=[GIF_EMBED])
        job = {"kind": "query", "text": "что он имел ввиду?", "channel_id": "9", "context_messages": 10, "context_before_id": "3",
               "message": message(3, "Rager", "что он имел ввиду?")}
        fetched = []
        def fetch(url, **kw):
            fetched.append(url)
            return gif(), "image/gif"
        with patch.object(inputs, "_history", return_value=[claim, reaction]), patch.object(inputs, "fetch", side_effect=fetch):
            images, texts, _ = inputs.collect(job)
        text = "\n".join(texts)
        self.assertIn("Richard Cruise: [GIF: tony soprano stare sopranos]", text)
        self.assertNotIn(TENOR, text.split("</conversation_before>")[0])
        self.assertEqual(fetched, ["https://media.tenor.com/abc/AAAPo/stare.mp4"])  # the moving GIF, not its still thumbnail
        self.assertEqual(len(images), 1)
        self.assertEqual(job["image_meta"][0]["animated"], 6)
        self.assertIn("Posted by Richard Cruise at [00:07]", job["image_meta"][0]["caption"])
        self.assertIn('GIF from Tenor titled "tony soprano stare sopranos"', text)  # in <attached_images>
        _, prompt = pipeline.build_user_content(job, images, texts)
        self.assertIn(pipeline.REACTION_RULE, prompt)

    def test_reply_to_a_gif_message_uses_the_gif_page_and_one_storyboard(self):
        target = message(2, "Richard Cruise", TENOR, embeds=[GIF_EMBED])
        page = ('<html><head><meta property="og:title" content="Tony Soprano Stare GIF">'
                '<meta name="keywords" content="tony soprano,stare,sopranos,gif">'
                '<meta property="og:video:secure_url" content="https://media.tenor.com/abc/AAAPo/stare.mp4"></head></html>').encode()
        def fetch(url, **kw):
            return (page, "text/html") if url == TENOR else (gif(), "image/gif")
        job = {"kind": "message", "message": target, "channel_id": "9", "question": "нахера он это скинул?"}
        with patch.object(inputs, "_history", return_value=[]), patch.object(inputs, "fetch", side_effect=fetch), \
                patch.object(links, "fetch", side_effect=fetch):
            images, texts, _ = inputs.collect(job)
        text = "\n".join(texts)
        self.assertIn('kind="gif" attached_images="1"', text)
        self.assertIn("Tags: tony soprano,stare,sopranos,gif", text)
        self.assertIn("[GIF: tony soprano stare sopranos]", text)
        self.assertEqual(len(images), 1)  # the embed does not add the same GIF again

    def test_stickers_are_named_and_shown(self):
        target = message(2, "Rager", "", sticker_items=[{"id": "77", "name": "cat facepalm", "format_type": 1}])
        out = io.BytesIO()
        Image.new("RGB", (160, 160), "orange").save(out, "PNG")
        with patch.object(inputs, "_history", return_value=[]), patch.object(inputs, "fetch", return_value=(out.getvalue(), "image/png")) as fetch:
            images, texts, _ = inputs.collect({"kind": "message", "message": target, "channel_id": "9"})
        fetch.assert_called_once_with("https://media.discordapp.net/stickers/77.png", max_bytes=inputs.MAX_DOWNLOAD)
        self.assertIn("[sticker: cat facepalm]", "\n".join(texts))
        self.assertIn('Discord sticker "cat facepalm"', job_caption(images, texts))

    def test_saved_messages_keep_gif_video_and_stickers(self):
        trimmed = interactions.trim_message({**message(2, "R", TENOR, embeds=[GIF_EMBED]),
                                             "sticker_items": [{"id": "77", "name": "x", "format_type": 4}]})
        self.assertEqual(trimmed["embeds"][0]["video"]["url"], GIF_EMBED["video"]["url"])
        self.assertEqual(trimmed["embeds"][0]["type"], "gifv")
        self.assertEqual(trimmed["sticker_items"][0]["format_type"], 4)
        self.assertTrue(interactions._has_content({"sticker_items": [{"id": "1"}]}))


def job_caption(images, texts):
    return next(t for t in texts if t.startswith("<attached_images"))


if __name__ == "__main__":
    unittest.main()
