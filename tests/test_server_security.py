"""gui/server.py's two security gates, exercised against a real running server:

  - _known_base(): every client-supplied `base` must name a real transcript on
    disk, closing the path-traversal gap (base="../../../etc/passwd").
  - _origin_allowed(): a request carrying an Origin/Referer from anywhere but
    this panel is rejected — the defense against a malicious webpage driving
    this unauthenticated, 127.0.0.1-bound panel via the victim's own browser.
"""
import http.client
import json
import threading
from pathlib import Path

import pytest

from gui import server as srv
from stt import config
from conftest import mfile


@pytest.fixture
def running_server(sandbox):
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    t.join(timeout=2)


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    body = json.loads(r.read())
    conn.close()
    return r.status, body


def _post(port, path, payload, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json", **(headers or {})}
    conn.request("POST", path, body=json.dumps(payload).encode(), headers=h)
    r = conn.getresponse()
    body = json.loads(r.read())
    conn.close()
    return r.status, body


def _make_meeting(base):
    (mfile(base, ".json")).write_text(json.dumps({
        "source_file": f"{base}.m4a", "duration_sec": 10.0, "strict": False,
        "speakers": [], "segments": [], "words": []}))
    (mfile(base, ".txt")).write_text("stub")


def test_kick_jobs_suppressed_right_after_a_stop(sandbox, monkeypatch):
    """The panel's idle self-heal must not respawn a job in the window right
    after a Stop — a killed run may have re-queued it, and re-kicking it is the
    "Starting… ↔ Stopped" flicker. An explicit new run clears the flag."""
    from stt import control, jobs
    jobs.add({"label": "redo", "files": ["a.m4a"], "at": 1.0})
    monkeypatch.setattr(control, "snapshot", lambda *a, **k: {"pids": [], "mem_mb": 0})
    spawned = []
    monkeypatch.setattr(srv, "_spawn", lambda a: spawned.append(a))

    srv._jobs_kicked["at"] = 0.0  # bypass the cooldown so only the stop-flag matters
    control.mark_stopping()
    srv._kick_jobs()
    assert spawned == [], "a job must not be respawned right after a Stop"

    control.clear_stopping()
    srv._jobs_kicked["at"] = 0.0
    srv._kick_jobs()
    assert len(spawned) == 1, "once the stop window clears, the queue drains normally"


def test_remove_sample_re_identifies_all_meetings(running_server, monkeypatch):
    """Removing a sample changes who a profile matches, so it must re-run
    identification like every other registry edit — otherwise a misattributed
    voice stays wrong in past transcripts until some unrelated relabel. A prior
    version skipped the relabel spawn only for this one action."""
    import numpy as np

    from stt import identify
    identify.enroll("Alex Rivera", np.ones(8), source="M1")
    identify.enroll("Alex Rivera", np.arange(1, 9, dtype=float), source="M2")
    spawned = []
    monkeypatch.setattr(srv, "_spawn", lambda cmd: spawned.append(list(map(str, cmd))))

    status, body = _post(running_server, "/api/remove_sample",
                         {"name": "Alex Rivera", "index": 0})
    assert body["ok"]
    assert any("relabel" in " ".join(c) for c in spawned), \
        "removing a sample must spawn a relabel to re-identify meetings"


# ---------- _known_base ----------

def test_known_base_rejects_traversal_and_missing(sandbox):
    _make_meeting("Real Meeting")
    assert srv._known_base("Real Meeting")
    assert not srv._known_base("../../../../etc/passwd")
    assert not srv._known_base("../Real Meeting")
    assert not srv._known_base("Nonexistent Meeting")
    assert not srv._known_base("")
    assert not srv._known_base(None)


# ---------- _origin_allowed ----------

def test_origin_allowed_same_origin_or_absent(sandbox):
    assert srv._origin_allowed({})
    assert srv._origin_allowed({"Origin": "http://127.0.0.1:8737"})
    assert srv._origin_allowed({"Referer": "http://127.0.0.1:8737/"})
    assert srv._origin_allowed({"Origin": "http://localhost:8737"})


def test_origin_rejected_for_foreign_site(sandbox):
    assert not srv._origin_allowed({"Origin": "https://evil.example.com"})
    assert not srv._origin_allowed({"Referer": "https://evil.example.com/page"})
    # a spoofed host merely containing "127.0.0.1" as a substring must not pass
    assert not srv._origin_allowed({"Origin": "https://127.0.0.1.evil.com"})


# ---------- end-to-end against a real running server ----------

def test_transcript_endpoint_rejects_path_traversal(running_server):
    status, body = _get(running_server, "/api/transcript?base=../../../../etc/passwd")
    assert status == 400 and "error" in body


def test_transcript_endpoint_accepts_real_base(running_server):
    _make_meeting("Legit Meeting")
    status, body = _get(running_server, "/api/transcript?base=Legit%20Meeting")
    assert status == 200 and body["base"] == "Legit Meeting"


def test_audio_txt_review_edits_suggest_reject_unknown_base(running_server):
    for path in ("/api/audio?base=../x", "/api/txt?base=../x",
                 "/api/review?base=../x", "/api/edits?base=../x",
                 "/api/suggest?base=../x"):
        status, _ = _get(running_server, path)
        assert status == 400, path


def test_export_rename_retranscribe_reject_unknown_base(running_server):
    status, _ = _post(running_server, "/api/export", {"base": "../../etc/passwd", "fmt": "docx"})
    assert status == 400
    status, _ = _post(running_server, "/api/rename", {"base": "../x", "new": "y"})
    assert status == 400
    status, _ = _post(running_server, "/api/retranscribe", {"base": "../x", "start": 0, "end": 1})
    assert status == 400
    status, _ = _post(running_server, "/api/review",
                      {"base": "../x", "index": 0, "action": "accept"})
    assert status == 400


def test_foreign_origin_blocked_on_get(running_server):
    status, _ = _get(running_server, "/api/state", headers={"Origin": "https://evil.example.com"})
    assert status == 403


def test_foreign_origin_blocked_on_post(running_server):
    status, _ = _post(running_server, "/api/run", {}, headers={"Origin": "https://evil.example.com"})
    assert status == 403


def test_same_origin_requests_still_work(running_server):
    status, body = _get(running_server, "/api/state", headers={"Origin": "http://127.0.0.1:8737"})
    assert status == 200 and "running" in body
    status, body = _post(running_server, "/api/pause", {}, headers={"Origin": "http://127.0.0.1:8737"})
    assert status == 200 and body["ok"]
    status, body = _post(running_server, "/api/resume", {})  # no Origin at all: also allowed
    assert status == 200 and body["ok"]


# ---------- check_updates() timeout ----------

def test_check_updates_passes_a_timeout_to_hf_api(monkeypatch):
    """An offline machine or a stalled connection must not hang "Check
    updates" indefinitely — there is no cancel button. Verify HfApi.model_info
    is actually called WITH a timeout, not just documented as having one."""
    import huggingface_hub

    calls = []

    class FakeInfo:
        sha = "abc123"

    class FakeApi:
        def model_info(self, repo, timeout=None, **kw):
            calls.append(timeout)
            return FakeInfo()

    class FakeCache:
        repos = []

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    monkeypatch.setattr(huggingface_hub, "scan_cache_dir", lambda: FakeCache())

    result = srv.check_updates()
    assert result["models"]  # at least one configured model repo
    assert calls, "model_info was never called"
    assert all(t is not None and t > 0 for t in calls)


# ---------- pick_folder AppleScript injection (finding #1) ----------

def test_pick_folder_does_not_interpolate_prompt_into_applescript(monkeypatch):
    """The client-supplied prompt must never reach the AppleScript SOURCE — a
    `"` there closes the string literal and the rest runs as code. It has to be
    passed as an inert `on run argv` item instead."""
    captured = {}

    class R:
        returncode = 0
        stdout = "/some/folder\n"

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return R()

    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    payload = 'x" & (do shell script "touch /tmp/pwned") & "'
    srv.pick_folder(payload)

    cmd = captured["cmd"]
    # every -e script argument is the AppleScript SOURCE; the injection must
    # not appear in any of them (pre-fix it was spliced straight in)
    scripts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert all("do shell script" not in s for s in scripts), scripts
    # the payload survives only as a standalone argv item (after `--`)
    assert payload in cmd
    assert "--" in cmd and cmd.index(payload) > cmd.index("--")


# ---------- snippet path race (finding #11) ----------

def test_snippet_uses_a_unique_path_per_request(sandbox, monkeypatch):
    """Two concurrent snippet extractions must not share one fixed
    work/snippet.wav — that lets one request overwrite the other's audio."""
    monkeypatch.setattr(srv.review, "find_voice_clips",
                        lambda key, meeting=None, n=1: [
                            {"base": "Mtg", "start": 0.0, "dur": 2.0, "index": 0}])
    monkeypatch.setattr(srv, "_meeting_audio", lambda base: sandbox / "a.m4a")

    def fake_run(cmd, *a, **k):
        out = cmd[-1]  # ffmpeg output path (last arg) — write recognizable bytes
        Path(out).write_bytes(b"clip-for-" + Path(out).name.encode())
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(srv.subprocess, "run", fake_run)

    p1 = srv._snippet_for("Mtg", "Alice")
    b1 = p1.read_bytes()
    p2 = srv._snippet_for("Mtg", "Bob")
    # distinct output paths, and p1's bytes are untouched by the p2 extraction
    assert p1 != p2
    assert p1.read_bytes() == b1
    p1.unlink(missing_ok=True)
    p2.unlink(missing_ok=True)


# ---------- gather_state resilience to a vanished meeting (finding #16) ----------

def test_gather_state_skips_meeting_that_vanished_mid_request(sandbox, monkeypatch):
    """A meeting folder can disappear between meeting_bases() and the sort key
    (a concurrent /api/rename); that must drop the item, not 500 the poll."""
    _make_meeting("Real Meeting")
    real = list(config.meeting_bases())
    monkeypatch.setattr(config, "meeting_bases",
                        lambda dest_dir=None: real + ["Ghost Meeting"])
    st = srv.gather_state()  # pre-fix: FileNotFoundError from stat() in sort key
    assert isinstance(st, dict)
    assert {m["base"] for m in st["meetings"]} == {"Real Meeting"}


# ---------- _env_file_set concurrent writes (finding #17) ----------

def test_env_file_set_concurrent_writes_keep_both_keys(sandbox, monkeypatch):
    """Two settings POSTs on separate server threads must both persist — an
    unlocked read-modify-write drops one when their windows overlap."""
    import time

    real_get = srv._env_file_get

    def slow_get():
        res = real_get()
        time.sleep(0.15)  # widen the read->write window deterministically
        return res

    monkeypatch.setattr(srv, "_env_file_get", slow_get)

    errs = []

    def w(k, v):
        try:
            srv._env_file_set({k: v})
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    t1 = threading.Thread(target=w, args=("STT_A", "1"))
    t2 = threading.Thread(target=w, args=("STT_B", "2"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert not errs, errs
    env = config._env_file()
    assert env.get("STT_A") == "1" and env.get("STT_B") == "2", env


# ---------- /api/cloud_keys: set, clear, never echo ----------

def _clean_key_env(monkeypatch):
    """Hermetic: a real provider key in the developer's environment must not
    leak into availability checks (api_key falls back to os.environ)."""
    from stt import asr_cloud
    for meta in asr_cloud.PROVIDERS.values():
        monkeypatch.delenv(meta["key_env"], raising=False)


def test_cloud_keys_set_then_clear_removes_the_line(running_server, monkeypatch):
    _clean_key_env(monkeypatch)
    port = running_server
    st, body = _post(port, "/api/cloud_keys", {"scribe": "sk-test-123"})
    assert st == 200 and body["ok"] and body["set"]["scribe"] is True
    assert config._env_file().get("STT_ELEVENLABS_KEY") == "sk-test-123"

    st, body = _post(port, "/api/cloud_keys", {"clear": ["scribe"]})
    assert st == 200 and body["ok"] and body["set"]["scribe"] is False
    assert "STT_ELEVENLABS_KEY" not in config._env_file()


def test_cloud_keys_clear_keeps_other_keys_and_comments(running_server, monkeypatch):
    _clean_key_env(monkeypatch)
    port = running_server
    envp = config.PROJECT_DIR / "stt.env"
    envp.write_text("# provider keys\nSTT_ELEVENLABS_KEY=aaa\nSTT_OPENAI_KEY=bbb\n")
    st, body = _post(port, "/api/cloud_keys", {"clear": ["scribe"]})
    assert st == 200
    assert body["set"] == {"scribe": False, "openai": True, "voxtral": False}
    text = envp.read_text()
    assert "# provider keys" in text and "STT_OPENAI_KEY=bbb" in text
    assert "STT_ELEVENLABS_KEY" not in text


def test_cloud_keys_paste_wins_over_clear_and_empty_is_noop(running_server, monkeypatch):
    _clean_key_env(monkeypatch)
    port = running_server
    _post(port, "/api/cloud_keys", {"scribe": "old"})
    # the dialog posts all three fields — blank ones must not clear anything
    st, body = _post(port, "/api/cloud_keys", {"scribe": "", "openai": "", "voxtral": ""})
    assert st == 200 and body["set"]["scribe"] is True
    # paste + clear for the same provider in one request: the paste wins
    st, body = _post(port, "/api/cloud_keys", {"scribe": "new", "clear": ["scribe"]})
    assert body["set"]["scribe"] is True
    assert config._env_file()["STT_ELEVENLABS_KEY"] == "new"


def test_cloud_keys_never_echoed_to_the_client(running_server, monkeypatch):
    """Presence booleans only — the actual key value must never appear in any
    payload the page can read (the POST response or the state snapshot)."""
    _clean_key_env(monkeypatch)
    port = running_server
    secret = "sk-super-secret-value"
    st, body = _post(port, "/api/cloud_keys", {"openai": secret})
    assert st == 200 and secret not in json.dumps(body)
    st, state = _get(port, "/api/state")
    assert secret not in json.dumps(state)
    assert state["cloud_keys"] == {"scribe": False, "openai": True, "voxtral": False}


# ---------- /api/audio Range handling (finding #18) ----------

def _raw_get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    body = r.read()
    hdrs = {k: v for k, v in r.getheaders()}
    status = r.status
    conn.close()
    return status, hdrs, body


def _make_meeting_with_audio(base, payload: bytes):
    _make_meeting(base)
    (mfile(base, ".m4a")).write_bytes(payload)


def test_audio_range_suffix_and_unsatisfiable(running_server):
    payload = bytes(range(10))  # 10 known bytes: 00 01 .. 09
    _make_meeting_with_audio("Ranged", payload)
    q = "/api/audio?base=Ranged"

    # (a) suffix range: bytes=-4 must return the LAST 4 bytes
    status, hdrs, body = _raw_get(running_server, q, {"Range": "bytes=-4"})
    assert status == 206
    assert body == payload[-4:]
    assert hdrs.get("Content-Range") == "bytes 6-9/10"

    # (b) start past the end: unsatisfiable -> 416, empty body
    status, hdrs, body = _raw_get(running_server, q, {"Range": "bytes=100-"})
    assert status == 416
    assert hdrs.get("Content-Range") == "bytes */10"
    assert body == b""

    # (c) regression: a normal explicit range still works
    status, hdrs, body = _raw_get(running_server, q, {"Range": "bytes=2-5"})
    assert status == 206
    assert body == payload[2:6]
    assert hdrs.get("Content-Range") == "bytes 2-5/10"


# ---------- HTML/JS defects (findings #19, #12) ----------

def test_tvrow_escapes_flags_in_title_attribute():
    """tvRow's title interpolates flag text into an HTML attribute; a flag
    containing a quote (a speaker name flows into `possible:<name>`) would
    break out of the attribute without esc()."""
    html = srv.HTML
    assert "'Uncertain: '+esc(g.flags.join(', '))+' — tap to listen'" in html
    # the raw, un-escaped form must be gone
    assert "'Uncertain: '+g.flags.join(', ')+' — tap to listen'" not in html


def test_spkwirenew_restores_prior_selection_on_cancel():
    """Cancelling the New-person prompt must restore the segment's prior
    speaker, not silently jump to option 0 (misattributing the line)."""
    html = srv.HTML
    # remembers the prior real selection and restores it on cancel
    assert "sel._prev" in html
    assert "if(sel._prev!=null)sel.value=sel._prev;else sel.selectedIndex=0" in html
    # the old unconditional reset must be gone
    assert "if(!nm){sel.selectedIndex=0;return}" not in html


# ---------- /api/snippet: stale meeting reference falls back, never 400s ----------

def test_snippet_with_stale_meeting_falls_back_to_search(running_server, monkeypatch):
    """A registry that references a renamed/deleted meeting must not kill voice
    playback: the unknown-but-harmless meeting string is DISCARDED (never used
    as a path) and the clip search runs across the whole library instead."""
    calls = []

    def fake_snippet(meeting, speaker, secs=12.0):
        calls.append((meeting, speaker))
        return None  # no clip found — the endpoint should 404, not 400

    monkeypatch.setattr(srv, "_snippet_for", fake_snippet)
    st, body = _get(running_server,
                    "/api/snippet?speaker=U001&meeting=Renamed%20Away%20Mtg")
    assert st == 404 and body["error"] == "no snippet"
    assert calls == [("", "U001")]   # stale name dropped, library-wide search

    # a REAL meeting still passes through untouched
    _make_meeting("Real Mtg")
    calls.clear()
    st, _ = _get(running_server, "/api/snippet?speaker=U001&meeting=Real%20Mtg")
    assert st == 404 and calls == [("Real Mtg", "U001")]


# ---------- /api/voice_clips: per-meeting clips for the naming dialog ----------

def test_voice_clips_lists_each_meeting_and_skips_stale_names(running_server):
    """'Who is this?' shows this voice's longest turn per meeting it was heard
    in — meeting names resolve from the SERVER's unknown registry, and a stale
    (renamed-away) reference is skipped, never an error."""
    from stt import unknowns

    for base, uid_start in (("Mtg A", 3.0), ("Mtg B", 7.0)):
        (mfile(base, ".json")).write_text(json.dumps({
            "source_file": f"{base}.m4a", "duration_sec": 60.0, "strict": False,
            "speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 9",
                          "global_id": "U009", "match_score": None}],
            "segments": [
                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "name": None,
                 "display": "Speaker 9", "text": "short", "flags": [], "overlap": False},
                {"start": uid_start, "end": uid_start + 6.0, "speaker": "SPEAKER_00",
                 "name": None, "display": "Speaker 9", "text": "their longest turn here",
                 "flags": [], "overlap": False}],
            "words": []}))
        (mfile(base, ".txt")).write_text("stub")
    unknowns.save({"speakers": {"U009": {
        "file": "U009.npy", "meetings": ["Mtg A", "Mtg B", "Renamed Away Mtg"]}}})

    st, body = _get(running_server, "/api/voice_clips?speaker=U009")
    assert st == 200
    assert [(c["meeting"], c["index"]) for c in body["clips"]] == \
        [("Mtg A", 1), ("Mtg B", 1)]                 # longest turn's index, per meeting
    assert body["clips"][0]["start"] == 3.0 and body["clips"][0]["dur"] == 6.0

    st, body = _get(running_server, "/api/voice_clips?speaker=U404")
    assert st == 200 and body["clips"] == []          # unknown uid: empty, not error


