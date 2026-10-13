"""Forum search, attribution, bounded navigation and research tool-loop regression checks."""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import forum_reader as F
import inputs
import pipeline as P
import research as R


PAGE = b'''<html><head><title>Forum</title></head><body><h1>A community discussion</h1>
<div class="post_block" id="post_id_1"><span class="author vcard">Alice</span>
<p class="member_title">A public title</p><abbr class="published" title="2018-01-01"></abbr>
<a rel="bookmark" href="index.php?s=secret-session&amp;showtopic=1#entry1">#1</a>
<div class="post entry-content"><p>Alice's actual statement.</p>
<p class="citation">Bob wrote:</p><div class="blockquote">Bob's quoted statement.</div>
<p>Alice's reply.</p></div><div class="signature">Current guild signature of Alice.</div>
<div class="custom_fields">Private unrelated contact or city.</div></div>
<div class="post_block" id="post_id_2"><span class="author vcard">Carol</span>
<div class="post entry-content">Carol is unrelated to this guild.</div>
<div class="signature">A different signature.</div></div>
<a rel="next" href="index.php?s=secret-session&amp;showtopic=1&amp;st=20">Next</a></body></html>'''
ROOT = b'''<html><form action="https://forum.example/index.php?s=guest&amp;app=core&amp;module=search&amp;do=search&amp;fromMainBar=1" method="post"></form></html>'''
SEARCH = b'''<h1>Search results</h1><a href="index.php?s=guest&amp;showtopic=1">Relevant topic</a>
<a href="index.php?showtopic=1&amp;st=20">2</a><a href="index.php?showuser=42">PublicHandle</a>'''


def response(content, stop="end_turn"):
    return 200, json.dumps({"content": content, "stop_reason": stop}).encode()


class ForumReaderTests(unittest.TestCase):
    def test_posts_quotes_signatures_and_authors_remain_separate(self):
        page = F.parse_page(PAGE, "https://forum.example/index.php?showtopic=1")
        self.assertEqual(page["kind"], "forum_topic")
        self.assertEqual(page["title"], "A community discussion")
        alice, carol = page["posts"]
        self.assertEqual(alice["author"], "Alice")
        self.assertNotIn("Bob's", alice["text"])
        self.assertEqual(alice["quotes"][0]["attribution"], "Bob wrote:")
        self.assertIn("Bob's", alice["quotes"][0]["text"])
        self.assertIn("Alice", alice["current_signature"])
        self.assertNotIn("Alice", carol["current_signature"])
        self.assertNotIn("contact or city", json.dumps(page))
        self.assertNotIn("secret-session", json.dumps(page))
        self.assertIn("st=20", page["pages"][0]["url"])

    def test_native_search_uses_the_observed_public_form_and_member_mode(self):
        session = F.Session(["https://forum.example"])
        with patch.object(session, "fetch", side_effect=[ROOT, SEARCH]) as fetch:
            result = session.search("https://forum.example", "PublicHandle", "members")
        query = fetch.call_args.args[0]
        self.assertIn("search_app=members", query)
        self.assertIn("search_term=PublicHandle", query)
        self.assertNotIn("s=guest", query)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["kind"], "profile")

    def test_canonical_urls_do_not_double_decode_query_names(self):
        self.assertIn("section=search", F.canonical("https://forum.example/index.php?app=core&section=search"))
        self.assertIn("section=search", F.canonical("https://forum.example/index.php?app=core&amp;section=search"))

    def test_unobserved_domains_private_hosts_mutation_routes_and_budgets_are_rejected(self):
        session = F.Session(["https://forum.example"], max_pages=0)
        with self.assertRaisesRegex(ValueError, "domain"):
            session.read("https://other.example/index.php?showtopic=1")
        with self.assertRaisesRegex(ValueError, "budget"):
            session.read("https://forum.example/index.php?showtopic=1")
        with self.assertRaisesRegex(ValueError, "Only public read"):
            session.read("https://forum.example/index.php?do=delete")
        session = F.Session(["http://127.0.0.1"])
        with self.assertRaises(inputs._Blocked):
            session.read("http://127.0.0.1/index.php?showtopic=1")

    def test_access_denial_is_not_a_profile_or_proof_of_absence(self):
        page = F.parse_page('Необходимо войти: вы не имеете прав'.encode(), "https://forum.example/index.php?showuser=1")
        self.assertEqual(page["kind"], "forum_unavailable")

    def test_long_page_is_explicitly_partial_and_prioritizes_matching_posts(self):
        posts = [{"author": str(i), "text": "x"*2000, "url": f"https://forum.example/t/1#{i}"} for i in range(30)]
        posts[-1]["text"] = "RareHandle is relevant."
        page = F.bounded({"posts": posts}, "RareHandle")
        self.assertTrue(page["partial"])
        self.assertTrue(any(p["author"] == "29" for p in page["posts"]))
        self.assertLess(len(json.dumps(page["posts"])), F.MAX_PAGE_CHARS + 100)


class ResearchQualityTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test", "WEB_RESEARCH": "1"})
        env.start(); self.addCleanup(env.stop)

    def test_client_forum_results_reach_model_and_make_verifiable_answer_evidence(self):
        url = "https://forum.example/index.php?showtopic=1#entry1"
        page = {"kind": "forum_topic", "url": url, "title": "Relevant topic", "posts": [{"url": url, "author": "Alice", "text": "Evidence"}]}
        final = {"findings": [{"fact": "A direct fact", "source_url": url, "source_title": "Relevant topic"}],
                 "answer_status": "ready", "direct_answer": "A direct answer", "evidence_urls": [url], "clarification_question": ""}
        calls = [response([{"type": "tool_use", "id": "forum1", "name": "forum_read", "input": {"url": url}}], "tool_use"),
                 response([{"type": "text", "text": json.dumps(final)}])]
        job = {"kind": "query", "text": "кто такой Alice на https://forum.example/"}
        with patch.object(P, "_http", side_effect=calls) as http, patch.object(F.Session, "read", return_value=page):
            findings = R.web_research(job, [], [])
        body = json.loads(http.call_args_list[1].args[1])
        self.assertEqual(body["messages"][-1]["content"][0]["tool_use_id"], "forum1")
        self.assertIn("Evidence", body["messages"][-1]["content"][0]["content"])
        self.assertEqual(findings[0]["url"], url)
        self.assertEqual(job["research_assessment"]["status"], "ready")
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})

    def test_unknown_public_entity_cannot_be_declared_unsearched_without_retry(self):
        answer = {"findings": [], "answer_status": "not_found", "direct_answer": "", "evidence_urls": [],
                  "clarification_question": "Which public forum?"}
        with patch.object(P, "_http", return_value=response([{"type": "text", "text": json.dumps(answer)}])) as http:
            job = {"kind": "query", "text": "кто такой RareHandle"}
            R.web_research(job, [], [])
        self.assertEqual(http.call_count, 2)
        self.assertEqual(job["research_assessment"]["status"], "not_found")

    def test_background_facts_do_not_launch_visuals_when_the_actual_answer_is_missing(self):
        def research(job, *_):
            job["research_assessment"] = {"status": "partial", "direct_answer": "The server exists, but the claimed deal is unverified.",
                                          "question": "Which announcement describes the deal?"}
            return [{"url": "https://forum.example/", "title": "Forum", "summary": "The forum exists."}]
        job, texts = {"kind": "query", "text": "Why did this player buy a leadership role?"}, []
        with tempfile.TemporaryDirectory() as directory, patch.object(R, "web_research", side_effect=research), \
                patch("assets.prepare_visuals") as visuals, patch.object(P, "_safe_text") as send:
            P.prepare_sources(job, [], texts, directory)
            self.assertTrue(P.request_missing_context(job))
        visuals.assert_not_called()
        self.assertIn("Which announcement", send.call_args.args[1])

    def test_visual_planning_receives_the_verified_answer_first(self):
        def research(job, *_):
            job["research_assessment"] = {"status": "ready", "direct_answer": "Animus here is an SS13 community."}
            return [{"url": "https://forum.example/t/1", "title": "Topic", "summary": "Specific evidence"}]
        def visuals(job, images, texts, directory):
            self.assertIn("Specific evidence", "\n".join(texts))
            self.assertEqual(job["research_assessment"]["status"], "ready")
            return [], []
        with tempfile.TemporaryDirectory() as directory, patch.object(R, "web_research", side_effect=research), \
                patch("assets.prepare_visuals", side_effect=visuals):
            P.prepare_sources({"kind": "query", "text": "who?"}, [], [], directory)

    def test_named_question_is_not_replaced_by_an_ambient_reaction_gif(self):
        other = {"id": "20", "content": "A reaction", "author": {"username": "Other"}, "embeds": [
            {"type": "gifv", "url": "https://tenor.com/view/x-gif-1", "video": {"url": "https://cdn.example/a.mp4"}}]}
        job = {"kind": "query", "text": "кто такой RareHandle с Оникса", "channel_id": "1", "context_messages": 10}
        with patch.object(inputs, "_history", return_value=[other]), patch.object(inputs, "fetch") as fetch:
            images, texts, _ = inputs.collect(job)
        fetch.assert_not_called(); self.assertEqual(images, [])
        _, prompt = P.build_user_content(job, images, texts)
        self.assertIn("Answer the user's CURRENT question directly", prompt)
        self.assertNotIn(P.REACTION_RULE, prompt)

    def test_unanswered_request_never_reaches_video_or_voice_generation(self):
        def research(job, *_):
            job["research_assessment"] = {"status": "ambiguous", "direct_answer": "", "question": "Какое сообщество имеется в виду?"}
            return []
        with patch.dict(os.environ, {"FULL_GEN": "1", "VISUAL_ASSETS": "1"}), \
                patch.object(P, "collect", return_value=([], [], [])), patch.object(R, "web_research", side_effect=research), \
                patch.object(P, "_safe_text") as send, patch("assets.prepare_visuals") as visuals, \
                patch("fullgen.make_video_fullgen") as fullgen, patch.object(P, "voice_all") as voice, \
                patch.object(P, "discord_video") as delivery:
            P.handle_job({"kind": "query", "text": "кто такой RareHandle", "response_format": "video"})
        for unused in (visuals, fullgen, voice, delivery):
            unused.assert_not_called()
        self.assertIn("сообщество", send.call_args.args[1])

    def test_research_refusal_is_not_an_invitation_to_provide_more_harmful_details(self):
        with patch.object(P, "_safe_text") as send:
            self.assertTrue(P.request_missing_context({"request_user_id": "42", "research_assessment": {
                "status": "refused", "direct_answer": "Не могу помочь с этим запросом."}}))
        self.assertEqual(send.call_args.args[1], "Не могу помочь с этим запросом.")

    def test_supported_partial_answer_without_a_missing_detail_is_not_an_unnecessary_question(self):
        job = {"research_assessment": {"status": "partial", "direct_answer": "Вот что подтверждено просмотренными сообщениями.",
                                       "evidence_urls": ["https://forum.example/t/1"], "question": ""}}
        with patch("text_answers.deliver") as deliver, patch.object(P, "_safe_text") as question:
            self.assertTrue(P.request_missing_context(job))
        deliver.assert_called_once_with(job, job["research_assessment"]["direct_answer"])
        question.assert_not_called()


if __name__ == "__main__":
    unittest.main()
