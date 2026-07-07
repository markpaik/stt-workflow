"""status: run lifecycle across states — start, stages, finish, end, recover."""
from stt import status


def test_full_lifecycle(sandbox):
    status.start_run(["a.m4a", "b.m4a"])
    d = status.read()
    assert d["running"] and d["pending"] == ["a.m4a", "b.m4a"]
    assert d["pgid"]  # stop path depends on this

    status.set_stage("a.m4a", "transcribing", progress=0.5, duration=600)
    d = status.read()
    assert d["active"]["a.m4a"]["stage"] == "transcribing"
    assert "a.m4a" not in d["pending"]

    status.finish_file("a.m4a", True, "2 speaker(s)")
    d = status.read()
    assert "a.m4a" not in d["active"]
    assert d["recent"][0]["ok"]

    status.end_run()
    d = status.read()
    assert not d["running"] and d["pgid"] is None and d["active"] == {}


def test_duration_remembered_across_stage_calls(sandbox):
    status.start_run(["a"])
    status.set_stage("a", "converting", duration=900)
    status.set_stage("a", "transcribing")  # no duration passed
    assert status.read()["active"]["a"]["duration"] == 900


def test_recent_is_rolling_last_20(sandbox):
    status.start_run([])
    for i in range(25):
        status.finish_file(f"f{i}", True, "")
    recent = status.read()["recent"]
    assert len(recent) == 20
    assert recent[0]["name"] == "f24"  # newest first, oldest pushed out


def test_failures_recorded(sandbox):
    status.start_run([])
    status.finish_file("bad.m4a", False, "boom")
    r = status.read()["recent"][0]
    assert r["ok"] is False and r["summary"] == "boom"


def test_estimate_progress_states(sandbox):
    # unknown duration -> unknowable
    assert status.estimate_progress({"stage": "transcribing"}) == (None, None)
    # unknown stage -> unknowable
    assert status.estimate_progress({"stage": "??", "duration": 100}) == (None, None)
    # mid-diarization is further along than start-of-transcription
    f1, _ = status.estimate_progress({"stage": "transcribing", "duration": 600, "progress": 0.0})
    f2, eta2 = status.estimate_progress({"stage": "diarizing", "duration": 600, "progress": 0.5})
    assert 0 <= f1 < f2 <= 0.99 and eta2 >= 0


def test_missing_status_file_reads_empty(sandbox):
    assert status.read() == {}


def test_estimate_progress_verifying_stage(sandbox):
    """Verify runs report a 'verifying' stage — ETA must keep working there,
    and the extra pass must appear in the total only when actually verifying."""
    plain, _ = status.estimate_progress({"stage": "transcribing", "duration": 600, "progress": 1.0})
    f, eta = status.estimate_progress({"stage": "verifying", "duration": 600, "progress": 0.5})
    assert f is not None and 0 < f < 1 and eta > 0
    # non-verify estimates exclude the verifying stage entirely
    assert "verifying" not in status.stage_estimates(600)
    assert status.stage_estimates(600, verify=True)["verifying"] > 0
