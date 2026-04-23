#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import socket
import time
from typing import Any, Dict

from channel_common import (
    GitHubQueueClient,
    format_response_comment,
    parse_cmd_issue_body,
)


class CommandHandlers:
    @staticmethod
    def handle(command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if command == "ping":
            return {"pong": True, "utc": dt.datetime.utcnow().isoformat() + "Z"}
        if command == "echo":
            return {"echo": args}
        if command == "system.info":
            return {
                "hostname": socket.gethostname(),
                "utc_time": dt.datetime.utcnow().isoformat() + "Z",
            }
        raise ValueError(f"unknown command: {command}")


def make_client() -> GitHubQueueClient:
    token = os.getenv("GITHUB_TOKEN", "")
    owner = os.getenv("CHANNEL_OWNER", "")
    repo = os.getenv("CHANNEL_REPO", "")
    if not token or not owner or not repo:
        raise RuntimeError("need GITHUB_TOKEN, CHANNEL_OWNER, CHANNEL_REPO")
    return GitHubQueueClient(token, owner, repo)


def process_one_issue(client: GitHubQueueClient, issue) -> None:
    try:
        cmd = parse_cmd_issue_body(issue.body)
        result = CommandHandlers.handle(cmd["command"], cmd.get("args", {}))
        resp = {
            "version": "v1",
            "request_id": cmd["request_id"],
            "status": "ok",
            "result": result,
            "processed_at": dt.datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        req_id = "unknown"
        try:
            req_id = json.loads(issue.body).get("request_id", "unknown")
        except Exception:
            pass
        resp = {
            "version": "v1",
            "request_id": req_id,
            "status": "error",
            "error": str(e),
            "processed_at": dt.datetime.utcnow().isoformat() + "Z",
        }

    client.add_comment(issue.number, format_response_comment(resp))
    labels = [l for l in issue.labels if l != "channel:pending"]
    for l in ["channel:done"]:
        if l not in labels:
            labels.append(l)
    client.update_issue(issue.number, {"state": "closed", "labels": labels})

    # Optional reverse event issue
    evt_title = f"[evt] response {resp['request_id']}"
    evt_body = json.dumps(resp, ensure_ascii=False)
    client.create_issue(evt_title, evt_body, ["channel:event", "channel:response"])


def run_once(client: GitHubQueueClient):
    issues = client.list_open_cmd_issues(per_page=30)
    for issue in issues:
        process_one_issue(client, issue)
    return len(issues)


def run_loop(client: GitHubQueueClient, interval: int):
    while True:
        cnt = run_once(client)
        print(f"[{dt.datetime.utcnow().isoformat()}Z] processed={cnt}")
        time.sleep(interval)


def main():
    p = argparse.ArgumentParser(description="Server worker for GitHub issue queue")
    sub = p.add_subparsers(dest="mode", required=True)

    sub.add_parser("once", help="process current pending cmd issues once")
    lp = sub.add_parser("loop", help="run forever")
    lp.add_argument("--interval", type=int, default=int(os.getenv("CHANNEL_POLL_SECONDS", "3")))

    args = p.parse_args()
    client = make_client()

    if args.mode == "once":
        count = run_once(client)
        print(f"processed={count}")
    else:
        run_loop(client, args.interval)


if __name__ == "__main__":
    main()
