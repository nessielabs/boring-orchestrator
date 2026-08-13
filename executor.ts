import { spawn, execSync } from "child_process";
import { createRun, appendTranscript, finishRun, hasRunningRun, type Agent } from "./db.js";
import { computeOpenAICost } from "./pricing.js";

function runPreScript(agent: Agent): { ok: boolean; output: string } {
  if (!agent.pre_script.trim()) return { ok: true, output: "" };

  try {
    const output = execSync(agent.pre_script, {
      cwd: agent.cwd || undefined,
      timeout: 60_000,
      encoding: "utf-8",
      shell: "/bin/bash",
      env: process.env,
    }).trim();

    if (!output) {
      console.log(`[executor] Agent "${agent.name}" pre-script returned empty output, skipping run`);
      return { ok: false, output: "" };
    }

    return { ok: true, output };
  } catch (err: any) {
    const reason = err.killed ? `timeout (${err.signal})` : `exit code ${err.status}`;
    console.log(`[executor] Agent "${agent.name}" pre-script failed: ${reason}, skipping run`);
    return { ok: false, output: err.stdout?.trim() || "" };
  }
}

// Group pre-script JSON lines by the value of `laneKey` in each line.
// Non-JSON lines and lines missing the key fall into the "" lane.
function partitionByLane(output: string, laneKey: string): Map<string, string[]> {
  const lanes = new Map<string, string[]>();
  for (const line of output.split("\n")) {
    if (!line.trim()) continue;
    let lane = "";
    try {
      const value = JSON.parse(line)?.[laneKey];
      if (value !== undefined && value !== null) lane = String(value);
    } catch {}
    if (!lanes.has(lane)) lanes.set(lane, []);
    lanes.get(lane)!.push(line);
  }
  return lanes;
}

// Returns the ids of runs started this tick (empty when everything was
// skipped). Laneless agents start at most one run; agents with a lane_key
// start one run per lane, serialized within a lane, parallel across lanes.
export function executeAgent(agent: Agent, triggerPayload?: string): string[] {
  const laneKey = agent.lane_key?.trim() || "";

  // Lanes only make sense when a pre-script produces partitionable output.
  if (!laneKey || !agent.pre_script.trim()) {
    if (hasRunningRun(agent.id)) {
      console.log(`[executor] Agent "${agent.name}" already has a running run, skipping`);
      return [];
    }
    const pre = runPreScript(agent);
    if (!pre.ok) return [];
    const prompt = pre.output
      ? agent.prompt.replace(/\{\{pre_script_output\}\}/g, pre.output)
      : agent.prompt;
    if (agent.script_only) {
      return [recordScriptOnlyRun(agent, "", triggerPayload, pre.output)];
    }
    return [startRun(agent, prompt, "", triggerPayload, pre.output)];
  }

  const pre = runPreScript(agent);
  if (!pre.ok) return [];

  const runIds: string[] = [];
  for (const [lane, lines] of partitionByLane(pre.output, laneKey)) {
    if (hasRunningRun(agent.id, lane)) {
      console.log(`[executor] Agent "${agent.name}" lane "${lane}" already has a running run, skipping lane`);
      continue;
    }
    const laneOutput = lines.join("\n");
    if (agent.script_only) {
      runIds.push(recordScriptOnlyRun(agent, lane, triggerPayload, laneOutput));
      continue;
    }
    const prompt = agent.prompt.replace(/\{\{pre_script_output\}\}/g, laneOutput);
    runIds.push(startRun(agent, prompt, lane, triggerPayload, laneOutput));
  }
  return runIds;
}

function recordScriptOnlyRun(agent: Agent, lane: string, triggerPayload: string | undefined, preOutput: string): string {
  const run = createRun(agent.id, triggerPayload, lane);
  appendTranscript(run.id, JSON.stringify({ type: "pre_script", text: preOutput }));
  finishRun(run.id, "success", {
    duration_ms: 0,
    total_cost_usd: 0,
    num_turns: 0,
    result_text: preOutput,
  });
  console.log(`[executor] Agent "${agent.name}" recorded script-only run ${run.id}${lane ? ` (lane: ${lane})` : ""}`);
  return run.id;
}

