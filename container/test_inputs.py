"""Quoted posts, reply chains, and bounded conversation context; no network."""
import unittest
from unittest.mock import patch

import inputs


def post(mid, text, ref=None, author="Reader"):
    return {"id": str(mid), "content": text, "author": {"id": "42", "username": author}, "message_reference": ref}


class ContextTests(unittest.TestCase):
    def test_quote_keeps_ancestors_and_deduplicates_surrounding_discussion(self):
        target = post(200, "Target claim", {"message_id": "100", "channel_id": "789"})
        parents = {
            "/channels/789/messages/100": post(100, "Parent claim", {"message_id": "50"}),
            "/channels/789/messages/50": post(50, "Root post"),
        }
        older = [post(180, "Earlier discussion"), post(190, "Overlapping discussion")]
        recent = [older[1], target, post(250, "Counterargument"), {"id": "260", "content": "Bot output", "author": {"id": "999"}}]
        def history(channel, before=None, limit=10):
            self.assertEqual((channel, limit), ("789", 10))
            return {"200": older, "300": recent}[before]
        job = {"kind": "message", "message": target, "channel_id": "789", "context_before_id": "300", "bot_user_id": "999"}
        with patch.object(inputs, "_discord_get", side_effect=parents.get), patch.object(inputs, "_history", side_effect=history):
            _, blocks, _ = inputs.collect(job)
        text = "\n".join(blocks)
        self.assertLess(text.index("Root post"), text.index("Parent claim"))
        self.assertIn("Target claim", text)
        self.assertIn("Counterargument", text)
        self.assertEqual(text.count("Overlapping discussion"), 1)
        self.assertEqual(text.count("Target claim"), 1)
        self.assertNotIn("Bot output", text)

    def test_query_history_is_anchored_before_trigger_message(self):
        with patch.object(inputs, "_history", return_value=[post(250, "Conversation")]) as history:
            _, blocks, _ = inputs.collect({"kind": "query", "text": "why?", "channel_id": "789", "context_messages": 10, "context_before_id": "300"})
        history.assert_called_once_with("789", before="300", limit=10)
        self.assertIn("Conversation", "\n".join(blocks))

    def test_reply_chain_never_fetches_other_channel(self):
        with patch.object(inputs, "_discord_get") as get:
            parents = inputs._reply_chain(post(200, "visible", {"message_id": "100", "channel_id": "123"}), "789")
        self.assertEqual(parents, [])
        get.assert_not_called()

    def test_reply_chain_stops_at_four_and_handles_cycles(self):
        def get(path):
            mid = int(path.rsplit("/", 1)[1])
            return post(mid, "parent", {"message_id": str(mid - 1)})
        with patch.object(inputs, "_discord_get", side_effect=get) as fetch:
            parents = inputs._reply_chain(post(200, "target", {"message_id": "199"}), "789")
        self.assertEqual(len(parents), 4)
        self.assertEqual(fetch.call_count, 4)
        with patch.object(inputs, "_discord_get", return_value=post(100, "parent", {"message_id": "200"})) as fetch:
            self.assertEqual(len(inputs._reply_chain(post(200, "target", {"message_id": "100"}), "789")), 1)
            fetch.assert_called_once()

    def test_quoted_images_and_links_are_collected(self):
        target = post(200, "See https://example.com/article")
        target["attachments"] = [{"url": "https://example.com/image", "content_type": "image/png"}]
        block = {"type": "image", "source": {"data": "test"}}
        with patch.object(inputs, "_history", return_value=[]), patch.object(inputs, "fetch", return_value=(b"pixels", "image/png")), patch.object(inputs, "image_block", return_value=block), patch("links.read_link", return_value={"kind": "web_page", "text": "Article text", "images": [], "links": []}):
            images, texts, _ = inputs.collect({"kind": "message", "message": target, "channel_id": "789"})
        self.assertEqual(images, [block])
        self.assertIn("Article text", "\n".join(texts))

    def test_parent_image_survives_when_target_is_a_text_reply(self):
        target = post(200, "This chart is wrong", {"message_id": "100"})
        parent = post(100, "A chart")
        parent["attachments"] = [{"url": "https://example.com/chart", "content_type": "image/png"}]
        block = {"type": "image", "source": {"data": "test"}}
        with patch.object(inputs, "_discord_get", return_value=parent), patch.object(inputs, "_history", return_value=[]), patch.object(inputs, "fetch", return_value=(b"pixels", "image/png")), patch.object(inputs, "image_block", return_value=block):
            images, texts, _ = inputs.collect({"kind": "message", "message": target, "channel_id": "789"})
        self.assertEqual(images, [block])
        self.assertIn("A chart", "\n".join(texts))

    def test_shared_post_link_brings_its_text_images_and_source(self):
        page = {"kind": "x_post", "text": "X (Twitter) post by Garry Tan (@garrytan)\nText:\nShip it", "links": [],
                "images": [{"url": "https://pbs.twimg.com/media/a.jpg", "caption": "Photo from the linked X post",
                            "credit": "@garrytan (X)", "source_url": "https://x.com/garrytan/status/1"}]}
        own = post(300, "https://fixvx.com/garrytan/status/2109410082769482174 че это значит")
        block = {"type": "image", "source": {"data": "test"}}
        job = {"kind": "query", "text": own["content"], "message": own, "channel_id": "789"}
        with patch("links.read_link", return_value=page) as read, patch.object(inputs, "fetch", return_value=(b"pixels", "image/jpeg")), patch.object(inputs, "image_block", return_value=block), patch.object(inputs, "_fingerprint", return_value="a"):
            images, texts, _ = inputs.collect(job)
        read.assert_called_once_with("https://fixvx.com/garrytan/status/2109410082769482174")
        self.assertEqual(images, [block])
        self.assertIn('kind="x_post" attached_images="1"', "\n".join(texts))
        self.assertIn("Ship it", "\n".join(texts))
        self.assertEqual(job["image_meta"][0]["source_url"], "https://x.com/garrytan/status/1")
        import pipeline
        _, prompt = pipeline.build_user_content(job, images, texts)
        self.assertIn(pipeline.LINK_RULE, prompt)

    def test_same_picture_from_preview_and_link_is_kept_once(self):
        target = post(200, "https://example.com/a")
        target["embeds"] = [{"url": "https://example.com/a", "image": {"url": "https://cdn.discordapp.com/x.png"}}]
        page = {"kind": "web_page", "text": "A", "links": [], "images": [{"url": "https://example.com/og.png"}]}
        block = {"type": "image", "source": {"data": "test"}}
        with patch.object(inputs, "_history", return_value=[]), patch("links.read_link", return_value=page), patch.object(inputs, "fetch", return_value=(b"pixels", "image/png")), patch.object(inputs, "image_block", return_value=block), patch.object(inputs, "_fingerprint", return_value="same"):
            images, _, _ = inputs.collect({"kind": "message", "message": target, "channel_id": "789"})
        self.assertEqual(len(images), 1)


if __name__ == "__main__":
    unittest.main()
