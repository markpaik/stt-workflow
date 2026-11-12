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


# ---------- speaker naming, relabel, merge, line edits ----------
#
# Every naming, speaker rename, merge, sample edit and relabel pass ends in
# relabel.relabel_one, and every line edit, insert, delete, split and
# accept ends in review._rewrite. Both hand the meeting to mirror.touch.

from tests.test_relabel_targets import _meeting as _cached_meeting  # noqa: E402
from tests.test_relabel_targets import _post, _unit  # noqa: E402


@pytest.fixture
def running_server(sandbox):
    import threading

    from gui import server as srv
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    t.join(timeout=2)


@pytest.fixture
def real_queue(monkeypatch, sandbox):
    """The panel's own relabel queue, relabeling in-process."""
    from gui import jobqueue
    from gui import server as srv
    q = jobqueue.RelabelQueue().start()
    monkeypatch.setattr(srv, "RELABEL_QUEUE", q)
    monkeypatch.setattr(srv, "_spawn", lambda cmd: pytest.fail(f"spawned {cmd}"))
    yield q
    q.stop(timeout=5)


def _drained(q):
    from tests.test_jobqueue import _drain
    _drain(q)
    assert mirror.wait(10)


def _copy(drive, base):
    return (drive / f"{base}.txt").read_text()


def test_a_relabel_updates_the_mirror(drive):
    import relabel
    from tests.test_relabel import _seed_meeting
    _seed_meeting(WEEKLY)
    mirror.reconcile()
    assert _copy(drive, WEEKLY) == "stub"
    assert relabel.relabel_one(WEEKLY) is True
    assert mirror.wait(10)
    assert (drive / f"{WEEKLY}.txt").read_bytes() == \
        config.meeting_file(WEEKLY, ".txt").read_bytes()
    assert "Voice 1" in _copy(drive, WEEKLY)


def test_a_strict_relabel_removes_the_copy(drive):
    import relabel
    from tests.test_relabel import _seed_meeting
    _seed_meeting(WEEKLY)
    mirror.reconcile()
    assert relabel.relabel_one(WEEKLY, strict=True) is True
    assert mirror.wait(10)
    assert _names(drive) == []


def test_a_meeting_local_name_from_the_panel_updates_the_mirror(drive, running_server):
    from tests.test_relabel import _seed_meeting
    _seed_meeting(WEEKLY)
    mirror.reconcile()
    status, body = _post(running_server, "/api/name",
                         {"meeting": WEEKLY, "speaker": "SPEAKER_00",
                          "name": "Alex Rivera", "local": True})
    assert status == 200 and body["ok"] and body["applied"]
    assert mirror.wait(10)
    assert "Alex Rivera" in _copy(drive, WEEKLY)


def test_a_speaker_rename_from_the_panel_updates_every_copy(drive, running_server,
                                                            real_queue):
    v = _unit(1)
    from stt import identify
    identify.enroll("Dana Cole", v, source=WEEKLY)
    _cached_meeting(WEEKLY, {"SPEAKER_00": v},
                    speakers=[{"id": "SPEAKER_00", "name": "Dana Cole",
                               "display": "Dana Cole"}])
    _cached_meeting(BUDGET, {"SPEAKER_00": _unit(3)})
    mirror.reconcile()
    status, body = _post(running_server, "/api/rename_speaker",
                         {"name": "Dana Cole", "new": "Dana Brooks"})
    assert status == 200 and body["ok"] and body["targets"] == [WEEKLY]
    _drained(real_queue)
    assert "Dana Brooks" in _copy(drive, WEEKLY)
    assert "Dana Cole" not in _copy(drive, WEEKLY)
    assert _copy(drive, BUDGET) == "stub"       # a meeting the change missed


def test_a_merge_from_the_panel_updates_the_copy(drive, running_server, real_queue):
    from stt import identify
    va, vb = _unit(1), _unit(2)
    identify.enroll("Dana Cole", va, source=WEEKLY)
    identify.enroll("D. Cole", vb, source=BUDGET)
    _cached_meeting(BUDGET, {"SPEAKER_00": vb},
                    speakers=[{"id": "SPEAKER_00", "name": "D. Cole",
                               "display": "D. Cole"}])
    mirror.reconcile()
    status, body = _post(running_server, "/api/merge_speakers",
                         {"src": "name:D. Cole", "dst": "name:Dana Cole",
                          "confirm": True})
    assert status == 200 and body["ok"]
    _drained(real_queue)
    assert "Dana Cole" in _copy(drive, BUDGET)


def _review_meeting(base):
    from tests.test_review import _make_meeting
    d = _make_meeting(None, base=base)
    config.meeting_file(base, ".json").write_text(
        json.dumps({"meeting_id": f"{next(_ids):016x}", **d}))
    return d


def test_a_line_edit_updates_the_mirror(drive):
    from stt import review
    _review_meeting(WEEKLY)
    mirror.reconcile()
    r = review.apply(WEEKLY, 1, "edit", start=5.0, text="Revised in review.")
    assert r["ok"]
    assert mirror.wait(10)
    assert "Revised in review." in _copy(drive, WEEKLY)


def test_an_inserted_and_a_deleted_line_update_the_mirror(drive):
    from stt import review
    _review_meeting(WEEKLY)
    mirror.reconcile()
    r = review.insert_segment(WEEKLY, 6.5, 7.5, "name:Jordan Lee", "One more thing.")
    assert r["ok"]
    assert mirror.wait(10)
    assert "Jordan Lee: One more thing." in _copy(drive, WEEKLY)
    assert review.delete_segment(WEEKLY, r["index"], start=6.5)["ok"]
    assert mirror.wait(10)
    assert "One more thing." not in _copy(drive, WEEKLY)


def test_a_line_edit_through_the_panel_updates_the_mirror(drive, running_server):
    _review_meeting(WEEKLY)
    mirror.reconcile()
    status, body = _post(running_server, "/api/review",
                         {"base": WEEKLY, "action": "edit", "index": 0,
                          "start": 0.0, "end": 5.0, "text": "Opening, revised."})
    assert status == 200 and body["ok"]
    assert mirror.wait(10)
    assert "Opening, revised." in _copy(drive, WEEKLY)


def test_a_panel_edit_does_not_wait_for_the_mirror(drive, monkeypatch):
    """The copy runs after the action, in the background: a slow sync
    folder never holds a save."""
    import threading
    import time

    from stt import review
    _review_meeting(WEEKLY)
    gate = threading.Event()
    real = mirror.reconcile

    def slow(*a, **k):
        gate.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(mirror, "reconcile", slow)
    t0 = time.monotonic()
    assert review.apply(WEEKLY, 1, "accept", start=5.0)["ok"]
    assert time.monotonic() - t0 < 2.0
    assert _names(drive) == []
    gate.set()
    assert mirror.wait(10)
    assert _names(drive) == [f"{WEEKLY}.txt"]


def test_a_panel_edit_with_the_folder_missing_still_saves(sandbox, monkeypatch, capsys,
                                                          tmp_path_factory):
    from stt import review
    d = tmp_path_factory.mktemp("away") / "Drive"
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    monkeypatch.setattr(mirror, "_said", {"msg": None})
    _review_meeting(WEEKLY)
    assert review.apply(WEEKLY, 1, "accept", start=5.0)["ok"]
    assert review.apply(WEEKLY, 0, "edit", start=0.0, text="Again.")["ok"]
    assert mirror.wait(10)
    assert capsys.readouterr().err.count("is missing or not a folder") == 1
    assert "Again." in config.meeting_file(WEEKLY, ".txt").read_text()
