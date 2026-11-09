"""output: atomic writes + honest uncertainty marks; diarcache roundtrip;
icloud materialization states; unknowns registry lifecycle."""
import json

import numpy as np
import pytest

from stt import diarcache, icloud, output, unknowns


# ---------- output ----------

def test_txt_marks_fragile_segments(sandbox):
    p = sandbox / "t.txt"
    segs = [
        {"start": 0, "end": 5, "speaker": "S0", "name": "Mark", "text": "Clean turn.",
         "flags": [], "attribution": "diarized"},
        {"start": 5, "end": 6, "speaker": "S1", "name": None, "text": "Uncertain bit.",
         "flags": ["overlap"], "attribution": "diarized"},
    ]
    output.write_txt(p, segs, header="h")
    t = p.read_text()
    assert "Mark: Clean turn." in t
    assert "[*]: Uncertain bit." in t.replace("Speaker 2 [*]", "[*]")  # marked
    assert "uncertain attribution" in t  # legend appears only when needed
    assert not (sandbox / "t.txt.tmp").exists()  # atomic write cleaned up


def test_a_header_under_a_minute_gives_seconds(sandbox):
    """round(2 / 60, 1) printed '0.0 min' for a 2 s clip and '0.1 min' for
    5 s. Under a minute the header gives whole seconds, and both header
    readers take that form."""
    from stt import summarize
    h = output.txt_header("2026-11-08 Clip", 2.4, [])
    assert h == "2026-11-08 Clip  |  2 sec  |  0 speaker(s)"
    assert output.txt_header("x", 59.4, []).startswith("x  |  59 sec  |")
    assert output.txt_header("x", 59.6, []).startswith("x  |  1.0 min  |")   # never '60 sec'
    assert output.txt_header("x", 60.0, []).startswith("x  |  1.0 min  |")
    p = sandbox / "t.txt"
    p.write_text(h + "\n\n[00:00] Voice 1: hi\n")
    assert output.refresh_txt_header(p, "2026-11-09 Clip")
    assert p.read_text().startswith("2026-11-09 Clip  |  2 sec  |  0 speaker(s)\n")
    assert summarize._TXT_HEADER.sub("", p.read_text(), count=1) == "[00:00] Voice 1: hi\n"


def test_a_transcript_with_no_speech_says_so(sandbox):
    """A silent file wrote a header and an empty body, with no word on why."""
    p = sandbox / "t.txt"
    output.write_txt(p, [], header="h")
    assert p.read_text() == "h\n\n\n\n(no speech detected)\n"
    output.write_txt(p, [{"start": 0, "end": 1, "speaker": None, "text": "  "}], header="h")
    assert p.read_text().endswith("(no speech detected)\n")
    output.write_txt(p, [{"start": 0, "end": 1, "speaker": "S0", "name": "A",
                          "text": "hi", "flags": []}], header="h")
    assert "no speech" not in p.read_text()


def test_txt_no_legend_when_all_clean(sandbox):
    p = sandbox / "t.txt"
    output.write_txt(p, [{"start": 0, "end": 1, "speaker": "S0", "name": "A",
                          "text": "hi", "flags": [], "attribution": "diarized"}])
    assert "uncertain attribution" not in p.read_text()


def test_speaker_display_states():
    """Transcript-LOCAL unnamed clusters render as 'Voice N' — a namespace
    deliberately disjoint from the global unknowns' 'Speaker N', so a local
    fallback label can never impersonate (or collide with) a stable global
    number in the panel."""
    assert output.speaker_display("SPEAKER_00", "Mark") == "Mark"
    assert output.speaker_display("SPEAKER_02", None) == "Voice 3"
    assert output.speaker_display(None, None) == "Voice ?"
    assert unknowns.display("U007") == "Speaker 7"  # the global space, unchanged


def test_json_roundtrip_and_real_scores(sandbox):
    p = sandbox / "m.json"
    speakers = output.build_speakers(
        ["S0", "S1"],
        {"S0": {"name": "Mark", "score": 0.91}, "S1": {"name": None, "score": 0.0}})
    output.write_json(p, {"duration_sec": 60}, speakers, [], [])
    d = json.loads(p.read_text())
    assert d["speakers"][0]["match_score"] == 0.91
    assert d["speakers"][1]["name"] is None


