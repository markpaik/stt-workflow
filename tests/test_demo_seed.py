"""tools/demo_seed.py builds a synthetic data home; verify it (a) loads through
the REAL loaders the panel uses — never the seeder's own code — and (b) that the
STT_HOME override in config.py redirects every data path at import time while
staying backwards compatible. Also covers the CLI's refuse-to-clobber rails."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import demo_seed  # noqa: E402

from stt import config, holds, identify, jobs, manifest, review, status, unknowns  # noqa: E402
from conftest import decision_count  # noqa: E402


# --------------------------------------------------------------- override ---

def _run_config(env_extra):
    """Import stt.config in a FRESH interpreter (STT_HOME is import-time only) and
    dump the paths it resolves. env_extra may include STT_HOME; all other STT_*
    vars are stripped so the parent's real settings can't leak in."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env["PYTHONPATH"] = str(REPO)
    env.update(env_extra)
    code = (
        "import json; import stt.config as c; "
        "print(json.dumps({'home': c.STT_HOME, 'project': str(c.PROJECT_DIR), "
        "'meetings': str(c.MEETINGS_DIR), 'source': str(c.ICLOUD_DIR), "
        "'recordings': str(c.RECORDINGS_DIR), 'voiceprints': str(c.VOICEPRINTS_DIR), "
        "'manifest': str(c.MANIFEST_PATH)}))"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(REPO),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_stt_home_backwards_compatible():
    """Unset STT_HOME => paths are the real defaults, exactly as before."""
    p = _run_config({})
    assert p["home"] is None
    # the checkout itself, under whatever name its folder carries: it was
    # "STT_workflow" once and is "stt_workflow" now, and a worktree is named
    # for its branch. PROJECT_DIR defaults to the folder holding stt/.
    assert Path(p["project"]).name == REPO.name
    # the neutral default init.sh offers, never a person's own library path,
    # unless this checkout's own stt.env names the library (an owner's
    # checkout does; a fresh clone and a worktree do not)
    from stt import envfile
    own = envfile.parse(REPO / "stt.env").get("STT_MEETINGS_DIR") if (REPO / "stt.env").exists() else None
    assert p["meetings"] == str(Path(own).expanduser() if own else
                                Path.home() / "Documents/Meeting Transcripts")
    assert Path(p["voiceprints"]).name == "voiceprints"


def test_the_default_transcripts_folder_is_the_one_init_offers():
    """config.py defaulted to the owner's own library path while init.sh
    offered ~/Documents/Meeting Transcripts. One neutral default now, and
    no code path names a person's folder."""
    import re
    init = (REPO / "init.sh").read_text()
    m = re.search(r'^DEFAULT_DST="\$HOME/([^"]+)"$', init, re.M)
    assert m and Path.home() / m.group(1) == config.DEFAULT_MEETINGS_DIR
    for d in ("stt", "gui", "tools"):
        for f in (REPO / d).rglob("*.py"):
            assert "Projects/brain" not in f.read_text(), f
    for f in ("init.sh", "setup.sh", "run.sh", "run_batch.py", "stt.env.example"):
        assert "Projects/brain" not in (REPO / f).read_text(), f


def test_a_process_without_stt_env_in_its_environment_reads_the_folders_from_it(tmp_path):
    """launchd starts the menu bar (and its panel) without stt.env in the
    environment, so config.MEETINGS_DIR there was the default wherever the
    library really was. The import-time folders now read stt.env too, and
    a variable set in the environment still wins."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "stt.env").write_text(f'STT_MEETINGS_DIR="{tmp_path}/lib"\n'
                                  f"STT_ICLOUD_DIR={tmp_path}/src\n")
    p = _run_config({"STT_PROJECT_DIR": str(proj)})
    assert p["meetings"] == str(tmp_path / "lib")
    assert p["source"] == str(tmp_path / "src")
    p = _run_config({"STT_PROJECT_DIR": str(proj), "STT_MEETINGS_DIR": str(tmp_path / "env")})
    assert p["meetings"] == str(tmp_path / "env")


def test_stt_home_redirects_every_state_path(tmp_path):
    home = tmp_path / "home"
    p = _run_config({"STT_HOME": str(home)})
    assert p["home"] == str(home)
    assert Path(p["project"]) == home
    assert Path(p["meetings"]) == home / "meetings"
    assert Path(p["source"]) == home / "source"
    assert Path(p["recordings"]) == home / "recordings"
    assert Path(p["voiceprints"]) == home / "voiceprints"
    assert Path(p["manifest"]) == home / "manifest.json"


def test_specific_env_var_still_wins_over_home(tmp_path):
    home = tmp_path / "home"
    custom = tmp_path / "elsewhere"
    p = _run_config({"STT_HOME": str(home), "STT_MEETINGS_DIR": str(custom)})
    assert Path(p["meetings"]) == custom            # explicit override wins
    assert Path(p["voiceprints"]) == home / "voiceprints"  # others still under the home


def test_state_modules_follow_home_at_import(tmp_path):
    """status/holds/jobs/manifest paths hang off PROJECT_DIR, so STT_HOME moves
    them too (this is what lets the panel's history/holds/queue redirect)."""
    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env["PYTHONPATH"] = str(REPO)
    env["STT_HOME"] = str(home)
    code = (
        "from stt import status, holds, jobs, config; "
        "print(status.STATUS_PATH); print(status.HISTORY_LOG); "
        "print(holds.PATH); print(jobs.PATH)"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(REPO),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines == [str(home / "status.json"), str(home / "results.jsonl"),
                     str(home / "holds.json"), str(home / "queued_jobs.json")]


# ------------------------------------------------ loads via REAL loaders ---

@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Seed a home, then repoint every real loader at it (the in-process
    equivalent of STT_HOME), so the assertions run through the panel's own
    loaders — config, meeting json, review, speaker store, history."""
    home = tmp_path / "demo_home"
    demo_seed.build(home)
    monkeypatch.setattr(config, "PROJECT_DIR", home)
    monkeypatch.setattr(config, "MEETINGS_DIR", home / "meetings")
    monkeypatch.setattr(config, "ICLOUD_DIR", home / "source")
    monkeypatch.setattr(config, "RECORDINGS_DIR", home / "recordings")
    monkeypatch.setattr(config, "VOICEPRINTS_DIR", home / "voiceprints")
    monkeypatch.setattr(config, "MANIFEST_PATH", home / "manifest.json")
    monkeypatch.setattr(status, "STATUS_PATH", home / "status.json")
    monkeypatch.setattr(status, "HISTORY_LOG", home / "results.jsonl")
    monkeypatch.setattr(holds, "PATH", home / "holds.json")
    monkeypatch.setattr(jobs, "PATH", home / "queued_jobs.json")
    return home


def test_meetings_load_through_config(seeded):
    bases = config.meeting_bases()
    assert len(bases) >= 8
    # month grouping needs a stored ISO date on every meeting; >= 3 months, 2 years
    months, years = set(), set()
    for b in bases:
        d = json.loads(config.meeting_file(b, ".json").read_text())
        assert d["date"], f"{b} has no stored date"
        months.add(d["date"][:7])
        years.add(d["date"][:4])
    assert len(months) >= 3 and len(years) >= 2
    assert config.archived_bases() == ["2025-03-04 Old Sync Meeting"]


def test_inbox_meeting_flagged_unreviewed(seeded):
    inbox = [b for b in config.meeting_bases()
             if json.loads(config.meeting_file(b, ".json").read_text()).get("reviewed") is False]
    assert inbox == ["2026-07-10 Recording 0915"]
    # and processed meetings must NOT read as needing review
    others = [b for b in config.meeting_bases() if b not in inbox]
    assert all(json.loads(config.meeting_file(b, ".json").read_text()).get("reviewed") is not False
               for b in others)


def test_review_flags_load_with_alt_and_minor(seeded):
    out = review.list_flagged("2026-04-08 LT Weekly Meeting – Reserve Target")
    assert out["items"], "review meeting should have flagged segments"
    assert out["n_minor"] >= 1
    assert any(not it["minor"] for it in out["items"])          # substantial ones too
    alts = [it["alt"] for it in out["items"] if it.get("alt")]
    assert alts and alts[0][0]["theirs"], "second-engine alternative text present"
    assert decision_count("2026-04-08 LT Weekly Meeting – Reserve Target") >= 1


def test_speaker_store_loads(seeded):
    reg = identify.load_registry()
    assert set(reg) >= {"Morgan Ellis", "Alex Rivera", "Jordan Lee", "Priya Shah",
                        "Sam Chen", "Dana Fox"}
    vps = identify.load_voiceprints()
    for name, arr in vps.items():
        assert arr.ndim == 2 and arr.shape[1] == demo_seed.EMB_DIM
    sp = unknowns.load()["speakers"]
    active = [u for u, m in sp.items() if not m.get("archived")]
    hidden = [u for u, m in sp.items() if m.get("archived")]
    assert active and hidden, "need one unknown ('Who is this?') and one hidden"
    # the active unknown must be playable: it names a real meeting it was heard in
    uid = active[0]
    assert review.find_voice_clips(uid, sp[uid]["meetings"][0], n=1)


def test_a_fresh_seed_passes_the_registry_doctor(seeded, monkeypatch):
    """unknowns.json had no "next" key, so registry_doctor reported the
    fresh seed as stale_next and exited 1: the doctor's exit-status gate
    could not be shown on the demo."""
    from stt import registry_doctor
    monkeypatch.setattr(config, "MEETINGS_INDEX_PATH", seeded / "meetings_index.json")
    raw = json.loads((seeded / "voiceprints" / "unknowns.json").read_text())
    assert raw["next"] == unknowns.next_floor(raw)
    assert registry_doctor.check(seeded / "voiceprints", seeded / "meetings") == []


def test_history_has_success_and_failure(seeded):
    rows = status.history()
    assert len(rows) >= 8
    fails = [r for r in rows if not r["ok"]]
    assert len(fails) == 1
    assert "ffmpeg" in fails[0]["summary"] and "moov atom" in fails[0]["summary"]
    assert all(r.get("at") for r in rows)


def test_queue_holds_and_manifest(seeded):
    assert holds.items() == {"Draft Personal Memo 07112026.wav"}
    assert jobs.items() == []            # empty: gather_state must not auto-spawn
    m = manifest.load()
    assert m["processed"], "manifest should record processed meetings"
    # a waiting queue file exists and is NOT marked processed
    waiting = config.source_dir() / "Team Standup 07112026 0900.wav"
    assert waiting.exists()
    assert not manifest.is_processed(m, waiting.name, waiting.stat().st_mtime)


def test_seeded_home_carries_a_sandbox_launchd_plist(seeded):
    """gui/server.py resolves AGENT under STT_HOME, so the home ships a plist
    copy: the panel's Automation section and /api/schedule exercise it (and
    only it) — launchctl is skipped entirely under STT_HOME."""
    import plistlib
    p = seeded / "LaunchAgents" / "com.stt-workflow.batch.plist"
    assert p.exists()
    d = plistlib.loads(p.read_bytes())
    assert d["Label"] == "com.stt-workflow.batch"
    assert d["StartCalendarInterval"] == {"Hour": 2, "Minute": 0}
    assert d["WatchPaths"] == [str(seeded / "source"), str(seeded / "recordings")]
    assert d["ProgramArguments"] == ["/usr/bin/true"]   # inert: launchd never sees it


def test_meeting_audio_is_real_and_segments_fit(seeded):
    import wave
    for b in config.meeting_bases():
        ap = config.meeting_audio(b)
        assert ap is not None, f"{b} has no audio"
        with wave.open(str(ap)) as w:
            dur = w.getnframes() / w.getframerate()
        segs = json.loads(config.meeting_file(b, ".json").read_text())["segments"]
        assert max(s["end"] for s in segs) <= dur + 0.05


# --------------------------------------------------------- CLI safety ------

def test_refuses_without_dir():
    with pytest.raises(SystemExit):
        demo_seed.main([])


def test_refuses_dir_overlapping_real_data(tmp_path, monkeypatch):
    real = tmp_path / "real_meetings"
    real.mkdir()
    monkeypatch.setattr(config, "MEETINGS_DIR", real)
    monkeypatch.setattr(config, "ICLOUD_DIR", tmp_path / "src")
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "rec")
    with pytest.raises(SystemExit):
        demo_seed.main(["--dir", str(real)])            # exact overlap
    with pytest.raises(SystemExit):
        demo_seed.main(["--dir", str(real / "inside")])  # nested under real data


def test_the_guard_folds_letter_case_as_apfs_does(tmp_path, monkeypatch):
    """resolve() keeps the case it is given and APFS folds it, so
    ~/projects/brain/meetings/demo_x passed while ~/Projects/... was
    refused. synth_corpus uses the same compare."""
    real = tmp_path / "Real Meetings"
    real.mkdir()
    monkeypatch.setattr(config, "MEETINGS_DIR", real)
    monkeypatch.setattr(config, "ICLOUD_DIR", tmp_path / "src")
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "rec")
    for t in (tmp_path / "real meetings" / "demo_x", tmp_path / "REAL MEETINGS",
              Path(str(tmp_path).upper()) / "Real Meetings"):
        with pytest.raises(SystemExit, match="overlaps a real configured"):
            demo_seed.resolve_target(str(t))
    # composed and decomposed accents are one name on APFS too
    assert demo_seed._overlaps(Path("/a/Caf\u00e9/x"), Path("/a/cafe\u0301"))
    assert not demo_seed._overlaps(Path("/a/Real Meetings2"), Path("/a/real meetings"))
    assert demo_seed.resolve_target(str(tmp_path / "demo")) == (tmp_path / "demo").resolve()


def _checkout_copy(tmp_path, env_text):
    """A copy of the code (stt/ and tools/demo_seed.py) whose own stt.env
    names a synthetic live library, so every live folder is a tmp path."""
    import shutil
    co = tmp_path / "checkout"
    shutil.copytree(REPO / "stt", co / "stt",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (co / "tools").mkdir()
    shutil.copy(REPO / "tools" / "demo_seed.py", co / "tools" / "demo_seed.py")
    (co / "stt.env").write_text(env_text)
    return co


def _resolve_in(co, env_extra, *targets):
    """resolve_target(t) for each target, in a fresh interpreter run from
    the copy `co`: 'ACCEPTED <path>' or 'REFUSED <why>' per target. It
    writes nothing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update(env_extra, PYTHONPATH=str(co))
    code = ("import sys; sys.path.insert(0, 'tools'); import demo_seed\n"
            "for t in sys.argv[1:]:\n"
            "    try: print('ACCEPTED', demo_seed.resolve_target(t))\n"
            "    except SystemExit as e: print('REFUSED', e)\n")
    r = subprocess.run([sys.executable, "-c", code, *map(str, targets)], env=env,
                       cwd=str(co), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_the_guard_sees_the_live_library_with_stt_home_exported(tmp_path):
    """_real_data_dirs said it read the folders without STT_HOME, but
    nothing cleared it. With STT_HOME exported every folder it listed was
    inside the scratch home, and a --dir inside the real library passed."""
    live = (tmp_path / "Live Library").resolve()
    rec = (tmp_path / "live rec").resolve()
    co = _checkout_copy(tmp_path, f"STT_MEETINGS_DIR={live}\nSTT_RECORDINGS_DIR={rec}\n")
    home = tmp_path / "scratch"
    out = _resolve_in(co, {"STT_HOME": str(home),
                           "STT_MEETINGS_DIR": str(home / "meetings")},
                      live / "demo_x", rec, co / "voiceprints" / "x",
                      tmp_path / "elsewhere")
    assert out[0] == (f"REFUSED refusing: --dir {live / 'demo_x'} overlaps a real "
                      f"configured data folder ({live}). Pick a throwaway path "
                      "such as qa/demo_home.")
    assert out[1].startswith("REFUSED") and out[2].startswith("REFUSED")
    assert out[3] == f"ACCEPTED {(tmp_path / 'elsewhere').resolve()}"
    # the run without STT_HOME refuses the same way, as before
    assert _resolve_in(co, {}, live / "demo_x")[0].startswith("REFUSED")


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    *args], cwd=str(cwd), check=True, capture_output=True)


def test_the_guard_run_from_a_worktree_sees_the_main_checkouts_library(tmp_path):
    """The guard read only its own checkout's stt.env. A worktree has
    none, so it listed the defaults and a --dir inside the real library
    (named in the main checkout's stt.env) passed."""
    import shutil
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    (main / "README").write_text("x\n")
    _git("add", "README", cwd=main)
    _git("commit", "-q", "-m", "init", cwd=main)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), cwd=main)
    shutil.copytree(REPO / "stt", wt / "stt",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (wt / "tools").mkdir()
    shutil.copy(REPO / "tools" / "demo_seed.py", wt / "tools" / "demo_seed.py")
    assert not (wt / "stt.env").exists()
    live = (tmp_path / "Live Library").resolve()
    src = (tmp_path / "Live Source").resolve()
    (main / "stt.env").write_text(f"STT_MEETINGS_DIR={live}\nSTT_ICLOUD_DIR={src}\n")
    fake_home = tmp_path / "user"   # no batch agent plist
    out = _resolve_in(wt, {"HOME": str(fake_home)}, live / "demo_x", src,
                      main / "voiceprints" / "x", tmp_path / "elsewhere")
    assert out[0].startswith(f"REFUSED refusing: --dir {live / 'demo_x'} overlaps")
    assert out[1].startswith("REFUSED") and out[2].startswith("REFUSED")
    assert out[3] == f"ACCEPTED {(tmp_path / 'elsewhere').resolve()}"


def test_the_guard_sees_the_library_of_the_batch_agents_checkout(tmp_path):
    """The batch agent runs from its plist's WorkingDirectory. That
    checkout's stt.env names the live library, wherever the seeder runs."""
    import plistlib
    live = (tmp_path / "Agent Library").resolve()
    agent_co = tmp_path / "agent checkout"
    agent_co.mkdir()
    (agent_co / "stt.env").write_text(f'STT_MEETINGS_DIR="{live}"\n')
    fake_home = tmp_path / "user"
    plist = fake_home / demo_seed.BATCH_PLIST_NAME
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"Label": "com.stt-workflow.batch",
                                      "WorkingDirectory": str(agent_co)}))
    co = _checkout_copy(tmp_path, "")
    out = _resolve_in(co, {"HOME": str(fake_home)}, live / "demo_x",
                      agent_co / "voicememos", tmp_path / "elsewhere")
    assert out[0].startswith(f"REFUSED refusing: --dir {live / 'demo_x'} overlaps")
    assert out[1].startswith("REFUSED")
    assert out[2].startswith("ACCEPTED")
    # a damaged plist is no guard, never a crash
    plist.write_bytes(b"not a plist")
    assert _resolve_in(co, {"HOME": str(fake_home)}, tmp_path / "elsewhere")[0] \
        .startswith("ACCEPTED")


