"""summarize.suggest_title parsing: title/summary/next-steps extraction from
the LLM's structured reply (the LLM itself is faked — parsing is under test)."""
import json
import os
import threading
import time
from datetime import datetime as _real_datetime

import pytest

from stt import config, review, summarize
from conftest import mfile


def _meeting(base="Mtg"):
    mfile(base, ".txt").write_text("[00:00] Alex: We will ship it.\n")
    mfile(base, ".json").write_text(json.dumps(
        {"source_file": f"{base}.m4a", "generated_at": "2026-07-01T10:00:00",
         "speakers": [], "segments": [], "words": []}))


def test_parses_title_summary_and_next_steps(sandbox, monkeypatch):
    _meeting()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: (
        "TITLE: Budget Planning Session\n"
        "SUMMARY: The team reviewed the draft budget.\n"
        "It was mostly agreed.\n"
        "NEXT STEPS:\n"
        "- [Alex Rivera] will circulate the revised budget by Friday\n"
        "- [Jordan Lee] will confirm vendor pricing by no date given\n"))
    r = summarize.suggest_title("Mtg")
    assert r["title"] == "Budget Planning Session"
    assert r["summary"] == "The team reviewed the draft budget. It was mostly agreed."
    assert r["next_steps"] == [
        "[Alex Rivera] will circulate the revised budget by Friday",
        "[Jordan Lee] will confirm vendor pricing by no date given"]
    d = json.loads(config.meeting_file("Mtg", ".json").read_text())
    assert d["ai_next_steps"] == r["next_steps"]  # persisted for the panel
    assert d["ai_summary"] == r["summary"]


def test_summary_prompt_asks_for_brief_varied_prose(sandbox, monkeypatch):
    """Guards the restyle: summaries must be asked for as 2-3 sentences that
    do not all open with 'The meeting ...' — a prompt revert would quietly
    bring the samey, long summaries back."""
    _meeting()
    prompts = []
    monkeypatch.setattr(summarize, "_generate",
                        lambda p, **k: prompts.append(p) or "TITLE: T\nSUMMARY: S\n")
    summarize.suggest_title("Mtg")
    (p,) = prompts
    assert "2-3 sentences" in p
    assert "Never begin with 'The meeting'" in p
    # the output contract the parser and panel depend on is unchanged
    assert "TITLE: <title>" in p and "NEXT STEPS" in p


def test_no_commitments_yields_empty_list(sandbox, monkeypatch):
    _meeting()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: (
        "TITLE: Casual Catch Up\n"
        "SUMMARY: General discussion, no decisions.\n"
        "NEXT STEPS:\n"
        "- none\n"))
    r = summarize.suggest_title("Mtg")
    assert r["next_steps"] == []
    d = json.loads(config.meeting_file("Mtg", ".json").read_text())
    assert d["ai_next_steps"] == []


def test_missing_next_steps_section_is_tolerated(sandbox, monkeypatch):
    """An older/other-model reply without the section must not break parsing."""
    _meeting()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: (
        "TITLE: Legacy Reply\nSUMMARY: Just a summary.\n"))
    r = summarize.suggest_title("Mtg")
    assert r["summary"] == "Just a summary."
    assert r["next_steps"] == []


def _meeting_with_segment(base="Mtg"):
    """A meeting real enough for review.apply to edit a segment in place."""
    mfile(base, ".txt").write_text("stub")
    data = {"source_file": f"{base}.m4a", "generated_at": "2026-07-01T10:00:00",
            "duration_sec": 5.0, "strict": False,
            "speakers": [{"id": "SPEAKER_00", "name": "Alex", "display": "Alex",
                          "match_score": 0.9}],
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00",
                          "name": "Alex", "display": "Alex", "text": "original text",
                          "flags": [], "overlap": False}],
            "words": []}
    mfile(base, ".json").write_text(json.dumps(data))


