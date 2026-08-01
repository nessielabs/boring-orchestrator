#!/bin/bash
# Check open PRs across configured repos for review/re-review needs.
# Prepares exact-head worktrees and emits parallel review runs. Empty output = nothing to do.
#
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
source "$SCRIPT_DIR/review-control.sh" || exit 1

AGENT_USER="lil-nessie"
MENTION="@lil-nessie"
REPO_CACHE_ROOT="${PR_REVIEW_REPO_CACHE_ROOT:-$HOME/nessie-review-repos}"
WORKTREE_ROOT="${PR_REVIEW_WORKTREE_ROOT:-$HOME/nessie-review-worktrees}"
MAX_PARALLEL_REVIEWS=20

REPOS=(
  "nessielabs/nessie-dashboard"
  "nessielabs/nessie-campaigns"
  "nessielabs/claude-plugins"
  "nessielabs/agents"
  "nessielabs/boring-orchestrator"
  "nessielabs/nessie-notes-landing"
  "nessielabs/nessie-codebase"
  "nessielabs/nessie-notes-go"
  "nessielabs/nessie-skill"
  "nessielabs/nessie-hermes"
)

review_requests=()

json_user_login() {
  jq -r '(.user | if type == "object" then .login else . end) // empty'
}

for repo in "${REPOS[@]}"; do
  # Get all open PRs
  prs=$(gh pr list --repo "$repo" --state open --json number,title,body,createdAt,headRefName --jq '.[] | @json' 2>/dev/null)
  [ -z "$prs" ] && continue

  while IFS= read -r pr_json; do
    number=$(echo "$pr_json" | jq -r '.number')
    title=$(echo "$pr_json" | jq -r '.title')
    pr_body=$(echo "$pr_json" | jq -r '.body // ""')
    pr_created=$(echo "$pr_json" | jq -r '.createdAt // ""')

    latest_mention_time=""
    latest_agent_response_time=""

    if [[ "$pr_body" == *"$MENTION"* ]] && [ -n "$pr_created" ]; then
      latest_mention_time="$pr_created"
    fi

    # Check top-level PR comments for explicit @lil-nessie mentions and replies.
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

    # Check inline review comments too, so "@lil-nessie" on a diff line works.
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

    # Reviews are how the agent usually replies. Count them as responses to avoid
    # re-triggering after lil-nessie has already reviewed the latest mention.
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

    # Trigger only when there is an unreplied explicit mention.
    if [ -n "$latest_mention_time" ] && { [ -z "$latest_agent_response_time" ] || [[ "$latest_mention_time" > "$latest_agent_response_time" ]]; }; then
      review_requests+=("$(jq -cn \
        --arg repo "$repo" \
        --argjson number "$number" \
        --arg title "$title" \
        --arg latest_mention "$latest_mention_time" \
        --arg latest_agent_response "$latest_agent_response_time" \
        '{repo:$repo,number:$number,title:$title,action:"review-requested",latest_mention:$latest_mention,latest_agent_response:$latest_agent_response}')")
      if [ "${#review_requests[@]}" -ge "$MAX_PARALLEL_REVIEWS" ]; then
        break 2
      fi
    fi

  done <<< "$prs"
done

# Empty means nothing to do, so Boring Orchestrator skips this tick.
if [ "${#review_requests[@]}" -eq 0 ]; then
  exit 0
fi

mkdir -p -- "$REPO_CACHE_ROOT" "$WORKTREE_ROOT"
prepared_cache_repos=()
prepared_worktrees=()
run_specs=()
handoff_complete=false

