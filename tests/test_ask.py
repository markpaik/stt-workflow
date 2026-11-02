"""Library Ask (unified plan Phase 4F): stt/ask.py and its endpoints.

What these lock down:
  - retrieval reads the FTS5 index and keeps to the scope
  - citations are numbered, carry meeting_id, and an unknown id is dropped
  - an answer with no valid citation, NOT FOUND, or no retrieval hit is the
    nothing-found answer, with the closest hits and no invented text
  - the model is not called when retrieval finds nothing
  - a busy model is a 503 with busy:true; a missing model is a 503
  - threads save under ask_threads/ (folder 700, files 600), keep their
    scope, survive a rename through meeting_id, and purge clears them
  - the stop rule on the synthetic evaluation store, with the extractive
    stand-in for the model
All data is synthetic.
"""
import json
import stat

import pytest

from stt import ask, config, search, summarize
from tools import ask_eval
from conftest import mfile
from test_server_security import _get, _post, running_server  # noqa: F401


def _mk(base, lines, mid=None, day="2026-10-01", strict=False):
    d = {"source_file": f"{base}.m4a", "duration_sec": 60.0 * len(lines),
         "date": day, "strict": strict,
         "speakers": [{"id": "SPEAKER_00", "name": "Avery Stone", "display": "Avery Stone"}],
         "segments": [{"start": float(i * 20), "end": float(i * 20 + 15),
                       "speaker": "SPEAKER_00", "display": "Avery Stone",
                       "text": t, "flags": []} for i, t in enumerate(lines)],
         "words": []}
    if mid:
        d = {"meeting_id": mid, **d}
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("\n".join(lines))


FILL = ["Thanks for joining.", "Let us keep going.", "That sounds fine.",
        "Moving on now.", "Any other thoughts here?"]


def _world():
    _mk("Roof Budget 10012026", FILL + ["The roof reserve is 412 thousand dollars."] + FILL,
        mid="mid-roof")
    _mk("Bus Routes 10022026", FILL + ["Route seventeen adds a stop at Maple."] + FILL,
        mid="mid-bus", day="2026-10-02")


def _cite_first(prompt):
    """A model that answers with the first excerpt line naming 'reserve' or
    'seventeen', cited by its excerpt id."""
    sid = None
    for line in prompt.split("EXCERPTS:", 1)[1].splitlines():
        if line.startswith("[S") and "]" in line[:5]:
            sid = line[1:line.index("]")]
        elif sid and ("reserve" in line or "seventeen" in line):
            return line.split(": ", 1)[-1] + f" [{sid}]"
    return "NOT FOUND"


# ------------------------------------------------------------- retrieval ---

def test_terms_drop_question_words():
    assert ask.terms("What did we decide about the roof reserve?") == ["roof", "reserve"]
    assert ask.terms("Who said 412?") == ["412"]


def test_passages_hold_the_hit_with_its_neighbors(sandbox):
    _world()
    ps = ask.passages("How big is the roof reserve?")
    assert ps and ps[0]["base"] == "Roof Budget 10012026"
    assert ps[0]["mid"] == "mid-roof" and ps[0]["sid"] == "S1"
    texts = [ln["text"] for ln in ps[0]["lines"]]
    assert "The roof reserve is 412 thousand dollars." in texts
    assert len(texts) == 5                       # two before, the hit, two after


def test_scope_limits_the_meetings(sandbox):
    _world()
    bases, label, err = ask.scope_bases({"kind": "days", "days": 3650})
    assert err is None and label == "Last 3650 days" and len(bases) == 2
    ps = ask.passages("roof reserve", bases={"Bus Routes 10022026"})
    assert all(p["base"] == "Bus Routes 10022026" for p in ps)
    assert ask.scope_bases({"kind": "group", "key": "gnone"})[2]
    assert ask.scope_bases({"kind": "nonsense"})[2]


# --------------------------------------------------------------- answers ---

def test_an_answer_cites_with_meeting_id(sandbox):
    _world()
    r = ask.ask("How big is the roof reserve?", generate=_cite_first)
    assert r["ok"] and not r["nothing_found"]
    assert r["answer"] == "The roof reserve is 412 thousand dollars. [1]"
    (c,) = r["citations"]
    assert c["meeting_id"] == "mid-roof" and c["base"] == "Roof Budget 10012026"
    assert c["start"] == 100.0 and c["n"] == 1 and c["date"] == "2026-10-01"


