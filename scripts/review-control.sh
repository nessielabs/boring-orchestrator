#!/bin/bash

# Build the JSON prompt for one PR without passing large GitHub responses in
# process arguments. Bash function arguments stay inside the current process;
# the large values reach jq only through stdin.
build_review_context() {
  local repo=$1
  local selected=$2
  local workspace=$3
  local base_sha=$4
  local head_sha=$5
  local pr_context=$6
  local issue_comments=$7
  local inline_review_comments=$8
  local review_events=${9}
  local latest_mention
  local latest_agent_response

  latest_mention=$(jq -r '.latest_mention' <<< "$selected") || return 1
  latest_agent_response=$(jq -r '.latest_agent_response' <<< "$selected") || return 1

  printf '%s\n%s\n%s\n%s\n' \
    "$pr_context" \
    "$issue_comments" \
    "$inline_review_comments" \
    "$review_events" |
    jq -cs \
      --arg repo "$repo" \
      --arg action "review-requested" \
      --arg latest_mention "$latest_mention" \
      --arg latest_agent_response "$latest_agent_response" \
      --arg workspace "$workspace" \
      --arg base_sha "$base_sha" \
      --arg head_sha "$head_sha" \
      '{repo:$repo,number:.[0].number,title:.[0].title,action:$action,latest_mention:$latest_mention,latest_agent_response:$latest_agent_response,workspace:$workspace,base_sha:$base_sha,head_sha:$head_sha,pr_context:.[0],issue_comments:.[1],inline_review_comments:.[2],review_events:.[3]}'
}

# The orchestrator expects prompt_output to be a JSON string. Serialize the
# context from stdin so large reviews never become argv entries.
build_review_run_spec() {
  local review_context=$1
  local cwd=$2
  local cleanup_script=$3

  jq -ce \
    --arg cwd "$cwd" \
    --arg cleanup_script "$cleanup_script" \
    '{prompt_output:tojson,cwd:$cwd,cleanup_script:$cleanup_script}' \
    <<< "$review_context"
}

# Slurp newline-delimited run specs into the single fan-out control payload.
# Reject empty or oversized batches before the caller relinquishes cleanup.
build_review_fanout_control() {
  jq -cse '
    if length >= 1 and length <= 20 then
      {runs:.}
    else
      error("review fan-out must contain 1-20 runs")
    end
  '
}
