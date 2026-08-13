import assert from "node:assert/strict";
import test from "node:test";
import { createAgent, deleteAgent, getRun } from "./db.js";
import { executeAgent } from "./executor.js";

test("script-only agents record pre-script output without a provider run", (t) => {
  const agent = createAgent({
    name: `script-only-test-${crypto.randomUUID()}`,
    trigger_type: "manual",
    trigger_config: "",
    provider: "claude",
    prompt: "This prompt must not be sent to a provider.",
    cwd: "",
    model: "claude-haiku-4-5",
    reasoning_effort: "",
    pre_script: "printf 'preview ready'",
    script_only: 1,
    lane_key: "",
    skip_permissions: 0,
    enabled: 1,
  });
  t.after(() => deleteAgent(agent.id));

  const runIds = executeAgent(agent, JSON.stringify({ trigger: "test" }));

  assert.equal(runIds.length, 1);
  const run = getRun(runIds[0]);
  assert.equal(run?.status, "success");
  assert.equal(run?.result_text, "preview ready");
  assert.equal(run?.total_cost_usd, 0);
  assert.equal(run?.num_turns, 0);
});
