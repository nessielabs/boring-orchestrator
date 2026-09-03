#!/usr/bin/env python3
"""Discover per-company monitoring sources (careers, news, blog, changelog) with Firecrawl map.

Reads the same company roster the website-change producer consumes, asks
Firecrawl's map endpoint for every URL it knows on each company's site, and
classifies the results into a deterministic source registry: one row per
(company, source type, URL). The registry is itself a valid producer input, so
``website_change_events.py prepare`` can monitor careers pages and newsrooms
exactly the way it already monitors homepages.

Map results are cached per company so re-running discovery is free unless
``--refresh`` is passed. No model is involved: classification is regex-based and
the chosen URL for each type is the shortest matching path on the company's own
host, which is the index page rather than an individual article or posting.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

try:
    from scripts.website_change_events import (
        DEFAULT_API_BASE_URL,
        MonitorError,
        canonicalize_url,
        read_api_key,
        read_targets,
    )
except ImportError:  # executed directly from the scripts directory
    from website_change_events import DEFAULT_API_BASE_URL, MonitorError, canonicalize_url, read_api_key, read_targets

REGISTRY_ID_COLUMN = "Company ID"
REGISTRY_NAME_COLUMN = "Company Name"
REGISTRY_TYPE_COLUMN = "Source Type"
REGISTRY_URL_COLUMN = "Website URL"
REGISTRY_HOMEPAGE_COLUMN = "Homepage"

# Ordered so the registry is stable and the most decision-relevant sources come first.
SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("careers", re.compile(r"^/(?:[a-z]{2}/)?(?:careers?|jobs?|join(?:-us)?|hiring|open-?roles|openings|positions|work-with-us|we-are-hiring)/?$", re.I)),
    ("news", re.compile(r"^/(?:[a-z]{2}/)?(?:news|newsroom|press|press-releases?|media|announcements|in-the-news)/?$", re.I)),
    ("blog", re.compile(r"^/(?:[a-z]{2}/)?(?:blog|insights|articles|resources/blog|category/blog)/?$", re.I)),
    ("changelog", re.compile(r"^/(?:[a-z]{2}/)?(?:changelog|release-?notes|releases|updates|whats-new|product-updates|docs/changelog)/?$", re.I)),
    ("events", re.compile(r"^/(?:[a-z]{2}/)?(?:events|webinars?)/?$", re.I)),
    ("customers", re.compile(r"^/(?:[a-z]{2}/)?(?:customers|case-?studies|success-stories|customer-stories)/?$", re.I)),
    ("pricing", re.compile(r"^/(?:[a-z]{2}/)?pricing/?$", re.I)),
)
DEFAULT_SOURCE_TYPES = ("careers", "news", "blog", "changelog")

# External applicant-tracking hosts. Map rarely returns them, but when it does the
# job board is a better careers source than a marketing page that links to it.
ATS_HOST_PATTERN = re.compile(
    r"(?:^|\.)(?:boards\.greenhouse\.io|job-boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|"
    r"apply\.workable\.com|ats\.rippling\.com|jobs\.gem\.com|wellfound\.com|jobs\.dover\.com)$",
    re.I,
)


@dataclass(frozen=True)
class DiscoveredSource:
    source_type: str
    url: str


def _host_family(hostname: str) -> str:
    """Treat www.example.com and example.com as the same site."""
    host = hostname.lower()
    return host[4:] if host.startswith("www.") else host


def classify_links(homepage: str, links: Sequence[str], source_types: Sequence[str]) -> list[DiscoveredSource]:
    """Pick one canonical URL per requested source type from a map result."""
    home = urllib.parse.urlsplit(homepage)
    home_family = _host_family(home.hostname or "")
    home_path = home.path.rstrip("/") or "/"

    candidates: dict[str, list[tuple[int, int, int, str]]] = {}
    for raw in links:
        url = canonicalize_url(raw) if isinstance(raw, str) else None
        if url is None:
            continue
        parsed = urllib.parse.urlsplit(url)
        hostname = parsed.hostname or ""
        if ATS_HOST_PATTERN.search(hostname):
            if "careers" in source_types:
                candidates.setdefault("careers", []).append((0, len(parsed.path), len(url), url))
            continue
        family = _host_family(hostname)
        same_site = family == home_family or family.endswith("." + home_family)
        if not same_site:
            continue
        path = parsed.path.rstrip("/") or "/"
        if path == home_path:
            continue
        for source_type, pattern in SOURCE_PATTERNS:
            if source_type not in source_types or not pattern.match(path):
                continue
            # Prefer: no query string (pagination), then same host over subdomain, then shorter path.
            query_penalty = 20 if parsed.query else 0
            subdomain_penalty = 0 if family == home_family else 10
            candidates.setdefault(source_type, []).append(
                (query_penalty + subdomain_penalty, len(path), len(url), url)
            )
            break

    chosen: list[DiscoveredSource] = []
    for source_type in source_types:
        ranked = sorted(candidates.get(source_type, []))
        if ranked:
            chosen.append(DiscoveredSource(source_type, ranked[0][3]))
    return chosen


class FirecrawlMapClient:
    """Minimal Firecrawl v2 map client."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_API_BASE_URL, limit: int = 500):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.limit = limit

    def map(self, url: str) -> list[str]:
        payload = {"url": url, "limit": self.limit, "sitemap": "include"}
        request = urllib.request.Request(
            f"{self.base_url}/map",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "boring-orchestrator-discover-company-sources/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                parsed = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(500).decode("utf-8", errors="replace")
            raise MonitorError(f"Firecrawl HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise MonitorError(f"Firecrawl map failed: {error}") from error
        if not isinstance(parsed, dict):
            raise MonitorError("Firecrawl map returned a non-object response")
        links = parsed.get("links", [])
        if not isinstance(links, list):
            raise MonitorError("Firecrawl map returned invalid links")
        return [link.get("url") if isinstance(link, dict) else link for link in links if link]


def cached_map(client: FirecrawlMapClient, cache_dir: Path, company_id: str, url: str, *, refresh: bool) -> list[str]:
    cache_path = cache_dir / f"{company_id}.json"
    if cache_path.exists() and not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("url") == url and isinstance(cached.get("links"), list):
            return cached["links"]
    links = client.map(url)
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"url": url, "links": links}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache_path)
    return links