# ---------- diarcache ----------

def test_diarcache_roundtrip_with_missing_embeddings(sandbox):
    p = sandbox / "m.diar.npz"
    turns = [{"start": 0.0, "end": 2.0, "cluster": "SPEAKER_00"},
             {"start": 2.0, "end": 2.2, "cluster": "SPEAKER_01"}]
    tembs = [np.ones(256), None]  # short turn had no usable embedding
    cents = {"SPEAKER_00": np.ones(256) * 0.5}
    diarcache.save(p, turns, tembs, cents)
    t2, e2, c2 = diarcache.load(p)[:3]
    assert t2 == turns
    assert np.allclose(e2[0], np.ones(256)) and e2[1] is None
    assert np.allclose(c2["SPEAKER_00"], 0.5)


def test_diarcache_save_is_atomic(sandbox, monkeypatch):
    """A crash mid-save must not corrupt a previously-good cache: the damage
    lands on the .tmp sibling and os.replace never fires."""
    p = sandbox / "m.diar.npz"
    turns = [{"start": 0.0, "end": 2.0, "cluster": "SPEAKER_00"}]
    tembs = [np.ones(256)]
    cents = {"SPEAKER_00": np.ones(256) * 0.5}
    diarcache.save(p, turns, tembs, cents)  # good cache on disk

    def boom(target, **kw):
        # write garbage wherever np.savez is pointed, then die mid-write
        if hasattr(target, "write"):
            target.write(b"garbage")
        else:
            open(str(target) + (".npz" if not str(target).endswith(".npz") else ""),
                 "wb").write(b"garbage")
        raise RuntimeError("crash mid-write")

    monkeypatch.setattr(diarcache.np, "savez", boom)
    with pytest.raises(RuntimeError):
        diarcache.save(p, turns, tembs, cents)
    # the pre-existing good cache is untouched and still loads
    t2 = diarcache.load(p)[0]
    assert t2 == turns


# ---------- icloud ----------

def test_local_file_is_fully_present(sandbox):
    f = sandbox / "a.m4a"
    f.write_bytes(b"x" * 4096)
    assert icloud._fully_present(f)
    assert not icloud.is_dataless(f)
    assert icloud.materialize(f, timeout=1)  # returns immediately, no download


def test_missing_file_not_present(sandbox):
    assert not icloud._fully_present(sandbox / "nope.m4a")


def test_materialize_returns_at_once_on_an_empty_file(sandbox):
    """A 0-byte file is not an iCloud placeholder (a placeholder reports
    its full size) and holds nothing to wait for. materialize() used to
    poll it until the timeout, 900 s by default, with the batch lock held."""
    import time
    f = sandbox / "zero.m4a"
    f.write_bytes(b"")
    t0 = time.monotonic()
    assert icloud.materialize(f, timeout=5, poll=0.1) is False
    assert time.monotonic() - t0 < 1.0
    assert not icloud.is_dataless(sandbox / "nope.m4a")


# ---------- unknowns ----------

def test_unknowns_lifecycle(sandbox):
    v = np.random.default_rng(1).normal(size=256)
    uid = unknowns.assign({"SPEAKER_00": v}, {"SPEAKER_00": None}, "Mtg A")["SPEAKER_00"]
    assert uid.startswith("U")
    assert unknowns.display(uid).startswith("Speaker ")

    # same voice in another meeting -> SAME stable id
    assert unknowns.assign({"SPEAKER_03": v + 0.01}, {"SPEAKER_03": None},
                           "Mtg B")["SPEAKER_03"] == uid
    assert set(unknowns.load()["speakers"][uid]["meetings"]) == {"Mtg A", "Mtg B"}

    # a different voice -> different id; a NAMED cluster is never assigned a uid
    w = np.random.default_rng(2).normal(size=256)
    out = unknowns.assign({"SPEAKER_01": w, "SPEAKER_02": v},
                          {"SPEAKER_01": None, "SPEAKER_02": "Mark"}, "Mtg B")
    uid2 = out["SPEAKER_01"]
    assert uid2 != uid and "SPEAKER_02" not in out

    # promote to a real person -> enrolled + removed from unknowns
    from stt import identify
    assert unknowns.promote(uid, "Jane")
    assert "Jane" in identify.load_registry()
    assert uid not in unknowns.load()["speakers"]

    # drop the other one: a TOMBSTONE remains (see the resurrection test), it
    # just never surfaces or matches into a global label again
    assert unknowns.drop(uid2)
    assert unknowns.load()["speakers"][uid2]["dropped"]
    assert not unknowns.drop(uid2)  # double-drop is a no-op


