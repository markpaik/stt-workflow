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
    # stage_since (the wall-clock bound) is monotonic now, so a corrected/jumped
    # wall clock can't inflate it; drive that clock to exercise the bound.
    monkeypatch.setattr(st._time, "monotonic", lambda: t["now"])
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

    # past the estimate the stage is RUNNING LONG: no remainder pinned at 3%
    # of the stage (it froze the countdown for hours); the tail is a fraction
    # of the elapsed stage time, so it grows as the stage runs on, and the
    # stage is still never claimed done
    monkeypatch.setattr(st, "stage_tail", lambda stage, n_active=1: 0.2)
    t["now"] = 2500.0
    d = st.progress_detail(entry)
    assert d["over"] == 500.0
    assert d["eta"] == 0.2 * 1500.0 and d["pct"] < 1.0
    t["now"] = 3100.0
    later = st.progress_detail(entry)
    assert later["over"] == 1100.0 and later["eta"] == 0.2 * 2100.0


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


def test_set_stage_concurrent_no_lost_update(sandbox):
    """Two processes/threads mutating the SAME status.json must serialize their
    read-modify-write. Without the flock, a writer built from a pre-read snapshot
    silently clobbers another writer's just-published entry (lost update)."""
    import threading
    from stt import status as st

    st.start_run([])  # active = {}
    ev_a_paused = threading.Event()
    ev_b_done = threading.Event()
    state = {"paused_once": False}
    real_write = st._write

    def instrumented_write(d):
        # Pause thread A AFTER it has read empty + built its {A} update, opening
        # a window for B to run its whole RMW. With the lock, B blocks on the
        # flock and this wait times out -> A commits, releases, then B commits;
        # without the lock, B writes {B} and A's stale write drops it.
        if not state["paused_once"] and "A.m4a" in d.get("active", {}):
            state["paused_once"] = True
            ev_a_paused.set()
            ev_b_done.wait(1.5)
        real_write(d)

    st._write = instrumented_write
    try:
        ta = threading.Thread(target=lambda: st.set_stage(
            "A.m4a", "transcribing", progress=0.5, duration=100.0))
        tb = threading.Thread(target=lambda: st.set_stage(
            "B.m4a", "transcribing", progress=0.5, duration=100.0))
        ta.start()
        assert ev_a_paused.wait(3)  # A has read + built {A}, now paused pre-write
        tb.start()
        tb.join(4)
        ev_b_done.set()
        ta.join(4)
    finally:
        st._write = real_write

    active = st.read().get("active", {})
    assert "A.m4a" in active and "B.m4a" in active  # neither write lost


def test_estimate_progress_ignores_wall_clock_jump(sandbox):
    """The stalled-hook wall bound is anchored on a monotonic clock, so an NTP
    correction / resume that jumps the wall clock forward must NOT lift the brake
    on an over-eager progress hook and report the stage as nearly done."""
    from stt import status as st

    st.start_run([])
    st.set_stage("A.m4a", "transcribing", progress=0.95, duration=600.0)
    entry = st.read()["active"]["A.m4a"]

    base_overall, _ = st.estimate_progress(entry, n_active=1)
    real_time = st._time.time
    try:
        st._time.time = lambda: real_time() + 100000.0  # huge forward wall jump
        jumped_overall, _ = st.estimate_progress(entry, n_active=1)
    finally:
        st._time.time = real_time

    assert abs(jumped_overall - base_overall) < 0.01


def test_stalled_hook_keeps_the_countdown_moving(sandbox):
    """The diarizer's hook races to ~87% then reports NOTHING through the
    clustering tail. min(hook, wall) froze the ETA at the hook's value for
    the whole silent stretch, then collapsed it all at once when the stage
    flipped ('35m left' one poll, '4m' the next). Once the hook is stalled,
    the wall clock must keep the countdown moving — without ever claiming
    the stage finished."""
    import time as _t

    entry = {"stage": "diarizing", "duration": 4200.0, "progress": 0.87,
             "progress_at": _t.time() - 300}       # hook silent for 5 minutes
    est = status.stage_estimates(4200.0)["diarizing"]

    entry["stage_since"] = _t.monotonic() - 0.90 * est   # wall past the hook
    _, eta_early = status.estimate_progress(entry)
    entry["stage_since"] = _t.monotonic() - 0.96 * est   # 6% more wall time
    _, eta_later = status.estimate_progress(entry)

    assert eta_later < eta_early, "ETA froze while the hook was stalled"
    # ...but a LIVE hook that is genuinely behind still bounds the estimate
    # (a fresh progress_at means the hook is trustworthy; wall may not race it)
    live = {"stage": "diarizing", "duration": 4200.0, "progress": 0.30,
            "progress_at": _t.time(), "stage_since": _t.monotonic() - 0.90 * est}
    frac_live, _ = status.estimate_progress(live)
    stalled_frac, _ = status.estimate_progress(entry)
    assert frac_live < stalled_frac
    # and the stage is never declared done from the clock alone: five times
    # its estimate in, it is running long, with time still on the clock
    entry["stage_since"] = _t.monotonic() - 5.0 * est
    frac_cap, eta_cap = status.estimate_progress(entry)
    assert frac_cap < 1.0 and eta_cap > 0
    assert status.progress_detail(entry)["over"] > 3.9 * est