def write_registry(
    output: Path,
    rows: Sequence[dict[str, str]],
    metadata_columns: Sequence[str],
) -> None:
    fieldnames = [
        REGISTRY_ID_COLUMN,
        REGISTRY_NAME_COLUMN,
        REGISTRY_TYPE_COLUMN,
        REGISTRY_URL_COLUMN,
        REGISTRY_HOMEPAGE_COLUMN,
        *metadata_columns,
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, output)


def discover(args: argparse.Namespace) -> int:
    source_types = tuple(dict.fromkeys(args.source_type)) if args.source_type else DEFAULT_SOURCE_TYPES
    unknown = sorted(set(source_types) - {name for name, _ in SOURCE_PATTERNS})
    if unknown:
        raise MonitorError(f"unknown source types: {', '.join(unknown)}")

    targets, missing_urls = read_targets(
        args.input,
        url_column=args.url_column,
        name_column=args.name_column,
        id_column=args.id_column,
        metadata_columns=args.metadata_column,
    )
    if not targets:
        raise MonitorError("roster contains no valid URLs")
    if args.limit:
        targets = targets[: args.limit]

    client = FirecrawlMapClient(read_api_key(args.api_key_file), base_url=args.api_base_url, limit=args.map_limit)
    errors: dict[str, str] = {}
    discovered: dict[str, list[DiscoveredSource]] = {}
    link_counts: dict[str, int] = {}

    def run(target: Any) -> None:
        try:
            links = cached_map(client, args.cache_dir, target.company_id, target.url, refresh=args.refresh)
        except MonitorError as error:
            errors[target.company_id] = str(error)
            return
        link_counts[target.company_id] = len(links)
        discovered[target.company_id] = classify_links(target.url, links, source_types)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, targets))

    rows: list[dict[str, str]] = []
    per_type: dict[str, int] = {name: 0 for name in source_types}
    for target in targets:
        base = {
            REGISTRY_ID_COLUMN: target.company_id,
            REGISTRY_NAME_COLUMN: target.company_name,
            REGISTRY_HOMEPAGE_COLUMN: target.url,
            **{column: target.metadata.get(column, "") for column in args.metadata_column},
        }
        if args.include_homepage:
            rows.append({**base, REGISTRY_TYPE_COLUMN: "homepage", REGISTRY_URL_COLUMN: target.url})
        for source in discovered.get(target.company_id, []):
            per_type[source.source_type] += 1
            rows.append({**base, REGISTRY_TYPE_COLUMN: source.source_type, REGISTRY_URL_COLUMN: source.url})

    write_registry(args.output, rows, args.metadata_column)
    if args.errors_file:
        args.errors_file.parent.mkdir(parents=True, exist_ok=True)
        args.errors_file.write_text(json.dumps(errors, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    companies_with_any = sum(1 for company_id in discovered if discovered[company_id])
    summary = {
        "companies": len(targets),
        "missingUrls": missing_urls,
        "mapped": len(discovered),
        "mapErrors": len(errors),
        "companiesWithAnySource": companies_with_any,
        "registryRows": len(rows),
        "perType": per_type,
    }
    print(json.dumps(summary, separators=(",", ":")), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True, help="company roster CSV")
    parser.add_argument("--output", type=Path, required=True, help="registry CSV to write")
    parser.add_argument("--cache-dir", type=Path, required=True, help="per-company map cache directory")
    parser.add_argument("--errors-file", type=Path, help="JSON file of company id -> map error")
    parser.add_argument("--url-column", default="url")
    parser.add_argument("--name-column", default="name")
    parser.add_argument("--id-column")
    parser.add_argument("--metadata-column", action="append", default=[])
    parser.add_argument("--source-type", action="append", help=f"repeatable; default {', '.join(DEFAULT_SOURCE_TYPES)}")
    parser.add_argument("--include-homepage", action="store_true", help="also emit a homepage row per company")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL)
    parser.add_argument("--map-limit", type=int, default=500)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, help="only process the first N companies (for canaries)")
    parser.add_argument("--refresh", action="store_true", help="ignore cached map results")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workers <= 0 or args.map_limit <= 0:
        raise MonitorError("--workers and --map-limit must be positive")
    try:
        return discover(args)
    except MonitorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
