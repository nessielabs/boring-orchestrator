import contextlib
import importlib.util
import io
from pathlib import Path
import unittest


SCRIPT = Path(__file__).with_name("upsert-trial-lifecycle-agent.py")
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


if __name__ == "__main__":
    unittest.main()
