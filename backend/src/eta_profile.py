"""How fast this machine processes video, for the progress page's time-left estimate.

The frontend's built-in rates were fitted on the hosted service (AssemblyAI
plus a cloud model): about 0.6 s of transcription and 0.4 s of clip finding
per minute of source. Local Whisper and a local model on CPU are 20-80x
slower, which made the estimate promise minutes for runs that took hours.

Each value is seconds per minute of source (or per clip). None means "use the
frontend's default". ETA_* environment variables override the guesses.
"""

from __future__ import annotations

import os
from typing import Optional

# Measured on a 12-core desktop CPU (i9-12900K), 6-hour Twitch VOD, 2026-11.
FASTER_WHISPER_SECONDS_PER_MINUTE = 12.5
OPENAI_WHISPER_SECONDS_PER_MINUTE = 60.0
LOCAL_LLM_FIND_SECONDS_PER_MINUTE = 34.0


def _env_float(name: str) -> Optional[float]:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _transcribe_rate() -> Optional[float]:
    provider = (os.getenv("TRANSCRIPTION_PROVIDER") or "").strip().lower()
    if provider != "whisper":
        return None
    engine = (os.getenv("WHISPER_ENGINE") or "faster").strip().lower()
    if engine in {"openai", "openai-whisper", "openai_whisper"}:
        return OPENAI_WHISPER_SECONDS_PER_MINUTE
    return FASTER_WHISPER_SECONDS_PER_MINUTE


def _find_rate() -> Optional[float]:
    primary = (os.getenv("LLM") or "").strip().lower()
    return LOCAL_LLM_FIND_SECONDS_PER_MINUTE if primary.startswith("ollama:") else None


def eta_profile() -> dict:
    transcribe = _env_float("ETA_TRANSCRIBE_SECONDS_PER_MINUTE")
    find = _env_float("ETA_FIND_SECONDS_PER_MINUTE")
    return {
        "transcribe_seconds_per_minute": transcribe if transcribe is not None else _transcribe_rate(),
        "find_seconds_per_minute": find if find is not None else _find_rate(),
        "render_seconds_per_clip": _env_float("ETA_RENDER_SECONDS_PER_CLIP"),
    }
