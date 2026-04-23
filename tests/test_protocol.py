import json
import unittest

from channel_common import (
    RESPONSE_SENTINEL,
    extract_response_comments,
    format_response_comment,
    parse_cmd_issue_body,
    safe_json_arg,
)


class ProtocolTests(unittest.TestCase):
    def test_parse_cmd_issue_body(self):
        body = json.dumps(
            {
                "version": "v1",
                "request_id": "req1",
                "command": "ping",
                "args": {},
            }
        )
        obj = parse_cmd_issue_body(body)
        self.assertEqual(obj["command"], "ping")

    def test_parse_cmd_issue_missing(self):
        with self.assertRaises(ValueError):
            parse_cmd_issue_body("{}")

    def test_response_comment_roundtrip(self):
        resp = {"version": "v1", "request_id": "r", "status": "ok", "result": {"x": 1}}
        text = format_response_comment(resp)
        self.assertTrue(text.startswith(RESPONSE_SENTINEL))

        comments = [{"body": text}, {"body": "hello"}]
        got = extract_response_comments(comments)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["request_id"], "r")

    def test_safe_json_arg(self):
        self.assertEqual(safe_json_arg('{"a":1}')["a"], 1)
        with self.assertRaises(ValueError):
            safe_json_arg("[]")


if __name__ == "__main__":
    unittest.main()
