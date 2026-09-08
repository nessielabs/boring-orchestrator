import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import signal_batches as batches
from scripts import trigger_signal_events as pipeline


def event(company, number=0, text=""):
    return {"company": {"id": company}, "eventId": f"{company}-{number}", "text": text}


def invoke(command, root, *extra):
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        result = pipeline.main([command, "--state-dir", root, *extra])
    return result, output.getvalue()


class BatchLimitsTest(unittest.TestCase):
    def test_interleaved_company_events_stay_together_and_input_counts_utf8(self):
        events = [event("a", text="你好🙂" * 20), event("b"), event("a", 1)]
        expected = [{**e, "batchId": "0" * 36} for e in (events[0], events[2])]
        cap = batches.input_bytes(expected)
        limits = argparse.Namespace(max_companies=10, max_events=40, max_input_bytes=cap)
        selected, remaining, blocked = batches.select_batch(events, limits, "0" * 36)
        self.assertEqual(selected, expected)
        self.assertEqual(remaining, [events[1]])
        self.assertEqual(blocked, [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            pipeline.emit(selected)
        self.assertEqual(len(output.getvalue().encode("utf-8")), cap)
        limits.max_input_bytes = cap - 1
        selected, remaining, blocked = batches.select_batch(events, limits, "0" * 36)
        self.assertEqual([e["eventId"] for e in selected], ["b-0"])
        self.assertEqual(len(remaining), 2)
        self.assertEqual(blocked[0]["companyId"], "a")

    def test_event_limit_retains_oversized_company_while_other_work_proceeds(self):
        limits = argparse.Namespace(max_companies=10, max_events=2, max_input_bytes=32_000)
        events = [event("large", n) for n in range(3)] + [event("small")]
        selected, remaining, blocked = batches.select_batch(events, limits, "0" * 36)
        self.assertEqual([e["eventId"] for e in selected], ["small-0"])
        self.assertEqual(remaining, events[:3])
        self.assertEqual(blocked[0]["events"], 3)


class BacklogTest(unittest.TestCase):
    def test_preview_then_restart_drain_replay_ack_and_no_duplicate_refetch(self):
        events = [event("a"), event("b"), event("a", 1), event("c")]
        with tempfile.TemporaryDirectory() as root:
            options = ["--registry", "unused", "--max-companies", "1"]
            with patch.object(pipeline, "collect", return_value=events) as collect:
                result, output = invoke("preview", root, *options)
                self.assertEqual(result, 0)
                preview = json.loads(output)
                self.assertEqual(preview["backlog"]["events"], 4)
                self.assertEqual(preview["nextBatch"]["events"], 2)
                self.assertIsNone(preview["pendingBatchId"])
                self.assertEqual(collect.call_count, 1)
            # A fresh invocation uses disk state, even if collection is unavailable.
            with patch.object(pipeline, "collect", side_effect=AssertionError("refetched backlog")):
                emitted = []
                for expected in (["a-0", "a-1"], ["b-0"], ["c-0"]):
                    result, output = invoke("prepare", root, *options)
                    self.assertEqual(result, 0)
                    rows = [json.loads(line) for line in output.splitlines()]
                    self.assertEqual([e["eventId"] for e in rows], expected)
                    self.assertEqual(invoke("prepare", root, *options), (0, output))
                    self.assertEqual(invoke("ack", root, "--batch-id", "wrong")[0], 1)
                    self.assertEqual(invoke("prepare", root, *options), (0, output))
                    self.assertEqual(invoke("ack", root, "--batch-id", rows[0]["batchId"])[0], 0)
                    emitted += expected
                    state = json.loads((Path(root) / "state.json").read_text())
                    self.assertEqual(set(state["seen"]), set(emitted))
                self.assertEqual(state["backlog"], [])
            with patch.object(pipeline, "collect", return_value=events):
                self.assertEqual(invoke("prepare", root, *options), (0, ""))
