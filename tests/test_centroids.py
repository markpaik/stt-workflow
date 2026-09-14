"""stt/centroids.py: the library-wide cluster index and the targeting question
it exists to answer — "which meetings can this identity change touch?"

Every test runs on the sandbox library, never the real one.
"""
import json

import numpy as np

from stt import centroids, config, diarcache, unknowns
from conftest import mfile


def _meeting(base, cents, speakers=None):
    """One meeting on disk: a transcript json with a roster, and a .diar.npz
    holding the cluster centroids the index reads."""
    (mfile(base, ".json")).write_text(json.dumps({
        "source_file": f"{base}.m4a", "duration_sec": 9.0, "strict": False,
        "speakers": speakers or [{"id": k, "name": None, "display": f"Speaker {i+1}"}
                                 for i, k in enumerate(cents)],
        "segments": [], "words": []}))
    (mfile(base, ".txt")).write_text("stub")
    raw_turns = [{"start": 0.0, "end": 5.0, "cluster": k} for k in cents]
    diarcache.save(mfile(base, ".diar.npz"), raw_turns,
                   [None] * len(raw_turns), cents)


def _unit(seed, dim=256):
    v = np.random.default_rng(seed).normal(size=dim)
    return v / np.linalg.norm(v)


def _library():
    """Three meetings. Alpha holds the voice we will search for; Bravo's roster
    already names Dana; Charlie is unrelated to both."""
    target = _unit(1)
    _meeting("Alpha Mtg", {"SPEAKER_00": target, "SPEAKER_01": _unit(2)})
    _meeting("Bravo Mtg", {"SPEAKER_00": _unit(3)},
             speakers=[{"id": "SPEAKER_00", "name": "Dana Cole", "display": "Dana Cole"}])
    _meeting("Charlie Mtg", {"SPEAKER_00": _unit(4)})
    return target


def test_index_covers_every_cached_meeting(sandbox):
    _library()
    idx = centroids.index()
    assert set(idx) == {"Alpha Mtg", "Bravo Mtg", "Charlie Mtg"}
    assert set(idx["Alpha Mtg"]) == {"SPEAKER_00", "SPEAKER_01"}
    assert centroids.index_path().exists() and centroids.meta_path().exists()


def test_index_skips_a_meeting_with_no_diarization_cache(sandbox):
    """relabel can never update a meeting with no .diar.npz, so targeting it
    would only produce a "no cache" skip line."""
    _library()
    (mfile("No Cache Mtg", ".json")).write_text(json.dumps(
        {"source_file": "x.m4a", "duration_sec": 1.0, "speakers": [],
         "segments": [], "words": []}))
    assert "No Cache Mtg" not in centroids.index()


def test_index_rereads_only_what_changed_and_drops_what_is_gone(sandbox):
    """The cache is keyed on each .diar.npz's mtime. An unchanged meeting must
    not be re-read (that re-read, 99 times, is the cost this index removes),
    and a deleted meeting must not linger in the answer."""
    _library()
    centroids.index()

    reads = []
    real_load = diarcache.load

    def counting_load(path):
        reads.append(str(path))
        return real_load(path)

    import stt.centroids as mod
    mod.diarcache.load = counting_load
    try:
        centroids.index()
        assert reads == [], "an unchanged library must not re-read any cache"

        # Charlie's diarization is redone: only Charlie is re-read
        new = {"SPEAKER_00": _unit(9), "SPEAKER_01": _unit(10)}
        _meeting("Charlie Mtg", new)
        import os
        p = config.meeting_file("Charlie Mtg", ".diar.npz")
        os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 10))
        idx = centroids.index()
        assert [r for r in reads if "Charlie" in r], "the changed meeting re-reads"
        assert not [r for r in reads if "Alpha" in r or "Bravo" in r]
        assert set(idx["Charlie Mtg"]) == {"SPEAKER_00", "SPEAKER_01"}
    finally:
        mod.diarcache.load = real_load

    # and a removed meeting leaves the index entirely
    import shutil
    shutil.rmtree(config.meeting_dir("Bravo Mtg"))
    assert set(centroids.index()) == {"Alpha Mtg", "Charlie Mtg"}


def test_targets_finds_the_meeting_holding_the_voice(sandbox):
    target = _library()
    assert centroids.targets(vector=target) == ["Alpha Mtg"]
    # a stranger's voice matches nobody
    assert centroids.targets(vector=_unit(99)) == []
    # a whole voiceprint STACK works the same way: the person's best sample wins
    stack = np.vstack([_unit(98), target])
    assert centroids.targets(vector=stack) == ["Alpha Mtg"]


def test_targets_reaches_just_below_the_naming_threshold(sandbox):
    """A change that moves a score a little must not miss a cluster that sat
    just UNDER the bar before it, so the floor is the threshold less a margin."""
    base_vec = _unit(1)
    _library()
    floor = config.NAMING_THRESHOLD - centroids.TARGET_MARGIN
    # a vector at ~0.55 cosine to Alpha's cluster: under the naming threshold
    # (0.60) but inside the margin
    other = _unit(77)
    other = other - base_vec * float(np.dot(other, base_vec))
    other /= np.linalg.norm(other)
    near = base_vec * 0.55 + other * float(np.sqrt(1 - 0.55 ** 2))
    score = float(np.dot(near, base_vec))
    assert floor <= score < config.NAMING_THRESHOLD, score
    assert centroids.targets(vector=near) == ["Alpha Mtg"]


def test_targets_finds_a_name_already_on_a_roster(sandbox):
    _library()
    assert centroids.targets(names=["Dana Cole"]) == ["Bravo Mtg"]
    assert centroids.targets(names=["Nobody At All"]) == []
    # blank entries are not a wildcard
    assert centroids.targets(names=["", "  "]) == []


def test_targets_finds_the_meetings_an_unknown_was_heard_in(sandbox):
    _library()
    v = _unit(4)
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                          "Charlie Mtg")["SPEAKER_00"]
    assert uid, "the fixture must actually mint an unknown"
    assert centroids.targets(uids=[uid]) == ["Charlie Mtg"]
    assert centroids.targets(uids=["U999"]) == []


def test_targets_unions_the_three_signals_and_sorts(sandbox):
    target = _library()
    v = _unit(4)
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None},
                          "Charlie Mtg")["SPEAKER_00"]
    assert centroids.targets(vector=target, names=["Dana Cole"], uids=[uid]) == \
        ["Alpha Mtg", "Bravo Mtg", "Charlie Mtg"]
    assert centroids.targets() == []


def test_a_corrupt_index_costs_a_re_read_not_an_answer(sandbox):
    target = _library()
    centroids.index()
    centroids.index_path().write_bytes(b"not an npz")
    assert centroids.targets(vector=target) == ["Alpha Mtg"]
    centroids.meta_path().write_text("{ not json")
    assert centroids.targets(vector=target) == ["Alpha Mtg"]


def test_the_cache_lives_under_the_project_dir(sandbox):
    """It is derived state, so it belongs where the sandbox can redirect it —
    never inside the user's meetings library."""
    _library()
    centroids.index()
    assert centroids.index_path().parent == config.PROJECT_DIR
    assert centroids.meta_path().parent == config.PROJECT_DIR
    assert not list(config.MEETINGS_DIR.glob("centroid_index.*"))
