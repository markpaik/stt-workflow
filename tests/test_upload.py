"""POST /api/upload — drag-and-drop's server side: one raw-body audio upload
lands in the primary watched folder (source_dir), where the existing watcher/
queue picks it up naturally. The name is client-supplied and BECOMES a file on
disk, so it runs through the house sanitizer + ' (N)' uniquify; the body
streams to a dot-prefixed .part tmp in the same directory and is os.replace'd
into place, so the watcher can never see a partial file."""
import http.client
import json
import time
import urllib.parse

from gui import server as srv
from stt import config
from conftest import mfile
# the running-server fixture and HTTP helpers live with the security tests
from test_server_security import _post, running_server  # noqa: F401


def _upload(port, name, body, headers=None):
    """POST the raw body to /api/upload?name=<name>; (status, json)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", "/api/upload?name=" + urllib.parse.quote(name),
                 body=body, headers=headers or {})
    r = conn.getresponse()
    out = r.read()
    conn.close()
    return r.status, json.loads(out)


def _source_names():
    return sorted(p.name for p in config.source_dir().iterdir())


# ---------- happy path ----------

def test_upload_lands_exact_bytes_and_joins_the_queue(running_server):
    payload = bytes(range(256)) * 40
    st, body = _upload(running_server, "Team Sync.m4a", payload)
    assert st == 200 and body == {"ok": True, "name": "Team Sync.m4a"}
    f = config.source_dir() / "Team Sync.m4a"
    assert f.read_bytes() == payload
    # no staging debris: the .part tmp was replaced, not copied
    assert _source_names() == ["Team Sync.m4a"]

    # the existing watcher/queue picks it up on the next state poll — it shows
    # as a plain waiting row, exactly like a file dropped in from Finder
    state = srv.gather_state()
    assert [q["name"] for q in state["queue"]] == ["Team Sync.m4a"]
    row = next(r for r in state["timeline"] if r["id"] == "src:Team Sync.m4a")
    assert row["state"] == "waiting"
    # and the queue-file gate (preview/hold/delete) recognizes it
    assert srv._queue_file("Team Sync.m4a") is not None


# ---------- filename sanitizing ----------

def test_upload_name_sanitizer_table(sandbox):
    """_upload_name is the gate that turns a hostile name into a safe basename
    (or refuses it): path components dropped, leading dots stripped, control
    chars removed, extension whitelisted case-insensitively."""
    ok = srv._upload_name
    assert ok("Team Sync.m4a") == "Team Sync.m4a"
    assert ok("Clip.M4A") == "Clip.m4a"                  # ext normalized
    assert ok("Board Audio.flac") == "Board Audio.flac"  # watcher formats accepted
    assert ok("Voice Memo.CAF") == "Voice Memo.caf"
    assert ok("Podcast.opus") == "Podcast.opus"
    assert ok("Screen Rec.webm") == "Screen Rec.webm"
    assert ok("../../etc/passwd.m4a") == "passwd.m4a"    # traversal -> basename
    assert ok("/etc/cron.d/evil.wav") == "evil.wav"      # absolute -> basename
    assert ok("..\\..\\evil.mp3") == "evil.mp3"          # backslash traversal
    assert ok(".hidden.m4a") == "hidden.m4a"             # never a dotfile
    assert ok("a\x00b\x1f.m4a") == "ab.m4a"              # control chars dropped
    assert ok("  spaced   name .wav") == "spaced name.wav"
    # refusals: bad/no extension, dot-only stems, nothing left after sanitize
    for bad in ("notes.txt", "clip.exe", "noext", ".m4a", "..m4a", "...",
                "../..", "/", "", None, "<>|?*.m4a"):
        assert ok(bad) is None, bad


def test_upload_allowlist_is_exactly_the_watcher_list(sandbox):
    """Anything droppable is anything watchable: the upload gate accepts every
    extension the folder watcher picks up (config.AUDIO_EXTS) and nothing
    else -- the two lists are the SAME set, not a subset."""
    for ext in config.AUDIO_EXTS:
        assert srv._upload_name("Clip" + ext) == "Clip" + ext, ext
        assert srv._upload_name("Clip" + ext.upper()) == "Clip" + ext, ext
    # a format the watcher ignores is refused, so a drop can never land a file
    # the pipeline would silently skip
    for ext in (".txt", ".pdf", ".zip", ".json", ".part"):
        assert ext not in config.AUDIO_EXTS
        assert srv._upload_name("Clip" + ext) is None, ext


def test_upload_traversal_names_land_inside_the_watched_folder(running_server, tmp_path):
    st, body = _upload(running_server, "../../outside.m4a", b"x" * 64)
    assert st == 200 and body["name"] == "outside.m4a"
    assert (config.source_dir() / "outside.m4a").exists()
    assert not (config.source_dir().parent / "outside.m4a").exists()
    assert not (tmp_path / "outside.m4a").exists()

    st, body = _upload(running_server, ".sneaky.m4a", b"x" * 64)
    assert st == 200 and body["name"] == "sneaky.m4a"    # dotfile de-dotted
    assert not any(n.startswith(".") for n in _source_names())


def test_upload_wrong_or_missing_extension_is_rejected(running_server):
    for name in ("notes.txt", "payload.exe", "noext", ".m4a", "../../etc/passwd"):
        st, body = _upload(running_server, name, b"x" * 16)
        assert st == 400 and "error" in body, name
    # missing name param entirely
    st, body = _post(running_server, "/api/upload", {})
    assert st == 400 and "error" in body
    assert not config.source_dir().exists() or _source_names() == []


# ---------- size gates ----------

def test_upload_empty_body_is_rejected(running_server):
    st, body = _upload(running_server, "Empty.m4a", b"")
    assert st == 400 and "empty" in body["error"]
    assert not config.source_dir().exists() or _source_names() == []


def test_upload_over_two_gb_is_refused_without_reading_the_body(running_server):
    # fake the header — the server must answer 413 from Content-Length alone
    st, body = _upload(running_server, "Huge.m4a", b"",
                       headers={"Content-Length": str(2 * 1024 ** 3 + 1)})
    assert st == 413 and "large" in body["error"]
    assert not config.source_dir().exists() or _source_names() == []
    # exactly at the cap is still allowed (checked via the gate, not 2GB of IO)
    assert srv._UPLOAD_MAX == 2 * 1024 ** 3


# ---------- collisions ----------

def test_upload_collision_uniquifies_never_overwrites(running_server):
    st, body = _upload(running_server, "Sync.m4a", b"first")
    assert body["name"] == "Sync.m4a"
    st, body = _upload(running_server, "Sync.m4a", b"second")
    assert st == 200 and body["name"] == "Sync (2).m4a"  # house ' (N)' rule
    st, body = _upload(running_server, "Sync.m4a", b"third")
    assert body["name"] == "Sync (3).m4a"
    assert (config.source_dir() / "Sync.m4a").read_bytes() == b"first"
    assert (config.source_dir() / "Sync (2).m4a").read_bytes() == b"second"
    assert (config.source_dir() / "Sync (3).m4a").read_bytes() == b"third"


def test_upload_never_collides_with_an_existing_meeting_name(running_server):
    """Same rule as recorder._uniquify: a stem matching an existing meeting
    would land the batch's output in that meeting's folder — uniquify instead
    (a deliberate redo goes through the Redo path, not a name collision)."""
    mfile("Weekly", ".json").write_text("{}")
    st, body = _upload(running_server, "Weekly.m4a", b"x" * 32)
    assert st == 200 and body["name"] == "Weekly (2).m4a"


# ---------- partial upload: the watcher must never see it ----------

def test_broken_upload_leaves_no_visible_file_and_no_part_debris(running_server):
    """Lie about Content-Length, send a fraction, drop the connection. The tmp
    is dot-prefixed AND .part-suffixed — invisible to the watcher either way —
    and it must be unlinked, leaving the folder exactly as it was."""
    conn = http.client.HTTPConnection("127.0.0.1", running_server, timeout=5)
    conn.putrequest("POST", "/api/upload?name=Broken.m4a")
    conn.putheader("Content-Length", "4096")
    conn.endheaders()
    conn.send(b"x" * 64)  # a fraction of the promised body
    conn.close()

    deadline = time.time() + 3.0
    while time.time() < deadline:
        leftover = list(config.source_dir().iterdir()) if config.source_dir().exists() else []
        if not leftover:
            break
        time.sleep(0.05)
    assert leftover == [], leftover  # no Broken.m4a, no .upload-*.part
    # and the queue never surfaced anything
    assert srv.gather_state()["queue"] == []


# ---------- origin gate (house rule: every POST) ----------

def test_upload_is_origin_gated(running_server):
    st, body = _upload(running_server, "Evil.m4a", b"x" * 16,
                       headers={"Origin": "https://evil.example.com"})
    assert st == 403
    assert not config.source_dir().exists() or _source_names() == []
