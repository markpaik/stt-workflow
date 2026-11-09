"""The new description (gist, sections, tags, catalog title), the Ollama
backend with its MLX fallback, the hardware limits, the Redo out-of-date
flag and its idle regeneration, and what the panel receives. The model is
faked throughout. Every name is synthetic."""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from stt import config, series, summarize, titles
from conftest import mfile

DASH = "–"
SPOKEN = [{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00", "text": "we will ship it"},
          {"start": 4.0, "end": 8.0, "speaker": "SPEAKER_01", "text": "agreed"}]
REPLY = (
    "SERIES: weekly_checkin\n"
    "PERSON: Robin Cole\n"
    "TOPIC: Hiring Plan And Budget\n"
    "GIST: Hiring plan approved with a smaller budget and two open roles posted this week for review.\n"
    "TAGS: Hiring and staffing, budget-finance, invented-tag, team-operations\n"
    "DECISIONS:\n"
    "- Robin Cole owns the posting.\n"
    "- The budget drops by ten percent.\n"
    "OPEN QUESTIONS:\n"
    "- none\n"
    "NEXT STEPS:\n"
    "- [Robin Cole] will post both roles by Friday\n"
    "- [Owen Park] will send the budget sheet to Robin\n")


def _meeting(base="Robin Check-in 10-5 10012026", strict=False):
    mfile(base, ".txt").write_text("[00:00] Owen Park: We will ship it.\n[00:04] Robin Cole: Agreed.\n")
    mfile(base, ".json").write_text(json.dumps(
        {"meeting_id": "mid-1", "source_file": f"{base}.m4a", "date": "2026-10-01",
         "strict": strict, "title_state": "needs_title", "title_source": "model",
         "speakers": [{"id": "Owen Park", "name": "Owen Park", "display": "Owen Park"},
                      {"id": "Robin Cole", "name": "Robin Cole", "display": "Robin Cole"}],
         "segments": [dict(SPOKEN[0], speaker="Owen Park"), dict(SPOKEN[1], speaker="Robin Cole")],
         "words": []}))
    return base


def _load(base):
    return json.loads(config.meeting_file(base, ".json").read_text())


@pytest.fixture
def owner(sandbox):
    (sandbox / "stt.env").write_text("STT_MIC_SPEAKER=Owen Park\n")


# ---------------------------------------------------------------- parsing ---
def test_the_reply_becomes_a_catalog_title_gist_sections_and_tags(owner, monkeypatch):
    base = _meeting()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: REPLY)
    r = summarize.suggest_title(base)
    assert r["title"] == f"Robin Weekly Check-in {DASH} Hiring Plan and Budget"
    d = _load(base)
    assert d["ai_series"] == "weekly_checkin" and d["ai_person"] == "Robin"
    assert len(d["ai_gist"].split()) <= summarize.GIST_MAX_WORDS
    assert d["ai_gist"].endswith(".")
    assert d["ai_tags"] == ["hiring-staffing", "budget-finance", "team-operations"]
    assert d["ai_decisions"] == ["Robin Cole owns the posting.", "The budget drops by ten percent."]
    assert d["ai_open_questions"] == []
    # next steps were removed: a reply that still lists them stores none
    assert "ai_next_steps" not in d and "post both roles" not in d["ai_summary"]
    assert d["ai_summary"].startswith(d["ai_gist"])
    assert "Decisions:\n- Robin Cole owns the posting." in d["ai_summary"]
    assert d["ai_title"] == r["title"]
    assert "ai_stale" not in d and d["ai_tmpl"]["v"] == 1
    # suggest_title never renames: the folder is untouched
    assert config.meeting_dir(base).exists()


def test_the_automatic_title_uses_the_catalog_title_whole(owner, monkeypatch):
    base = _meeting()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: REPLY)
    summarize.suggest_title(base)
    r = summarize.apply_auto_title(base)
    assert r["ok"] and r["base"] == f"2026-10-01 Robin Weekly Check-in {DASH} Hiring Plan and Budget"
    assert titles.auto_title({"ai_title": "X Y", "ai_series": "one_off",
                              "speakers": [{"id": 1, "name": "Kim"}]}) == "X Y"


