import assert from "node:assert/strict";
import test from "node:test";
import { PRE_SCRIPT_CONTROL_PREFIX, parsePreScriptOutput } from "./pre-script-control.js";

test("leaves ordinary pre-script output unchanged", () => {
  const result = parsePreScriptOutput('{"repo":"nessielabs/nessie-codebase","number":1144}\n');

  assert.equal(result.promptOutput, '{"repo":"nessielabs/nessie-codebase","number":1144}');
  assert.deepEqual(result.control, {});
});

test("extracts workspace controls without exposing them to the prompt", () => {
  const result = parsePreScriptOutput([
    '{"repo":"nessielabs/nessie-codebase","number":1144}',
    `${PRE_SCRIPT_CONTROL_PREFIX}{"cwd":"/tmp/review-1144","cleanup_script":"remove-review 1144"}`,
  ].join("\n"));

  assert.equal(result.promptOutput, '{"repo":"nessielabs/nessie-codebase","number":1144}');
  assert.deepEqual(result.control, {
    cwd: "/tmp/review-1144",
    cleanup_script: "remove-review 1144",
  });
});

test("extracts independent fan-out runs", () => {
  const result = parsePreScriptOutput(
    `${PRE_SCRIPT_CONTROL_PREFIX}${JSON.stringify({
      runs: [
        { prompt_output: '{"repo":"nessielabs/one"}', cwd: "/tmp/one", cleanup_script: "clean one" },
        { prompt_output: '{"repo":"nessielabs/two"}', cwd: "/tmp/two", cleanup_script: "clean two" },
      ],
    })}`,
  );

  assert.equal(result.promptOutput, "");
  assert.equal(result.control.runs?.length, 2);
  assert.equal(result.control.runs?.[1].cwd, "/tmp/two");
});

test("rejects ambiguous or malformed controls", () => {
  assert.throws(
    () => parsePreScriptOutput(`${PRE_SCRIPT_CONTROL_PREFIX}{not-json}`),
    /invalid Boring Orchestrator control JSON/,
  );
  assert.throws(
    () => parsePreScriptOutput([
      `${PRE_SCRIPT_CONTROL_PREFIX}{"cwd":"/tmp/one"}`,
      `${PRE_SCRIPT_CONTROL_PREFIX}{"cwd":"/tmp/two"}`,
    ].join("\n")),
    /more than one/,
  );
  assert.throws(
    () => parsePreScriptOutput(`${PRE_SCRIPT_CONTROL_PREFIX}{"runs":[]}`),
    /must contain 1-20 items/,
  );
  assert.throws(
    () => parsePreScriptOutput([
      "ordinary prompt text",
      `${PRE_SCRIPT_CONTROL_PREFIX}{"runs":[{"prompt_output":"review"}]}`,
    ].join("\n")),
    /cannot include prompt text/,
  );
});
