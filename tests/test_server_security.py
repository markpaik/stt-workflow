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


# ---------- speaker-mutation endpoints (G2) ----------

def test_name_endpoint_rejects_an_unknown_meeting_base(running_server):
    """G2: enroll-from-meeting turns the client's meeting into a .diar.npz path,
    so it must gate the base the same way transcript/audio/rename do."""
    status, _ = _post(running_server, "/api/name",
                      {"meeting": "../../etc/passwd", "speaker": "SPEAKER_00", "name": "X"})
    assert status == 400


def _make_meeting_with_cache(base, cluster_turns, cent_emb):
    """A meeting plus the .diar.npz relabel cache: cluster_turns is
    {label: [(start, end), ...]}, cent_emb {label: vector}."""
    import numpy as np

    from stt import diarcache
    _make_meeting(base)
    raw = sorted(({"start": s, "end": e, "cluster": lbl}
                  for lbl, spans in cluster_turns.items() for s, e in spans),
                 key=lambda t: t["start"])
    diarcache.save(mfile(base, ".diar.npz"), raw, [None] * len(raw),
                   {k: np.asarray(v, float) for k, v in cent_emb.items()})


def test_name_endpoint_refuses_enrolling_a_thin_cluster(running_server):
    """The enrollment quality gate: a cluster with seconds of speech cannot
    identify anyone — the endpoint refuses with the plain-language error the
    naming panel shows inline, and the registry stays untouched."""
    import numpy as np

    from stt import identify
    v = np.random.default_rng(41).normal(size=256)
    _make_meeting_with_cache("Thin Mtg",
                             {"SPEAKER_00": [(0.0, 2.0), (5.0, 7.0), (9.0, 11.0)]},
                             {"SPEAKER_00": v})
    status, body = _post(running_server, "/api/name",
                         {"meeting": "Thin Mtg", "speaker": "SPEAKER_00",
                          "name": "Somebody New"})
    assert status == 200 and body["ok"] is False
    assert "too little to identify anyone reliably" in body["error"]
    assert "Somebody New" not in identify.load_registry()


def test_name_endpoint_requires_confirm_for_a_suspect_sample(running_server, monkeypatch):
    """The same-meeting-different-cluster gate: enrolling meeting M's OTHER
    cluster onto a person whose stack came from M is almost always the wrong
    voice. First call returns a warning with the scores; an explicit
    confirm=true second call proceeds (the API stays additive)."""
    import numpy as np

    from stt import identify
    spawned = []
    monkeypatch.setattr(srv, "_spawn", lambda cmd: spawned.append(cmd))
    rng = np.random.default_rng(43)
    priya = rng.normal(size=256)
    priya /= np.linalg.norm(priya)
    other = rng.normal(size=256)
    other -= (other @ priya) * priya
    other /= np.linalg.norm(other)
    spans = [(i * 4.0, i * 4.0 + 3.0) for i in range(12)]      # over the floor
    _make_meeting_with_cache("Pair Mtg",
                             {"SPEAKER_00": spans,
                              "SPEAKER_01": [(s + 60, e + 60) for s, e in spans]},
                             {"SPEAKER_00": priya, "SPEAKER_01": other})
    identify.enroll("Priya Shah", priya, source="Pair Mtg")

    status, body = _post(running_server, "/api/name",
                         {"meeting": "Pair Mtg", "speaker": "SPEAKER_01",
                          "name": "Priya Shah"})
    assert status == 200 and body["ok"] is False and body.get("warn")
    assert "own" in body and body["own"] < 0.45
    assert identify.load_registry()["Priya Shah"]["n_samples"] == 1  # untouched

    status, body = _post(running_server, "/api/name",
                         {"meeting": "Pair Mtg", "speaker": "SPEAKER_01",
                          "name": "Priya Shah", "confirm": True})
    assert status == 200 and body["ok"] is True
    assert identify.load_registry()["Priya Shah"]["n_samples"] == 2
    assert spawned, "a confirmed enrollment must still spawn the relabel"


