"""Phase 4: what it takes for a cluster to claim an existing global number,
and what happens to the near miss that does not.

The audit of 2026-09-13 found U050 linking two clusters that score 0.48
against each other. Nothing linked them directly — sample drift did. Every
link appends a centroid to the stored stack, so a chain of barely-passing
0.62 matches walks the stack off the voice it started on until one global
number spans two people. Two rules stop that: a returning unknown needs 0.70,
not 0.60, and the stack has to still agree with itself AFTER the append.

The third rule is about numbers, not voices. A freed number is never reused,
so "Speaker 3" in a year-old transcript can never come back meaning somebody
else.
"""
import numpy as np
import pytest

from stt import config, registry_doctor, unknowns

GOOD_STATS = {"talk_secs": 120.0, "reliable_turns": 40}


def _basis(dim=256, seed=0, n=4):
    """n orthonormal vectors, so a cosine written below is the cosine you get."""
    rng = np.random.default_rng(seed)
    q, _ = np.linalg.qr(rng.normal(size=(dim, n)))
    return [np.asarray(q[:, i]) for i in range(n)]


def _mix(basis, *coeffs):
    v = sum(c * b for c, b in zip(basis, coeffs))
    return v / np.linalg.norm(v)


def _cos(a, b):
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def _seed_unknown(uid, rows, meetings, nxt=None, links=None):
    """Put one unknown on disk with an exact sample stack."""
    arr = np.vstack([np.asarray(r, float) for r in rows])
    np.save(config.VOICEPRINTS_DIR / f"{uid}.npy", arr)
    meta = {"file": f"{uid}.npy", "meetings": list(meetings),
            "meeting_ids": [None] * len(meetings)}
    if links is not None:
        meta["links"] = links
    reg = unknowns.load()
    reg["speakers"][uid] = meta
    if nxt is not None:
        reg["next"] = nxt
    unknowns.save(reg)


# ---------- monotonic ids ----------

def test_a_freed_global_number_is_never_handed_out_again(sandbox):
    """Mint, merge the minted number away, mint again: the new number is
    HIGHER. Under the old lowest-free-slot scan the merged number came back
    on the very next stranger, which is how a stale reference silently
    changed who it meant."""
    e = _basis(seed=1)
    u1 = unknowns.assign({"S0": e[0]}, {"S0": None}, "M1",
                         stats={"S0": GOOD_STATS})["S0"]
    u2 = unknowns.assign({"S1": e[1]}, {"S1": None}, "M1",
                         stats={"S1": GOOD_STATS})["S1"]
    assert (u1, u2) == ("U001", "U002")

    assert unknowns.merge(u2, u1)                       # U002 is freed
    assert set(unknowns.load()["speakers"]) == {"U001"}

    u3 = unknowns.assign({"S2": e[2]}, {"S2": None}, "M2",
                         stats={"S2": GOOD_STATS})["S2"]
    assert u3 != u2, "the merged number came back on the next stranger"
    assert unknowns.uid_num(u3) > unknowns.uid_num(u2)
    assert u3 == "U003"
    assert unknowns.load()["next"] == 4


def test_load_heals_a_counter_that_fell_behind_its_own_ids(sandbox):
    """The live registry reads next=6 while its ids run to U058 — nothing has
    maintained the key since numbering moved to a free-slot scan. A counter
    below the high-water mark would hand out a number that is already taken."""
    e = _basis(seed=2)
    _seed_unknown("U058", [e[0]], ["Old"], nxt=6)
    assert unknowns.load()["next"] == 59

    minted = unknowns.assign({"S0": e[1]}, {"S0": None}, "New",
                             stats={"S0": GOOD_STATS})["S0"]
    assert minted == "U059"
    assert unknowns.load()["next"] == 60


# ---------- the link bar ----------