def test_unknown_ids_and_uncited_answers_are_nothing_found(sandbox):
    _world()
    ps = ask.passages("roof reserve")
    r = ask.parse_answer("The roof reserve is large [S9].", ps)
    assert r["nothing_found"] and r["answer"] == ask.NOT_FOUND_TEXT
    r = ask.parse_answer("The roof reserve is large.", ps)
    assert r["nothing_found"]
    r = ask.parse_answer("<think>hmm</think>NOT FOUND", ps)
    assert r["nothing_found"]
    r = ask.parse_answer("Reserve [S1, S9]; again [S1].", ps)
    assert r["answer"] == "Reserve [1]; again [1]." and len(r["citations"]) == 1


def test_nothing_found_offers_the_closest_hits_and_no_text(sandbox):
    _world()
    r = ask.ask("Is the roof heater fixed?", generate=lambda p: "NOT FOUND")
    assert r["nothing_found"] and r["answer"] == ask.NOT_FOUND_TEXT
    assert r["citations"] == [] and r["hits"]
    assert r["hits"][0]["base"] == "Roof Budget 10012026"


def test_the_model_is_not_called_when_nothing_matches(sandbox):
    _world()
    called = []
    r = ask.ask("Who coaches the soccer team?", generate=lambda p: called.append(p) or "x")
    assert r["nothing_found"] and r["hits"] == [] and called == []


def test_an_empty_library_says_so(sandbox):
    r = ask.ask("Anything?")
    assert r["ok"] is False and r["empty"] is True


def test_a_cloud_backend_never_receives_a_strict_meeting(sandbox, monkeypatch):
    _mk("Strict Review 10032026", FILL + ["The roof reserve is secret."] + FILL,
        strict=True)
    _mk("Open Review 10042026", FILL + ["The roof reserve is public."] + FILL)
    monkeypatch.setattr(summarize, "llm_backend", lambda: "anthropic")
    sent = []

    def fake(prompt, **k):
        sent.append(prompt)
        return "NOT FOUND"
    monkeypatch.setattr(summarize, "_generate", fake)
    r = ask.ask("roof reserve")
    assert r["left_out"] == 1
    (p,) = sent
    assert "secret" not in p and "public" in p


def test_a_reply_cut_off_inside_its_reasoning_is_an_error_not_an_answer(sandbox):
    """QA 4F #1: max_tokens can end the reply before </think>. The reasoning
    names [S1], and it must never pass for a cited answer."""
    _world()
    ps = ask.passages("roof reserve")
    cut = "<think>\nThe user asks about the reserve. Excerpt [S1] mentions it. Let me check whether"
    r = ask.parse_answer(cut, ps)
    assert r == {"error": ask.OUT_OF_ROOM_TEXT}
    assert ask.parse_answer("<think>all of it</think>", ps) == {"error": ask.OUT_OF_ROOM_TEXT}
    r = ask.ask("How big is the roof reserve?", generate=lambda p: cut)
    assert r["ok"] is False and "ran out of room" in r["error"]
    assert "citations" not in r


def test_a_closing_not_found_makes_the_answer_nothing_found(sandbox):
    """QA 4F #5: a reply that cites, then ends in NOT FOUND, is not an answer."""
    _world()
    ps = ask.passages("roof reserve")
    for txt in ("The excerpts mention [S1] a reserve, but not the vote. NOT FOUND",
                "The reserve is named [S1].\nNOT FOUND.",
                "The reserve is named [S1]. **NOT FOUND**"):
        r = ask.parse_answer(txt, ps)
        assert r["nothing_found"] and r["citations"] == [], txt
    # "not found" inside a sentence is still an answer
    r = ask.parse_answer("The reserve was not found in the audit [S1].", ps)
    assert not r["nothing_found"]


