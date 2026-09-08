#!/usr/bin/env bash
set -euo pipefail

orchestrator_dir=${BORING_ORCHESTRATOR_DIR:-/home/matrix/boring-orchestrator}
registry_path=${TRIGGER_RADAR_REGISTRY:-/home/matrix/trigger-radar/sources/source-registry.csv}
state_dir=${TRIGGER_RADAR_STATE_DIR:-/home/matrix/trigger-radar/state/signal-events}
api_key_file=${FIRECRAWL_API_KEY_FILE:-/home/matrix/.config/firecrawl/api-key}

# A registry is required. Never fall back to noisy homepage-only monitoring.
test -s "$registry_path"
test -s "$api_key_file"
python3 - "$api_key_file" <<'CHECK_KEY'
import os, stat, sys
mode = stat.S_IMODE(os.stat(sys.argv[1]).st_mode)
if mode not in (0o400, 0o600):
    raise SystemExit("Firecrawl API key file must have mode 400 or 600")
CHECK_KEY
exec python3 "$orchestrator_dir/scripts/trigger_signal_events.py" prepare \
  --registry "$registry_path" \
  --state-dir "$state_dir" \
  --api-key-file "$api_key_file" \
  --producers ats feeds \
  --max-companies 10 \
  --max-events 40 \
  --max-input-bytes 32000 \
  --workers 3 \
  --timeout-seconds 1200 "$@"
