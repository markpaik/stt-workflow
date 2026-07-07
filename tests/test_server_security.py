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

import pytest

from gui import server as srv
from stt import config


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
    (config.MEETINGS_DIR / f"{base}.json").write_text(json.dumps({
        "source_file": f"{base}.m4a", "duration_sec": 10.0, "strict": False,
        "speakers": [], "segments": [], "words": []}))
    (config.MEETINGS_DIR / f"{base}.txt").write_text("stub")


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