def test_speaker_mutations_are_blocked_from_a_foreign_origin(running_server):
    """G2: every speaker-registry mutation is a POST, so the centralized origin
    gate refuses a cross-site request before the handler runs. Spot-check the
    registry-wide ones that a CSRF could otherwise use to rename or drop people."""
    for path, payload in [
        ("/api/rename_speaker", {"name": "A", "new": "B"}),
        ("/api/remove_speaker", {"name": "A"}),
        ("/api/merge_speakers", {"src": "uid:U001", "dst": "name:B"}),
        ("/api/forget", {"uid": "U001"}),
    ]:
        status, _ = _post(running_server, path, payload,
                          headers={"Origin": "https://evil.example.com"})
        assert status == 403, f"{path} not origin-gated"


# ---------- queue preview / delete ----------

def test_queue_file_gate_refuses_anything_outside_the_watched_folders(sandbox):
    """Queue items have no meeting, so there is no base to gate on — _queue_file
    IS their gate. It must accept only real audio sitting directly in a watched
    folder, and refuse traversal, absolute paths, dotfiles (an in-progress
    .rec-*.caf capture!), non-audio, and symlinks pointing out of the folder."""
    from stt import config
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    (src / "Real Meeting.m4a").write_bytes(b"audio")
    (src / "notes.txt").write_bytes(b"x")
    (src / ".rec-live.caf").write_bytes(b"x")
    secret = config.PROJECT_DIR / "secret.m4a"
    secret.write_bytes(b"x")
    (src / "escape.m4a").symlink_to(secret)          # symlink out of the folder

    assert srv._queue_file("Real Meeting.m4a") is not None
    for bad in ("../../../../etc/passwd", "/etc/passwd", "../secret.m4a",
                "notes.txt", ".rec-live.caf", "escape.m4a", "nope.m4a", "", None):
        assert srv._queue_file(bad) is None, bad


def test_queue_audio_endpoint_refuses_traversal(running_server):
    status, _ = _get(running_server, "/api/queue_audio?name=../../etc/passwd")
    assert status == 400
    status, _ = _get(running_server, "/api/queue_audio?name=nope.m4a")
    assert status == 400


def test_queue_delete_gates_confirm_membership_and_active(running_server):
    from stt import config, status as st
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    f = src / "Bad Take.m4a"
    f.write_bytes(b"audio")
    (src / "Bad Take.opts.json").write_bytes(b"{}")   # the recorder's sidecar

    status, body = _post(running_server, "/api/queue_delete", {"name": "Bad Take.m4a"})
    assert status == 200 and not body["ok"] and "confirm" in body["error"]
    assert f.exists()
    status, body = _post(running_server, "/api/queue_delete",
                         {"name": "../../etc/passwd", "confirm": True})
    assert status == 200 and not body["ok"]
    # a file the batch is writing right now is never yanked out from under it
    st.set_stage("Bad Take.m4a", "transcribing")
    status, body = _post(running_server, "/api/queue_delete",
                         {"name": "Bad Take.m4a", "confirm": True})
    assert status == 200 and not body["ok"] and "processed" in body["error"]
    assert f.exists()
    st.clear_stage("Bad Take.m4a")
    status, body = _post(running_server, "/api/queue_delete",
                         {"name": "Bad Take.m4a", "confirm": True})
    assert status == 200 and body["ok"]
    assert not f.exists()
    assert not (src / "Bad Take.opts.json").exists()  # sidecar went with it


def test_queue_endpoints_are_origin_gated(running_server):
    status, _ = _get(running_server, "/api/queue_audio?name=x.m4a",
                     headers={"Origin": "https://evil.example.com"})
    assert status == 403
    status, _ = _post(running_server, "/api/queue_delete",
                      {"name": "x.m4a", "confirm": True},
                      headers={"Origin": "https://evil.example.com"})
    assert status == 403


# ---------- recorder permission fix ----------

