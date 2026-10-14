"""Bot side of the tool forge: demand threshold, owner card, owner-only approval, queue contents, results."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import forge_queue as F
import interactions as I
import router

GAP = {"verdict": "gap", "tools": [], "confidence": 0.9, "reason": "", "source": "system_two", "jev": None,
       "gap": {"capability": "qr_code", "summary": "Generate QR images.", "inputs": "text", "outputs": "png",
               "example_requests": ["сделай qr"], "network_hosts": [], "category": "conversion"}}
STATE = {"request": "сделай QR для https://private.example/secret-invite", "attachments": [], "link_hosts": [],
         "requested_format": "text", "task": "explain", "date": "2026-10-14"}


class ForgeQueueTests(unittest.TestCase):
    def setUp(self):
        self.data, self.queue = tempfile.mkdtemp(), tempfile.mkdtemp()
        env = patch.dict(os.environ, {"DATA_DIR": self.data, "FORGE_QUEUE": self.queue, "FORGE_OWNER_ID": "42",
                                      "FORGE_MIN_REQUESTS": "2", "DISCORD_BOT_TOKEN": "t", "TOOLS_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)
        self.backlog = router.Backlog(Path(self.data) / "router.sqlite")
        patcher = patch.object(router, "backlog", return_value=self.backlog)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.discord = []

        def fake(method, path, payload):
            self.discord.append((method, path, payload))
            return {"id": "777"}

        patcher = patch.object(F, "_discord", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def click(self, action, user="42"):
        return I.handle({"type": I.MESSAGE_COMPONENT, "user": {"id": user},
                         "data": {"custom_id": f"forge:qr_code:{action}"}}, lambda job: True)[1]

    def test_card_after_enough_demand_then_owner_builds(self):
        self.backlog.add(GAP, STATE, 1)
        self.assertFalse(F.consider(GAP))  # one request is not demand yet
        self.backlog.add(GAP, STATE, 1)
        self.assertTrue(F.consider(GAP))
        self.assertFalse(F.consider(GAP))  # offered once
        (_, dm, opened), (_, post, card) = self.discord
        self.assertEqual((dm, opened), ("/users/@me/channels", {"recipient_id": "42"}))
        self.assertEqual(post, "/channels/777/messages")
        self.assertIn("qr_code", card["content"])
        self.assertNotIn("secret-invite", card["content"])  # specifications, never users' requests
        self.assertEqual([b["custom_id"] for b in card["components"][0]["components"]], ["forge:qr_code:build", "forge:qr_code:ignore"])

        self.assertEqual(self.click("build", user="13")["data"]["content"], F.strings()["owner_only"])
        response = self.click("build")
        self.assertEqual(response["type"], I.UPDATE_MESSAGE)
        self.assertEqual(response["data"]["components"], [])
        jobs = list(Path(self.queue).iterdir())
        self.assertEqual(len(jobs), 1)
        request = json.loads((jobs[0] / "request.json").read_text())
        self.assertEqual((request["capability"], request["requests"]), ("qr_code", 2))
        self.assertNotIn("secret-invite", json.dumps(request))
        self.assertEqual(self.click("build")["data"]["content"], F.strings()["unknown"])  # one decision per card

        (jobs[0] / "response.json").write_text(json.dumps({"status": "pr_opened", "pr_url": "https://github.com/o/r/pull/9"}))
        self.assertEqual(F.collect(), 1)
        method, path, updated = self.discord[-1]
        self.assertEqual((method, path), ("PATCH", "/channels/777/messages/777"))
        self.assertIn("https://github.com/o/r/pull/9", updated["content"])
        self.assertEqual(F.collect(), 0)

    def test_untrusted_results_and_ignore(self):
        self.backlog.add(GAP, STATE, 1)
        self.backlog.add(GAP, STATE, 1)
        F.consider(GAP)
        self.click("build")
        job = next(Path(self.queue).iterdir())
        (job / "response.json").write_text(json.dumps({"status": "pr_opened", "pr_url": "https://evil.example/phish"}))
        F.collect()
        self.assertIn(F.strings()["failed"].split(":")[0], self.discord[-1][2]["content"])
        with patch.dict(os.environ, {"FORGE_QUEUE": ""}):
            self.assertFalse(F.enabled())
            self.assertFalse(F.consider(GAP))

    def test_existing_tools_are_never_offered(self):
        tools = Path(os.environ["TOOLS_DIR"])
        source = Path(__file__).resolve().parent.parent / "tools" / "date_calc"
        (tools / "qr_code").mkdir()
        for name in ("tool.py", "tests.json"):
            (tools / "qr_code" / name).write_text((source / name).read_text())
        (tools / "qr_code" / "manifest.json").write_text((source / "manifest.json").read_text().replace('"date_calc"', '"qr_code"'))
        import tool_registry
        with patch.object(tool_registry, "_registry", tool_registry.Registry(tools, Path(self.data) / "tools.sqlite")):
            self.backlog.add(GAP, STATE, 1)
            self.backlog.add(GAP, STATE, 1)
            self.assertFalse(F.consider(GAP))


if __name__ == "__main__":
    unittest.main()
