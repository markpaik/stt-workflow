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


def test_no_diarize_run_never_budgets_diarizing_time(sandbox):
    """A --no-diarize file's ETA must never include diarizing time — the old
    bug budgeted it in unconditionally, so progress% lagged the whole run
    then suddenly jumped once "writing" arrived (crediting a stage that was
    never actually going to happen)."""
    est_with = status.stage_estimates(600, diarize=True)
    est_without = status.stage_estimates(600, diarize=False)
    assert "diarizing" in est_with and "diarizing" not in est_without
    assert sum(est_without.values()) < sum(est_with.values())

    # a file that never reports diarize=True must see a STABLE percentage
    # across the stages it actually goes through — no jump at "writing"
    f_transcribing, _ = status.estimate_progress(
        {"stage": "transcribing", "duration": 600, "progress": 1.0, "diarize": False})
    f_writing, _ = status.estimate_progress(
        {"stage": "writing", "duration": 600, "progress": 0.0, "diarize": False})
    assert f_writing >= f_transcribing  # monotonic, no backward or absurd forward jump
    assert f_writing - f_transcribing < 0.15  # no discontinuity from phantom stage credit


def test_verify_mode_known_up_front_no_progress_jump(sandbox):
    """A verify-mode file must show verifying's time in its ETA from the very
    first stage, not just once "verifying" itself starts — otherwise the
    percentage jumps backward the instant verifying begins."""
    entry_early = {"stage": "transcribing", "duration": 600, "progress": 0.0, "verify": True}
    entry_at_verify = {"stage": "verifying", "duration": 600, "progress": 0.0, "verify": True}
    f_early, _ = status.estimate_progress(entry_early)
    f_at_verify, _ = status.estimate_progress(entry_at_verify)
    # transcribing must ALREADY account for verifying's time in its total —
    # so crossing into "verifying" doesn't suddenly inflate the denominator
    assert f_at_verify >= f_early

    # without verify, the same audio's transcribing-stage % must be HIGHER
    # (smaller total denominator) -- proving verify=True really did enlarge
    # the total from the start, not just once "verifying" began
    f_no_verify, _ = status.estimate_progress(
        {"stage": "transcribing", "duration": 600, "progress": 0.0, "verify": False})
    assert f_no_verify > f_early


def test_set_stage_remembers_diarize_and_verify_across_calls(sandbox):
    """diarize/verify are sent once (like duration) and must persist on
    later set_stage() calls that don't repeat them."""
    status.set_stage("f.m4a", "downloading", duration=600, diarize=False, verify=True)
    status.set_stage("f.m4a", "converting")  # no diarize/verify passed here
    entry = status.read()["active"]["f.m4a"]
    assert entry["diarize"] is False and entry["verify"] is True


def test_progress_at_stamps_only_when_the_bar_moves(sandbox, monkeypatch):
    """progress_at is the panel's "still working" signal: it updates when the
    progress VALUE changes and holds still while the value repeats — a frozen
    bar with an old stamp is how a hook-less stage tail gets labeled."""
    from stt import status as st

    t = {"now": 1000.0}
    monkeypatch.setattr(st._time, "time", lambda: t["now"])
    st.set_stage("A.m4a", "diarizing", progress=0.5, duration=100)
    assert st.read()["active"]["A.m4a"]["progress_at"] == 1000.0

    t["now"] = 1060.0
    st.set_stage("A.m4a", "diarizing", progress=0.5)  # same value — stamp holds
    assert st.read()["active"]["A.m4a"]["progress_at"] == 1000.0

    t["now"] = 1120.0
    st.set_stage("A.m4a", "diarizing", progress=0.87)  # moved — stamp updates
    assert st.read()["active"]["A.m4a"]["progress_at"] == 1120.0


def test_eta_trusts_wall_clock_when_the_hook_goes_silent(sandbox, monkeypatch):
    """pyannote's hook reports ~87% within minutes, then embeds/clusters
    silently for most of the stage's wall time. The ETA must not read that
    frozen 87% as '13% left': once elapsed-in-stage exceeds what the reported
    fraction implies, the wall clock wins (capped below done)."""
    from stt import status as st

    t = {"now": 1000.0}
    monkeypatch.setattr(st._time, "time", lambda: t["now"])
    monkeypatch.setattr(st, "stage_estimates",
                        lambda dur, n_active=1, verify=False, diarize=True:
                        {"downloading": 0, "converting": 0, "transcribing": 0,
                         "diarizing": 1000.0, "writing": 0})

    st.set_stage("A.m4a", "diarizing", progress=0.87, duration=3600)
    entry = st.read()["active"]["A.m4a"]

    # 10s into a 1000s stage, hook already claims 87%: the clock bounds the
    # credit — nearly the whole stage still lies ahead
    t["now"] = 1010.0
    pct, eta = st.estimate_progress(entry)
    assert eta > 900

    # 700s in, bar frozen at 0.87: ≈300s left, NOT the hook's 130
    t["now"] = 1700.0
    pct, eta = st.estimate_progress(entry)
    assert 250 < eta < 350

    # past the estimate: the hook's own remainder is all that's left, and it
    # never claims done while the stage runs
    t["now"] = 2500.0
    pct, eta = st.estimate_progress(entry)
    assert 20 <= eta <= 160 and pct < 1.0


def test_verify_timings_calibrate_the_secondary_engine(sandbox):
    """A verify pass is a transcription by the secondary engine — its measured
    seconds must feed that engine's learned rate, so verify-mode ETAs adapt
    to the machine instead of staying on factory defaults forever."""
    from stt import rates

    # 600s of audio, primary parakeet: verify ran on whisper turbo for 60s
    rates.record(600.0, {"transcribing": 10.0, "verifying": 60.0}, "parakeet")
    L = rates.learned()
    assert L["asr"]["parakeet@1"] == 60.0          # 600/10 primary
    assert L["asr"]["mlxwhisper:turbo@1"] == 10.0  # 600/60 from the verify pass
