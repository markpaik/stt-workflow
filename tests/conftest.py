"""Shared fixture: run every test against a throwaway sandbox so no test can
touch real voiceprints, manifests, status, or transcripts."""
import io
import os

import pytest

from stt import config, control, holds, jobs, manifest, rates, status


# The environment the test run started with. Importing run_batch (a test
# module does it at collection) loads the checkout's stt.env into os.environ,
# and monkeypatch.delenv on such a key RESTORES the leaked value at teardown.
# In an owner's checkout that handed later tests, and every subprocess a test
# starts, the real library paths and keys.
_ENV_AT_START = dict(os.environ)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    """Every test starts from the environment the run started with, before
    any fixture runs. A reset at setup holds whatever order fixtures tear
    down in."""
    os.environ.clear()
    os.environ.update(_ENV_AT_START)
    yield


@pytest.fixture(autouse=True)
def _no_live_ollama(monkeypatch):
    """No test may reach the real Ollama on this Mac: the local engine falls
    back to the (faked) MLX path unless a test turns Ollama on itself."""
    monkeypatch.setenv("STT_OLLAMA_URL", "off")
    # the LLM duty cycle sleeps between meetings: never in a test
    monkeypatch.setenv("STT_LLM_REST_MIN_SEC", "0")
    monkeypatch.setenv("STT_LLM_REST_RATIO", "0")
    from stt import summarize
    monkeypatch.setattr(summarize, "_ollama_seen", {"at": 0.0, "up": False, "key": None})
    # each test starts outside any model run, with no request sent, and with
    # the keep_alive defaults whatever the shell running the tests sets
    monkeypatch.setattr(summarize, "_RUN", {"depth": 0, "used": False})
    for name in summarize.KEEP_ALIVE_DEFAULTS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_qwen(monkeypatch):
    """No test sees this Mac's .venv-qwen or model cache: Qwen3-ASR, the
    one local engine, reads as not set up (as on a fresh clone, where a
    file it would transcribe fails with the fix) unless a test sets it up
    itself. Each test starts with no remembered engine failure."""
    from stt import asr_qwen3
    monkeypatch.setattr(asr_qwen3, "problem", lambda deep=False: "not set up in tests")
    monkeypatch.setattr(asr_qwen3, "_unavailable", None)


@pytest.fixture(autouse=True)
def _no_real_shortcuts(monkeypatch, tmp_path):
    """No test may run the real `shortcuts` binary: it acts on the real
    Voice Memos library. The delete-after-ingest step sees no binary unless
    a test installs its own fake, and the setting starts off."""
    from stt import voicememos
    monkeypatch.setattr(voicememos, "SHORTCUTS_BIN", tmp_path / "no-shortcuts-here")
    monkeypatch.delenv("STT_VM_DELETE_AFTER_INGEST", raising=False)
    monkeypatch.delenv("STT_VM_DELETE_SHORTCUT", raising=False)


def mfile(base, suffix):
    """A meeting artifact's path in the per-meeting-folder layout, creating
    the folder — what test fixtures use to build meetings on disk."""
    p = config.meeting_file(base, suffix)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class _OllamaResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOllama:
    """Ollama's /api/tags, /api/ps, /api/chat and /api/generate, in memory,
    in place of urllib.request.urlopen: no test reaches the real server.
    `loaded` is what /api/ps lists. A chat loads its model, as Ollama does,
    and so does a generate for a model that is not loaded. A generate with
    keep_alive 0 unloads it. `fail` maps a path to the exception it raises.
    `reply(prompt)` is the chat answer. Every call lands in `calls` as
    (path, body)."""

    def __init__(self, loaded=None, fail=None, ps_body=None, reply=None):
        self.loaded = list(loaded or [])
        self.fail = dict(fail or {})
        self.ps_body = ps_body
        self.reply = reply or (lambda prompt: "ANSWER")
        self.calls = []

    def __call__(self, req, timeout=None):
        import json
        url = req if isinstance(req, str) else req.full_url
        path = url.split(":11434", 1)[-1]
        data = None if isinstance(req, str) else req.data
        body = json.loads(data) if data else None
        self.calls.append((path, body))
        if path in self.fail:
            raise self.fail[path]
        if path == "/api/tags":
            from stt import summarize
            out = {"models": [{"name": summarize.ollama_model()}]}
        elif path == "/api/ps":
            if self.ps_body is not None:
                return _OllamaResp(self.ps_body)
            out = {"models": [{"name": m, "model": m} for m in self.loaded]}
        elif path == "/api/chat":
            if body["model"] not in self.loaded:
                self.loaded.append(body["model"])
            out = {"message": {"content": self.reply(body["messages"][-1]["content"])}}
        elif path == "/api/generate":
            if body.get("keep_alive") == 0:
                self.loaded = [m for m in self.loaded if m != body["model"]]
            elif body["model"] not in self.loaded:
                self.loaded.append(body["model"])
            out = {"model": body["model"], "done": True,
                   "done_reason": "unload" if body.get("keep_alive") == 0 else "load"}
        else:
            raise AssertionError(f"unexpected Ollama call {path}")
        return _OllamaResp(json.dumps(out).encode())

    def paths(self):
        return [p for p, _ in self.calls]

    def keep_alives(self, path="/api/chat"):
        return [b["keep_alive"] for p, b in self.calls if p == path]


