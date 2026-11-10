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



def test_a_message_written_for_the_user_reaches_the_user(running_server,
                                                        monkeypatch, capsys):
    """A planned failure keeps its own text: the catch-all turned "Google
    Chrome not found -- export Word instead" and "the local model (Ollama)
    is not reachable" into the fixed sentence, so the help never arrived."""
    from stt import errors, export, summarize
    _make_meeting("2026-04-08 Team Weekly")
    monkeypatch.setattr(export, "CHROME", "/nonexistent/Google Chrome")
    st, out = _post(running_server, "/api/export",
                    {"base": "2026-04-08 Team Weekly", "fmt": "pdf"})
    assert st == 500 and out["ok"] is False
    assert out["error"].startswith("Google Chrome not found")

    def down(*a, **k):
        raise summarize.OllamaDown("the local model (Ollama) is not reachable")
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "describe", down)
    monkeypatch.setattr(summarize, "has_speech", lambda base: True)
    st, out = _post(running_server, "/api/suggest", {"base": "2026-04-08 Team Weekly"})
    assert (st, out) == (503, {"ok": False,
                               "error": "the local model (Ollama) is not reachable"})
    monkeypatch.setattr(summarize, "_generate", down)
    st, out = _post(running_server, "/api/ask",
                    {"base": "2026-04-08 Team Weekly", "question": "who?"})
    assert st == 503 and out["error"] == "the local model (Ollama) is not reachable"

    def no_key(*a, **k):
        raise errors.UserError("no Anthropic API key — add one under Cloud "
                               "keys in Settings")
    from stt import ask
    monkeypatch.setattr(ask, "ask", no_key)
    st, out = _post(running_server, "/api/ask_library", {"question": "who?"})
    assert st == 500 and out["error"].startswith("no Anthropic API key")
    # a planned failure is not an unplanned one: no traceback in the log
    assert "Traceback" not in capsys.readouterr().err


def test_an_unknown_export_format_is_a_400(running_server):
    _make_meeting("2026-04-08 Team Weekly")
    st, out = _post(running_server, "/api/export",
                    {"base": "2026-04-08 Team Weekly", "fmt": "odt"})
    assert (st, out) == (400, {"error": "'fmt' must be docx, pdf or reveal"})


def test_an_unplanned_runtime_error_still_hides_its_text(running_server,
                                                        monkeypatch):
    """Only a UserError is shown. A plain RuntimeError (the MLX runner's
    stderr tail, say) can hold a path, so it stays the fixed sentence."""
    from stt import summarize
    _make_meeting("2026-04-08 Team Weekly")

    def runner(*a, **k):
        raise RuntimeError('LLM runner failed: File "/Users/someone/x.py"')
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "has_speech", lambda base: True)
    monkeypatch.setattr(summarize, "describe", runner)
    st, out = _post(running_server, "/api/suggest", {"base": "2026-04-08 Team Weekly"})
    assert (st, out) == (500, {"error": srv.INTERNAL_ERROR})

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


# ---------- typed fields, route by route ----------

B = "2026-04-08 Team Weekly"


