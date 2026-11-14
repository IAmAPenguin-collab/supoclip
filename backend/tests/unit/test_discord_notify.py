import httpx
import pytest

from src.services import discord_notify


def test_a_finished_task_links_to_its_page_and_counts_clips():
    payload = discord_notify.build_message(
        task_id="t1",
        app_base_url="http://localhost:3107/",
        title="CS2 FACEIT grind",
        succeeded=True,
        clips_count=20,
        queued_after=2,
        minutes=41.6,
    )
    embed = payload["embeds"][0]
    assert embed["title"] == "Clips ready: CS2 FACEIT grind"
    assert embed["url"] == "http://localhost:3107/tasks/t1"
    assert embed["description"] == "20 clips made."
    assert {"name": "Still in queue", "value": "2", "inline": True} in embed["fields"]
    assert {"name": "Took", "value": "42 min", "inline": True} in embed["fields"]
    assert payload["allowed_mentions"] == {"parse": []}


def test_a_failed_task_shows_the_error_and_an_untitled_task_has_a_name():
    payload = discord_notify.build_message(
        task_id="t2", app_base_url="http://x", title=None, succeeded=False, error="Download failed"
    )
    embed = payload["embeds"][0]
    assert embed["title"] == "Clipping failed: Your video"
    assert embed["description"] == "Download failed"
    assert embed["color"] == discord_notify.COLOR_FAILED
    assert embed["fields"] == []


@pytest.mark.asyncio
async def test_nothing_is_sent_without_a_webhook(monkeypatch):
    monkeypatch.delenv(discord_notify.WEBHOOK_ENV, raising=False)

    def fail(*_args, **_kwargs):
        raise AssertionError("no request expected")

    monkeypatch.setattr(httpx, "AsyncClient", fail)
    await discord_notify.notify_task_finished(
        task_id="t", app_base_url="http://x", title="v", succeeded=True
    )


@pytest.mark.asyncio
async def test_a_webhook_error_never_reaches_the_task(monkeypatch):
    monkeypatch.setenv(discord_notify.WEBHOOK_ENV, "https://discord.invalid/api/webhooks/1/secret")
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise httpx.ConnectError("down", request=request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    await discord_notify.notify_task_finished(
        task_id="t", app_base_url="http://x", title="v", succeeded=True, clips_count=1
    )
    assert len(sent) == 1
    assert sent[0].url.host == "discord.invalid"
