"""Deterministic process, storage and public-viewer operational checks."""

from __future__ import annotations

import os
import signal
import tempfile
import threading
from pathlib import Path
from types import FrameType
from typing import Callable

from issue_graphrag.config import Settings

VIEWER_SECRET_NAMES = (
    "GITHUB_TOKEN",
    "GITHUB_WEBHOOK_SECRET",
    "LLM_API_KEY",
)


def validate_public_viewer(settings: Settings) -> None:
    """Fail closed if a public Viewer can see an execution credential."""
    if not settings.public_radar_only:
        return
    configured = {
        "GITHUB_TOKEN": settings.github_token,
        "GITHUB_WEBHOOK_SECRET": settings.github_webhook_secret,
        "LLM_API_KEY": settings.llm_api_key,
    }
    exposed = sorted(
        name
        for name in VIEWER_SECRET_NAMES
        if configured[name] is not None or os.getenv(f"{name}_FILE")
    )
    if exposed:
        raise ValueError(
            "public Viewer must not receive credentials: " + ", ".join(exposed)
        )


def probe_readable_path(path: Path) -> None:
    candidate = Path(path)
    if not candidate.exists():
        raise OSError(f"required path does not exist: {candidate}")
    if candidate.is_file():
        with candidate.open("rb") as handle:
            handle.read(1)
        return
    next(candidate.iterdir(), None)


def probe_writable_directory(path: Path) -> None:
    """Perform an actual create/fsync/unlink probe, not an os.access guess."""
    directory = Path(path)
    if not directory.is_dir():
        raise OSError(f"writable directory does not exist: {directory}")
    probe: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=".readiness-",
            delete=False,
        ) as handle:
            probe = Path(handle.name)
            handle.write(b"ready\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if probe is not None and probe.exists():
            probe.unlink()


def install_shutdown_handlers(stop_event: threading.Event) -> Callable[[], None]:
    """Translate SIGINT/SIGTERM into one cooperative stop event."""
    previous: dict[signal.Signals, signal.Handlers] = {}

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore
