"""identify: enrollment lifecycle + open-set matching from every state —
empty library, one person, near-duplicate voices, strangers, corrupt vectors."""
import numpy as np
import pytest

from stt import identify


def _vec(seed, dim=256):
    return np.random.default_rng(seed).normal(size=dim)


def _near(axis, i, dim=256):
    """A vector clearly dominated by `axis` (so np.argmax(|v|) == axis) but
    distinct enough from its siblings that enroll's 0.999 dedup keeps them all."""
    v = np.zeros(dim)
    v[axis] = 1.0
    v[10 + i] = 0.2
    return v


def test_empty_library_names_nobody(sandbox):
    res = identify.name_speakers({"SPEAKER_00": _vec(1)})
    assert res["SPEAKER_00"]["name"] is None


def test_enroll_and_match(sandbox):
    identify.enroll("Mark", _vec(1))
    res = identify.name_speakers({"S0": _vec(1) + 0.05 * _vec(9)}, threshold=0.6, margin=0.1)
    assert res["S0"]["name"] == "Mark"
    assert res["S0"]["score"] > 0.9


def test_stranger_stays_unknown(sandbox):
    identify.enroll("Mark", _vec(1))
    res = identify.name_speakers({"S0": _vec(2)}, threshold=0.6, margin=0.1)
    assert res["S0"]["name"] is None


def test_margin_blocks_ambiguous_match(sandbox):
    """Two enrolled voices nearly identical: neither may claim a matching speaker
    without a clear margin — open-set safety."""
    base = _vec(1)
    identify.enroll("A", base)
    identify.enroll("B", base + 0.01 * _vec(3))
    res = identify.name_speakers({"S0": base}, threshold=0.6, margin=0.15)
    assert res["S0"]["name"] is None  # ambiguous -> no guess


def test_greedy_uniqueness(sandbox):
    identify.enroll("Mark", _vec(1))
    # two clusters both resembling Mark: only ONE gets the name
    res = identify.name_speakers({"S0": _vec(1), "S1": _vec(1) + 0.02 * _vec(4)},
                                 threshold=0.5, margin=0.0)
    named = [k for k, v in res.items() if v["name"] == "Mark"]
    assert len(named) == 1


def test_multi_sample_rolling_window(sandbox):
    for i in range(identify.MAX_SAMPLES + 3):
        identify.enroll("Mark", _vec(i))
    reg = identify.load_registry()
    assert reg["Mark"]["n_samples"] == identify.MAX_SAMPLES
    assert identify.load_voiceprints()["Mark"].shape[0] == identify.MAX_SAMPLES


def test_legacy_1d_voiceprint_still_loads(sandbox):
    from stt import config
    identify.enroll("Old", _vec(5))
    # rewrite the file in the legacy single-centroid 1-D format
    f = config.VOICEPRINTS_DIR / "Old.npy"
    np.save(f, _vec(5))
    vp = identify.load_voiceprints()["Old"]
    assert vp.ndim == 2 and vp.shape[0] == 1


def test_zero_vector_rejected(sandbox):
    with pytest.raises(ValueError):
        identify.enroll("Bad", np.zeros(256))


def test_nan_cosine_never_matches(sandbox):
    identify.enroll("Mark", _vec(1))
    bad = np.full(256, np.nan)
    res = identify.name_speakers({"S0": bad}, threshold=0.0, margin=0.0)
    assert res["S0"]["name"] is None  # NaN must not clear any threshold


def test_rename_and_merge_and_remove(sandbox):
    identify.enroll("Marc", _vec(1))
    assert identify.rename_person("Marc", "Mark")
    assert "Mark" in identify.load_registry()
    identify.enroll("Mark P", _vec(1) + 0.05 * _vec(2))
    assert identify.merge_people("Mark P", "Mark")
    assert "Mark P" not in identify.load_registry()
    assert identify.load_registry()["Mark"]["n_samples"] == 2
    assert identify.remove_person("Mark")
    assert identify.load_registry() == {}


def test_rename_to_existing_becomes_merge(sandbox):
    identify.enroll("A", _vec(1))
    identify.enroll("B", _vec(2))
    assert identify.rename_person("A", "B")
    reg = identify.load_registry()
    assert "A" not in reg and reg["B"]["n_samples"] == 2


