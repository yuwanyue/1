import os
import json
import unittest

from channel_common import Issue, format_response_comment
from controller_cli import enqueue, replay_issue, requeue_issue
from server_worker import (
    FAILURE_PREFIX,
    LABEL_DEAD,
    LABEL_DONE,
    LABEL_PENDING,
    LABEL_PROCESSING,
    LABEL_RETRY,
    claim_issue,
    finalize_issue,
    lease_label,
    process_one_issue,
    run_once,
)


class FakeClient:
    def __init__(self, issue=None, issues=None):
        self.issue = issue
        self.issues = {}
        if issues:
            for item in issues:
                self.issues[item.number] = item
        if issue is not None:
            self.issues[issue.number] = issue
        self.comments = []
        self.created_issues = []
        self.fail_comment = False

    def update_issue(self, issue_number, payload):
        issue = self.issues.get(issue_number)
        if issue is None:
            raise AssertionError("unexpected issue number")
        if "labels" in payload:
            issue.labels = list(payload["labels"])
        if "state" in payload:
            issue.state = payload["state"]
        return {
            "number": issue.number,
            "state": issue.state,
            "labels": [{"name": x} for x in issue.labels],
        }

    def get_issue(self, issue_number):
        issue = self.issues.get(issue_number)
        if issue is None:
            raise AssertionError("unexpected issue number")
        return issue

    def list_comments(self, issue_number):
        if issue_number not in self.issues:
            raise AssertionError("unexpected issue number")
        return list(self.comments)

    def add_comment(self, issue_number, body):
        if issue_number not in self.issues:
            raise AssertionError("unexpected issue number")
        if self.fail_comment:
            raise RuntimeError("comment broken")
        self.comments.append({"body": body})
        return {"body": body}

    def list_issues(self, state="open", per_page=100, labels=None):
        _ = per_page
        result = list(self.issues.values())
        result.extend(
            Issue(
                number=999 + idx,
                title=data["title"],
                body=data["body"],
                labels=data["labels"],
                state="open",
            )
            for idx, data in enumerate(self.created_issues)
        )
        if state != "all":
            result = [issue for issue in result if issue.state == state]
        if labels:
            result = [issue for issue in result if all(label in issue.labels for label in labels)]
        return result

    def list_open_cmd_issues(self, per_page=50):
        return self.list_issues(state="open", per_page=per_page, labels=["channel:cmd", "channel:pending"])

    def create_issue(self, title, body, labels):
        number = 100 + len(self.created_issues)
        self.created_issues.append({"title": title, "body": body, "labels": labels, "number": number})
        self.issues[number] = Issue(number=number, title=title, body=body, labels=labels, state="open")
        return {"number": number, "title": title, "body": body, "labels": labels}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.prev_max_failures = os.environ.get("CHANNEL_MAX_FAILURES")
        os.environ["CHANNEL_MAX_FAILURES"] = "3"

    def tearDown(self):
        if self.prev_max_failures is None:
            os.environ.pop("CHANNEL_MAX_FAILURES", None)
        else:
            os.environ["CHANNEL_MAX_FAILURES"] = self.prev_max_failures

    def make_issue(self, body=None, labels=None):
        return Issue(
            number=1,
            title="[cmd] ping (req1)",
            body=body
            or json.dumps(
                {"version": "v1", "request_id": "req1", "command": "ping", "args": {}}
            ),
            labels=labels or ["channel:cmd", "channel:pending"],
            state="open",
        )

    def test_claim_issue_adds_processing_and_lease(self):
        issue = self.make_issue()
        client = FakeClient(issue)
        claimed = claim_issue(client, issue, "worker-a")
        self.assertIsNotNone(claimed)
        self.assertIn(LABEL_PROCESSING, claimed.labels)
        self.assertNotIn(LABEL_PENDING, claimed.labels)
        self.assertIn(lease_label("worker-a"), claimed.labels)

    def test_finalize_issue_removes_processing_and_closes(self):
        issue = self.make_issue(labels=["channel:cmd", "channel:processing", lease_label("worker-a")])
        client = FakeClient(issue)
        finalize_issue(client, issue.number, "worker-a")
        self.assertEqual(issue.state, "closed")
        self.assertIn(LABEL_DONE, issue.labels)
        self.assertNotIn(LABEL_PROCESSING, issue.labels)
        self.assertNotIn(lease_label("worker-a"), issue.labels)

    def test_process_one_issue_reuses_existing_response(self):
        issue = self.make_issue()
        client = FakeClient(issue)
        client.comments.append(
            {
                "body": format_response_comment(
                    {"version": "v1", "request_id": "req1", "status": "ok", "result": {"pong": True}}
                )
            }
        )
        result = process_one_issue(client, issue, "worker-a")
        self.assertEqual(result["status"], "processed")
        self.assertEqual(len(client.comments), 1)
        self.assertEqual(issue.state, "closed")

    def test_process_one_issue_rolls_back_when_comment_write_fails(self):
        issue = self.make_issue(body=json.dumps({"version": "v1", "request_id": "req1", "command": "echo", "args": {"x": 1}}))
        client = FakeClient(issue)
        client.fail_comment = True
        with self.assertRaises(RuntimeError):
            process_one_issue(client, issue, "worker-a")
        self.assertEqual(issue.state, "open")
        self.assertIn(LABEL_PENDING, issue.labels)
        self.assertIn(LABEL_RETRY, issue.labels)
        self.assertIn(f"{FAILURE_PREFIX}1", issue.labels)

    def test_process_one_issue_moves_to_dead_letter_after_max_failures(self):
        issue = self.make_issue(
            body=json.dumps({"version": "v1", "request_id": "req1", "command": "echo", "args": {"x": 1}}),
            labels=["channel:cmd", "channel:pending", f"{FAILURE_PREFIX}2"],
        )
        client = FakeClient(issue)
        client.fail_comment = True
        with self.assertRaises(RuntimeError) as ctx:
            process_one_issue(client, issue, "worker-a")
        self.assertIn("dead-letter", str(ctx.exception))
        self.assertEqual(issue.state, "closed")
        self.assertIn(LABEL_DEAD, issue.labels)
        self.assertIn(f"{FAILURE_PREFIX}3", issue.labels)
        self.assertNotIn(LABEL_PENDING, issue.labels)

    def test_run_once_returns_structured_stats(self):
        issue = self.make_issue()
        client = FakeClient(issue)
        result = run_once(client, "worker-a")
        self.assertEqual(result["stats"]["seen"], 1)
        self.assertEqual(result["stats"]["processed"], 1)
        self.assertEqual(result["stats"]["errors"], 0)
        self.assertEqual(result["results"][0]["issue"], 1)
        self.assertEqual(result["worker_id"], "worker-a")


