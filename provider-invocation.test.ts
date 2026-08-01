import assert from "node:assert/strict";
import test from "node:test";
import type { Agent } from "./db.js";
import { buildProviderInvocation } from "./provider-invocation.js";

test("sends large Claude prompts through stdin instead of argv", () => {
  const prompt = "x".repeat(256 * 1024);
  const invocation = buildProviderInvocation(makeAgent({ provider: "claude" }), prompt);

  assert.equal(invocation.command, "claude");
  assert.equal(invocation.stdin, prompt);
  assert.equal(invocation.args.includes(prompt), false);
  assert.deepEqual(invocation.args.slice(0, 2), ["-p", "--output-format"]);
});

test("preserves Codex stdin and reasoning configuration", () => {
  const invocation = buildProviderInvocation(makeAgent({
    provider: "codex",
    model: "gpt-5.6-sol",
    reasoning_effort: "xhigh",
  }), "review this PR");

  assert.equal(invocation.command, "codex");
  assert.equal(invocation.stdin, "review this PR");
  assert.deepEqual(invocation.args, [
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
    "--model",
    "gpt-5.6-sol",
    "--config",
    'model_reasoning_effort="xhigh"',
    "-",
  ]);
});

function makeAgent(overrides: Partial<Agent>): Agent {
  return {
    id: "reviewer",
    name: "PR Reviewer",
    trigger_type: "manual",
    trigger_config: "",
    provider: "claude",
    prompt: "Review {{pre_script_output}}",
    cwd: "",
    model: "claude-opus-5",
    reasoning_effort: "",
    pre_script: "",
    skip_permissions: 1,
    enabled: 1,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:00Z",
    ...overrides,
  };
}
