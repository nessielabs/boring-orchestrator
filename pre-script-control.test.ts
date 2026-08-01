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
});
