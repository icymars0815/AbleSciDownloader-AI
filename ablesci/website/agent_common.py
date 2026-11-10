"""Names shared by browser_agent and its mixin modules (refactor stage 3)."""


BASE = "https://www.ablesci.com"


def _count(locator):
    """Playwright count() as an int (mock pages may return non-ints, treated as 0)."""
    try:
        n = locator.count()
    except Exception:
        return 0
    return n if isinstance(n, int) else 0


class SessionExpired(ValueError):
    """The 科研通 session is gone (redirected to the login page)."""


class PreconditionError(ValueError):
    """A command refused by its own precondition: it did nothing, so the item's status must not change."""


class TransientError(ValueError):
    """A temporary network/site problem (timeout, busy site). Never pauses the pipeline; the monitor backs off."""


class RetryScheduled(ValueError):
    """A download did not finish but will be retried automatically; the item must NOT go to attention."""


OPEN_TIMEOUT_TEXT = "科研通页面在 45 秒内未能打开，可能较为繁忙，请稍后重试"


def is_transient(exc):
    """Network-ish failures that say nothing about the account or the workflow."""
    if isinstance(exc, (TransientError, TimeoutError, ConnectionError)):
        return True
    if any(c.__name__ == "TimeoutError" for c in type(exc).__mro__):   # playwright's TimeoutError
        return True
    if any(c.__name__ == "TargetClosedError" for c in type(exc).__mro__):   # browser was closed (idle close / crash): reopen
        return True
    text = str(exc)
    # the page navigated or re-rendered under us (real 2026-11-10: right after the high-speed confirm the download
    # page changed and the progress read failed; the next attempt is expected to work)
    return any(s in text for s in ("net::ERR_", "has been closed", "Execution context was destroyed",
                                   "most likely because of a navigation", "Frame was detached"))
