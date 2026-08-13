#!/usr/bin/env bash
# Cost gate for trial lifecycle campaign previews.
#
# The script runs deterministic campaign previews first. It prints one compact
# JSON object only when at least one recipient is due, so Boring Orchestrator
# skips Claude entirely on empty ticks. This script never sends email.

set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
: "${RESEND_API_KEY:?RESEND_API_KEY must be set in the orchestrator environment}"

CAMPAIGNS_DIR="${NESSIE_CAMPAIGNS_DIR:-$HOME/nessie-campaigns}"
PYTHON_BIN="${NESSIE_CAMPAIGNS_PYTHON:-$CAMPAIGNS_DIR/.venv/bin/python}"
CAMPAIGN_IDS=(
  "trial-needs-activation"
  "trial-near-expiry"
)

cd "$CAMPAIGNS_DIR"
git pull --ff-only --quiet

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Campaign Python environment not found at $PYTHON_BIN" >&2
  echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

for campaign_id in "${CAMPAIGN_IDS[@]}"; do
  preview_output=""
  if ! preview_output=$(NESSIE_CAMPAIGNS_DIR="$CAMPAIGNS_DIR" \
    "$PYTHON_BIN" scripts/preview.py "$campaign_id" 2>&1); then
    echo "Preview failed for $campaign_id:" >&2
    echo "$preview_output" >&2
    exit 1
  fi
done

"$PYTHON_BIN" - "${CAMPAIGN_IDS[@]}" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

campaigns = []
for campaign_id in sys.argv[1:]:
    preview_file = Path("/tmp") / f"preview-{campaign_id}.json"
    if not preview_file.exists():
        continue
    recipients = json.loads(preview_file.read_text())
    if not recipients:
        continue
    variants = Counter(
        recipient.get("template_variant", "default")
        for recipient in recipients
    )
    campaigns.append(
        {
            "campaign_id": campaign_id,
            "recipient_count": len(recipients),
            "variants": dict(sorted(variants.items())),
            "sample": [
                {
                    "email": recipient.get("email"),
                    "name": recipient.get("name") or "",
                    "template_variant": recipient.get(
                        "template_variant", "default"
                    ),
                }
                for recipient in recipients[:20]
            ],
        }
    )

if campaigns:
    print(json.dumps({"mode": "preview_only", "campaigns": campaigns}))
PY
