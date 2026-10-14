"""Web research step: server-tool loop, source checks, and failure isolation; no network."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import pipeline
import research


def response(content, stop="end_turn"):
    return 200, json.dumps({"content": content, "stop_reason": stop}).encode()


SEARCH = {"type": "web_search_tool_result", "content": [
    {"type": "web_search_result", "url": "https://grep.ai/blog/agentrun", "title": "AgentRun announcement"},
    {"type": "web_search_result", "url": "https://news.example/aml", "title": "AML alerts explained"}]}
FINDINGS = {"findings": [
    {"fact": "AgentRun is a harness by Grep.ai for repetitive knowledge work.", "source_url": "https://grep.ai/blog/agentrun", "source_title": "AgentRun"},
    {"fact": "Invented claim.", "source_url": "https://made-up.example/page"},
    {"fact": "About 95% of AML screening alerts are false positives.", "source_url": "https://news.example/aml"}]}



class ResearchTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "WEB_RESEARCH": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_follows_pause_turn_and_keeps_only_sources_the_tools_returned(self):
        replies = [response([{"type": "server_tool_use", "name": "web_search"}, SEARCH], "pause_turn"),
                   response([{"type": "text", "text": json.dumps(FINDINGS)}])]
        with patch.object(pipeline, "_http", side_effect=replies) as http:
            found = research.web_research({"kind": "query", "text": "что такое AgentRun?"}, [], [])
        first, second = (json.loads(c.args[1]) for c in http.call_args_list)
        self.assertEqual([t["name"] for t in first["tools"]], ["web_search", "web_fetch", "forum_search", "forum_read"])
        self.assertEqual(first["tools"][0]["type"], "web_search_20260209")
        self.assertEqual(second["messages"][-1]["role"], "assistant")  # the paused turn is continued, not restarted
        self.assertEqual(first["system"], second["system"])
        self.assertEqual(first["system"][-1]["cache_control"]["type"], "ephemeral")
        self.assertNotIn("cache_control", first)  # do not write every unique question at the 1h rate
        self.assertEqual([f["url"] for f in found], ["https://grep.ai/blog/agentrun", "https://news.example/aml"])
        self.assertEqual(found[1]["title"], "AML alerts explained")  # missing title falls back to the search hit
        self.assertIn("95%", found[1]["summary"])

    def test_refusal_and_switch_off_give_no_findings(self):
        with patch.object(pipeline, "_http", return_value=response([{"type": "text", "text": "no"}], "refusal")):
            self.assertEqual(research.web_research({"kind": "query", "text": "x"}, [], []), [])
        with patch.dict(os.environ, {"WEB_RESEARCH": "0"}), patch.object(pipeline, "_http") as http:
            self.assertEqual(research.web_research({"kind": "query", "text": "x"}, [], []), [])
        http.assert_not_called()

    def test_findings_reach_the_script_with_the_rule_and_sources(self):
        checked = [{"url": "https://grep.ai/blog/agentrun", "title": "AgentRun", "summary": "A harness."}]
        codex = ([], [{"url": "https://grep.ai/blog/agentrun", "title": "dup", "summary": "dup"},
                      {"url": "https://wiki.example/aml", "title": "AML", "summary": "Anti-money laundering."}])
        job, texts = {"kind": "query", "text": "что такое AgentRun?"}, []
        with tempfile.TemporaryDirectory() as d, patch.object(research, "web_research", return_value=checked), \
                patch("assets.prepare_visuals", return_value=codex):
            pipeline.prepare_sources(job, [], texts, d)
        self.assertEqual([r["url"] for r in job["research"]], ["https://grep.ai/blog/agentrun", "https://wiki.example/aml"])
        _, prompt = pipeline.build_user_content(job, [], texts)
        self.assertIn('<research_reference url="https://grep.ai/blog/agentrun">', prompt)
        self.assertIn(research.RULE, prompt)

    def test_research_failure_keeps_the_visuals(self):
        job, texts = {"kind": "query", "text": "x"}, []
        with tempfile.TemporaryDirectory() as d, patch.object(research, "web_research", side_effect=RuntimeError("search disabled")), \
                patch("assets.prepare_visuals", return_value=([{"id": "visual_1"}], [])):
            pipeline.prepare_sources(job, [], texts, d)
        self.assertEqual(job["assets"], [{"id": "visual_1"}])
        self.assertEqual(job["research"], [])

    def test_food_image_gets_a_complete_text_answer_without_optional_episode_question(self):
        from unittest.mock import Mock
        import text_answers as T
        job={"kind":"query","text":"что за макароны дай рецепт текстом","response_format":"text","text_task":"explain"}
        images=[{"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":"eA=="}}]
        assessment={"answer_status":"partial","direct_answer":"Похоже на спагетти с фрикадельками; можно предложить домашний вариант.",
                    "clarification_required":False,"clarification_question":"Нужен точный рецепт из конкретной серии?", "findings":[]}
        answer=Mock(return_value="Домашний вариант: ингредиенты и шаги.")
        with patch.object(research,"ask",return_value=(json.dumps(assessment),{})), \
             patch("followups.prepare",return_value=(images,[],[])),patch.object(T,"answer",answer), \
             patch.object(T,"deliver") as deliver,patch.object(pipeline,"_safe_text"),patch("clarifications.create") as picker:
            pipeline.handle_job(job)
        self.assertFalse(job['research_assessment']['clarification_required'])
        self.assertEqual(job['research_assessment']['question'],"")
        self.assertEqual(job['prepared_inputs'][0],images)
        answer.assert_called_once();deliver.assert_called_once();picker.assert_not_called()
        _,context=pipeline.build_user_content(job,images,[])
        self.assertIn('Preserve uncertainty',context)

    def test_optional_flag_cannot_bypass_missing_identity_evidence(self):
        job={"kind":"query","text":"кто такой RareHandle"}
        parsed={"answer_status":"partial","direct_answer":"An unverified guess.","clarification_required":False,"findings":[]}
        with patch.object(research,"ask",return_value=(json.dumps(parsed),{})):
            research.web_research(job,[],[])
        self.assertTrue(job['research_assessment']['clarification_required'])
        self.assertTrue(research.blocks_answer(job['research_assessment']))
        self.assertTrue(research.blocks_answer({'status':'ambiguous','direct_answer':'Guess','clarification_required':False}))

    def test_partial_without_an_explicit_sufficiency_decision_stays_blocked(self):
        for required in (None,True,'false'):
            self.assertTrue(research.blocks_answer({'status':'partial','direct_answer':'Some context','clarification_required':required}))

    def test_unsearched_phrase_comparison_is_reconsidered_with_lookup_before_clarification(self):
        first={"answer_status":"ambiguous","direct_answer":"No gaming context supplied.","clarification_question":"Which server?"}
        final={"answer_status":"ready","direct_answer":"These expressions describe opposite sides of a parent-child relationship.",
               "clarification_required":False,"evidence_urls":["https://grep.ai/blog/agentrun"],"findings":[]}
        replies=[response([{"type":"text","text":json.dumps(first)}]),
                 response([{"type":"server_tool_use","id":"lookup","name":"web_search"},SEARCH,
                           {"type":"text","text":json.dumps(final)}])]
        job={"kind":"query","text":"dad girls vs girl dads"}
        with patch.object(pipeline,"_http",side_effect=replies) as http:
            research.web_research(job,[],[])
        self.assertEqual(http.call_count,2)
        retry=json.loads(http.call_args_list[1].args[1])['messages'][-1]['content']
        self.assertIn('perform an actual lookup',retry)
        self.assertIn('Do not search incidental participants',retry)
        self.assertEqual(job['research_assessment']['status'],'ready')
        self.assertTrue(job['research_assessment']['unsearched_retry'])
        self.assertEqual(job['research_assessment']['tool_calls'],1)
        self.assertFalse(pipeline.request_missing_context(job))

    def test_known_ordinary_comparison_can_answer_without_forced_external_lookup(self):
        final={"answer_status":"not_needed","direct_answer":"A comparison of ordinary grammar terms.","clarification_required":False,"findings":[]}
        with patch.object(pipeline,"_http",return_value=response([{"type":"text","text":json.dumps(final)}])) as http:
            job={"kind":"query","text":"subject vs object in grammar"}
            research.web_research(job,[],[])
        self.assertEqual(http.call_count,1)
        self.assertEqual(job['research_assessment']['status'],'not_needed')
        self.assertFalse(pipeline.request_missing_context(job))

    def test_failed_reconsideration_remains_bounded_and_does_not_launch_generation(self):
        final={"answer_status":"ambiguous","direct_answer":"","clarification_required":True,"clarification_question":"Which document?"}
        with patch.object(pipeline,"_http",return_value=response([{"type":"text","text":json.dumps(final)}])) as http:
            job={"kind":"query","text":"compare those things"}
            research.web_research(job,[],[])
        self.assertEqual(http.call_count,2)
        self.assertTrue(research.blocks_answer(job['research_assessment']))


if __name__ == "__main__":
    unittest.main()
