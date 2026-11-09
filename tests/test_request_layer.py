"""gui/server.py's request layer: one place that turns a request the client
got wrong into HTTP 400 with a plain message, and an error nobody planned
for into a 500 that carries no exception text and no server path.

Before it, every handler parsed its own body and read its own fields, and
the catch-all answered every exception as {"error": str(e)} with status 500:
'Expecting value: line 1 column 1 (char 0)', "'list' object has no attribute
'get'", "'new'", and on a corrupt diar cache the server's absolute path."""
import http.client
import inspect
import json
import re
import subprocess
import threading

import pytest

from gui import server as srv
from stt import config, control, status
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


def _raw(port, method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, (json.loads(data) if data else None)


def _post(port, path, payload):
    return _raw(port, "POST", path, json.dumps(payload).encode(),
                {"Content-Type": "application/json"})


def _get(port, path):
    return _raw(port, "GET", path)


def _make_meeting(base, **extra):
    mfile(base, ".json").write_text(json.dumps({
        "source_file": f"{base}.m4a", "duration_sec": 21.0, "strict": False,
        "speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 1"}],
        "segments": [{"start": 1.0, "end": 3.0, "speaker": "SPEAKER_00",
                      "text": "hello there"}],
        "words": [], **extra}))
    mfile(base, ".txt").write_text("stub")


def post_routes() -> list:
    """Every POST route, read from do_POST itself, so a route added later is
    walked without anyone remembering to list it here."""
    src = inspect.getsource(srv.Handler.do_POST)
    return sorted(set(re.findall(r'u\.path == "(/api/[a-z_]+)"', src)))


def test_the_route_list_is_real():
    routes = post_routes()
    # a broken regex must not turn the walk below into a test of nothing
    assert len(routes) > 40, routes
    for r in ("/api/run", "/api/review", "/api/name", "/api/bulk",
              "/api/delete_meeting", "/api/ask_library", "/api/schedule"):
        assert r in routes


MALFORMED = [
    (b"notjson", "the request body is not valid JSON"),
    (b"{bad", "the request body is not valid JSON"),
    (b'{"a": NaN}', "the request body is not valid JSON"),
    (b'{"a": Infinity}', "the request body is not valid JSON"),
    (b"\xff\xfe\xfd", "the request body is not valid JSON"),
    (b"[" * 100000, "the request body is not valid JSON"),
    (b"[]", "the request body must be a JSON object"),
    (b'"text"', "the request body must be a JSON object"),
    (b"123", "the request body must be a JSON object"),
    (b"null", "the request body must be a JSON object"),
]


def test_every_post_route_answers_a_malformed_body_with_400(running_server,
                                                           monkeypatch):
    """The walk: every JSON route, every kind of body that is not one JSON
    object. 400 with one of two plain sentences, never a 500, and nothing
    runs: the body is refused before the route is dispatched."""
    ran = []

    def tripwire(name):
        def f(*a, **k):
            ran.append(name)
            raise AssertionError(f"{name} ran on a malformed body")
        return f
    # the routes with effects outside the sandbox: none may be reached
    monkeypatch.setattr(srv, "pick_folder", tripwire("pick_folder"))
    monkeypatch.setattr(srv, "pick_files", tripwire("pick_files"))
    monkeypatch.setattr(srv, "_spawn", tripwire("_spawn"))
    monkeypatch.setattr(control, "stop_run", tripwire("stop_run"))
    monkeypatch.setattr(srv.subprocess, "run", tripwire("subprocess.run"))
    failures = []
    for route in post_routes():
        if route == "/api/upload":
            continue        # the body is the audio file itself, not JSON
        for body, msg in MALFORMED:
            st, out = _raw(running_server, "POST", route, body,
                           {"Content-Type": "application/json"})
            if st != 400 or out != {"error": msg}:
                failures.append((route, body[:12], st, out))
    assert failures == []
    assert ran == []


def test_an_empty_body_is_still_an_empty_object(running_server):
    """Several buttons post no body at all: that stays {}, as before."""
    st, out = _raw(running_server, "POST", "/api/pause", b"")
    assert st == 200 and out == {"ok": True}
    st, out = _raw(running_server, "POST", "/api/resume", b"  \n")
    assert st == 200 and out == {"ok": True}


def test_a_body_over_the_limit_is_refused_unread(running_server):
    conn = http.client.HTTPConnection("127.0.0.1", running_server, timeout=10)
    conn.putrequest("POST", "/api/rename")
    conn.putheader("Content-Length", str(srv._BODY_MAX + 1))
    conn.endheaders()
    conn.send(b"{}")
    r = conn.getresponse()
    assert r.status == 413
    assert json.loads(r.read()) == {"error": "the request body is too large"}
    conn.close()


def test_a_bad_content_length_is_a_400(running_server):
    conn = http.client.HTTPConnection("127.0.0.1", running_server, timeout=10)
    conn.putrequest("POST", "/api/pause")
    conn.putheader("Content-Length", "lots")
    conn.endheaders()
    r = conn.getresponse()
    assert r.status == 400
    assert json.loads(r.read()) == {"error": "Content-Length must be a number"}
    conn.close()


