import assert from "node:assert/strict";
import test from "node:test";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { createAgent, deleteAgent, getRun, type Agent } from "./db.js";
import { buildProviderInvocation, executeAgent } from "./executor.js";

test("Claude prompts are streamed over stdin instead of argv", () => {
  const prompt = "x".repeat(1024 * 1024);
  const agent = {
    name: "large-prompt-test",
    provider: "claude",
    model: "claude-haiku-4-5",
    skip_permissions: 0,
  } as Agent;

  const invocation = buildProviderInvocation(agent, prompt);

  assert.equal(invocation.command, "claude");
  assert.equal(invocation.stdin, prompt);
  assert.equal(invocation.args.includes(prompt), false);
  assert.equal(invocation.args[0], "-p");
});

test("script-only agents record pre-script output without a provider run", async (t) => {
  const agent = createAgent({
    name: `script-only-test-${crypto.randomUUID()}`,
    trigger_type: "manual",
    trigger_config: "",
    provider: "claude",
    prompt: "",
    cwd: "",
    model: "claude-haiku-4-5",
    reasoning_effort: "",
    pre_script: "printf 'preview ready'",
    pre_script_timeout_ms: 60_000,
    script_only: 1,
    lane_key: "",
    skip_permissions: 0,
    enabled: 1,
  });
  t.after(() => deleteAgent(agent.id));

  const runIds = await executeAgent(agent, JSON.stringify({ trigger: "test" }));

  assert.equal(runIds.length, 1);
  const run = getRun(runIds[0]);
  assert.equal(run?.status, "success");
  assert.equal(run?.result_text, "preview ready");
  assert.equal(run?.total_cost_usd, 0);
  assert.equal(run?.num_turns, 0);
});

test("script-only agents without a pre-script fail closed", async (t) => {
  const agent = createAgent({
    name: `invalid-script-only-test-${crypto.randomUUID()}`,
    trigger_type: "manual",
    trigger_config: "",
    provider: "claude",
    prompt: "",
    cwd: "",
    model: "claude-haiku-4-5",
    reasoning_effort: "",
    pre_script: "",
    pre_script_timeout_ms: 60_000,
    script_only: 1,
    lane_key: "",
    skip_permissions: 0,
    enabled: 1,
  });
  t.after(() => deleteAgent(agent.id));

  assert.deepEqual(await executeAgent(agent, JSON.stringify({ trigger: "test" })), []);
});

test("pre-script execution honors the per-agent timeout", async (t) => {
  const agent = createAgent({
    name: `pre-script-timeout-test-${crypto.randomUUID()}`,
    trigger_type: "manual",
    trigger_config: "",
    provider: "claude",
    prompt: "",
    cwd: "",
    model: "claude-haiku-4-5",
    reasoning_effort: "",
    pre_script: "while :; do :; done",
    pre_script_timeout_ms: 20,
    script_only: 1,
    lane_key: "",
    skip_permissions: 0,
    enabled: 1,
  });
  t.after(() => deleteAgent(agent.id));

  assert.deepEqual(await executeAgent(agent, JSON.stringify({ trigger: "test" })), []);
});

test("the test suite uses an isolated in-memory database", () => {
  assert.equal(process.env.BORING_ORCHESTRATOR_DATABASE_PATH, ":memory:");
});

test("an early provider exit cannot raise an unhandled stdin error", async (t) => {
  const child = new EventEmitter() as any;
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  const stub = t.mock.method(childProcess, "spawn", () => child);
  syncBuiltinESMExports();
  t.after(() => { stub.mock.restore(); syncBuiltinESMExports(); });
  const agent = createAgent({
    name: `early-exit-${crypto.randomUUID()}`, trigger_type: "manual", trigger_config: "",
    provider: "claude", prompt: "test prompt", cwd: "", model: "claude-opus-5",
    reasoning_effort: "", pre_script: "", pre_script_timeout_ms: 1000,
    script_only: 0, lane_key: "", skip_permissions: 0, enabled: 0,
  });
  t.after(() => deleteAgent(agent.id));
  const [id] = await executeAgent(agent);
  assert.doesNotThrow(() => child.stdin.emit("error", new Error("write EPIPE")));
  child.emit("close", 1);
  assert.equal(getRun(id)?.status, "error");
  assert.match(getRun(id)?.transcript || "", /stdin: write EPIPE/);
});
