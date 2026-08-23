"""One HTTP boundary for every GitHub request the product makes.

The product's central promise is that it never writes to GitHub. That promise
is only as strong as the narrowest place it can be checked, so the live worker
and the read-only pilot share this session instead of each counting for
itself. It counts every method, and by default refuses a non-GET before the
request reaches the network rather than reporting the write afterwards.
"""

from __future__ import annotations

from typing import Any


class ReadOnlyViolation(RuntimeError):
    """A non-GET request was attempted on a session that forbids writes."""


class CountingSession:
    """Count read and non-GET requests at the HTTP boundary.

    The counts live on the session rather than in a report literal. A later
    POST/PUT/PATCH/DELETE therefore shows up even when a caller forgets to
    update the evaluator.

    ``block_writes`` decides what a non-GET does. The live product refuses it
    before dispatch, so a coding mistake cannot reach GitHub at all. The pilot
    measures instead of refusing: its regression sends a real write and asserts
    the counter reports it, which is what proves the number is measured and not
    a hard-coded zero. Either way the attempt is counted.
    """

    def __init__(self, session: Any, *, block_writes: bool = True):
        self._session = session
        self.block_writes = block_writes
        self.read_count = 0
        self.write_count = 0

    def _count(self, method: str) -> None:
        normalized = method.upper()
        if normalized == "GET":
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

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)