def test_an_older_shaped_reply_keeps_its_prose_summary(owner, monkeypatch):
    base = _meeting()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: (
        "TITLE: Budget Talk\nSUMMARY: A long prose summary. Second sentence.\nNEXT STEPS:\n- none\n"))
    r = summarize.suggest_title(base)
    d = _load(base)
    assert r["title"] == "Budget Talk"
    assert d["ai_summary"] == "A long prose summary. Second sentence."
    assert "ai_gist" not in d and "ai_series" not in d


def test_gist_rules_and_clipping():
    long = " ".join(["word"] * 30)
    assert len(summarize.clip_gist(long).split()) == 20
    assert summarize.gist_problems("The meeting covered budgets.") == ["opens with the meeting"]
    assert "opens with a date" in summarize.gist_problems("Oct 5 budget review.")
    assert "opens with a date" not in summarize.gist_problems("Marked progress on budgets.")
    assert "opens with a person" in summarize.gist_problems("Robin approved it.", ["Robin Cole"])
    assert summarize.gist_problems("Budget approved for spring.") == []


def test_the_description_is_rendered_against_the_roster_at_write_time(owner, monkeypatch):
    """The model can run for minutes: a naming that lands meanwhile must
    show in the stored text."""
    base = _meeting()

    def slow(*a, **k):
        d = _load(base)
        for s in d["speakers"]:
            if s["id"] == "Robin Cole":
                s["display"] = "Robin Cole-Ash"
        config.meeting_file(base, ".json").write_text(json.dumps(d))
        return REPLY
    monkeypatch.setattr(summarize, "_generate", slow)
    summarize.suggest_title(base)
    d = _load(base)
    assert d["ai_decisions"][0] == "Robin Cole-Ash owns the posting."


def test_a_strict_meeting_never_reaches_a_cloud_backend_for_descriptions(sandbox, monkeypatch):
    base = _meeting(strict=True)
    (sandbox / "stt.env").write_text("STT_LLM_BACKEND=anthropic\nSTT_ANTHROPIC_KEY=k\n")
    monkeypatch.setattr(summarize, "LLM_PY", Path(sys.executable))
    seen = []
    monkeypatch.setattr(summarize, "_generate",
                        lambda p, backend=None, **k: seen.append(backend) or REPLY)
    summarize.describe(base)
    assert seen == ["mlx"]


# ------------------------------------------------------------------ Ollama ---
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ollama_on(monkeypatch, sandbox, url="http://localhost:11434"):
    (sandbox / "stt.env").write_text(f"STT_OLLAMA_URL={url}\n")
    monkeypatch.delenv("STT_OLLAMA_URL", raising=False)