def fake_ollama(monkeypatch, sandbox, env="", **kw):
    """Turn Ollama on at its default loopback URL in the sandbox's stt.env
    (plus `env` lines) and answer every urlopen with a FakeOllama."""
    import urllib.request

    from stt import summarize
    (sandbox / "stt.env").write_text("STT_OLLAMA_URL=http://localhost:11434\n" + env)
    monkeypatch.delenv("STT_OLLAMA_URL", raising=False)
    monkeypatch.setattr(summarize, "renice_ollama", lambda: 0)
    # Ollama is the engine: no MLX fallback, whatever the checkout holds
    monkeypatch.setattr(summarize, "LLM_PY", sandbox / "no-venv-llm" / "bin" / "python")
    fake = FakeOllama(**kw)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def decision_count(base) -> int:
    """How many saved human edits meeting `base` has: the length of its
    .reviews.json. The panel read this through review.count_decisions for
    the classic page's Redo warning, so it is a test helper now."""
    import json
    try:
        return len(json.loads(config.meeting_file(base, ".reviews.json").read_text()))
    except (FileNotFoundError, ValueError):
        return 0


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    d = tmp_path
    (d / "meetings").mkdir()
    (d / "source").mkdir()
    (d / "voiceprints").mkdir()
    monkeypatch.setattr(config, "PROJECT_DIR", d)
    monkeypatch.setattr(config, "VOICEPRINTS_DIR", d / "voiceprints")
    monkeypatch.setattr(config, "MANIFEST_PATH", d / "manifest.json")
    # the meeting-id index binds its path at import like MANIFEST_PATH, so
    # without this every test that renames, archives or backfills a sandbox
    # meeting would write the id map of REAL meeting names into the checkout
    monkeypatch.setattr(config, "MEETINGS_INDEX_PATH", d / "meetings_index.json")
    # the search index binds its path at import too, and it holds transcript
    # text: a sandbox search must index the sandbox, never the checkout
    monkeypatch.setattr(config, "SEARCH_DB", d / "search.db")
    monkeypatch.setattr(config, "CALIBRATION_LOG", d / "calibration.jsonl")
    monkeypatch.setattr(config, "WORK_DIR", d / "work")
    # housekeeping rotates config.LOG_DIR at batch start: a test that runs
    # run_batch.main must never truncate the checkout's own logs
    monkeypatch.setattr(config, "LOG_DIR", d / "logs")
    monkeypatch.setattr(config, "MEETINGS_DIR", d / "meetings")
    monkeypatch.setattr(config, "ICLOUD_DIR", d / "source")
    monkeypatch.setattr(config, "RECORDINGS_DIR", d / "recordings")
    monkeypatch.setattr(config, "MIC_SPEAKER", None)  # don't inherit the real user's setting
    # a bad setting warns once per process: each test starts with none said
    monkeypatch.setattr(config, "_setting_warned", set())
    # the undo journals and rename plans live outside the repo, by default in
    # the REAL Application Support folder: a purge in a test reads and
    # rewrites that folder, so it must be the sandbox's own
    monkeypatch.setenv("STT_MIGRATION_DIR", str(d / "migration"))
    # the Voice Memos sync runs at every batch start: a sandbox run must read
    # a synthetic library, never the real one, even with STT_HOME unset
    monkeypatch.setenv("STT_VOICEMEMOS_DIR", str(d / "vm_container"))
    monkeypatch.delenv("STT_VOICEMEMOS_STAGING", raising=False)
    monkeypatch.delenv("STT_VOICEMEMOS_SYNC", raising=False)
    monkeypatch.delenv("STT_VOICEMEMOS_SINCE", raising=False)
    monkeypatch.delenv("STT_VOICEMEMOS_PLACES", raising=False)
    monkeypatch.setattr(status, "STATUS_PATH", d / "status.json")
    monkeypatch.setattr(status, "HISTORY_LOG", d / "results.jsonl")
    monkeypatch.setattr(control, "PAUSE_FLAG", d / "paused.flag")
    monkeypatch.setattr(control, "STOP_FLAG", d / "stopping.flag")
    monkeypatch.setattr(rates, "RATES_LOG", d / "rates.jsonl")
    monkeypatch.setattr(jobs, "PATH", d / "queued_jobs.json")
    monkeypatch.setattr(jobs, "_LOCK", d / "queued_jobs.lock")
    # queue holds bind their paths at import, so a test that parks a file was
    # writing the REAL holds.json — leaving a phantom hold on the developer's
    # own queue, and leaking that state into the next test run (a file "held"
    # by nobody is skipped by every automatic run, silently, forever)
    monkeypatch.setattr(holds, "PATH", d / "holds.json")
    monkeypatch.setattr(holds, "_LOCK", d / "holds.lock")
    monkeypatch.setattr(rates, "_cache", {"sig": None, "learned": None})
    monkeypatch.setattr(control, "_snap", {"t": 0.0, "pids": [], "mem_mb": 0})
    # HERMETIC process discovery: pgrep sees the REAL machine — without this,
    # stop_run() inside a test finds (and kills!) an actual running batch.
    # Tests exercising group logic use the sandboxed status.json pgid path.
    monkeypatch.setattr(control, "_parent_pids", lambda: [])
    return d


