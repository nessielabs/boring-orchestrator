#!/usr/bin/env python3
"""Deterministic buyer-signal producers for Trigger Radar.

Three producers, one event shape. Each reads the source registry written by
``discover_company_sources.py`` (or the raw roster) and emits JSONL events that
carry evidence, never guesses:

``ats``      resolve each company's applicant-tracking board (Greenhouse, Lever,
             Ashby) from its careers page, pull the public JSON feed, and emit
             one event per posting whose description matches the fixed keyword
             list. Companies without a public ATS fall back to a keyword scan of
             the careers page itself (lower confidence, no per-posting dates).
``github``   find each company's GitHub org from its site links, then search the
             org's public repos for agent-configuration files (CLAUDE.md,
             AGENTS.md, .cursor/rules, skills/). Uses the ``gh`` CLI.
``feeds``    probe each blog / news page for an RSS or Atom feed and emit recent
             posts. Plain HTTP, no credits.

State is a JSON file of already-emitted event ids so re-runs only emit new
signals. Pass ``--since-days`` to bound "recent".
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from scripts.website_change_events import MonitorError, canonicalize_url, read_api_key
except ImportError:
    from website_change_events import MonitorError, canonicalize_url, read_api_key

SCHEMA_VERSION = 1
USER_AGENT = "boring-orchestrator-trigger-signals/1"

# Evidence keywords. Weight 3 = names the exact behaviour Nessie sells into;
# weight 2 = internal agent-tooling ownership; weight 1 = general AI adoption.
KEYWORDS: tuple[tuple[str, int, re.Pattern[str]], ...] = tuple(
    (label, weight, re.compile(pattern, re.I))
    for label, weight, pattern in (
        ("claude-code", 3, r"\bclaude[ -]code\b"),
        ("agents-md", 3, r"\bAGENTS\.md\b|\bCLAUDE\.md\b|\.cursor/rules|\bcursorrules\b"),
        ("mcp", 3, r"\bMCP\b|model context protocol"),
        ("context-layer", 3, r"context (layer|engineering|management)|cross-session (memory|context)|shared (skills|context)|skills (library|marketplace)"),
        ("agent-harness", 3, r"agent (harness|orchestrat\w+|framework)|coding agents?\b"),
        ("cursor", 2, r"\bcursor\b(?!\s*(position|pointer))"),
        ("codex", 2, r"\bcodex\b"),
        ("ai-enablement", 2, r"ai enablement|internal ai (platform|tool\w*|team|systems)|ai (operations|ops)\b|applied ai (engineer|lead)"),
        ("internal-agents", 2, r"internal (agents?|automation|tooling)|agents? for (our|internal) (team|ops|operations)"),
        ("copilot", 1, r"\bcopilot\b"),
        ("llm", 1, r"\bLLMs?\b|large language model"),
        ("ai-first", 1, r"ai[- ]native|ai[- ]first|everyone (at \w+ )?uses (claude|cursor|ai)"),
    )
)
MIN_SCORE = 3

ATS_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)", re.I),
    "greenhouse_embed": re.compile(r"boards\.greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)", re.I),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.I),
}
GITHUB_ORG = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)(?:/|$|\")", re.I)
GITHUB_SKIP = {"features", "topics", "sponsors", "orgs", "apps", "marketplace", "login", "join", "about", "pricing", "settings", "explore", "collections", "events", "site", "security", "readme", "search", "trending", "enterprise", "customer-stories", "contact", "team", "new"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def http_get(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30, data: bytes | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read(2000)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        return 0, str(error).encode()


def score_text(text: str) -> tuple[int, list[str]]:
    score = 0
    hits: list[str] = []
    for label, weight, pattern in KEYWORDS:
        if pattern.search(text):
            score += weight
            hits.append(label)
    return score, hits


def excerpt(text: str, hits: Sequence[str], width: int = 220) -> str:
    """Return the sentence around the first highest-weight keyword hit."""
    flat = re.sub(r"\s+", " ", text)
    for label, _, pattern in sorted(KEYWORDS, key=lambda item: -item[1]):
        if label not in hits:
            continue
        match = pattern.search(flat)
        if match:
            start = max(0, match.start() - width // 2)
            return flat[start : start + width].strip()
    return flat[:width]


def event_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(("trigger-signal-v1", *parts)).encode()).hexdigest()


def make_event(kind: str, company: dict[str, str], source: dict[str, Any], evidence: dict[str, Any], *parts: str) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": event_id(kind, company["id"], *parts),
        "eventType": kind,
        "observedAt": utcnow().isoformat(),
        "company": company,
        "source": source,
        "evidence": evidence,
    }


def read_registry(path: Path) -> dict[str, dict[str, Any]]:
    """Group registry rows by company id: {id: {name, homepage, metadata, urls: {type: url}}}."""
    companies: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            cid = row["Company ID"]
            entry = companies.setdefault(
                cid,
                {
                    "id": cid,
                    "name": row["Company Name"],
                    "homepage": row.get("Homepage") or row["Website URL"],
                    "metadata": {k: v for k, v in row.items() if k not in {"Company ID", "Company Name", "Source Type", "Website URL", "Homepage"}},
                    "urls": {},
                },
            )
            entry["urls"].setdefault(row["Source Type"], row["Website URL"])
    return companies


class SeenStore:
    def __init__(self, path: Path):
        self.path = path
        self.seen: set[str] = set()
        if path.exists():
            try:
                self.seen = set(json.loads(path.read_text(encoding="utf-8")).get("seen", []))
            except (OSError, json.JSONDecodeError):
                self.seen = set()

    def filter_new(self, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [event for event in events if event["eventId"] not in self.seen]

    def commit(self, events: Iterable[dict[str, Any]]) -> None:
        self.seen.update(event["eventId"] for event in events)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": sorted(self.seen)}, indent=0), encoding="utf-8")
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- ATS producer


def firecrawl_scrape(api_key: str, url: str, formats: Sequence[Any]) -> dict[str, Any]:
    payload = json.dumps({"url": url, "formats": list(formats), "onlyMainContent": True, "timeout": 45_000}).encode()
    for attempt in range(4):
        status, body = http_get(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=90,
            data=payload,
        )
        if status == 200:
            break
        if status == 429 or status >= 500:
            import time

            time.sleep(50 if status == 429 else 5 * (attempt + 1))
            continue
        raise MonitorError(f"Firecrawl scrape {url} HTTP {status}: {body[:200]!r}")
    else:
        raise MonitorError(f"Firecrawl scrape {url} HTTP {status}: {body[:200]!r}")
    parsed = json.loads(body)
    return parsed.get("data", parsed) if isinstance(parsed, dict) else {}


def detect_ats(links: Iterable[str], html_or_md: str) -> tuple[str, str] | None:
    corpus = "\n".join(links) + "\n" + html_or_md
    for provider, pattern in ATS_PATTERNS.items():
        match = pattern.search(corpus)
        if match:
            slug = match.group(1)
            if slug.lower() in {"embed", "jobs", "job", "boards"}:
                continue
            return provider.replace("_embed", ""), slug
    return None


def ats_get(url: str) -> tuple[int, bytes]:
    """Retry transient board failures before failing the collection closed."""
    import time

    for attempt in range(4):
        status, body = http_get(url)
        if status not in (0, 429) and status < 500:
            return status, body
        if attempt < 3:
            time.sleep(5 * (attempt + 1))
    return status, body


def fetch_ats_postings(provider: str, slug: str) -> list[dict[str, Any]]:
    postings: list[dict[str, Any]] = []
    if provider == "greenhouse":
        status, body = ats_get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
        if status != 200:
            raise MonitorError(f"greenhouse {slug} HTTP {status}")
        for job in json.loads(body).get("jobs", []):
            postings.append(
                {
                    "id": str(job.get("id")),
                    "title": job.get("title", ""),
                    "url": job.get("absolute_url", ""),
                    "postedAt": job.get("first_published") or job.get("updated_at"),
                    "location": (job.get("location") or {}).get("name", ""),
                    "department": ", ".join(d.get("name", "") for d in job.get("departments", []) if isinstance(d, dict)),
                    "text": re.sub(r"<[^>]+>", " ", __import__("html").unescape(job.get("content", ""))),
                }
            )
    elif provider == "lever":
        status, body = ats_get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if status != 200:
            raise MonitorError(f"lever {slug} HTTP {status}")
        for job in json.loads(body):
            created = job.get("createdAt")
            postings.append(
                {
                    "id": str(job.get("id")),
                    "title": job.get("text", ""),
                    "url": job.get("hostedUrl", ""),
                    "postedAt": datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat() if isinstance(created, (int, float)) else None,
                    "location": (job.get("categories") or {}).get("location", ""),
                    "department": (job.get("categories") or {}).get("team", ""),
                    "text": re.sub(r"<[^>]+>", " ", job.get("descriptionPlain") or job.get("description", "")) + " " + " ".join(re.sub(r"<[^>]+>", " ", l.get("content", "")) for l in job.get("lists", []) if isinstance(l, dict)),
                }
            )
    elif provider == "ashby":
        status, body = ats_get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
        if status != 200:
            raise MonitorError(f"ashby {slug} HTTP {status}")
        for job in json.loads(body).get("jobs", []):
            postings.append(
                {
                    "id": str(job.get("id")),
                    "title": job.get("title", ""),
                    "url": job.get("jobUrl", ""),
                    "postedAt": job.get("publishedAt"),
                    "location": job.get("location", ""),
                    "department": job.get("department", "") or job.get("team", ""),
                    "text": re.sub(r"<[^>]+>", " ", job.get("descriptionHtml") or "") or job.get("descriptionPlain", ""),
                }
            )
    else:
        raise MonitorError(f"unknown ATS provider {provider}")
    return postings


def parse_when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def run_ats(args: argparse.Namespace) -> int:
    companies = read_registry(args.registry)
    api_key = read_api_key(args.api_key_file)
    cache_dir = args.state_dir / "ats-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cutoff = utcnow() - timedelta(days=args.since_days)
    targets = [c for c in companies.values() if c["urls"].get("careers")]
    if args.limit:
        targets = targets[: args.limit]
    events: list[dict[str, Any]] = []
    stats = {"companies": len(targets), "ats": 0, "fallback": 0, "postings": 0, "errors": 0}
    errors: dict[str, str] = {}

    def resolve(company: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        cache = cache_dir / f"{company['id']}.json"
        if cache.exists() and not args.refresh:
            try:
                return company, json.loads(cache.read_text(encoding="utf-8")), None
            except (OSError, json.JSONDecodeError):
                pass
        careers_url = company["urls"]["careers"]
        direct_ats = detect_ats([careers_url], "")
        if direct_ats:
            return company, {"careersUrl": careers_url, "ats": direct_ats}, None
        try:
            page = firecrawl_scrape(api_key, careers_url, ["markdown", "links", "html"])
        except MonitorError as error:
            return company, None, str(error)
        links = [l if isinstance(l, str) else l.get("url", "") for l in page.get("links", [])]
        ats = detect_ats(links, page.get("html", "") or "")
        resolved = {"careersUrl": careers_url, "ats": ats, "markdown": page.get("markdown", "")[:20_000]}
        cache.write_text(json.dumps(resolved, ensure_ascii=False), encoding="utf-8")
        return company, resolved, None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        resolved_all = list(pool.map(resolve, targets))

    for company, resolved, error in resolved_all:
        company_ref = {"id": company["id"], "name": company["name"], "homepage": company["homepage"], "metadata": company["metadata"]}
        if error or resolved is None:
            stats["errors"] += 1
            errors[company["name"]] = error or "no result"
            continue
        ats = resolved.get("ats")
        if ats:
            provider, slug = ats
            try:
                postings = fetch_ats_postings(provider, slug)
            except (MonitorError, json.JSONDecodeError) as fetch_error:
                stats["errors"] += 1
                errors[company["name"]] = str(fetch_error)
                continue
            stats["ats"] += 1
            stats["postings"] += len(postings)
            for posting in postings:
                when = parse_when(posting.get("postedAt"))
                if when and when < cutoff:
                    continue
                score, hits = score_text(f"{posting['title']}\n{posting['text']}")
                if score < MIN_SCORE:
                    continue
                events.append(
                    make_event(
                        "careers.posting_matched",
                        company_ref,
                        {"provider": provider, "board": slug, "url": posting["url"], "postedAt": posting.get("postedAt"), "confidence": "high"},
                        {"title": posting["title"], "department": posting["department"], "location": posting["location"], "score": score, "keywords": hits, "excerpt": excerpt(posting["text"], hits)},
                        provider,
                        posting["id"],
                    )
                )
        else:
            stats["fallback"] += 1
            markdown = resolved.get("markdown", "")
            score, hits = score_text(markdown)
            if score >= MIN_SCORE:
                events.append(
                    make_event(
                        "careers.page_matched",
                        company_ref,
                        {"provider": "careers-page", "url": resolved["careersUrl"], "postedAt": None, "confidence": "low"},
                        {"title": None, "score": score, "keywords": hits, "excerpt": excerpt(markdown, hits)},
                        "page",
                        hashlib.sha256(markdown.encode()).hexdigest()[:16],
                    )
                )
    return finish(args, events, stats, errors)


# ------------------------------------------------------------- GitHub producer


def gh_json(args: Sequence[str]) -> Any:
    import time

    for attempt in range(4):
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            if "search/code" in " ".join(args):
                time.sleep(6.5)  # GitHub code search allows 10 requests per minute
            elif "search/" in " ".join(args):
                time.sleep(2.5)  # other GitHub search endpoints allow 30 per minute
            return json.loads(result.stdout or "null")
        if "rate limit" in result.stderr.lower() and attempt < 3:
            time.sleep(65)
            continue
        raise MonitorError(result.stderr.strip()[:300])
    raise MonitorError("gh: retries exhausted")


def _domain(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def lookup_org_by_domain(company: dict[str, Any], cache_path: Path) -> str | None:
    """Search GitHub orgs by company name; accept only an org whose website domain matches."""
    cache: dict[str, str | None] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    if company["id"] in cache:
        return cache[company["id"]]
    domain = _domain(company["homepage"])
    name = re.sub(r"\s*\(.*?\)\s*", " ", company["name"]).strip()
    result: str | None = None
    try:
        candidates = gh_json(["api", "-X", "GET", "search/users", "-f", f"q={name} type:org", "-f", "per_page=5", "--jq", "[.items[].login]"]) or []
        for login in candidates:
            try:
                profile = gh_json(["api", f"users/{login}", "--jq", "{blog: .blog, name: .name}"])
            except MonitorError:
                continue
            blog = profile.get("blog") or ""
            if blog and _domain(blog if "://" in blog else f"https://{blog}") == domain:
                result = login
                break
    except MonitorError:
        result = None
    cache[company["id"]] = result
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=0), encoding="utf-8")
    return result


def run_github(args: argparse.Namespace) -> int:
    companies = read_registry(args.registry)
    cutoff = utcnow() - timedelta(days=args.since_days)
    orgs: dict[str, dict[str, Any]] = {}
    for company in companies.values():
        cache = args.map_cache_dir / f"{company['id']}.json"
        corpus = ""
        if cache.exists():
            corpus = cache.read_text(encoding="utf-8", errors="replace")
        ats_cache = args.state_dir / "ats-cache" / f"{company['id']}.json"
        if ats_cache.exists():
            corpus += ats_cache.read_text(encoding="utf-8", errors="replace")
        found = None
        for match in GITHUB_ORG.finditer(corpus):
            org = match.group(1)
            if org.lower() in GITHUB_SKIP:
                continue
            found = org
            break
        if not found:
            found = lookup_org_by_domain(company, args.state_dir / "github-org-cache.json")
        if found:
            orgs.setdefault(found.lower(), {"org": found, "company": company})
    targets = list(orgs.values())[: args.limit] if args.limit else list(orgs.values())
    events: list[dict[str, Any]] = []
    stats = {"companies": len(companies), "orgsFound": len(orgs), "repoHits": 0, "errors": 0}
    errors: dict[str, str] = {}
    for entry in targets:
        org, company = entry["org"], entry["company"]
        company_ref = {"id": company["id"], "name": company["name"], "homepage": company["homepage"], "metadata": company["metadata"]}
        hits: list[dict[str, str]] = []
        failed = False
        for qualifier in ("filename:CLAUDE.md", "filename:AGENTS.md", "path:.cursor/rules", "path:.claude/skills"):
            try:
                hits.extend(gh_json(["api", "-X", "GET", "search/code", "-f", f"q=org:{org} {qualifier}", "-f", "per_page=30", "--jq", "[.items[] | {repo: .repository.full_name, path: .path, url: .html_url}]"]) or [])
            except MonitorError as error:
                failed = True
                errors[org] = str(error)
                break
        if failed:
            stats["errors"] += 1
            continue
        by_repo: dict[str, list[dict[str, str]]] = {}
        for hit in hits or []:
            by_repo.setdefault(hit["repo"], []).append(hit)
        for repo, files in by_repo.items():
            try:
                meta = gh_json(["api", f"repos/{repo}", "--jq", "{pushed: .pushed_at, stars: .stargazers_count, fork: .fork, desc: .description}"])
            except MonitorError:
                meta = {}
            pushed = parse_when(meta.get("pushed")) if meta else None
            if pushed and pushed < cutoff:
                continue
            if meta.get("fork"):
                continue
            stats["repoHits"] += 1
            events.append(
                make_event(
                    "github.agent_config_found",
                    company_ref,
                    {"provider": "github", "org": org, "repo": repo, "url": f"https://github.com/{repo}", "pushedAt": meta.get("pushed"), "confidence": "high"},
                    {"files": [f["path"] for f in files][:10], "stars": meta.get("stars"), "description": meta.get("desc"), "score": 3, "keywords": ["agents-md"]},
                    repo,
                    ",".join(sorted(f["path"] for f in files)),
                )
            )
    return finish(args, events, stats, errors)


# -------------------------------------------------------------- feeds producer

FEED_SUFFIXES = ("/feed", "/rss.xml", "/feed.xml", "/rss", "/atom.xml", "/index.xml", "/feed/", "/blog/feed", "/blog/rss.xml")


def discover_feed(page_url: str) -> tuple[str, bytes] | None:
    status, body = http_get(page_url, timeout=20)
    if status == 200:
        text = body.decode("utf-8", errors="replace")
        for match in re.finditer(r"<link[^>]+type=[\"'](?:application/(?:rss|atom)\+xml)[\"'][^>]*>", text, re.I):
            href = re.search(r"href=[\"']([^\"']+)[\"']", match.group(0))
            if href:
                feed_url = urllib.parse.urljoin(page_url, href.group(1))
                s, b = http_get(feed_url, timeout=20)
                if s == 200 and b.lstrip().startswith(b"<"):
                    return feed_url, b
    base = page_url.rstrip("/")
    root = f"{urllib.parse.urlsplit(page_url).scheme}://{urllib.parse.urlsplit(page_url).netloc}"
    for candidate in [base + s for s in FEED_SUFFIXES] + [root + s for s in FEED_SUFFIXES]:
        s, b = http_get(candidate, timeout=15)
        if s == 200 and b.lstrip().startswith(b"<") and (b"<rss" in b[:2000] or b"<feed" in b[:2000]):
            return candidate, b
    return None


def parse_feed(body: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items: list[dict[str, Any]] = []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for item in root.iter("item"):
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "url": (item.findtext("link") or "").strip(),
                "publishedAt": item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date"),
                "text": re.sub(r"<[^>]+>", " ", (item.findtext("description") or "") + " " + (item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded") or "")),
            }
        )
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        link = entry.find("atom:link", ns)
        items.append(
            {
                "title": (entry.findtext("atom:title", default="", namespaces=ns) or "").strip(),
                "url": link.get("href", "") if link is not None else "",
                "publishedAt": entry.findtext("atom:published", default=None, namespaces=ns) or entry.findtext("atom:updated", default=None, namespaces=ns),
                "text": re.sub(r"<[^>]+>", " ", (entry.findtext("atom:summary", default="", namespaces=ns) or "") + " " + (entry.findtext("atom:content", default="", namespaces=ns) or "")),
            }
        )
    return items


def run_feeds(args: argparse.Namespace) -> int:
    companies = read_registry(args.registry)
    cutoff = utcnow() - timedelta(days=args.since_days)
    cache_path = args.state_dir / "feed-urls.json"
    feed_cache: dict[str, str | None] = {}
    if cache_path.exists():
        try:
            feed_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            feed_cache = {}
    targets: list[tuple[dict[str, Any], str, str]] = []
    feed_companies = [c for c in companies.values() if any(c["urls"].get(t) for t in ("blog", "news", "changelog"))]
    if args.limit:
        feed_companies = feed_companies[:args.limit]
    for company in feed_companies:
        for source_type in ("blog", "news", "changelog"):
            url = company["urls"].get(source_type)
            if url:
                targets.append((company, source_type, url))
    events: list[dict[str, Any]] = []
    stats = {"companies": len(feed_companies), "pages": len(targets), "feedsFound": 0, "recentPosts": 0, "matchedPosts": 0}

    def probe(target: tuple[dict[str, Any], str, str]) -> tuple[tuple[dict[str, Any], str, str], str | None, list[dict[str, Any]]]:
        company, source_type, url = target
        if url in feed_cache and not args.refresh:
            feed_url = feed_cache[url]
            if not feed_url:
                return target, None, []
            s, b = http_get(feed_url, timeout=20)
            return target, feed_url, parse_feed(b) if s == 200 else []
        found = discover_feed(url)
        if not found:
            return target, None, []
        return target, found[0], parse_feed(found[1])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(probe, targets))

    for (company, source_type, url), feed_url, items in results:
        feed_cache[url] = feed_url
        if not feed_url:
            continue
        stats["feedsFound"] += 1
        company_ref = {"id": company["id"], "name": company["name"], "homepage": company["homepage"], "metadata": company["metadata"]}
        for item in items:
            when = parse_when(item.get("publishedAt"))
            if not when or when < cutoff:
                continue
            stats["recentPosts"] += 1
            score, hits = score_text(f"{item['title']}\n{item['text']}")
            if score < MIN_SCORE:
                continue
            events.append(
                make_event(
                    f"{source_type}.post_published",
                    company_ref,
                    {"provider": "rss", "feed": feed_url, "url": item["url"], "publishedAt": item.get("publishedAt"), "confidence": "high"},
                    {"title": item["title"], "score": score, "keywords": hits, "excerpt": excerpt(item["text"], hits) if hits else re.sub(r"\s+", " ", item["text"])[:220]},
                    item["url"] or item["title"],
                )
            )
            stats["matchedPosts"] += 1
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(feed_cache, indent=0), encoding="utf-8")
    return finish(args, events, stats, {})


# ---------------------------------------------------------------------- shared


def finish(args: argparse.Namespace, events: list[dict[str, Any]], stats: dict[str, Any], errors: dict[str, str]) -> int:
    store = SeenStore(args.state_dir / f"seen-{args.command}.json")
    fresh = store.filter_new(events) if not args.replay else events
    fresh.sort(key=lambda e: (-e["evidence"].get("score", 0), e["company"]["name"]))
    out = args.output.open("a", encoding="utf-8") if args.output else sys.stdout
    try:
        for event in fresh:
            out.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    finally:
        if args.output:
            out.close()
    if not args.dry_run and not errors:
        store.commit(fresh)
    stats["events"] = len(fresh)
    stats["suppressedSeen"] = len(events) - len(fresh)
    print(json.dumps(stats, separators=(",", ":")), file=sys.stderr)
    if errors:
        print(json.dumps({"errors": errors}, ensure_ascii=False)[:4000], file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--registry", type=Path, required=True, help="source-registry.csv from discover_company_sources.py")
        p.add_argument("--state-dir", type=Path, required=True)
        p.add_argument("--output", type=Path, help="append events JSONL here instead of stdout")
        p.add_argument("--since-days", type=int, default=14)
        p.add_argument("--limit", type=int)
        p.add_argument("--workers", type=int, default=6)
        p.add_argument("--refresh", action="store_true", help="ignore resolution caches")
        p.add_argument("--replay", action="store_true", help="emit already-seen events too")
        p.add_argument("--dry-run", action="store_true", help="do not record emitted events as seen")

    ats = sub.add_parser("ats", help="applicant-tracking feeds with keyword classification")
    common(ats)
    ats.add_argument("--api-key-file", type=Path)
    ats.set_defaults(handler=run_ats)

    github = sub.add_parser("github", help="agent-config files in company GitHub orgs")
    common(github)
    github.add_argument("--map-cache-dir", type=Path, required=True, help="map cache from discover_company_sources.py")
    github.set_defaults(handler=run_github)

    feeds = sub.add_parser("feeds", help="recent posts from blog/news/changelog RSS feeds")
    common(feeds)
    feeds.set_defaults(handler=run_feeds)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except MonitorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