def _fake_urlopen(monkeypatch, calls, tags=True, fail=False):
    import urllib.request

    def fake(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if fail:
            raise urllib.error.URLError("refused")
        if url.endswith("/api/tags"):
            names = [{"name": summarize.OLLAMA_MODEL}] if tags else []
            return _Resp(json.dumps({"models": names}).encode())
        calls.append(json.loads(req.data))
        return _Resp(json.dumps({"message": {"content": "<think>x</think>ANSWER"}}).encode())
    import urllib.error  # noqa: F401
    monkeypatch.setattr(urllib.request, "urlopen", fake)


def test_ollama_is_the_default_local_engine_when_it_answers(sandbox, monkeypatch):
    _ollama_on(monkeypatch, sandbox)
    calls = []
    _fake_urlopen(monkeypatch, calls)
    renices = []
    monkeypatch.setattr(summarize, "renice_ollama", lambda: renices.append(1) or 0)
    assert summarize.local_engine() == "ollama"
    assert summarize._generate("hi", max_tokens=50, think=False) == "ANSWER"
    (body,) = calls
    assert body["model"] == "qwen3.8:27b-mlx" and body["think"] is False
    assert body["stream"] is False
    assert "num_thread" not in body["options"] and "num_gpu" not in body["options"]
    assert body["options"]["num_predict"] == 50
    assert renices == [1]                     # lowered priority before the call


def test_think_on_gets_its_own_token_budget(sandbox, monkeypatch):
    _ollama_on(monkeypatch, sandbox)
    calls = []
    _fake_urlopen(monkeypatch, calls)
    monkeypatch.setattr(summarize, "renice_ollama", lambda: 0)
    summarize._generate("hi", max_tokens=50, think=True)
    assert calls[0]["think"] is True
    assert calls[0]["options"]["num_predict"] == 50 + summarize.OLLAMA_THINK_TOKENS


def test_a_non_loopback_ollama_url_is_refused(sandbox, monkeypatch):
    _ollama_on(monkeypatch, sandbox, url="http://10.0.0.5:11434")
    calls = []
    _fake_urlopen(monkeypatch, calls)
    assert summarize._ollama_url() is None
    assert summarize.local_engine() == "mlx"


def test_ollama_down_falls_back_to_mlx_at_lowered_priority(sandbox, monkeypatch):
    _ollama_on(monkeypatch, sandbox)
    monkeypatch.setattr(summarize, "ollama_up", lambda ttl=10.0: True)
    _fake_urlopen(monkeypatch, [], fail=True)
    monkeypatch.setattr(summarize, "renice_ollama", lambda: 0)
    monkeypatch.setattr(summarize, "LLM_PY", Path(sys.executable))
    seen = []

    def fake_run(args, **k):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "mlx"}), stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os, "getpriority", lambda which, who: 0)
    assert summarize._generate("hi") == "mlx"
    assert seen[0][:5] == ["taskpolicy", "-b", "nice", "-n", "10"]


def test_the_mlx_fallback_lands_at_the_setting_whatever_the_batch_runs_at(sandbox, monkeypatch):
    """nice -n adds to the caller's own nice. A batch already at nice 4
    must start the model at 10, not 14."""
    _ollama_on(monkeypatch, sandbox)
    monkeypatch.setattr(summarize, "ollama_up", lambda ttl=10.0: True)
    _fake_urlopen(monkeypatch, [], fail=True)
    monkeypatch.setattr(summarize, "renice_ollama", lambda: 0)
    monkeypatch.setattr(summarize, "LLM_PY", Path(sys.executable))
    seen = []

    def fake_run(args, **k):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "mlx"}), stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    for mine, inc in ((4, "6"), (10, "0"), (15, "0")):
        seen.clear()
        monkeypatch.setattr(os, "getpriority", lambda which, who, v=mine: v)
        assert summarize._generate("hi") == "mlx"
        assert seen[0][:5] == ["taskpolicy", "-b", "nice", "-n", inc]


def test_renice_targets_every_ollama_pid_and_never_raises(sandbox, monkeypatch):
    calls = []

    def fake_run(args, **k):
        calls.append(args)
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 0, stdout="101\n202\n" if "-x" in args else "303\n", stderr="")
        if args[-1] == "202":
            raise OSError("not permitted")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert summarize.renice_ollama() == 2
    renice = [c for c in calls if c[0] == "renice"]
    assert {c[-1] for c in renice} == {"101", "202", "303"}
    # the absolute form: "renice -n" is an increment on macOS
    assert all(c[1:3] == ["10", "-p"] for c in renice)
    # background QoS too: it lowers the GPU priority that nice cannot
    qos = [c for c in calls if c[0] == "taskpolicy"]
    assert {c[-1] for c in qos} == {"101", "202", "303"}
    assert all(c[1:3] == ["-b", "-p"] for c in qos)


