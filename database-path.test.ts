import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveDatabasePath } from "./database-path.js";

test("default database lives outside the source checkout and honors XDG state", () => {
  const root = mkdtempSync(join(tmpdir(), "orchestrator-state-"));
  try {
    assert.equal(resolveDatabasePath(root, {}, root), join(root, ".local/state/boring-orchestrator/boring-orchestrator.db"));
    assert.equal(resolveDatabasePath(root, { XDG_STATE_HOME: join(root, "state") }, root), join(root, "state/boring-orchestrator/boring-orchestrator.db"));
    assert.equal(resolveDatabasePath(root, { BORING_ORCHESTRATOR_DATABASE_PATH: ":memory:" }), ":memory:");
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("legacy database cannot silently become an empty installation", () => {
  const root = mkdtempSync(join(tmpdir(), "orchestrator-legacy-"));
  try {
    writeFileSync(join(root, "boring-orchestrator.db"), "existing data");
    assert.throws(() => resolveDatabasePath(root, {}, root), /Legacy database/);
    const explicit = join(root, "private/data.db");
    assert.equal(resolveDatabasePath(root, { BORING_ORCHESTRATOR_DATABASE_PATH: explicit }), explicit);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
