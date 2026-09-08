#!/usr/bin/env python3
"""Durable ATS/RSS batches; collection never acknowledges delivery."""
import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

try:
    from scripts.website_change_events import MonitorError, StateStore, emit, status
except ImportError:
    from website_change_events import MonitorError, StateStore, emit, status


def collect(args):
    events = {}
    for producer in args.producers:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "events.jsonl"
            command = [sys.executable, str(Path(__file__).with_name("trigger_signals.py")), producer,
                       "--registry", str(args.registry), "--state-dir", str(args.state_dir / "cache"),
                       "--output", str(output), "--dry-run", "--replay",
                       "--since-days", str(args.since_days), "--workers", str(args.workers)]
            if args.limit:
                command += ["--limit", str(args.limit)]
            if producer == "ats" and args.api_key_file:
                command += ["--api-key-file", str(args.api_key_file)]
            try:
                result = subprocess.run(command, timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired as error:
                raise MonitorError(f"{producer} collection timed out") from error
            if result.returncode:
                raise MonitorError(f"{producer} collection failed; no batch acknowledged")
            for line in output.read_text().splitlines():
                event = json.loads(line)
                events[event["eventId"]] = event
    return sorted(events.values(), key=lambda e: e["eventId"])


def prepare(args):
    store = StateStore(args.state_dir)
    with store.locked():
        state = store.load()
        pending = state.get("pending")
        if pending:
            emit(pending["events"])
            return 0
        seen = set(state.get("seen", []))
        events = [e for e in collect(args) if e["eventId"] not in seen]
        if events:
            batch_id = str(uuid.uuid4())
            for event in events:
                event["batchId"] = batch_id
            state["pending"] = {"batchId": batch_id, "events": events}
            store.save(state)
        emit(events)
    return 0


def acknowledge(args):
    store = StateStore(args.state_dir)
    with store.locked():
        state = store.load()
        pending = state.get("pending")
        if not pending or pending["batchId"] != args.batch_id:
            raise MonitorError("batch id does not match the pending event batch")
        state["seen"] = sorted(set(state.get("seen", [])) | {e["eventId"] for e in pending["events"]})
        state["pending"] = None
        state["lastAcknowledgedBatchId"] = args.batch_id
        store.save(state)
    print(json.dumps({"acknowledged": args.batch_id}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("prepare", prepare), ("ack", acknowledge), ("status", status)):
        child = sub.add_parser(name)
        child.add_argument("--state-dir", type=Path, required=True)
        child.set_defaults(handler=handler)
        if name == "ack":
            child.add_argument("--batch-id", required=True)
        if name == "prepare":
            child.add_argument("--registry", type=Path, required=True)
            child.add_argument("--api-key-file", type=Path)
            child.add_argument("--producers", nargs="+", choices=("ats", "feeds"), default=["ats", "feeds"])
            child.add_argument("--since-days", type=int, default=14)
            child.add_argument("--limit", type=int)
            child.add_argument("--workers", type=int, default=3)
            child.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (MonitorError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