def test_hardware_settings_come_from_stt_env(sandbox, monkeypatch):
    monkeypatch.delenv("STT_LLM_REST_MIN_SEC", raising=False)
    monkeypatch.delenv("STT_LLM_REST_RATIO", raising=False)
    from datetime import datetime
    day, night = datetime(2026, 11, 5, 14, 0), datetime(2026, 11, 5, 23, 30)
    assert config.llm_nice() == 10
    assert config.llm_rest_seconds(10, now=day) == 20         # the floor
    assert config.llm_rest_seconds(100, now=day) == 100       # 1.0 x the work
    assert config.llm_rest_seconds(100, now=night) == 0       # night ratio 0
    assert config.llm_quiet_now(datetime(2026, 11, 5, 7, 0))
    assert not config.llm_quiet_now(datetime(2026, 11, 5, 22, 0))
    (sandbox / "stt.env").write_text(
        "STT_LLM_REST_RATIO=0.5\nSTT_LLM_REST_MIN_SEC=5\nSTT_LLM_NICE=15\n"
        "STT_LLM_QUIET_HOURS=22:00-06:00\nSTT_LLM_REST_RATIO_NIGHT=0.25\n")
    assert config.llm_quiet_now(night) and not config.llm_quiet_now(day)
    assert config.llm_rest_seconds(2, now=night) == 5
    assert config.llm_rest_seconds(40, now=night) == 20
    assert config.llm_rest_seconds(40, now=day) == 10
    assert config.llm_nice() == 15
    (sandbox / "stt.env").write_text("STT_LLM_QUIET_HOURS=garbage\n")
    assert config.llm_quiet_now(day)                          # the default window
    (sandbox / "stt.env").write_text("STT_LLM_QUIET_HOURS=00:00-00:00\nSTT_LLM_REST_RATIO=1\n")
    slept, logs = [], []
    assert config.llm_rest(30, log=logs.append, sleep=slept.append) == 30
    assert slept == [30] and "resting 30s" in logs[0]


# -------------------------------------------------------- Redo and idle ---
def test_a_redo_carries_the_description_marked_out_of_date(sandbox):
    from stt import pipeline
    prev = {"source_file": "x.m4a", "ai_summary": "S", "ai_gist": "G", "ai_tags": ["math"],
            "ai_next_steps": ["[A] will x by y"], "ai_decisions": ["D"], "ai_tmpl": {"v": 1}}
    meta = {}
    pipeline._title_fields(meta, prev, Path("x.m4a"), SPOKEN)
    assert meta["ai_stale"] is True
    assert meta["ai_gist"] == "G" and meta["ai_decisions"] == ["D"]
    assert "ai_next_steps" not in meta      # removed: a Redo never carries it
    assert "ai_tmpl" not in meta            # its clusters died with the Redo
    fresh = {}
    pipeline._title_fields(fresh, {"source_file": "x.m4a"}, Path("x.m4a"), SPOKEN)
    assert "ai_stale" not in fresh


def test_the_batch_regenerates_stale_descriptions_when_idle(owner, monkeypatch):
    import run_batch
    a, b, c = (_meeting(f"Mtg {n} 10012026") for n in "abc")
    for n, base in enumerate((a, b, c)):
        d = _load(base)
        d["meeting_id"] = f"mid-s{n}"
        config.meeting_file(base, ".json").write_text(json.dumps(d))
        config.index_set(d["meeting_id"], base)
    for base in (a, b):
        d = _load(base)
        meta = {"ai_stale": True, "ai_summary": "old"}
        # the pipeline writes the flag among the metadata, ahead of segments
        d = {**{k: v for k, v in d.items() if k not in ("speakers", "segments", "words")},
             **meta, "speakers": d["speakers"], "segments": d["segments"], "words": []}
        config.meeting_file(base, ".json").write_text(json.dumps(d, indent=2))
        summarize.stale_mark(d["meeting_id"], base)       # what a Redo does
    assert summarize.stale_bases() == sorted([a, b])
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "_generate", lambda *a_, **k: REPLY)
    rests = []
    monkeypatch.setattr(config, "llm_rest", lambda sec, log=None, sleep=None: rests.append(sec))
    assert run_batch.regenerate_stale(limit=1) == 1
    # the limit is reached: no rest after the last one this run does
    assert len(summarize.stale_bases()) == 1 and rests == []
    assert run_batch.regenerate_stale() == 1
    assert summarize.stale_bases() == [] and rests == []
    assert json.loads(summarize._stale_path().read_text()) == {}
    for base in (a, b):
        d = _load(base)
        assert "ai_stale" not in d and d["ai_gist"]
        assert d["title_state"] == "needs_title"   # the title is never touched
    assert "ai_gist" not in _load(c)


