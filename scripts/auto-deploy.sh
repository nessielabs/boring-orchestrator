#!/bin/bash
# Auto-deploy script for boring-orchestrator.
# Checks if origin/main has new commits and deploys if so.
# Run via system cron every minute: * * * * * ~/boring-orchestrator/scripts/auto-deploy.sh >> ~/boring-orchestrator/deploy.log 2>&1
#
# Zero cost — no AI tokens used. Pure bash.

set -euo pipefail

DIR="$HOME/boring-orchestrator"
cd "$DIR"

LOCAL=$(git rev-parse HEAD)
git fetch origin main --quiet 2>/dev/null
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Deploying: $LOCAL -> $REMOTE"
git log --oneline "$LOCAL..$REMOTE"

# Pull
git reset --hard origin/main

# Install deps if package.json changed
if git diff --name-only "$LOCAL" "$REMOTE" | grep -q "package.json"; then
  echo "package.json changed, running npm install..."
  export PATH="$HOME/.local/share/fnm/node-versions/v22.22.2/installation/bin:$HOME/.local/bin:$PATH"
  npm install
fi

# Restart server
export PATH="$DIR/node_modules/.bin:$HOME/.local/share/fnm/node-versions/v22.22.2/installation/bin:$HOME/.local/bin:$PATH"
fuser -k 44066/tcp 2>/dev/null || true
sleep 2
nohup bash -c "cd $DIR && export PATH=\"$DIR/node_modules/.bin:$HOME/.local/share/fnm/node-versions/v22.22.2/installation/bin:$HOME/.local/bin:\$PATH\" && tsx server.ts" > "$DIR/server.log" 2>&1 &
disown

sleep 3
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:44066/api/agents)
if [ "$HTTP_CODE" = "200" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Deploy successful. Server responding 200."
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARNING: Server returned $HTTP_CODE after restart!"
fi
