"""jobs: the panel's queued-run store — a Redo clicked mid-batch must wait
visibly, never vanish on the single-instance lock."""
import pytest

from stt import jobs


def test_add_items_remove_roundtrip(sandbox):
    assert jobs.items() == []
    jobs.add({"paths": ["/x/A.m4a"], "force": True, "label": "A"})
    jobs.add({"files": ["B.m4a"], "strict": True, "label": "B"})
    got = jobs.items()
    assert [j["label"] for j in got] == ["A", "B"]
    assert all("at" in j for j in got)
    # claim (what run_batch does once it holds the lock)
    assert jobs.remove(got[0]["at"]) is True
    assert [j["label"] for j in jobs.items()] == ["B"]
    # double-claim is a no-op, not an error (lost lock race)
    assert jobs.remove(got[0]["at"]) is False


def test_spawn_args_carry_all_options(sandbox):
    jobs.add({"paths": ["/x/A.m4a"], "force": True, "strict": True,
              "verify": True, "parallel": 2, "label": "A"})
    j = jobs.items()[0]
    args = jobs.spawn_args(j)
    assert args[:3] == ["caffeinate", "-i", "-s"]
    for want in ("--ignore-pause", "--job", "--paths", "--force", "--strict",
                 "--verify", "--parallel"):
        assert want in args
    assert args[args.index("--job") + 1] == str(j["at"])


def test_corrupt_queue_degrades_to_empty(sandbox):
    jobs.PATH.write_text("{nope")
    assert jobs.items() == []
    jobs.add({"label": "ok"})  # and recovers on the next write
    assert len(jobs.items()) == 1


@pytest.mark.parametrize("text", ["{}", '"abc"', "123", "null"])
def test_a_queue_file_of_the_wrong_shape_reads_empty_and_add_still_queues(sandbox, text, capsys):
    """Valid JSON that is not a list made add() raise TypeError ('dict' +
    'list'), so the Run click failed instead of queueing."""
    jobs.PATH.write_text(text)
    assert jobs.items() == []
    got = jobs.add({"label": "ok"})
    assert [j["label"] for j in jobs.items()] == ["ok"] and "at" in got
    # the damaged file is kept beside the queue, not written over
    assert jobs.PATH.with_suffix(".json.bad").read_text() == text
    assert "queued_jobs.json.bad" in capsys.readouterr().err


def test_entries_that_are_not_jobs_are_dropped(sandbox):
    jobs.PATH.write_text('[1, "x", null, {"label": "A", "at": 5.0}]')
    assert jobs.items() == [{"label": "A", "at": 5.0}]
    assert jobs.remove(5.0) is True and jobs.items() == []


def test_a_good_queue_file_is_never_set_aside(sandbox):
    jobs.add({"label": "A"})
    jobs.add({"label": "B"})
    assert not jobs.PATH.with_suffix(".json.bad").exists()


def test_clear_drops_everything(sandbox):
    jobs.add({"label": "A"})
    jobs.add({"label": "B"})
    assert jobs.clear() == 2
    assert jobs.items() == []
    assert jobs.clear() == 0


def test_stop_run_clears_queued_jobs(sandbox):
    """'Stop processing' must not be followed by a queued job auto-starting."""
    from stt import control
    jobs.add({"label": "A"})
    res = control.stop_run()
    assert res["cleared_jobs"] == 1 and jobs.items() == []


def test_same_millisecond_adds_get_distinct_ids(sandbox, monkeypatch):
    import stt.jobs as J
    monkeypatch.setattr(J.time, "time", lambda: 1000.0)
    J.add({"label": "A"})
    J.add({"label": "B"})
    ats = [j["at"] for j in J.items()]
    assert len(set(ats)) == 2


def test_mutate_uses_tmp_then_atomic_replace(sandbox, monkeypatch):
    """_mutate() must go through write-tmp-then-os.replace, not a direct
    write — spy on os.replace to prove the mechanism is actually used."""
    import os
    calls = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (calls.append((src, dst)), real_replace(src, dst)))
    jobs.add({"label": "A"})
    assert len(calls) == 1
    src, dst = calls[0]
    assert str(src).endswith(".tmp") and dst == jobs.PATH
    assert not __import__("pathlib").Path(src).exists()