def test_fix_recorder_permissions_is_scoped_to_our_bundle_only(running_server, monkeypatch):
    """The endpoint may ONLY reset TCC for com.stt-workflow.recorder — never a
    bare service-wide reset (which would revoke every app's grant)."""
    from gui import server as srv
    calls = []

    class R:
        returncode = 0
        stdout = stderr = ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return R()
    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    status, body = _post(running_server, "/api/fix_recorder_permissions", {})
    assert status == 200 and body["ok"]
    assert len(calls) == 2
    for argv in calls:
        assert argv[0] == "/usr/bin/tccutil" and argv[1] == "reset"
        assert argv[-1] == "com.stt-workflow.recorder"   # always scoped
    # and it is origin-gated like every other POST
    status, _ = _post(running_server, "/api/fix_recorder_permissions", {},
                      headers={"Origin": "https://evil.example.com"})
    assert status == 403


# ---------- archive / category endpoints ----------

def test_archive_category_endpoints_reject_unknown_base(running_server):
    """set_category and archive_meeting turn the client's base into a path, so
    they gate on live-meeting membership like every other base endpoint."""
    for path, payload in [
        ("/api/set_category", {"base": "../../etc/passwd", "category": "work"}),
        ("/api/archive_meeting", {"base": "../../etc/passwd"}),
    ]:
        status, _ = _post(running_server, path, payload)
        assert status == 400, path
    # restore/delete gate on ARCHIVED (or either) membership inside the handler,
    # so they answer 200 with ok:false rather than 400 — never touching a path
    status, body = _post(running_server, "/api/restore_meeting", {"base": "../../etc/passwd"})
    assert status == 200 and not body["ok"]
    status, body = _post(running_server, "/api/delete_meeting",
                         {"base": "../../etc/passwd", "confirm": True})
    assert status == 200 and not body["ok"]


def test_delete_requires_explicit_confirmation(running_server):
    _make_meeting("Legit Meeting")
    status, body = _post(running_server, "/api/delete_meeting", {"base": "Legit Meeting"})
    assert status == 200 and not body["ok"] and "confirm" in body["error"]
    from stt import config
    assert "Legit Meeting" in config.meeting_bases()  # still there


def test_archived_meeting_stops_answering_on_the_live_endpoints(running_server):
    """Archiving is enforced by the membership gate, so an archived meeting must
    fall out of transcript/export/ask — not just out of the rendered list."""
    from stt import config
    _make_meeting("Secret Meeting")
    status, _ = _get(running_server, "/api/transcript?base=Secret%20Meeting")
    assert status == 200
    status, body = _post(running_server, "/api/archive_meeting", {"base": "Secret Meeting"})
    assert status == 200 and body["ok"]
    assert config.archived_bases() == ["Secret Meeting"]
    for path in ("/api/transcript?base=Secret%20Meeting", "/api/txt?base=Secret%20Meeting"):
        status, _ = _get(running_server, path)
        assert status == 400, path
    status, _ = _post(running_server, "/api/export", {"base": "Secret Meeting", "fmt": "docx"})
    assert status == 400
    # and it comes back cleanly
    status, body = _post(running_server, "/api/restore_meeting", {"base": "Secret Meeting"})
    assert status == 200 and body["ok"]
    status, _ = _get(running_server, "/api/transcript?base=Secret%20Meeting")
    assert status == 200


def test_archive_endpoints_are_blocked_from_a_foreign_origin(running_server):
    for path, payload in [("/api/archive_meeting", {"base": "x"}),
                          ("/api/restore_meeting", {"base": "x"}),
                          ("/api/delete_meeting", {"base": "x", "confirm": True}),
                          ("/api/set_category", {"base": "x", "category": "work"})]:
        status, _ = _post(running_server, path, payload,
                          headers={"Origin": "https://evil.example.com"})
        assert status == 403, path


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
    monkeypatch.delenv("STT_ANTHROPIC_KEY", raising=False)  # assistant key


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
    assert body["set"] == {"scribe": False, "openai": True, "voxtral": False,
                           "anthropic": False}
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
    assert state["cloud_keys"] == {"scribe": False, "openai": True,
                                   "voxtral": False, "anthropic": False}


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

