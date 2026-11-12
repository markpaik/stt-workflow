"""tools/mirror.py: a dry run by default, and --apply for a full reconcile.
The first --apply is the backfill. Every name here is synthetic."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from stt import config
from conftest import mfile

REPO = Path(__file__).resolve().parent.parent
WEEKLY = "2026-08-03 Weekly Check-in"
BUDGET = "2026-08-04 Budget Review"
HUDDLE = "2026-08-05 Staff Huddle"


def _tool():
    spec = importlib.util.spec_from_file_location("mirror_tool", REPO / "tools" / "mirror.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def drive(sandbox, tmp_path_factory):
    d = tmp_path_factory.mktemp("drive")
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    return d


def _meeting(base, strict=False):
    mfile(base, ".json").write_text(json.dumps({
        "meeting_id": f"{abs(hash(base)) % 16**16:016x}", "strict": strict,
        "date": base[:10], "speakers": [], "segments": [], "words": []}))
    mfile(base, ".txt").write_text(f"{base}  |  1.0 min  |  1 speaker(s)\n\n"
                                   "[00:01] Alex Rivera: hello.\n")


def test_the_default_is_a_dry_run(drive, capsys):
    _meeting(WEEKLY)
    _meeting(BUDGET)
    assert _tool().main([]) == 0
    out = capsys.readouterr().out
    assert f"Mirror folder: {drive}" in out
    assert f"  write    {WEEKLY}.txt" in out and f"  write    {BUDGET}.txt" in out
    assert "Would write 2, update 0, remove 0. 0 unchanged." in out
    assert "Dry run: nothing was written." in out
    assert os.listdir(drive) == []
    assert _tool().main(["--dry-run"]) == 0
    assert os.listdir(drive) == []


def test_the_first_apply_is_the_backfill_and_the_next_changes_nothing(drive, capsys):
    _meeting(WEEKLY)
    _meeting(BUDGET)
    _meeting(HUDDLE, strict=True)
    assert _tool().main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "Wrote 2, updated 0, removed 0. 0 unchanged. 0 blocked, 0 changed. " \
           "1 strict meeting(s) stay on this Mac." in out
    assert sorted(os.listdir(drive)) == [f"{WEEKLY}.txt", f"{BUDGET}.txt"]
    assert _tool().main(["--apply"]) == 0
    assert "Wrote 0, updated 0, removed 0. 2 unchanged." in capsys.readouterr().out


def test_it_names_what_it_leaves_alone(drive, capsys):
    _meeting(WEEKLY)
    _meeting(BUDGET)
    (drive / f"{BUDGET}.txt").write_text("typed by hand")
    assert _tool().main(["--apply"]) == 0
    (drive / f"{WEEKLY}.txt").write_text("edited in Drive")
    config.meeting_file(WEEKLY, ".txt").write_text("v2\n")
    assert _tool().main([]) == 0
    out = capsys.readouterr().out
    assert (f"  blocked  {BUDGET}.txt  (a file the mirror did not write holds "
            "this name)") in out
    assert f"  changed  {WEEKLY}.txt  (changed after the mirror wrote it" in out
    assert (drive / f"{BUDGET}.txt").read_text() == "typed by hand"


def test_it_exits_1_when_the_mirror_is_off(sandbox, capsys):
    assert _tool().main([]) == 1
    assert "STT_MIRROR_DIR is not set" in capsys.readouterr().err


def test_it_exits_1_on_a_bad_setting(sandbox, capsys):
    (sandbox / "stt.env").write_text("STT_MIRROR_DIR=relative/folder\n")
    assert _tool().main(["--apply"]) == 1
    assert "not an absolute folder path" in capsys.readouterr().err


def test_it_exits_1_when_the_folder_is_missing(sandbox, capsys, tmp_path_factory):
    d = tmp_path_factory.mktemp("away") / "Meeting Transcripts"
    (sandbox / "stt.env").write_text(f"STT_MIRROR_DIR={d}\n")
    _meeting(WEEKLY)
    assert _tool().main(["--apply"]) == 1
    assert "is missing or not a folder" in capsys.readouterr().err
    assert not d.exists()


def test_apply_and_dry_run_together_is_an_error(drive):
    with pytest.raises(SystemExit):
        _tool().main(["--apply", "--dry-run"])
