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

  args.push("-");
  return args;
}
