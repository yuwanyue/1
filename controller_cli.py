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
    sanitize_title,
    safe_json_arg,
)


def make_client() -> GitHubQueueClient:
    token = os.getenv("GITHUB_TOKEN", "")
    owner = os.getenv("CHANNEL_OWNER", "")
    repo = os.getenv("CHANNEL_REPO", "")
    if not token or not owner or not repo:
        raise RuntimeError("need GITHUB_TOKEN, CHANNEL_OWNER, CHANNEL_REPO")
    return GitHubQueueClient(token, owner, repo)


def enqueue(client: GitHubQueueClient, command: str, args: dict, request_id: str = ""):
    rid = request_id or generate_request_id()
    title = sanitize_title(f"[cmd] {command} ({rid})")
    body = json.dumps(
        {"version": "v1", "request_id": rid, "command": command, "args": args},
        ensure_ascii=False,
    )
    issue = client.create_issue(title, body, ["channel:cmd", "channel:pending"])
    return rid, issue["number"]


def wait_response(client: GitHubQueueClient, issue_number: int, request_id: str, timeout: int = 120, interval: int = 2):
    start = time.time()
    while time.time() - start < timeout:
        comments = client.list_comments(issue_number)
        responses = extract_response_comments(comments)
        for r in responses:
            if r.get("request_id") == request_id:
                return r
        time.sleep(interval)
    raise TimeoutError("wait response timeout")


def cmd_enqueue(args):
    client = make_client()
    obj = safe_json_arg(args.args)
    rid, issue_no = enqueue(client, args.command, obj, args.request_id)
    print(json.dumps({"request_id": rid, "issue_number": issue_no}, ensure_ascii=False))


def cmd_wait(args):
    client = make_client()
    resp = wait_response(client, args.issue, args.request_id, args.timeout, args.interval)
    print(json.dumps(resp, ensure_ascii=False))


def cmd_call(args):
    client = make_client()
    obj = safe_json_arg(args.args)
    rid, issue_no = enqueue(client, args.command, obj, args.request_id)
    resp = wait_response(client, issue_no, rid, args.timeout, args.interval)
    print(json.dumps({"issue": issue_no, "response": resp}, ensure_ascii=False))


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

    args = p.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
