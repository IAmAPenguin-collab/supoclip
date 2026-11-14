import asyncio

import pytest

from src import ai


@pytest.fixture
def cloud_first(monkeypatch):
    monkeypatch.setattr(ai, "_PRIMARY_IS_LOCAL", False)
    monkeypatch.setattr(ai, "_LOCAL_MODEL_NAMES", {"gemma4:12b"})
    sent = []

    async def fake_notify(**kwargs):
        sent.append(kwargs)

    from src.services import discord_notify

    monkeypatch.setattr(discord_notify, "notify_fallback", fake_notify)
    return sent


@pytest.mark.asyncio
async def test_answers_are_counted_and_the_first_local_one_alerts_once(cloud_first):
    usage = ai.ModelUsage(task_id="t1", title="VOD")
    ai.model_usage.set(usage)
    for name in ["gemini-3.6-flash", "gemini-3.6-flash", "gemma4:12b", "gemma4:12b"]:
        ai._record_model_answer(name)
    await asyncio.sleep(0)

    assert usage.answered == {"gemini-3.6-flash": 2, "gemma4:12b": 2}
    assert usage.local_answers == 2
    assert usage.summary() == "gemini-3.6-flash ×2 · gemma4:12b ×2"
    assert [alert["model_name"] for alert in cloud_first] == ["gemma4:12b"]
    assert cloud_first[0]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_a_local_first_chain_never_alerts(cloud_first, monkeypatch):
    monkeypatch.setattr(ai, "_PRIMARY_IS_LOCAL", True)
    ai.model_usage.set(ai.ModelUsage(task_id="t2"))
    ai._record_model_answer("gemma4:12b")
    await asyncio.sleep(0)
    assert cloud_first == []


def test_without_a_task_nothing_is_recorded(cloud_first):
    ai.model_usage.set(None)
    ai._record_model_answer("gemma4:12b")
    assert cloud_first == []
