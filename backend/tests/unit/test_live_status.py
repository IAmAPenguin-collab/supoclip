import asyncio
import re

import pytest

from src import live_status


def test_clock_and_size_read_like_a_person_would_say_them():
    assert live_status.clock(38) == "38s"
    assert live_status.clock(245) == "4m 05s"
    assert live_status.clock(4320) == "1h 12m"
    assert live_status.size(1536) == "2 KB"
    assert live_status.size(412 * 1024 * 1024) == "412.0 MB"


@pytest.mark.asyncio
async def test_updates_are_throttled_but_forced_and_new_ones_get_through(monkeypatch):
    sent = []

    async def callback(percent, message, status):
        sent.append((percent, message))

    live_status.install(callback)
    await live_status.say(30, "Transcribing: a", force=True)
    await live_status.say(31, "Transcribing: b")  # too soon after the last one
    await live_status.say(31, "Transcribing: a", force=True)  # same text: skipped
    await live_status.say(32, "Picking clips: c", force=True)
    assert sent == [(30, "Transcribing: a"), (32, "Picking clips: c")]


@pytest.mark.asyncio
async def test_a_worker_thread_reports_in_order(monkeypatch):
    sent = []

    async def callback(percent, message, status):
        sent.append(message)

    live_status.install(callback)
    await asyncio.to_thread(live_status.say_from_thread, 30, "Transcribing: from a thread", force=True)
    assert sent == ["Transcribing: from a thread"]


@pytest.mark.asyncio
async def test_nothing_happens_without_a_reporter():
    live_status.install(None)
    await live_status.say(1, "x", force=True)
    live_status.say_from_thread(1, "x", force=True)


@pytest.mark.asyncio
async def test_a_growing_download_folder_is_reported(tmp_path, monkeypatch):
    sent = []

    async def callback(percent, message, status):
        sent.append(message)

    monkeypatch.setattr(live_status, "MIN_INTERVAL_SECONDS", 0)
    live_status.install(callback)
    async with live_status.watch_download(tmp_path, 10, "Downloading the Twitch VOD's audio", interval=0.05):
        (tmp_path / "source.part").write_bytes(b"x" * 300_000)
        await asyncio.sleep(0.2)
    assert sent, "no progress reported"
    assert re.match(r"Downloading the Twitch VOD's audio — \d+ KB so far · .+/s · \d+s in", sent[-1])