function startRun(agent: Agent, prompt: string, lane: string, triggerPayload: string | undefined, preOutput: string): string {
  const run = createRun(agent.id, triggerPayload, lane);

  if (preOutput) {
    appendTranscript(run.id, JSON.stringify({ type: "pre_script", text: preOutput }));
  }

  const provider = agent.provider || "claude";
  const args = provider === "codex" ? codexArgs(agent) : claudeArgs(agent, prompt);
  const command = provider === "codex" ? "codex" : "claude";

  const spawnOpts: { cwd?: string; env: NodeJS.ProcessEnv } = {
    env: {
      ...process.env,
      PATH: `${process.env.HOME}/.local/bin:${process.env.HOME}/.local/share/fnm:${process.env.PATH}`,
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    },
  };

  if (agent.cwd) {
    spawnOpts.cwd = agent.cwd;
  }

  const child = spawn(command, args, { ...spawnOpts, stdio: provider === "codex" ? ["pipe", "pipe", "pipe"] : ["ignore", "pipe", "pipe"] });
  if (provider === "codex") {
    child.stdin!.end(prompt);
  }

  let buffer = "";
  let resultText = "";
  let meta = { duration_ms: 0, total_cost_usd: 0, num_turns: 0 };
  const startedAt = Date.now();

  child.stdout!.on("data", (chunk: Buffer) => {
    buffer += chunk.toString();
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        const parsed = JSON.parse(line);
        appendTranscript(run.id, line);
        captureResult(parsed);
      } catch {
        appendTranscript(run.id, JSON.stringify({ type: "raw", text: line }));
      }
    }
  });

  child.stderr!.on("data", (chunk: Buffer) => {
    const text = chunk.toString().trim();
    if (text) {
      appendTranscript(run.id, JSON.stringify({ type: "stderr", text }));
    }
  });

  child.on("close", (code) => {
    if (buffer.trim()) {
      try {
        const parsed = JSON.parse(buffer);
        appendTranscript(run.id, buffer);
        captureResult(parsed);
      } catch {
        appendTranscript(run.id, JSON.stringify({ type: "raw", text: buffer }));
      }
    }

    const status = code === 0 ? "success" : "error";
    if (!meta.duration_ms) {
      meta.duration_ms = Date.now() - startedAt;
    }
    try {
      finishRun(run.id, status, { ...meta, result_text: resultText });
      console.log(`[executor] Agent "${agent.name}" run ${run.id} finished: ${status} (${meta.duration_ms}ms, $${meta.total_cost_usd.toFixed(4)}, ${meta.num_turns} turns)`);
    } catch (err: any) {
      console.error(`[executor] Agent "${agent.name}" run ${run.id} failed to finalize:`, err.message);
      finishRun(run.id, "error", { result_text: `Failed to finalize run: ${err.message}` });
    }
  });

  child.on("error", (err) => {
    appendTranscript(run.id, JSON.stringify({ type: "error", text: err.message }));
    finishRun(run.id, "error", { result_text: err.message });
    console.error(`[executor] Agent "${agent.name}" run ${run.id} spawn error:`, err.message);
  });

  console.log(`[executor] Agent "${agent.name}" started run ${run.id}${lane ? ` (lane: ${lane})` : ""}`);
  return run.id;

  function captureResult(parsed: any): void {
    if (provider === "codex") {
      captureCodexResult(parsed);
    } else {
      captureClaudeResult(parsed);
    }
  }

  function captureClaudeResult(parsed: any): void {
    if (parsed.type === "result") {
      meta.duration_ms = parsed.duration_ms || 0;
      meta.total_cost_usd = parsed.total_cost_usd || 0;
      meta.num_turns = parsed.num_turns || 0;
      if (typeof parsed.result === "string") resultText = parsed.result;
      return;
    }

    if (parsed.type === "assistant" && parsed.message && typeof parsed.message === "object") {
      const text = parsed.message.content
        ?.filter((content: any) => content.type === "text")
        .map((content: any) => content.text || "")
        .join("");
      if (text) resultText = text;
    }
  }

  function captureCodexResult(parsed: any): void {
    if (parsed.type === "item.completed" && parsed.item?.type === "agent_message") {
      if (typeof parsed.item.text === "string") resultText = parsed.item.text;
      return;
    }

    if (parsed.type === "turn.completed") {
      meta.num_turns += 1;
      if (parsed.usage) {
        const model = agent.model?.trim() || "o4-mini";
        const cost = computeOpenAICost(model, parsed.usage);
        if (cost !== null) {
          meta.total_cost_usd += cost;
        } else {
          console.warn(`[executor] Agent "${agent.name}": unknown model "${model}", cannot compute cost`);
        }
      }
    }
  }
}

function claudeArgs(agent: Agent, prompt: string): string[] {
  const args = [
    "-p", prompt,
    "--output-format", "stream-json",
    "--verbose",
    "--model", agent.model || "claude-sonnet-4-6",
    "--max-turns", "100",
  ];

  if (agent.skip_permissions) {
    args.push("--dangerously-skip-permissions");
  }

  return args;
}

function codexArgs(agent: Agent): string[] {
  const args = [
    "exec",
    "--json",
    "--skip-git-repo-check",
  ];

  if (agent.skip_permissions) {
    args.push("--dangerously-bypass-approvals-and-sandbox");
  }

  if (agent.model.trim()) {
    args.push("--model", agent.model.trim());
  }

  if (agent.reasoning_effort) {
    args.push("--config", `model_reasoning_effort=${JSON.stringify(agent.reasoning_effort)}`);
  }

  args.push("-");
  return args;
}
