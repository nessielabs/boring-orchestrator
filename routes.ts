import { Router } from "express";
import { listAgents, getAgent, createAgent, updateAgent, deleteAgent, listRuns, getRun } from "./db.js";
import { executeAgent } from "./executor.js";
import { syncScheduler } from "./scheduler.js";

const router = Router();

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
  const { name, trigger_type, trigger_config, provider, prompt, cwd, model, pre_script, skip_permissions, enabled } = req.body;
  if (!name || !trigger_type || !prompt) {
    return res.status(400).json({ error: "name, trigger_type, and prompt are required" });
  }
  const normalizedProvider = provider === "codex" ? "codex" : "claude";
  const agent = createAgent({
    name,
    trigger_type,
    trigger_config: trigger_config || "",
    provider: normalizedProvider,
    prompt,
    cwd: cwd || "",
    model: model || (normalizedProvider === "claude" ? "claude-sonnet-4-6" : ""),
    pre_script: pre_script || "",
    skip_permissions: skip_permissions ? 1 : 0,
    enabled: enabled !== false ? 1 : 0,
  });
  syncScheduler();
  res.status(201).json(agent);
});

router.put("/api/agents/:id", (req, res) => {
  const updates = { ...req.body };
  if ("provider" in updates) updates.provider = updates.provider === "codex" ? "codex" : "claude";
  if ("skip_permissions" in updates) updates.skip_permissions = updates.skip_permissions ? 1 : 0;
  if ("enabled" in updates) updates.enabled = updates.enabled ? 1 : 0;
  const agent = updateAgent(req.params.id, updates);
  if (!agent) return res.status(404).json({ error: "Agent not found" });
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
  const runId = executeAgent(agent, JSON.stringify({ trigger: "manual" }));
  if (!runId) return res.json({ skipped: true, reason: "pre-script returned empty or exited non-zero" });
  res.json({ run_id: runId });
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