def test_finished_stage_actuals_accumulate(sandbox):
    """Stage transitions record each finished stage's REAL wall seconds
    (done_secs), which the panel shows instead of one pipeline-wide guess."""
    import time as _t
    status.start_run(["a"])
    status.set_stage("a", "converting", duration=600)
    _t.sleep(0.05)
    status.set_stage("a", "transcribing")
    _t.sleep(0.05)
    status.set_stage("a", "diarizing")
    e = status.read()["active"]["a"]
    assert set(e["done_secs"]) == {"converting", "transcribing"}
    assert all(v >= 0 for v in e["done_secs"].values())


def test_stage_breakdown_states(sandbox):
    """done/active/next per stage, with actuals for done and est for ahead."""
    import time as _t
    status.start_run(["a"])
    status.set_stage("a", "converting", duration=600)
    status.set_stage("a", "transcribing", progress=0.4)
    bd = status.stage_breakdown(status.read()["active"]["a"])
    by = {x["stage"]: x for x in bd}
    assert by["converting"]["state"] == "done"
    assert by["transcribing"]["state"] == "active"
    assert by["transcribing"]["est"] > 0 and by["transcribing"]["secs"] is not None
    assert by["diarizing"]["state"] == "next" and by["diarizing"]["est"] > 0
    # unknown duration -> no breakdown at all
    assert status.stage_breakdown({"stage": "transcribing"}) is None


def test_finish_file_appends_to_permanent_history(sandbox):
    status.start_run(["a.m4a", "b.m4a"])
    status.finish_file("a.m4a", True, "2 speaker(s)")
    status.finish_file("b.m4a", False, "ffmpeg exploded")
    lines = status.HISTORY_LOG.read_text().splitlines()
    assert len(lines) == 2  # one line per result, oldest first on disk
    h = status.history()
    assert [r["name"] for r in h] == ["b.m4a", "a.m4a"]  # newest first
    assert h[0]["ok"] is False and "ffmpeg" in h[0]["summary"]


def test_history_includes_pre_log_ring_entries_once(sandbox):
    # results recorded before results.jsonl existed live only in the status
    # ring — history() must surface them, and must not double-count entries
    # present in both places
    import json
    status.start_run(["new.m4a"])
    status.finish_file("new.m4a", True, "ok")
    d = status.read()
    d["recent"].append({"name": "old.m4a", "ok": True,
                        "summary": "from before the log", "at": "2026-01-01T09:00:00"})
    status.STATUS_PATH.write_text(json.dumps(d))
    h = status.history()
    assert [r["name"] for r in h] == ["new.m4a", "old.m4a"]


def test_history_survives_a_corrupt_log_line(sandbox):
    status.start_run(["a.m4a"])
    status.finish_file("a.m4a", True, "ok")
    with open(status.HISTORY_LOG, "a") as f:
        f.write("{not json\n")
    status.start_run(["b.m4a"])
    status.finish_file("b.m4a", True, "ok")
    assert [r["name"] for r in status.history()] == ["b.m4a", "a.m4a"]


def test_recording_key_roundtrips_and_survives_a_run(sandbox):
    status.set_recording({"pid": 4242, "caf": "/x/.rec-a.caf", "started_at": "2026-07-09T10:00:00"})
    assert status.recording()["pid"] == 4242
    # a batch starting mid-recording must NOT wipe the banner state
    status.start_run(["a.m4a"])
    assert status.recording()["pid"] == 4242
    status.set_stage("a.m4a", "transcribing", duration=600)
    assert status.recording()["pid"] == 4242
    status.finish_file("a.m4a", True, "ok")
    status.end_run()
    assert status.recording()["pid"] == 4242  # outlives the whole run
    status.clear_recording()
    assert status.recording() is None


def test_history_log_is_trimmed_when_it_grows_too_large(sandbox, monkeypatch):
    """R1: the permanent results log must not grow without bound. Once it passes
    the size cap, finish_file rewrites it keeping only the most recent lines,
    and history() still returns them newest-first."""
    import json

    from stt import status
    monkeypatch.setattr(status, "HISTORY_MAX_BYTES", 50)   # tiny cap for the test
    monkeypatch.setattr(status, "HISTORY_KEEP_LINES", 3)
    for i in range(50):
        status.finish_file(f"m{i}.m4a", True, "x" * 40)
    lines = status.HISTORY_LOG.read_text().splitlines()
    assert len(lines) <= 3                                   # bounded, not 50
    assert json.loads(lines[-1])["name"] == "m49.m4a"        # kept the most recent
    assert status.history()[0]["name"] == "m49.m4a"          # still browsable