def test_enroll_records_source_provenance(sandbox):
    identify.enroll("Mark", _vec(1), source="LT Meeting A")
    identify.enroll("Mark", _vec(2), source="LT Meeting B")
    reg = identify.load_registry()
    assert reg["Mark"]["sources"] == ["LT Meeting A", "LT Meeting B"]
    # rolling window keeps sources aligned with samples
    for i in range(identify.MAX_SAMPLES):
        identify.enroll("Mark", _vec(10 + i), source=f"M{i}")
    reg = identify.load_registry()
    assert len(reg["Mark"]["sources"]) == identify.MAX_SAMPLES
    assert reg["Mark"]["sources"][-1] == f"M{identify.MAX_SAMPLES - 1}"


def test_enroll_keeps_a_diverse_sample_over_recent_duplicates(sandbox):
    """The 5-sample cap keeps a spread across recording conditions, not just the
    most-recent samples. A distinctive early sample must survive a later run of
    near-duplicates from one room — which most-recent eviction would drop, since
    matching by max cosine gains little from five clips of the same recording."""
    identify.enroll("Mark", _near(1, 0), source="room-B")   # distinctive, first
    for i in range(5):  # five near-duplicates from one room, arriving later
        identify.enroll("Mark", _near(0, i), source=f"room-A-{i}")
    reg = identify.load_registry()
    assert reg["Mark"]["n_samples"] == identify.MAX_SAMPLES
    # the distinctive room-B sample survived (most-recent eviction drops it)
    assert "room-B" in reg["Mark"]["sources"]
    axes = {int(np.argmax(np.abs(row))) for row in identify.load_voiceprints()["Mark"]}
    assert axes == {0, 1}, f"a recording condition was lost at the cap: {axes}"


def test_merge_combines_sources(sandbox):
    identify.enroll("A", _vec(1), source="M1")
    identify.enroll("B", _vec(2), source="M2")
    identify.merge_people("A", "B")
    assert identify.load_registry()["B"]["sources"] == ["M2", "M1"]


def test_merge_keeps_a_spread_of_both_voices(sandbox):
    """Merging two FULL profiles must carry both people's voice into the result.
    A plain tail of [dst, src] dropped every one of dst's samples once dst held
    MAX_SAMPLES, so the merged profile represented only the voice merged in last
    — the one the user was NOT looking at."""
    for i in range(identify.MAX_SAMPLES):
        identify.enroll("Katie", _near(0, i), source=f"k{i}")
    for i in range(identify.MAX_SAMPLES):
        identify.enroll("Bob", _near(1, i), source=f"b{i}")
    assert identify.merge_people("Bob", "Katie")  # Bob folded into Katie
    merged = identify.load_voiceprints()["Katie"]
    assert merged.shape[0] == identify.MAX_SAMPLES
    axes = {int(np.argmax(np.abs(row))) for row in merged}
    assert axes == {0, 1}, f"a voice was dropped in the merge: axes={axes}"
    # sources stay aligned with whichever samples survived
    assert len(identify.load_registry()["Katie"]["sources"]) == identify.MAX_SAMPLES


def test_reassign_sample_moves_the_embedding_with_its_source(sandbox):
    """A misattributed sample MOVES to the right person — embedding and source
    provenance preserved — rather than being discarded (which loses the voice)
    or left in the wrong profile."""
    identify.enroll("Katie", _near(0, 0), source="M1")
    identify.enroll("Katie", _near(1, 0), source="M2")  # actually Bob's voice
    r = identify.reassign_sample("Katie", 1, "Bob")
    assert r["ok"] and r["to"] == "Bob" and r["source_emptied"] is False
    reg = identify.load_registry()
    assert reg["Katie"]["n_samples"] == 1 and reg["Katie"]["sources"] == ["M1"]
    assert reg["Bob"]["n_samples"] == 1 and reg["Bob"]["sources"] == ["M2"]
    # the axis-1 voice really landed on Bob (not a copy of Katie's)
    assert int(np.argmax(np.abs(identify.load_voiceprints()["Bob"][0]))) == 1


def test_reassign_last_sample_drops_the_wrong_profile(sandbox):
    """Reassigning a profile's ONLY sample means the whole profile was the wrong
    person: the source is removed, the destination gains the voice."""
    identify.enroll("Ghost", _near(0, 0), source="M1")
    r = identify.reassign_sample("Ghost", 0, "Real Person")
    assert r["ok"] and r["source_emptied"] is True
    reg = identify.load_registry()
    assert "Ghost" not in reg and reg["Real Person"]["n_samples"] == 1


def test_reassign_sample_validates(sandbox):
    identify.enroll("Katie", _near(0, 0), source="M1")
    assert not identify.reassign_sample("Katie", 0, "")["ok"]        # no destination
    assert not identify.reassign_sample("Katie", 0, "Katie")["ok"]   # same person
    assert not identify.reassign_sample("Nobody", 0, "Bob")["ok"]    # unknown source
    assert not identify.reassign_sample("Katie", 9, "Bob")["ok"]     # no such sample