def _shell_js():
    return (Path(srv.__file__).resolve().parent / "static" / "app.js"
            ).read_text(encoding="utf-8")


def test_flag_title_attribute_escapes_flag_text():
    """The transcript's uncertainty marker interpolates flag text into an HTML
    title attribute; a flag containing a quote (a speaker name flows into
    `possible:<name>`) would break out of the attribute without esc()."""
    js = _shell_js()
    assert 'title="Uncertain: ${esc((g.flags||[]).join(\', \'))}"' in js
    # the raw, un-escaped form must never appear
    assert 'title="Uncertain: ${(g.flags||[]).join(\', \')}"' not in js


def test_speaker_select_restores_prior_selection_on_cancel():
    """Cancelling the New-person prompt must restore the segment's prior
    speaker, not silently jump to option 0 (misattributing the line)."""
    js = _shell_js()
    # remembers the prior real selection and restores it on cancel
    assert "sel._prev" in js
    assert "if(sel._prev!=null)sel.value=sel._prev;else sel.selectedIndex=0" in js
    # the unconditional reset must never return
    assert "if(!nm){sel.selectedIndex=0;return}" not in js


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
    assert "reason" not in body       # no refs at all is not "sources deleted"


def test_voice_clips_says_why_when_every_source_meeting_was_deleted(running_server):
    """An unknown whose every 'heard in' meeting is gone (not live, not
    archived) has no audio anywhere: the endpoint must say WHY the list is
    empty, and the panel must agree by not advertising the voice at all — no
    'heard in 2 meetings' badge sitting over zero playable clips."""
    from stt import unknowns
    unknowns.save({"speakers": {"U005": {
        "file": "U005.npy", "meetings": ["Deleted Mtg A", "Deleted Mtg B"]}}})

    st, body = _get(running_server, "/api/voice_clips?speaker=U005")
    assert st == 200
    assert body["clips"] == [] and body["reason"] == "sources_deleted"

    state = srv.gather_state()   # the panel's count agrees: hidden, i.e. zero
    assert [u["uid"] for u in state["unknowns"]] == []
    assert [t for t in state["tray"] if t["kind"] == "unknown_voice"] == []


def test_voice_clips_archived_refs_are_not_reported_deleted(running_server):
    """Archived is NOT deleted — a restore brings playback straight back, so
    the 'recordings were deleted' reason must not show while a ref still
    resolves into the archive."""
    from stt import archive, unknowns
    _make_meeting("Shelved Mtg")
    (mfile("Shelved Mtg", ".m4a")).write_bytes(b"audio")
    assert archive.archive_meeting("Shelved Mtg")["ok"]
    unknowns.save({"speakers": {"U006": {
        "file": "U006.npy", "meetings": ["Shelved Mtg"]}}})

    st, body = _get(running_server, "/api/voice_clips?speaker=U006")
    assert st == 200
    assert body["clips"] == [] and "reason" not in body


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


def test_enrolled_speakers_listed_alphabetically(sandbox):
    """The Speakers card lists people by first name (registry order was
    enrollment order); unknowns render in their own section below."""
    import numpy as np

    from stt import identify
    rng = np.random.default_rng(5)
    for name in ("Priya Shah", "alex rivera", "Jordan Lee"):
        identify.enroll(name, rng.normal(size=16), source="M1")
    st = srv.gather_state()
    assert [e["name"] for e in st["enrolled"]] == \
        ["alex rivera", "Jordan Lee", "Priya Shah"]   # case-insensitive


def test_llm_backend_endpoint_switches_and_validates(running_server, monkeypatch):
    """Settings picker: writes STT_LLM_BACKEND, refuses unknown backends and
    ones whose key/venv is missing."""
    from stt import summarize
    st, body = _post(running_server, "/api/llm_backend", {"backend": "bogus"})
    assert st == 400
    st, body = _post(running_server, "/api/llm_backend", {"backend": "anthropic"})
    assert st == 400 and "key" in body["error"]          # no key yet
    _post(running_server, "/api/cloud_keys", {"anthropic": "sk-ant-test"})
    st, body = _post(running_server, "/api/llm_backend", {"backend": "anthropic"})
    assert st == 200 and body["ok"]
    assert config._env_file()["STT_LLM_BACKEND"] == "anthropic"
    assert summarize.llm_backend() == "anthropic"
    st, state = _get(running_server, "/api/state")
    assert state["llm_backend"] == "anthropic"
    assert state["llm_backends"]["anthropic"] is True
    assert state["llm_available"] is True


