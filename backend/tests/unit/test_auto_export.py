from datetime import datetime

from src.services import auto_export


def _clip(tmp_path, order, hook, name):
    path = tmp_path / "render" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"mp4-" + name.encode())
    return {
        "file_path": str(path),
        "clip_order": order,
        "hook_title": hook,
        "text": "he aced the whole team",
        "virality_score": 81,
        "start_time": "12:04",
        "end_time": "12:40",
    }


def test_clips_land_in_one_dated_folder_in_clip_order_with_captions(tmp_path):
    clips = [
        _clip(tmp_path, 2, "Second", "b.mp4"),
        _clip(tmp_path, 1, 'He said WHAT?! <no way>', "a.mp4"),
    ]
    folder, saved = auto_export.export_task_clips(
        root=tmp_path / "out",
        task_id="abcdef1234",
        title="CS2: FACEIT | rage",
        clips=clips,
        now=datetime(2026, 11, 14),
    )
    assert saved == 2
    assert folder.name == "2026-11-14 CS2 FACEIT rage"
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["01 He said WHAT! no way.mp4", "01 He said WHAT! no way.txt", "02 Second.mp4", "02 Second.txt"]
    assert (folder / "01 He said WHAT! no way.mp4").read_bytes() == b"mp4-a.mp4"
    caption = (folder / "02 Second.txt").read_text(encoding="utf-8")
    assert caption.startswith("Second\n\nhe aced the whole team\n")
    assert "Virality score: 81" in caption


def test_a_second_export_of_the_same_title_gets_its_own_folder(tmp_path):
    clips = [_clip(tmp_path, 1, "Hook", "a.mp4")]
    first, _ = auto_export.export_task_clips(root=tmp_path, task_id="t", title="VOD", clips=clips, now=datetime(2026, 1, 1))
    second, _ = auto_export.export_task_clips(root=tmp_path, task_id="t", title="VOD", clips=clips, now=datetime(2026, 1, 1))
    assert first != second
    assert second.name == "2026-01-01 VOD (2)"


def test_missing_files_are_skipped_and_no_clips_means_no_folder(tmp_path):
    missing = {"file_path": str(tmp_path / "gone.mp4"), "clip_order": 1, "hook_title": "x"}
    folder, saved = auto_export.export_task_clips(root=tmp_path / "out", task_id="t", title=None, clips=[missing])
    assert saved == 0
    assert folder is not None and list(folder.iterdir()) == []
    assert auto_export.export_task_clips(root=tmp_path / "o2", task_id="t", title="x", clips=[]) == (None, 0)


def test_export_is_off_without_a_directory(monkeypatch):
    monkeypatch.delenv(auto_export.EXPORT_DIR_ENV, raising=False)
    assert auto_export.export_dir() is None
