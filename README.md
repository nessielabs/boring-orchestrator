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

- Agents are stored in SQLite under `$XDG_STATE_HOME/boring-orchestrator/`
  (default `~/.local/state/boring-orchestrator/`). Set
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

## Installation-specific automation

Keep job scripts and operational configuration in a separate deployment repository.
Pre-scripts can invoke those scripts by absolute path; they do not need to live
inside this application's source checkout. The application supplies the scheduler,
executor, dashboard, and database schema without bundling a particular organization's jobs.

Prompts, schedules, run history, credentials, logs, and generated queues are runtime
data. Keep them outside the source checkout and outside version control. Set
`BORING_ORCHESTRATOR_DATABASE_PATH` to the deployment's private SQLite file.
If a legacy database exists in the checkout and no explicit path is set, startup
refuses to create an empty replacement. Stop the application, back up and move
the database, set the explicit path, then restart and verify existing history.

New database directories are created with mode `0700` on POSIX systems. Existing
directories keep their permissions; the engine does not change shared parent
directories such as `~/.local`. When selecting an explicit database path, use a
directory that is already private to the account running the engine.

This is a trusted local tool. Agent prompts and pre-scripts can execute commands
in your environment. Do not expose it publicly without your own access control.

## License

MIT
