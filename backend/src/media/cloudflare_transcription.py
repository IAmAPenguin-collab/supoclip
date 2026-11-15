"""Share a long transcription between Cloudflare Workers AI and this machine.

Workers AI runs the same Whisper large-v3-turbo model as the local default.
The free plan gives 10,000 neurons a day and the model costs about 46.63
neurons per audio minute, so roughly 3.5 hours of audio a day are free. There
is no overage on the free plan: past the allowance Cloudflare refuses the
request, it never bills.

The audio is cut into pieces (10 minutes by default). This machine takes
pieces from the start of the queue, Cloudflare takes them from the end, until
they meet. A piece Cloudflare refuses or fails goes back on the queue and is
transcribed here instead, so the result is always complete.

Settings (all optional except the two credentials):
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN  credentials (Workers AI token)
    CLOUDFLARE_TRANSCRIBE=off                    turn the split off
    CLOUDFLARE_DAILY_NEURONS=9500                budget to use per UTC day
    CLOUDFLARE_CHUNK_SECONDS=600                 piece length
    CLOUDFLARE_PARALLEL=3                        pieces sent at once
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from .common import logger

MODEL = "@cf/openai/whisper-large-v3-turbo"
NEURONS_PER_MINUTE = 46.63
API_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
# Cloudflare's error code for "daily free allocation used up".
QUOTA_ERROR_CODES = {4006}


def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default


def enabled() -> bool:
    if (os.getenv("CLOUDFLARE_TRANSCRIBE") or "on").strip().lower() in {"0", "off", "false", "no"}:
        return False
    return bool(os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip() and os.getenv("CLOUDFLARE_API_TOKEN", "").strip())


class QuotaExhausted(Exception):
    """Cloudflare refused the piece because today's allowance is used up."""


class DailyBudget:
    """Minutes of audio sent to Cloudflare per UTC day, kept in a small JSON file.

    Cloudflare resets the free allowance at 00:00 UTC. The file survives worker
    restarts so a second VOD the same day does not spend the allowance twice.
    """

    def __init__(self, path: Path, daily_neurons: int, now: Callable[[], datetime] | None = None):
        self.path = path
        self.daily_minutes = daily_neurons / NEURONS_PER_MINUTE
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        day = data.get(self._today())
        return day if isinstance(day, dict) else {}

    def _write(self, day: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({self._today(): day}), encoding="utf-8")
        except OSError:
            logger.warning("Could not save the Cloudflare usage file %s", self.path, exc_info=True)

    def minutes_left(self) -> float:
        with self._lock:
            day = self._read()
            if day.get("exhausted"):
                return 0.0
            return max(0.0, self.daily_minutes - float(day.get("minutes", 0)))

    def reserve(self, minutes: float) -> bool:
        with self._lock:
            day = self._read()
            used = float(day.get("minutes", 0))
            if day.get("exhausted") or used + minutes > self.daily_minutes:
                return False
            day["minutes"] = used + minutes
            self._write(day)
            return True

    def refund(self, minutes: float) -> None:
        with self._lock:
            day = self._read()
            day["minutes"] = max(0.0, float(day.get("minutes", 0)) - minutes)
            self._write(day)

    def mark_exhausted(self) -> None:
        with self._lock:
            day = self._read()
            day["exhausted"] = True
            self._write(day)


def default_budget() -> DailyBudget:
    from ..config import get_config

    path = Path(os.getenv("CLOUDFLARE_USAGE_FILE") or Path(get_config().temp_dir) / "cloudflare_ai_usage.json")
    return DailyBudget(path, _env_int("CLOUDFLARE_DAILY_NEURONS", 9500, 0))


def transcribe_piece(
    audio: bytes, language: Optional[str], client: httpx.Client
) -> Dict[str, Any]:
    """One piece through Workers AI. Times in the result are seconds from the piece start."""
    url = API_URL.format(account=os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(), model=MODEL)
    payload: Dict[str, Any] = {
        "audio": base64.b64encode(audio).decode("ascii"),
        "vad_filter": True,
        # Matches the local run: stops one hallucinated line repeating through music.
        "condition_on_previous_text": False,
    }
    if language:
        payload["language"] = language
    response = client.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {os.getenv('CLOUDFLARE_API_TOKEN', '').strip()}"},
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    codes = {e.get("code") for e in body.get("errors") or [] if isinstance(e, dict)}
    if response.status_code == 429 or codes & QUOTA_ERROR_CODES:
        raise QuotaExhausted(f"HTTP {response.status_code} {sorted(c for c in codes if c)}")
    if response.status_code >= 400 or not body.get("success", False):
        raise RuntimeError(f"Workers AI HTTP {response.status_code} errors={sorted(c for c in codes if c)}")
    result = body.get("result") or {}
    segments = []
    for seg in result.get("segments") or []:
        segments.append(
            {
                "start": float(seg.get("start", 0)),
                "end": float(seg.get("end", 0)),
                "text": seg.get("text", ""),
                "words": [
                    {
                        "word": w.get("word", ""),
                        "start": float(w.get("start", 0)),
                        "end": float(w.get("end", 0)),
                        "probability": 1.0,
                    }
                    for w in seg.get("words") or []
                ],
            }
        )
    info = result.get("transcription_info") or {}
    return {"segments": segments, "language": info.get("language") or language}


@dataclass
class Piece:
    index: int
    start: float
    length: float


def plan_pieces(duration: float, chunk_seconds: float) -> List[Piece]:
    pieces, start, index = [], 0.0, 0
    while start < duration - 0.5:
        length = min(chunk_seconds, duration - start)
        pieces.append(Piece(index, start, length))
        start += length
        index += 1
    return pieces