def test_regeneration_stops_for_a_stop_request(owner, monkeypatch):
    import run_batch
    from stt import control
    base = _meeting()
    d = _load(base)
    d = {"ai_stale": True, "ai_summary": "old", **d}
    config.meeting_file(base, ".json").write_text(json.dumps(d, indent=2))
    config.index_set("mid-1", base)
    summarize.stale_mark("mid-1", base)
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(control, "finish_requested", lambda: True)
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: pytest.fail("no call"))
    assert run_batch.regenerate_stale() == 0


# ------------------------------------------------------------------ panel ---
def test_the_panel_receives_gist_sections_tags_and_the_stale_flag(owner, monkeypatch):
    from gui import server as srv
    base = _meeting()
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: REPLY)
    summarize.suggest_title(base)
    d = _load(base)
    d["ai_stale"] = True
    config.meeting_file(base, ".json").write_text(json.dumps(d))
    srv._meet_cache.clear()
    m = srv._meeting_meta(config.meeting_file(base, ".json"), config.meetings_dir())
    assert m["gist"] == d["ai_gist"]
    assert m["decisions"] == d["ai_decisions"] and m["open_questions"] == []
    assert m["tags"] == ["Hiring and staffing", "Budget and finance", "Team operations"]
    assert m["series"] == "weekly_checkin" and m["summary_stale"] is True
    assert m["summary"] == d["ai_summary"]


def test_the_shell_shows_the_gist_sections_and_tag_filter():
    static = Path(__file__).resolve().parent.parent / "gui" / "static"
    nxt = (static / "next.js").read_text()
    meet = (static / "next-meeting.js").read_text()
    assert "m.gist||" in nxt                                  # the row preview
    assert "(m.tags||[]).some(t=>t.toLowerCase()===q)" in nxt  # tag filter
    for s in ("'Decisions'", "'Open questions'", "data-act=\"m-tag\"",
              "'m-tag'(el)", "summary_stale"):
        assert s in meet
    assert "'Next steps'" not in meet and "next_steps" not in meet


def test_search_indexes_tag_labels(sandbox):
    """Only a tag the panel shows is searchable. A slug outside the
    vocabulary (a hand edit) is hidden in the list, so a title search must
    not find the meeting by it either."""
    from stt import search
    txt = search._summary_text({"ai_title": "T", "ai_summary": "S", "ai_tags": ["math", "nope"]})
    assert "Math" in txt and "nope" not in txt


def test_an_unknown_tag_slug_does_not_match_a_title_search(sandbox):
    from stt import search
    j = mfile("2026-02-02 Edge Case Lab", ".json")
    j.write_text(json.dumps({"title": "Edge Case Lab", "segments": [], "speakers": [],
                             "ai_tags": ["special-education", "bogus-tag"]}))
    assert search.meetings("bogus") == []
    assert [m["base"] for m in search.meetings("special education")] == [
        "2026-02-02 Edge Case Lab"]


def test_a_title_that_is_a_series_name_fixes_the_series_in_code(owner, monkeypatch):
    base = _meeting("Robin Weekly Check-in 10012026")
    prompts = []
    # the model names another series: code keeps the exact match
    monkeypatch.setattr(summarize, "_generate", lambda p, **k: prompts.append(p) or
                        REPLY.replace("SERIES: weekly_checkin", "SERIES: lt_weekly")
                        .replace("PERSON: Robin Cole", "PERSON: none"))
    desc = summarize.describe(base)
    assert desc["series"] == "weekly_checkin" and desc["series_by_code"] is True
    assert desc["person"] == "Robin"
    assert "The series is known: weekly_checkin" in prompts[0]
    assert series.exact_series("Sam and Robin C. Weekly Check-in") == (
        "weekly_checkin", "Sam and Robin C.")
    assert series.exact_series("Weekly Check-in Robin") == (None, "")


