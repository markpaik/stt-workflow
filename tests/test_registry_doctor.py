"""The registry doctor: find what the audit of 2026-09-13 found by hand, and
repair the part a machine can repair without guessing.

Phase 4 stops new duplicates and new split stacks from forming. This is the
tool for the ones already on disk — plus the reference and lock litter that
accumulates beside them.
"""
import hashlib
import json
import os
import shutil
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


# ---------- the directory a repair lands in ----------

def _hashes(d: Path) -> dict:
    """Every file under `d`, by relative path, with its content hash."""
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}


def _seed_every_fixable_defect():
    """One of each voiceprint defect fix() repairs, in the configured dir."""
    for b in ("Live 05012026", "Live 05022026", "Live 05032026"):
        _meeting(b)
    e = _basis(seed=9)
    f = _basis(seed=10)
    # duplicate: one voice under two global numbers
    _seed_unknown("U004", [f[0]], ["Live 05012026"])
    _seed_unknown("U009", [_mix(f, 0.97, np.sqrt(1 - 0.97 ** 2))], ["Live 05022026"])
    # split: one stack spanning two voices
    _seed_unknown("U050", [e[0], _mix(e, 0.9, np.sqrt(1 - 0.9 ** 2)),
                           _mix(e, 0.3, 0.0, np.sqrt(1 - 0.09))],
                  ["Live 05012026", "Live 05022026", "Live 05032026"])
    # dangling: a meeting ref and a sample source that name nothing on disk
    _seed_unknown("U001", [f[3]], ["Live 05012026", "Deleted 01012020"])
    identify.enroll("Alice", f[2], source="Deleted 01012020")
    # stale counter
    raw = json.loads((config.VOICEPRINTS_DIR / "unknowns.json").read_text())
    raw["next"] = 6
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(json.dumps(raw))


def test_fix_repairs_the_directory_it_checked_and_never_the_configured_one(
        sandbox, tmp_path):
    """fix() took voiceprints_dir but every repair read and wrote
    config.VOICEPRINTS_DIR, so `--fix --voiceprints DIR` checked DIR and
    then merged, split and rewrote the CONFIGURED registry. Two directories
    with the same defects: the one named gets every repair, and the
    configured one is byte-for-byte what it was."""
    _seed_every_fixable_defect()
    home = config.VOICEPRINTS_DIR
    other = tmp_path / "other_voiceprints"
    shutil.copytree(home, other)
    before_home = _hashes(home)

    findings = registry_doctor.check(other)
    assert {"duplicate", "split", "dangling", "stale_next"} <= set(_kinds(findings))
    done = registry_doctor.fix(findings, other)

    assert _hashes(home) == before_home, \
        "a repair meant for the named dir landed in the configured registry"
    assert config.VOICEPRINTS_DIR == home, "the configured dir was not put back"

    assert "merged U009 into U004" in done
    raw = json.loads((other / "unknowns.json").read_text())
    assert set(raw["speakers"]) == {"U001", "U004", "U050", "U051"}
    assert raw["speakers"]["U051"]["unlinked_from"] == "U050"
    assert raw["speakers"]["U001"]["meetings"] == ["Live 05012026"]
    assert raw["next"] == 52
    assert json.loads((other / "registry.json").read_text())["Alice"]["sources"] == ["?"]
    assert [f for f in registry_doctor.check(other) if f.fixable] == []


def test_the_cli_fix_with_voiceprints_never_touches_the_configured_dir(
        sandbox, tmp_path):
    """The same guarantee through the command a person actually runs: the
    configured registry under STT_HOME keeps every file hash, and the
    directory passed as --voiceprints is the one that gets merged."""
    home = tmp_path / "home"
    (home / "voiceprints").mkdir(parents=True)
    (home / "meetings").mkdir(parents=True)
    e = _basis(seed=11)
    np.save(home / "voiceprints/U001.npy", e[0].reshape(1, -1))
    np.save(home / "voiceprints/U002.npy",
            _mix(e, 0.97, np.sqrt(1 - 0.97 ** 2)).reshape(1, -1))
    (home / "voiceprints/unknowns.json").write_text(json.dumps({"next": 1, "speakers": {
        "U001": {"file": "U001.npy", "meetings": ["M1"]},
        "U002": {"file": "U002.npy", "meetings": ["M2"]}}}))
    other = tmp_path / "other_voiceprints"
    shutil.copytree(home / "voiceprints", other)
    before_home = _hashes(home / "voiceprints")

    r = _run(home, "--fix", "--voiceprints", str(other))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "merged U002 into U001" in r.stdout
    assert _hashes(home / "voiceprints") == before_home, \
        "--fix --voiceprints DIR rewrote the configured registry"
    left = json.loads((other / "unknowns.json").read_text())
    assert set(left["speakers"]) == {"U001"} and left["next"] == 3


