#!/usr/bin/env bash
set -euo pipefail

orchestrator_dir=${BORING_ORCHESTRATOR_DIR:-/home/matrix/boring-orchestrator}
roster_path=${TRIGGER_RADAR_ROSTER:-/home/matrix/trigger-radar/sources/ashtan-combined-deduplicated.csv}
registry_path=${TRIGGER_RADAR_REGISTRY:-/home/matrix/trigger-radar/sources/source-registry.csv}
state_dir=${TRIGGER_RADAR_STATE_DIR:-/home/matrix/trigger-radar/state/website-change-events}
api_key_file=${FIRECRAWL_API_KEY_FILE:-/home/matrix/.config/firecrawl/api-key}

test -s "$api_key_file"

# Prefer the discovered source registry (homepage + careers + news + blog +
# changelog per company). Fall back to homepage-only monitoring from the raw
# roster until scripts/discover-trigger-radar-sources.sh has been run.
if [ -s "$registry_path" ]; then
  input_path=$registry_path
  input_args=(--id-column 'Company ID' --metadata-column 'Source Type' --metadata-column 'Homepage')
else
  test -s "$roster_path"
  input_path=$roster_path
  input_args=()
fi

key_mode=$(stat -c '%a' "$api_key_file")
case "$key_mode" in
  400|600) ;;
  *)
    echo "Firecrawl API key file must have mode 400 or 600, not $key_mode" >&2
    exit 1
    ;;
esac

exec python3 "$orchestrator_dir/scripts/website_change_events.py" prepare \
  --input "$input_path" \
  --state-dir "$state_dir" \
  ${input_args[@]+"${input_args[@]}"} \
  --name-column 'Company Name' \
  --url-column 'Website URL' \
  --metadata-column 'Description' \
  --metadata-column 'Headcount' \
  --metadata-column 'Funding Total' \
  --metadata-column 'Last Funding Type' \
  --metadata-column 'Last Funding Date' \
  --metadata-column 'Primary contact name' \
  --metadata-column 'City' \
  --metadata-column 'Country' \
  --metadata-column 'Source Lists' \
  --api-key-file "$api_key_file" \
  --tag 'nessie-trigger-radar-daily' \
  --batch-size 25 \
  --page-timeout-ms 60000 \
  --timeout-seconds 600