def test_a_link_that_would_split_the_stack_is_refused_and_suggested(sandbox):
    """THE U050 shape. U001 already holds two samples that agree with each
    other at 0.62. A third cluster scores 0.72 against one of them — past the
    0.70 link bar, and in the old code an automatic link — but only 0.48
    against the other. Appending it would leave one global number spanning two
    people. Refuse, mint, and keep the near miss as evidence for the panel."""
    e = _basis(seed=3)
    a = e[0]
    b = _mix(e, 0.62, np.sqrt(1 - 0.62 ** 2))
    y = (0.48 - 0.62 * 0.72) / np.sqrt(1 - 0.62 ** 2)
    c = _mix(e, 0.72, y, np.sqrt(1 - 0.72 ** 2 - y ** 2))
    assert _cos(c, a) == pytest.approx(0.72, abs=1e-6)
    assert _cos(c, b) == pytest.approx(0.48, abs=1e-6)
    _seed_unknown("U001", [a, b], ["M1", "M2"], nxt=2)
    before = np.load(config.VOICEPRINTS_DIR / "U001.npy").copy()

    out = unknowns.assign({"S0": c}, {"S0": None}, "M3", stats={"S0": GOOD_STATS})
    assert out["S0"] == "U002", "the 0.48 cross link was taken anyway"

    reg = unknowns.load()["speakers"]
    assert reg["U001"]["meetings"] == ["M1", "M2"]     # untouched
    assert np.allclose(np.load(config.VOICEPRINTS_DIR / "U001.npy"), before)
    assert reg["U002"]["suggested"] == [{"uid": "U001",
                                         "score": pytest.approx(0.72, abs=1e-6)}]


def test_the_refused_link_leaves_no_split_behind(sandbox):
    """The other half of the same refusal: the drift never happens, so the
    doctor has nothing to find. This is the invariant the bar exists for —
    after the refusal every stack still agrees with itself."""
    e = _basis(seed=4)
    a = e[0]
    b = _mix(e, 0.62, np.sqrt(1 - 0.62 ** 2))
    y = (0.48 - 0.62 * 0.72) / np.sqrt(1 - 0.62 ** 2)
    c = _mix(e, 0.72, y, np.sqrt(1 - 0.72 ** 2 - y ** 2))
    _seed_unknown("U001", [a, b], ["M1", "M2"], nxt=2)

    unknowns.assign({"S0": c}, {"S0": None}, "M3", stats={"S0": GOOD_STATS})
    assert [f for f in registry_doctor.check() if f.kind == "split"] == []


def test_a_strong_link_with_a_consistent_stack_records_its_evidence(sandbox):
    """0.72 against a stack that stays coherent after the append: link, and
    write down the meeting, its id, the cluster and the score that carried
    it. Without the evidence, a bad link is untraceable after the fact —
    which is exactly why U050 took an audit to find."""
    from conftest import mfile
    import json
    e = _basis(seed=5)
    a = e[0]
    c = _mix(e, 0.72, np.sqrt(1 - 0.72 ** 2))
    _seed_unknown("U001", [a], ["M1"], nxt=2)
    mfile("M2", ".json").write_text(json.dumps(
        {"meeting_id": "abc123", "source_file": "M2.m4a",
         "segments": [], "speakers": [], "words": []}))

    out = unknowns.assign({"SPEAKER_04": c}, {"SPEAKER_04": None}, "M2",
                          stats={"SPEAKER_04": GOOD_STATS})
    assert out["SPEAKER_04"] == "U001"

    meta = unknowns.load()["speakers"]["U001"]
    assert meta["meetings"] == ["M1", "M2"]
    assert np.load(config.VOICEPRINTS_DIR / "U001.npy").shape[0] == 2
    assert meta["links"] == [{"meeting": "M2", "meeting_id": "abc123",
                              "cluster": "SPEAKER_04",
                              "score": pytest.approx(0.72, abs=1e-6)}]


def test_a_relabel_of_the_same_meeting_leaves_one_link_entry(sandbox):
    """Relabel runs after every identity change, and a link record that
    stacked a copy per pass would be as useless as the duplicate samples the
    same rule already prevents."""
    e = _basis(seed=6)
    a = e[0]
    c = _mix(e, 0.72, np.sqrt(1 - 0.72 ** 2))
    _seed_unknown("U001", [a], ["M1"], nxt=2)

    for _ in range(3):
        assert unknowns.assign({"S0": c}, {"S0": None}, "M2",
                               stats={"S0": GOOD_STATS})["S0"] == "U001"
    meta = unknowns.load()["speakers"]["U001"]
    assert len(meta["links"]) == 1
    assert meta["links"][0]["meeting"] == "M2"
    assert np.load(config.VOICEPRINTS_DIR / "U001.npy").shape[0] == 2


