#!/bin/bash
# Check open PRs across configured repos for review/re-review needs.
# Outputs JSON lines for each PR needing attention. Empty output = nothing to do.
#
AGENT_USER="lil-nessie"
MENTION="@lil-nessie"

REPOS=(
  "nessielabs/nessie-dashboard"
  "nessielabs/nessie-campaigns"
  "nessielabs/claude-plugins"
  "nessielabs/agents"
  "nessielabs/boring-orchestrator"
  "nessielabs/ubs"
  "nessielabs/nessie-notes-landing"
  "nessielabs/nessie-codebase"
  "nessielabs/nessie-notes-go"
  "nessielabs/nessie-skill"
)

results=()

json_user_login() {
  jq -r '(.user | if type == "object" then .login else . end) // empty'
}

for repo in "${REPOS[@]}"; do
  prs=$(gh pr list --repo "$repo" --state open --json number,title,body,createdAt,headRefName,author --jq '.[] | @json' 2>/dev/null)
  [ -z "$prs" ] && continue

  while IFS= read -r pr_json; do
    number=$(echo "$pr_json" | jq -r '.number')
    title=$(echo "$pr_json" | jq -r '.title')
    author=$(echo "$pr_json" | jq -r '.author.login // ""')
    pr_body=$(echo "$pr_json" | jq -r '.body // ""')
    pr_created=$(echo "$pr_json" | jq -r '.createdAt // ""')

    latest_mention_time=""
    latest_agent_response_time=""

    if [[ "$pr_body" == *"$MENTION"* ]] && [ -n "$pr_created" ]; then
      latest_mention_time="$pr_created"
    fi

    while IFS= read -r comment; do
      [ -z "$comment" ] && continue
      user=$(echo "$comment" | json_user_login)
      created=$(echo "$comment" | jq -r '.created_at')
      body=$(echo "$comment" | jq -r '.body // ""')

      if [ "$user" != "$AGENT_USER" ] && [[ "$body" == *"$MENTION"* ]]; then
        if [ -z "$latest_mention_time" ] || [[ "$created" > "$latest_mention_time" ]]; then
          latest_mention_time="$created"
        fi
      fi

      if [ "$user" = "$AGENT_USER" ]; then
        if [ -z "$latest_agent_response_time" ] || [[ "$created" > "$latest_agent_response_time" ]]; then
          latest_agent_response_time="$created"
        fi
      fi
    done <<< "$(gh api "repos/$repo/issues/$number/comments" --paginate --jq '.[] | @json' 2>/dev/null)"

    while IFS= read -r comment; do
      [ -z "$comment" ] && continue
      user=$(echo "$comment" | json_user_login)
      created=$(echo "$comment" | jq -r '.created_at')
      body=$(echo "$comment" | jq -r '.body // ""')

      if [ "$user" != "$AGENT_USER" ] && [[ "$body" == *"$MENTION"* ]]; then
        if [ -z "$latest_mention_time" ] || [[ "$created" > "$latest_mention_time" ]]; then
          latest_mention_time="$created"
        fi
      fi

      if [ "$user" = "$AGENT_USER" ]; then
        if [ -z "$latest_agent_response_time" ] || [[ "$created" > "$latest_agent_response_time" ]]; then
          latest_agent_response_time="$created"
        fi
      fi
    done <<< "$(gh api "repos/$repo/pulls/$number/comments" --paginate --jq '.[] | @json' 2>/dev/null)"

    while IFS= read -r review; do
      [ -z "$review" ] && continue
      user=$(echo "$review" | json_user_login)
      submitted=$(echo "$review" | jq -r '.submitted_at')

      if [ "$user" = "$AGENT_USER" ]; then
        if [ -n "$submitted" ] && { [ -z "$latest_agent_response_time" ] || [[ "$submitted" > "$latest_agent_response_time" ]]; }; then
          latest_agent_response_time="$submitted"
        fi
      fi
    done <<< "$(gh api "repos/$repo/pulls/$number/reviews" --paginate --jq '.[] | @json' 2>/dev/null)"

    if [ -n "$latest_mention_time" ] && { [ -z "$latest_agent_response_time" ] || [[ "$latest_mention_time" > "$latest_agent_response_time" ]]; }; then
      results+=("{\"repo\":\"$repo\",\"number\":$number,\"title\":$(echo "$title" | jq -Rs .),\"author\":\"$author\",\"action\":\"review-requested\",\"latest_mention\":\"$latest_mention_time\",\"latest_agent_response\":\"$latest_agent_response_time\"}")
    fi
  done <<< "$prs"
done

if [ ${#results[@]} -eq 0 ]; then
  exit 0
fi

for result in "${results[@]}"; do
  echo "$result"
done