def test_a_missing_field_names_the_field(running_server):
    """A required field the client left out is a 400 that names it. It was a
    500 whose whole body was the bare KeyError text: {"error": "'new'"}."""
    _make_meeting("2026-04-08 Team Weekly")
    st, out = _post(running_server, "/api/rename", {"base": "2026-04-08 Team Weekly"})
    assert st == 400 and out == {"error": "missing field 'new'"}
    st, out = _post(running_server, "/api/export", {"base": "2026-04-08 Team Weekly"})
    assert st == 400 and out == {"error": "missing field 'fmt'"}
    st, out = _post(running_server, "/api/unqueue", {})
    assert st == 400 and out == {"error": "missing field 'at'"}


def test_snippet_without_a_speaker_is_a_400(running_server):
    st, out = _get(running_server, "/api/snippet?base=nonexistent&t=1")
    assert st == 400 and out == {"error": "missing field 'speaker'"}


def test_an_unplanned_error_answers_a_fixed_sentence_and_logs_the_detail(
        running_server, monkeypatch, capsys):
    """A 500 carries no exception text and no path. The route, the exception
    and the traceback go to the panel log (stderr) instead."""
    secret = "/Users/someone/private/2026-04-08 Team Weekly.json"

    def boom(*a, **k):
        raise OSError(2, "No such file or directory", secret)
    monkeypatch.setattr(status, "history", boom)
    monkeypatch.setattr(control, "pause", boom)
    st, out = _get(running_server, "/api/history")
    assert st == 500 and out == {"error": srv.INTERNAL_ERROR}
    st, out = _post(running_server, "/api/pause", {})
    assert st == 500 and out == {"error": srv.INTERNAL_ERROR}
    err = capsys.readouterr().err
    assert "panel: GET /api/history failed" in err
    assert "panel: POST /api/pause failed" in err
    assert secret in err and "Traceback" in err


def test_a_failed_bulk_item_names_no_path(running_server, monkeypatch, capsys):
    from stt import archive
    _make_meeting("2026-04-08 Team Weekly")

    def boom(base):
        raise PermissionError(13, "Permission denied",
                              str(config.meeting_dir(base)))
    monkeypatch.setattr(archive, "archive_meeting", boom)
    st, out = _post(running_server, "/api/bulk",
                    {"action": "archive", "bases": ["2026-04-08 Team Weekly"]})
    assert st == 200 and out["results"][0]["ok"] is False
    assert str(config.PROJECT_DIR) not in json.dumps(out)
    assert "Permission denied" not in json.dumps(out)
    assert str(config.meeting_dir("2026-04-08 Team Weekly")) in capsys.readouterr().err


def test_a_failed_second_listen_names_no_path(running_server, monkeypatch, capsys):
    _make_meeting("2026-04-08 Team Weekly")
    trace = ('Traceback (most recent call last):\n  File "/Users/someone/'
             'checkout/stt/retranscribe.py", line 9\nRuntimeError: boom')
    monkeypatch.setattr(srv.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1, stdout="", stderr=trace))
    st, out = _post(running_server, "/api/retranscribe",
                    {"base": "2026-04-08 Team Weekly", "start": 1, "end": 2})
    assert st == 500
    assert "/Users" not in out["error"] and "Traceback" not in out["error"]
    assert "/Users/someone/checkout" in capsys.readouterr().err


# ---------- Fields, the typed reader ----------

def test_fields_readers():
    f = srv.Fields({"s": "x", "n": 2.5, "i": 3.0, "b": True, "l": ["a"],
                    "o": {}, "z": None})
    assert f.text("s") == "x" and f.text("z", "dflt") == "dflt"
    assert f.number("n") == 2.5 and f.integer("i") == 3
    assert f.flag("b") is True and f.flag("absent") is False
    assert f.texts("l") == ["a"] and f.obj("o") == {}
    assert f.get("absent") is None and "s" in f
    for call, msg in [
            (lambda: f.text("n"), "'n' must be text"),
            (lambda: f.text("absent"), "missing field 'absent'"),
            (lambda: f["absent"], "missing field 'absent'"),
            (lambda: f.integer("n"), "'n' must be a whole number"),
            (lambda: f.number("s"), "'s' must be a number"),
            (lambda: f.number("b"), "'b' must be a number"),
            (lambda: f.flag("s"), "'s' must be true or false"),
            (lambda: f.texts("s"), "'s' must be a list of text values"),
            (lambda: f.obj("l"), "'l' must be an object"),
            (lambda: f.integer("i", lo=0, hi=2), "'i' must be from 0 to 2"),
            (lambda: f.text("s", max_len=0), "'s' is too long (0 characters at most)")]:
        with pytest.raises(srv.BadRequest) as e:
            call()
        assert e.value.msg == msg and e.value.code == 400


def test_query_fields_parse_numbers_from_text():
    q = srv.Fields({"secs": "12.5", "n": "3", "bad": "nan", "inf": "1e999"},
                   query=True)
    assert q.number("secs") == 12.5 and q.integer("n") == 3
    for k in ("bad", "inf"):
        with pytest.raises(srv.BadRequest):
            q.number(k)
    # a JSON body never parses text as a number
    with pytest.raises(srv.BadRequest):
        srv.Fields({"n": "3"}).integer("n")
