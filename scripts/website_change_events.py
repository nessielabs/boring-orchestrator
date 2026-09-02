#!/usr/bin/env python3
"""Emit durable, deterministic website-change events for an LLM consumer."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


SCHEMA_VERSION = 1
DEFAULT_API_BASE_URL = "https://api.firecrawl.dev/v2"
EVENT_STATUSES = frozenset({"changed", "removed"})


class MonitorError(RuntimeError):
    """Raised when the producer cannot safely complete a monitoring pass."""


@dataclass(frozen=True)
class Target:
    """One company-to-URL monitoring target."""

    company_id: str
    company_name: str
    url: str
    metadata: dict[str, str]


class StateStore:
    """Atomic JSON state with an exclusive process lock."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_path = state_dir / "state.json"
        self.lock_path = state_dir / "producer.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schemaVersion": SCHEMA_VERSION, "pending": None}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MonitorError(f"cannot read state: {error}") from error
        if state.get("schemaVersion") != SCHEMA_VERSION:
            raise MonitorError("unsupported state schema version")
        return state

    def save(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, temporary_path = tempfile.mkstemp(prefix="state.", suffix=".tmp", dir=self.state_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.state_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)


class FirecrawlClient:
    """Small Firecrawl v2 batch-scrape client with bounded retries."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_API_BASE_URL,
        poll_seconds: float = 2,
        timeout_seconds: int = 1_800,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds

    def scrape(self, urls: Sequence[str], *, tag: str, max_concurrency: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "urls": list(urls),
            "ignoreInvalidURLs": True,
            "formats": [
                "markdown",
                {"type": "changeTracking", "modes": ["git-diff"], "tag": tag},
            ],
            "onlyMainContent": True,
            "maxAge": 0,
            "storeInCache": True,
        }
        if max_concurrency is not None:
            payload["maxConcurrency"] = max_concurrency

        response = self._request("POST", f"{self.base_url}/batch/scrape", payload)
        job_id = response.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise MonitorError("Firecrawl did not return a batch job id")

        deadline = time.monotonic() + self.timeout_seconds
        status_url = f"{self.base_url}/batch/scrape/{urllib.parse.quote(job_id)}"
        while True:
            status = self._request("GET", status_url)
            if status.get("status") == "completed":
                return self._read_pages(status)
            if status.get("status") == "failed":
                raise MonitorError(f"Firecrawl batch {job_id} failed")
            if time.monotonic() >= deadline:
                raise MonitorError(f"Firecrawl batch {job_id} timed out")
            time.sleep(self.poll_seconds)

    def _read_pages(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        current = response
        while True:
            data = current.get("data", [])
            if not isinstance(data, list):
                raise MonitorError("Firecrawl batch returned invalid data")
            pages.extend(page for page in data if isinstance(page, dict))
            next_url = current.get("next")
            if not isinstance(next_url, str) or not next_url:
                return pages
            current = self._request("GET", urllib.parse.urljoin(f"{self.base_url}/", next_url))

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "boring-orchestrator-website-change-events/1",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    parsed = json.load(response)
                if not isinstance(parsed, dict):
                    raise MonitorError("Firecrawl returned a non-object response")
                return parsed
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or error.code >= 500
                if retryable and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                detail = error.read(2_000).decode("utf-8", errors="replace")
                raise MonitorError(f"Firecrawl HTTP {error.code}: {detail}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise MonitorError(f"Firecrawl request failed: {error}") from error
        raise AssertionError("unreachable")


def canonicalize_url(raw_url: str) -> str | None:
    value = raw_url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    hostname = parsed.hostname.lower()
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), hostname, path, parsed.query, ""))


def read_targets(
    input_path: Path,
    *,
    url_column: str,
    name_column: str,
    id_column: str | None,
    metadata_columns: Sequence[str],
) -> tuple[list[Target], int]:
    try:
        handle = input_path.open(newline="", encoding="utf-8-sig")
    except OSError as error:
        raise MonitorError(f"cannot open target CSV: {error}") from error
    with handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        required = {url_column, name_column, *metadata_columns}
        if id_column:
            required.add(id_column)
        missing_headers = sorted(required - headers)
        if missing_headers:
            raise MonitorError(f"target CSV is missing columns: {', '.join(missing_headers)}")

        targets: list[Target] = []
        missing_urls = 0
        seen: set[tuple[str, str]] = set()
        for row_number, row in enumerate(reader, start=2):
            url = canonicalize_url(row.get(url_column, ""))
            if url is None:
                missing_urls += 1
                continue
            name = row.get(name_column, "").strip() or f"row-{row_number}"
            raw_id = row.get(id_column, "").strip() if id_column else ""
            company_id = raw_id or hashlib.sha256(f"{name}\0{url}".encode()).hexdigest()[:24]
            key = (company_id, url)
            if key in seen:
                continue
            seen.add(key)
            metadata = {column: row.get(column, "").strip() for column in metadata_columns}
            targets.append(Target(company_id, name, url, metadata))
    return sorted(targets, key=lambda target: (target.company_id, target.url)), missing_urls


def build_events(
    targets: Sequence[Target],
    pages: Sequence[dict[str, Any]],
    *,
    batch_id: str,
    observed_at: str,
    max_diff_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    targets_by_url: dict[str, list[Target]] = {}
    targets_by_host: dict[str, list[Target]] = {}
    for target in targets:
        targets_by_url.setdefault(target.url, []).append(target)
        host = urllib.parse.urlsplit(target.url).hostname or ""
        targets_by_host.setdefault(host, []).append(target)

    events: list[dict[str, Any]] = []
    unmapped = 0
    for page in pages:
        tracking = page.get("changeTracking")
        if not isinstance(tracking, dict) or tracking.get("changeStatus") not in EVENT_STATUSES:
            continue
        metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
        raw_url = metadata.get("sourceURL") or metadata.get("url")
        source_url = canonicalize_url(raw_url) if isinstance(raw_url, str) else None
        matched = targets_by_url.get(source_url or "", [])
        if not matched and source_url:
            host = urllib.parse.urlsplit(source_url).hostname or ""
            host_targets = targets_by_host.get(host, [])
            if len(host_targets) == 1:
                matched = host_targets
        if not matched:
            unmapped += 1
            continue

        raw_diff = tracking.get("diff")
        diff = raw_diff if isinstance(raw_diff, dict) else {}
        diff_text = raw_diff if isinstance(raw_diff, str) else diff.get("text", "")
        if not isinstance(diff_text, str):
            diff_text = ""
        fingerprint = json.dumps(
            {
                "status": tracking.get("changeStatus"),
                "previousScrapeAt": tracking.get("previousScrapeAt"),
                "diff": diff_text,
                "markdownHash": hashlib.sha256(str(page.get("markdown", "")).encode()).hexdigest(),
            },
            sort_keys=True,
        )
        truncated = len(diff_text) > max_diff_chars
        rendered_diff = diff_text[:max_diff_chars]
        for target in matched:
            event_id = hashlib.sha256(
                f"website-change-v1\0{target.company_id}\0{target.url}\0{fingerprint}".encode()
            ).hexdigest()
            events.append(
                {
                    "schemaVersion": SCHEMA_VERSION,
                    "batchId": batch_id,
                    "eventId": event_id,
                    "eventType": "website.content_changed",
                    "observedAt": observed_at,
                    "company": {
                        "id": target.company_id,
                        "name": target.company_name,
                        "metadata": target.metadata,
                    },
                    "source": {
                        "url": source_url or target.url,
                        "title": metadata.get("title"),
                        "statusCode": metadata.get("statusCode"),
                        "changeStatus": tracking.get("changeStatus"),
                        "previousScrapeAt": tracking.get("previousScrapeAt"),
                        "visibility": tracking.get("visibility"),
                    },
                    "change": {
                        "diff": rendered_diff,
                        "diffTruncated": truncated,
                        "diffSha256": hashlib.sha256(diff_text.encode()).hexdigest(),
                    },
                }
            )
    events.sort(key=lambda event: (event["company"]["id"], event["source"]["url"], event["eventId"]))
    return events, unmapped


def chunks(items: Sequence[Target], size: int) -> Iterable[Sequence[Target]]:
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def read_api_key(api_key_file: Path | None) -> str:
    path = api_key_file or (Path(os.environ["FIRECRAWL_API_KEY_FILE"]) if os.environ.get("FIRECRAWL_API_KEY_FILE") else None)
    if path:
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise MonitorError(f"cannot read Firecrawl API key file: {error}") from error
    else:
        key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        raise MonitorError("set FIRECRAWL_API_KEY or FIRECRAWL_API_KEY_FILE")
    return key


def load_fixture(path: Path) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MonitorError(f"cannot read fixture: {error}") from error
    data = parsed.get("data") if isinstance(parsed, dict) else parsed
    if not isinstance(data, list) or not all(isinstance(page, dict) for page in data):
        raise MonitorError("fixture must be a page array or an object with a data array")
    return data


def emit(events: Sequence[dict[str, Any]]) -> None:
    for event in events:
        print(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def prepare(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)
    with store.locked():
        state = store.load()
        pending = state.get("pending")
        if isinstance(pending, dict) and pending.get("events"):
            emit(pending["events"])
            print(f"replayed {len(pending['events'])} pending events", file=sys.stderr)
            return 0

        targets, missing_urls = read_targets(
            args.input,
            url_column=args.url_column,
            name_column=args.name_column,
            id_column=args.id_column,
            metadata_columns=args.metadata_column,
        )
        if not targets:
            raise MonitorError("target CSV contains no valid URLs")

        batch_id = str(uuid.uuid4())
        observed_at = datetime.now(timezone.utc).isoformat()
        client = None if args.fixture else FirecrawlClient(
            read_api_key(args.api_key_file),
            base_url=args.api_base_url,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout_seconds,
        )
        all_events: list[dict[str, Any]] = []
        unmapped = 0
        target_batches = [targets] if args.fixture else list(chunks(targets, args.batch_size))
        for index, target_batch in enumerate(target_batches):
            pages = load_fixture(args.fixture) if args.fixture else client.scrape(
                [target.url for target in target_batch],
                tag=args.tag,
                max_concurrency=args.max_concurrency,
            )
            events, batch_unmapped = build_events(
                target_batch,
                pages,
                batch_id=batch_id,
                observed_at=observed_at,
                max_diff_chars=args.max_diff_chars,
            )
            unmapped += batch_unmapped
            all_events.extend(events)
            if all_events:
                unique_events = {event["eventId"]: event for event in all_events}
                all_events = sorted(
                    unique_events.values(),
                    key=lambda event: (event["company"]["id"], event["source"]["url"], event["eventId"]),
                )
                state["pending"] = {
                    "batchId": batch_id,
                    "createdAt": observed_at,
                    "events": all_events,
                }
                store.save(state)
            if args.fixture:
                break
            print(f"processed batch {index + 1}/{len(target_batches)}", file=sys.stderr)

        emit(all_events)
        print(
            f"targets={len(targets)} missing_urls={missing_urls} events={len(all_events)} unmapped={unmapped}",
            file=sys.stderr,
        )
        return 0


def acknowledge(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)
    with store.locked():
        state = store.load()
        pending = state.get("pending")
        if not isinstance(pending, dict):
            raise MonitorError("there is no pending event batch")
        if pending.get("batchId") != args.batch_id:
            raise MonitorError("batch id does not match the pending event batch")
        state["pending"] = None
        state["lastAcknowledgedBatchId"] = args.batch_id
        state["lastAcknowledgedAt"] = datetime.now(timezone.utc).isoformat()
        store.save(state)
    print(json.dumps({"acknowledged": args.batch_id}, separators=(",", ":")))
    return 0


def status(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)
    with store.locked():
        state = store.load()
    pending = state.get("pending")
    print(
        json.dumps(
            {
                "pendingBatchId": pending.get("batchId") if isinstance(pending, dict) else None,
                "pendingEvents": len(pending.get("events", [])) if isinstance(pending, dict) else 0,
                "lastAcknowledgedBatchId": state.get("lastAcknowledgedBatchId"),
                "lastAcknowledgedAt": state.get("lastAcknowledgedAt"),
            },
            separators=(",", ":"),
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="emit pending events or collect a new Firecrawl pass")
    prepare_parser.add_argument("--input", type=Path, required=True, help="CSV containing one company website per row")
    prepare_parser.add_argument("--state-dir", type=Path, required=True)
    prepare_parser.add_argument("--url-column", default="url")
    prepare_parser.add_argument("--name-column", default="name")
    prepare_parser.add_argument("--id-column")
    prepare_parser.add_argument("--metadata-column", action="append", default=[])
    prepare_parser.add_argument("--api-key-file", type=Path)
    prepare_parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    prepare_parser.add_argument("--tag", default="boring-orchestrator-daily")
    prepare_parser.add_argument("--batch-size", type=int, default=100)
    prepare_parser.add_argument("--max-concurrency", type=int)
    prepare_parser.add_argument("--poll-seconds", type=float, default=2)
    prepare_parser.add_argument("--timeout-seconds", type=int, default=1_800)
    prepare_parser.add_argument("--max-diff-chars", type=int, default=12_000)
    prepare_parser.add_argument("--fixture", type=Path, help="offline Firecrawl page response fixture")
    prepare_parser.set_defaults(handler=prepare)

    ack_parser = subparsers.add_parser("ack", help="acknowledge the pending batch after successful consumption")
    ack_parser.add_argument("--state-dir", type=Path, required=True)
    ack_parser.add_argument("--batch-id", required=True)
    ack_parser.set_defaults(handler=acknowledge)

    status_parser = subparsers.add_parser("status", help="show pending and acknowledged batch state")
    status_parser.add_argument("--state-dir", type=Path, required=True)
    status_parser.set_defaults(handler=status)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    for name in ("batch_size", "timeout_seconds", "max_diff_chars"):
        if hasattr(args, name) and getattr(args, name) <= 0:
            raise MonitorError(f"--{name.replace('_', '-')} must be positive")
    if hasattr(args, "poll_seconds") and args.poll_seconds < 0:
        raise MonitorError("--poll-seconds cannot be negative")
    if getattr(args, "max_concurrency", None) is not None and args.max_concurrency <= 0:
        raise MonitorError("--max-concurrency must be positive")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        return args.handler(args)
    except MonitorError as error:
        print(f"website-change-events: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
