#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

RESPONSE_SENTINEL = "<!-- channel-response-v1 -->"


class GitHubAPIError(RuntimeError):
    pass


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: List[str]
    state: str


class GitHubQueueClient:
    def __init__(self, token: str, owner: str, repo: str):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.base = f"https://api.github.com/repos/{owner}/{repo}"

    def _req(self, method: str, path: str, data: Optional[Dict[str, Any]] = None):
        url = f"{self.base}{path}"
        raw = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "async-channel-template/1.0",
        }
        if data is not None:
            raw = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=raw, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except Exception as e:
            raise GitHubAPIError(f"GitHub API {method} {path} failed: {e}")

    def list_open_cmd_issues(self, per_page: int = 50) -> List[Issue]:
        # NOTE: GitHub issue search by labels can be eventually consistent right after
        # issue creation. To avoid missing fresh commands, list open issues then filter
        # client-side.
        q = urllib.parse.urlencode(
            {
                "state": "open",
                "sort": "created",
                "direction": "asc",
                "per_page": str(per_page),
            }
        )
        data = self._req("GET", f"/issues?{q}")
        result = []
        for i in data:
            labels = [x.get("name", "") for x in i.get("labels", [])]
            if "channel:cmd" not in labels or "channel:pending" not in labels:
                continue
            result.append(
                Issue(
                    number=i["number"],
                    title=i.get("title", ""),
                    body=i.get("body", "") or "",
                    labels=labels,
                    state=i.get("state", "open"),
                )
            )
        return result

    def create_issue(self, title: str, body: str, labels: List[str]) -> Dict[str, Any]:
        return self._req(
            "POST",
            "/issues",
            {"title": title, "body": body, "labels": labels},
        )

    def add_comment(self, issue_number: int, body: str) -> Dict[str, Any]:
        return self._req("POST", f"/issues/{issue_number}/comments", {"body": body})

    def list_comments(self, issue_number: int) -> List[Dict[str, Any]]:
        return self._req("GET", f"/issues/{issue_number}/comments?per_page=100")

    def update_issue(self, issue_number: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._req("PATCH", f"/issues/{issue_number}", payload)


def parse_cmd_issue_body(body: str) -> Dict[str, Any]:
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid issue body json: {e}")

    required = ["version", "request_id", "command", "args"]
    for k in required:
        if k not in obj:
            raise ValueError(f"missing key: {k}")
    return obj


def format_response_comment(resp_obj: Dict[str, Any]) -> str:
    return f"{RESPONSE_SENTINEL}\n" + json.dumps(resp_obj, ensure_ascii=False)


def extract_response_comments(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for c in comments:
        body = c.get("body", "") or ""
        if RESPONSE_SENTINEL not in body:
            continue
        payload = body.split(RESPONSE_SENTINEL, 1)[1].strip()
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return out


def generate_request_id() -> str:
    return f"req_{int(time.time())}_{os.getpid()}"


def safe_json_arg(s: str) -> Dict[str, Any]:
    try:
        v = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"args must be valid json: {e}")
    if not isinstance(v, dict):
        raise ValueError("args must be a JSON object")
    return v


def sanitize_title(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
