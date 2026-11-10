"""The stable meeting id: the backfill, the id index, locks keyed on the id,
and registry references that survive a rename the name-chase missed.

A basename is a meeting's NAME. Six stores keyed on it, so every rename had to
chase references through all of them, best-effort, inside one try that swallowed
failures. These tests are about the identity that does not move.
"""
import fcntl
import json
import re
import threading

import numpy as np

from stt import config, identify, review, summarize, unknowns
from conftest import mfile


def _meeting(base, **extra):
    """A minimal meeting on disk, carrying the save clock a real one carries."""
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0, "date": "2026-05-01",
         "generated_at": "2026-05-01T09:00:00", "processed_at": "2026-05-01T08:00:00",
         "speakers": [], "segments": [], "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d, indent=2))
    mfile(base, ".txt").write_text("stub")
    return config.meeting_file(base, ".json")


def _locks():
    return config.meetings_dir() / ".locks"


# ---------- the backfill ----------

def test_backfill_stamps_once_and_never_moves_generated_at(sandbox):
    """Every meeting that predates the id gets one, and NOTHING else in the
    file changes value — generated_at above all. The panel reloads the open
    meeting when that stamp moves, so a migration that re-stamped it would
    yank the page out from under whoever was reading it."""
    j = _meeting("Alpha Mtg 05012026")
    before = json.loads(j.read_text())

    assert config.backfill_meeting_ids() == 1
    after = json.loads(j.read_text())
    mid = after["meeting_id"]
    assert re.fullmatch(r"[0-9a-f]{16}", mid), "16 hex chars of a uuid4"
    assert {k: v for k, v in after.items() if k != "meeting_id"} == before, \
        "the id is the only field added, and no other value was rewritten"
    assert after["generated_at"] == "2026-05-01T09:00:00"

    # idempotent: a second pass finds nothing to do and leaves the id alone
    assert config.backfill_meeting_ids() == 0
    assert json.loads(j.read_text())["meeting_id"] == mid
    assert config.id_of("Alpha Mtg 05012026") == mid
    assert config.load_meetings_index()["ids"] == {mid: "Alpha Mtg 05012026"}


def test_backfill_stamps_the_archive_without_indexing_it(sandbox):
    """A restore has to bring the id back with the meeting, so the archive is
    stamped too — but an archived meeting is not a live target, so it never
    enters the index."""
    _meeting("Live Mtg 05012026")
    d = config.archive_dir() / "Old Mtg 01012026"
    d.mkdir(parents=True)
    (d / "Old Mtg 01012026.json").write_text(json.dumps(
        {"source_file": "Old Mtg 01012026.m4a", "date": "2026-01-01",
         "speakers": [], "segments": [], "words": []}))

    assert config.backfill_meeting_ids() == 1                      # the live one
    assert config.backfill_meeting_ids(config.archive_dir()) == 1   # the archived one
    arc_id = config.id_of("Old Mtg 01012026", config.archive_dir())
    assert arc_id and arc_id not in config.load_meetings_index()["ids"]
    assert config.base_of(arc_id) is None, "archived is not resolvable as live"


def test_the_archive_backfill_leaves_no_base_named_lock(sandbox):
    """The panel stamps the archive at start. Each archived meeting was
    locked under its base (no id yet), and only the LIVE pass swept the
    locks, so a fresh demo home showed 'orphan_lock <archived base>.lock'
    in the registry doctor right after the panel started."""
    from stt import registry_doctor
    _meeting("Live Mtg 05012026")
    d = config.archive_dir() / "Old Mtg 01012026"
    d.mkdir(parents=True)
    (d / "Old Mtg 01012026.json").write_text(json.dumps(
        {"source_file": "Old Mtg 01012026.m4a", "date": "2026-01-01",
         "speakers": [], "segments": [], "words": []}))

    config.backfill_meeting_ids()
    config.backfill_meeting_ids(config.archive_dir())
    arc_id = config.id_of("Old Mtg 01012026", config.archive_dir())
    assert arc_id
    assert not (_locks() / "Old Mtg 01012026.lock").exists()
    assert config.orphan_lock_files() == []
    assert not [f for f in registry_doctor.check() if f.kind == "orphan_lock"]
    # the archived meeting still locks, now under its id
    with review.lock_meeting("Old Mtg 01012026"):
        assert (_locks() / f"{arc_id}.lock").exists()
    assert config.orphan_lock_files() == []