@pytest.mark.parametrize("route, body, msg", [
    ("/api/review", {"base": B, "index": "abc", "action": "accept"},
     "'index' must be a whole number"),
    ("/api/review", {"base": B, "action": "accept"}, "missing field 'index'"),
    ("/api/review", {"base": B, "index": 0, "action": "accept", "start": "abc"},
     "'start' must be a number"),
    ("/api/review", {"base": B, "index": 0}, "missing field 'action'"),
    ("/api/review", {"base": B, "action": "insert", "start": "x", "end": 2,
                     "speaker": "SPEAKER_00", "text": "hi"},
     "'start' must be a number"),
    ("/api/ask_library", {"question": 123}, "'question' must be text"),
    ("/api/ask_library", {"question": "why", "scope": "all"},
     "'scope' must be an object"),
    ("/api/ask", {"base": B, "question": ["x"]}, "'question' must be text"),
    ("/api/bulk", {"action": "archive", "bases": B},
     "'bases' must be a list of text values"),
    ("/api/bulk", {"action": "rename", "bases": [B], "value": 7},
     "'value' must be text"),
    ("/api/run", {"files": "notalist"}, "'files' must be a list of text values"),
    ("/api/run", {"parallel": "x"}, "'parallel' must be a whole number"),
    ("/api/run", {"parallel": 5}, "'parallel' must be from 1 to 2"),
    ("/api/run", {"force": "yes"}, "'force' must be true or false"),
    ("/api/unqueue", {"at": "abc"}, "'at' must be a number"),
    ("/api/schedule", {"hour": 99, "minute": 1}, "'hour' must be from 0 to 23"),
    ("/api/schedule", {"hour": 9}, "missing field 'minute'"),
    ("/api/merge_speakers", {"src": "Jordan", "dst": "name:Alex"},
     "'src' must look like uid:U003 or name:Jordan"),
    ("/api/set_folder", {"which": "x", "path": "/tmp"},
     "'which' must be source or dest"),
    ("/api/rename", {"base": B, "new": 5}, "'new' must be text"),
    ("/api/automation", {"watch": "yes"}, "'watch' must be true or false"),
    ("/api/name", {"meeting": B, "speaker": "SPEAKER_00", "name": "X",
                   "local": "no"}, "'local' must be true or false"),
    ("/api/rename_speaker", {"name": "Alex Rivera"}, "missing field 'new'"),
    ("/api/remove_sample", {"name": "Alex Rivera", "index": "0"},
     "'index' must be a whole number"),
    ("/api/retranscribe", {"base": B, "start": 1}, "missing field 'end'"),
    ("/api/cloud_keys", {"clear": "scribe"},
     "'clear' must be a list of text values"),
])
def test_a_wrong_or_missing_field_is_a_400_that_names_it(running_server, monkeypatch,
                                                         route, body, msg):
    """Each of these was a 500 carrying Python's own words: "invalid literal
    for int() with base 10: 'abc'", "'int' object has no attribute 'strip'",
    "can only concatenate str (not \\"list\\") to str", or an empty string
    from a bare assert. /api/bulk iterated a string as its characters."""
    from stt import jobs, summarize
    _make_meeting(B)
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(srv, "_spawn", lambda a: pytest.fail("spawned"))
    st, out = _post(running_server, route, body)
    assert (st, out) == (400, {"error": msg})
    assert jobs.items() == [], "a refused run must queue nothing"


def test_snippet_secs_that_is_not_a_number_is_a_400(running_server):
    st, out = _get(running_server, "/api/snippet?speaker=U001&secs=abc")
    assert (st, out) == (400, {"error": "'secs' must be a number"})


def test_delete_confirmation_must_be_a_real_true(running_server):
    """The README promises delete-forever behind a two-step confirm. The API
    tested only truthiness, so {"confirm": "false"} deleted the meeting."""
    _make_meeting(B)
    for bad in ("false", "true", 1, [True], {"x": 1}):
        st, out = _post(running_server, "/api/delete_meeting",
                        {"base": B, "confirm": bad})
        assert (st, out) == (400, {"error": "'confirm' must be true or false"}), bad
        assert B in config.meeting_bases()
    st, out = _post(running_server, "/api/delete_meeting", {"base": B, "confirm": False})
    assert out == {"ok": False, "error": "missing confirmation"}
    st, out = _post(running_server, "/api/bulk",
                    {"action": "delete", "bases": [B], "confirm": "false"})
    assert st == 400 and B in config.meeting_bases()
    st, out = _post(running_server, "/api/delete_meeting", {"base": B, "confirm": True})
    assert st == 200 and out["ok"] is True
    assert B not in config.meeting_bases()


def test_queue_deletes_need_a_real_true_too(running_server):
    src = config.source_dir()
    src.mkdir(parents=True, exist_ok=True)
    (src / "take one.m4a").write_bytes(b"\0" * 64)
    for route, body in (("/api/queue_delete", {"name": "take one.m4a"}),
                        ("/api/queue_delete_dupes", {})):
        st, out = _post(running_server, route, {**body, "confirm": "false"})
        assert (st, out) == (400, {"error": "'confirm' must be true or false"})
    assert (src / "take one.m4a").exists()


def test_the_insert_route_refuses_a_time_outside_the_recording(running_server):
    _make_meeting(B)                # 21 s long
    st, out = _post(running_server, "/api/review",
                    {"base": B, "action": "insert", "start": 9999, "end": 10001,
                     "speaker": "SPEAKER_00", "text": "way past end"})
    assert st == 200 and out == {
        "ok": False,
        "error": "that time is outside the recording, which runs 00:00 to 00:21"}
    st, out = _raw(running_server, "POST", "/api/review",
                   b'{"base": "%s", "action": "insert", "start": NaN, "end": 2,'
                   b' "speaker": "SPEAKER_00", "text": "x"}' % B.encode())
    assert (st, out) == (400, {"error": "the request body is not valid JSON"})