# ---------- independent automation triggers (folder watch vs nightly) ----------

@pytest.fixture
def fake_agent(sandbox, monkeypatch):
    """A sandboxed launchd plist with both triggers on, reloads recorded."""
    import plistlib
    p = sandbox / "com.stt-workflow.batch.plist"
    p.write_bytes(plistlib.dumps({
        "Label": "com.stt-workflow.batch",
        "ProgramArguments": ["/x/python", "/x/run_batch.py"],
        "StartCalendarInterval": {"Hour": 2, "Minute": 15},
        "WatchPaths": ["/old/watched/folder"],
        "RunAtLoad": True}))
    monkeypatch.setattr(srv, "AGENT", p)
    reloads = []
    monkeypatch.setattr(srv, "_agent_reload", lambda: reloads.append(1))
    return p, reloads


def test_watch_and_nightly_toggle_independently(fake_agent):
    """The user's model: manual vs always-watching is one switch, the
    scheduled run is its own — either can be off while the other stays on."""
    import plistlib
    p, reloads = fake_agent

    r = srv.write_automation(watch=False)
    assert r["ok"] and r["watch"] is False and r["nightly"] is True
    d = plistlib.loads(p.read_bytes())
    assert "WatchPaths" not in d and d["StartCalendarInterval"]["Hour"] == 2
    assert d["RunAtLoad"] is True          # nightly still needs login catch-up

    r = srv.write_automation(nightly=False)
    assert r["nightly"] is False
    d = plistlib.loads(p.read_bytes())
    assert "StartCalendarInterval" not in d
    assert d["RunAtLoad"] is False         # fully manual: nothing runs itself
    assert config._env_file()["STT_SCHEDULE_SAVED"] == "2:15"

    # re-enable nightly: the remembered time comes back
    r = srv.write_automation(nightly=True)
    d = plistlib.loads(p.read_bytes())
    assert d["StartCalendarInterval"] == {"Hour": 2, "Minute": 15}
    assert d["RunAtLoad"] is True

    # re-enable watch: stamps the CURRENT source folder AND the recordings
    # staging folder (the meeting recorder's output), healing stale paths
    r = srv.write_automation(watch=True)
    d = plistlib.loads(p.read_bytes())
    assert d["WatchPaths"] == [str(config.source_dir()), str(config.recordings_dir())]
    assert len(reloads) == 4               # every change reloaded the agent


def test_setting_a_time_implies_nightly_on(fake_agent):
    import plistlib
    p, _ = fake_agent
    srv.write_automation(nightly=False)
    srv.write_schedule(23, 30)             # the Change… dialog path
    d = plistlib.loads(p.read_bytes())
    assert d["StartCalendarInterval"] == {"Hour": 23, "Minute": 30}
    assert srv.read_schedule()["nightly"] is True


def test_automation_endpoint_round_trip(running_server, fake_agent):
    st, body = _post(running_server, "/api/automation", {"watch": False})
    assert st == 200 and body["ok"] and body["watch"] is False
    st, state = _get(running_server, "/api/state")
    assert state["schedule"]["watch"] is False
    assert state["schedule"]["nightly"] is True


def test_automation_requires_the_agent(sandbox, monkeypatch):
    monkeypatch.setattr(srv, "AGENT", sandbox / "missing.plist")
    r = srv.write_automation(watch=False)
    assert r["ok"] is False and "install-agent" in r["error"]


# ---------- STT_HOME sandboxes the launchd agent (QA bit the real one) ----------