def test_the_index_rebuilds_from_a_scan_when_it_is_missing(sandbox):
    """The index is a cache of what the meeting jsons say. Deleting it (or
    never having had one) costs one scan, not a broken library."""
    _meeting("Alpha Mtg 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Alpha Mtg 05012026")

    config.MEETINGS_INDEX_PATH.unlink()
    assert config.base_of(mid) == "Alpha Mtg 05012026"
    assert config.MEETINGS_INDEX_PATH.exists(), "the lookup healed the file"

    # a mapping that names a meeting which no longer carries that id is stale,
    # and is likewise re-derived rather than believed
    config.MEETINGS_INDEX_PATH.write_text(json.dumps({"ids": {mid: "Some Other Mtg"}}))
    assert config.base_of(mid) == "Alpha Mtg 05012026"


def _count_rebuilds(monkeypatch):
    calls = []
    real = config.rebuild_meetings_index

    def counting(dest_dir=None):
        calls.append(dest_dir)
        return real(dest_dir)
    monkeypatch.setattr(config, "rebuild_meetings_index", counting)
    return calls


def test_a_retained_archived_reference_does_not_rescan_on_every_poll(
        sandbox, monkeypatch):
    """resolved_meetings / resolved_sources ask base_of about every reference
    whose stored name is not live, on every 2 s panel poll. An archived
    meeting's id is a MISS by design, and each miss used to rescan the whole
    library and rewrite meetings_index.json. Repeated misses against an
    unchanged index and library must cost neither."""
    from stt import archive
    _meeting("Team Sync 05012026")
    _meeting("Other Mtg 05022026")
    config.backfill_meeting_ids()
    mid = config.id_of("Team Sync 05012026")
    assert archive.archive_meeting("Team Sync 05012026")["ok"]
    rebuilds = _count_rebuilds(monkeypatch)

    assert config.base_of(mid) is None
    first = len(rebuilds)
    assert first <= 1
    stamp = config.MEETINGS_INDEX_PATH.stat().st_mtime_ns
    for _ in range(20):
        assert config.base_of(mid) is None
    voice = {"meetings": ["Team Sync 05012026"], "meeting_ids": [mid]}
    for _ in range(5):                       # the real poll path
        assert unknowns.resolved_meetings(voice) == ["Team Sync 05012026"]
    assert len(rebuilds) == first, "a repeated miss rescanned the library"
    assert config.MEETINGS_INDEX_PATH.stat().st_mtime_ns == stamp, \
        "a repeated miss rewrote the index"

    # the heal still works: an out-of-band move back into the library (no
    # index_set) moves the meetings dir, so the next miss rebuilds ONCE
    import shutil
    shutil.move(str(config.archive_dir() / "Team Sync 05012026"),
                str(config.meetings_dir() / "Team Sync 05012026"))
    assert config.base_of(mid) == "Team Sync 05012026"
    assert len(rebuilds) == first + 1
    assert config.base_of(mid) == "Team Sync 05012026"
    assert len(rebuilds) == first + 1, "a hit never rebuilds"


def test_a_missing_index_rebuilds_once_then_misses_are_free(sandbox, monkeypatch):
    _meeting("Alpha Mtg 05012026")
    config.backfill_meeting_ids()
    config.MEETINGS_INDEX_PATH.unlink()
    rebuilds = _count_rebuilds(monkeypatch)
    ghost = "f" * 16
    for _ in range(10):
        assert config.base_of(ghost) is None
    assert len(rebuilds) == 1, "the missing file costs one scan, not one per ask"
    assert config.base_of(config.id_of("Alpha Mtg 05012026")) == "Alpha Mtg 05012026"
    assert len(rebuilds) == 1


def test_a_miss_is_memoized_when_the_mtimes_cannot_order(sandbox, monkeypatch):
    """A meetings dir stamped in the future (a skewed clock, a synced volume)
    makes the index look older than the library forever. The memo keyed on
    the index and dir mtimes still stops the second scan."""
    import os
    import time
    _meeting("Alpha Mtg 05012026")
    config.backfill_meeting_ids()
    future = time.time() + 3600
    os.utime(config.meetings_dir(), (future, future))
    rebuilds = _count_rebuilds(monkeypatch)
    ghost = "e" * 16
    for _ in range(10):
        assert config.base_of(ghost) is None
    assert len(rebuilds) == 1


# ---------- the index follows every move ----------

def test_base_of_follows_a_rename(sandbox):
    """Rename is a folder move plus one index update. The id is what the
    queue, the locks and the registries hold, so it has to keep resolving."""
    _meeting("Team Sync 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Team Sync 05012026")

    r = summarize.rename_meeting("Team Sync 05012026", "Focus Group")
    assert r["ok"] and r["base"] == "2026-05-01 Focus Group"
    assert config.base_of(mid) == "2026-05-01 Focus Group"
    assert config.id_of("2026-05-01 Focus Group") == mid, "the id itself never moves"