def test_remove_sample_states(sandbox):
    identify.enroll("Mark", _vec(1), source="M1")
    identify.enroll("Mark", _vec(2), source="M2")
    identify.enroll("Mark", _vec(3), source="M3")
    assert identify.remove_sample("Mark", 1)  # drop the M2 sample
    reg = identify.load_registry()
    assert reg["Mark"]["n_samples"] == 2
    assert reg["Mark"]["sources"] == ["M1", "M3"]
    assert not identify.remove_sample("Mark", 9)      # out of range
    assert identify.remove_sample("Mark", 0)
    assert not identify.remove_sample("Mark", 0)      # last sample protected
    assert not identify.remove_sample("Nobody", 0)    # unknown person


def test_enroll_sanitized_filename_collision_does_not_overwrite(sandbox):
    """'A/B' and 'A_B' both sanitize to 'A_B.npy' — enrolling the second must
    never silently overwrite the first person's voiceprint file."""
    v1, v2 = _vec(1), _vec(2)
    identify.enroll("A_B", v1)
    identify.enroll("A/B", v2)

    reg = identify.load_registry()
    assert reg["A_B"]["file"] != reg["A/B"]["file"]

    vps = identify.load_voiceprints()
    assert np.allclose(vps["A_B"][0], v1 / np.linalg.norm(v1))
    assert np.allclose(vps["A/B"][0], v2 / np.linalg.norm(v2))


def test_reenroll_reuses_own_disambiguated_file(sandbox):
    """Adding a second sample to an already-disambiguated person must reuse
    THEIR file (from the registry), not recompute a fresh, colliding guess."""
    identify.enroll("A_B", _vec(1))
    identify.enroll("A/B", _vec(2))  # gets disambiguated to A_B_2.npy
    disambiguated_file = identify.load_registry()["A/B"]["file"]

    identify.enroll("A/B", _vec(3))  # a second sample for the SAME person

    reg = identify.load_registry()
    assert reg["A/B"]["file"] == disambiguated_file  # unchanged, not re-derived
    assert reg["A/B"]["n_samples"] == 2
    assert reg["A_B"]["n_samples"] == 1  # untouched


def test_rename_into_colliding_name_does_not_overwrite(sandbox):
    """Renaming into a name whose sanitized filename collides with an
    unrelated existing person must not destroy that person's voiceprint."""
    v_existing, v_renamed = _vec(1), _vec(2)
    identify.enroll("A_B", v_existing)
    identify.enroll("Someone Else", v_renamed)

    assert identify.rename_person("Someone Else", "A/B") is True

    reg = identify.load_registry()
    assert reg["A_B"]["file"] != reg["A/B"]["file"]
    vps = identify.load_voiceprints()
    assert np.allclose(vps["A_B"][0], v_existing / np.linalg.norm(v_existing))
    assert np.allclose(vps["A/B"][0], v_renamed / np.linalg.norm(v_renamed))


def test_lock_registry_serializes_concurrent_enroll(sandbox):
    """Real OS-level flock contention: a slow holder blocks a concurrent
    enroll() from running until it releases."""
    import threading
    import time

    identify.enroll("Existing", _vec(1))

    def holder(release):
        with identify.lock_registry():
            release.wait(1.0)

    release = threading.Event()
    t = threading.Thread(target=holder, args=(release,))
    t.start()
    time.sleep(0.05)

    start = time.monotonic()
    identify.enroll("New Person", _vec(2))
    elapsed = time.monotonic() - start
    release.set()
    t.join()

    assert elapsed >= 0.9, f"enroll() returned after {elapsed:.2f}s — did not wait for the lock"
    assert "New Person" in identify.load_registry()


def test_lock_registry_is_reentrant_within_one_thread(sandbox):
    """promote() holds the lock and calls enroll() (which also acquires it) —
    must not deadlock against itself."""
    from stt import unknowns
    v = _vec(3)
    reg = {"next": 2, "speakers": {"U001": {"file": "U001.npy", "meetings": ["Mtg"]}}}
    (identify.config.VOICEPRINTS_DIR / "unknowns.json").write_text(
        __import__("json").dumps(reg))
    np.save(identify.config.VOICEPRINTS_DIR / "U001.npy", v.reshape(1, -1))

    import threading
    done = threading.Event()

    def go():
        assert unknowns.promote("U001", "Dana") is True
        done.set()

    t = threading.Thread(target=go)
    t.start()
    t.join(timeout=3)
    assert done.is_set(), "promote() deadlocked re-acquiring its own lock via enroll()"
    assert "Dana" in identify.load_registry()


