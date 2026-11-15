"""Splitting a transcription between Cloudflare Workers AI and this machine."""

import json
import threading
from datetime import datetime, timezone

import httpx
import pytest

from src.media import cloudflare_transcription as cf


def _budget(tmp_path, neurons=9500, day="2026-11-15"):
    now = lambda: datetime.fromisoformat(f"{day}T12:00:00+00:00")  # noqa: E731
    return cf.DailyBudget(tmp_path / "usage.json", neurons, now=now)


def _cut(audio_path, piece, folder):
    path = folder / f"piece_{piece.index}.mp3"
    path.write_bytes(str(piece.index).encode())
    return path


def _result(label):
    return {
        "segments": [
            {"start": 1.0, "end": 2.0, "text": f" {label}",
             "words": [{"word": label, "start": 1.0, "end": 2.0, "probability": 1.0}]}
        ],
        "language": "en",
    }


def _local(path):
    return _result(f"local{path.read_bytes().decode()}")


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_CHUNK_SECONDS", "600")
    monkeypatch.setenv("CLOUDFLARE_PARALLEL", "2")


def test_pieces_cover_the_audio_exactly():
    pieces = cf.plan_pieces(1500, 600)
    assert [(p.start, p.length) for p in pieces] == [(0, 600), (600, 600), (1200, 300)]


def test_enabled_needs_both_credentials_and_can_be_switched_off(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    assert cf.enabled() is False
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    assert cf.enabled() is True
    monkeypatch.setenv("CLOUDFLARE_TRANSCRIBE", "off")
    assert cf.enabled() is False


def test_the_budget_resets_each_utc_day_and_stops_at_the_limit(tmp_path):
    budget = _budget(tmp_path, neurons=1000)  # about 21 minutes
    assert budget.reserve(20) is True
    assert budget.reserve(5) is False
    budget.refund(20)
    assert budget.reserve(20) is True
    budget.mark_exhausted()
    assert budget.minutes_left() == 0
    assert _budget(tmp_path, neurons=1000, day="2026-11-16").minutes_left() == pytest.approx(1000 / 46.63)


def test_pieces_are_shared_and_merged_in_order_with_real_times(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    cloud_calls = []

    def cloud(data):
        cloud_calls.append(data)
        return _result(f"cloud{data.decode()}")

    out = cf.transcribe_split(audio, 3600, "en", _local, budget=_budget(tmp_path), cloud=cloud, cut=_cut)

    starts = [s["start"] for s in out["segments"]]
    assert starts == [1.0 + 600 * i for i in range(6)]
    assert out["segments"][3]["words"][0]["start"] == 1801.0
    assert out["pieces"]["cloud"] + out["pieces"]["local"] == 6
    assert out["pieces"]["cloud"] >= 1
    # Cloudflare works from the end, this PC from the start.
    assert out["segments"][0]["text"] == " local0"
    assert out["segments"][-1]["text"].startswith(" cloud")
    assert not (tmp_path / "a.pieces").exists()


def test_a_refused_piece_is_done_here_and_the_day_is_marked_used_up(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    budget = _budget(tmp_path)

    def cloud(data):
        raise cf.QuotaExhausted("HTTP 429")

    out = cf.transcribe_split(audio, 1800, None, _local, budget=budget, cloud=cloud, cut=_cut)
    assert out["pieces"] == {"cloud": 0, "local": 3}
    assert [s["text"] for s in out["segments"]] == [" local0", " local1", " local2"]
    assert budget.minutes_left() == 0


def test_cloud_errors_fall_back_to_this_pc_and_refund_the_minutes(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    budget = _budget(tmp_path)

    def cloud(data):
        raise RuntimeError("Workers AI HTTP 500")

    out = cf.transcribe_split(audio, 1800, None, _local, budget=budget, cloud=cloud, cut=_cut)
    assert out["pieces"]["local"] == 3
    assert budget.minutes_left() == pytest.approx(9500 / 46.63)


def test_no_allowance_left_runs_everything_here(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    budget = _budget(tmp_path)
    budget.mark_exhausted()
    called = threading.Event()
    out = cf.transcribe_split(
        audio, 1200, None, _local, budget=budget, cloud=lambda d: called.set(), cut=_cut
    )
    assert out["pieces"] == {"cloud": 0, "local": 2}
    assert not called.is_set()


def _response(status, body):
    return httpx.Response(status, content=json.dumps(body).encode(),
                          request=httpx.Request("POST", "https://api.cloudflare.com/x"))


def test_the_api_reply_is_read_and_quota_errors_are_recognised(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    sent = {}

    def handler(request):
        sent["url"] = str(request.url)
        sent["auth"] = request.headers["authorization"]
        sent["body"] = json.loads(request.content)
        return _response(200, {"success": True, "result": {
            "transcription_info": {"language": "en"},
            "segments": [{"start": 0.5, "end": 1.5, "text": " hi",
                          "words": [{"word": "hi", "start": 0.5, "end": 1.5}]}]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = cf.transcribe_piece(b"abc", "en", client)
    assert sent["url"].endswith("/accounts/acct/ai/run/@cf/openai/whisper-large-v3-turbo")
    assert sent["auth"] == "Bearer tok"
    assert sent["body"]["audio"] == "YWJj" and sent["body"]["vad_filter"] is True
    assert out["segments"][0]["words"][0] == {"word": "hi", "start": 0.5, "end": 1.5, "probability": 1.0}

    quota = httpx.Client(transport=httpx.MockTransport(
        lambda r: _response(400, {"success": False, "errors": [{"code": 4006, "message": "daily"}]})))
    with pytest.raises(cf.QuotaExhausted):
        cf.transcribe_piece(b"abc", None, quota)
    broken = httpx.Client(transport=httpx.MockTransport(lambda r: _response(500, {"success": False})))
    with pytest.raises(RuntimeError):
        cf.transcribe_piece(b"abc", None, broken)
