"""Errors an end user can be shown, and the translation of every other exception into a message that is safe and kind to show.

Nothing here (or anywhere that shows an error on the page) passes along an exception's class name, a file path, or a stack
trace. The details of an unexpected error go to the log (the terminal window) instead."""
from __future__ import annotations

import logging

log = logging.getLogger("job-finder")


class UserError(RuntimeError):
    """An error whose message is written for the person using the program: plain words, what happened, and what to do next."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message, self.code = message, code


class BadModelAnswer(UserError, ValueError):
    """Claude's answer came back in a shape the program cannot use."""

    def __init__(self, message: str = "Claude's answer came back incomplete. Please try again.", code: str = "bad_answer"):
        super().__init__(message, code)


# the HTTP status each kind of failure is reported with (anything not listed is a 422: the person can fix it)
STATUS = {"internal": 500, "rate_limited": 429, "offline": 502, "claude_down": 502}


def explain(ex: BaseException) -> tuple[str, str]:
    """(message, code) for any exception: a UserError as written, a known failure from the Anthropic API or the file system
    in plain words, and anything else as a generic message, with the real details sent to the log."""
    if isinstance(ex, UserError):
        return ex.message, ex.code
    status, name, text = getattr(ex, "status_code", None), type(ex).__name__, str(ex).lower()
    if name == "AuthenticationError" or status == 401:
        return "Anthropic did not accept your API key. Check ANTHROPIC_API_KEY in the .env file, then restart the viewer.", "bad_api_key"
    if "credit balance" in text:
        return "Your Anthropic account is out of credit. Add credit in the Anthropic console, then try again.", "no_credit"
    if name == "PermissionDeniedError" or status == 403:
        return "Your Anthropic account is not allowed to do this. Check your account and the permissions of your API key.", "forbidden"
    if name == "RateLimitError" or status == 429:
        return "Claude is getting too many requests right now. Wait a minute and try again.", "rate_limited"
    if name in ("APIConnectionError", "APITimeoutError") or isinstance(ex, (ConnectionError, TimeoutError)):
        return "Could not reach Claude. Check your internet connection and try again.", "offline"
    if status in (500, 502, 503, 504, 529) or name in ("InternalServerError", "OverloadedError", "ServiceUnavailableError"):
        return "Claude is having trouble right now. Try again in a minute.", "claude_down"
    if status == 400:
        return "Claude could not process that request. Try again, and if it keeps happening, check your Anthropic account.", "claude_rejected"
    if isinstance(ex, PermissionError):
        return "Could not save the file. If it is open in another program, close it and try again.", "file_in_use"
    if isinstance(ex, FileNotFoundError):
        return "A file the program needs was not found. Check that resume.pdf and profile.json are in the job-finder folder.", "missing_file"
    log.error("Unexpected error: %s", name, exc_info=ex)
    return "Something went wrong on our side. Try again, and if it keeps happening, look at the terminal window for details.", "internal"
