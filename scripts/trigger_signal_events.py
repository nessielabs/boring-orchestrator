#!/usr/bin/env python3
"""Durable ATS/RSS batches; collection never acknowledges delivery."""
import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from scripts.website_change_events import MonitorError, StateStore, emit
    from scripts.signal_batches import fits, select_batch, summary
except ImportError:
    from website_change_events import MonitorError, StateStore, emit
    from signal_batches import fits, select_batch, summary


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


def fill_backlog(state, store, args):
    # Rapid follow-up runs drain without refetching. Daily refresh merges new
    # candidates without expiring older queued evidence or starving discovery.
    now = datetime.now(timezone.utc)
    last = state.get("lastCollectedAt")
    due = not last or now - datetime.fromisoformat(last) >= timedelta(hours=24)
    if not state.get("pending") and (not state.get("backlog") or due):
        seen = set(state.get("seen", []))
        queued = {e["eventId"]: e for e in state.get("backlog", [])}
        for event in collect(args):
            if event["eventId"] not in seen:
                queued.setdefault(event["eventId"], event)
        state["backlog"] = list(queued.values())
        state["lastCollectedAt"] = now.isoformat()
        store.save(state)


def inspect_queue(args):
    store = StateStore(args.state_dir)
    with store.locked():
        state = store.load()
        if args.command == "preview":
            fill_backlog(state, store, args)
        print(json.dumps(summary(state, args), sort_keys=True))
    return 0


def prepare(args):
    store = StateStore(args.state_dir)
    with store.locked():
        state = store.load()
        pending = state.get("pending")
        if pending:
            if not fits(pending["events"], args):
                raise MonitorError("pending batch exceeds limits; retained unchanged. Inspect status and reconcile prior delivery before changing limits")
            emit(pending["events"])
            return 0
        fill_backlog(state, store, args)
        batch_id = str(uuid.uuid4())
        events, remaining, blocked = select_batch(state["backlog"], args, batch_id)
        if blocked:
            print(json.dumps({"oversizedCompanies": blocked}), file=sys.stderr)
        if not events and remaining:
            raise MonitorError("all queued companies exceed batch limits; events retained. Inspect status")
        if events:
            state["backlog"] = remaining
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


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("prepare", prepare), ("preview", inspect_queue),
                          ("ack", acknowledge), ("status", inspect_queue)):
        child = sub.add_parser(name)
        child.add_argument("--state-dir", type=Path, required=True)
        child.set_defaults(handler=handler)
        if name != "ack":
            child.add_argument("--max-companies", type=positive_int, default=10)
            child.add_argument("--max-events", type=positive_int, default=40)
            child.add_argument("--max-input-bytes", type=positive_int, default=32_000)
        if name == "ack":
            child.add_argument("--batch-id", required=True)
        if name in ("prepare", "preview"):
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
