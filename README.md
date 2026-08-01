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

- Agents are stored in local SQLite at `boring-orchestrator.db`.
- A starter `dummy agent` is seeded on first run. Every 30 minutes it captures the current time with `date` and asks Claude Haiku to say hello with that timestamp.
- Cron agents are scheduled with `node-cron`.
- Manual agents can be triggered from the dashboard.
- Webhook agents can be triggered with `POST /webhook` or `POST /webhook/:path`.
- Each run executes `claude -p --output-format stream-json` and stores the streamed transcript.
- Agents can run on Claude or Codex. Claude uses `claude -p`; Codex uses `codex exec --json --skip-git-repo-check`.
- Codex agents can set a per-agent reasoning effort from `none` through `max`; `xhigh` is labeled "Extra high" in the dashboard.
- The "Dangerously skip permissions" checkbox maps to `--dangerously-skip-permissions` for Claude and `--dangerously-bypass-approvals-and-sandbox` for Codex.
- Optional pre-scripts run before the agent. Their stdout is available to the prompt as `{{pre_script_output}}`; empty output or a non-zero exit skips the run.

## Notes

This is a trusted local tool. Agent prompts and pre-scripts can execute commands in your environment, especially if you enable skipped permissions. Do not expose it to the public internet without adding your own access control.

Generated runtime files are ignored by git:

- `*.db`
- `*.db-shm`
- `*.db-wal`
- `*.log`

## License

MIT