def test_suggest_title_persist_does_not_clobber_concurrent_review(sandbox, monkeypatch):
    """Finding #14: suggest_title's json persist must take lock_meeting and
    re-read inside it, so it can't overwrite a human review edit that lands
    while the (slow) LLM call is in flight. Forces the interleaving:
    suggest_title reads d, THEN a concurrent review.apply edits the same
    segment, THEN suggest_title writes. Without the lock the stale write wins
    and the edit is lost."""
    _meeting_with_segment()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: (
        "TITLE: Weekly Sync\nSUMMARY: We discussed the roadmap.\n"))

    read_done = threading.Event()   # suggest_title has taken its (stale) read
    edit_done = threading.Event()   # review.apply has finished its edit

    # datetime.now() is called in the persist block AFTER the read and BEFORE the
    # write, in both the buggy and fixed code — the exact seam to interpose on.
    class _Barrier:
        @staticmethod
        def now(*a, **k):
            read_done.set()
            edit_done.wait(timeout=2.0)   # bounded: under the fix the lock makes
            return _real_datetime.now()   # the writer wait out, then proceed
        @staticmethod
        def fromisoformat(s):
            return _real_datetime.fromisoformat(s)

    monkeypatch.setattr(summarize, "datetime", _Barrier)

    errors = {}

    def do_summary():
        try:
            summarize.suggest_title("Mtg")
        except Exception as e:  # pragma: no cover
            errors["summary"] = repr(e)

    def do_review():
        try:
            read_done.wait(timeout=2.0)
            review.apply("Mtg", 0, "edit", start=0.0, text="EDITED BY REVIEWER")
        except Exception as e:  # pragma: no cover
            errors["review"] = repr(e)
        finally:
            edit_done.set()

    ts = threading.Thread(target=do_summary)
    tr = threading.Thread(target=do_review)
    ts.start(); tr.start()
    ts.join(timeout=10.0); tr.join(timeout=10.0)

    assert not errors, errors
    d = json.loads(config.meeting_file("Mtg", ".json").read_text())
    # BOTH writes must survive: the reviewer's edit AND the ai_summary.
    assert d["segments"][0]["text"] == "EDITED BY REVIEWER"  # not clobbered
    assert d["ai_summary"] == "We discussed the roadmap."


def test_rename_meeting_waits_for_concurrent_relabel_lock(sandbox):
    """Finding #9: rename_meeting must hold lock_meeting(base) so it serializes
    against a relabel_one(base) mid-write. Here a fake relabel holds the lock and
    rewrites base.json; rename must block until that write lands, then carry the
    relabel's output into the renamed file — never orphan a stale copy."""
    _meeting_with_segment()
    old_dir = config.meeting_dir("Mtg")
    jpath_old = config.meeting_file("Mtg", ".json")  # captured BEFORE the rename

    r_locked = threading.Event()
    release = threading.Event()
    m_done = threading.Event()
    errors = {}
    MARK = "RELABELED-BY-CONCURRENT-WRITER"

    def fake_relabel():
        try:
            with review.lock_meeting("Mtg"):
                r_locked.set()
                release.wait(timeout=5.0)
                # relabel's own atomic rewrite of the meeting json
                d = json.loads(jpath_old.read_text())
                d["relabel_marker"] = MARK
                tmp = jpath_old.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(d))
                os.replace(tmp, jpath_old)
        except Exception as e:
            errors["relabel"] = repr(e)

    def do_rename():
        try:
            r_locked.wait(timeout=5.0)
            summarize.rename_meeting("Mtg", "MtgNew")
        except Exception as e:
            errors["rename"] = repr(e)
        finally:
            m_done.set()

    tr = threading.Thread(target=fake_relabel)
    tm = threading.Thread(target=do_rename)
    tr.start(); tm.start()

    r_locked.wait(timeout=5.0)
    # While the relabel holds the per-meeting lock, rename must NOT complete.
    completed_while_locked = m_done.wait(timeout=0.5)
    release.set()
    tr.join(timeout=5.0); tm.join(timeout=5.0)

    assert not completed_while_locked, "rename_meeting ignored lock_meeting(base)"
    assert not errors, errors
    newj = config.meeting_file("MtgNew", ".json")
    assert json.loads(newj.read_text()).get("relabel_marker") == MARK
    # no stale pre-rename json left orphaned inside the new folder
    assert not (config.meeting_dir("MtgNew") / "Mtg.json").exists()


# ---------- answer_question: grounded per-meeting Q&A ----------

