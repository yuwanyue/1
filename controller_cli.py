#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time

from channel_common import (
    GitHubQueueClient,
    extract_response_comments,
    generate_request_id,
    parse_cmd_issue_body,
    sanitize_title,
    safe_json_arg,
)

LABEL_PENDING = "channel:pending"
LABEL_PROCESSING = "channel:processing"
LABEL_DONE = "channel:done"
LABEL_RETRY = "channel:retry"
LABEL_DEAD = "channel:dead"
LEASE_PREFIX = "channel:lease:"
FAILURE_PREFIX = "channel:failures:"


def auto_cleanup_enabled() -> bool:
    return os.getenv("CHANNEL_AUTO_CLEANUP", "true").strip().lower() not in {"0", "false", "no"}


def make_client() -> GitHubQueueClient:
    token = os.getenv("GITHUB_TOKEN", "")
    owner = os.getenv("CHANNEL_OWNER", "")
    repo = os.getenv("CHANNEL_REPO", "")
    if not token or not owner or not repo:
        raise RuntimeError("need GITHUB_TOKEN, CHANNEL_OWNER, CHANNEL_REPO")
    return GitHubQueueClient(token, owner, repo)


def enqueue(client: GitHubQueueClient, command: str, args: dict, request_id: str = ""):
    rid = request_id or generate_request_id()
    existing = find_issue_by_request_id(client, rid)
    if existing is not None:
        payload = parse_cmd_issue_body(existing.body)
        if payload["command"] != command or payload["args"] != args:
            raise ValueError(
                f"request_id={rid} already exists on issue #{existing.number} with different payload"
            )
        return rid, existing.number
    title = sanitize_title(f"[cmd] {command} ({rid})")
    body = json.dumps(
        {"version": "v1", "request_id": rid, "command": command, "args": args},
        ensure_ascii=False,
    )
    issue = client.create_issue(title, body, ["channel:cmd", "channel:pending"])
    return rid, issue["number"]


def find_issue_by_request_id(client: GitHubQueueClient, request_id: str):
    for issue in client.list_issues(state="all", labels=["channel:cmd"]):
        try:
            payload = parse_cmd_issue_body(issue.body)
        except ValueError:
            continue
        if payload["request_id"] == request_id:
            return issue
    return None


def normalize_labels(labels):
    return sorted({label for label in labels if label})


def reset_issue_for_queue(issue, keep_done: bool = False):
    labels = []
    for label in issue.labels:
        if label.startswith(LEASE_PREFIX):
            continue
        if label.startswith(FAILURE_PREFIX):
            continue
        if label in {LABEL_PROCESSING, LABEL_RETRY, LABEL_DEAD}:
            continue
        if label == LABEL_DONE and not keep_done:
            continue
        labels.append(label)
    if LABEL_PENDING not in labels:
        labels.append(LABEL_PENDING)
    return normalize_labels(labels)


def requeue_issue(client: GitHubQueueClient, issue_number: int):
    issue = client.get_issue(issue_number)
    payload = parse_cmd_issue_body(issue.body)
    labels = reset_issue_for_queue(issue)
    client.update_issue(issue.number, {"state": "open", "labels": labels})
    return {
        "action": "requeue",
        "issue_number": issue.number,
        "request_id": payload["request_id"],
        "labels": labels,
    }


def replay_issue(client: GitHubQueueClient, issue_number: int, new_request_id: str = ""):
    issue = client.get_issue(issue_number)
    payload = parse_cmd_issue_body(issue.body)
    rid = new_request_id or generate_request_id()
    if rid == payload["request_id"]:
        return requeue_issue(client, issue_number)
    return_payload = {
        "version": payload["version"],
        "request_id": rid,
        "command": payload["command"],
        "args": payload["args"],
    }
    title = sanitize_title(f"[cmd] {payload['command']} ({rid})")
    created = client.create_issue(title, json.dumps(return_payload, ensure_ascii=False), ["channel:cmd", "channel:pending"])
    return {
        "action": "replay",
        "source_issue_number": issue.number,
        "issue_number": created["number"],
        "request_id": rid,
    }


