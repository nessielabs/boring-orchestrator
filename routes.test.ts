import assert from "node:assert/strict";
import test from "node:test";
import express from "express";
import { deleteAgent } from "./db.js";
import router from "./routes.js";

// API mutations re-sync cron jobs. Remove the seeded cron fixture so route
// tests cannot schedule or launch a provider process at a clock boundary.
deleteAgent("dummy-agent");

async function withServer(run: (baseUrl: string) => Promise<void>): Promise<void> {
  const app = express();
  app.use(express.json());
  app.use(router);
  const server = app.listen(0, "127.0.0.1");
  await new Promise<void>((resolve) => server.once("listening", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Test server did not bind a TCP port");
  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
      server.closeAllConnections();
    });
  }
}

test("script-only agents allow an empty prompt and receive the default timeout", async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/agents`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: `script-only-route-test-${crypto.randomUUID()}`,
        trigger_type: "manual",
        pre_script: "printf ready",
        script_only: true,
        prompt: "",
      }),
    });
    assert.equal(response.status, 201);
    const agent = await response.json() as { id: string; prompt: string; pre_script_timeout_ms: number };
    assert.equal(agent.prompt, "");
    assert.equal(agent.pre_script_timeout_ms, 60_000);
    deleteAgent(agent.id);
  });
});

test("provider agents still require a prompt", async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/agents`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: `provider-route-test-${crypto.randomUUID()}`,
        trigger_type: "manual",
        script_only: false,
        prompt: "",
      }),
    });
    assert.equal(response.status, 400);
  });
});

test("pre-script timeouts must be positive and at most one hour", async () => {
  await withServer(async (baseUrl) => {
    for (const preScriptTimeout of [0, 3_600_001, 1.5]) {
      const response = await fetch(`${baseUrl}/api/agents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: `timeout-route-test-${crypto.randomUUID()}`,
          trigger_type: "manual",
          pre_script: "printf ready",
          pre_script_timeout_ms: preScriptTimeout,
          script_only: true,
          prompt: "",
        }),
      });
      assert.equal(response.status, 400);
    }
  });
});