def test_snippet_secs_is_clamped_and_never_past_the_turn(running_server, monkeypatch):
    """A longer clip request plays more of the SAME turn — capped at 45s and at
    the turn's real end, so it can't bleed into the next speaker's voice."""
    import subprocess as sp

    from stt import review
    monkeypatch.setattr(review, "find_voice_clips",
                        lambda key, mtg=None, n=1: [
                            {"base": "X", "start": 10.0, "dur": 100.0, "index": 0}])
    monkeypatch.setattr(srv, "_meeting_audio", lambda base: Path("/dev/null"))
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append([str(c) for c in cmd])
        Path(str(cmd[-1])).write_bytes(b"RIFFxxxx")
        return sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    st, _ = srv._snippet_for("", "U009", secs=600.0), None   # absurd request
    t = float(cmds[0][cmds[0].index("-t") + 1])
    assert t == 45.0                                  # clamped
    cmds.clear()
    monkeypatch.setattr(review, "find_voice_clips",
                        lambda key, mtg=None, n=1: [
                            {"base": "X", "start": 10.0, "dur": 4.0, "index": 0}])
    srv._snippet_for("", "U009", secs=45.0)
    t = float(cmds[0][cmds[0].index("-t") + 1])
    assert t == 4.0                                   # never past the turn's end