def test_a_cloud_backend_never_receives_a_strict_answer_from_the_thread(sandbox, monkeypatch):
    """QA 4F #2: an earlier local answer drawn from a strict meeting stays
    out of a cloud follow-up. A citation that no longer resolves counts as
    strict."""
    _mk("Strict Review 10032026", FILL + ["The settlement figure is ninety thousand."] + FILL,
        mid="m-strict", strict=True)
    _mk("Open Plan 10042026", FILL + ["The settlement plan is public."] + FILL,
        mid="m-open", day="2026-10-04")
    r = ask.ask("settlement figure",
                generate=lambda p: "Ninety thousand [S1]" if "ninety" in p else "NOT FOUND")
    assert r["citations"][0]["meeting_id"] == "m-strict"
    t = ask.save_message(None, {"kind": "all"}, r["scope_label"], "settlement figure", r)
    r = ask.ask("settlement plan", generate=lambda p: "It is public [S1]")
    t = ask.save_message(t["id"], {"kind": "all"}, r["scope_label"], "settlement plan", r)
    hist = [{"q": "an exchange from a purged meeting", "a": "Gone answer",
             "citations": [{"meeting_id": "m-purged", "base": "Purged 10012026"}]},
            {"q": "no citations stored", "a": "Bare answer"}] + ask.history_of(t["id"])
    monkeypatch.setattr(summarize, "llm_backend", lambda: "anthropic")
    sent = []

    def fake(prompt, **k):
        sent.append(prompt)
        return "NOT FOUND"
    monkeypatch.setattr(summarize, "_generate", fake)
    ask.ask("settlement plan again", history=hist)
    (p,) = sent
    assert "Ninety thousand" not in p and "ninety" not in p.lower()
    assert "Gone answer" not in p and "Bare answer" not in p
    assert "It is public" in p                   # an open meeting's answer stays
    # the local model still sees the whole thread
    monkeypatch.setattr(summarize, "llm_backend", lambda: "local")
    sent.clear()
    ask.ask("settlement plan again", history=hist)
    assert "Ninety thousand" in sent[0]


# --------------------------------------------------------------- threads ---

def test_threads_save_private_and_survive_a_rename(sandbox):
    _world()
    r = ask.ask("How big is the roof reserve?", generate=_cite_first)
    t = ask.save_message(None, {"kind": "all"}, "All recordings", "How big?", r)
    d = ask.threads_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    f = d / f"{t['id']}.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    t2 = ask.save_message(t["id"], {"kind": "days"}, "ignored", "And then?", r)
    assert t2["id"] == t["id"] and len(t2["messages"]) == 2
    assert t2["scope"] == {"kind": "all", "label": "All recordings"}
    assert ask.list_threads()[0]["n"] == 2
    assert summarize.rename_meeting("Roof Budget 10012026", "Roof Money")["ok"]
    got = ask.get_thread(t["id"])
    c = got["messages"][0]["citations"][0]
    assert c["base"] == "Roof Money 10012026" and c["gone"] is False
    assert ask.history_of(t["id"])[0]["q"] == "How big?"
    assert ask.get_thread("../../etc") is None
    assert ask.delete_thread(t["id"]) and ask.list_threads() == []


def test_purge_removes_threads_that_cite_the_meeting(sandbox, monkeypatch, tmp_path):
    from tools import purge
    monkeypatch.setenv("STT_PURGE_SCAN_DIRS", str(tmp_path / "none"))
    _world()
    r1 = ask.ask("roof reserve?", generate=_cite_first)
    r2 = ask.ask("route seventeen?", generate=_cite_first)
    t1 = ask.save_message(None, {"kind": "all"}, "All recordings", "roof", r1)
    t2 = ask.save_message(None, {"kind": "all"}, "All recordings", "bus", r2)
    assert purge.plan("Roof Budget 10012026")["threads"]
    purge.apply("Roof Budget 10012026")
    left = {p.stem for p in ask.threads_dir().glob("*.json")}
    assert left == {t2["id"]} and t1["id"] not in left


# ---------------------------------------------------------- the endpoints ---

def test_api_ask_library_saves_a_thread(running_server, monkeypatch):
    _world()
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "_generate", lambda p, **k: _cite_first(p))
    st, r = _post(running_server, "/api/ask_library",
                  {"question": "How big is the roof reserve?"})
    assert st == 200 and r["ok"] and r["scope_label"] == "All recordings"
    assert r["message"]["citations"][0]["meeting_id"] == "mid-roof"
    tid = r["thread"]
    st, r2 = _post(running_server, "/api/ask_library",
                   {"question": "Which route adds a stop at Maple, route seventeen?",
                    "thread": tid})
    assert st == 200 and r2["thread"] == tid
    st, lst = _get(running_server, "/api/threads")
    assert [t["id"] for t in lst["threads"]] == [tid] and lst["threads"][0]["n"] == 2
    st, t = _get(running_server, f"/api/thread?id={tid}")
    assert st == 200 and len(t["messages"]) == 2
    st, _ = _get(running_server, "/api/thread?id=../x")
    assert st == 404
    st, r = _post(running_server, "/api/ask_library", {"question": "x", "thread": "zz"})
    assert st == 400
    st, r = _post(running_server, "/api/thread_delete", {"id": tid})
    assert r["ok"]


