"""The triggers that keep the transcript mirror (stt/mirror.py) current,
each against a temp mirror folder. Every name here is synthetic.

The batch reconciles the whole folder at the end of every run, an empty
one too, after the descriptions and automatic titles."""
import json
import os
import signal
import subprocess
import sys

import pytest

import run_batch
from stt import config, mirror
from conftest import mfile

WEEKLY = "2026-08-03 Weekly Check-in"
BUDGET = "2026-08-04 Budget Review"


@pytest.fixture
def drive(sandbox, tmp_path_factory):
    d = tmp_path_factory.mktemp("drive")
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    return d


_ids = iter(range(1, 10_000))


def _meeting(base, line="Alex Rivera: the numbers look right."):
    mid = f"{next(_ids):016x}"
    mfile(base, ".json").write_text(json.dumps({
        "meeting_id": mid, "source_file": f"{base}.m4a", "duration_sec": 60.0,
        "strict": False, "date": base[:10], "title": base[11:],
        "speakers": [], "segments": [], "words": []}))
    mfile(base, ".txt").write_text(
        f"{base}  |  1.0 min  |  1 speaker(s)\n\n[00:01] {line}\n")
    mfile(base, ".m4a").write_bytes(b"audio")
    return mid


def _names(d):
    return sorted(os.listdir(d))


# ---------- the batch ----------

def _tone(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:a", "aac", str(path)],
                   check=True)


def _fake_process_file(s, dest_dir=None, **kw):
    """The pipeline's write, minus the models: a meeting folder with its
    json and .txt, named for the source."""
    from pathlib import Path
    base = Path(s).stem
    config.meeting_dir(base, dest_dir).mkdir(parents=True, exist_ok=True)
    mid = config.new_meeting_id()
    j = config.meeting_file(base, ".json", dest_dir)
    j.write_text(json.dumps({"meeting_id": mid, "strict": False, "date": base[:10],
                             "segments": [], "speakers": [], "words": []}))
    config.index_set(mid, base)
    t = config.meeting_file(base, ".txt", dest_dir)
    t.write_text(f"{base}  |  1.0 min  |  1 speaker(s)\n\n[00:01] Jordan Lee: new.\n")
    return {"base": base, "txt": t, "json": j, "emb": None,
            "duration_sec": 60.0, "n_speakers": 1, "identified": []}


def _batch(monkeypatch, *extra, describe=None, order=None):
    from stt import icloud, pipeline, summarize
    monkeypatch.setattr(pipeline, "process_file", _fake_process_file)
    monkeypatch.setattr(icloud, "materialize", lambda p: True)
    monkeypatch.setattr(run_batch, "battery_ok", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", describe or (lambda *a, **k: None))
    monkeypatch.setattr(run_batch, "record_review_metrics", lambda: False)
    monkeypatch.setattr(summarize, "available", lambda: False)
    if order is not None:
        def note(name, ret):
            def f(*a, **k):
                order.append(name)
                return ret
            return f
        monkeypatch.setattr(run_batch, "auto_summarize", note("descriptions", 0))
        monkeypatch.setattr(run_batch, "regenerate_stale", note("stale", 0))
        real = mirror.reconcile

        def spy(bases=None, **k):
            if bases is None:
                order.append("mirror")
            return real(bases, **k)
        monkeypatch.setattr(mirror, "reconcile", spy)
    else:
        monkeypatch.setattr(run_batch, "auto_summarize", lambda *a, **k: 0)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--no-diarize", "--ignore-pause",
                                      "--source", str(config.ICLOUD_DIR), *extra])
    old = signal.getsignal(signal.SIGTERM)
    try:
        return run_batch.main()
    finally:
        signal.signal(signal.SIGTERM, old)


