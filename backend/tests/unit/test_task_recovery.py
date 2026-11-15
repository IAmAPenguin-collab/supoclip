"""A video task whose worker died goes back in line instead of blocking the queue."""

import asyncio

import pytest

from src.workers import task_recovery as rec

ARGS = ("task-1", "https://example.com/v", "youtube", "user-1")


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.queue = {}
        self.sets = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def exists(self, key):
        return int(key in self.values)

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)

    async def zrem(self, name, member):
        self.queue.pop(member, None)

    async def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)


@pytest.fixture
def env(monkeypatch):
    calls = {"status": [], "metadata": [], "deferred": [], "progress": []}
    tasks = [("task-1", "user-1", {"processing_job_id": "task-1:old"})]
    job_args = {"task-1:old": ARGS}

    async def processing_tasks(db):
        return tasks

    async def read_args(redis, job_id):
        return job_args.get(job_id)

    async def update_status(db, task_id, status, progress=None, progress_message=None, expected_statuses=None):
        calls["status"].append((task_id, status, progress_message, expected_statuses))
        return True

    async def save_metadata(db, task_id, metadata):
        calls["metadata"].append((task_id, metadata))

    async def defer(db, task_id, args, seconds):
        calls["deferred"].append((task_id, args, seconds))

    class Tracker:
        def __init__(self, redis, task_id):
            self.task_id = task_id

        async def update(self, percent, message, status="processing"):
            calls["progress"].append((self.task_id, status, message))

    monkeypatch.setattr(rec, "_processing_tasks", processing_tasks)
    monkeypatch.setattr(rec, "_job_args", read_args)
    monkeypatch.setattr(rec.TaskRepository, "update_task_status", update_status)
    monkeypatch.setattr(rec.TaskRepository, "save_source_metadata", save_metadata)
    monkeypatch.setattr(rec, "defer_video_job", defer)
    monkeypatch.setattr(rec, "ProgressTracker", Tracker)
    monkeypatch.delenv("VIDEO_TASK_AUTO_RECOVERIES", raising=False)
    monkeypatch.delenv("VIDEO_TASK_HEARTBEAT_TTL_SECONDS", raising=False)
    return calls, tasks, job_args


@pytest.mark.asyncio
async def test_a_task_with_a_live_heartbeat_is_left_alone(env):
    calls, _, _ = env
    redis = FakeRedis()
    redis.values[rec.HEARTBEAT_KEY.format(task_id="task-1")] = "task-1:old"
    assert await rec.recover_interrupted_tasks(None, redis) == {"requeued": [], "failed": []}
    assert calls["status"] == [] and calls["deferred"] == []


@pytest.mark.asyncio
async def test_a_silent_task_is_queued_again_and_its_dead_job_removed(env):
    calls, _, _ = env
    redis = FakeRedis()
    redis.queue["task-1:old"] = 1
    redis.values["arq:in-progress:task-1:old"] = b"1"
    redis.values["arq:job:task-1:old"] = b"job"
    redis.sets["user_video_active:user-1"] = {"task-1"}

    result = await rec.recover_interrupted_tasks(None, redis)

    assert result == {"requeued": ["task-1"], "failed": []}
    task_id, status, message, expected = calls["status"][0]
    assert (task_id, status, expected) == ("task-1", "queued", ["processing"])
    assert "restart 1 of 2" in message
    assert calls["metadata"] == [("task-1", {"auto_recoveries": 1})]
    assert calls["deferred"] == [("task-1", ARGS, 0)]
    assert calls["progress"][0][1] == "queued"
    assert "task-1:old" not in redis.queue
    assert "arq:in-progress:task-1:old" not in redis.values
    assert "arq:job:task-1:old" not in redis.values
    assert redis.sets["user_video_active:user-1"] == set()


@pytest.mark.asyncio
async def test_a_task_that_keeps_killing_the_worker_is_failed(env):
    calls, tasks, _ = env
    tasks[0] = ("task-1", "user-1", {"processing_job_id": "task-1:old", "auto_recoveries": 2})
    result = await rec.recover_interrupted_tasks(None, FakeRedis())
    assert result == {"requeued": [], "failed": ["task-1"]}
    assert calls["status"][0][1] == "error"
    assert "3 times" in calls["status"][0][2]
    assert calls["deferred"] == []


@pytest.mark.asyncio
async def test_a_task_whose_job_is_gone_is_failed_with_a_retry_hint(env):
    calls, _, job_args = env
    job_args.clear()
    result = await rec.recover_interrupted_tasks(None, FakeRedis())
    assert result["failed"] == ["task-1"]
    assert "Press Retry" in calls["status"][0][2]


@pytest.mark.asyncio
async def test_two_sweeps_at_once_recover_a_task_only_once(env):
    calls, _, _ = env
    redis = FakeRedis()
    await asyncio.gather(
        rec.recover_interrupted_tasks(None, redis), rec.recover_interrupted_tasks(None, redis)
    )
    assert len(calls["deferred"]) == 1


@pytest.mark.asyncio
async def test_the_heartbeat_is_kept_after_a_shutdown_and_cleared_after_a_finish():
    redis = FakeRedis()
    key = rec.HEARTBEAT_KEY.format(task_id="task-1")
    beat = await rec.start_heartbeat(redis, "task-1", "task-1:job")
    assert redis.values[key] == "task-1:job"
    await rec.stop_heartbeat(beat, redis, "task-1", clear=False)
    assert key in redis.values
    beat = await rec.start_heartbeat(redis, "task-1", "task-1:job")
    await rec.stop_heartbeat(beat, redis, "task-1", clear=True)
    assert key not in redis.values


def test_settings_have_safe_floors(monkeypatch):
    monkeypatch.setenv("VIDEO_TASK_HEARTBEAT_TTL_SECONDS", "5")
    assert rec.heartbeat_ttl_seconds() == 90
    monkeypatch.setenv("VIDEO_TASK_AUTO_RECOVERIES", "nope")
    assert rec.max_auto_recoveries() == 2