def wait_response(client: GitHubQueueClient, issue_number: int, request_id: str, timeout: int = 120, interval: int = 2):
    start = time.time()
    while time.time() - start < timeout:
        comments = client.list_comments(issue_number)
        responses = extract_response_comments(comments)
        for r in responses:
            if r.get("request_id") == request_id:
                return r
        time.sleep(interval)
    waited = int(time.time() - start)
    raise TimeoutError(
        f"wait response timeout for request_id={request_id} issue={issue_number} after {waited}s"
    )


def cleanup_request_records(client: GitHubQueueClient, issue_number: int, request_id: str):
    errors = []
    try:
        client.delete_issue(issue_number)
    except Exception as e:
        errors.append(f"delete cmd issue #{issue_number} failed: {e}")

    for issue in client.list_issues(state="all", labels=["channel:event", "channel:response"]):
        if issue.title != f"[evt] response {request_id}":
            continue
        try:
            client.delete_issue(issue.number)
        except Exception as e:
            errors.append(f"delete event issue #{issue.number} failed: {e}")
    return errors


def cmd_enqueue(args):
    client = make_client()
    obj = safe_json_arg(args.args)
    rid, issue_no = enqueue(client, args.command, obj, args.request_id)
    print(json.dumps({"request_id": rid, "issue_number": issue_no}, ensure_ascii=False))


def cmd_wait(args):
    client = make_client()
    resp = wait_response(client, args.issue, args.request_id, args.timeout, args.interval)
    cleanup_errors = []
    if auto_cleanup_enabled():
        cleanup_errors = cleanup_request_records(client, args.issue, args.request_id)
        for err in cleanup_errors:
            print(f"warning: {err}", file=sys.stderr)
    print(json.dumps(resp, ensure_ascii=False))


def cmd_call(args):
    client = make_client()
    obj = safe_json_arg(args.args)
    rid, issue_no = enqueue(client, args.command, obj, args.request_id)
    resp = wait_response(client, issue_no, rid, args.timeout, args.interval)
    cleanup_errors = []
    if auto_cleanup_enabled():
        cleanup_errors = cleanup_request_records(client, issue_no, rid)
        for err in cleanup_errors:
            print(f"warning: {err}", file=sys.stderr)
    print(json.dumps({"issue": issue_no, "response": resp}, ensure_ascii=False))


def cmd_requeue(args):
    client = make_client()
    result = requeue_issue(client, args.issue)
    print(json.dumps(result, ensure_ascii=False))


def cmd_replay(args):
    client = make_client()
    result = replay_issue(client, args.issue, args.request_id)
    print(json.dumps(result, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="External controller for async issue channel")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue")
    p_enq.add_argument("command")
    p_enq.add_argument("--args", default="{}")
    p_enq.add_argument("--request-id", default="")
    p_enq.set_defaults(func=cmd_enqueue)

    p_wait = sub.add_parser("wait")
    p_wait.add_argument("request_id")
    p_wait.add_argument("--issue", type=int, required=True)
    p_wait.add_argument("--timeout", type=int, default=120)
    p_wait.add_argument("--interval", type=int, default=2)
    p_wait.set_defaults(func=cmd_wait)

    p_call = sub.add_parser("call")
    p_call.add_argument("command")
    p_call.add_argument("--args", default="{}")
    p_call.add_argument("--request-id", default="")
    p_call.add_argument("--timeout", type=int, default=120)
    p_call.add_argument("--interval", type=int, default=2)
    p_call.set_defaults(func=cmd_call)

    p_requeue = sub.add_parser("requeue")
    p_requeue.add_argument("--issue", type=int, required=True)
    p_requeue.set_defaults(func=cmd_requeue)

    p_replay = sub.add_parser("replay")
    p_replay.add_argument("--issue", type=int, required=True)
    p_replay.add_argument("--request-id", default="")
    p_replay.set_defaults(func=cmd_replay)

    args = p.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
