"""Profile hygiene: the voice registries stay clean BY CONSTRUCTION.

assign() must never mint a second entry for a voice the registry already
knows (the margin rule separates distinct strangers; at split-fragment
similarity the two candidates ARE the same voice — pre-fix, identical junk
twins at cosine 1.000 minted unboundedly on every relabel), and must never
register a cluster too thin to ever be named (a noise floor with seconds of
"speech" is not a person). One shared invariant checker enforces the same
rules on synthetic fixtures and — when present — on the real on-disk
registry, so a hygiene regression on this machine fails CI before it piles
junk into the Speakers panel."""
import json

import numpy as np
import pytest

from stt import config, identify, unknowns


def _unit(seed, dim=256):
    v = np.random.default_rng(seed).normal(size=dim)
    return v / np.linalg.norm(v)


def _at_cosine(u, c, seed=99):
    """A unit vector whose cosine against unit vector `u` is exactly c."""
    rng = np.random.default_rng(seed)
    r = rng.normal(size=u.shape)
    r -= (r @ u) * u
    r /= np.linalg.norm(r)
    return c * u + np.sqrt(1.0 - c * c) * r


def _cos(a, b):
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


GOOD_STATS = {"talk_secs": 120.0, "reliable_turns": 40}
THIN_STATS = {"talk_secs": 6.0, "reliable_turns": 2}


# ---------- the shared invariant checker ----------

def hygiene_errors(vp_dir) -> list:
    """Every invariant the registries must satisfy, as human-readable
    violations. Empty list = clean. Checks BOTH files:

    registry.json — each entry's .npy exists; n_samples matches the rows;
    sources stay aligned; every sample is a finite unit vector; no two
    samples within one stack are duplicates (cosine > 0.999 adds nothing
    to max-cosine matching and squeezes out a diverse sample at the cap).

    unknowns.json — each entry's .npy exists with finite unit rows; no two
    NON-dropped unknowns are the same voice (max cross cosine > 0.999):
    that is one stranger holding two global numbers, the unbounded-mint bug.
    """
    errs = []
    reg_p = vp_dir / "registry.json"
    if reg_p.exists():
        for name, meta in json.loads(reg_p.read_text()).items():
            f = vp_dir / meta["file"]
            if not f.exists():
                errs.append(f"{name}: voiceprint file {meta['file']} missing")
                continue
            arr = np.load(f)
            arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
            if meta.get("n_samples") not in (None, arr.shape[0]):
                errs.append(f"{name}: n_samples={meta['n_samples']} but "
                            f"{arr.shape[0]} rows on disk")
            srcs = meta.get("sources")
            if srcs is not None and len(srcs) != arr.shape[0]:
                errs.append(f"{name}: {len(srcs)} sources for {arr.shape[0]} samples")
            for i, row in enumerate(arr):
                if not np.isfinite(row).all() or abs(np.linalg.norm(row) - 1.0) > 1e-3:
                    errs.append(f"{name}[{i}]: not a finite unit vector")
            for i in range(arr.shape[0]):
                for j in range(i + 1, arr.shape[0]):
                    if _cos(arr[i], arr[j]) > 0.999:
                        errs.append(f"{name}: samples {i} and {j} are duplicates")
    unk_p = vp_dir / "unknowns.json"
    if unk_p.exists():
        stacks = {}
        for uid, meta in json.loads(unk_p.read_text())["speakers"].items():
            f = vp_dir / meta["file"]
            if not f.exists():
                errs.append(f"{uid}: sample file {meta['file']} missing")
                continue
            arr = np.load(f)
            arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
            for i, row in enumerate(arr):
                if not np.isfinite(row).all() or abs(np.linalg.norm(row) - 1.0) > 1e-3:
                    errs.append(f"{uid}[{i}]: not a finite unit vector")
            if not meta.get("dropped"):
                stacks[uid] = arr
        uids = sorted(stacks)
        for i, a in enumerate(uids):
            for b in uids[i + 1:]:
                worst = max(_cos(x, y) for x in stacks[a] for y in stacks[b])
                if worst > 0.999:
                    errs.append(f"{a}/{b}: one voice under two global numbers "
                                f"(cross cosine {worst:.3f})")
    return errs


def test_hygiene_checker_passes_on_a_registry_built_by_the_app(sandbox):
    """Everything the app's own code paths produce satisfies the invariants."""
    identify.enroll("Alice", _unit(1), source="M1")
    identify.enroll("Alice", _at_cosine(_unit(1), 0.9, seed=2), source="M2")
    identify.enroll("Bob", _unit(3), source="M1")
    unknowns.assign({"S0": _unit(4)}, {"S0": None}, "M1", stats={"S0": GOOD_STATS})
    unknowns.assign({"S1": _unit(5)}, {"S1": None}, "M2", stats={"S1": GOOD_STATS})
    assert hygiene_errors(config.VOICEPRINTS_DIR) == []


