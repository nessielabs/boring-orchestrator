import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest


SCRIPT = Path(__file__).with_name("upsert-trial-lifecycle-agent.py")
SENDER_SCRIPT = Path(__file__).with_name("check-trial-lifecycle.sh")
SPEC = importlib.util.spec_from_file_location("upsert_trial_lifecycle_agent", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class UpsertTrialLifecycleAgentTests(unittest.TestCase):
    def test_creates_script_only_agent_when_missing(self):
        calls = []

        def fake_request(path, *, method="GET", payload=None):
            calls.append((path, method, payload))
            if path == "/api/agents" and method == "GET":
                return []
            return {"id": "created-agent"}

        original_request = MODULE.request
        MODULE.request = fake_request
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.main()
        finally:
            MODULE.request = original_request

        self.assertEqual(calls[1][0:2], ("/api/agents", "POST"))
        self.assertTrue(calls[1][2]["script_only"])
        self.assertEqual(calls[1][2]["name"], "Trial lifecycle outreach")
        self.assertIn("sender", calls[1][2]["prompt"])

    def test_updates_existing_agent(self):
        calls = []

        def fake_request(path, *, method="GET", payload=None):
            calls.append((path, method, payload))
            if path == "/api/agents":
                return [{"id": "existing-agent", "name": MODULE.AGENT_NAME}]
            return {"id": "existing-agent"}

        original_request = MODULE.request
        MODULE.request = fake_request
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.main()
        finally:
            MODULE.request = original_request

        self.assertEqual(calls[1][0:2], ("/api/agents/existing-agent", "PUT"))
        self.assertTrue(calls[1][2]["script_only"])

    def test_updates_legacy_preview_agent_in_place(self):
        calls = []

        def fake_request(path, *, method="GET", payload=None):
            calls.append((path, method, payload))
            if path == "/api/agents":
                return [
                    {"id": "legacy-agent", "name": "Trial lifecycle preview"}
                ]
            return {"id": "legacy-agent"}

        original_request = MODULE.request
        MODULE.request = fake_request
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.main()
        finally:
            MODULE.request = original_request

        self.assertEqual(calls[1][0:2], ("/api/agents/legacy-agent", "PUT"))
        self.assertEqual(calls[1][2]["name"], MODULE.AGENT_NAME)

    def test_sender_script_is_valid_and_contains_required_gates(self):
        subprocess.run(["bash", "-n", str(SENDER_SCRIPT)], check=True)
        script = SENDER_SCRIPT.read_text()
        self.assertIn('scripts/preview.py "$campaign_id"', script)
        self.assertIn('scripts/send.py "$campaign_id" --yes', script)
        self.assertIn("publish_audits", script)
        self.assertIn("flock -n", script)


if __name__ == "__main__":
    unittest.main()
