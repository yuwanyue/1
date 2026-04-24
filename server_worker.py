#!/usr/bin/env python3
import argparse
import base64
import datetime as dt
import json
import os
import socket
import time
import urllib.error
import urllib.request
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


def _api_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "async-channel-template/1.0",
    }


def _api_json(method: str, url: str, token: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    data = None
    headers = _api_headers(token)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


def _download_bytes(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, method="GET", headers=_api_headers(token))
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _delete_release_and_tag(base: str, token: str, run_id: str) -> None:
    release = _api_json("GET", f"{base}/releases/tags/run-{run_id}", token)
    release_id = release.get("id")
    if release_id:
        req = urllib.request.Request(
            f"{base}/releases/{release_id}",
            method="DELETE",
            headers=_api_headers(token),
        )
        with urllib.request.urlopen(req, timeout=30):
            pass

    req = urllib.request.Request(
        f"{base}/git/refs/tags/run-{run_id}",
        method="DELETE",
        headers=_api_headers(token),
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise


def run_github_egress_fetch(args: Dict[str, Any]) -> Dict[str, Any]:
    token = os.getenv("GITHUB_TOKEN", "")
    owner = os.getenv("CHANNEL_OWNER", "")
    repo = os.getenv("CHANNEL_REPO", "")
    if not token or not owner or not repo:
        raise RuntimeError("need GITHUB_TOKEN, CHANNEL_OWNER, CHANNEL_REPO")

    url = str(args.get("url", "")).strip()
    if not url:
        raise ValueError("egress.fetch requires args.url")

    method = str(args.get("method", "GET")).upper().strip() or "GET"
    mode = str(args.get("mode", "fetch")).strip() or "fetch"
    workflow = str(args.get("workflow", os.getenv("CHANNEL_EGRESS_WORKFLOW", "egress-fetch.yml")))
    request_id = str(args.get("request_id", f"eg_{int(time.time())}_{uuid4().hex[:8]}"))

    body_text = str(args.get("body", ""))
    browser_script = str(args.get("browser_script", ""))
    terminal_cmd = str(args.get("terminal_cmd", ""))
    browser_wait_ms = str(args.get("browser_wait_ms", "3000"))
    browser_headless = str(args.get("browser_headless", "true"))

    body_b64 = base64.b64encode(body_text.encode("utf-8")).decode("ascii") if body_text else ""
    browser_script_b64 = (
        base64.b64encode(browser_script.encode("utf-8")).decode("ascii") if browser_script else ""
    )
    terminal_cmd_b64 = (
        base64.b64encode(terminal_cmd.encode("utf-8")).decode("ascii") if terminal_cmd else ""
    )

    dispatch_payload = {
        "ref": os.getenv("CHANNEL_EGRESS_REF", "main"),
        "inputs": {
            "url": url,
            "method": method,
            "body_b64": body_b64,
            "mode": mode,
            "browser_script_b64": browser_script_b64,
            "terminal_cmd_b64": terminal_cmd_b64,
            "browser_wait_ms": browser_wait_ms,
            "browser_headless": browser_headless,
            "request_id": request_id,
        },
    }

    base = f"https://api.github.com/repos/{owner}/{repo}"
    dispatch_url = f"{base}/actions/workflows/{workflow}/dispatches"
    _api_json("POST", dispatch_url, token, dispatch_payload)

    max_wait = int(args.get("max_wait_seconds", os.getenv("CHANNEL_EGRESS_MAX_WAIT", "180")))
    poll_interval = int(args.get("poll_interval_seconds", os.getenv("CHANNEL_EGRESS_POLL_INTERVAL", "3")))

    run_id = ""
    deadline = time.time() + max_wait
    while time.time() < deadline and not run_id:
        runs_url = f"{base}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=20"
        runs = _api_json("GET", runs_url, token)
        for run in runs.get("workflow_runs", []):
            title = (run.get("display_title") or "")
            if request_id in title:
                run_id = str(run.get("id", ""))
                break
        if not run_id:
            time.sleep(max(1, poll_interval))
    if not run_id:
        raise TimeoutError(f"egress workflow run not found for request_id={request_id}")

    conclusion = ""
    while time.time() < deadline:
        run = _api_json("GET", f"{base}/actions/runs/{run_id}", token)
        status = run.get("status", "")
        conclusion = run.get("conclusion", "") or ""
        if status == "completed":
            break
        time.sleep(max(1, poll_interval))
    else:
        raise TimeoutError(f"egress workflow run timeout run_id={run_id}")

    if conclusion != "success":
        raise RuntimeError(f"egress workflow failed run_id={run_id} conclusion={conclusion}")

    release = _api_json("GET", f"{base}/releases/tags/run-{run_id}", token)
    assets = release.get("assets", [])
    if not assets:
        raise RuntimeError(f"no release asset for run-{run_id}")
    asset_url = assets[0].get("browser_download_url", "")
    if not asset_url:
        raise RuntimeError(f"invalid asset url for run-{run_id}")

    import io
    import tarfile

    archive = _download_bytes(asset_url, token)
    if str(args.get("cleanup_release", os.getenv("CHANNEL_EGRESS_AUTO_CLEANUP", "true"))).strip().lower() not in {
        "0",
        "false",
        "no",
    }:
        try:
            _delete_release_and_tag(base, token, run_id)
        except Exception:
            pass
    status_code = "unknown"
    headers_preview = ""
    body_preview = ""
    body_b64_out = ""
    page = {}
    command = {}
    terminal_stdout_preview = ""
    terminal_stderr_preview = ""

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tf:
        members = {m.name: m for m in tf.getmembers()}

        def _member_by_suffix(name: str):
            if name in members:
                return members[name]
            for k, v in members.items():
                if k.endswith("/" + name) or k.endswith(name):
                    return v
            return None

        def _read_text(name: str, limit: int | None = None) -> str:
            member = _member_by_suffix(name)
            if member is None:
                return ""
            f = tf.extractfile(member)
            if f is None:
                return ""
            data = f.read(limit) if isinstance(limit, int) and limit > 0 else f.read()
            return data.decode("utf-8", "replace")

        def _read_json(name: str):
            text = _read_text(name)
            if not text:
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}

        m_status = _member_by_suffix("status_code.txt")
        if m_status is not None:
            status_code = tf.extractfile(m_status).read().decode("utf-8", "replace").strip()

        m_headers = _member_by_suffix("headers.txt")
        if m_headers is not None:
            headers_preview = tf.extractfile(m_headers).read(4096).decode("utf-8", "replace")

        m_body = _member_by_suffix("body.bin")
        if m_body is not None:
            body = tf.extractfile(m_body).read()
            body_preview = body[:1000].decode("utf-8", "replace")
            body_b64_out = base64.b64encode(body[:32768]).decode("ascii")

        page = _read_json("page.json")
        command = _read_json("command.json")
        terminal_stdout_preview = _read_text("terminal_stdout.txt", limit=2000)
        terminal_stderr_preview = _read_text("terminal_stderr.txt", limit=2000)

    return {
        "request_id": request_id,
        "run_id": run_id,
        "url": url,
        "method": method,
        "mode": mode,
        "status_code": status_code,
        "headers_preview": headers_preview,
        "body_preview": body_preview,
        "body_b64_head": body_b64_out,
        "page": page,
        "command": command,
        "terminal_stdout_preview": terminal_stdout_preview,
        "terminal_stderr_preview": terminal_stderr_preview,
        "has_browser_artifacts": mode in {"screenshot", "browser"},
        "has_terminal_artifacts": bool(terminal_cmd),
    }


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
        if command == "egress.fetch":
            return run_github_egress_fetch(args)
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
