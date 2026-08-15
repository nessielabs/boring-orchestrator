import { Router } from "express";
import { listAgents, getAgent, createAgent, updateAgent, deleteAgent, listRuns, getRun, type ReasoningEffort } from "./db.js";
import { executeAgent } from "./executor.js";
import { syncScheduler } from "./scheduler.js";

const router = Router();
const REASONING_EFFORTS = new Set<ReasoningEffort>(["", "none", "low", "medium", "high", "xhigh", "max"]);

function normalizeReasoningEffort(value: unknown): ReasoningEffort | null {
  const normalized = typeof value === "string" ? value.trim().toLowerCase() : "";
  return REASONING_EFFORTS.has(normalized as ReasoningEffort) ? normalized as ReasoningEffort : null;
}

// --- Agents ---

router.get("/api/agents", (_req, res) => {
  res.json(listAgents());
});

router.get("/api/agents/:id", (req, res) => {
  const agent = getAgent(req.params.id);
  if (!agent) return res.status(404).json({ error: "Agent not found" });
  res.json(agent);
});

router.post("/api/agents", (req, res) => {
  const { name, trigger_type, trigger_config, provider, prompt, cwd, model, reasoning_effort, pre_script, script_only, lane_key, skip_permissions, enabled } = req.body;
  if (!name || !trigger_type || !prompt) {
    return res.status(400).json({ error: "name, trigger_type, and prompt are required" });
  }
  if (script_only && !pre_script?.trim()) {
    return res.status(400).json({ error: "script_only requires pre_script" });
  }
  const normalizedProvider = provider === "codex" ? "codex" : "claude";
  const normalizedReasoningEffort = normalizeReasoningEffort(reasoning_effort);
  if (normalizedReasoningEffort === null) {
    return res.status(400).json({ error: "Invalid reasoning_effort" });
  }
  const agent = createAgent({
    name,
    trigger_type,
    trigger_config: trigger_config || "",
    provider: normalizedProvider,
    prompt,
    cwd: cwd || "",
    model: model || (normalizedProvider === "claude" ? "claude-sonnet-4-6" : ""),
    reasoning_effort: normalizedReasoningEffort,
    pre_script: pre_script || "",
    script_only: script_only ? 1 : 0,
    lane_key: lane_key || "",
    skip_permissions: skip_permissions ? 1 : 0,
    enabled: enabled !== false ? 1 : 0,
  });
  syncScheduler();
  res.status(201).json(agent);
});

router.put("/api/agents/:id", (req, res) => {
  const existingAgent = getAgent(req.params.id);
  if (!existingAgent) return res.status(404).json({ error: "Agent not found" });

  const updates = { ...req.body };
  if ("provider" in updates) updates.provider = updates.provider === "codex" ? "codex" : "claude";
  if ("reasoning_effort" in updates) {
    const normalizedReasoningEffort = normalizeReasoningEffort(updates.reasoning_effort);
    if (normalizedReasoningEffort === null) {
      return res.status(400).json({ error: "Invalid reasoning_effort" });
    }
    updates.reasoning_effort = normalizedReasoningEffort;
  }
  if ("skip_permissions" in updates) updates.skip_permissions = updates.skip_permissions ? 1 : 0;
  if ("script_only" in updates) updates.script_only = updates.script_only ? 1 : 0;
  const effectiveScriptOnly = "script_only" in updates ? updates.script_only : existingAgent.script_only;
  const effectivePreScript = "pre_script" in updates ? updates.pre_script : existingAgent.pre_script;
  if (effectiveScriptOnly && !(typeof effectivePreScript === "string" && effectivePreScript.trim())) {
    return res.status(400).json({ error: "script_only requires pre_script" });
  }
  if ("enabled" in updates) updates.enabled = updates.enabled ? 1 : 0;
  const agent = updateAgent(req.params.id, updates);
  syncScheduler();
  res.json(agent);
});

router.delete("/api/agents/:id", (req, res) => {
  const deleted = deleteAgent(req.params.id);
  if (!deleted) return res.status(404).json({ error: "Agent not found" });
  syncScheduler();
  res.status(204).end();
});

// --- Manual trigger ---

router.post("/api/agents/:id/trigger", (req, res) => {
  const agent = getAgent(req.params.id);
  if (!agent) return res.status(404).json({ error: "Agent not found" });
  const runIds = executeAgent(agent, JSON.stringify({ trigger: "manual" }));
  if (runIds.length === 0) return res.json({ skipped: true, reason: "pre-script returned empty, exited non-zero, or all lanes are busy" });
  res.json({ run_id: runIds[0], run_ids: runIds });
});

// --- Runs ---

router.get("/api/agents/:id/runs", (req, res) => {
  const limit = parseInt(req.query.limit as string) || 50;
  res.json(listRuns(req.params.id, limit));
});

router.get("/api/runs/:id", (req, res) => {
  const run = getRun(req.params.id);
  if (!run) return res.status(404).json({ error: "Run not found" });
  res.json(run);
});

export default router;