def _diarizing_clock(monkeypatch, est=20000.0, tail=0.2):
    """A 4-hour file 'in' diarizing, stalled hook, with the monotonic clock
    under the test's control. Returns (entry, clock)."""
    from stt import status as st
    t = {"now": 0.0}
    monkeypatch.setattr(st._time, "monotonic", lambda: t["now"])
    monkeypatch.setattr(st, "stage_estimates",
                        lambda dur, n_active=1, verify=False, diarize=True:
                        {"downloading": 0.0, "converting": 0.0, "transcribing": 0.0,
                         "diarizing": est, "writing": 0.0})
    monkeypatch.setattr(st, "stage_tail", lambda stage, n_active=1: tail)
    entry = {"stage": "diarizing", "duration": 4 * 3600.0, "progress": 0.87,
             "progress_at": st._time.time() - 3600, "stage_since": 0.0}
    return entry, t


def test_the_countdown_never_sits_still_for_two_minutes(sandbox, monkeypatch):
    """A 4-hour board meeting showed "10-11 min" for hours: once the elapsed
    time passed 97 percent of the estimate, the stage was pinned at 97 percent
    and 3 percent of a long stage stayed on the clock. Read every minute from
    the start of the stage to twice its estimate, the value the panel shows
    (the countdown's minutes, or the overrun's minutes past the estimate)
    must change at least every two minutes."""
    from stt import status as st
    entry, t = _diarizing_clock(monkeypatch)
    entry["stage_since"] = 1.0          # truthy, like a real monotonic stamp
    shown = []
    for minute in range(0, int(2 * 20000 / 60)):
        t["now"] = 1.0 + minute * 60.0
        d = st.progress_detail(entry)
        shown.append(("over", int(d["over"] // 60)) if d["over"] is not None
                     else ("eta", round(d["eta"] / 60)))
    for i in range(2, len(shown)):
        assert not (shown[i] == shown[i - 1] == shown[i - 2]), \
            f"frozen at minute {i}: {shown[i]}"
    # the countdown reaches the estimate, then the overrun takes over
    assert shown[330] == ("eta", 3) and shown[334][0] == "over"


def test_running_long_reports_the_overrun_and_a_growing_tail(sandbox, monkeypatch):
    """Past the estimate: `over` is the seconds past it, the file's ETA is the
    model's tail times the elapsed stage time plus the stages still ahead, and
    the overall fraction never steps back at the estimate and never reaches
    done."""
    from stt import status as st
    entry, t = _diarizing_clock(monkeypatch, est=1000.0, tail=0.25)
    entry["stage_since"] = 1.0
    t["now"] = 1.0 + 999.0
    edge = st.progress_detail(entry)
    assert edge["over"] is None                            # within: counting down
    t["now"] = 1.0 + 1600.0
    a = st.progress_detail(entry)
    t["now"] = 1.0 + 2400.0
    b = st.progress_detail(entry)
    assert a["over"] == 600.0 and b["over"] == 1400.0
    assert a["eta"] == 0.25 * 1600.0 and b["eta"] == 0.25 * 2400.0
    assert edge["pct"] <= a["pct"] <= b["pct"] < 1.0


def test_the_tail_comes_from_the_models_residuals(sandbox, monkeypatch):
    """No stub on the tail: a stage past its estimate is given the learned
    model's own overrun fraction, which moves with the samples."""
    from stt import rates
    from stt import status as st
    monkeypatch.setattr(rates, "current_asr_key", lambda: "parakeet")
    import time as _t
    entry = {"stage": "diarizing", "duration": 3600.0, "progress": 0.87,
             "progress_at": _t.time() - 600}
    est = st.stage_estimates(3600.0)["diarizing"]
    entry["stage_since"] = _t.monotonic() - 2 * est
    d = st.progress_detail(entry)
    assert abs(d["eta"] - (rates.DEFAULT_TAIL * 2 * est + st.stage_estimates(3600.0)["writing"])) < 5
    # samples that overran their prediction by 60 percent, three in ten
    for i, dur in enumerate([600, 900, 1200, 1500, 1800, 2100, 2400, 2700, 3000, 3600]):
        rates.record(dur, {"diarizing": dur * (1.6 if i in (2, 5, 8) else 1.0),
                           "writing": 10}, "parakeet", 1)
    tail = st.stage_tail("diarizing")
    assert tail == rates.learned()["tail"]["diarize"]["1"] and tail > rates.DEFAULT_TAIL
    est = st.stage_estimates(3600.0)["diarizing"]
    entry["stage_since"] = _t.monotonic() - 2 * est
    d = st.progress_detail(entry)
    assert abs(d["eta"] - (tail * 2 * est + st.stage_estimates(3600.0)["writing"])) < 5
