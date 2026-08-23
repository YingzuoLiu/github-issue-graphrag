"""The read-only boundary is only worth having if it cannot be walked around."""

from __future__ import annotations

import inspect

import pytest
import requests

from issue_graphrag.http_boundary import SAFE_METHODS, CountingSession, ReadOnlyViolation


class RecordingSession:
    """Answers to every dispatch name a caller might reach for."""

    def __init__(self):
        self.calls = []

    def _record(self, name):  # noqa: ANN001, ANN202
        def call(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            self.calls.append((name, args, kwargs))
            return "dispatched"

        return call

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        return self._record(name)


@pytest.mark.parametrize("method", sorted(SAFE_METHODS))
def test_safe_methods_are_counted_as_reads(method):
    session = RecordingSession()
    counter = CountingSession(session)

    getattr(counter, method.lower())("https://api.github.com/repos/o/r")

    assert (counter.read_count, counter.write_count) == (1, 0)
    assert session.calls[0][0] == method.lower()


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_unsafe_methods_are_refused_before_dispatch_and_still_counted(method):
    session = RecordingSession()
    counter = CountingSession(session)

    with pytest.raises(ReadOnlyViolation):
        getattr(counter, method)("https://api.github.com/repos/o/r")

    assert session.calls == []
    assert counter.write_count == 1


def test_generic_request_is_policed_by_its_method_argument():
    session = RecordingSession()
    counter = CountingSession(session)

    with pytest.raises(ReadOnlyViolation):
        counter.request("post", "https://api.github.com/repos/o/r")

    assert session.calls == []
    assert counter.write_count == 1


def test_no_unwrapped_dispatch_path_is_reachable():
    """Anything that can put bytes on the wire must go through the counter.

    Attribute delegation would hand a caller the raw session's ``send``, the
    verb helpers this module does not wrap, and any helper a future
    ``requests`` release adds -- each of them a silent bypass.
    """
    counter = CountingSession(RecordingSession())

    assert not hasattr(counter, "send")
    assert not hasattr(counter, "prepare_request")
    assert not hasattr(counter, "resolve_redirects")
    assert not hasattr(counter, "calls")  # not even the fake's own attributes

    dispatch_names = {
        name
        for name in dir(requests.Session)
        if not name.startswith("_")
        and callable(getattr(requests.Session, name, None))
        and name not in {"close", "get_adapter", "mount", "merge_environment_settings"}
    }
    exposed = {name for name in dispatch_names if hasattr(counter, name)}
    policed = {"request", "get", "head", "options", "post", "put", "patch", "delete"}

    assert exposed == policed, f"unpoliced dispatch surface: {sorted(exposed - policed)}"


def test_every_policed_method_actually_counts():
    """A wrapper that forgot its ``_count`` call would still pass the tests above."""
    counter = CountingSession(RecordingSession(), block_writes=False)

    for name in ("request", "get", "head", "options", "post", "put", "patch", "delete"):
        method = getattr(counter, name)
        source = inspect.getsource(method)
        assert "self._count(" in source, f"{name} does not go through the counter"


def test_close_is_delegated_without_being_counted():
    session = RecordingSession()
    counter = CountingSession(session)

    counter.close()

    assert (counter.read_count, counter.write_count) == (0, 0)
    assert session.calls[0][0] == "close"
