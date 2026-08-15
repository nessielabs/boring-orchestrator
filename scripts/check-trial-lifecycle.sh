#!/usr/bin/env bash
# Deterministic trial lifecycle sender.
#
# Each campaign is previewed immediately before sending. Empty audiences do
# nothing. Non-empty audiences are sent through the campaign runner, recorded
# in runs/ audit files, committed, and pushed. The Boring Orchestrator agent is
# script-only, so no LLM participates in recipient selection or delivery.

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

CAMPAIGNS_DIR="${NESSIE_CAMPAIGNS_DIR:-$HOME/nessie-campaigns}"
PYTHON_BIN="${NESSIE_CAMPAIGNS_PYTHON:-$CAMPAIGNS_DIR/.venv/bin/python}"
RESEND_KEY_FILE="${NESSIE_RESEND_API_KEY_FILE:-$HOME/.config/nessie-campaigns/resend-api-key}"
SYNC_GIT="${NESSIE_CAMPAIGNS_SYNC_GIT:-1}"
CAMPAIGN_IDS=(
  "trial-needs-activation"
  "trial-near-expiry"
)

if [ -z "${RESEND_API_KEY:-}" ] && [ -r "$RESEND_KEY_FILE" ]; then
  IFS= read -r RESEND_API_KEY < "$RESEND_KEY_FILE"
  export RESEND_API_KEY
fi
: "${RESEND_API_KEY:?RESEND_API_KEY must be set or stored in $RESEND_KEY_FILE}"

cd "$CAMPAIGNS_DIR"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Campaign Python environment not found at $PYTHON_BIN" >&2
  echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if command -v flock >/dev/null 2>&1; then
  exec 9>"${NESSIE_CAMPAIGNS_LOCK_FILE:-/tmp/nessie-campaigns-send.lock}"
  if ! flock -n 9; then
    # Another campaign sender owns the checkout. The next six-hour tick will
    # safely resolve the still-due audience.
    exit 0
  fi
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Tracked changes exist in $CAMPAIGNS_DIR; refusing automated send" >&2
  git status --short >&2
  exit 1
fi

publish_audits() {
  git add -A -- \
    campaigns/trial-needs-activation \
    campaigns/trial-near-expiry
  if git diff --cached --quiet; then
    return 0
  fi

  git commit --quiet \
    -m "Run: trial lifecycle (automated)" \
    -m "Record the unattended trial activation and near-expiry campaign sends."

  if [ "$SYNC_GIT" != "1" ]; then
    return 0
  fi

  for attempt in 1 2 3; do
    if git pull --rebase --quiet && git push --quiet; then
      return 0
    fi
    echo "Audit push attempt $attempt failed; retrying" >&2
  done
  echo "Could not push trial lifecycle audit records after three attempts" >&2
  return 1
}

# Recover and publish a run record left by an interrupted prior tick before
# resolving a new audience. Provider and runs-based dedup prevent re-sends.
publish_audits
if [ "$SYNC_GIT" = "1" ]; then
  git pull --ff-only --quiet
fi

run_files=()
for campaign_id in "${CAMPAIGN_IDS[@]}"; do
  preview_output=""
  if ! preview_output=$(NESSIE_CAMPAIGNS_DIR="$CAMPAIGNS_DIR" \
    "$PYTHON_BIN" scripts/preview.py "$campaign_id" 2>&1); then
    echo "Preview failed for $campaign_id:" >&2
    echo "$preview_output" >&2
    exit 1
  fi

  recipient_count=$("$PYTHON_BIN" - "$campaign_id" <<'PY'
import json
import sys
from pathlib import Path

path = Path("/tmp") / f"preview-{sys.argv[1]}.json"
print(len(json.loads(path.read_text())) if path.exists() else 0)
PY
  )
  if [ "$recipient_count" -eq 0 ]; then
    continue
  fi

  before_status=$(git status --porcelain -- "campaigns/$campaign_id/runs")
  if [ -n "$before_status" ]; then
    echo "Unexpected pending run files for $campaign_id after recovery" >&2
    echo "$before_status" >&2
    exit 1
  fi

  send_output=""
  if ! send_output=$(NESSIE_CAMPAIGNS_DIR="$CAMPAIGNS_DIR" \
    "$PYTHON_BIN" scripts/send.py "$campaign_id" --yes 2>&1); then
    echo "Send failed for $campaign_id:" >&2
    echo "$send_output" >&2
    exit 1
  fi

  while IFS= read -r path; do
    [ -n "$path" ] && run_files+=("$path")
  done < <(
    git status --porcelain -- "campaigns/$campaign_id/runs" \
      | sed -E 's/^...//'
  )
done

if [ "${#run_files[@]}" -eq 0 ]; then
  exit 0
fi

summary=$("$PYTHON_BIN" - "${run_files[@]}" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

campaigns = []
total = Counter()
for filename in sys.argv[1:]:
    record = yaml.safe_load(Path(filename).read_text())
    send = record["send"]
    variants = Counter(
        recipient.get("template_variant", "default")
        for recipient in record.get("recipients", [])
    )
    item = {
        "campaign_id": record["campaign_id"],
        "attempted": send["attempted"],
        "sent": send["succeeded"],
        "failed": send["failed"],
        "not_attempted": send.get("not_attempted", 0),
        "suppressed_at_send": record.get("dedup", {}).get(
            "suppressed_at_send", 0
        ),
        "variants": dict(sorted(variants.items())),
    }
    campaigns.append(item)
    total.update(
        attempted=item["attempted"],
        sent=item["sent"],
        failed=item["failed"],
        not_attempted=item["not_attempted"],
        suppressed_at_send=item["suppressed_at_send"],
    )

print(json.dumps({
    "mode": "automated_send",
    "campaigns": campaigns,
    "total": dict(total),
}, sort_keys=True))
PY
)

publish_audits
printf '%s\n' "$summary"
