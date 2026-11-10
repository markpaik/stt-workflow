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
    # the neutral default init.sh offers, never a person's own library path
    assert p["meetings"] == str(Path.home() / "Documents/Meeting Transcripts")
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
    assert review.count_decisions("2026-04-08 LT Weekly Meeting – Reserve Target") >= 1


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