def test_the_fixed_prompt_comes_first_and_the_transcript_last(owner, monkeypatch):
    a, b = _meeting("Mtg A 10012026"), _meeting("Other B 10022026")
    prompts = []
    monkeypatch.setattr(summarize, "_generate", lambda p, **k: prompts.append(p) or REPLY)
    summarize.describe(a)
    summarize.describe(b)
    pa, pb = prompts
    cut = pa.index("THE MEETING")
    assert pa[:cut] == pb[:cut]                    # one cached prefix
    assert "Mtg A" not in pa[:cut] and series.catalog_text() in pa[:cut]
    assert pa.rstrip().endswith("[00:04] Robin Cole: Agreed.")


def test_next_steps_are_gone_from_prompt_panel_search_and_storage(owner, monkeypatch):
    from gui import server as srv
    from stt import search
    base = _meeting()
    j = config.meeting_file(base, ".json")
    d = _load(base)
    d["ai_next_steps"] = ["[Robin Cole] will keep this by Friday"]
    j.write_text(json.dumps(d))
    prompts = []
    monkeypatch.setattr(summarize, "_generate", lambda p, **k: prompts.append(p) or REPLY)
    r = summarize.suggest_title(base)
    assert "NEXT STEPS" not in prompts[0] and "next_steps" not in r
    d = _load(base)
    assert "ai_next_steps" not in d                       # never lingers
    assert all("next_steps" not in str(k) for k in d["ai_tmpl"]["fields"])
    srv._meet_cache.clear()
    assert "next_steps" not in srv._meeting_meta(j, config.meetings_dir())
    assert "will keep this" not in search._summary_text(
        {"ai_summary": "S", "ai_next_steps": ["[A] will keep this by Friday"]})
    assert not hasattr(summarize, "normalize_step")      # its one user is gone


def test_an_old_template_with_next_steps_renders_nothing_for_them(sandbox):
    from stt import speaker_text
    d = {"speakers": [{"id": "S1", "name": "Alice Chen", "display": "Alice Chen"}], "segments": []}
    tmpl = speaker_text.make({"gist": "Alice Chen opened."}, d)
    tmpl["fields"]["next_steps"] = ["\u27e6s:S1\u27e7 will x by y"]
    out = speaker_text.render(tmpl, d)
    assert "ai_next_steps" not in out and out["ai_gist"] == "Alice Chen opened."


def test_the_description_call_uses_the_chosen_think_setting(owner, monkeypatch):
    base = _meeting()
    seen = []
    monkeypatch.setattr(summarize, "_generate",
                        lambda p, think=None, **k: seen.append(think) or REPLY)
    summarize.describe(base)
    summarize.describe(base, think=False)
    assert seen == [summarize.DESCRIPTION_THINK, False]


# ------------------------------------------------- review findings 1-10 ---
def _stale_meeting(base, mid, speech=True):
    _meeting(base)
    d = _load(base)
    d.update({"meeting_id": mid, "ai_stale": True, "ai_summary": "old"})
    if not speech:
        d["segments"] = []
    config.meeting_file(base, ".json").write_text(json.dumps(d))
    config.index_set(mid, base)
    summarize.stale_mark(mid, base)


def test_finding1_the_whole_limit_leaves_room_for_the_think_budget():
    on, off = summarize.ollama_whole_chars(True), summarize.ollama_whole_chars(False)
    assert on < off
    budget = summarize.DESCRIPTION_MAX_TOKENS + summarize.OLLAMA_THINK_TOKENS
    tokens = on / summarize.OLLAMA_CHARS_PER_TOKEN + summarize.OLLAMA_FIXED_PROMPT_TOKENS
    assert tokens + budget <= summarize.OLLAMA_NUM_CTX


def test_finding1_a_long_meeting_goes_to_parts_with_think_on(owner, monkeypatch):
    base = _meeting()
    line = "[00:00] Owen Park: " + "budget " * 20 + "\n"
    n = summarize.ollama_whole_chars(True) // len(line) + 50
    mfile(base, ".txt").write_text(line * n)          # between the two limits
    assert summarize.ollama_whole_chars(True) < len(line) * n < summarize.ollama_whole_chars(False)
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: "DECISIONS: none")
    heading, body, parts = summarize._summary_source(base, "ollama", think=True)
    assert parts > 1
    assert summarize._summary_source(base, "ollama", think=False)[2] == 1


