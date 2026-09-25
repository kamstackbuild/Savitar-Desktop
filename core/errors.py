"""Error classification (rules 21 & 22).

Five failure classes, distinct codes. Only TEMPORARY_FAILURE is client-retryable.
Raw yt-dlp stderr is kept on the exception's ``debug`` field for server-side logging
only — it is NEVER placed in ``user_message``.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    UNSUPPORTED_SITE = "UNSUPPORTED_SITE"
    EXTRACTOR_FAILED = "EXTRACTOR_FAILED"
    PRIVATE_OR_LOGIN_REQUIRED = "PRIVATE_OR_LOGIN_REQUIRED"
    NOT_FOUND_OR_DELETED = "NOT_FOUND_OR_DELETED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"


# Short, user-facing messages. Never leak stderr into these.
_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.UNSUPPORTED_SITE: "This link isn't supported.",
    # Distinct from UNSUPPORTED_SITE on purpose: the site *is* supported, the
    # engine just could not read this page. Saying "not supported" here sent
    # people looking for another app when an engine update was the fix.
    ErrorCode.EXTRACTOR_FAILED: "The engine couldn't read this post. Open Settings → Engine and update yt-dlp, then try again — sites change often and the newest engine usually fixes it.",
    ErrorCode.PRIVATE_OR_LOGIN_REQUIRED: "This video is private or age-restricted. Go to Settings → Privacy & Cookies, import your signed-in cookies.txt file or select your browser (e.g. Firefox), and try again. For Instagram, you can also try the SaveVid button below.",
    ErrorCode.NOT_FOUND_OR_DELETED: "This video was not found. It may have been deleted.",
    ErrorCode.TEMPORARY_FAILURE: "Something went wrong. Please try again in a moment.",
}


class ExtractError(Exception):
    """Extraction failure carrying a user-safe message + server-only debug text."""

    def __init__(
        self,
        code: ErrorCode,
        user_message: str | None = None,
        *,
        debug: str | None = None,
    ) -> None:
        self.code = code
        self.user_message = user_message or _MESSAGES[code]
        self.debug = debug  # raw stderr / internal detail — log only, never return
        super().__init__(f"{code.value}: {self.user_message}")


# Substring patterns matched against LOWERCASED stderr, most specific class first.
# Ordered: login/private -> not-found/deleted -> unsupported -> temporary.
_PRIVATE_PATTERNS = (
    "private video",
    "this video is private",
    "video is private",
    "login required",
    "requires authentication",
    "you must be logged in",
    "log in",
    "sign in to confirm",
    "sign in to view",
    "account is private",
    "followers only",
    "only available to registered",
    "age-restricted",
    "confirm your age",
    "use --cookies",
)
_NOT_FOUND_PATTERNS = (
    "video unavailable",
    "this video has been removed",
    "has been deleted",
    "no longer available",
    "not found",
    "404",
    "does not exist",
    "content isn't available",
    "content is no longer available",
    "page not found",
    "post not found",
    "unable to find video",
    "the tweet is unavailable",
    "this post is unavailable",
)
_UNSUPPORTED_PATTERNS = (
    "unsupported url",
    "no video could be found",
    "there is no video",
    "is not a valid url",
)
# The extractor ran but could not read the page. This is the everyday TikTok /
# Instagram failure mode after a site redesign, and it is fixed by a newer
# yt-dlp — not by a different app, which is what "unsupported" implied.
_EXTRACTOR_FAILED_PATTERNS = (
    "unable to extract",
    "unable to find pattern",
    "failed to parse json",
    "no video formats found",
    "please report this issue",
)
_TEMPORARY_PATTERNS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "http error 5",
    "http error 429",
    "too many requests",
    "rate-limit",
    "rate limit",
    "unable to download webpage",
    "connection",
    "network",
    "getaddrinfo",
    "name or service not known",
    "read timed out",
)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(p in text for p in patterns)


def classify_stderr(stderr: str | None) -> tuple[ErrorCode, str]:
    """Map yt-dlp stderr to (ErrorCode, user_message).

    Unknown output defaults to TEMPORARY_FAILURE — we can't prove it permanent,
    and only that code invites a client retry (rule 22).
    """
    text = (stderr or "").lower()
    if _matches(text, _PRIVATE_PATTERNS):
        code = ErrorCode.PRIVATE_OR_LOGIN_REQUIRED
    elif _matches(text, _NOT_FOUND_PATTERNS):
        code = ErrorCode.NOT_FOUND_OR_DELETED
    elif _matches(text, _UNSUPPORTED_PATTERNS):
        code = ErrorCode.UNSUPPORTED_SITE
    elif _matches(text, _EXTRACTOR_FAILED_PATTERNS):
        code = ErrorCode.EXTRACTOR_FAILED
    elif _matches(text, _TEMPORARY_PATTERNS):
        code = ErrorCode.TEMPORARY_FAILURE
    else:
        code = ErrorCode.TEMPORARY_FAILURE
    return code, _MESSAGES[code]