class ControllerTests(unittest.TestCase):
    def make_issue(self, issue_number, request_id, command="ping", args=None, state="open"):
        return Issue(
            number=issue_number,
            title=f"[cmd] {command} ({request_id})",
            body=json.dumps(
                {
                    "version": "v1",
                    "request_id": request_id,
                    "command": command,
                    "args": args or {},
                }
            ),
            labels=["channel:cmd", "channel:pending"],
            state=state,
        )

    def test_enqueue_reuses_existing_issue_for_same_request_id(self):
        client = FakeClient(issues=[self.make_issue(7, "req-fixed")])
        rid, issue_no = enqueue(client, "ping", {}, "req-fixed")
        self.assertEqual(rid, "req-fixed")
        self.assertEqual(issue_no, 7)
        self.assertEqual(client.created_issues, [])

    def test_enqueue_rejects_payload_mismatch_for_same_request_id(self):
        client = FakeClient(issues=[self.make_issue(7, "req-fixed", args={"a": 1})])
        with self.assertRaises(ValueError):
            enqueue(client, "ping", {"a": 2}, "req-fixed")

    def test_requeue_issue_reopens_dead_issue(self):
        client = FakeClient(
            issues=[
                self.make_issue(
                    7,
                    "req-fixed",
                    state="closed",
                )
            ]
        )
        client.issues[7].labels = ["channel:cmd", "channel:dead", "channel:done", "channel:failures:3"]
        result = requeue_issue(client, 7)
        self.assertEqual(result["action"], "requeue")
        self.assertEqual(client.issues[7].state, "open")
        self.assertIn(LABEL_PENDING, client.issues[7].labels)
        self.assertNotIn(LABEL_DEAD, client.issues[7].labels)

    def test_replay_issue_creates_new_issue_with_new_request_id(self):
        client = FakeClient(issues=[self.make_issue(7, "req-fixed", command="echo", args={"a": 1}, state="closed")])
        result = replay_issue(client, 7, "req-new")
        self.assertEqual(result["action"], "replay")
        self.assertEqual(result["source_issue_number"], 7)
        self.assertNotEqual(result["issue_number"], 7)
        created = client.issues[result["issue_number"]]
        body = json.loads(created.body)
        self.assertEqual(body["request_id"], "req-new")
        self.assertEqual(body["command"], "echo")

    def test_replay_issue_same_request_id_falls_back_to_requeue(self):
        client = FakeClient(issues=[self.make_issue(7, "req-fixed", state="closed")])
        client.issues[7].labels = ["channel:cmd", "channel:dead", "channel:failures:3"]
        result = replay_issue(client, 7, "req-fixed")
        self.assertEqual(result["action"], "requeue")
        self.assertEqual(client.issues[7].state, "open")


if __name__ == "__main__":
    unittest.main()