def test_archive_drops_the_id_and_restore_brings_it_back(sandbox):
    """The index maps LIVE meetings. An archived one resolves to nothing until
    it comes back — under whatever name it comes back with."""
    from stt import archive
    _meeting("Team Sync 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Team Sync 05012026")

    assert archive.archive_meeting("Team Sync 05012026")["ok"]
    assert config.base_of(mid) is None
    assert config.load_meetings_index()["ids"] == {}

    assert archive.restore_meeting("Team Sync 05012026")["ok"]
    assert config.base_of(mid) == "Team Sync 05012026"


def test_delete_forgets_the_id(sandbox):
    """A deleted meeting's id must not keep resolving to a folder that is
    gone — purge_meeting_dir is the one removal path, so it does the forgetting."""
    from stt import archive
    _meeting("Team Sync 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Team Sync 05012026")

    assert archive.delete_meeting("Team Sync 05012026")["ok"]
    assert config.load_meetings_index()["ids"] == {}
    assert config.base_of(mid) is None


# ---------- registry references ----------

def test_registry_refs_resolve_after_two_renames_with_the_chase_broken(
        sandbox, monkeypatch):
    """The name-chase is best-effort and always was: it runs inside one try
    that swallows failures. With it broken, the stored NAME goes stale — and
    the ▶ clip used to die with it. The aligned ids carry the reference
    through, across two renames, with no chase at all."""
    _meeting("Team Sync 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Team Sync 05012026")

    rng = np.random.default_rng(7)
    identify.enroll("Dana", rng.normal(size=32), source="Team Sync 05012026")
    unknowns.save({"speakers": {"U001": {
        "file": "U001.npy", "meetings": ["Team Sync 05012026"],
        "meeting_ids": [mid]}}})
    assert identify.load_registry()["Dana"]["source_ids"] == [mid]

    def boom(*a, **kw):
        raise RuntimeError("the chase failed")
    monkeypatch.setattr(identify, "rename_source_refs", boom)
    monkeypatch.setattr(unknowns, "rename_meeting_refs", boom)

    assert summarize.rename_meeting("Team Sync 05012026", "Focus Group")["ok"]
    r = summarize.set_meeting_date("2026-05-01 Focus Group", "2026-06-15")
    assert r["ok"] and r["base"] == "2026-06-15 Focus Group"

    person = identify.load_registry()["Dana"]
    assert person["sources"] == ["Team Sync 05012026"], "the stored name IS stale"
    assert identify.resolved_sources(person) == ["2026-06-15 Focus Group"]
    voice = unknowns.load()["speakers"]["U001"]
    assert unknowns.resolved_meetings(voice) == ["2026-06-15 Focus Group"]


def test_a_legacy_registry_entry_with_no_ids_still_reads(sandbox):
    """Every registry on disk predates this. An entry with no id list reads
    exactly as it did: base-only, no resolution, no crash."""
    _meeting("Team Sync 05012026")
    legacy = {"file": "Dana.npy", "dim": 32, "n_samples": 2,
              "sources": ["Team Sync 05012026", "Gone Mtg 01012026"]}
    assert identify.aligned_source_ids(legacy) == [None, None]
    assert identify.resolved_sources(legacy) == ["Team Sync 05012026",
                                                 "Gone Mtg 01012026"]
    assert unknowns.aligned_meeting_ids({"meetings": ["A", "B"]}) == [None, None]


def test_naming_an_unknown_carries_each_samples_meeting_id_into_the_profile(
        sandbox, monkeypatch):
    """promote() enrolled each sample with its base alone, so enroll()
    re-derived the id from a name the rename chase may have missed. A stale
    base resolves to nothing, and the new profile lost the one reference
    that survives a rename. The unknown already holds the id: pass it."""
    _meeting("Team Sync 05012026")
    _meeting("Staff Mtg 05022026")
    config.backfill_meeting_ids()
    mid_a = config.id_of("Team Sync 05012026")
    mid_b = config.id_of("Staff Mtg 05022026")

    rng = np.random.default_rng(13)
    np.save(config.VOICEPRINTS_DIR / "U001.npy", rng.normal(size=(2, 32)))
    unknowns.save({"speakers": {"U001": {
        "file": "U001.npy", "meetings": ["Team Sync 05012026", "Staff Mtg 05022026"],
        "meeting_ids": [mid_a, mid_b]}}})

    def boom(*a, **kw):
        raise RuntimeError("the chase failed")
    monkeypatch.setattr(unknowns, "rename_meeting_refs", boom)
    assert summarize.rename_meeting("Team Sync 05012026", "Focus Group")["ok"]
    assert unknowns.load()["speakers"]["U001"]["meetings"][0] == \
        "Team Sync 05012026", "the stored name IS stale"

    assert unknowns.promote("U001", "Dana")
    person = identify.load_registry()["Dana"]
    assert person["sources"] == ["Team Sync 05012026", "Staff Mtg 05022026"]
    assert person["source_ids"] == [mid_a, mid_b], \
        "the profile re-derived the id from a stale base and lost it"
    assert identify.resolved_sources(person) == ["2026-05-01 Focus Group",
                                                 "Staff Mtg 05022026"]


