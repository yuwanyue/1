#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import uuid4

RESPONSE_SENTINEL = "<!-- channel-response-v1 -->"


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, details: Optional[str] = None):
        self.status = status
        self.details = details or ""
        super().__init__(message)


@dataclass
class Issue:
    number: int
    node_id: str
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
        self.graphql = "https://api.github.com/graphql"

    def _req(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        retries: int = 3,
    ):
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
        last_error = None
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(url, data=raw, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                retry_after = e.headers.get("Retry-After", "").strip()
                detail = self._extract_error_message(body)
                last_error = GitHubAPIError(
                    f"GitHub API {method} {path} failed with HTTP {e.code}: {detail}",
                    status=e.code,
                    details=body,
                )
                if attempt >= retries or not self._should_retry_status(e.code):
                    raise last_error
                delay = self._retry_delay(attempt, retry_after)
                time.sleep(delay)
            except Exception as e:
                last_error = GitHubAPIError(f"GitHub API {method} {path} failed: {e}")
                if attempt >= retries:
                    raise last_error
                time.sleep(self._retry_delay(attempt))
        if last_error is not None:
            raise last_error
        raise GitHubAPIError(f"GitHub API {method} {path} failed for an unknown reason")

    @staticmethod
    def _extract_error_message(body: str) -> str:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body.strip() or "empty response body"
        if isinstance(payload, dict):
            message = payload.get("message", "GitHub API error")
            errors = payload.get("errors")
            if errors:
                return f"{message}; errors={errors}"
            return str(message)
        return str(payload)

    @staticmethod
    def _should_retry_status(status: int) -> bool:
        return status == 429 or 500 <= status < 600

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str = "") -> float:
        if retry_after.isdigit():
            return max(1.0, float(retry_after))
        return min(8.0, float(2 ** (attempt - 1)))

    @staticmethod
    def _issue_from_payload(data: Dict[str, Any]) -> Issue:
        return Issue(
            number=data["number"],
            node_id=data.get("node_id", ""),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            labels=[x.get("name", "") for x in data.get("labels", [])],
            state=data.get("state", "open"),
        )

    def _graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "async-channel-template/1.0",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(self.graphql, data=raw, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                payload = json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            detail = self._extract_error_message(body)
            raise GitHubAPIError(
                f"GitHub GraphQL request failed with HTTP {e.code}: {detail}",
                status=e.code,
                details=body,
            )
        except Exception as e:
            raise GitHubAPIError(f"GitHub GraphQL request failed: {e}")
        if payload.get("errors"):
            raise GitHubAPIError(f"GitHub GraphQL request failed: {payload['errors']}")
        return payload.get("data", {})

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
            result.append(self._issue_from_payload(i))
        return result

    def list_issues(
        self,
        state: str = "open",
        per_page: int = 100,
        labels: Optional[List[str]] = None,
    ) -> List[Issue]:
        params = {
            "state": state,
            "sort": "created",
            "direction": "desc",
            "per_page": str(per_page),
        }
        if labels:
            params["labels"] = ",".join(labels)
        q = urllib.parse.urlencode(params)
        data = self._req("GET", f"/issues?{q}")
        return [self._issue_from_payload(i) for i in data]

    def create_issue(self, title: str, body: str, labels: List[str]) -> Dict[str, Any]:
        return self._req(
            "POST",
            "/issues",
            {"title": title, "body": body, "labels": labels},
        )

    def add_comment(self, issue_number: int, body: str) -> Dict[str, Any]:
        return self._req("POST", f"/issues/{issue_number}/comments", {"body": body})

    def list_comments(self, issue_number: int) -> List[Dict[str, Any]]:
        page = 1
        result = []
        while True:
            batch = self._req("GET", f"/issues/{issue_number}/comments?per_page=100&page={page}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def get_issue(self, issue_number: int) -> Issue:
        data = self._req("GET", f"/issues/{issue_number}")
        return self._issue_from_payload(data)

    def update_issue(self, issue_number: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._req("PATCH", f"/issues/{issue_number}", payload)

    def delete_issue(self, issue_number: int) -> None:
        issue = self.get_issue(issue_number)
        if not issue.node_id:
            raise GitHubAPIError(f"issue #{issue_number} has no node_id for deletion")
        self._graphql(
            "mutation($id:ID!){ deleteIssue(input:{issueId:$id}) { clientMutationId } }",
            {"id": issue.node_id},
        )

    def delete_release(self, release_id: int) -> None:
        self._req("DELETE", f"/releases/{release_id}")

    def delete_tag_ref(self, tag_name: str) -> None:
        encoded = urllib.parse.quote(tag_name, safe="")
        self._req("DELETE", f"/git/refs/tags/{encoded}")


def parse_cmd_issue_body(body: str) -> Dict[str, Any]:
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid issue body json: {e}")

    required = ["version", "request_id", "command", "args"]
    for k in required:
        if k not in obj:
            raise ValueError(f"missing key: {k}")
    if not isinstance(obj["request_id"], str) or not obj["request_id"].strip():
        raise ValueError("request_id must be a non-empty string")
    if not isinstance(obj["command"], str) or not obj["command"].strip():
        raise ValueError("command must be a non-empty string")
    if not isinstance(obj["args"], dict):
        raise ValueError("args must be a JSON object")
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
    return f"req_{int(time.time())}_{uuid4().hex[:10]}"


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


def find_response_for_request_id(comments: List[Dict[str, Any]], request_id: str) -> Optional[Dict[str, Any]]:
    for resp in extract_response_comments(comments):
        if resp.get("request_id") == request_id:
            return resp
    return None
