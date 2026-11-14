"""Copy a finished task's clips into a folder on the host, ready to post.

AUTO_EXPORT_DIR is a path inside the worker container (docker-compose binds
it to AUTO_EXPORT_HOST_DIR on the host). Unset means off. Each task gets one
folder named after its date and source title; each clip is saved as
"NN Hook title.mp4" with a .txt beside it holding the hook title, the clip's
transcript and its virality score, for writing the post.

The rendered clips are already vertical H.264 MP4s, which TikTok, YouTube
Shorts and Instagram Reels all accept, so they are copied, not re-encoded.
Export problems are logged and never affect the task.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

EXPORT_DIR_ENV = "AUTO_EXPORT_DIR"
MAX_NAME_CHARS = 80
# Windows forbids these in file names; the folder is usually on a Windows host.
# Separators become spaces ("CS2: FACEIT" -> "CS2 FACEIT"); the rest go away
# ("WHAT?!" -> "WHAT!").
_SEPARATORS = re.compile(r'[:/\\|\x00-\x1f]+')
_DROPPED = re.compile(r'[<>"?*]+')


def export_dir() -> Optional[Path]:
    value = os.getenv(EXPORT_DIR_ENV, "").strip()
    return Path(value) if value else None


def safe_name(text: Optional[str], fallback: str) -> str:
    cleaned = _DROPPED.sub("", _SEPARATORS.sub(" ", text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:MAX_NAME_CHARS].rstrip(" .")) or fallback


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for number in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def _caption(clip: dict[str, Any]) -> str:
    lines = []
    if clip.get("hook_title"):
        lines += [str(clip["hook_title"]).strip(), ""]
    if clip.get("text"):
        lines += [str(clip["text"]).strip(), ""]
    lines.append(f"Virality score: {clip.get('virality_score') or 0}")
    lines.append(f"Source time: {clip.get('start_time', '?')} - {clip.get('end_time', '?')}")
    return "\n".join(lines) + "\n"


def export_task_clips(
    *,
    root: Path,
    task_id: str,
    title: Optional[str],
    clips: Iterable[dict[str, Any]],
    now: Optional[datetime] = None,
) -> tuple[Optional[Path], int]:
    """Copy the clips; returns the task folder and how many clips were saved."""
    ordered = sorted(clips, key=lambda clip: clip.get("clip_order") or 0)
    if not ordered:
        return None, 0
    stamp = (now or datetime.now()).strftime("%Y-%m-%d")
    folder = _unique(root / f"{stamp} {safe_name(title, task_id[:8])}")
    folder.mkdir(parents=True, exist_ok=True)
    saved = 0
    for position, clip in enumerate(ordered, start=1):
        source = Path(str(clip.get("file_path") or ""))
        if not source.is_file():
            logger.warning("Auto-export: clip file missing for task %s: %s", task_id, source.name)
            continue
        name = safe_name(clip.get("hook_title") or (clip.get("text") or "")[:60], f"clip {position}")
        target = _unique(folder / f"{position:02d} {name}{source.suffix or '.mp4'}")
        shutil.copy2(source, target)
        target.with_suffix(".txt").write_text(_caption(clip), encoding="utf-8")
        saved += 1
    return folder, saved
