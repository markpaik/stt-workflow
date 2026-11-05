"""The new description (gist, sections, tags, catalog title), the Ollama
backend with its MLX fallback, the hardware limits, the Redo out-of-date
flag and its idle regeneration, and what the panel receives. The model is
faked throughout. Every name is synthetic."""
import io
import json
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
    # chief-of-staff's line format, with the missing deadline filled in
    assert d["ai_next_steps"] == ["[Robin Cole] will post both roles by Friday",
                                  "[Owen Park] will send the budget sheet to Robin by no date given"]
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
    assert r["ok"] and r["base"] == f"Robin Weekly Check-in {DASH} Hiring Plan and Budget 10012026"
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
    assert d["ai_next_steps"][0] == "[Robin Cole-Ash] will post both roles by Friday"


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
    assert summarize._generate("hi") == "mlx"
    assert seen[0][:5] == ["taskpolicy", "-b", "nice", "-n", "10"]


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
    assert all(c[1:3] == ["-n", "10"] for c in renice)
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
            "ai_next_steps": ["[A] will x by y"], "ai_tmpl": {"v": 1}}
    meta = {}
    pipeline._title_fields(meta, prev, Path("x.m4a"), SPOKEN)
    assert meta["ai_stale"] is True
    assert meta["ai_gist"] == "G" and meta["ai_next_steps"] == ["[A] will x by y"]
    assert "ai_tmpl" not in meta            # its clusters died with the Redo
    fresh = {}
    pipeline._title_fields(fresh, {"source_file": "x.m4a"}, Path("x.m4a"), SPOKEN)
    assert "ai_stale" not in fresh


def test_the_batch_regenerates_stale_descriptions_when_idle(owner, monkeypatch):
    import run_batch
    a, b, c = (_meeting(f"Mtg {n} 10012026") for n in "abc")
    for base in (a, b):
        d = _load(base)
        meta = {"ai_stale": True, "ai_summary": "old"}
        # the pipeline writes the flag among the metadata, ahead of segments
        d = {**{k: v for k, v in d.items() if k not in ("speakers", "segments", "words")},
             **meta, "speakers": d["speakers"], "segments": d["segments"], "words": []}
        config.meeting_file(base, ".json").write_text(json.dumps(d, indent=2))
    assert summarize.stale_bases() == sorted([a, b])
    monkeypatch.setattr(summarize, "available", lambda: True)
    monkeypatch.setattr(summarize, "_generate", lambda *a_, **k: REPLY)
    rests = []
    monkeypatch.setattr(config, "llm_rest", lambda sec, log=None, sleep=None: rests.append(sec))
    assert run_batch.regenerate_stale(limit=1) == 1
    assert len(summarize.stale_bases()) == 1 and len(rests) == 1
    assert run_batch.regenerate_stale() == 1
    assert summarize.stale_bases() == []
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
    for s in ("'Decisions'", "'Open questions'", "'Next steps'", "data-act=\"m-tag\"",
              "'m-tag'(el)", "summary_stale"):
        assert s in meet


def test_search_indexes_tag_labels(sandbox):
    from stt import search
    txt = search._summary_text({"ai_title": "T", "ai_summary": "S", "ai_tags": ["math", "nope"]})
    assert "Math" in txt and "nope" in txt


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
