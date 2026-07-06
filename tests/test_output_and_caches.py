"""output: atomic writes + honest uncertainty marks; diarcache roundtrip;
icloud materialization states; unknowns registry lifecycle."""
import json

import numpy as np

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


def test_txt_no_legend_when_all_clean(sandbox):
    p = sandbox / "t.txt"
    output.write_txt(p, [{"start": 0, "end": 1, "speaker": "S0", "name": "A",
                          "text": "hi", "flags": [], "attribution": "diarized"}])
    assert "uncertain attribution" not in p.read_text()


def test_speaker_display_states():
    assert output.speaker_display("SPEAKER_00", "Mark") == "Mark"
    assert output.speaker_display("SPEAKER_02", None) == "Speaker 3"
    assert output.speaker_display(None, None) == "Speaker ?"


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


# ---------- icloud ----------

def test_local_file_is_fully_present(sandbox):
    f = sandbox / "a.m4a"
    f.write_bytes(b"x" * 4096)
    assert icloud._fully_present(f)
    assert not icloud.is_dataless(f)
    assert icloud.materialize(f, timeout=1)  # returns immediately, no download


def test_missing_file_not_present(sandbox):
    assert not icloud._fully_present(sandbox / "nope.m4a")
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

    # drop the other one entirely
    assert unknowns.drop(uid2)
    assert unknowns.load()["speakers"] == {}


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


def test_unknown_numbers_restart_after_naming(sandbox):
    """Freed Speaker numbers are reused: name Speaker 1, and the next new voice
    becomes Speaker 1 again instead of counting up forever."""
    v1 = np.random.default_rng(1).normal(size=256)
    v2 = np.random.default_rng(2).normal(size=256)
    v3 = np.random.default_rng(3).normal(size=256)
    u1 = unknowns.assign({"S0": v1}, {"S0": None}, "M1")["S0"]
    u2 = unknowns.assign({"S1": v2}, {"S1": None}, "M1")["S1"]
    assert (u1, u2) == ("U001", "U002")
    assert unknowns.promote(u1, "Jane")          # Speaker 1 gets named
    u3 = unknowns.assign({"S2": v3}, {"S2": None}, "M2")["S2"]
    assert u3 == "U001"                          # the freed number is reused
    assert unknowns.display(u3) == "Speaker 1"


def test_enroll_skips_near_duplicate_sample(sandbox):
    from stt import identify
    v = np.random.default_rng(5).normal(size=256)
    identify.enroll("Jane", v, source="M1")
    identify.enroll("Jane", v * 2.0, source="M1")  # same direction after L2 norm
    assert identify.load_registry()["Jane"]["n_samples"] == 1
