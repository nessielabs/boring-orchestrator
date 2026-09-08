#!/usr/bin/env bash
# Build (or refresh) the per-company source registry the daily producer monitors.
# Uses Firecrawl map once per company (1 credit each); results are cached so
# re-runs cost nothing unless --refresh is passed through.
set -euo pipefail

orchestrator_dir=${BORING_ORCHESTRATOR_DIR:-/home/matrix/boring-orchestrator}
roster_path=${TRIGGER_RADAR_ROSTER:-/home/matrix/trigger-radar/sources/ashtan-combined-deduplicated.csv}
registry_path=${TRIGGER_RADAR_REGISTRY:-/home/matrix/trigger-radar/sources/source-registry.csv}
cache_dir=${TRIGGER_RADAR_MAP_CACHE_DIR:-/home/matrix/trigger-radar/state/map-cache}
api_key_file=${FIRECRAWL_API_KEY_FILE:-/home/matrix/.config/firecrawl/api-key}

test -s "$roster_path"
test -s "$api_key_file"

exec python3 "$orchestrator_dir/scripts/discover_company_sources.py" \
  --input "$roster_path" \
  --output "$registry_path" \
  --cache-dir "$cache_dir" \
  --errors-file "${registry_path%.csv}-errors.json" \
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
  --include-homepage \
  --api-key-file "$api_key_file" \
  "$@"
