"""Company-preserving limits for the event JSONL passed to the consumer."""
import json
import math


def company_key(event):
    return event.get("company", {}).get("id") or event["eventId"]


def input_bytes(events):
    return sum(len((json.dumps(event, ensure_ascii=False, separators=(",", ":"),
                               sort_keys=True) + "\n").encode("utf-8")) for event in events)


def metrics(events):
    size = input_bytes(events)
    return {"events": len(events), "companies": len({company_key(e) for e in events}),
            "inputBytes": size, "estimatedInputTokens": math.ceil(size / 4)}


def fits(events, args):
    return (len(events) <= args.max_events
            and len({company_key(e) for e in events}) <= args.max_companies
            and input_bytes(events) <= args.max_input_bytes)


def select_batch(events, args, batch_id):
    groups = {}
    for event in events:
        groups.setdefault(company_key(event), []).append({**event, "batchId": batch_id})
    selected, deferred, blocked = [], [], []
    selected_bytes = selected_companies = 0
    for company, group in groups.items():
        group_metrics = metrics(group)
        group_bytes = group_metrics["inputBytes"]
        if (len(group) > args.max_events or group_bytes > args.max_input_bytes
                or group_metrics["companies"] > args.max_companies):
            blocked.append({"companyId": company, **group_metrics})
            deferred.extend(group)
        elif (selected_companies < args.max_companies
              and len(selected) + len(group) <= args.max_events
              and selected_bytes + group_bytes <= args.max_input_bytes):
            selected.extend(group)
            selected_bytes += group_bytes
            selected_companies += 1
        else:
            deferred.extend(group)
    # Deferred events have no delivery identity until selected for a real batch.
    return selected, [{k: v for k, v in e.items() if k != "batchId"} for e in deferred], blocked


def summary(state, args):
    pending = state.get("pending")
    pending_events = pending["events"] if pending else []
    backlog = state.get("backlog", [])
    selected, deferred, blocked = select_batch(backlog, args, "0" * 36)
    return {"limits": {"maxCompanies": args.max_companies, "maxEvents": args.max_events,
                       "maxInputBytes": args.max_input_bytes},
            "pendingBatchId": pending["batchId"] if pending else None,
            "pending": metrics(pending_events), "backlog": metrics(backlog),
            "nextBatch": metrics(pending_events if pending else selected),
            "remainingAfterNextBatch": metrics(backlog if pending else deferred),
            "pendingExceedsLimits": bool(pending and not fits(pending_events, args)),
            "oversizedCompanies": blocked,
            "lastCollectedAt": state.get("lastCollectedAt"),
            "lastAcknowledgedBatchId": state.get("lastAcknowledgedBatchId"),
            "tokenEstimateBasis": "event JSONL UTF-8 bytes / 4; not a model token limit"}