def test_naming_a_legacy_unknown_with_no_ids_still_derives_them(sandbox):
    """An unknown written before meeting ids existed has no id list. It
    passes None per sample, and enroll() derives the id from the base
    exactly as it did before."""
    _meeting("Team Sync 05012026")
    config.backfill_meeting_ids()
    rng = np.random.default_rng(17)
    np.save(config.VOICEPRINTS_DIR / "U001.npy", rng.normal(size=(1, 32)))
    unknowns.save({"speakers": {"U001": {
        "file": "U001.npy", "meetings": ["Team Sync 05012026"]}}})

    assert unknowns.promote("U001", "Dana")
    assert identify.load_registry()["Dana"]["source_ids"] == \
        [config.id_of("Team Sync 05012026")]


def test_removing_a_sample_keeps_the_two_lists_aligned(sandbox):
    """sources and source_ids are parallel lists indexed by sample. Any edit
    that drops a sample has to drop from both, or every later reference
    resolves to the wrong meeting."""
    for b in ("One Mtg 05012026", "Two Mtg 05022026", "Three Mtg 05032026"):
        _meeting(b)
    config.backfill_meeting_ids()
    rng = np.random.default_rng(11)
    for b in ("One Mtg 05012026", "Two Mtg 05022026", "Three Mtg 05032026"):
        identify.enroll("Dana", rng.normal(size=32), source=b)

    meta = identify.load_registry()["Dana"]
    assert meta["source_ids"] == [config.id_of(b) for b in meta["sources"]]

    assert identify.remove_sample("Dana", 1)
    meta = identify.load_registry()["Dana"]
    assert meta["sources"] == ["One Mtg 05012026", "Three Mtg 05032026"]
    assert meta["source_ids"] == [config.id_of(b) for b in meta["sources"]]


# ---------- locks ----------

def test_a_held_lock_survives_a_rename_and_a_second_holder_waits(sandbox):
    """The lock file is named for the id, so the key cannot change mid-hold.
    A rename runs UNDER this lock; a second caller who knows only the new name
    still queues on the same file instead of walking straight in."""
    _meeting("Team Sync 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Team Sync 05012026")

    renamed, release, got = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with review.lock_meeting("Team Sync 05012026"):
            # exactly what summarize's rename path does inside this lock
            summarize._move_folder("Team Sync 05012026", "2026-06-15 Focus Group")
            renamed.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert renamed.wait(10), "the rename never happened"
    assert config.base_of(mid) == "2026-06-15 Focus Group"
    assert [p.name for p in _locks().iterdir()] == [f"{mid}.lock"], \
        "one lock file, named for the id — the rename left no orphan"

    def second():
        with review.lock_meeting("2026-06-15 Focus Group"):
            got.set()

    t2 = threading.Thread(target=second, daemon=True)
    t2.start()
    assert not got.wait(0.5), "the new name walked into a lock that is HELD"
    release.set()
    assert got.wait(10), "the second holder never got in after the release"
    t.join(5)
    t2.join(5)
    assert [p.name for p in _locks().iterdir()] == [f"{mid}.lock"]


def test_a_meeting_with_no_id_still_locks_on_its_base(sandbox):
    """A meeting the backfill has not reached (or one outside the store) has
    no id, and locks exactly as it did before."""
    _meeting("Legacy Mtg 05012026")           # deliberately NOT backfilled
    with review.lock_meeting("Legacy Mtg 05012026"):
        pass
    assert (_locks() / "Legacy Mtg 05012026.lock").exists()