def test_hygiene_checker_catches_each_planted_defect_class(sandbox):
    """The checker itself is load-bearing (it guards the REAL registry), so
    prove it sees every defect class it claims to."""
    v = _unit(1)
    identify.enroll("Alice", v, source="M1")
    # duplicate rows inside one stack (bypassing enroll's 0.999 dedup)
    f = config.VOICEPRINTS_DIR / identify.load_registry()["Alice"]["file"]
    np.save(f, np.vstack([v, v]))
    # two NON-dropped unknowns carrying the same voice
    w = _unit(2)
    unknowns.save({"speakers": {
        "U001": {"file": "U001.npy", "meetings": ["A"]},
        "U002": {"file": "U002.npy", "meetings": ["B"]},
        "U003": {"file": "U003.npy", "meetings": []},  # missing .npy
    }})
    np.save(config.VOICEPRINTS_DIR / "U001.npy", w.reshape(1, -1))
    np.save(config.VOICEPRINTS_DIR / "U002.npy", w.reshape(1, -1))
    errs = "\n".join(hygiene_errors(config.VOICEPRINTS_DIR))
    assert "n_samples=1 but 2 rows" in errs
    assert "samples 0 and 1 are duplicates" in errs
    assert "U001/U002: one voice under two global numbers" in errs
    assert "U003: sample file U003.npy missing" in errs


@pytest.mark.skipif(
    not (config.VOICEPRINTS_DIR / "registry.json").exists()
    or config.STT_HOME is not None,
    reason="no real voiceprint registry on this machine")
def test_real_registry_satisfies_the_hygiene_invariants():
    """The CI invariant on the SHIPPED registry: whatever state the app has
    accumulated on this machine must satisfy the same rules the fixtures do.
    (No sandbox fixture on purpose — this reads the real, live registry.)"""
    errs = hygiene_errors(config.VOICEPRINTS_DIR)
    assert errs == [], "real registry violates hygiene:\n" + "\n".join(errs)


# ---------- assign(): duplicate guard ----------

def test_assign_never_mints_a_twin_of_a_known_unknown(sandbox):
    """THE unbounded-mint regression: two identical junk entries already in
    the registry (cosine 1.000 — the margin rule can never separate them, so
    the old code minted a THIRD on every relabel, forever). At split-fragment
    similarity the candidates are the same voice: reuse the best entry."""
    v = _unit(7)
    unknowns.save({"speakers": {
        "U001": {"file": "U001.npy", "meetings": ["Mtg A"]},
        "U002": {"file": "U002.npy", "meetings": ["Mtg B"]},
    }})
    np.save(config.VOICEPRINTS_DIR / "U001.npy", v.reshape(1, -1))
    np.save(config.VOICEPRINTS_DIR / "U002.npy", v.reshape(1, -1))

    for relabel_pass in range(3):  # every pass used to mint another twin
        out = unknowns.assign({"S0": v}, {"S0": None}, "Mtg C",
                              stats={"S0": GOOD_STATS})
        assert out["S0"] in {"U001", "U002"}, \
            f"pass {relabel_pass}: minted {out['S0']} instead of matching a twin"
        assert set(unknowns.load()["speakers"]) == {"U001", "U002"}


def test_assign_duplicate_guard_still_respects_dropped_tombstones(sandbox):
    """A voice whose only registry entries are twins of a DROPPED tombstone is
    recognized and suppressed — the guard must strengthen the tombstone, not
    resurrect the voice under a new number."""
    v = _unit(8)
    uid = unknowns.assign({"S0": v}, {"S0": None}, "Mtg A",
                          stats={"S0": GOOD_STATS})["S0"]
    assert unknowns.drop(uid)
    near = _at_cosine(v, 0.9, seed=3)  # same voice, another day's centroid
    out = unknowns.assign({"S0": near}, {"S0": None}, "Mtg B",
                          stats={"S0": GOOD_STATS})
    assert "S0" not in out                       # suppressed, not re-minted
    assert set(unknowns.load()["speakers"]) == {uid}