def test_dropped_unknown_never_resurrects_on_relabel(sandbox):
    """'Not a real speaker' used to DELETE the entry — and the relabel that
    runs after every naming re-registered the same voice from the meeting
    caches under the next free number, seconds after the user removed it.
    The tombstone must recognize the voice and suppress it instead."""
    v = np.random.default_rng(3).normal(size=256)
    uid = unknowns.assign({"S0": v}, {"S0": None}, "Mtg A")["S0"]
    assert unknowns.drop(uid)

    # the relabel pass sees the same cluster again: no label comes back, no
    # new entry is minted, and the tombstone keeps its number reserved
    out = unknowns.assign({"S0": v + 0.01}, {"S0": None}, "Mtg A")
    assert "S0" not in out
    assert set(unknowns.load()["speakers"]) == {uid}

    # a genuinely NEW voice still registers, and does not reuse the
    # tombstone's number
    w = np.random.default_rng(4).normal(size=256)
    uid2 = unknowns.assign({"S1": w}, {"S1": None}, "Mtg B")["S1"]
    assert uid2 != uid

    # and the panel list never shows the tombstone. The panel now also hides
    # any unknown whose meetings no longer exist on disk, so give uid2's
    # meeting a real transcript for it to resolve against.
    from conftest import mfile
    mfile("Mtg B", ".json").write_text(json.dumps(
        {"source_file": "Mtg B.m4a", "segments": [], "speakers": [], "words": []}))
    from gui import server as srv
    st = srv.gather_state()
    assert uid not in {u["uid"] for u in st["unknowns"]}
    assert uid2 in {u["uid"] for u in st["unknowns"]}


def test_relabel_same_meeting_does_not_stack_duplicate_samples(sandbox):
    """Every relabel re-runs assign; the returning unknown must keep ONE sample
    per meeting (this stacked identical centroids before)."""
    v = np.random.default_rng(1).normal(size=256)
    uid = unknowns.assign({"S0": v}, {"S0": None}, "Mtg A")["S0"]
    for _ in range(3):  # three relabels of the same meeting
        assert unknowns.assign({"S0": v}, {"S0": None}, "Mtg A")["S0"] == uid
    reg = unknowns.load()
    samples = np.load(sandbox / "voiceprints" / reg["speakers"][uid]["file"])
    assert samples.shape[0] == 1


def test_unknown_numbers_never_reuse_a_freed_one(sandbox):
    """Phase 4: a freed Speaker number is gone for good. Numbering used to
    restart at the lowest free slot, so naming Speaker 1 handed "Speaker 1" to
    the next stranger — and every reference to Speaker 1 anywhere else in the
    library quietly changed meaning. `next` is the counter and only moves
    forward."""
    v1 = np.random.default_rng(1).normal(size=256)
    v2 = np.random.default_rng(2).normal(size=256)
    v3 = np.random.default_rng(3).normal(size=256)
    u1 = unknowns.assign({"S0": v1}, {"S0": None}, "M1")["S0"]
    u2 = unknowns.assign({"S1": v2}, {"S1": None}, "M1")["S1"]
    assert (u1, u2) == ("U001", "U002")
    assert unknowns.promote(u1, "Jane")          # Speaker 1 gets named
    u3 = unknowns.assign({"S2": v3}, {"S2": None}, "M2")["S2"]
    assert u3 == "U003"                          # NOT the freed U001
    assert unknowns.display(u3) == "Speaker 3"
    assert unknowns.load()["next"] == 4


def _orthonormal(v, rng):
    """A unit vector orthogonal to v (for building a cluster that scores low
    against v yet is a legitimate embedding)."""
    r = rng.normal(size=v.shape)
    r = r - (r @ v) / (v @ v) * v
    return r / np.linalg.norm(r)


