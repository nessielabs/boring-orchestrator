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

### Pre-script workspaces

A pre-script can prepare an isolated workspace and ask the executor to run the agent there. Emit one reserved control line alongside the ordinary prompt output:

```text
::boring-orchestrator::{"cwd":"/absolute/path/to/worktree","cleanup_script":"git -C /path/to/repo worktree remove --force /absolute/path/to/worktree"}
```

The control line is removed before `{{pre_script_output}}` is expanded. `cwd` must be an existing absolute directory. The optional cleanup script runs once after the agent exits or fails to start, and also runs when a prepared workspace is followed by empty output or a pre-script failure.

A pre-script can launch up to 20 runs in parallel by emitting a single fan-out control line. Each item receives its own prompt expansion, working directory, run record, transcript, cost accounting, and cleanup lifecycle:

```text
::boring-orchestrator::{"runs":[{"prompt_output":"review PR 1","cwd":"/tmp/pr-1","cleanup_script":"cleanup-pr-1"},{"prompt_output":"review PR 2","cwd":"/tmp/pr-2","cleanup_script":"cleanup-pr-2"}]}
```

## Notes

This is a trusted local tool. Agent prompts and pre-scripts can execute commands in your environment, especially if you enable skipped permissions. Do not expose it to the public internet without adding your own access control.

Generated runtime files are ignored by git:

- `*.db`
- `*.db-shm`
- `*.db-wal`
- `*.log`

## License

MIT
