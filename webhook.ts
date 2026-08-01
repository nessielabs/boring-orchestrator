import { createHmac, timingSafeEqual } from "crypto";
import type { Request, Response } from "express";
import { listAgents } from "./db.js";
import { executeAgent } from "./executor.js";

export function handleWebhook(req: Request, res: Response): void {
  const agents = listAgents().filter(
    (a) => a.trigger_type === "webhook" && a.enabled
  );

  if (agents.length === 0) {
    res.status(200).json({ message: "No webhook agents configured" });
    return;
  }

  const payload = JSON.stringify(req.body);
  const triggered: string[] = [];

  for (const agent of agents) {
    // trigger_config for webhooks is the path suffix, e.g. "pr-review"
    // the webhook URL would be /webhook/pr-review
    const expectedPath = agent.trigger_config;
    const actualPath = req.params.path || "";

    if (expectedPath && expectedPath !== actualPath) continue;

    // inject webhook payload into the prompt via template vars
    const prompt = renderPrompt(agent.prompt, req.body);
    const agentWithPrompt = { ...agent, prompt };

    const runIds = executeAgent(agentWithPrompt, payload);
    if (!runIds) continue;

    triggered.push(...runIds);
    console.log(`[webhook] Triggered agent "${agent.name}" -> ${runIds.length} run(s)`);
  }

  res.status(200).json({ triggered, count: triggered.length });
}

export function verifyGitHubSignature(payload: string, signature: string | undefined, secret: string): boolean {
  if (!signature) return false;
  const expected = "sha256=" + createHmac("sha256", secret).update(payload).digest("hex");
  try {
    return timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  } catch {
    return false;
  }
}

function renderPrompt(template: string, payload: any): string {
  return template.replace(/\{\{(\w[\w.]*)\}\}/g, (_match, path: string) => {
    const value = path.split(".").reduce((obj: any, key: string) => obj?.[key], payload);
    return value !== undefined ? String(value) : `{{${path}}}`;
  });
}