# The board meeting that showed "Voices 22" on the strip and "Voices 21" on its
# To finish row: 22 unnamed diarizer clusters, two of which carry one global
# unknown id (the diarizer split one person) and so both display "Speaker 68".
# Naming U068 names both clusters, so it is ONE voice: 21 voices in 22
# clusters. Four clusters are transcript-local "Voice N" labels with no uid.
# The gather_state test (tests/test_timeline_feed.py) counts it.
SPLIT_VOICE_BASE = "Board Meeting 09232026"
SPLIT_VOICE_UID = "U068"


def split_voice_meeting(base=SPLIT_VOICE_BASE):
    """Write the meeting (json, txt, audio) and its unknown registry into the
    sandbox. Returns (clusters, voices): 22 and 21."""
    import json

    from stt import unknowns
    uids = ["U068", "U068"] + [f"U{n:03d}" for n in range(70, 86)]
    speakers = [{"id": f"SPEAKER_{i:02d}", "name": None, "global_id": u,
                 "display": unknowns.display(u)} for i, u in enumerate(uids)]
    speakers += [{"id": f"SPEAKER_{i:02d}", "name": None, "global_id": None,
                  "display": f"Voice {i - 17}"} for i in range(18, 22)]
    segments = [{"start": i * 10.0, "end": i * 10.0 + 4.0, "speaker": s["id"],
                 "display": s["display"], "text": f"line from {s['id']}"}
                for i, s in enumerate(speakers)]
    d = {"source_file": f"{base}.m4a", "duration_sec": 7200.0,
         "date": "2026-09-23", "speakers": speakers, "segments": segments,
         "words": []}
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    mfile(base, ".m4a").write_bytes(b"audio")
    reg = {"speakers": {u: {"file": f"{u}.npy", "meetings": [base]}
                        for u in dict.fromkeys(uids)}}
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps(reg))
    return len(speakers), len({s["display"] for s in speakers})

