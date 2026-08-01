import type { Agent } from "./db.js";

export interface ProviderInvocation {
  command: "claude" | "codex";
  args: string[];
  stdin: string;
}

export function buildProviderInvocation(agent: Agent, prompt: string): ProviderInvocation {
  if (agent.provider === "codex") {
    return {
      command: "codex",
      args: codexArgs(agent),
      stdin: prompt,
    };
  }

  return {
    command: "claude",
    args: claudeArgs(agent),
    stdin: prompt,
  };
}

function claudeArgs(agent: Agent): string[] {
  const args = [
    "-p",
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
