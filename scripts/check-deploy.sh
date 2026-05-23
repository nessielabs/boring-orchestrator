#!/bin/bash
# Pre-script for self-deploy agent.
# Outputs the new commits only when origin/main is ahead of the running code.
# Empty output = no deploy needed = agent run skipped (zero cost).

cd ~/boring-orchestrator || exit 1

LOCAL=$(git rev-parse HEAD)
git fetch origin main --quiet 2>/dev/null
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  # Nothing new — empty output skips the agent run
  exit 0
fi

# There are new commits — output them so the agent can deploy
echo "NEW_COMMITS_DETECTED"
echo "local=$LOCAL"
echo "remote=$REMOTE"
git log --oneline "$LOCAL..$REMOTE"