def test_prune_lock_files_removes_orphans_and_keeps_what_is_live(sandbox):
    """The live library carried 262 lock files against 101 meetings: every
    rename left one behind and nothing ever removed them."""
    _meeting("Alpha Mtg 05012026")
    config.backfill_meeting_ids()
    mid = config.id_of("Alpha Mtg 05012026")
    _meeting("Legacy Mtg 05012026")            # arrived AFTER the backfill: no id

    with review.lock_meeting("Alpha Mtg 05012026"):
        pass                                   # -> <id>.lock
    with review.lock_meeting("Legacy Mtg 05012026"):
        pass                                   # -> <base>.lock, still the live key
    (_locks() / "Renamed Away 04012026.lock").write_text("")   # orphan base
    (_locks() / ("f" * 16 + ".lock")).write_text("")           # orphan id

    assert config.prune_lock_files() == 2
    assert {p.name for p in _locks().iterdir()} == {
        f"{mid}.lock", "Legacy Mtg 05012026.lock"}

    # and once the legacy meeting HAS an id, its base-named lock is dead too:
    # lock_key can never ask for that name again, so the backfill's own prune
    # takes it. The next caller opens the id-named one.
    config.backfill_meeting_ids()
    assert {p.name for p in _locks().iterdir()} == {f"{mid}.lock"}
    with review.lock_meeting("Legacy Mtg 05012026"):
        pass
    assert (_locks() / f"{config.id_of('Legacy Mtg 05012026')}.lock").exists()


def test_prune_never_removes_a_lock_somebody_is_holding(sandbox):
    """Unlinking a held lock file is worse than leaking one: the holder keeps
    a lock on an unlinked inode, the next caller creates a fresh file, and both
    think they have the meeting to themselves."""
    _meeting("Alpha Mtg 05012026")
    config.backfill_meeting_ids()
    _locks().mkdir(parents=True, exist_ok=True)
    held = _locks() / "Renamed Away 04012026.lock"     # an orphan by its stem
    held.write_text("")
    holding, release = threading.Event(), threading.Event()

    def holder():
        with open(held, "r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            holding.set()
            release.wait(10)
            fcntl.flock(fh, fcntl.LOCK_UN)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert holding.wait(10)
    assert config.prune_lock_files() == 0
    assert held.exists()
    release.set()
    t.join(5)
    assert config.prune_lock_files() == 1        # released: now it goes


def test_prune_keeps_an_archived_meetings_lock(sandbox):
    """restore_meeting and a delete from the archive lock the ARCHIVED base,
    and lock_key answers with the archived id. That stem can be requested
    again, so it is not an orphan: unlinking it reopens the two-holder race
    for the next restore."""
    from stt import archive
    _meeting("Team Sync 05012026")
    config.backfill_meeting_ids()
    _meeting("Legacy Old 01012026")          # arrives after the backfill: no id
    mid = config.id_of("Team Sync 05012026")
    assert archive.archive_meeting("Team Sync 05012026")["ok"]
    assert archive.archive_meeting("Legacy Old 01012026")["ok"]
    assert config.id_of("Legacy Old 01012026", config.archive_dir()) is None
    _locks().mkdir(parents=True, exist_ok=True)
    for stem in (mid, "Legacy Old 01012026", "Gone Away 03012026"):
        (_locks() / f"{stem}.lock").write_text("")

    assert config.prune_lock_files() == 1
    names = {p.name for p in _locks().iterdir()}
    assert f"{mid}.lock" in names, "an archived meeting's id lock was pruned"
    assert "Legacy Old 01012026.lock" in names, \
        "an archived legacy meeting's base lock was pruned"
    assert "Gone Away 03012026.lock" not in names
    # a restore asks for exactly the stem that was kept
    assert config.lock_key("Team Sync 05012026") == mid


def test_prune_rechecks_a_stem_under_the_flock_before_unlinking(
        sandbox, monkeypatch):
    """The first scan and the unlink are not atomic. A stem that is dead at
    the scan can be requested again by the time the flock is taken — here a
    legacy meeting arrives under that very name. The stem is re-checked with
    the flock held, and a stem that came back to life is never unlinked."""
    _meeting("Alpha Mtg 05012026")
    config.backfill_meeting_ids()
    _locks().mkdir(parents=True, exist_ok=True)
    arriving = _locks() / "Arriving Mtg 05012026.lock"
    arriving.write_text("")
    real_flock = fcntl.flock

    def racing_flock(fh, op):
        real_flock(fh, op)
        if (op & fcntl.LOCK_NB and str(getattr(fh, "name", "")) == str(arriving)
                and not config.meeting_file("Arriving Mtg 05012026", ".json").exists()):
            _meeting("Arriving Mtg 05012026")   # no id: it locks on its base
    monkeypatch.setattr(fcntl, "flock", racing_flock)

    assert config.prune_lock_files() == 0
    assert arriving.exists(), "a stem that is requestable again was unlinked"
    assert config.lock_key("Arriving Mtg 05012026") == "Arriving Mtg 05012026"
    assert config.orphan_lock_files() == []
