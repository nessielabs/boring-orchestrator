import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
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
        self.assertEqual(calls[1][2]["prompt"], "")
        self.assertEqual(calls[1][2]["pre_script_timeout_ms"], 600_000)

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
        self.assertEqual(script.count("--untracked-files=all"), 2)


class TrialLifecycleSenderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.campaigns = self.root / "nessie-campaigns"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.campaigns.mkdir()
        for campaign_id in ("trial-needs-activation", "trial-near-expiry"):
            campaign_dir = self.campaigns / "campaigns" / campaign_id
            campaign_dir.mkdir(parents=True)
            (campaign_dir / ".keep").write_text("")

        self.real_git = shutil.which("git")
        assert self.real_git is not None
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Trial Sender Test")
        self._git("config", "user.email", "trial-sender-test@example.com")
        self._git("add", ".")
        self._git("commit", "-m", "Initial campaign fixtures")

        self.fake_python = self.fake_bin / "fake-python"
        self.fake_python.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-c" ]; then
  exit 0
fi
if [ "${1:-}" = "scripts/preview.py" ]; then
  exit 0
fi
if [ "${1:-}" = "scripts/send.py" ]; then
  campaign_id="$2"
  mkdir -p "$NESSIE_CAMPAIGNS_DIR/campaigns/$campaign_id/runs"
  printf 'campaign_id: %s\nsend:\n  attempted: 1\n  succeeded: 1\n  failed: 0\n' "$campaign_id" > "$NESSIE_CAMPAIGNS_DIR/campaigns/$campaign_id/runs/$campaign_id-run.yaml"
  exit 0
fi
if [ "${1:-}" = "-" ]; then
  if [ "$#" -eq 2 ] && [[ "$2" == trial-* ]]; then
    printf '%s\n' "${FAKE_RECIPIENT_COUNT:-0}"
  else
    printf '{"audit_publish":"%s","mode":"automated_send","total":{"attempted":2,"failed":0,"sent":2}}\n' "$2"
  fi
  exit 0
fi
echo "Unexpected fake Python invocation: $*" >&2
exit 1
"""
        )
        self.fake_python.chmod(0o755)

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *args, check=True):
        return subprocess.run(
            [self.real_git, *args],
            cwd=self.campaigns,
            check=check,
            capture_output=True,
            text=True,
        )

    def _run_sender(self, recipient_count, *, sync_git, use_fake_git=False):
        env = os.environ.copy()
        env.update(
            {
                "FAKE_RECIPIENT_COUNT": str(recipient_count),
                "HOME": str(self.root),
                "NESSIE_CAMPAIGNS_DIR": str(self.campaigns),
                "NESSIE_CAMPAIGNS_LOCK_FILE": str(self.root / "sender.lock"),
                "NESSIE_CAMPAIGNS_PYTHON": str(self.fake_python),
                "NESSIE_CAMPAIGNS_SYNC_GIT": str(sync_git),
                "RESEND_API_KEY": "test-only-key",
            }
        )
        if use_fake_git:
            env.update(
                {
                    "GIT_TEST_STATE": str(self.root / "git-push-count"),
                    "REAL_GIT": self.real_git,
                }
            )
        env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        return subprocess.run(
            ["bash", str(SENDER_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
        )

    def _install_git_wrapper_that_fails_after_first_push(self):
        wrapper = self.fake_bin / "git"
        wrapper.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "pull" ]; then
  exit 0
fi
if [ "${1:-}" = "push" ]; then
  count=0
  if [ -f "$GIT_TEST_STATE" ]; then
    count=$(<"$GIT_TEST_STATE")
  fi
  count=$((count + 1))
  printf '%s\n' "$count" > "$GIT_TEST_STATE"
  if [ "$count" -eq 1 ]; then
    exit 0
  fi
  exit 1
fi
exec "$REAL_GIT" "$@"
"""
        )
        wrapper.chmod(0o755)

    def test_delivery_summary_survives_audit_push_failure(self):
        self._install_git_wrapper_that_fails_after_first_push()

        result = self._run_sender(1, sync_git=1, use_fake_git=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["mode"], "automated_send")
        self.assertEqual(summary["audit_publish"], "pending")
        self.assertEqual(summary["total"]["sent"], 2)
        self.assertIn("Delivery completed, but audit publishing is pending", result.stderr)
        self.assertEqual((self.root / "git-push-count").read_text().strip(), "4")
        self.assertEqual(
            self._git("log", "-1", "--pretty=%s").stdout.strip(),
            "Run: trial lifecycle (automated)",
        )

    def test_stale_rebase_is_aborted_before_dirty_tree_check(self):
        conflict_file = self.campaigns / "conflict.txt"
        conflict_file.write_text("base\n")
        self._git("add", "conflict.txt")
        self._git("commit", "-m", "Add conflict fixture")
        self._git("switch", "-c", "upstream")
        conflict_file.write_text("upstream\n")
        self._git("commit", "-am", "Change upstream")
        self._git("switch", "main")
        conflict_file.write_text("main\n")
        self._git("commit", "-am", "Change main")
        rebase = self._git("rebase", "upstream", check=False)
        self.assertNotEqual(rebase.returncode, 0)
        marker_path = self._git(
            "rev-parse", "--git-path", "boring-orchestrator-trial-lifecycle-rebase"
        ).stdout.strip()
        (self.campaigns / marker_path).write_text("")

        result = self._run_sender(0, sync_git=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NESSIE_CAMPAIGNS_REBASE_ABORTED", result.stderr)
        self.assertFalse((self.campaigns / marker_path).exists())
        self.assertEqual(self._git("status", "--porcelain").stdout, "")
        self.assertEqual(conflict_file.read_text(), "main\n")


if __name__ == "__main__":
    unittest.main()
