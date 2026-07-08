"""POST /api/ask — the panel's per-meeting Q&A endpoint. The LLM itself is
faked; request validation, error shapes, and passthrough are under test."""
import pytest

from stt import summarize
# the running-server fixture and HTTP helpers live with the security tests
from test_server_security import _get, _post, _make_meeting, running_server  # noqa: F401


def test_ask_unknown_base_is_rejected(running_server):
    st, body = _post(running_server, "/api/ask",
                     {"base": "../../etc/passwd", "question": "hi?"})
    assert st == 400 and body["error"] == "unknown meeting"


def test_ask_requires_llm_and_a_real_question(running_server, monkeypatch):
    _make_meeting("Mtg")
    monkeypatch.setattr(summarize, "available", lambda: False)
    st, body = _post(running_server, "/api/ask", {"base": "Mtg", "question": "hi?"})
    assert st == 503 and "LLM" in body["error"]

    monkeypatch.setattr(summarize, "available", lambda: True)
    st, body = _post(running_server, "/api/ask", {"base": "Mtg", "question": "   "})
    assert st == 400
    st, body = _post(running_server, "/api/ask",
                     {"base": "Mtg", "question": "x" * 2001})
    assert st == 400


def test_ask_happy_path_passes_history_through(running_server, monkeypatch):
    _make_meeting("Mtg")
    seen = {}

    def fake(base, q, history=None):
        seen.update(base=base, q=q, history=history)
        return {"ok": True, "answer": "42 [00:10]", "truncated": True,
                "elapsed_sec": 0.1}

    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "answer_question", fake)
    st, body = _post(running_server, "/api/ask",
                     {"base": "Mtg", "question": "meaning?",
                      "history": [{"q": "a", "a": "b"}]})
    assert st == 200
    assert body["answer"] == "42 [00:10]" and body["truncated"] is True
    assert seen == {"base": "Mtg", "q": "meaning?", "history": [{"q": "a", "a": "b"}]}

    # a malformed history is dropped, never crashed on
    st, body = _post(running_server, "/api/ask",
                     {"base": "Mtg", "question": "again?", "history": "bogus"})
    assert st == 200 and seen["history"] is None


def test_ask_not_ok_result_maps_to_400(running_server, monkeypatch):
    _make_meeting("Mtg")
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "answer_question",
                        lambda *a, **k: {"ok": False, "error": "no transcript"})
    st, body = _post(running_server, "/api/ask", {"base": "Mtg", "question": "hi?"})
    assert st == 400 and body["error"] == "no transcript"


def test_ask_busy_model_maps_to_503(running_server, monkeypatch):
    _make_meeting("Mtg")
    monkeypatch.setattr(summarize, "available", lambda: True)

    def busy(*a, **k):
        raise summarize.LLMBusy("busy")

    monkeypatch.setattr(summarize, "answer_question", busy)
    st, body = _post(running_server, "/api/ask", {"base": "Mtg", "question": "hi?"})
    assert st == 503 and "busy" in body["error"].lower()


def test_ask_foreign_origin_is_rejected(running_server):
    _make_meeting("Mtg")
    st, body = _post(running_server, "/api/ask",
                     {"base": "Mtg", "question": "hi?"},
                     headers={"Origin": "https://evil.example"})
    assert st == 403
