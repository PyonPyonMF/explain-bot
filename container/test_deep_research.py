"""Deep research routing, bounded persistence, source attribution and partial-result delivery."""
import json
import os
import unittest
from unittest.mock import Mock, patch

import deep_research as D
import pipeline as P
from render_modes import response_format, text_task


def result(text=None, calls=(), stop="end_turn"):
    blocks = list(calls)
    if text is not None:
        blocks.append({"type": "text", "text": json.dumps(text)})
    return {"content": blocks, "stop_reason": stop}


def call(name="forum_read", url="https://forum.example/index.php?showtopic=1", ident="t1"):
    return {"type": "tool_use", "id": ident, "name": name, "input": {"url": url, "base_url": "https://forum.example/", "query": "Guild"}}


def topic(url="https://forum.example/index.php?showtopic=1", author="Alice"):
    return {"kind": "forum_topic", "url": url, "title": "An event", "sections": ["Server A"],
            "posts": [{"url": url + "#entry1", "author": author, "date": "2018-01-01", "text": "I opposed the shutdown.",
                       "quotes": [{"attribution": "Bob", "text": "The server will close."}], "current_signature": "Current group"}]}


class DeepResearchTests(unittest.TestCase):
    def test_explicit_modes_and_exact_user_example(self):
        for query in ("рисерч: история сервера https://forum.ss13.ru", "research: public source history",
                      "Сделай глубокое исследование форума", "какие причины убийства анимуса злофениксом и как гильдия демагогов на это повлияла, изучи https://forum.ss13.ru",
                      "изучи форум https://example.org/", "проведи подробное расследование закрытия сервера"):
            self.assertEqual(text_task(query), "research", query)
            self.assertEqual(response_format(query), "video", query)
        for query in ('объясни слово "рисерч"', 'Не проводи исследование', 'не хочу исследование',
                      '> research: history\nОбъясни термин', 'объясни что такое исследование',
                      'найди группу на https://forum.ss13.ru', 'https://example.org/research',
                      'не изучи форум, а объясни слово текстом'):
            self.assertEqual(text_task(query), "explain", query)

    def test_source_registry_preserves_authors_quotes_and_ignores_search_snippets(self):
        evidence = D.Evidence()
        evidence.server([{"type": "web_search_tool_result", "content": [{"url": "https://example.org", "title": "Snippet"}]}])
        self.assertEqual(evidence.documents, {})
        evidence.forum(topic())
        evidence.forum(topic())
        self.assertEqual(len(evidence.documents), 1)
        self.assertIn('"author": "Alice"', next(iter(evidence.documents.values()))["content"])
        self.assertIn('"attribution": "Bob"', next(iter(evidence.documents.values()))["content"])
        self.assertEqual(len(evidence.topics), 1)

    def test_report_drops_unsupported_claims_and_invented_urls(self):
        evidence = D.Evidence(); evidence.forum(topic())
        report = {"title": "History", "sections": [{"heading": "Accounts", "paragraphs": [
            {"text": "Alice opposed closing.", "kind": "attributed", "sources": ["S1"]},
            {"text": "Invented fact", "kind": "documented", "sources": ["S999"]},
            {"text": "Unsupported paragraph", "kind": "documented", "sources": []},
            {"text": "Tentative consequence https://invented.example", "kind": "inference", "sources": ["S1"]}]}],
            "gaps": ["The cause remains unproven."]}
        output = D.render_report(report, evidence, {"topics": 1, "pages": 2, "searches": 0, "reason": "time_limit"})
        self.assertIn("Версия участника", output)
        self.assertIn("Вывод по источникам", output)
        self.assertIn("showtopic=1#entry1", output)
        self.assertIn("лимит времени", output)
        self.assertNotIn("Invented fact", output)
        self.assertNotIn("Unsupported paragraph", output)
        self.assertNotIn("invented.example", output)

    def test_persists_after_first_hit_and_answers_every_tool_call(self):
        session = Mock(used=2)
        session.read.return_value = topic()
        replies = [result(calls=[call(ident=f"t{i}") for i in range(5)]), result({"done": True, "gaps": ["Cause unknown"]}),
                   RuntimeError("provider failed")]
        with patch.object(D, "Session", return_value=session), patch.object(D, "_call", side_effect=replies) as api:
            evidence, checkpoint, stats, _ = D.investigate("research history", ["https://forum.example"])
        self.assertEqual(api.call_count, 3)  # One relevant topic is not sufficient to stop deep research.
        results = api.call_args_list[1].args[0][2]["content"]
        self.assertEqual(len(results), 5)
        self.assertTrue(results[-1]["is_error"])
        self.assertEqual(len(evidence.documents), 1)
        self.assertEqual(stats["reason"], "lookup_error")
        self.assertTrue(stats["partial"])

    def test_stops_when_no_new_evidence_and_respects_hard_round_bound(self):
        session = Mock(used=1)
        session.search.return_value = {"kind": "forum_search", "matches": []}
        with patch.object(D, "Session", return_value=session), patch.object(D, "_call", return_value=result(calls=[call("forum_search")])) as api:
            evidence, _, stats, _ = D.investigate("research history", ["https://forum.example"])
        self.assertEqual(api.call_count, 5)
        self.assertEqual(stats["reason"], "no_new_evidence")
        self.assertFalse(evidence.documents)

    def test_stops_at_page_budget_and_preserves_evidence(self):
        session = Mock(used=48)
        session.read.return_value = topic()
        with patch.object(D, "Session", return_value=session), patch.object(D, "_call", return_value=result(calls=[call()])) as api:
            evidence, _, stats, _ = D.investigate("research history", ["https://forum.example"])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(stats["reason"], "source_limit")
        self.assertEqual(len(evidence.documents), 1)

    def test_refusal_never_proceeds_to_report_or_more_search(self):
        response = {"content": [{"type": "text", "text": "I cannot help with that."}], "stop_reason": "refusal"}
        with patch.object(D, "_call", return_value=response) as api:
            answer = D.answer({"text": "research harmful request"})
        self.assertIn("cannot help", answer)
        self.assertEqual(api.call_count, 1)

    def test_timeout_before_first_lookup_is_bounded(self):
        with patch.object(D.time, "monotonic", side_effect=[0, 1000, 1001]), patch.object(D, "_call") as api:
            _, _, stats, _ = D.investigate("research history", [])
        self.assertEqual(stats["reason"], "time_limit")
        api.assert_not_called()

    def test_synthesis_failure_delivers_available_sources(self):
        evidence = D.Evidence(); evidence.forum(topic())
        stats = {"reason": "lookup_error", "topics": 1, "pages": 2, "searches": 1}
        with patch.object(D, "investigate", return_value=(evidence, {}, stats, 100000)), \
                patch.object(D, "_call", side_effect=TimeoutError()):
            answer = D.answer({"text": "research history"})
        self.assertIn("showtopic=1", answer)
        self.assertIn("выводы не сформированы", answer)

    def test_pipeline_uses_deep_mode_then_video_without_second_research_or_collection(self):
        job = {"kind": "query", "text": "research: forum history"}
        def dossier(job, progress):
            job.update(deep_research_outcome="partial", prepared_inputs=([], ["Opened sources"], []))
            return "Detailed report with verified chronology"
        with patch.dict(os.environ, {"FULL_GEN": "1"}), patch.object(D, "answer", side_effect=dossier) as deep, patch("text_answers.deliver") as delivery, \
                patch.object(P, "collect") as collect, patch.object(P, "prepare_sources") as shallow, \
                patch.object(P, "research_progress"), patch.object(P, "_safe_text"), \
                patch("fullgen.make_video_fullgen", return_value=("result.mp4", "History")) as render, patch.object(P, "discord_video") as upload:
            P.handle_job(job)
        deep.assert_called_once()
        delivery.assert_not_called()
        self.assertEqual(job["text_task"], "research")
        self.assertTrue(job["research_video"])
        self.assertEqual(job["response_format"], "video")
        collect.assert_not_called(); shallow.assert_not_called(); render.assert_called_once(); upload.assert_called_once()
        _, prompt = P.build_user_content(job, [], ["Huge raw source archive"])
        self.assertIn("Detailed report with verified chronology", prompt)
        self.assertNotIn("Huge raw source archive", prompt)

    def test_explicit_research_text_is_still_available(self):
        for request in ("рисерч текстом: история сервера", "исследуй историю текстом", "research: history, explain in text"):
            self.assertEqual(response_format(request), "text", request)
        self.assertEqual(response_format('рисерч: тема, в посте сказано "ответь текстом"'), "video")
        self.assertEqual(response_format('рисерч текстом: тема. Лучше видео'), "video")
        job = {"kind": "query", "text": "рисерч текстом: история сервера"}
        with patch.object(D, "answer", return_value="Detailed report"), patch("text_answers.deliver") as delivery, \
                patch.object(P, "research_progress"), patch("fullgen.make_video_fullgen") as render:
            P.handle_job(job)
        delivery.assert_called_once_with(job, "Detailed report")
        render.assert_not_called()

    def test_unavailable_video_research_never_posts_the_long_dossier(self):
        def failed(job, progress):
            job.update(deep_research_outcome="unavailable", deep_research_notice="No usable evidence.")
            return "Very long working notes. " * 1000
        job = {"kind": "query", "text": "research: history"}
        with patch.object(D, "answer", side_effect=failed), patch("text_answers.deliver") as delivery, \
                patch.object(P, "research_progress"), patch.object(P, "_safe_text") as notice, patch("fullgen.make_video_fullgen") as render:
            P.handle_job(job)
        delivery.assert_not_called(); render.assert_not_called()
        notice.assert_called_once_with(job, "No usable evidence.")

    def test_research_narration_preserves_longer_plan_and_rejects_truncated_speech(self):
        import fullgen as F
        plan = {"title": "History", "scenes": [{"heading": str(i), "narration": "A" * 260 + "."} for i in range(9)]}
        text = "<plan>" + json.dumps(plan) + "</plan>"
        research = F.extract_plan(text, research=True)
        self.assertEqual(len(research["scenes"]), 9)
        self.assertGreater(sum(len(s["narration"]) for s in research["scenes"]), 1600)
        self.assertTrue(all(s["narration"].endswith(".") for s in research["scenes"]))
        self.assertLessEqual(sum(len(s["narration"]) for s in F.extract_plan(text)["scenes"]), 1600)
        plan["scenes"][0]["narration"] = "A" * 700
        with self.assertRaises(F.FullGenFailed):
            F.extract_plan("<plan>" + json.dumps(plan) + "</plan>", research=True)

    def test_research_caption_has_only_a_few_links(self):
        from assets import source_text
        job = {"research_video": True, "research": [{"title": str(i), "url": f"https://forum.example/{i}"} for i in range(20)]}
        self.assertEqual(source_text(job).count("https://"), 3)

    def test_env_limits_cannot_make_work_unbounded(self):
        with patch.dict(os.environ, {"DEEP_RESEARCH_TIMEOUT": "99999", "DEEP_RESEARCH_MAX_PAGES": "-2", "DEEP_RESEARCH_MAX_SEARCHES": "bad"}):
            self.assertEqual(D.limits(), {"seconds": 720, "pages": 8, "searches": 12})

    def test_followup_retains_cited_late_evidence_and_long_report_tail(self):
        import followups
        evidence = D.Evidence()
        for index in range(20):
            evidence.add(f"https://forum.example/topic/{index}", "Topic", {"text": "a" * 8500})
        report = {"title": "Report", "sections": [{"heading": "Finding", "paragraphs": [
            {"text": "An account found late in the investigation.", "kind": "attributed", "sources": ["S20"]}]}]}
        stats = {"reason": "source_limit", "topics": 20, "pages": 20, "searches": 1}
        job = {"text": "research history", "text_task": "research"}
        with patch.object(D, "investigate", return_value=(evidence, {}, stats, 100000)), patch.object(D, "_call", return_value=result(report)):
            D.answer(job)
        memory = job["prepared_inputs"][1][-1]
        retained = json.loads(memory.split("\n", 1)[1].rsplit("\n", 1)[0])
        self.assertLess(len(retained), 20)
        self.assertIn("S20", [source["id"] for source in retained])
        followups.record_text(job, "a" * 17000 + "TAIL")
        self.assertTrue(job["text_answer_record"]["text"].endswith("TAIL"))


if __name__ == "__main__":
    unittest.main()
