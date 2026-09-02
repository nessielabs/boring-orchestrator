# Boring Orchestrator

A small local dashboard for defining, scheduling, and watching recurring Claude Code agents.

This is intentionally boring: Express, SQLite, node-cron, and the `claude` CLI already installed in your environment. It does not bundle Claude Code or try to containerize your editor/CLI setup.

## Setup

```bash
npm install
npm run typecheck
npm start
```

By default the server listens on port `44066`:

```bash
open http://localhost:44066
```

To use a different port:

```bash
PORT=3000 npm start
```

## How It Works

- Agents are stored in local SQLite at `boring-orchestrator.db`. Set
  `BORING_ORCHESTRATOR_DATABASE_PATH` to use a different database; the test
  suite uses `:memory:` so it cannot modify the production database.
- A starter `dummy agent` is seeded on first run. Every 30 minutes it captures the current time with `date` and asks Claude Haiku to say hello with that timestamp.
- Cron agents are scheduled with `node-cron`.
- Manual agents can be triggered from the dashboard.
- Webhook agents can be triggered with `POST /webhook` or `POST /webhook/:path`.
- Each run streams its prompt to `claude -p --output-format stream-json` over
  stdin and stores the streamed transcript. Using stdin keeps large pre-script
  payloads out of the process argument list.
- Agents can run on Claude or Codex. Claude uses `claude -p`; Codex uses `codex exec --json --skip-git-repo-check`.
- Codex agents can set a per-agent reasoning effort from `none` through `max`; `xhigh` is labeled "Extra high" in the dashboard.
- The "Dangerously skip permissions" checkbox maps to `--dangerously-skip-permissions` for Claude and `--dangerously-bypass-approvals-and-sandbox` for Codex.
- Optional pre-scripts run before the agent with a configurable per-agent
  timeout. Their stdout is available to the prompt as
  `{{pre_script_output}}`; empty output or a non-zero exit skips the run.
- Script-only agents require a non-empty pre-script, allow an empty prompt,
  record non-empty pre-script output as a successful run, and never launch
  Claude or Codex.

## Deterministic Website Change Events

`scripts/website_change_events.py` turns a CSV of company websites into compact
JSONL change events before an agent is launched. It uses Firecrawl batch scrape
with line-level change tracking, silently establishes first-scrape baselines,
and emits only `changed` or `removed` pages. No LLM participates in target
selection, fetching, diffing, event identity, or retry behavior.
Each page has a bounded scrape timeout so one unresponsive site cannot leave a
multi-site batch running indefinitely.

The producer keeps one pending batch in a private runtime directory. Re-running
`prepare` returns that batch byte-for-byte without calling Firecrawl again.
After the consumer finishes its work and delivers its result, it must
acknowledge the exact `batchId`; failed consumers therefore get the same events
on their next run instead of losing them.

Keep the target CSV, API key, and state outside this repository. For example:

```bash
chmod 600 ~/.config/firecrawl/api-key
python3 scripts/website_change_events.py prepare \
  --input /srv/company-monitor/targets.csv \
  --state-dir /srv/company-monitor/state \
  --name-column 'Company Name' \
  --url-column 'Website URL' \
  --metadata-column 'Source List' \
  --api-key-file ~/.config/firecrawl/api-key
```

Use that command as an agent's pre-script and include
`{{pre_script_output}}` in the prompt. Empty output means no changed pages, so
Boring Orchestrator skips the model call. After successful processing, the
consumer acknowledges the shared batch ID:

```bash
python3 scripts/website_change_events.py ack \
  --state-dir /srv/company-monitor/state \
  --batch-id '<batchId>'
```

Inspect queue state without fetching anything:

```bash
python3 scripts/website_change_events.py status \
  --state-dir /srv/company-monitor/state
```

For offline validation, pass `--fixture response.json` to `prepare`. A fixture
is either an array of Firecrawl page results or an object with a `data` array;
no API credential or network call is used.

### Nessie Trigger Radar

The committed Trigger Radar configuration uses this producer as its pre-script,
then launches Opus only when at least one changed or removed page is emitted.
Opus interprets those events and acknowledges the pending batch only after its
report is delivered. It no longer receives or researches the complete roster.

On Matrix, store the Firecrawl service credential outside the repository and
then apply the safe-disabled configuration:

```bash
mkdir -p ~/.config/firecrawl
chmod 700 ~/.config/firecrawl
# Write the key to ~/.config/firecrawl/api-key, then:
chmod 600 ~/.config/firecrawl/api-key
cd ~/boring-orchestrator
python3 scripts/upsert-trigger-radar-agent.py
```

The upsert deliberately leaves the agent disabled. Establish the first baseline
with `scripts/prepare-trigger-radar-events.sh`, verify that it emits no events,
and only then enable the agent in Boring Orchestrator. The current Ashton roster
contains 525 rows: 444 have valid unique website URLs and 81 need explicit URL
curation before the producer can monitor them. It will not guess missing sites.

## Notes

This is a trusted local tool. Agent prompts and pre-scripts can execute commands in your environment, especially if you enable skipped permissions. Do not expose it to the public internet without adding your own access control.

Generated runtime files are ignored by git:

- `*.db`
- `*.db-shm`
- `*.db-wal`
- `*.log`

## Nessie Trial Lifecycle Outreach

The broader trial lifecycle has three campaign IDs. `new-user-welcome` remains
on its existing separate every-minute automation; this sender owns the other
two, `trial-needs-activation` and `trial-near-expiry`. Every six hours, its
pre-script runs those two deterministic campaign previews. Empty audiences do
nothing. Non-empty
audiences are sent immediately through `scripts/send.py --yes`, and the run
audit files are committed and pushed to `nessie-campaigns`. Recipient
selection, delivery, and auditing run in script-only mode with no LLM call.

The campaign runner remains the source of truth for dev exclusions, global and
campaign-specific suppression, prior-run and provider deduplication, template
variants, unsubscribe tokens, and the final strongly consistent suppression
gate immediately before delivery.

On Matrix, install the campaign dependencies in a repository-local virtual
environment when `python3-venv` is available. Otherwise, install them in the
system Python; the sender validates imports before any preview or send. Store
the Resend key in a mode-600 file so it does not live in an agent prompt or
committed source. Then create or update the agent:

```bash
cd ~/nessie-campaigns
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/nessie-campaigns
chmod 700 ~/.config/nessie-campaigns
# Write the key to ~/.config/nessie-campaigns/resend-api-key, then:
chmod 600 ~/.config/nessie-campaigns/resend-api-key
cd ~/boring-orchestrator
python3 scripts/upsert-trial-lifecycle-agent.py
```

The agent runs every six hours (at minute 39, offset from top-of-hour cron
load, plus up to five minutes of jitter inside the sender script) in
script-only mode with a twenty-minute pre-script timeout. An empty audience creates no run. A non-empty audience creates one compact run summary with aggregate
attempted, sent, failed, not-attempted, suppression, and template-variant
counts. Recipient emails are kept in the campaign repo's audit record and are
not copied into the orchestrator summary. If delivery succeeds but the audit
push fails, the run is still recorded with `audit_publish: pending`; the next
tick retries that audit before it is allowed to resolve or send a new audience.

The committed upsert payload is the source of truth. Running it again replaces
dashboard edits to this agent's schedule, pre-script, or other settings.

## License

MIT
