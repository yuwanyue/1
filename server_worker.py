#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import socket
import time
from typing import Any, Dict
from uuid import uuid4

from channel_common import (
    GitHubQueueClient,
    find_response_for_request_id,
    format_response_comment,
    parse_cmd_issue_body,
)

LABEL_CMD = "channel:cmd"
LABEL_PENDING = "channel:pending"
LABEL_PROCESSING = "channel:processing"
LABEL_DONE = "channel:done"
LABEL_RETRY = "channel:retry"
LABEL_DEAD = "channel:dead"
LEASE_PREFIX = "channel:lease:"
FAILURE_PREFIX = "channel:failures:"


class CommandHandlers:
    @staticmethod
    def handle(command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if command == "ping":
            return {"pong": True, "utc": utc_now_iso()}
        if command == "echo":
            return {"echo": args}
        if command == "system.info":
            return {
                "hostname": socket.gethostname(),
                "utc_time": utc_now_iso(),
            }
        raise ValueError(f"unknown command: {command}")


def make_client() -> GitHubQueueClient:
    token = os.getenv("GITHUB_TOKEN", "")
    owner = os.getenv("CHANNEL_OWNER", "")
    repo = os.getenv("CHANNEL_REPO", "")
    if not token or not owner or not repo:
        raise RuntimeError("need GITHUB_TOKEN, CHANNEL_OWNER, CHANNEL_REPO")
    return GitHubQueueClient(token, owner, repo)


def make_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


def lease_label(worker_id: str) -> str:
    return f"{LEASE_PREFIX}{worker_id}"


def normalize_labels(labels):
    return sorted({label for label in labels if label})


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def build_response(request_id: str, status: str, **extra) -> Dict[str, Any]:
    payload = {
        "version": "v1",
        "request_id": request_id,
        "status": status,
        "processed_at": utc_now_iso(),
    }
    payload.update(extra)
    return payload


def response_issue_title(request_id: str) -> str:
    return f"[evt] response {request_id}"


def max_failures() -> int:
    raw = os.getenv("CHANNEL_MAX_FAILURES", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(1, value)


def failure_count_from_labels(labels) -> int:
    count = 0
    for label in labels:
        if label.startswith(FAILURE_PREFIX):
            try:
                count = max(count, int(label[len(FAILURE_PREFIX) :]))
            except ValueError:
                continue
    return count


def replace_failure_label(labels, count: int):
    next_labels = [label for label in labels if not label.startswith(FAILURE_PREFIX)]
    next_labels.append(f"{FAILURE_PREFIX}{count}")
    return next_labels


def claim_issue(client: GitHubQueueClient, issue, worker_id: str):
    if issue.state != "open" or LABEL_PENDING not in issue.labels:
        return None
    mine = lease_label(worker_id)
    labels = [
        label
        for label in issue.labels
        if label != LABEL_PENDING and not label.startswith(LEASE_PREFIX)
    ]
    labels.extend([LABEL_PROCESSING, mine])
    client.update_issue(issue.number, {"labels": normalize_labels(labels)})
    refreshed = client.get_issue(issue.number)
    current_leases = [label for label in refreshed.labels if label.startswith(LEASE_PREFIX)]
    if refreshed.state != "open":
        return None
    if LABEL_PENDING in refreshed.labels:
        return None
    if LABEL_PROCESSING not in refreshed.labels:
        return None
    if current_leases != [mine]:
        return None
    return refreshed


def rollback_claim(client: GitHubQueueClient, issue_number: int, worker_id: str, reason: str):
    current = client.get_issue(issue_number)
    mine = lease_label(worker_id)
    labels = [
        label
        for label in current.labels
        if label != LABEL_PROCESSING and label != mine
    ]
    failures = failure_count_from_labels(labels) + 1
    labels = replace_failure_label(labels, failures)
    if failures >= max_failures():
        labels = [label for label in labels if label != LABEL_PENDING and label != LABEL_RETRY]
        labels.append(LABEL_DEAD)
        state = "closed"
    else:
        if LABEL_DONE not in labels and LABEL_PENDING not in labels:
            labels.append(LABEL_PENDING)
        if LABEL_RETRY not in labels:
            labels.append(LABEL_RETRY)
        state = "open"
    client.update_issue(issue_number, {"state": state, "labels": normalize_labels(labels)})
    return {"reason": reason, "failure_count": failures, "dead_lettered": failures >= max_failures()}


def ensure_response_event_issue(client: GitHubQueueClient, resp: Dict[str, Any]) -> None:
    title = response_issue_title(resp["request_id"])
    for issue in client.list_issues(state="all", labels=["channel:event", "channel:response"]):
        if issue.title == title:
            return
    evt_body = json.dumps(resp, ensure_ascii=False)
    client.create_issue(title, evt_body, ["channel:event", "channel:response"])


def finalize_issue(client: GitHubQueueClient, issue_number: int, worker_id: str) -> None:
    current = client.get_issue(issue_number)
    mine = lease_label(worker_id)
    labels = [
        label
        for label in current.labels
        if label not in {LABEL_PENDING, LABEL_PROCESSING, LABEL_RETRY, LABEL_DEAD, mine}
        and not label.startswith(LEASE_PREFIX)
    ]
    labels.append(LABEL_DONE)
    client.update_issue(issue_number, {"state": "closed", "labels": normalize_labels(labels)})


def process_one_issue(client: GitHubQueueClient, issue, worker_id: str) -> Dict[str, Any]:
    claimed = claim_issue(client, issue, worker_id)
    if claimed is None:
        return {"issue": issue.number, "status": "skipped", "reason": "claim-lost"}

    response_written = False
    resp = None
    try:
        cmd = parse_cmd_issue_body(claimed.body)
        existing = find_response_for_request_id(client.list_comments(claimed.number), cmd["request_id"])
        if existing is not None:
            resp = existing
            response_written = True
        else:
            result = CommandHandlers.handle(cmd["command"], cmd.get("args", {}))
            resp = build_response(cmd["request_id"], "ok", result=result)
            client.add_comment(claimed.number, format_response_comment(resp))
            response_written = True
    except Exception as e:
        try:
            req_id = json.loads(claimed.body).get("request_id", "unknown")
        except Exception:
            req_id = "unknown"
        resp = build_response(
            req_id,
            "error",
            error={
                "type": e.__class__.__name__,
                "message": str(e),
            },
        )
        try:
            existing = find_response_for_request_id(client.list_comments(claimed.number), req_id)
            if existing is None:
                client.add_comment(claimed.number, format_response_comment(resp))
            response_written = True
        except Exception as comment_error:
            rollback = rollback_claim(
                client,
                claimed.number,
                worker_id,
                f"response comment failed after retries: {comment_error}",
            )
            outcome = "moved to dead-letter queue" if rollback["dead_lettered"] else "returned to pending queue"
            raise RuntimeError(
                f"issue #{claimed.number} {outcome} after failure #{rollback['failure_count']}: {comment_error}"
            ) from comment_error

    try:
        ensure_response_event_issue(client, resp)
        finalize_issue(client, claimed.number, worker_id)
    except Exception as finalize_error:
        if not response_written:
            rollback = rollback_claim(
                client,
                claimed.number,
                worker_id,
                f"finalize failed before response persisted: {finalize_error}",
            )
            outcome = "moved to dead-letter queue" if rollback["dead_lettered"] else "returned to pending queue"
            raise RuntimeError(
                f"issue #{claimed.number} {outcome} after failure #{rollback['failure_count']}: {finalize_error}"
            ) from finalize_error
        raise RuntimeError(
            f"issue #{claimed.number} response persisted but finalization is incomplete: {finalize_error}"
        ) from finalize_error

    return {
        "issue": claimed.number,
        "status": "processed",
        "request_id": resp["request_id"],
        "replayed_response": response_written and existing is not None,
    }


def run_once(client: GitHubQueueClient, worker_id: str):
    issues = client.list_open_cmd_issues(per_page=30)
    stats = {
        "seen": len(issues),
        "processed": 0,
        "skipped": 0,
        "replayed": 0,
        "retried": 0,
        "dead_lettered": 0,
        "errors": 0,
    }
    results = []
    for issue in issues:
        before_labels = list(issue.labels)
        try:
            result = process_one_issue(client, issue, worker_id)
            if result["status"] == "processed":
                stats["processed"] += 1
                if result.get("replayed_response"):
                    stats["replayed"] += 1
            else:
                stats["skipped"] += 1
            results.append(result)
        except Exception as exc:
            stats["errors"] += 1
            current = client.get_issue(issue.number)
            current_failures = failure_count_from_labels(current.labels)
            if LABEL_DEAD in current.labels:
                stats["dead_lettered"] += 1
            elif current_failures > failure_count_from_labels(before_labels):
                stats["retried"] += 1
            results.append(
                {
                    "issue": issue.number,
                    "status": "error",
                    "error": str(exc),
                    "failure_count": current_failures,
                    "dead_lettered": LABEL_DEAD in current.labels,
                }
            )
    return {"worker_id": worker_id, "ts": utc_now_iso(), "stats": stats, "results": results}


def run_loop(client: GitHubQueueClient, interval: int, worker_id: str):
    while True:
        result = run_once(client, worker_id)
        print(json.dumps(result, ensure_ascii=False))
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser(description="Server worker for GitHub issue queue")
    sub = p.add_subparsers(dest="mode", required=True)

    sub.add_parser("once", help="process current pending cmd issues once")
    lp = sub.add_parser("loop", help="run forever")
    lp.add_argument("--interval", type=int, default=int(os.getenv("CHANNEL_POLL_SECONDS", "3")))

    args = p.parse_args()
    client = make_client()
    worker_id = os.getenv("CHANNEL_WORKER_ID", "").strip() or make_worker_id()

    if args.mode == "once":
        result = run_once(client, worker_id)
        print(json.dumps(result, ensure_ascii=False))
    else:
        run_loop(client, args.interval, worker_id)


if __name__ == "__main__":
    main()
