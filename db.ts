import Database from "better-sqlite3";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const db = new Database(join(__dirname, "boring-orchestrator.db"));

db.pragma("journal_mode = WAL");
db.pragma("foreign_keys = ON");

db.exec(`
  CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('cron', 'webhook', 'manual')),
    trigger_config TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'claude' CHECK(provider IN ('claude', 'codex')),
    prompt TEXT NOT NULL,
    cwd TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    reasoning_effort TEXT NOT NULL DEFAULT '' CHECK(reasoning_effort IN ('', 'none', 'low', 'medium', 'high', 'xhigh', 'max')),
    pre_script TEXT NOT NULL DEFAULT '',
    skip_permissions INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running', 'success', 'error')),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    duration_ms INTEGER,
    total_cost_usd REAL,
    num_turns INTEGER,
    trigger_payload TEXT,
    transcript TEXT NOT NULL DEFAULT '[]',
    result_text TEXT
  );

  CREATE INDEX IF NOT EXISTS idx_runs_agent_id ON runs(agent_id);
  CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
`);

// migration: add pre_script column if missing
try { db.exec("ALTER TABLE agents ADD COLUMN pre_script TEXT NOT NULL DEFAULT ''"); } catch {}
try { db.exec("ALTER TABLE agents ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude'"); } catch {}
try { db.exec("ALTER TABLE agents ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT '' CHECK(reasoning_effort IN ('', 'none', 'low', 'medium', 'high', 'xhigh', 'max'))"); } catch {}

export type ReasoningEffort = "" | "none" | "low" | "medium" | "high" | "xhigh" | "max";

export interface Agent {
  id: string;
  name: string;
  trigger_type: "cron" | "webhook" | "manual";
  trigger_config: string;
  provider: "claude" | "codex";
  prompt: string;
  cwd: string;
  model: string;
  reasoning_effort: ReasoningEffort;
  pre_script: string;
  skip_permissions: number;
  enabled: number;
  created_at: string;
  updated_at: string;
}

export interface Run {
  id: string;
  agent_id: string;
  status: "running" | "success" | "error";
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  total_cost_usd: number | null;
  num_turns: number | null;
  trigger_payload: string | null;
  transcript: string;
  result_text: string | null;
}

function genId(): string {
  return crypto.randomUUID();
}

function seedInitialAgents(): void {
  const now = new Date().toISOString();
  db.prepare(`
    INSERT OR IGNORE INTO agents (id, name, trigger_type, trigger_config, provider, prompt, cwd, model, reasoning_effort, pre_script, skip_permissions, enabled, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    "dummy-agent",
    "dummy agent",
    "cron",
    "0 */30 * * * *",
    "claude",
    "Say exactly: hello world, it's {{pre_script_output}}.",
    "",
    "claude-haiku-4-5",
    "",
    "date '+%Y-%m-%d %H:%M:%S %Z'",
    0,
    1,
    now,
    now
  );
}

seedInitialAgents();

// --- Agents ---

export function listAgents(): Agent[] {
  return db.prepare("SELECT * FROM agents ORDER BY created_at DESC").all() as Agent[];
}

export function getAgent(id: string): Agent | undefined {
  return db.prepare("SELECT * FROM agents WHERE id = ?").get(id) as Agent | undefined;
}

export function createAgent(agent: Omit<Agent, "id" | "created_at" | "updated_at">): Agent {
  const id = genId();
  const now = new Date().toISOString();
  db.prepare(`
    INSERT INTO agents (id, name, trigger_type, trigger_config, provider, prompt, cwd, model, reasoning_effort, pre_script, skip_permissions, enabled, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).run(id, agent.name, agent.trigger_type, agent.trigger_config, agent.provider, agent.prompt, agent.cwd, agent.model, agent.reasoning_effort, agent.pre_script, agent.skip_permissions, agent.enabled, now, now);
  return getAgent(id)!;
}

export function updateAgent(id: string, updates: Partial<Omit<Agent, "id" | "created_at">>): Agent | undefined {
  const agent = getAgent(id);
  if (!agent) return undefined;

  const fields: string[] = [];
  const values: any[] = [];

  for (const [key, value] of Object.entries(updates)) {
    if (key === "id" || key === "created_at") continue;
    fields.push(`${key} = ?`);
    values.push(value);
  }

  fields.push("updated_at = ?");
  values.push(new Date().toISOString());
  values.push(id);

  db.prepare(`UPDATE agents SET ${fields.join(", ")} WHERE id = ?`).run(...values);
  return getAgent(id);
}

export function deleteAgent(id: string): boolean {
  const result = db.prepare("DELETE FROM agents WHERE id = ?").run(id);
  return result.changes > 0;
}

// --- Runs ---

export function listRuns(agentId: string, limit = 50): Omit<Run, "transcript">[] {
  return db.prepare("SELECT id, agent_id, status, started_at, finished_at, duration_ms, total_cost_usd, num_turns, trigger_payload, result_text FROM runs WHERE agent_id = ? ORDER BY started_at DESC LIMIT ?").all(agentId, limit) as Omit<Run, "transcript">[];
}

export function getRun(id: string): Run | undefined {
  return db.prepare("SELECT * FROM runs WHERE id = ?").get(id) as Run | undefined;
}

export function hasRunningRun(agentId: string): boolean {
  const row = db.prepare("SELECT 1 FROM runs WHERE agent_id = ? AND status = 'running' LIMIT 1").get(agentId);
  return !!row;
}

export function createRun(agentId: string, triggerPayload?: string): Run {
  const id = genId();
  db.prepare(`
    INSERT INTO runs (id, agent_id, trigger_payload)
    VALUES (?, ?, ?)
  `).run(id, agentId, triggerPayload ?? null);
  return getRun(id)!;
}

export function appendTranscript(runId: string, line: string): void {
  db.prepare(`
    UPDATE runs SET transcript = json_insert(transcript, '$[#]', json(?)) WHERE id = ?
  `).run(line, runId);
}

export function finishRun(runId: string, status: "success" | "error", meta: { duration_ms?: number; total_cost_usd?: number; num_turns?: number; result_text?: string }): void {
  db.prepare(`
    UPDATE runs SET status = ?, finished_at = datetime('now'), duration_ms = ?, total_cost_usd = ?, num_turns = ?, result_text = ? WHERE id = ?
  `).run(status, meta.duration_ms ?? null, meta.total_cost_usd ?? null, meta.num_turns ?? null, meta.result_text ?? null, runId);
}

export default db;
