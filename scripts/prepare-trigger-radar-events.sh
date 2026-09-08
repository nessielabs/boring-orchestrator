#!/usr/bin/env bash
set -euo pipefail

orchestrator_dir=${BORING_ORCHESTRATOR_DIR:-/home/matrix/boring-orchestrator}
registry_path=${TRIGGER_RADAR_REGISTRY:-/home/matrix/trigger-radar/sources/source-registry.csv}
state_dir=${TRIGGER_RADAR_STATE_DIR:-/home/matrix/trigger-radar/state/signal-events}
api_key_file=${FIRECRAWL_API_KEY_FILE:-/home/matrix/.config/firecrawl/api-key}

# A registry is required. Never fall back to noisy homepage-only monitoring.
test -s "$registry_path"
test -s "$api_key_file"
exec python3 "$orchestrator_dir/scripts/trigger_signal_events.py" prepare \
  --registry "$registry_path" \
  --state-dir "$state_dir" \
  --api-key-file "$api_key_file" \
  --producers ats feeds \
  --workers 3 \
  --timeout-seconds 1200 "$@"