def test_two_distinct_clusters_never_collapse_to_one_speaker(sandbox):
    """Two genuinely different strangers in one meeting who both resemble the same
    past unknown must NOT both be labeled the same global Speaker N."""
    rng = np.random.default_rng(7)
    v = rng.normal(size=256)
    u1 = unknowns.assign({"S0": v}, {"S0": None}, "M1")["S0"]
    assert u1 == "U001"

    vhat = v / np.linalg.norm(v)
    c1 = v + rng.normal(size=256) * 1e-4               # ~identical to v
    orth = _orthonormal(v, rng)
    c2 = 0.7 * vhat + np.sqrt(1 - 0.49) * orth          # cos(c2, v) == 0.7
    # both clusters match U001 (>= MATCH_MIN) but are distinct from each other
    out = unknowns.assign({"S0": c1, "S1": c2}, {"S0": None, "S1": None}, "M2")
    assert len(set(out.values())) == 2                  # not conflated
    assert "U001" in out.values()                       # the real returning voice keeps its id


def test_over_segmented_voice_still_shares_one_speaker(sandbox):
    """Guard the split-cluster case: one person diarized into two near-identical
    clusters keeps a SINGLE global Speaker N (the fix must not over-restrict)."""
    rng = np.random.default_rng(11)
    v = rng.normal(size=256)
    u1 = unknowns.assign({"S0": v}, {"S0": None}, "M1")["S0"]
    c1 = v + rng.normal(size=256) * 1e-4
    c2 = v + rng.normal(size=256) * 1e-4
    out = unknowns.assign({"S0": c1, "S1": c2}, {"S0": None, "S1": None}, "M2")
    assert set(out.values()) == {u1}                    # both map to the same id


def test_enroll_skips_near_duplicate_sample(sandbox):
    from stt import identify
    v = np.random.default_rng(5).normal(size=256)
    identify.enroll("Jane", v, source="M1")
    identify.enroll("Jane", v * 2.0, source="M1")  # same direction after L2 norm
    assert identify.load_registry()["Jane"]["n_samples"] == 1


def test_materialize_deadline_uses_monotonic_not_wallclock(sandbox, monkeypatch):
    """A sleep/lid-close during a long download makes wall-clock time jump on
    wake; the poll deadline must be immune to that (same fix already applied
    to rates.py and run_batch.py's report(), for the identical reason).
    Proven by making time.time() raise: if materialize() still depends on it
    for the deadline, this fails loudly instead of silently passing."""
    def _boom():
        raise AssertionError("materialize() must not call time.time() for its deadline")
    monkeypatch.setattr(icloud.time, "time", _boom)
    f = sandbox / "never_appears.m4a"
    assert icloud.materialize(f, timeout=0.3, poll=0.1) is False


def test_unknown_ghosts_pruned_when_their_only_meeting_gets_named(sandbox):
    """A cluster registered as an unknown, later matched to an enrolled person
    (so promote() never ran): re-assigning that meeting must retire the stale
    unknown — otherwise 'Speaker N' ghosts pile up in the panel for meetings
    where everyone is already named. Unknowns seen in OTHER meetings stay."""
    from stt import config, unknowns
    v1 = np.random.default_rng(1).normal(size=256)
    v2 = np.random.default_rng(2).normal(size=256)

    out = unknowns.assign({"SPEAKER_00": v1, "SPEAKER_01": v2},
                          {"SPEAKER_00": None, "SPEAKER_01": None}, "MtgA")
    assert set(out.values()) == {"U001", "U002"}
    # U002's voice also appears in MtgB — multi-meeting evidence
    unknowns.assign({"SPEAKER_00": v2}, {"SPEAKER_00": None}, "MtgB")

    # relabel of MtgA after enrollment: both clusters now NAMED
    out = unknowns.assign({"SPEAKER_00": v1, "SPEAKER_01": v2},
                          {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}, "MtgA")
    assert out == {}
    reg = unknowns.load()
    assert "U001" not in reg["speakers"]  # only-MtgA ghost retired
    assert not (config.VOICEPRINTS_DIR / "U001.npy").exists()
    assert "U002" in reg["speakers"]  # still evidenced by MtgB


