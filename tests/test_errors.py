"""Offline tests for errors.py: what people are shown when something goes wrong."""
import unittest

from errors import BadModelAnswer, UserError, explain


def exc(name, status=None, text=""):
    """An exception shaped like the Anthropic SDK's (matched by class name and status code, so no import is needed)."""
    e = type(name, (Exception,), {})(text)
    e.status_code = status
    return e


class ExplainTests(unittest.TestCase):
    def test_a_user_error_is_shown_as_written(self):
        self.assertEqual(explain(UserError("Do this.", "x")), ("Do this.", "x"))
        self.assertEqual(explain(BadModelAnswer())[1], "bad_answer")
        self.assertIsInstance(BadModelAnswer(), ValueError)   # existing callers that catch ValueError still work

    def test_known_failures_become_plain_instructions(self):
        for ex, code in ((exc("AuthenticationError", 401), "bad_api_key"), (exc("PermissionDeniedError", 403), "forbidden"),
                         (exc("BadRequestError", 400, "Your credit balance is too low"), "no_credit"), (exc("RateLimitError", 429), "rate_limited"),
                         (exc("APIConnectionError"), "offline"), (exc("APITimeoutError"), "offline"), (ConnectionError("x"), "offline"),
                         (exc("InternalServerError", 500), "claude_down"), (exc("APIStatusError", 529), "claude_down"),
                         (exc("BadRequestError", 400, "invalid"), "claude_rejected"), (PermissionError("x"), "file_in_use"),
                         (FileNotFoundError("C:/secret/resume.pdf"), "missing_file")):
            message, got = explain(ex)
            self.assertEqual(got, code, ex)
            self.assertTrue(message.endswith("."), message)
            for leak in ("Error", "Traceback", "secret", "status", "400", "401", "429", "500"):
                self.assertNotIn(leak, message, (code, message))

    def test_anything_else_is_generic_and_logged_with_its_details(self):
        with self.assertLogs("job-finder", level="ERROR") as logs:
            message, code = explain(KeyError("secret_column"))
        self.assertEqual(code, "internal")
        self.assertNotIn("secret_column", message)
        self.assertIn("secret_column", "\n".join(logs.output))   # the developer can still find out what happened


if __name__ == "__main__":
    unittest.main()