def test_assign_margin_rule_still_separates_distinct_strangers(sandbox):
    """The guard fires only at split-fragment similarity: a NEW stranger who
    resembles two existing DISTINCT unknowns at 0.70 each (margin 0 — exactly
    the ambiguity the margin rule exists for) still mints a fresh number,
    because 0.70 < SPLIT_SIM."""
    rng = np.random.default_rng(9)
    u = rng.normal(size=256)
    u /= np.linalg.norm(u)
    r = rng.normal(size=256)      # second draw: independent of u
    r -= (r @ u) * u
    r /= np.linalg.norm(r)
    v1 = 0.7 * u + np.sqrt(1 - 0.49) * r
    v2 = 0.7 * u - np.sqrt(1 - 0.49) * r
    unknowns.save({"speakers": {
        "U001": {"file": "U001.npy", "meetings": ["A"]},
        "U002": {"file": "U002.npy", "meetings": ["B"]},
    }})
    np.save(config.VOICEPRINTS_DIR / "U001.npy", v1.reshape(1, -1))
    np.save(config.VOICEPRINTS_DIR / "U002.npy", v2.reshape(1, -1))
    out = unknowns.assign({"S2": u}, {"S2": None}, "M3", stats={"S2": GOOD_STATS})
    assert out["S2"] == "U003"


# ---------- assign(): minting quality floor ----------

def test_assign_floor_refuses_to_mint_a_thin_cluster(sandbox):
    """A NEW unknown needs real evidence — >= 30s of talk AND >= 10 reliable
    turns. A noise-floor cluster (seconds of hiss the diarizer split off)
    stays transcript-local instead of becoming a nameable 'Speaker N'."""
    out = unknowns.assign({"S0": _unit(10)}, {"S0": None}, "Mtg A",
                          stats={"S0": THIN_STATS})
    assert out == {}
    assert unknowns.load()["speakers"] == {}
    # plenty of talk but too few reliable turns: still no mint
    out = unknowns.assign({"S0": _unit(10)}, {"S0": None}, "Mtg A",
                          stats={"S0": {"talk_secs": 90.0, "reliable_turns": 4}})
    assert out == {}
    # both halves satisfied: mints normally
    out = unknowns.assign({"S0": _unit(10)}, {"S0": None}, "Mtg A",
                          stats={"S0": GOOD_STATS})
    assert out["S0"].startswith("U")


def test_assign_floor_never_blocks_matching_an_existing_unknown(sandbox):
    """Matching an EXISTING unknown stays unrestricted: a returning stranger
    heard only briefly today still keeps their stable global number."""
    v = _unit(11)
    uid = unknowns.assign({"S0": v}, {"S0": None}, "Mtg A",
                          stats={"S0": GOOD_STATS})["S0"]
    out = unknowns.assign({"S0": v}, {"S0": None}, "Mtg B",
                          stats={"S0": THIN_STATS})
    assert out["S0"] == uid
    assert "Mtg B" in unknowns.load()["speakers"][uid]["meetings"]


def test_assign_without_stats_stays_permissive(sandbox):
    """Callers without turn data (older paths, tests) mint as before — the
    floor engages only when the caller can actually measure the cluster."""
    out = unknowns.assign({"S0": _unit(12)}, {"S0": None}, "Mtg A")
    assert out["S0"].startswith("U")


def test_talk_stats_counts_talk_and_reliable_turns_per_cluster(sandbox):
    turns = ([{"start": i * 4.0, "end": i * 4.0 + 3.0, "cluster": "A"}
              for i in range(12)]                       # 36s, 12 reliable
             + [{"start": 100.0, "end": 100.4, "cluster": "A"}]   # short: talk only
             + [{"start": 200.0, "end": 202.0, "cluster": "B"}])  # thin cluster
    st = unknowns.talk_stats(turns)
    assert st["A"]["reliable_turns"] == 12
    assert abs(st["A"]["talk_secs"] - 36.4) < 1e-6
    assert st["B"] == {"talk_secs": 2.0, "reliable_turns": 1}


# ---------- open-set near miss, end to end through the floor ----------

def test_near_miss_stranger_stays_open_set_and_floor_gates_the_mint(sandbox):
    """A stranger scoring just under the naming bar against an enrolled person
    must NOT inherit the name (open set) — and whether they earn a global
    'Speaker N' depends on the evidence floor, not on resemblance."""
    alice = _unit(20)
    identify.enroll("Alice", alice, source="M0")
    stranger = _at_cosine(alice, 0.55, seed=6)   # near miss: 0.55 < 0.60

    named = identify.name_speakers({"S0": stranger})
    assert named["S0"]["name"] is None

    # substantial cluster: registers as a nameable unknown
    out = unknowns.assign({"S0": stranger}, {"S0": None}, "Mtg A",
                          stats={"S0": GOOD_STATS})
    assert out["S0"].startswith("U")
    # a second thin near-miss voice: suppressed by the floor, not registered
    thin = _at_cosine(alice, 0.55, seed=7)
    out2 = unknowns.assign({"S1": thin}, {"S1": None}, "Mtg B",
                           stats={"S1": THIN_STATS})
    assert "S1" not in out2
    assert len(unknowns.load()["speakers"]) == 1