cleanup_prepared_workspaces() {
  if [ "${handoff_complete:-false}" = "true" ]; then
    return
  fi
  for ((index=${#prepared_worktrees[@]} - 1; index >= 0; index--)); do
    git --git-dir="${prepared_cache_repos[$index]}" worktree remove --force "${prepared_worktrees[$index]}" >&2 || true
    git --git-dir="${prepared_cache_repos[$index]}" worktree prune --expire now >&2 || true
  done
}
trap cleanup_prepared_workspaces EXIT

for selected in "${review_requests[@]}"; do
  repo=$(echo "$selected" | jq -r '.repo')
  number=$(echo "$selected" | jq -r '.number')

  # Re-query immediately before preparing each workspace so every run is pinned
  # to the exact open PR head rather than the earlier discovery snapshot.
  pr_context=$(gh pr view "$number" --repo "$repo" --json \
    number,title,body,author,url,state,isDraft,baseRefName,headRefName,headRefOid,additions,deletions,changedFiles,files,comments,reviews,commits) || exit 1
  pr_refs=$(gh api "repos/$repo/pulls/$number") || exit 1

  if [ "$(echo "$pr_refs" | jq -r '.state')" != "open" ]; then
    continue
  fi

  base_ref=$(echo "$pr_refs" | jq -r '.base.ref')
  expected_head_sha=$(echo "$pr_refs" | jq -r '.head.sha')
  repo_key=${repo//\//__}
  cache_repo="$REPO_CACHE_ROOT/$repo_key.git"

  # Clone each repository only once. Every review thereafter fetches into the
  # same blobless object store and gets an isolated linked worktree.
  if [ ! -e "$cache_repo" ]; then
    gh repo clone "$repo" "$cache_repo" -- --bare --filter=blob:none >&2 || exit 1
  fi

  if [ "$(git --git-dir="$cache_repo" rev-parse --is-bare-repository 2>/dev/null)" != "true" ]; then
    echo "PR reviewer cache is not a bare Git repository: $cache_repo" >&2
    exit 1
  fi

  git --git-dir="$cache_repo" fetch --quiet --force origin \
    "+refs/heads/$base_ref:refs/review/base-$number" \
    "+refs/pull/$number/head:refs/review/head-$number" || exit 1

  base_sha=$(git --git-dir="$cache_repo" rev-parse "refs/review/base-$number^{commit}") || exit 1
  head_sha=$(git --git-dir="$cache_repo" rev-parse "refs/review/head-$number^{commit}") || exit 1

  if [ "$head_sha" != "$expected_head_sha" ]; then
    echo "PR changed while preparing review workspace; retrying on the next tick" >&2
    exit 1
  fi

  worktree_path=$(mktemp -d "$WORKTREE_ROOT/${repo_key}-pr${number}-XXXXXX") || exit 1
  rmdir -- "$worktree_path" || exit 1
  git --git-dir="$cache_repo" worktree add --quiet --detach "$worktree_path" "$head_sha" || exit 1
  prepared_cache_repos+=("$cache_repo")
  prepared_worktrees+=("$worktree_path")

  issue_comments=$(gh api "repos/$repo/issues/$number/comments" --paginate) || exit 1
  inline_review_comments=$(gh api "repos/$repo/pulls/$number/comments" --paginate) || exit 1
  review_events=$(gh api "repos/$repo/pulls/$number/reviews" --paginate) || exit 1

  review_context=$(build_review_context \
    "$repo" \
    "$selected" \
    "$worktree_path" \
    "$base_sha" \
    "$head_sha" \
    "$pr_context" \
    "$issue_comments" \
    "$inline_review_comments" \
    "$review_events") || exit 1

  printf -v cleanup_script 'git --git-dir=%q worktree remove --force %q && git --git-dir=%q worktree prune --expire now' \
    "$cache_repo" "$worktree_path" "$cache_repo"
  run_spec=$(build_review_run_spec "$review_context" "$worktree_path" "$cleanup_script") || exit 1
  run_specs+=("$run_spec")
done

if [ "${#run_specs[@]}" -eq 0 ]; then
  exit 0
fi

if [ "${#run_specs[@]}" -ne "${#prepared_worktrees[@]}" ]; then
  echo "PR reviewer prepared workspaces without matching run specs" >&2
  exit 1
fi

control=$(printf '%s\n' "${run_specs[@]}" | build_review_fanout_control) || exit 1
printf '%s%s\n' '::boring-orchestrator::' "$control" || exit 1

# Boring Orchestrator owns every worktree after it receives the fan-out line.
handoff_complete=true
trap - EXIT
