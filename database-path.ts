import { existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export function resolveDatabasePath(sourceDir: string, env: NodeJS.ProcessEnv = process.env, home = homedir()): string {
  const explicit = env.BORING_ORCHESTRATOR_DATABASE_PATH;
  if (!explicit && existsSync(join(sourceDir, "boring-orchestrator.db"))) {
    throw new Error("Legacy database found in the source checkout. Move it during an offline maintenance window and set BORING_ORCHESTRATOR_DATABASE_PATH; refusing to create an empty replacement.");
  }
  const path = explicit || join(env.XDG_STATE_HOME || join(home, ".local", "state"), "boring-orchestrator", "boring-orchestrator.db");
  if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  return path;
}