def test_answer_question_prompt_grounding_and_history(sandbox, monkeypatch):
    _meeting()
    prompts = []
    monkeypatch.setattr(summarize, "_generate",
                        lambda p, **k: prompts.append(p) or "It shipped [00:00].")
    r = summarize.answer_question("Mtg", "Did we ship?",
                                  history=[{"q": "Who spoke?", "a": "Alex."}])
    assert r["ok"] and r["answer"] == "It shipped [00:00]."
    assert r["truncated"] is False
    (p,) = prompts
    assert "[00:00] Alex: We will ship it." in p           # transcript present
    assert "QUESTION: Did we ship?" in p
    assert "Q: Who spoke?" in p and "A: Alex." in p        # history rides along
    assert "ONLY from the transcript" in p                 # grounding rules
    assert "cite its timestamp" in p
    assert "omitted" not in p                # no truncation note when it fits whole


def test_answer_question_flags_a_sampled_long_transcript(sandbox, monkeypatch):
    mfile("Big", ".txt").write_text(
        "HEADMARK " + "x" * (summarize.QA_MAX_CHARS + 30_000) + " TAILMARK")
    prompts = []
    monkeypatch.setattr(summarize, "_generate",
                        lambda p, **k: prompts.append(p) or "ok")
    r = summarize.answer_question("Big", "anything?")
    assert r["ok"] and r["truncated"] is True
    (p,) = prompts
    assert "HEADMARK" in p and "TAILMARK" in p and "[...]" in p
    assert "omitted" in p                                  # the model is told


def test_answer_question_rejects_empty_and_missing(sandbox, monkeypatch):
    _meeting()
    called = []
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: called.append(1) or "x")
    assert summarize.answer_question("Mtg", "   ")["ok"] is False
    r = summarize.answer_question("Nope", "hi?")
    assert r["ok"] is False and "Nope" in r["error"]
    assert not called


def test_answer_question_never_leaks_unterminated_think(sandbox, monkeypatch):
    """If generation dies inside a <think> block the closing tag never arrives,
    _generate's strip can't match, and raw chain-of-thought would become the
    'answer' — the guard must eat it."""
    _meeting()
    monkeypatch.setattr(summarize, "_generate",
                        lambda *a, **k: "<think>half-finished secret reasoning")
    r = summarize.answer_question("Mtg", "hi?")
    assert r["ok"] is False and "secret" not in json.dumps(r)
    monkeypatch.setattr(summarize, "_generate",
                        lambda *a, **k: "<think>hmm</think>The answer.")
    assert summarize.answer_question("Mtg", "hi?")["answer"] == "The answer."


# ---------- _llm_lock: one resident model at a time ----------

def _stub_llm_subprocess(monkeypatch, delay=0.0, tracker=None):
    """Fake the .venv-llm subprocess under the REAL _generate/_llm_lock path."""
    import subprocess as sp

    guard = threading.Lock()

    def fake_run(*a, **k):
        if tracker is not None:
            with guard:
                tracker["inside"] += 1
                tracker["peak"] = max(tracker["peak"], tracker["inside"])
        if delay:
            time.sleep(delay)
        if tracker is not None:
            with guard:
                tracker["inside"] -= 1
        return sp.CompletedProcess(a, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(summarize, "available", lambda: True)


def test_generate_never_runs_two_models_at_once(sandbox, monkeypatch):
    tracker = {"inside": 0, "peak": 0}
    _stub_llm_subprocess(monkeypatch, delay=0.2, tracker=tracker)
    outs, errs = [], []

    def w():
        try:
            outs.append(summarize._generate("x"))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=w) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=10)
    assert not errs, errs
    assert outs == ["ok", "ok"]
    assert tracker["peak"] == 1, "two LLM subprocesses overlapped"


def test_interactive_generate_fails_fast_when_model_is_busy(sandbox, monkeypatch):
    _stub_llm_subprocess(monkeypatch)
    release = threading.Event()
    held = threading.Event()

    def holder():
        with summarize._llm_lock():
            held.set()
            release.wait(timeout=10)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(timeout=5)
    with pytest.raises(summarize.LLMBusy):
        summarize._generate("x", lock_timeout=0.1)
    release.set()
    t.join(timeout=5)
    assert summarize._generate("x") == "ok"   # blocking default succeeds once free
