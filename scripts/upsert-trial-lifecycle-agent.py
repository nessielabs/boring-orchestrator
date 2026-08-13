#!/usr/bin/env python3
"""Create or update the preview-only Boring Orchestrator lifecycle agent."""

from __future__ import annotations

import json
import os
import urllib.request


BASE_URL = os.environ.get("BORING_ORCHESTRATOR_URL", "http://localhost:44066")
AGENT_NAME = "Trial lifecycle preview"

PAYLOAD = {
    "name": AGENT_NAME,
    "trigger_type": "cron",
    "trigger_config": "0 */6 * * *",
    "provider": "claude",
    "pre_script": "bash ~/boring-orchestrator/scripts/check-trial-lifecycle.sh",
    "prompt": """The deterministic trial lifecycle preview found recipients:\n\n{{pre_script_output}}\n\nSummarize the eligible counts by campaign and template variant for Anna or Tiger. This is preview-only. Never run send.py and never send email. Do not inspect user prompts, contexts, sources, or repositories. Flag an audience larger than 20 recipients for extra review. End with the exact preview commands the operator can run before explicitly approving any send.""",
    "cwd": "/home/matrix/nessie-campaigns",
    "model": "claude-haiku-4-5",
    "reasoning_effort": "",
    "lane_key": "",
    "skip_permissions": False,
    "enabled": True,
}


def request(path, *, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def main():
    agents = request("/api/agents")
    existing = next(
        (agent for agent in agents if agent.get("name") == AGENT_NAME), None
    )
    if existing:
        agent = request(
            f"/api/agents/{existing['id']}", method="PUT", payload=PAYLOAD
        )
        print(f"updated {AGENT_NAME}: {agent.get('id', existing['id'])}")
    else:
        agent = request("/api/agents", method="POST", payload=PAYLOAD)
        print(f"created {AGENT_NAME}: {agent.get('id')}")


if __name__ == "__main__":
    main()
