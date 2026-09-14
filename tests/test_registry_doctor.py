"""The registry doctor: find what the audit of 2026-09-13 found by hand, and
repair the part a machine can repair without guessing.

Phase 4 stops new duplicates and new split stacks from forming. This is the
tool for the ones already on disk — plus the reference and lock litter that
accumulates beside them.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import mfile
from stt import config, identify, registry_doctor, unknowns

ROOT = Path(__file__).resolve().parent.parent


def _basis(dim=256, seed=0, n=4):
    rng = np.random.default_rng(seed)
    q, _ = np.linalg.qr(rng.normal(size=(dim, n)))
    return [np.asarray(q[:, i]) for i in range(n)]


def _mix(basis, *coeffs):
    v = sum(c * b for c, b in zip(basis, coeffs))
    return v / np.linalg.norm(v)


def _seed_unknown(uid, rows, meetings, links=None, **extra):
    np.save(config.VOICEPRINTS_DIR / f"{uid}.npy",
            np.vstack([np.asarray(r, float) for r in rows]))
    meta = {"file": f"{uid}.npy", "meetings": list(meetings),
            "meeting_ids": [None] * len(meetings)}
    if links is not None:
        meta["links"] = links
    meta.update(extra)
    reg = unknowns.load()
    reg["speakers"][uid] = meta
    unknowns.save(reg)


def _kinds(findings):
    return sorted({f.kind for f in findings})


def _of(findings, kind):
    return [f for f in findings if f.kind == kind]


def _meeting(base):
    mfile(base, ".json").write_text(json.dumps(
        {"meeting_id": base.replace(" ", "")[:16], "source_file": f"{base}.m4a",
         "segments": [], "speakers": [], "words": []}))


# ---------- duplicate ----------

def test_a_seeded_duplicate_pair_is_found_and_merged(sandbox):
    """One voice under two global numbers — the U028/U040 shape. The old
    checker only caught a byte-identical twin at 0.999; 0.95 catches the same
    person on two different days. The repair keeps the LOWER number, because
    that is the one every other store already references and the monotonic
    counter guarantees it is never reissued."""
    e = _basis(seed=1)
    v = e[0]
    w = _mix(e, 0.97, np.sqrt(1 - 0.97 ** 2))
    _seed_unknown("U004", [v], ["M1"])
    _seed_unknown("U009", [w], ["M2"])

    found = _of(registry_doctor.check(), "duplicate")
    assert [f.subject for f in found] == ["U004/U009"]
    assert found[0].fixable
    assert "one voice under two global numbers" in found[0].detail

    done = registry_doctor.fix(registry_doctor.check())
    assert any("merged U009 into U004" in line for line in done)
    reg = unknowns.load()["speakers"]
    assert set(reg) == {"U004"}
    assert reg["U004"]["meetings"] == ["M2", "M1"] or \
        sorted(reg["U004"]["meetings"]) == ["M1", "M2"]
    assert _of(registry_doctor.check(), "duplicate") == []


def test_two_enrolled_people_sharing_a_voice_are_reported_not_merged(sandbox):
    """A human decides this one. Two named profiles at 0.97 are either a
    mis-enrolled sample or one person under two spellings, and merging them
    destroys a distinction the tool cannot recover."""
    e = _basis(seed=2)
    identify.enroll("Dana Cole", e[0], source="M1")
    identify.enroll("Dana C.", _mix(e, 0.97, np.sqrt(1 - 0.97 ** 2)), source="M2")

    found = _of(registry_doctor.check(), "duplicate")
    assert [f.subject for f in found] == ["Dana C./Dana Cole"]
    assert found[0].fixable is False

    registry_doctor.fix(registry_doctor.check())
    assert set(identify.load_registry()) == {"Dana Cole", "Dana C."}
    assert _of(registry_doctor.check(), "duplicate")     # still reported


# ---------- split ----------

def test_a_seeded_split_is_unlinked_into_a_fresh_id_and_its_links_follow(sandbox):
    """U050's shape on disk: one number whose own samples disagree at 0.30.
    The majority keeps the number; the minority moves to a number nobody has
    ever held, carrying its meeting reference and its link evidence with it."""
    e = _basis(seed=3)
    a = e[0]
    b = _mix(e, 0.9, np.sqrt(1 - 0.9 ** 2))
    z = _mix(e, 0.3, 0.0, np.sqrt(1 - 0.09))
    links = [{"meeting": "M1", "meeting_id": None, "cluster": "SPEAKER_00",
              "score": 1.0},
             {"meeting": "M2", "meeting_id": None, "cluster": "SPEAKER_01",
              "score": 0.9},
             {"meeting": "M3", "meeting_id": None, "cluster": "SPEAKER_07",
              "score": 0.72}]
    _seed_unknown("U050", [a, b, z], ["M1", "M2", "M3"], links=links)
    reg = unknowns.load()
    reg["next"] = 51
    unknowns.save(reg)

    found = _of(registry_doctor.check(), "split")
    assert [f.subject for f in found] == ["U050"]
    assert found[0].fixable
    assert "min pairwise cosine 0.270" in found[0].detail   # b against z

    registry_doctor.fix(registry_doctor.check())
    reg = unknowns.load()["speakers"]
    assert set(reg) == {"U050", "U051"}

    kept = np.load(config.VOICEPRINTS_DIR / "U050.npy")
    assert kept.shape[0] == 2                       # a and b stayed together
    assert reg["U050"]["meetings"] == ["M1", "M2"]
    assert [ln["cluster"] for ln in reg["U050"]["links"]] == ["SPEAKER_00",
                                                              "SPEAKER_01"]

    moved = np.load(config.VOICEPRINTS_DIR / "U051.npy")
    assert moved.shape[0] == 1
    assert float(moved[0] @ z) == pytest.approx(1.0, abs=1e-9)
    assert reg["U051"]["meetings"] == ["M3"]
    assert reg["U051"]["unlinked_from"] == "U050"
    assert reg["U051"]["links"] == [links[2]]

    assert _of(registry_doctor.check(), "split") == []


# ---------- dangling ----------

def test_a_dangling_reference_is_reported_and_dropped(sandbox):
    """A reference whose base AND id both resolve to nothing is dead weight:
    it drives a ▶ button that cannot play and a 'heard in N meetings' count
    that is wrong."""
    e = _basis(seed=4)
    _meeting("Live 05012026")
    _seed_unknown("U001", [e[0]], ["Live 05012026", "Deleted 01012020"],
                  links=[{"meeting": "Deleted 01012020", "meeting_id": None,
                          "cluster": "SPEAKER_02", "score": 0.8}])
    identify.enroll("Alice", e[1], source="Deleted 01012020")

    found = _of(registry_doctor.check(), "dangling")
    assert sorted(f.subject for f in found) == ["Alice", "U001"]
    assert all(f.fixable for f in found)

    registry_doctor.fix(registry_doctor.check())
    meta = unknowns.load()["speakers"]["U001"]
    assert meta["meetings"] == ["Live 05012026"]
    assert meta["links"] == []
    # a named profile's sources are index-aligned with its sample ROWS, so the
    # dead one is blanked rather than removed — dropping it would misalign
    # every sample after it with the meeting it came from
    assert identify.load_registry()["Alice"]["sources"] == ["?"]
    assert _of(registry_doctor.check(), "dangling") == []


def test_dangling_is_not_reported_when_the_library_is_unreadable(sandbox):
    """An empty meetings store makes every reference look dead. That is a
    statement about the store, not the registry, so the check stays quiet
    rather than telling the user to delete everything."""
    e = _basis(seed=5)
    _seed_unknown("U001", [e[0]], ["Some Meeting 01012026"])
    assert config.meeting_bases() == []
    assert _of(registry_doctor.check(), "dangling") == []


# ---------- orphan locks and the counter ----------

def test_orphan_locks_and_a_stale_counter_are_found_and_healed(sandbox):
    e = _basis(seed=6)
    _meeting("Live 05012026")
    locks = config.meetings_dir() / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / "gone-meeting-id.lock").write_text("")
    _seed_unknown("U040", [e[0]], ["Live 05012026"])
    raw = json.loads((config.VOICEPRINTS_DIR / "unknowns.json").read_text())
    raw["next"] = 6                       # what the live registry actually holds
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps(raw))

    found = registry_doctor.check()
    assert _of(found, "orphan_lock")[0].subject == "gone-meeting-id.lock"
    stale = _of(found, "stale_next")
    assert stale and "next=6 is below max+1=41" in stale[0].detail

    done = registry_doctor.fix(found)
    assert any("orphan lock" in line for line in done)
    assert any("healed unknowns.json next: 6 -> 41" in line for line in done)
    assert not (locks / "gone-meeting-id.lock").exists()
    assert json.loads(
        (config.VOICEPRINTS_DIR / "unknowns.json").read_text())["next"] == 41
    assert _kinds(registry_doctor.check()) == []


# ---------- the CLI ----------

def _run(tmp, *args):
    env = dict(os.environ, STT_HOME=str(tmp))
    for k in ("STT_PROJECT_DIR", "STT_VOICEPRINTS_DIR", "STT_MEETINGS_DIR",
              "STT_ICLOUD_DIR", "STT_RECORDINGS_DIR"):
        env.pop(k, None)
    return subprocess.run([sys.executable, str(ROOT / "tools/registry_doctor.py"),
                           *args], capture_output=True, text=True, env=env,
                          cwd=str(ROOT))


def test_the_cli_reports_without_writing_then_fixes(sandbox, tmp_path):
    """Report is the default and exits 1 on any finding; --fix exits 0 only
    when nothing is left. The subprocess gets its own STT_HOME, so it reads
    the sandbox and can never reach the real registry."""
    home = tmp_path / "home"
    (home / "voiceprints").mkdir(parents=True)
    (home / "meetings").mkdir(parents=True)
    e = _basis(seed=7)
    v, w = e[0], _mix(e, 0.97, np.sqrt(1 - 0.97 ** 2))
    np.save(home / "voiceprints/U001.npy", v.reshape(1, -1))
    np.save(home / "voiceprints/U002.npy", w.reshape(1, -1))
    (home / "voiceprints/unknowns.json").write_text(json.dumps({"next": 1, "speakers": {
        "U001": {"file": "U001.npy", "meetings": ["M1"]},
        "U002": {"file": "U002.npy", "meetings": ["M2"]}}}))

    r = _run(home)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "U001/U002" in r.stdout
    # report mode wrote nothing
    assert json.loads((home / "voiceprints/unknowns.json").read_text())["next"] == 1

    r = _run(home, "--fix")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "merged U002 into U001" in r.stdout
    left = json.loads((home / "voiceprints/unknowns.json").read_text())
    assert set(left["speakers"]) == {"U001"} and left["next"] == 3

    assert _run(home).returncode == 0


def test_the_cli_exits_1_when_a_finding_cannot_be_fixed(sandbox, tmp_path):
    """Two enrolled people sharing a voice is report-only, so --fix still
    exits 1 — a CI step or a cron gate must not read that as clean."""
    home = tmp_path / "home"
    (home / "voiceprints").mkdir(parents=True)
    (home / "meetings").mkdir(parents=True)
    e = _basis(seed=8)
    np.save(home / "voiceprints/a.npy", e[0].reshape(1, -1))
    np.save(home / "voiceprints/b.npy",
            _mix(e, 0.97, np.sqrt(1 - 0.97 ** 2)).reshape(1, -1))
    (home / "voiceprints/registry.json").write_text(json.dumps({
        "Dana Cole": {"file": "a.npy", "n_samples": 1, "sources": ["M1"]},
        "Dana C.": {"file": "b.npy", "n_samples": 1, "sources": ["M2"]}}))

    r = _run(home, "--fix")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "two enrolled profiles share a voice" in r.stdout
    assert set(json.loads((home / "voiceprints/registry.json").read_text())) == \
        {"Dana Cole", "Dana C."}