def test_registry_backup_survives_a_wipe(sandbox):
    """Every save that would overwrite a registry WITH people first rolls it
    to registry.json.bak — one accidental wipe stays a seconds-long restore,
    not a rebuild of biometric data."""
    from stt import config, identify
    identify.enroll("Alice", np.random.default_rng(3).normal(size=256), source="M")
    identify.enroll("Bob", np.random.default_rng(4).normal(size=256), source="M")
    identify.save_registry({})  # the wipe
    assert identify.load_registry() == {}
    bak = config.VOICEPRINTS_DIR / "registry.json.bak"
    assert bak.exists()
    saved = json.loads(bak.read_text())
    assert set(saved) == {"Alice", "Bob"}  # full pre-wipe registry, restorable


def test_archive_hides_but_keeps_matching(sandbox):
    """Archiving a one-time voice keeps it matchable: a later meeting with the
    same voice reuses the SAME number instead of minting a new one (numbers
    stay stable), and restore() brings the entry back for naming."""
    from stt import unknowns
    v = np.random.default_rng(9).normal(size=256)
    uid = unknowns.assign({"S0": v}, {"S0": None}, "Focus Group A")["S0"]
    assert unknowns.archive(uid)
    assert unknowns.load()["speakers"][uid]["archived"] is True

    again = unknowns.assign({"S0": v}, {"S0": None}, "Focus Group B")["S0"]
    assert again == uid  # archived entry still matched, no duplicate speaker

    assert unknowns.restore(uid)
    assert "archived" not in unknowns.load()["speakers"][uid]
    assert not unknowns.archive("U999")  # unknown uid: clean False


def test_promoted_split_voice_does_not_resurrect_as_new_unknown(sandbox):
    """The diarizer split one person into two clusters; both unknowns were
    named to the same person (promote moves their samples into the enrolled
    profile and deletes the unknown entries). The next relabel can give the
    name to only ONE cluster (one-name-per-meeting), so the loser cluster
    used to match nothing and resurrect as a fresh 'Speaker 1' seconds after
    the naming. A voice that strongly matches an ENROLLED profile must never
    mint a new unknown."""
    from stt import identify

    rng = np.random.default_rng(7)
    va, vb = rng.normal(size=256), rng.normal(size=256)   # split: far apart
    uids = unknowns.assign({"SA": va, "SB": vb}, {"SA": None, "SB": None}, "Mtg T")
    assert unknowns.promote(uids["SA"], "Casey Example")
    assert unknowns.promote(uids["SB"], "Casey Example")
    assert unknowns.load()["speakers"] == {}
    assert identify.load_registry()["Casey Example"]["n_samples"] == 2

    # relabel pass: cluster A won the name, cluster B lost the race
    out = unknowns.assign({"SA": va, "SB": vb},
                          {"SA": "Casey Example", "SB": None}, "Mtg T")
    assert "SB" not in out                       # suppressed, no global label
    assert unknowns.load()["speakers"] == {}     # and nothing was minted

    # a genuine stranger in the same pass still registers normally
    w = rng.normal(size=256)
    out = unknowns.assign({"SC": w}, {"SC": None}, "Mtg T")
    assert out["SC"].startswith("U")


