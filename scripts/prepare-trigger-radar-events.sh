#!/usr/bin/env bash
set -euo pipefail

orchestrator_dir=${BORING_ORCHESTRATOR_DIR:-/home/matrix/boring-orchestrator}
roster_path=${TRIGGER_RADAR_ROSTER:-/home/matrix/trigger-radar/sources/ashtan-combined-deduplicated.csv}
state_dir=${TRIGGER_RADAR_STATE_DIR:-/home/matrix/trigger-radar/state/website-change-events}
api_key_file=${FIRECRAWL_API_KEY_FILE:-/home/matrix/.config/firecrawl/api-key}

test -s "$roster_path"
test -s "$api_key_file"

key_mode=$(stat -c '%a' "$api_key_file")
case "$key_mode" in
  400|600) ;;
  *)
    echo "Firecrawl API key file must have mode 400 or 600, not $key_mode" >&2
    exit 1
    ;;
esac

exec python3 "$orchestrator_dir/scripts/website_change_events.py" prepare \
  --input "$roster_path" \
  --state-dir "$state_dir" \
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