def test_fix_puts_the_configured_dir_back_when_a_repair_raises(
        sandbox, tmp_path, monkeypatch):
    """The repoint is undone in a finally: a repair that raises must not
    leave every later registry read in this process aimed at the other dir."""
    e = _basis(seed=12)
    _seed_unknown("U004", [e[0]], ["M1"])
    _seed_unknown("U009", [_mix(e, 0.97, np.sqrt(1 - 0.97 ** 2))], ["M2"])
    home = config.VOICEPRINTS_DIR
    other = tmp_path / "other_voiceprints"
    shutil.copytree(home, other)

    def boom(*a, **kw):
        raise RuntimeError("merge failed")
    monkeypatch.setattr(unknowns, "merge", boom)
    with pytest.raises(RuntimeError, match="merge failed"):
        registry_doctor.fix(registry_doctor.check(other), other)
    assert config.VOICEPRINTS_DIR == home


def test_fix_refuses_to_repoint_while_this_thread_holds_the_registry_lock(
        sandbox, tmp_path):
    """lock_registry() is re-entrant by depth, so a nested acquire under a
    repoint would skip the other dir's lock file. Refuse instead."""
    other = tmp_path / "other_voiceprints"
    other.mkdir()
    with identify.lock_registry():
        with pytest.raises(RuntimeError, match="holds the registry lock"):
            registry_doctor.fix([], other)
    assert config.VOICEPRINTS_DIR == sandbox / "voiceprints"


# ---------- a lost registry, and shapes nobody wrote ----------

def _unit(seed, dim=256):
    v = np.random.default_rng(seed).normal(size=dim)
    return v / np.linalg.norm(v)


def test_a_corrupt_registry_and_its_orphaned_files_are_reported(sandbox):
    """A truncated registry.json read as empty, and after one new enrollment
    the report said "clean" while every earlier voiceprint sat on disk with
    nothing pointing at it."""
    vp = config.VOICEPRINTS_DIR
    for i, n in enumerate(("Alex Rivera", "Sam Chen")):
        identify.enroll(n, _unit(i), source="M")
    reg = vp / "registry.json"
    reg.write_text(reg.read_text()[:25])        # truncated mid-write
    fs = registry_doctor.check()
    assert [(f.kind, f.subject) for f in _of(fs, "corrupt")] == [
        ("corrupt", "registry.json")]
    assert sorted(f.subject for f in _of(fs, "unreferenced_file")) == [
        "Alex Rivera.npy", "Sam Chen.npy"]
    assert not any(f.fixable for f in fs if f.kind in ("corrupt", "unreferenced_file"))
    # the registry reads as empty, so the next enrollment starts a new one;
    # the orphans are still reported, never "clean"
    identify.enroll("Jo Lee", _unit(7), source="M")
    fs = registry_doctor.check()
    assert _of(fs, "corrupt") == []
    assert sorted(f.subject for f in _of(fs, "unreferenced_file")) == [
        "Alex Rivera.npy", "Sam Chen.npy"]


@pytest.mark.parametrize("text", ['[1, 2]', '"x"', '{"Alex Rivera": "x"}',
                                  '{"Alex Rivera": {"file": 3}}',
                                  '{"Alex Rivera": {"file": "a.npy", "sources": "M1"}}'])
def test_a_registry_of_the_wrong_shape_is_a_finding_not_a_crash(sandbox, text):
    """'{"Alex Rivera": "x"}' raised AttributeError at meta.get."""
    vp = config.VOICEPRINTS_DIR
    (vp / "registry.json").write_text(text)
    np.save(vp / "a.npy", _unit(1).reshape(1, -1))
    fs = registry_doctor.check()
    assert {f.kind for f in fs} & {"corrupt", "invalid"}, fs


@pytest.mark.parametrize("text", ['{bad', '[1, 2]', '{"speakers": []}',
                                  '{"speakers": {"U001": "x"}}',
                                  '{"speakers": {"U001": {"file": "U001.npy", '
                                  '"meetings": "M1"}}}'])
def test_unknowns_of_the_wrong_shape_are_a_finding_not_a_crash(sandbox, text):
    (config.VOICEPRINTS_DIR / "unknowns.json").write_text(text)
    fs = registry_doctor.check()
    assert {f.kind for f in fs} & {"corrupt", "invalid"}, fs


def test_an_unreadable_voiceprint_file_is_invalid(sandbox):
    identify.enroll("Alex Rivera", _unit(1), source="M")
    (config.VOICEPRINTS_DIR / "Alex Rivera.npy").write_bytes(b"not numpy")
    fs = registry_doctor.check()
    assert [(f.kind, f.subject) for f in fs] == [("invalid", "Alex Rivera")]


def test_the_cli_names_a_corrupt_registry(sandbox, tmp_path):
    home = tmp_path / "home"
    (home / "voiceprints").mkdir(parents=True)
    (home / "meetings").mkdir()
    (home / "voiceprints" / "registry.json").write_text("{trunc")
    r = _run(home)          # its own STT_HOME: never the real registry
    assert r.returncode == 1, r.stderr
    assert "corrupt     registry.json: cannot be read" in r.stdout
    assert "Traceback" not in r.stderr
