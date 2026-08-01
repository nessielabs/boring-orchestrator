export const PRE_SCRIPT_CONTROL_PREFIX = "::boring-orchestrator::";

export interface PreScriptControl {
  cwd?: string;
  cleanup_script?: string;
}

export interface ParsedPreScriptOutput {
  promptOutput: string;
  control: PreScriptControl;
}

export function parsePreScriptOutput(rawOutput: string): ParsedPreScriptOutput {
  const promptLines: string[] = [];
  let control: PreScriptControl = {};
  let foundControl = false;

  for (const line of rawOutput.split(/\r?\n/)) {
    if (!line.startsWith(PRE_SCRIPT_CONTROL_PREFIX)) {
      promptLines.push(line);
      continue;
    }

    if (foundControl) {
      throw new Error("pre-script emitted more than one Boring Orchestrator control line");
    }

    const payload = line.slice(PRE_SCRIPT_CONTROL_PREFIX.length);
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload);
    } catch {
      throw new Error("pre-script emitted invalid Boring Orchestrator control JSON");
    }

    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("pre-script control payload must be a JSON object");
    }

    const candidate = parsed as Record<string, unknown>;
    control = {
      cwd: optionalNonEmptyString(candidate.cwd, "cwd"),
      cleanup_script: optionalNonEmptyString(candidate.cleanup_script, "cleanup_script"),
    };

    if (!control.cwd && !control.cleanup_script) {
      throw new Error("pre-script control payload must include cwd or cleanup_script");
    }
    foundControl = true;
  }

  return { promptOutput: promptLines.join("\n").trim(), control };
}

function optionalNonEmptyString(value: unknown, field: string): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`pre-script control ${field} must be a non-empty string`);
  }
  return value.trim();
}