def test_stranger_resembling_enrolled_person_re_mints_after_hard_delete(sandbox):
    """A cluster whose unknown entry was HARD-DELETED (registry GC) must be
    re-registered by the next relabel. The enrolled-voice mint gate used to
    fire on mere resemblance (>= MATCH_MIN): a DISTINCT stranger scoring ~0.65
    against somebody enrolled was suppressed forever — no registry entry,
    nothing in the panel to raise, an unresolvable raw label in the transcript
    (the third-voice-never-raised bug). Only a genuine split fragment of the
    enrolled voice (>= SPLIT_SIM) stays suppressed."""
    from stt import identify

    rng = np.random.default_rng(11)
    u = rng.normal(size=256)
    u /= np.linalg.norm(u)
    w1 = rng.normal(size=256)
    w1 -= (w1 @ u) * u
    w1 /= np.linalg.norm(w1)
    w2 = rng.normal(size=256)
    w2 -= (w2 @ u) * u + (w2 @ w1) * w1
    w2 /= np.linalg.norm(w2)
    identify.enroll("Quinn", u, source="Hearing")

    # a stranger RESEMBLING Quinn (0.65: >= MATCH_MIN, below SPLIT_SIM) whose
    # own unknown entry no longer exists (GC'd): must mint fresh, not orphan
    stranger = 0.65 * u + np.sqrt(1 - 0.65 ** 2) * w1
    out = unknowns.assign({"S1": u, "S2": stranger},
                          {"S1": "Quinn", "S2": None}, "Hearing")
    assert out.get("S2", "").startswith("U")            # nameable again
    assert unknowns.load()["speakers"][out["S2"]]["meetings"] == ["Hearing"]

    # a genuine split FRAGMENT of Quinn's own voice still never mints
    frag = 0.9 * u + np.sqrt(1 - 0.9 ** 2) * w2
    out2 = unknowns.assign({"S1": u, "S2": stranger, "S3": frag},
                           {"S1": "Quinn", "S2": None, "S3": None}, "Hearing")
    assert out2["S2"] == out["S2"]                      # stranger keeps their number
    assert "S3" not in out2                             # fragment suppressed
    assert set(unknowns.load()["speakers"]) == {out["S2"]}  # nothing else minted


def test_diarcache_channel_extras_roundtrip_and_backcompat(sandbox):
    p = sandbox / "m.diar.npz"
    turns = [{"start": 0.0, "end": 2.0, "cluster": "SPEAKER_00"}]
    diarcache.save(p, turns, [np.ones(8)], {"SPEAKER_00": np.ones(8)},
                   mark_spans=[(3.0, 5.0)], mark_embs=[np.ones(8) * 0.7],
                   channel_mode="stereo_channel_aware", mic_speaker="Mark Paik")
    ch = diarcache.load_channel(p)
    assert ch["mode"] == "stereo_channel_aware" and ch["mic_speaker"] == "Mark Paik"
    assert ch["spans"] == [(3.0, 5.0)]
    assert np.allclose(ch["embs"][0], np.ones(8) * 0.7)
    # a plain (mono) cache reports no channel data -> relabel treats it as mono
    q = sandbox / "mono.diar.npz"
    diarcache.save(q, turns, [np.ones(8)], {"SPEAKER_00": np.ones(8)})
    mono = diarcache.load_channel(q)
    assert mono["mode"] is None and mono["spans"] == []


def _corrupt_caches(tmp_path):
    import numpy as np
    good = tmp_path / "good.diar.npz"
    diarcache.save(good, [{"start": 0.0, "end": 1.0, "cluster": "SPEAKER_00"}],
                   [np.ones(4)], {"SPEAKER_00": np.ones(4)})
    out = {}
    out["truncated"] = tmp_path / "truncated.diar.npz"
    out["truncated"].write_bytes(good.read_bytes()[:40])
    out["garbage"] = tmp_path / "garbage.diar.npz"
    out["garbage"].write_bytes(b"\x80\x04 not an archive at all" * 10)
    out["empty"] = tmp_path / "empty.diar.npz"
    out["empty"].write_bytes(b"")
    wrong = tmp_path / "wrong.diar.npz"
    np.savez(wrong, something=np.ones(3))
    out["wrong shape"] = wrong
    return out


def test_an_unreadable_diar_cache_is_a_value_error_with_no_path(tmp_path):
    """A truncated cache raised zipfile.BadZipFile and a garbage one
    pickle.UnpicklingError. Neither is an OSError or a ValueError, so the
    (OSError, ValueError, KeyError) guards at every call site missed them,
    and the panel answered 500 with the server's absolute path."""
    for kind, p in _corrupt_caches(tmp_path).items():
        for fn in (diarcache.load, diarcache.load_channel):
            if kind == "wrong shape" and fn is diarcache.load_channel:
                continue   # no channel keys reads as a mono cache, by design
            with pytest.raises(diarcache.Unreadable) as e:
                fn(p)
            assert isinstance(e.value, ValueError), kind
            assert str(tmp_path) not in str(e.value) and "PosixPath" not in str(e.value)
    with pytest.raises(FileNotFoundError):
        diarcache.load(tmp_path / "missing.diar.npz")
