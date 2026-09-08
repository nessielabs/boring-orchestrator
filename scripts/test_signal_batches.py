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
    def test_bulk_selection_bounds_serialization_work_and_preserves_event_limit(self):
        events = [event(str(company), n, "你好🙂") for company in range(100) for n in range(2)]
        limits = argparse.Namespace(max_companies=80, max_events=150, max_input_bytes=100_000)
        with patch.object(batches, "input_bytes", wraps=batches.input_bytes) as size:
            selected, remaining, blocked = batches.select_batch(events, limits, "0" * 36)
        self.assertEqual([e["eventId"] for e in selected], [e["eventId"] for e in events[:150]])
        self.assertEqual(remaining, events[150:])
        self.assertEqual(blocked, [])
        # Guard against repeatedly serializing already-selected groups at bulk limits.
        self.assertEqual(sum(len(call.args[0]) for call in size.call_args_list), len(events))

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
    def test_daily_refresh_merges_new_events_without_expiring_queued_work(self):
        with tempfile.TemporaryDirectory() as root:
            store = pipeline.StateStore(Path(root))
            with store.locked():
                store.save({"schemaVersion": 1, "pending": None,
                            "lastCollectedAt": "2020-01-01T00:00:00+00:00",
                            "seen": ["done-0"], "backlog": [event("old")]})
            with patch.object(pipeline, "collect", return_value=[event("new"), event("done")]):
                result, output = invoke("preview", root, "--registry", "unused")
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(output)["backlog"]["events"], 2)
            state = store.load()
            self.assertEqual([e["eventId"] for e in state["backlog"]], ["old-0", "new-0"])
            self.assertNotEqual(state["lastCollectedAt"], "2020-01-01T00:00:00+00:00")

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

    def test_oversized_backlog_is_durable_and_can_be_released_with_explicit_limit(self):
        events = [event("large", n) for n in range(3)]
        with tempfile.TemporaryDirectory() as root:
            options = ["--registry", "unused", "--max-events", "2"]
            with patch.object(pipeline, "collect", return_value=events):
                self.assertEqual(invoke("prepare", root, *options), (1, ""))
            state_path = Path(root) / "state.json"
            before = state_path.read_bytes()
            with patch.object(pipeline, "collect", side_effect=AssertionError("unexpected fetch")):
                info = json.loads(invoke("status", root, "--max-events", "2")[1])
                self.assertEqual(info["oversizedCompanies"][0]["companyId"], "large")
                self.assertEqual(info["backlog"]["events"], 3)
                self.assertEqual(state_path.read_bytes(), before)
                result, output = invoke("prepare", root, "--registry", "unused", "--max-events", "3")
                self.assertEqual(result, 0)
                self.assertEqual(len(output.splitlines()), 3)

    def test_existing_pending_batch_is_never_silently_split_on_lower_limit(self):
        with tempfile.TemporaryDirectory() as root:
            store = pipeline.StateStore(Path(root))
            legacy = {"schemaVersion": 1, "pending": {"batchId": "legacy-id",
                      "events": [{**event("a"), "batchId": "legacy-id"},
                                 {**event("b"), "batchId": "legacy-id"}]}}
            with store.locked():
                store.save(legacy)
            before = store.state_path.read_bytes()
            with patch.object(pipeline, "collect", side_effect=AssertionError("unexpected fetch")):
                self.assertEqual(invoke("prepare", root, "--registry", "unused", "--max-companies", "1"), (1, ""))
                info = json.loads(invoke("status", root, "--max-companies", "1")[1])
                self.assertTrue(info["pendingExceedsLimits"])
                self.assertEqual(store.state_path.read_bytes(), before)
                result, output = invoke("prepare", root, "--registry", "unused")
                self.assertEqual(result, 0)
                self.assertEqual([json.loads(line) for line in output.splitlines()], legacy["pending"]["events"])

    def test_empty_preview_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as root, patch.object(pipeline, "collect", return_value=[]):
            info = json.loads(invoke("preview", root, "--registry", "unused")[1])
            self.assertEqual(info["nextBatch"]["events"], 0)
            self.assertIsNone(info["pendingBatchId"])
            for flag in ("--max-events", "--max-companies", "--max-input-bytes"):
                with self.assertRaises(SystemExit) as error:
                    invoke("prepare", root, "--registry", "unused", flag, "0")
                self.assertEqual(error.exception.code, 2)