def cut_piece(audio_path: Path, piece: Piece, folder: Path) -> Path:
    out = folder / f"piece_{piece.index:04d}.mp3"
    if not out.exists():
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{piece.start:.3f}", "-t", f"{piece.length:.3f}",
                "-i", str(audio_path), "-c", "copy", str(out),
            ],
            check=True,
            timeout=300,
        )
    return out


def merge(results: Dict[int, Dict[str, Any]], pieces: List[Piece]) -> Dict[str, Any]:
    """Join pieces in order, moving each piece's times to its place in the audio."""
    segments: List[Dict[str, Any]] = []
    language = None
    for piece in pieces:
        result = results[piece.index]
        language = language or result.get("language")
        for seg in result.get("segments") or []:
            segments.append(
                {
                    **seg,
                    "start": seg["start"] + piece.start,
                    "end": seg["end"] + piece.start,
                    "words": [
                        {**w, "start": w["start"] + piece.start, "end": w["end"] + piece.start}
                        for w in seg.get("words") or []
                    ],
                }
            )
    return {"text": "".join(s["text"] for s in segments), "segments": segments, "language": language}


def transcribe_split(
    audio_path: Path,
    duration: float,
    language: Optional[str],
    local: Callable[[Path], Dict[str, Any]],
    *,
    budget: DailyBudget | None = None,
    cloud: Callable[[bytes], Dict[str, Any]] | None = None,
    cut: Callable[[Path, Piece, Path], Path] = cut_piece,
    report: Callable[[str, float], None] | None = None,
) -> Dict[str, Any]:
    """Transcribe ``audio_path`` with this machine and Cloudflare at the same time.

    ``local(piece_path)`` returns a faster-whisper-shaped result for one piece.
    """
    budget = budget or default_budget()
    pieces = plan_pieces(duration, float(_env_int("CLOUDFLARE_CHUNK_SECONDS", 600, 60)))
    folder = audio_path.with_name(f"{audio_path.stem}.pieces")
    folder.mkdir(parents=True, exist_ok=True)

    queue = deque(pieces)
    results: Dict[int, Dict[str, Any]] = {}
    done_by = {"cloud": 0, "local": 0}
    state = {"in_flight": 0, "cloud_open": True, "error": None}
    cond = threading.Condition()
    started = time.monotonic()

    client = None
    if cloud is None:
        client = httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0))
        cloud = lambda data: transcribe_piece(data, language, client)  # noqa: E731

    def say() -> None:
        if report is None:
            return
        left = sum(p.length for p in pieces if p.index not in results)
        report(
            f"Transcribing on Cloudflare and this PC: {len(results)} of {len(pieces)} pieces done "
            f"(Cloudflare {done_by['cloud']} · this PC {done_by['local']}) · "
            f"{_clock(left)} of audio left · {_clock(time.monotonic() - started)} in",
            len(results) / max(1, len(pieces)),
        )

    def cloud_worker() -> None:
        failures = 0
        while True:
            with cond:
                if not state["cloud_open"] or not queue:
                    return
                piece = queue[-1]
                minutes = piece.length / 60
                if not budget.reserve(minutes):
                    state["cloud_open"] = False
                    logger.info("Cloudflare allowance for today is used up; the rest runs on this PC")
                    return
                queue.pop()
                state["in_flight"] += 1
            try:
                result = cloud(cut(audio_path, piece, folder).read_bytes())
            except QuotaExhausted as exc:
                budget.refund(minutes)
                budget.mark_exhausted()
                logger.info("Cloudflare refused a piece (%s); the rest runs on this PC", exc)
                with cond:
                    queue.append(piece)
                    state["cloud_open"] = False
                    state["in_flight"] -= 1
                    cond.notify_all()
                return
            except Exception as exc:
                budget.refund(minutes)
                failures += 1
                logger.warning("Cloudflare piece %d failed (%s); this PC will do it", piece.index, exc)
                with cond:
                    queue.append(piece)
                    state["in_flight"] -= 1
                    if failures >= 2:
                        state["cloud_open"] = False
                    cond.notify_all()
                if failures >= 2:
                    return
                continue
            failures = 0
            with cond:
                results[piece.index] = result
                done_by["cloud"] += 1
                state["in_flight"] -= 1
                cond.notify_all()
            say()

    threads = [
        threading.Thread(target=cloud_worker, name=f"cloudflare-{i}", daemon=True)
        for i in range(_env_int("CLOUDFLARE_PARALLEL", 3, 1))
    ]
    if budget.minutes_left() > 0:
        for thread in threads:
            thread.start()
    else:
        state["cloud_open"] = False
        threads = []

    try:
        say()
        while True:
            with cond:
                while not queue and state["in_flight"] > 0:
                    cond.wait(timeout=5)
                if not queue:
                    break
                piece = queue.popleft()
            result = local(cut(audio_path, piece, folder))
            with cond:
                results[piece.index] = result
                done_by["local"] += 1
            say()
        for thread in threads:
            thread.join(timeout=5)
    finally:
        if client is not None:
            client.close()
        shutil.rmtree(folder, ignore_errors=True)

    logger.info(
        "Split transcription finished in %.1f min: %d pieces on Cloudflare, %d on this PC",
        (time.monotonic() - started) / 60,
        done_by["cloud"],
        done_by["local"],
    )
    merged = merge(results, pieces)
    merged["pieces"] = dict(done_by)
    return merged


def _clock(seconds: float) -> str:
    from .. import live_status

    return live_status.clock(seconds)
