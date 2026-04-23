import json
import unittest

from channel_common import Issue, format_response_comment
from server_worker import (
    LABEL_DONE,
    LABEL_PENDING,
    LABEL_PROCESSING,
    LABEL_RETRY,
    claim_issue,
    finalize_issue,
    lease_label,
    process_one_issue,
)


class FakeClient:
    def __init__(self, issue=None):
        self.issue = issue
        self.comments = []
        self.created_issues = []
        self.fail_comment = False

    def update_issue(self, issue_number, payload):
        if issue_number != self.issue.number:
            raise AssertionError("unexpected issue number")
        if "labels" in payload:
            self.issue.labels = list(payload["labels"])
        if "state" in payload:
            self.issue.state = payload["state"]
        return {
            "number": self.issue.number,
            "state": self.issue.state,
            "labels": [{"name": x} for x in self.issue.labels],
        }

    def get_issue(self, issue_number):
        if issue_number != self.issue.number:
            raise AssertionError("unexpected issue number")
        return self.issue

    def list_comments(self, issue_number):
        if issue_number != self.issue.number:
            raise AssertionError("unexpected issue number")
        return list(self.comments)

    def add_comment(self, issue_number, body):
        if issue_number != self.issue.number:
            raise AssertionError("unexpected issue number")
        if self.fail_comment:
            raise RuntimeError("comment broken")
        self.comments.append({"body": body})
        return {"body": body}

    def list_issues(self, state="open", per_page=100, labels=None):
        _ = (state, per_page, labels)
        return [
            Issue(
                number=999,
                title=data["title"],
                body=data["body"],
                labels=data["labels"],
                state="open",
            )
            for data in self.created_issues
        ]

    def create_issue(self, title, body, labels):
        self.created_issues.append({"title": title, "body": body, "labels": labels})
        return {"title": title, "body": body, "labels": labels}


class WorkerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
