"""Reaction-to-reaction explanations preserve the chain and do not become a diagnosis lookup."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import conversation as C
import inputs
import interactions as I
import pipeline as P


QUESTION = "почему у морды горит жопа с японского слопа почему ему нравится нагетсы и какая тут связь с синдромом аспергера"


def post(mid, author, content, ref=None):
    return {"id": str(mid), "author": {"id": str(mid), "username": author, "global_name": author},
            "content": content, "message_reference": {"message_id": str(ref), "channel_id": "10"} if ref else None}


class ConversationTests(unittest.TestCase):
    def test_pragmatic_requests_use_the_conversation_but_factual_queries_keep_research(self):
        job = {"kind": "message", "message": post(3, "Bob", "I like how nuggets look.")}
        for question in (QUESTION, "объясни реакцию на реакцию", "что он имел ввиду?", "на что он намекает?",
                         "почему он так ответил?", "зачем он скинул эту гифку?", "почему ему нравятся наггетсы"):
            self.assertTrue(C.requested({**job, "question": question}), question)
        for question in ("какой рис лучше подходит для суши", "объясни реакцию замещения", "почему детям нравится сладкое",
                         "кто такой nicker_ps с Оникса, ищи на https://forum.ss13.ru/", "почему он злится, найди в интернете",
                         'В посте написано "почему он злится", объясни грамматику'):
            self.assertFalse(C.requested({**job, "question": question}), question)
        self.assertFalse(C.requested({"kind": "query", "text": "почему он злится?"}))  # no exchange supplied

    def test_graph_preserves_two_explicit_hops_and_does_not_invent_a_reply_edge(self):
        original = post(1, "Alice", "The ingredients are healthy.")
        reaction = post(2, "Bob", "I meant how it looks.", 1)
        target = post(3, "Carol", "So you agree about the ingredients?", 2)
        target["author"]["display_name"] = "Visible nickname"
        neighbor = post(4, "Dave", "Unrelated remark")
        block = C.graph({"channel_id": "10"}, target, [original, reaction], [neighbor])
        data = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
        nodes = {n["id"]: n for n in data["messages"]}
        self.assertEqual(data["target_id"], "3")
        self.assertEqual(nodes["3"]["reply_to"], "2")
        self.assertEqual(nodes["2"]["reply_to"], "1")
        self.assertIsNone(nodes["4"]["reply_to"])
        self.assertEqual(nodes["3"]["author"]["display_name"], "Visible nickname")

    def test_collection_adds_graph_and_keeps_reply_ancestors(self):
        target = post(3, "Carol", "So you agree?", 2)
        first = post(2, "Bob", "I meant the appearance.", 1)
        original = post(1, "Alice", "Healthy ingredients.")
        job = {"kind": "message", "message": target, "question": "объясни реакцию на реакцию", "channel_id": "10"}
        pages = {"/channels/10/messages/2": first, "/channels/10/messages/1": original}
        with patch.object(inputs, "_history", return_value=[]), patch.object(inputs, "_discord_get", side_effect=pages.get):
            _, texts, _ = inputs.collect(job)
        self.assertTrue(job["conversation_analysis"])
        graph = next(t for t in texts if t.startswith("<conversation_graph>"))
        self.assertIn('"reply_to": "2"', graph)
        self.assertIn('"reply_to": "1"', graph)
        _, prompt = P.build_user_content(job, [], texts)
        self.assertIn(C.RULE, prompt)
        self.assertIn("never", C.RULE.lower())

    def test_context_explanation_skips_external_lookup_and_asset_search(self):
        job = {"conversation_analysis": True, "research_assessment": {"status": "partial", "question": "Prove the diagnosis"}}
        with tempfile.TemporaryDirectory() as workdir, patch("research.web_research") as research, patch("assets.prepare_visuals") as visuals:
            P.prepare_sources(job, [], ["Conversation"], workdir)
        research.assert_not_called(); visuals.assert_not_called()
        self.assertEqual(job["research_assessment"]["status"], "not_needed")
        self.assertFalse(P.request_missing_context(job))

    def test_video_explains_the_exchange_instead_of_zooming_a_neighboring_picture(self):
        job = {"kind": "message", "question": QUESTION, "message": post(3, "Bob", "Nuggets look better.")}
        with patch.dict(os.environ, {"FULL_GEN": "1", "IMAGE_WALKTHROUGH": "1", "WEB_RESEARCH": "1", "VISUAL_ASSETS": "1"}), \
                patch("followups.prepare", return_value=([{"type": "image"}], ["<conversation_before>Food debate</conversation_before>"], [])), \
                patch.object(P, "_safe_text"), patch("research.web_research") as research, patch("assets.prepare_visuals") as visuals, \
                patch("image_story.make_video") as walkthrough, patch("fullgen.make_video_fullgen", return_value=("clip.mp4", "Reaction")) as video, \
                patch.object(P, "discord_video") as delivery:
            P.handle_job(job)
        self.assertEqual(job["response_format"], "video")
        research.assert_not_called(); visuals.assert_not_called(); walkthrough.assert_not_called()
        video.assert_called_once(); delivery.assert_called_once()

    def test_text_explanation_also_ignores_an_unverifiable_side_allegation(self):
        job = {"kind": "message", "question": QUESTION + ". Объясни текстом.", "message": post(3, "Bob", "Nuggets look better.")}
        with patch("followups.prepare", return_value=([], [], [])), patch.object(P, "_safe_text"), \
                patch("research.web_research") as research, patch("text_answers.answer", return_value="Explanation") as answer, \
                patch("text_answers.deliver") as delivery:
            P.handle_job(job)
        self.assertEqual(job["response_format"], "text")
        research.assert_not_called(); answer.assert_called_once(); delivery.assert_called_once()

    def test_aliases_survive_trimming_and_visible_name_has_priority(self):
        source = post(1, "account", "A reaction")
        source["author"].update(display_name="Server nickname", global_name="Global alias")
        trimmed = I.trim_message(source)
        self.assertEqual(inputs._author(trimmed), "Server nickname")
        self.assertEqual(trimmed["author"]["username"], "account")
        self.assertEqual(trimmed["author"]["id"], "1")

    def test_clarification_preview_never_cuts_midword(self):
        text = "Первое законченное предложение. " * 12 + "Поэтому невозможно сделать такой вывод без источника. " * 3
        preview = C.excerpt(text, 450)
        self.assertLessEqual(len(preview), 450)
        self.assertTrue(preview.endswith("."))
        long = C.excerpt("слово " * 100, 450)
        self.assertTrue(long.endswith("слово…"))


if __name__ == "__main__":
    unittest.main()