def test_agent_path_follows_stt_home_and_defaults_unchanged(tmp_path):
    """AGENT is derived at import time like every config path, so a fresh
    interpreter proves the derivation: with a monkeypatched HOME and no
    STT_HOME it is exactly the old ~/Library/LaunchAgents path; with STT_HOME
    it moves under the sandbox, so /api/schedule from a QA server can never
    name the real machine's plist."""
    import os
    import subprocess
    import sys
    repo = Path(srv.__file__).resolve().parent.parent
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env["PYTHONPATH"] = str(repo)
    env["HOME"] = str(fake_home)
    code = "from gui import server as srv; print(srv.AGENT)"
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(repo),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(
        fake_home / "Library/LaunchAgents/com.stt-workflow.batch.plist")

    env["STT_HOME"] = str(tmp_path / "home")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(repo),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str(
        tmp_path / "home" / "LaunchAgents" / "com.stt-workflow.batch.plist")


@pytest.fixture
def sandboxed_agent(sandbox, monkeypatch):
    """The STT_HOME QA shape, in-process: config.STT_HOME active, AGENT under
    the sandbox's LaunchAgents/ (what tools/demo_seed.py now seeds), and every
    subprocess recorded so a launchctl call cannot hide."""
    import plistlib
    p = sandbox / "LaunchAgents" / "com.stt-workflow.batch.plist"
    p.parent.mkdir()
    p.write_bytes(plistlib.dumps({
        "Label": "com.stt-workflow.batch",
        "ProgramArguments": ["/usr/bin/true"],
        "StartCalendarInterval": {"Hour": 2, "Minute": 0},
        "RunAtLoad": True}))
    monkeypatch.setattr(srv, "AGENT", p)
    monkeypatch.setattr(config, "STT_HOME", str(sandbox))
    calls = []
    monkeypatch.setattr(srv.subprocess, "run",
                        lambda cmd, **kw: calls.append([str(c) for c in cmd]))
    return p, calls


def test_sandboxed_schedule_writes_the_sandbox_plist_only(sandboxed_agent):
    """/api/schedule under STT_HOME: the sandbox plist changes, launchctl is
    never invoked, and the REAL ~/Library/LaunchAgents plist stays untouched
    (mutating the operator's live agent from QA is the 2026-07-12 incident)."""
    import plistlib
    p, calls = sandboxed_agent
    real = Path.home() / "Library/LaunchAgents/com.stt-workflow.batch.plist"
    real_before = real.read_bytes() if real.exists() else None

    srv.write_schedule(23, 30)                 # the /api/schedule handler's call
    d = plistlib.loads(p.read_bytes())
    assert d["StartCalendarInterval"] == {"Hour": 23, "Minute": 30}
    assert calls == []                         # no launchctl (or any subprocess)

    r = srv.write_automation(watch=False)      # the /api/automation path
    assert r["ok"] is True and r["watch"] is False
    assert "sandboxed" in r.get("note", "")    # the response says launchctl was skipped
    assert calls == []

    real_after = real.read_bytes() if real.exists() else None
    assert real_before == real_after           # the real agent: byte-identical


def test_history_endpoint_returns_full_merged_history(running_server):
    from stt import status
    status.start_run(["a.m4a", "b.m4a"])
    status.finish_file("a.m4a", True, "2 speaker(s)")
    status.finish_file("b.m4a", False, "boom")
    st, body = _get(running_server, "/api/history")
    assert st == 200
    names = [r["name"] for r in body["results"]]
    assert names == ["b.m4a", "a.m4a"]
    assert body["results"][0]["ok"] is False


def test_mic_speaker_endpoint_persists_and_clears(running_server):
    from stt import config
    st, body = _post(running_server, "/api/mic_speaker", {"name": "Mark Paik"})
    assert st == 200 and body["mic_speaker"] == "Mark Paik"
    assert config.mic_speaker() == "Mark Paik"
    st, body = _post(running_server, "/api/mic_speaker", {"name": ""})
    assert body["mic_speaker"] is None
    assert config.mic_speaker() is None
    # never echoed as an env dump; state carries only the name
    _, state = _get(running_server, "/api/state")
    assert "mic_speaker" in state