def test_finding3_and_6_markdown_and_numbered_headers_parse():
    f = summarize.parse_description("**DECISIONS:**\n- Move funds\n**NEXT STEPS:**\n"
                                    "- [A] will x by y\n")
    assert f["decisions"] == ["Move funds"] and f["next_steps"] == ["[A] will x by y"]
    f = summarize.parse_description(
        "1. SERIES: lt_weekly\n2. PERSON: none\n3. TOPIC: Budget\n4. GIST: Budget moved.\n"
        "5. TAGS: math\n6. DECISIONS:\n1. One\n2. Two\n7. OPEN QUESTIONS:\n- none\n"
        "8. NEXT STEPS:\n- [A] will x by y\n")
    assert (f["series"], f["topic"], f["gist"], f["tags"]) == ("lt_weekly", "Budget",
                                                                "Budget moved.", "math")
    assert f["decisions"] == ["One", "Two"] and f["open_questions"] == []
    f = summarize.parse_description("## **SERIES:** lt_weekly\n**GIST:** Budget moved.\n"
                                    "Decisions were made later.\nDECISIONS: one thing\n")
    assert f["series"] == "lt_weekly" and f["gist"] == "Budget moved."
    assert f["decisions"] == ["one thing"]


def test_finding5_catalog_names_normalize_to_ids():
    assert series.normalize_series("LT Weekly Meeting") == "lt_weekly"
    assert series.normalize_series("Weekly Check-in") == "weekly_checkin"
    assert series.normalize_series("Robin Weekly Check-in") == "weekly_checkin"
    assert series.normalize_series("Committee of the Whole.") == "board_cotw"


def test_finding7_ollama_dying_after_the_probe_falls_back_to_mlx(owner, monkeypatch):
    base = _meeting()
    monkeypatch.setattr(summarize, "local_engine", lambda: "ollama")
    monkeypatch.setattr(summarize, "LLM_PY", Path(sys.executable))
    seen = []

    def gen(prompt, backend=None, **k):
        seen.append(backend)
        if backend == "ollama":
            raise summarize.OllamaDown("gone")
        return REPLY
    monkeypatch.setattr(summarize, "_generate", gen)
    desc = summarize.describe(base)
    assert seen == ["ollama", "mlx"] and desc["engine"] == "mlx"
    assert desc["series"] == "weekly_checkin"


def test_finding10_gist_rules_see_the_unclipped_gist_and_whole_names(owner, monkeypatch):
    assert "opens with a person" in summarize.gist_problems(
        "James approved the budget.", ["James Lee"])
    base = _meeting()
    long = " ".join(["Budget"] + ["word"] * 30) + "."
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: REPLY.replace(
        REPLY.split("GIST: ")[1].split("\n")[0], long))
    desc = summarize.describe(base)
    assert "long" in desc["gist_problems"]
    assert len(desc["gist"].split()) == summarize.GIST_MAX_WORDS


