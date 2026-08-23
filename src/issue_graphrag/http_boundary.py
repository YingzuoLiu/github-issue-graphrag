"""One HTTP boundary for every GitHub request the product makes.

The product's central promise is that it never writes to GitHub. That promise
is only as strong as the narrowest place it can be checked, so both production
clients share this session instead of each counting for itself: it counts
every request, and refuses anything but a safe method before the request
reaches the network rather than reporting the write afterwards.

The boundary is closed on purpose. Every dispatch method is wrapped
explicitly and there is no attribute delegation, so a caller cannot reach the
underlying session's ``send``, an unwrapped verb helper, or any verb helper a
future ``requests`` release adds. Reaching a network call that this module has
not accounted for should be an ``AttributeError``, not a silent bypass.
"""

from __future__ import annotations

from typing import Any

#: Methods that cannot modify server state, per HTTP semantics. Everything
#: else counts as a write attempt, whatever the endpoint happens to do.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ReadOnlyViolation(RuntimeError):
    """An unsafe request was attempted on a session that forbids writes."""


class CountingSession:
    """Count requests at the HTTP boundary and refuse the unsafe ones.

    The counts live on the session rather than in a report literal, so a later
    POST/PUT/PATCH/DELETE shows up even when a caller forgets to update the
    evaluator.

    ``block_writes`` defaults to closed: both production clients refuse an
    unsafe request before dispatch, so a coding mistake cannot reach GitHub at
    all. The permissive mode exists for one narrow purpose -- a test that
    dispatches a real write and asserts the counter reports it, which is what
    proves the reported zero is measured rather than hard-coded. No shipped
    client constructs it.
    """

    def __init__(self, session: Any, *, block_writes: bool = True):
        self._session = session
        self.block_writes = block_writes
        self.read_count = 0
        self.write_count = 0

    def _count(self, method: str) -> None:
        normalized = method.upper()
        if normalized in SAFE_METHODS:
            self.read_count += 1
            return
        # Counted before the guard fires. An attempted write is evidence worth
        # keeping even when it never left the process.
        self.write_count += 1
        if self.block_writes:
            raise ReadOnlyViolation(
                f"{normalized} is not allowed on a read-only GitHub session"
            )

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self._count(method)
        return self._session.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        self._count("GET")
        return self._session.get(url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Any:
        self._count("HEAD")
        return self._session.head(url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> Any:
        self._count("OPTIONS")
        return self._session.options(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        self._count("POST")
        return self._session.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        self._count("PUT")
        return self._session.put(url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        self._count("PATCH")
        return self._session.patch(url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        self._count("DELETE")
        return self._session.delete(url, **kwargs)

    def close(self) -> None:
        """Lifecycle, not dispatch: the only call passed straight through."""
        close = getattr(self._session, "close", None)
        if close is not None:
            close()
