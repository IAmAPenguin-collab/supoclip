"""Detailed, live progress messages from inside a pipeline stage.

The pipeline reports a handful of stage messages ("Downloading video...",
"Generating transcript..."). Between them a 6-hour VOD can spend an hour in
one stage with nothing new on screen. This module lets the code doing the
work say exactly where it is ("Transcribing ... 1h 12m of 6h 12m") without
threading a callback through every function.

The video pipeline installs a reporter for its task; anything below it calls
``say`` (async code) or ``say_from_thread`` (work running in a thread).
Messages keep each stage's keyword ("download", "transcrib", "picking",
"selected segments", "creating clip n/m") because the progress page reads
the stage from the message.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str, str], Awaitable[None]]

# Each report is a database write and a push to the page; this is plenty.
MIN_INTERVAL_SECONDS = 3.0
THREAD_WAIT_SECONDS = 10.0


class LiveStatus:
    def __init__(self, callback: ProgressCallback, loop: asyncio.AbstractEventLoop):
        self._callback = callback
        self._loop = loop
        self._last_sent = 0.0
        self._last_message = ""

    def _due(self, message: str, force: bool) -> bool:
        if message == self._last_message:
            return False
        now = time.monotonic()
        if not force and now - self._last_sent < MIN_INTERVAL_SECONDS:
            return False
        self._last_sent = now
        self._last_message = message
        return True

    async def say(self, percent: int, message: str, *, force: bool = False) -> None:
        if not self._due(message, force):
            return
        await self._callback(int(percent), message, "processing")

    def say_from_thread(self, percent: int, message: str, *, force: bool = False) -> None:
        """Called from a worker thread; waits for the write so updates stay in order."""
        if not self._due(message, force):
            return
        future = asyncio.run_coroutine_threadsafe(
            self._callback(int(percent), message, "processing"), self._loop
        )
        try:
            future.result(timeout=THREAD_WAIT_SECONDS)
        except Exception as error:
            # Includes TaskCancelled: the pipeline notices that at its next check.
            logger.debug("Live status update skipped (%s)", type(error).__name__)


_current: ContextVar[Optional[LiveStatus]] = ContextVar("live_status", default=None)


def install(callback: Optional[ProgressCallback]) -> None:
    """Route ``say`` calls in this task (and threads it starts) to ``callback``."""
    if callback is None:
        _current.set(None)
        return
    _current.set(LiveStatus(callback, asyncio.get_running_loop()))


async def say(percent: int, message: str, *, force: bool = False) -> None:
    status = _current.get()
    if status is not None:
        try:
            await status.say(percent, message, force=force)
        except Exception as error:
            if type(error).__name__ == "TaskCancelled":
                raise
            logger.debug("Live status update failed (%s)", type(error).__name__)


def say_from_thread(percent: int, message: str, *, force: bool = False) -> None:
    status = _current.get()
    if status is not None:
        status.say_from_thread(percent, message, force=force)


# ---------------------------------------------------------------- formatting


def clock(seconds: Optional[float]) -> str:
    """1h 12m, 4m 05s, 38s."""
    if seconds is None or seconds < 0:
        return "?"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit in ("B", "KB") else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def _tree_bytes(root: Path) -> int:
    total = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


@asynccontextmanager
async def watch_download(root: Optional[Path], percent: int, label: str, interval: float = 3.0):
    """While the body runs, report how much has landed in ``root`` and how fast.

    Downloads run as yt-dlp/ffmpeg subprocesses with no progress hook, but
    their output grows on disk, which is the honest sign they are moving.
    """
    if root is None or _current.get() is None:
        yield
        return
    started = time.monotonic()
    baseline = await asyncio.to_thread(_tree_bytes, root) if root.exists() else 0

    async def poll() -> None:
        last_bytes, last_time = baseline, started
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            current = await asyncio.to_thread(_tree_bytes, root) if root.exists() else baseline
            got = max(0, current - baseline)
            speed = max(0, current - last_bytes) / max(now - last_time, 0.001)
            last_bytes, last_time = current, now
            if got:
                detail = f"{size(got)} so far · {size(speed)}/s"
            else:
                detail = "connecting to the source"
            await say(percent, f"{label} — {detail} · {clock(now - started)} in")

    watcher = asyncio.create_task(poll())
    try:
        yield
    finally:
        watcher.cancel()
        try:
            await watcher
        except (asyncio.CancelledError, Exception):
            pass
