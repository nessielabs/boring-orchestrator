import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const helperPath = fileURLToPath(new URL("./scripts/review-control.sh", import.meta.url));

test("packages review contexts larger than the Linux argument limit", () => {
  const fixtureDir = mkdtempSync(join(tmpdir(), "review-control-"));
  try {
    const selectedPath = writeJson(fixtureDir, "selected.json", {
      latest_mention: "2026-08-01T20:56:52Z",
      latest_agent_response: "2026-08-01T19:57:29Z",
    });
    const prContextPath = writeJson(fixtureDir, "pr-context.json", {
      number: 1147,
      title: "Large documentation review",
      body: "x".repeat(256 * 1024),
    });
    const issueCommentsPath = writeJson(fixtureDir, "issue-comments.json", []);
    const inlineCommentsPath = writeJson(fixtureDir, "inline-comments.json", []);
    const reviewsPath = writeJson(fixtureDir, "reviews.json", []);

    const script = `
      source "$1"
      selected=$(<"$2")
      pr_context=$(<"$3")
      issue_comments=$(<"$4")
      inline_comments=$(<"$5")
      reviews=$(<"$6")
      review_context=$(build_review_context \
        "nessielabs/nessie-codebase" \
        "$selected" \
        "/tmp/review-1147" \
        "base-sha" \
        "head-sha" \
        "$pr_context" \
        "$issue_comments" \
        "$inline_comments" \
        "$reviews") || exit 1
      run_spec=$(build_review_run_spec \
        "$review_context" \
        "/tmp/review-1147" \
        "cleanup review-1147") || exit 1
      printf '%s\n' "$run_spec" | build_review_fanout_control
    `;
    const result = spawnSync("bash", [
      "-c",
      script,
      "bash",
      helperPath,
      selectedPath,
      prContextPath,
      issueCommentsPath,
      inlineCommentsPath,
      reviewsPath,
    ], { encoding: "utf8", maxBuffer: 2 * 1024 * 1024 });

    assert.equal(result.status, 0, result.stderr);
    const control = JSON.parse(result.stdout);
    assert.equal(control.runs.length, 1);
    assert.equal(control.runs[0].cwd, "/tmp/review-1147");
    assert.equal(control.runs[0].cleanup_script, "cleanup review-1147");

    const prompt = JSON.parse(control.runs[0].prompt_output);
    assert.equal(prompt.number, 1147);
    assert.equal(prompt.pr_context.body.length, 256 * 1024);
  } finally {
    rmSync(fixtureDir, { recursive: true, force: true });
  }
});

function writeJson(directory: string, name: string, value: unknown): string {
  const path = join(directory, name);
  writeFileSync(path, JSON.stringify(value));
  return path;
}
