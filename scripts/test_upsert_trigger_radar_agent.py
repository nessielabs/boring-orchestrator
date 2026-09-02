import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest


SCRIPT = Path(__file__).with_name("upsert-trigger-radar-agent.py")
PREPARE_SCRIPT = Path(__file__).with_name("prepare-trigger-radar-events.sh")
SPEC = importlib.util.spec_from_file_location("upsert_trigger_radar_agent", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class UpsertTriggerRadarAgentTests(unittest.TestCase):
    def run_upsert(self, agents):
        calls = []

        def fake_request(path, *, method="GET", payload=None):
            calls.append((path, method, payload))
            if path == "/api/agents" and method == "GET":
                return agents
            return {"id": "trigger-radar-agent"}

        original_request = MODULE.request
        MODULE.request = fake_request
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                MODULE.main()
        finally:
            MODULE.request = original_request
        return calls

    def test_creates_safe_disabled_event_consumer(self):
        calls = self.run_upsert([])

        self.assertEqual(calls[1][0:2], ("/api/agents", "POST"))
        payload = calls[1][2]
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["script_only"])
        self.assertEqual(payload["model"], "claude-opus-5")
        self.assertIn("prepare-trigger-radar-events.sh", payload["pre_script"])
        self.assertIn("{{pre_script_output}}", payload["prompt"])
        self.assertIn("do not acknowledge", payload["prompt"])

    def test_updates_existing_agent_in_place(self):
        calls = self.run_upsert(
            [{"id": "existing-agent", "name": MODULE.AGENT_NAME, "enabled": 1}]
        )

        self.assertEqual(calls[1][0:2], ("/api/agents/existing-agent", "PUT"))
        self.assertFalse(calls[1][2]["enabled"])

    def test_prepare_script_is_valid_and_uses_fixed_roster_contract(self):
        subprocess.run(["bash", "-n", str(PREPARE_SCRIPT)], check=True)
        script = PREPARE_SCRIPT.read_text()

        self.assertIn("website_change_events.py", script)
        self.assertIn("ashtan-combined-deduplicated.csv", script)
        self.assertIn("--name-column 'Company Name'", script)
        self.assertIn("--url-column 'Website URL'", script)
        self.assertIn("--api-key-file", script)
        self.assertIn("nessie-trigger-radar-daily", script)


if __name__ == "__main__":
    unittest.main()