def test_an_empty_batch_run_brings_the_whole_folder_in_line(drive, monkeypatch, capsys):
    """The launch agent starts a run every 10 minutes with nothing to do:
    that run heals a trigger the panel missed."""
    import shutil
    _meeting(WEEKLY)
    _meeting(BUDGET)
    (drive / "2026-07-01 Old Meeting.txt").write_text("not ours")
    assert _batch(monkeypatch) == 0
    assert _names(drive) == ["2026-07-01 Old Meeting.txt", f"{WEEKLY}.txt", f"{BUDGET}.txt"]
    assert "  mirror: 2 written, 0 updated, 0 removed\n" in capsys.readouterr().out
    # nothing changed: no line
    assert _batch(monkeypatch) == 0
    assert "mirror:" not in capsys.readouterr().out
    # a delete that no trigger saw
    shutil.rmtree(config.meeting_dir(BUDGET))
    assert _batch(monkeypatch) == 0
    assert _names(drive) == ["2026-07-01 Old Meeting.txt", f"{WEEKLY}.txt"]
    assert "  mirror: 0 written, 0 updated, 1 removed\n" in capsys.readouterr().out


def test_a_new_meeting_lands_under_its_final_title_after_the_descriptions(
        drive, monkeypatch):
    raw = "2026-08-05 Recording 0915"
    final = "2026-08-05 Staff Huddle"
    _tone(config.ICLOUD_DIR / f"{raw}.m4a")
    order = []

    def describe(key, base=None, track=None):
        # the automatic title renames the folder, as summarize_one does
        order.append("title")
        od = config.meeting_dir(base)
        for f in od.iterdir():
            f.rename(od / (final + f.name[len(base):]))
        od.rename(config.meeting_dir(final))
        if track is not None:
            track["base"] = final
        return True
    assert _batch(monkeypatch, describe=describe, order=order) == 0
    assert _names(drive) == [f"{final}.txt"]
    assert ((drive / f"{final}.txt").read_bytes()
            == config.meeting_file(final, ".txt").read_bytes())
    # the whole-folder pass comes after the descriptions and the titles
    assert order.index("mirror") > max(order.index("title"),
                                       order.index("descriptions"),
                                       order.index("stale"))


def test_a_dry_run_never_mirrors(drive, monkeypatch):
    _meeting(WEEKLY)
    assert _batch(monkeypatch, "--dry-run") == 0
    assert _names(drive) == []


def test_a_run_into_another_folder_never_mirrors(drive, monkeypatch, tmp_path):
    _meeting(WEEKLY)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _batch(monkeypatch, "--dest", str(other)) == 0
    assert _names(drive) == []


def test_a_missing_mirror_folder_never_fails_the_batch(sandbox, monkeypatch, capsys,
                                                       tmp_path_factory):
    d = tmp_path_factory.mktemp("away") / "Meeting Transcripts"
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    _meeting(WEEKLY)
    assert _batch(monkeypatch) == 0
    err = capsys.readouterr().err
    assert err.count("mirror: the mirror folder") == 1
    assert "the next run retries" in err and not d.exists()


def test_a_mirror_that_raises_never_fails_the_batch(drive, monkeypatch, capsys):
    _meeting(WEEKLY)

    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(mirror, "reconcile", boom)
    assert _batch(monkeypatch) == 0
    assert "mirror: not updated (RuntimeError)" in capsys.readouterr().err


def test_each_finished_meeting_is_mirrored_before_the_next_one_starts(drive, monkeypatch):
    """A long backlog: the first meeting's copy lands while the run goes on."""
    first, second = "2026-08-05 Staff Huddle", "2026-08-06 Board Prep"
    _tone(config.ICLOUD_DIR / f"{first}.m4a")
    _tone(config.ICLOUD_DIR / f"{second}.m4a")
    seen = {}

    def describe(key, base=None, track=None):
        if base == second:
            mirror.wait(10)
            seen["first"] = (drive / f"{first}.txt").exists()
            seen["second"] = (drive / f"{second}.txt").exists()
        return True
    assert _batch(monkeypatch, describe=describe) == 0
    assert seen == {"first": True, "second": False}
    assert _names(drive) == [f"{first}.txt", f"{second}.txt"]
