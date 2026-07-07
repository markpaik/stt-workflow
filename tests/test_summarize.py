"""summarize.suggest_title parsing: title/summary/next-steps extraction from
the LLM's structured reply (the LLM itself is faked — parsing is under test)."""
import json

from stt import config, summarize
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