def test_finding4_regeneration_clears_no_speech_and_backs_off_failures(owner, monkeypatch):
    import run_batch
    _stale_meeting("Quiet 10012026", "mid-q", speech=False)
    _stale_meeting("Bad 10022026", "mid-b")
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(config, "llm_rest", lambda *a, **k: 0)
    # the store fails: suggest_title still answers, but nothing landed
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: REPLY)
    monkeypatch.setattr(summarize, "store_description",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert run_batch.regenerate_stale() == 0
    assert "ai_stale" not in _load("Quiet 10012026")      # no speech: flag cleared
    idx = json.loads(summarize._stale_path().read_text())
    assert list(idx) == ["mid-b"] and idx["mid-b"]["fails"] == 1
    # backed off: the next run does not retry it
    assert summarize.stale_bases() == []
    import time as _t
    assert summarize.stale_bases(now=_t.time() + summarize.STALE_BACKOFF_SEC + 1) == ["Bad 10022026"]


def test_finding9_stale_lookup_reads_the_index_not_every_json(owner, monkeypatch):
    _stale_meeting("Listed 10012026", "mid-l")
    _meeting("Unlisted 10022026")
    reads = []
    real = Path.read_text

    def spy(self, *a, **k):
        if self.suffix == ".json" and self.parent.parent == config.meetings_dir():
            reads.append(self.name)
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", spy)
    assert summarize.stale_bases() == ["Listed 10012026"]
    assert reads == ["Listed 10012026.json"]


def test_finding9_auto_summarize_does_not_rest_after_the_last(sandbox, monkeypatch):
    import run_batch
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(run_batch, "summarize_one", lambda k, b=None: True)
    rests = []
    monkeypatch.setattr(config, "llm_rest", lambda sec, log=None, sleep=None: rests.append(sec))
    assert run_batch.auto_summarize([("a", None), ("b", None), ("c", None)]) == 3
    assert len(rests) == 2


def test_a_redo_lists_the_meeting_in_the_stale_index(sandbox):
    from stt import pipeline
    meta = {"meeting_id": "mid-r"}
    pipeline._title_fields(meta, {"source_file": "x.m4a", "ai_summary": "S"},
                           Path("x.m4a"), SPOKEN)
    assert meta["ai_stale"] is True
    src = (Path(__file__).resolve().parent.parent / "stt" / "pipeline.py").read_text()
    assert "_summ.stale_mark(meta[\"meeting_id\"], base)" in src


# ------------------------------------------------- an empty or junk reply ---
GOOD = {"ai_gist": "Hiring plan approved.", "ai_title": "Good Title",
        "ai_decisions": ["Keep it."], "ai_stale": True}


@pytest.mark.parametrize("junk", ["", "I am sorry, I cannot do that.", "\n\n",
                                  "DECISIONS:\n- Keep the budget.",
                                  "SERIES: Weekly Check-in\nTOPIC: Hiring\nOPEN QUESTIONS:\n- Who?"])
def test_an_empty_reply_keeps_the_old_description_and_counts_as_a_failure(
        owner, monkeypatch, junk):
    """Ollama returns empty content when thinking uses the whole budget. A
    reply with no gist and no summary must not overwrite a good description,
    clear the out-of-date flag, or count as stored."""
    base = _meeting()
    p = config.meeting_file(base, ".json")
    p.write_text(json.dumps({**json.loads(p.read_text()), **GOOD}))
    summarize.stale_mark("mid-1", base)
    monkeypatch.setattr(summarize, "_generate", lambda *a, **k: junk)
    r = summarize.suggest_title(base)
    assert not r.get("stored") and r.get("ok") is False and r.get("error")
    d = _load(base)
    for k, v in GOOD.items():
        assert d[k] == v
    assert base in summarize.stale_bases()
    with pytest.raises(summarize.EmptyDescription):
        summarize.describe(base)
    # a cached empty reply (the retitle tool stores those later) is refused too
    cached = summarize.build_description(junk, summarize.context_for(base))
    with pytest.raises(summarize.EmptyDescription):
        summarize.store_description(base, cached)
    assert _load(base)["ai_gist"] == GOOD["ai_gist"]


def test_renice_holds_the_setting_over_repeated_calls(sandbox, monkeypatch):
    """renice -n adds to the current nice on macOS, so STT_LLM_NICE=10 put
    the model at 20 after two calls. A real process stays at 10."""
    if not Path("/usr/bin/renice").exists():
        pytest.skip("no renice")
    if os.getpriority(os.PRIO_PROCESS, 0) > 10:
        pytest.skip("this test runs above nice 10, and a user cannot lower it")
    p = subprocess.Popen(["/bin/cat"], stdin=subprocess.PIPE)
    real_run = subprocess.run

    def fake_run(args, **k):
        if args[0] == "pgrep":
            return subprocess.CompletedProcess(args, 0, stdout=f"{p.pid}\n" if "-x" in args else "", stderr="")
        if args[0] == "taskpolicy":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return real_run(args, **k)
    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        for _ in range(3):
            summarize.renice_ollama()
            assert os.getpriority(os.PRIO_PROCESS, p.pid) == 10
    finally:
        p.kill()
        p.wait()