def test_registry_writes_are_atomic_no_partial_file(sandbox):
    """A crash mid-write must never leave a half-written registry.json —
    save_registry always writes to a temp file then renames."""
    identify.enroll("Mark", _vec(1))
    # simulate the write path directly: the .tmp file must not linger after a
    # normal save (proves rename-not-copy, so a reader never sees a partial file)
    assert not (identify.config.VOICEPRINTS_DIR / "registry.json.tmp").exists()
    assert (identify.config.VOICEPRINTS_DIR / "registry.json").exists()


def test_corrupt_registry_warns_instead_of_silently_resetting(sandbox, capsys):
    (identify.config.VOICEPRINTS_DIR / "registry.json").write_text("{not json")
    reg = identify.load_registry()
    assert reg == {}
    assert "corrupt" in capsys.readouterr().err.lower()


def test_corrupt_registry_is_preserved_not_clobbered(sandbox):
    """A corrupt registry.json must be routed to a timestamped .json.corrupt
    sidecar before the next save overwrites it, and a previously-written GOOD
    .json.bak must survive untouched (never clobbered by the corrupt content)."""
    from stt import config
    # a good .bak already exists from an earlier valid save
    good_bak = config.VOICEPRINTS_DIR / "registry.json.bak"
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    good_bak.write_text('{"Priya": {"file": "Priya.npy", "n_samples": 1}}')
    # the live registry is now corrupt on disk
    (config.VOICEPRINTS_DIR / "registry.json").write_text("{not json")

    identify.enroll("Mark", _vec(1))  # a normal save over the corrupt file

    corrupt_sidecars = list(config.VOICEPRINTS_DIR.glob("registry.json.corrupt*"))
    assert corrupt_sidecars, "corrupt registry bytes were destroyed with no sidecar"
    assert corrupt_sidecars[0].read_text() == "{not json"
    # the good .bak is intact, not overwritten by the corrupt content
    assert '"Priya"' in good_bak.read_text()


def test_save_registry_no_absence_window_for_unlocked_reader(sandbox, monkeypatch):
    """save_registry must never remove registry.json from its path during the
    backup step — an unlocked concurrent reader (diarize.py load_voiceprints in a
    batch worker) must see the prior registry, never a transient empty one."""
    import threading

    identify.enroll("Existing", _vec(1))
    prior = identify.load_registry()
    assert prior  # non-empty baseline

    started, release = threading.Event(), threading.Event()
    real_atomic = identify._atomic_write

    def blocking_atomic(path, text):
        started.set()          # backup step is already done; window (if any) is open
        release.wait(2.0)
        real_atomic(path, text)

    monkeypatch.setattr(identify, "_atomic_write", blocking_atomic)

    def writer():
        with identify.lock_registry():
            identify.save_registry({**prior, "New": prior["Existing"]})

    t = threading.Thread(target=writer)
    t.start()
    assert started.wait(2.0)
    # reader does NOT take lock_registry (mirrors the real unlocked read sites)
    seen = identify.load_registry()
    release.set()
    t.join()

    assert seen, "unlocked reader saw an empty registry during the backup window"


def test_merge_and_rename_with_orphaned_file_fail_cleanly(sandbox):
    """A registry entry whose .npy is missing on disk must make merge/rename
    return False (like remove_sample), not raise FileNotFoundError."""
    from stt import config
    identify.enroll("Real", _vec(1))
    # forge an orphaned entry: registry references a file that isn't there
    reg = identify.load_registry()
    reg["Ghost"] = {"file": "Ghost.npy", "n_samples": 1, "sources": ["?"]}
    identify.save_registry(reg)

    assert identify.merge_people("Ghost", "Real") is False
    assert identify.rename_person("Ghost", "NewGhost") is False
    # registry left unchanged: Ghost still present, no NewGhost, Real intact
    after = identify.load_registry()
    assert set(after) == {"Real", "Ghost"}