def test_the_old_060_band_no_longer_links(sandbox):
    """The band between MATCH_MIN and LINK_MIN is where the drift lived. It
    now mints its own number and carries the candidate as a suggestion."""
    e = _basis(seed=7)
    a = e[0]
    c = _mix(e, 0.65, np.sqrt(1 - 0.65 ** 2))
    _seed_unknown("U001", [a], ["M1"], nxt=2)

    out = unknowns.assign({"S0": c}, {"S0": None}, "M2", stats={"S0": GOOD_STATS})
    assert out["S0"] == "U002"
    sugg = unknowns.load()["speakers"]["U002"]["suggested"]
    assert sugg == [{"uid": "U001", "score": pytest.approx(0.65, abs=1e-6)}]


# ---------- a stack that drifted before the floor ----------

def _drifted_u050():
    """A legacy U050 on disk: two samples at 0.48, below LINK_STACK_MIN,
    from before the floor existed. M2's cluster IS the second sample."""
    e = _basis(seed=8)
    a = e[0]
    b = _mix(e, 0.48, np.sqrt(1 - 0.48 ** 2))
    links = [{"meeting": "M1", "meeting_id": None, "cluster": "SPEAKER_00",
              "score": 1.0},
             {"meeting": "M2", "meeting_id": None, "cluster": "SPEAKER_01",
              "score": 0.72}]
    _seed_unknown("U050", [a, b], ["M1", "M2"], nxt=51, links=links)
    assert unknowns.min_pairwise(np.vstack([a, b])) < unknowns.LINK_STACK_MIN
    return a, b


def test_a_relabel_keeps_its_link_on_a_stack_that_drifted_before_the_floor(sandbox):
    """The relabel of a meeting the unknown already lists appends nothing,
    so the stack does not move and the floor has nothing to measure. The
    gate used to measure the unchanged stack anyway: a legacy drifted U050
    lost M2 to a fresh U051 on the next relabel while U050 still listed M2
    in its meetings and its links — one cluster, two numbers."""
    a, b = _drifted_u050()
    before = np.load(config.VOICEPRINTS_DIR / "U050.npy").copy()

    out = unknowns.assign({"SPEAKER_01": b}, {"SPEAKER_01": None}, "M2",
                          stats={"SPEAKER_01": GOOD_STATS})
    assert out == {"SPEAKER_01": "U050"}, "the relabel forked M2 onto a fresh number"

    reg = unknowns.load()
    assert set(reg["speakers"]) == {"U050"}
    assert reg["next"] == 51, "no number was minted"
    meta = reg["speakers"]["U050"]
    assert meta["meetings"] == ["M1", "M2"]
    assert np.allclose(np.load(config.VOICEPRINTS_DIR / "U050.npy"), before)
    assert [(ln["meeting"], ln["cluster"]) for ln in meta["links"]] == \
        [("M1", "SPEAKER_00"), ("M2", "SPEAKER_01")]
    assert meta["links"][1]["score"] == pytest.approx(1.0, abs=1e-9)
    # the drift is still there for the doctor to split
    assert [f.subject for f in registry_doctor.check()
            if f.kind == "split"] == ["U050"]


def test_a_new_meeting_still_cannot_link_to_a_drifted_stack(sandbox):
    """The other side of the same rule: a NEW meeting appends a sample, so
    the floor still applies, and a stack that already disagrees with itself
    takes no new link until the doctor splits it."""
    a, b = _drifted_u050()

    out = unknowns.assign({"S0": b}, {"S0": None}, "M3", stats={"S0": GOOD_STATS})
    assert out["S0"] == "U051"
    assert unknowns.load()["speakers"]["U050"]["meetings"] == ["M1", "M2"]
