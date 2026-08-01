export const PRE_SCRIPT_CONTROL_PREFIX = "::boring-orchestrator::";
export const MAX_PRE_SCRIPT_RUNS = 20;

export interface PreScriptControl {
  cwd?: string;
  cleanup_script?: string;
  runs?: PreScriptRunControl[];
}

export interface PreScriptRunControl {
  prompt_output: string;
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
    const runs = parseRuns(candidate.runs);
    const cwd = optionalNonEmptyString(candidate.cwd, "cwd");
    const cleanupScript = optionalNonEmptyString(candidate.cleanup_script, "cleanup_script");
    control = {};
    if (cwd) control.cwd = cwd;
    if (cleanupScript) control.cleanup_script = cleanupScript;
    if (runs) control.runs = runs;

    if (runs && (control.cwd || control.cleanup_script)) {
      throw new Error("pre-script control payload cannot mix runs with cwd or cleanup_script");
    }
    if (!runs && !control.cwd && !control.cleanup_script) {
      throw new Error("pre-script control payload must include runs, cwd, or cleanup_script");
    }
    foundControl = true;
  }

  const promptOutput = promptLines.join("\n").trim();
  if (control.runs && promptOutput) {
    throw new Error("fan-out pre-script output cannot include prompt text outside runs");
  }
  return { promptOutput, control };
}

function parseRuns(value: unknown): PreScriptRunControl[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_PRE_SCRIPT_RUNS) {
    throw new Error(`pre-script control runs must contain 1-${MAX_PRE_SCRIPT_RUNS} items`);
  }

  return value.map((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error(`pre-script control runs[${index}] must be a JSON object`);
    }
    const candidate = item as Record<string, unknown>;
    return {
      prompt_output: requiredNonEmptyString(candidate.prompt_output, `runs[${index}].prompt_output`),
      cwd: optionalNonEmptyString(candidate.cwd, `runs[${index}].cwd`),
      cleanup_script: optionalNonEmptyString(candidate.cleanup_script, `runs[${index}].cleanup_script`),
    };
  });
}

function optionalNonEmptyString(value: unknown, field: string): string | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`pre-script control ${field} must be a non-empty string`);
  }
  return value.trim();
}

function requiredNonEmptyString(value: unknown, field: string): string {
  const parsed = optionalNonEmptyString(value, field);
  if (!parsed) throw new Error(`pre-script control ${field} is required`);
  return parsed;
}