def test_rename_person_crash_after_copy_leaves_old_state_valid(sandbox, monkeypatch):
    """A crash between moving the .npy and committing the registry must not
    orphan the file. Copy-commit-delete keeps 'old' fully enrolled if save
    fails, and a retry then succeeds."""
    identify.enroll("Old", _vec(1))
    identify.enroll("Old", _vec(2))  # two samples so we can assert they survive

    real_save = identify.save_registry
    boom = {"armed": True}

    def crashing_save(reg):
        if boom["armed"]:
            raise RuntimeError("simulated crash before commit")
        return real_save(reg)

    monkeypatch.setattr(identify, "save_registry", crashing_save)
    with pytest.raises(RuntimeError):
        identify.rename_person("Old", "New")

    # on-disk registry untouched: 'Old' still resolves to a loadable voiceprint
    monkeypatch.setattr(identify, "save_registry", real_save)
    vps = identify.load_voiceprints()
    assert "Old" in vps and vps["Old"].shape[0] == 2
    assert "New" not in identify.load_registry()
    # retry self-heals
    assert identify.rename_person("Old", "New") is True
    assert identify.load_voiceprints()["New"].shape[0] == 2


def test_loo_scores_expose_a_wrong_voice_sample():
    """The LOO regression fixture: three samples of one voice score >0.85
    against their stackmates; a wrong-voice sample buried in the stack scores
    near zero — leave-one-out is the honest way to find a bad enrollment."""
    rng = np.random.default_rng(21)
    u = rng.normal(size=256)
    u /= np.linalg.norm(u)
    w = rng.normal(size=256)
    w -= (w @ u) * u
    w /= np.linalg.norm(w)                     # the intruder: orthogonal voice

    def near(_):
        r = rng.normal(size=256)
        r = 0.2 * r / np.linalg.norm(r)   # a small perturbation, not 256-dim noise
        v = u + r
        return v / np.linalg.norm(v)

    stack = np.vstack([near(1), near(2), w, near(3)])
    loo = identify.loo_scores(stack)
    assert loo[2] < 0.2, f"intruder not exposed: {loo}"
    assert all(s > 0.85 for i, s in enumerate(loo) if i != 2), loo
    assert int(np.argmin(loo)) == 2


def test_loo_scores_degenerate_stacks():
    v = _vec(1) / np.linalg.norm(_vec(1))
    assert identify.loo_scores(v.reshape(1, -1)) == [-1.0]  # nothing to compare


# ---------- sample_check: the enrollment quality gate ----------

def _ortho_pair(seed=31):
    rng = np.random.default_rng(seed)
    u = rng.normal(size=256)
    u /= np.linalg.norm(u)
    w = rng.normal(size=256)
    w -= (w @ u) * u
    w /= np.linalg.norm(w)
    return u, w


def test_sample_check_warns_on_a_low_own_stack_cosine(sandbox):
    """A candidate scoring < 0.45 against the person's existing samples is
    probably not their voice — surface the number, demand a confirm."""
    u, w = _ortho_pair()
    identify.enroll("Katie", u, source="M1")
    cand = 0.30 * u + np.sqrt(1 - 0.30 ** 2) * w
    warn = identify.sample_check("Katie", cand)
    assert warn is not None
    assert abs(warn["own"] - 0.30) < 0.01


def test_sample_check_warns_when_another_profile_fits_better(sandbox):
    """The same-meeting-wrong-cluster enrollment: the candidate clears the
    absolute bar against its target but matches somebody ELSE better."""
    u, w = _ortho_pair()
    identify.enroll("Katie", u, source="M1")
    identify.enroll("Mark", w, source="M1")
    cand = 0.50 * u + np.sqrt(1 - 0.50 ** 2) * w   # Katie 0.50, Mark 0.866
    warn = identify.sample_check("Katie", cand)
    assert warn is not None and warn["cross_name"] == "Mark"
    assert warn["cross"] > warn["own"]


def test_sample_check_clean_sample_and_new_person_pass(sandbox):
    u, w = _ortho_pair()
    identify.enroll("Katie", u, source="M1")
    good = 0.90 * u + np.sqrt(1 - 0.90 ** 2) * w
    assert identify.sample_check("Katie", good) is None
    # a brand-new person has no stack to disagree with
    assert identify.sample_check("Somebody New", w) is None


def test_cosine_dimension_mismatch_is_safe_not_a_crash():
    """A voiceprint saved under a different embedding size (e.g. after a
    model/backend change) must score as 'no match', not crash — and crucially
    must not raise inside a loop scoring it against every OTHER speaker too."""
    a = np.zeros(256); a[0] = 1.0
    b = np.zeros(192); b[0] = 1.0
    assert identify.cosine(a, b) == -1.0
    assert identify.cosine(b, a) == -1.0


def test_score_against_survives_one_mismatched_sample_among_many():
    """One bad-dimension sample in a person's stack must not crash scoring
    against their OTHER, correctly-shaped samples."""
    good = _vec(1)
    bad = np.zeros(10)
    score = identify.score_against(good, [bad, good])
    assert score > 0.99  # the good sample still matches; bad one is just ignored
