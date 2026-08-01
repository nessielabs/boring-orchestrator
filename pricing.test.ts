import assert from "node:assert/strict";
import test from "node:test";
import { computeOpenAICost } from "./pricing.js";

test("prices standard GPT-5.6 Sol usage", () => {
  const cost = computeOpenAICost("gpt-5.6-sol", {
    input_tokens: 100_000,
    cached_input_tokens: 20_000,
    output_tokens: 10_000,
  });

  assert.equal(cost, 0.71);
});

test("prices GPT-5.6 Sol cache writes at 1.25x input", () => {
  const cost = computeOpenAICost("gpt-5.6-sol", {
    input_tokens: 100_000,
    cached_input_tokens: 20_000,
    cache_write_input_tokens: 30_000,
  });

  assert.equal(cost, 0.4475);
});

test("applies GPT-5.6 Sol long-context multipliers", () => {
  const cost = computeOpenAICost("gpt-5.6-sol", {
    input_tokens: 300_000,
    cached_input_tokens: 100_000,
    output_tokens: 10_000,
  });

  assert.equal(cost, 2.55);
});

test("returns null for an unknown model", () => {
  assert.equal(computeOpenAICost("unknown", { input_tokens: 1_000 }), null);
});
