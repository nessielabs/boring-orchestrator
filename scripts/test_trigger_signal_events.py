import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import trigger_signal_events as pipeline


class DeliveryTest(unittest.TestCase):
    def test_retry_replays_without_refetch_and_only_exact_ack_suppresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ["prepare", "--state-dir", tmp, "--registry", "unused.csv"]
            event = {"eventId": "real-id", "evidence": {"title": "A posting"}}
            with patch.object(pipeline, "collect", return_value=[event]) as fetch:
                first, second = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(first):
                    self.assertEqual(pipeline.main(args), 0)
                with contextlib.redirect_stdout(second):
                    self.assertEqual(pipeline.main(args), 0)
                self.assertEqual(first.getvalue(), second.getvalue())
                self.assertEqual(fetch.call_count, 1)
                batch = json.loads(first.getvalue())["batchId"]
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(pipeline.main(["ack", "--state-dir", tmp, "--batch-id", "wrong"]), 1)
                self.assertIsNotNone(json.loads((Path(tmp) / "state.json").read_text())["pending"])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(pipeline.main(["ack", "--state-dir", tmp, "--batch-id", batch]), 0)
                after = io.StringIO()
                with contextlib.redirect_stdout(after):
                    self.assertEqual(pipeline.main(args), 0)
                self.assertEqual(after.getvalue(), "")
                self.assertEqual(fetch.call_count, 2)

    def test_failed_collection_does_not_record_seen_or_publish_partial_batch(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "collect", side_effect=pipeline.MonitorError("feed failed")):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(pipeline.main(["prepare", "--state-dir", tmp, "--registry", "unused.csv"]), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertFalse((Path(tmp) / "state.json").exists())
