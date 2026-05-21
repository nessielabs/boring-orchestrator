import cron, { type ScheduledTask } from "node-cron";
import { listAgents, type Agent } from "./db.js";
import { executeAgent } from "./executor.js";

const scheduledJobs = new Map<string, ScheduledTask>();
const scheduledConfigs = new Map<string, string>();

export function syncScheduler(): void {
  const agents = listAgents();
  const activeAgentIds = new Set<string>();

  for (const agent of agents) {
    if (agent.trigger_type !== "cron" || !agent.enabled || !agent.trigger_config) continue;
    activeAgentIds.add(agent.id);

    // Skip if already scheduled with the same config
    if (scheduledJobs.has(agent.id) && scheduledConfigs.get(agent.id) === agent.trigger_config) {
      continue;
    }

    // Config changed or new agent: stop old job if exists.
    if (scheduledJobs.has(agent.id)) {
      scheduledJobs.get(agent.id)!.stop();
      console.log(`[scheduler] Rescheduling agent "${agent.name}" (config changed)`);
    }

    if (!cron.validate(agent.trigger_config)) {
      console.warn(`[scheduler] Invalid cron expression for agent "${agent.name}": ${agent.trigger_config}`);
      continue;
    }

    const agentName = agent.name;
    const agentId = agent.id;
    const job = cron.schedule(agent.trigger_config, () => {
      console.log(`[scheduler] Cron callback fired for "${agentName}" (${agentId})`);
      try {
        // Re-read agent from DB to get latest prompt/config
        const freshAgent = listAgents().find(a => a.id === agentId);
        if (!freshAgent) {
          console.log(`[scheduler] Agent "${agentName}" not found in DB, skipping`);
          return;
        }
        if (!freshAgent.enabled) {
          console.log(`[scheduler] Agent "${agentName}" is disabled, skipping`);
          return;
        }
        console.log(`[scheduler] Triggering agent "${freshAgent.name}" (cron: ${freshAgent.trigger_config})`);
        executeAgent(freshAgent, JSON.stringify({ trigger: "cron", schedule: freshAgent.trigger_config }));
      } catch (err: any) {
        console.error(`[scheduler] ERROR in cron callback for "${agentName}":`, err.message || err);
      }
    }, { noOverlap: true });

    scheduledJobs.set(agent.id, job);
    scheduledConfigs.set(agent.id, agent.trigger_config);
    console.log(`[scheduler] Scheduled agent "${agent.name}" with cron: ${agent.trigger_config}`);
  }

  // stop jobs for agents that were removed or disabled
  for (const [agentId, job] of scheduledJobs) {
    if (!activeAgentIds.has(agentId)) {
      job.stop();
      scheduledJobs.delete(agentId);
      scheduledConfigs.delete(agentId);
      console.log(`[scheduler] Unscheduled removed/disabled agent: ${agentId}`);
    }
  }
}

export function startScheduler(): void {
  syncScheduler();
  // re-sync every 30s to pick up changes
  setInterval(syncScheduler, 30_000);
  console.log("[scheduler] Started (re-syncs every 30s)");
}