def test_crash_while_writing_tmp_leaves_the_real_queue_untouched(sandbox):
    jobs.add({"label": "A"})
    good_before = jobs.PATH.read_text()
    tmp = jobs.PATH.with_suffix(".json.tmp")
    tmp.write_text('[{"label": "half-written')  # torn write, crash before replace
    assert jobs.PATH.read_text() == good_before
    assert [j["label"] for j in jobs.items()] == ["A"]
    tmp.unlink()


def test_split_list_prefers_field_sep_falls_back_to_comma():
    assert jobs.split_list("a" + jobs.FIELD_SEP + "b") == ["a", "b"]
    assert jobs.split_list("a,b") == ["a", "b"]  # human-typed CLI convenience


def test_an_empty_list_round_trips_as_no_names():
    """split_list(join_list([])) gave [''], one empty name, not []."""
    assert jobs.join_list([]) == ""
    assert jobs.split_list(jobs.join_list([])) == []
    assert jobs.split_list("") == []


def test_join_list_survives_a_comma_in_a_real_filename():
    """A filename containing a literal comma (plausible for a renamed voice-
    memo export) must round-trip through spawn_args() -> argparse intact,
    not silently split into two bogus names."""
    joined = jobs.join_list(["Meeting, Part 1.m4a", "Normal.m4a"])
    assert jobs.split_list(joined) == ["Meeting, Part 1.m4a", "Normal.m4a"]


@pytest.mark.parametrize("items", [["Budget, Q3.m4a"], ["a,b,c.m4a", "d.m4a"],
                                   ["Plain.m4a"], ["/x/Budget, Q3.m4a"]])
def test_a_one_item_list_with_a_comma_round_trips(items):
    """join_list of one item added no FIELD_SEP, so split_list fell back to
    commas: a Redo of 'Budget, Q3.m4a' alone asked for 'Budget' and
    ' Q3.m4a', and the chosen file did not run."""
    assert jobs.split_list(jobs.join_list(items)) == items
    args = jobs.spawn_args({"at": 1.0, "files": items})
    assert jobs.split_list(args[args.index("--files") + 1]) == items
    assert jobs.split_list("a.m4a,b.m4a") == ["a.m4a", "b.m4a"]

def test_spawn_args_comma_filename_round_trips_through_real_argparse(sandbox):
    """End-to-end: spawn_args() output must survive being handed to
    run_batch.py's ACTUAL argparse parser and split back out correctly."""
    import run_batch
    job = jobs.add({"files": ["Meeting, Part 1.m4a", "Normal.m4a"], "label": "x"})
    args_list = jobs.spawn_args(job)
    files_idx = args_list.index("--files") + 1
    files_arg = args_list[files_idx]
    assert run_batch.jobs.split_list(files_arg) == ["Meeting, Part 1.m4a", "Normal.m4a"]


def test_onetime_flag_round_trips_to_spawn_args(sandbox):
    args = jobs.spawn_args({"at": 1.0, "files": ["A.m4a"], "onetime": True})
    assert "--one-time-speakers" in args
    args = jobs.spawn_args({"at": 1.0, "files": ["A.m4a"]})
    assert "--one-time-speakers" not in args


def test_a_spawned_job_runs_the_checkouts_run_sh_not_the_data_homes(sandbox):
    """Under STT_HOME the data home holds no run.sh. A run from the panel
    answered {ok: true}, sat queued, and spawned.log said '.../run.sh: No
    such file or directory'. Every spawn starts from the checkout."""
    from pathlib import Path

    from gui import menubar, server
    from stt import config, jobs
    assert config.CODE_DIR == Path(jobs.__file__).resolve().parent.parent
    assert config.CODE_DIR != config.PROJECT_DIR      # the sandbox data home
    argv = jobs.spawn_args({"at": 1.0})
    assert argv[3] == str(config.CODE_DIR / "run.sh")
    assert Path(argv[3]).is_file()
    assert server.RUN_SH == config.CODE_DIR / "run.sh"
    assert Path(menubar.ICON).is_file()
