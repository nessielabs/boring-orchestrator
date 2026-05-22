import { spawn, execSync } from "child_process";
import { createRun, appendTranscript, finishRun, hasRunningRun, type Agent } from "./db.js";

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

export function executeAgent(agent: Agent, triggerPayload?: string): string | null {
  if (hasRunningRun(agent.id)) {
    console.log(`[executor] Agent "${agent.name}" already has a running run, skipping`);
    return null;
  }

  // Run pre-script first
  const pre = runPreScript(agent);
  if (!pre.ok) return null;

  // Inject pre-script output into prompt via {{pre_script_output}}
  const prompt = pre.output
    ? agent.prompt.replace(/\{\{pre_script_output\}\}/g, pre.output)
    : agent.prompt;

  const run = createRun(agent.id, triggerPayload);

  if (pre.output) {
    appendTranscript(run.id, JSON.stringify({ type: "pre_script", text: pre.output }));
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

  console.log(`[executor] Agent "${agent.name}" started run ${run.id}`);
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

// OpenAI pricing per million tokens (https://developers.openai.com/api/docs/pricing)
// Models with long_context_threshold apply 2x input and 1.5x output for the full
// session when input_tokens exceeds the threshold.
const OPENAI_PRICING: Record<string, {
  input: number; cached_input: number; output: number;
  long_context_threshold?: number;
}> = {
  // GPT-5.x (5.5 and 5.4 have >272K long-context multiplier)
  "gpt-5.5":      { input: 5.00,  cached_input: 0.50,  output: 30.00, long_context_threshold: 272_000 },
  "gpt-5.4":      { input: 2.50,  cached_input: 0.25,  output: 15.00, long_context_threshold: 272_000 },
  "gpt-5.4-mini": { input: 0.75,  cached_input: 0.075, output: 4.50 },
  "gpt-5.4-nano": { input: 0.20,  cached_input: 0.02,  output: 1.25 },
  "gpt-5.3-codex": { input: 1.75, cached_input: 0.175, output: 14.00 },
  // GPT-4.1
  "gpt-4.1":      { input: 2.00,  cached_input: 0.50,  output: 8.00 },
  "gpt-4.1-mini": { input: 0.40,  cached_input: 0.10,  output: 1.60 },
  "gpt-4.1-nano": { input: 0.10,  cached_input: 0.025, output: 0.40 },
  // GPT-4o
  "gpt-4o":       { input: 2.50,  cached_input: 1.25,  output: 10.00 },
  "gpt-4o-mini":  { input: 0.15,  cached_input: 0.075, output: 0.60 },
  // o-series reasoning
  "o4-mini":      { input: 1.10,  cached_input: 0.275, output: 4.40 },
  "o3":           { input: 2.00,  cached_input: 0.50,  output: 8.00 },
  "o3-mini":      { input: 1.10,  cached_input: 0.55,  output: 4.40 },
};

function computeOpenAICost(
  model: string,
  usage: { input_tokens?: number; cached_input_tokens?: number; output_tokens?: number; reasoning_output_tokens?: number },
): number | null {
  const pricing = OPENAI_PRICING[model];
  if (!pricing) return null;
  const input = Math.max(usage.input_tokens ?? 0, 0);
  const cached = Math.max(Math.min(usage.cached_input_tokens ?? 0, input), 0);
  const nonCachedInput = input - cached;
  const output = Math.max(usage.output_tokens ?? 0, 0);

  // GPT-5.5/5.4: 2x input and 1.5x output when input exceeds threshold
  const longCtx = pricing.long_context_threshold != null && input > pricing.long_context_threshold;
  const inputRate = longCtx ? pricing.input * 2 : pricing.input;
  const cachedRate = longCtx ? pricing.cached_input * 2 : pricing.cached_input;
  const outputRate = longCtx ? pricing.output * 1.5 : pricing.output;

  return (
    (nonCachedInput * inputRate +
      cached * cachedRate +
      output * outputRate) /
    1_000_000
  );
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

  args.push("-");
  return args;
}