def test_wipe_only_removes_a_home_it_built(tmp_path):
    target = tmp_path / "home"
    demo_seed.main(["--dir", str(target)])
    assert (target / demo_seed.MARKER).exists()
    # re-run without --wipe: refuse
    with pytest.raises(SystemExit):
        demo_seed.main(["--dir", str(target)])
    # a non-empty dir WITHOUT our marker must never be wiped
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / "precious.txt").write_text("do not delete")
    with pytest.raises(SystemExit):
        demo_seed.main(["--dir", str(stranger), "--wipe"])
    assert (stranger / "precious.txt").exists()
    # with --wipe on our own marked home: rebuilds cleanly
    demo_seed.main(["--dir", str(target), "--wipe"])
    assert config.meeting_file  # sanity: module intact


def test_a_fresh_demo_home_is_doctor_clean(tmp_path):
    """Each seeded meeting lacked the store keys the pipeline writes (title,
    source_name, audio_fp, date_source, recorded_at) and its meeting_id,
    and unknowns.json had no `next`, so a fresh home failed `doctor.py
    check` with missing_keys on every meeting and a registry_stale_next.
    The seed also used to build folder names with a colon the sanitizer
    never writes. The check runs in a fresh interpreter under STT_HOME, as
    the finding's repro did. The two diarizer findings speak about the
    machine (a Hugging Face token, a built speakrs helper), not the home,
    so the check runs on a machine that has both settled."""
    from stt import provenance
    home = tmp_path / "demo_home"
    demo_seed.build(home)
    for j in sorted((home / "meetings").glob("*/*.json")):
        if j.name.endswith(".reviews.json"):
            continue
        d = json.loads(j.read_text())
        assert next(iter(d)) == "meeting_id" and len(d["meeting_id"]) == 16, j
        assert all(k in d for k in provenance.KEYS), j
        assert d["title"] and d["audio_fp"], j
        if d["recorded_at"]:
            assert d["date_source"] == "recorder" and d["recorded_at"][:10] == d["date"]
        assert ":" not in j.parent.name, j
    reg = json.loads((home / "voiceprints" / "unknowns.json").read_text())
    assert reg["next"] == unknowns.next_floor(reg)
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update({"STT_HOME": str(home), "STT_OLLAMA_URL": "off", "PYTHONPATH": str(REPO),
                "HF_TOKEN": "hf_synthetic", "STT_DIARIZER": "pyannote"})
    out = subprocess.run([sys.executable, str(REPO / "tools" / "doctor.py"), "check"],
                         env=env, cwd=str(REPO), capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "clean: nothing to report" in out.stdout


def test_a_recorder_meeting_starts_at_the_time_its_name_says(tmp_path):
    """recorder.final_name names a capture 'YYYY-MM-DD Recording HHMM' by
    its start, and provenance dates such a name 'recorder' at that time.
    The seed took the start from the processing time instead, so Home and
    the README screenshots showed 'Recording 1430' at 3:01 PM, and it read
    'Recording 0915' as a typed date with no time."""
    home = tmp_path / "demo_home"
    demo_seed.build(home)
    today = demo_seed.date.today().isoformat()
    for base, start in ((f"{today} Recording 1430", f"{today}T14:30"),
                        ("2026-07-10 Recording 0915", "2026-07-10T09:15")):
        d = json.loads((home / "meetings" / base / f"{base}.json").read_text())
        assert d["date_source"] == "recorder", base
        assert d["recorded_at"][:16] == start, base
    # a memo with no time in its name still starts before it was processed
    j = next((home / "meetings").glob("*Reading Partner Kickoff/*Kickoff.json"))
    d = json.loads(j.read_text())
    assert d["recorded_at"][:10] == d["date"]
    assert d["recorded_at"][11:16] < d["processed_at"][11:16]


def test_a_fresh_demo_home_is_registry_doctor_clean(tmp_path):
    """The README points at tools/registry_doctor.py for the voice
    registries alone, and the phase 8 plan asks for a demo home it passes
    clean. Run it as a person would: a fresh interpreter under STT_HOME,
    report only. Exit 0 means no finding of any kind, and the run writes
    nothing in the voiceprints folder."""
    home = tmp_path / "demo_home"
    demo_seed.build(home)
    vp = home / "voiceprints"
    before = {p.name: p.stat().st_mtime_ns for p in vp.iterdir()}
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update({"STT_HOME": str(home), "STT_OLLAMA_URL": "off", "PYTHONPATH": str(REPO)})
    out = subprocess.run([sys.executable, str(REPO / "tools" / "registry_doctor.py")],
                         env=env, cwd=str(REPO), capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "clean — nothing to report" in out.stdout
    assert {p.name: p.stat().st_mtime_ns for p in vp.iterdir()} == before


def test_a_sandbox_never_finds_the_real_mlx_model(tmp_path):
    """Code resolves from the checkout (config.CODE_DIR: run.sh, .venv,
    the menu-bar icon). The MLX model's own environment is the exception:
    under STT_HOME it resolves inside the home, so a demo or QA run with
    Ollama off never loads the real 8B model."""
    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update(PYTHONPATH=str(REPO), STT_HOME=str(home), STT_OLLAMA_URL="off")
    code = ("import json; from stt import config, summarize; "
            "print(json.dumps({'llm': str(summarize.LLM_PY), "
            "'code': str(config.CODE_DIR), 'local': summarize.backend_available('local')}))")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(REPO),
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    p = json.loads(out.stdout.strip().splitlines()[-1])
    assert p["llm"] == str(home / ".venv-llm" / "bin" / "python")
    assert p["code"] == str(REPO)
    assert p["local"] is False