def test_api_ask_library_busy_and_missing_model(running_server, monkeypatch):
    _world()
    monkeypatch.setattr(summarize, "available", lambda: False)
    st, r = _post(running_server, "/api/ask_library", {"question": "roof reserve?"})
    assert st == 503 and "not installed" in r["error"]
    monkeypatch.setattr(summarize, "available", lambda: True)

    def busy(*a, **k):
        raise summarize.LLMBusy("busy")
    monkeypatch.setattr(summarize, "_generate", busy)
    st, r = _post(running_server, "/api/ask_library", {"question": "roof reserve?"})
    assert st == 503 and r["busy"] is True and "stays here" in r["error"]
    assert ask.list_threads() == []          # nothing saved for a busy model
    monkeypatch.setattr(summarize, "_generate",
                        lambda p, **k: "<think>Excerpt [S1] has the reserve, but")
    st, r = _post(running_server, "/api/ask_library", {"question": "roof reserve?"})
    assert st == 400 and "ran out of room" in r["error"]
    assert ask.list_threads() == []          # nor for a reply cut off mid-reasoning
    st, r = _post(running_server, "/api/ask_library", {"question": "  "})
    assert st == 400


def test_api_ask_library_group_scope_counts_the_ask(running_server, monkeypatch):
    from stt import groups
    _mk("Grant Writing 10012026", FILL + ["The match rate is ten percent."], mid="g1")
    _mk("Grant Writing 10082026", FILL + ["The match rate rises."], mid="g2")
    key = groups.current()[0]["key"]
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "_generate", lambda p, **k: "NOT FOUND")
    st, r = _post(running_server, "/api/ask_library",
                  {"question": "match rate?", "scope": {"kind": "group", "key": key}})
    assert st == 200 and r["message"]["nothing_found"]
    assert r["scope_label"] == "Grant Writing"
    assert groups.usage_counts()["group_ask"] == 1


# -------------------------------------------------------------- stop rule ---

def test_the_stop_rule_passes_on_the_synthetic_store_with_the_stand_in(sandbox, tmp_path):
    """The plan's gate, wired end to end on synthetic data: 80% of known
    questions answered with a working citation, and every nothing-found
    question declines. The extractive stand-in replaces the model, so this
    tests retrieval, citation, and decline. tools/ask_eval.py --run is the
    same evaluation with the local model."""
    ask_eval.build(tmp_path / "home")
    r = ask_eval.evaluate(ask_eval.oracle)
    assert r["known"] == 25 and r["nothing_found"] == 12
    assert r["declined"] == 12
    # the near misses reach the model: retrieval finds a planted fact
    assert r["near_miss"] == 6 and r["near_miss_declined"] == 6
    assert r["known_rate"] >= ask_eval.GATE, [x for x in r["rows"] if not x["pass"]]
    assert r["gate"] is True


def test_the_stop_rule_fails_when_answers_lose_their_citations(sandbox, tmp_path):
    ask_eval.build(tmp_path / "home")
    r = ask_eval.evaluate(lambda p: ask_eval.oracle(p).split(" [S")[0])
    assert r["known_pass"] == 0 and r["gate"] is False


def test_the_stop_rule_fails_when_the_model_invents_an_answer(sandbox, tmp_path):
    ask_eval.build(tmp_path / "home")
    r = ask_eval.evaluate(lambda p: "It is twelve [S1].")
    assert r["declined"] < r["nothing_found"] and r["gate"] is False


def test_the_evaluation_refuses_a_real_home(monkeypatch):
    monkeypatch.delenv("STT_HOME", raising=False)
    with pytest.raises(SystemExit):
        ask_eval._guard()
    monkeypatch.setenv("STT_HOME", str(config.Path.home() / "Projects/brain/meetings"))
    with pytest.raises(SystemExit):
        ask_eval._guard()


def test_purge_finds_a_thread_citing_an_accented_name_without_an_id(sandbox, monkeypatch, tmp_path):
    from tools import purge
    monkeypatch.setenv("STT_PURGE_SCAN_DIRS", str(tmp_path / "none"))
    _mk("Café Budget 10012026", FILL + ["The roof reserve is 412 thousand dollars."] + FILL)
    r = ask.ask("roof reserve?", generate=_cite_first)
    assert r["citations"][0]["meeting_id"] is None
    t = ask.save_message(None, {"kind": "all"}, "All recordings", "roof", r)
    assert purge.plan("Café Budget 10012026")["threads"] == [
        str(ask.threads_dir() / f"{t['id']}.json")]
