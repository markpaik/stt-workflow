"""relabel.py end-to-end: a manually-named speaker (someone the diarizer never
heard, named via the review workflow) must survive the segment/roster rebuild
that a relabel does — repeatedly, without colliding with themself or anyone
else named the same way in a later session."""
import json

import numpy as np

from stt import config, diarcache, review
from conftest import mfile


def _seed_meeting(base="Mtg"):
    """Two diarized speakers, ~9s of words, no enrolled voiceprints — the
    simplest real input relabel_one() accepts."""
    words = ([{"start": 0.5 * i, "end": 0.5 * i + 0.4, "word": f"w{i}"}
              for i in range(10)]
             + [{"start": 5.0 + 0.5 * i, "end": 5.4 + 0.5 * i, "word": f"x{i}"}
                for i in range(8)])
    data = {"source_file": f"{base}.m4a", "duration_sec": 9.0, "strict": False,
            "speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 1"},
                         {"id": "SPEAKER_01", "name": None, "display": "Speaker 2"}],
            "segments": [], "words": words}
    (mfile(base, ".json")).write_text(json.dumps(data))
    (mfile(base, ".txt")).write_text("stub")

    rng = np.random.default_rng(3)
    raw_turns = [{"start": 0.0, "end": 5.0, "cluster": "SPEAKER_00"},
                 {"start": 5.0, "end": 9.0, "cluster": "SPEAKER_01"}]
    cent_emb = {"SPEAKER_00": rng.normal(size=256), "SPEAKER_01": rng.normal(size=256)}
    diarcache.save(mfile(base, ".diar.npz"), raw_turns,
                   [None, None], cent_emb)
    return data


def test_manual_speaker_survives_relabel_and_does_not_collide(sandbox):
    import relabel

    _seed_meeting("Mtg")
    # a human names a voice the diarizer missed entirely (crosstalk)
    r = review.insert_segment("Mtg", 6.5, 7.5, "name:Louise", "Quick aside.")
    assert r["ok"]
    before = json.loads((mfile("Mtg", ".json")).read_text())
    assert any(s["id"] == "MANUAL_1" and s["display"] == "Louise"
               for s in before["speakers"])

    assert relabel.relabel_one("Mtg") is True
    after = json.loads((mfile("Mtg", ".json")).read_text())
    manual = [s for s in after["speakers"] if str(s["id"]).startswith("MANUAL_")]
    assert manual == [{"id": "MANUAL_1", "name": "Louise", "global_id": None,
                       "display": "Louise", "match_score": None, "manual": True}]
    assert any(s["text"] == "Quick aside." and s["display"] == "Louise"
              for s in after["segments"])
    assert "Louise: Quick aside." in (mfile("Mtg", ".txt")).read_text()

    # a SECOND relabel (e.g. after enrolling someone else) must reuse MANUAL_1,
    # never mint a fresh MANUAL_2 for the same person
    assert relabel.relabel_one("Mtg") is True
    again = json.loads((mfile("Mtg", ".json")).read_text())
    manual_ids = [s["id"] for s in again["speakers"] if str(s["id"]).startswith("MANUAL_")]
    assert manual_ids == ["MANUAL_1"]


def test_editing_an_inserted_line_updates_it_in_place_not_a_new_decision(sandbox):
    """The specific P0-4 bug: fixing a typo in an inserted line used to record
    a competing 'edit' decision that the timestamp-based dedup let silently
    replace the 'insert' decision, so the line vanished on the next relabel."""
    _seed_meeting("Mtg")
    r = review.insert_segment("Mtg", 6.5, 7.5, "name:Louise", "Quikc aside.")
    idx = r["index"]
    assert review.count_decisions("Mtg") == 1

    r2 = review.apply("Mtg", idx, "edit", start=6.5, text="Quick aside.")
    assert r2["ok"]
    assert review.count_decisions("Mtg") == 1  # still one decision, not two

    decisions = json.loads((mfile("Mtg", ".reviews.json")).read_text())
    assert len(decisions) == 1
    assert decisions[0]["action"] == "insert"
    assert decisions[0]["text"] == "Quick aside."
    assert decisions[0]["speaker_id"] == "MANUAL_1"

    import relabel
    assert relabel.relabel_one("Mtg") is True
    after = json.loads((mfile("Mtg", ".json")).read_text())
    texts = [s["text"] for s in after["segments"]]
    assert "Quick aside." in texts
    assert "Quikc aside." not in texts


def test_two_different_manual_people_across_sessions_get_distinct_ids(sandbox):
    _seed_meeting("Mtg")
    review.insert_segment("Mtg", 6.5, 7.5, "name:Louise", "First aside.")
    import relabel
    relabel.relabel_one("Mtg")  # simulate time passing between namings

    review.insert_segment("Mtg", 8.0, 8.8, "name:Omar", "Second aside.")
    relabel.relabel_one("Mtg")

    data = json.loads((mfile("Mtg", ".json")).read_text())
    manual = {s["id"]: s["display"] for s in data["speakers"]
              if str(s["id"]).startswith("MANUAL_")}
    assert manual == {"MANUAL_1": "Louise", "MANUAL_2": "Omar"}


def test_relabel_recovers_mic_attribution_after_late_enrollment(sandbox, monkeypatch):
    """C6: a stereo recording first processed before the mic speaker was enrolled
    cached its ungated mic spans (mono_fallback_no_enroll). Enrolling the speaker
    and running relabel must now attribute the mic turns, no re-transcription."""
    import relabel
    from stt import channels, identify, refine

    base = "Early Call 05052026"
    words = [{"start": 0.5, "end": 0.9, "word": "hi"},      # SPEAKER_00
             {"start": 2.2, "end": 2.6, "word": "there"},   # SPEAKER_01
             {"start": 3.5, "end": 3.9, "word": "mine"},    # mic (3-5s, system quiet)
             {"start": 4.2, "end": 4.6, "word": "now"}]      # mic
    data = {"source_file": f"{base}.m4a", "duration_sec": 6.0, "strict": False,
            "speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 1"},
                         {"id": "SPEAKER_01", "name": None, "display": "Speaker 2"}],
            "segments": [], "words": words}
    mfile(base, ".json").write_text(json.dumps(data))
    mfile(base, ".txt").write_text("stub")

    rng = np.random.default_rng(5)
    raw_turns = [{"start": 0.0, "end": 2.0, "cluster": "SPEAKER_00"},
                 {"start": 2.0, "end": 3.0, "cluster": "SPEAKER_01"}]  # nothing at 3-5s
    cent_emb = {"SPEAKER_00": rng.normal(size=256), "SPEAKER_01": rng.normal(size=256)}
    diarcache.save(mfile(base, ".diar.npz"), raw_turns, [None, None], cent_emb,
                   mark_spans=[(3.0, 5.0)], mark_embs=[np.ones(256)],
                   channel_mode="mono_fallback_no_enroll", mic_speaker="Mark Paik")

    # the mic speaker is now enrolled; keep the SYSTEM path clean so the ONLY
    # Mark attribution comes from the recovered mic overlay
    monkeypatch.setattr(identify, "load_voiceprints", lambda: {"Mark Paik": np.ones((1, 256))})
    monkeypatch.setattr(identify, "name_speakers",
                        lambda emb, **k: {label: {"name": None} for label in emb})
    monkeypatch.setattr(identify, "score_against", lambda vec, samples: 0.99)
    monkeypatch.setattr(refine, "resolve_split_clusters", lambda cn, ce, vps: cn)
    monkeypatch.setattr(config, "REFINE", False)
    monkeypatch.setattr(config, "PUNCTUATE", False)

    assert relabel.relabel_one(base) is True
    after = json.loads(mfile(base, ".json").read_text())
    mark = [s for s in after["speakers"] if s["id"] == channels.MIC_ID]
    assert len(mark) == 1 and mark[0]["name"] == "Mark Paik"
    mark_segs = [seg for seg in after["segments"] if seg.get("name") == "Mark Paik"]
    assert mark_segs and "mine" in " ".join(s["text"] for s in mark_segs)


def test_one_time_speakers_flag_survives_relabel(sandbox, monkeypatch):
    """A meeting processed with 'one-time speakers' must NEVER register its
    unnamed voices globally — including on every later relabel, which
    otherwise re-runs unknown assignment and would quietly re-register the
    focus group. A normal meeting on the same relabel path still registers.
    (The minting floor is lowered here: this test is about the one-time flag,
    and the seed meeting's clusters are deliberately tiny.)"""
    import relabel

    from stt import unknowns

    monkeypatch.setattr(config, "UNKNOWN_MIN_TALK_SECS", 0.0)
    monkeypatch.setattr(config, "UNKNOWN_MIN_RELIABLE_TURNS", 0)
    _seed_meeting("Focus Group")
    d = json.loads(mfile("Focus Group", ".json").read_text())
    d["one_time_speakers"] = True  # as stamped by pipeline --one-time-speakers
    mfile("Focus Group", ".json").write_text(json.dumps(d))

    assert relabel.relabel_one("Focus Group")
    assert unknowns.load()["speakers"] == {}  # nothing registered

    _seed_meeting("Normal Mtg")
    assert relabel.relabel_one("Normal Mtg")
    assert len(unknowns.load()["speakers"]) == 2  # control: normal path registers


def test_known_voice_attributed_end_to_end_from_cache(sandbox, monkeypatch):
    """End-to-end known-voice attribution: an enrolled person's cluster comes
    out of a relabel NAMED (real match score, txt header, segments), while a
    thin stranger cluster keeps a transcript-LOCAL 'Voice N' — disjoint from
    the global unknowns' 'Speaker N' space — and mints nothing."""
    import relabel
    from stt import identify, unknowns

    monkeypatch.setattr(config, "PUNCTUATE", False)
    base = "Known Voice Mtg"
    rng = np.random.default_rng(17)
    alice = rng.normal(size=256)
    alice /= np.linalg.norm(alice)
    stranger = rng.normal(size=256)
    stranger -= (stranger @ alice) * alice
    stranger /= np.linalg.norm(stranger)

    words = ([{"start": 3.0 * i + 0.2, "end": 3.0 * i + 0.6, "word": f"w{i}"}
              for i in range(12)]
             + [{"start": 40.0, "end": 40.4, "word": "hm"}])
    data = {"source_file": f"{base}.m4a", "duration_sec": 45.0, "strict": False,
            "speakers": [], "segments": [], "words": words}
    mfile(base, ".json").write_text(json.dumps(data))
    mfile(base, ".txt").write_text("stub")
    # Alice: 12 reliable turns, 36s; the stranger: one 2s turn (under the floor)
    raw_turns = ([{"start": 3.0 * i, "end": 3.0 * i + 3.0, "cluster": "SPEAKER_00"}
                  for i in range(12)]
                 + [{"start": 39.0, "end": 41.0, "cluster": "SPEAKER_01"}])
    tembs = [alice] * 12 + [None]
    diarcache.save(mfile(base, ".diar.npz"), raw_turns, tembs,
                   {"SPEAKER_00": alice, "SPEAKER_01": stranger})

    identify.enroll("Alice Chen", alice, source="Elsewhere")
    assert relabel.relabel_one(base) is True

    after = json.loads(mfile(base, ".json").read_text())
    by_id = {s["id"]: s for s in after["speakers"]}
    assert by_id["Alice Chen"]["name"] == "Alice Chen"
    # the thin stranger: transcript-local display, no global registration
    assert by_id["SPEAKER_01"]["name"] is None
    assert by_id["SPEAKER_01"]["global_id"] is None
    assert by_id["SPEAKER_01"]["display"] == "Voice 2"
    assert unknowns.load()["speakers"] == {}
    txt = mfile(base, ".txt").read_text()
    assert "identified: Alice Chen" in txt
    assert "Alice Chen:" in txt and "Voice 2:" in txt
    assert "Speaker 2" not in txt      # the global-unknown namespace stays clear


def test_relabel_all_takes_the_relabel_lock(sandbox, monkeypatch):
    """run_batch's end-of-run relabel_all() used to interleave with a
    GUI-spawned `relabel --all` (which holds relabel.lock), doubling assign
    churn. relabel_all must now wait for the lock before touching anything."""
    import fcntl
    import threading
    import time

    import relabel

    monkeypatch.setattr(config, "PUNCTUATE", False)
    _seed_meeting("Mtg")
    touched = []
    real_one = relabel.relabel_one

    def tracking_one(base, **kw):
        touched.append(base)
        return real_one(base, **kw)

    monkeypatch.setattr(relabel, "relabel_one", tracking_one)

    holder = open(config.PROJECT_DIR / "relabel.lock", "w")
    fcntl.flock(holder, fcntl.LOCK_EX)         # a GUI pass is mid-flight
    done = threading.Event()

    t = threading.Thread(target=lambda: (relabel.relabel_all(), done.set()))
    t.start()
    time.sleep(0.4)
    assert not done.is_set(), "relabel_all ran while another relabel held the lock"
    assert touched == []

    fcntl.flock(holder, fcntl.LOCK_UN)         # the GUI pass finishes
    t.join(timeout=5)
    assert done.is_set()
    assert touched == ["Mtg"]


def test_naming_queued_during_a_pass_survives_it_and_both_names_land(sandbox, monkeypatch):
    """Two rapid namings: the first spawns relabel pass A; the second lands
    while A is still running, so its own relabel hits A's lock and queues the
    follow-up flag. A must consume only the flag present when it STARTED —
    it used to clear the flag at exit, silently cancelling the promised
    follow-up, and the second name never applied to any transcript. The
    kicked follow-up pass then lands the second name."""
    import sys as _sys

    import relabel
    from stt import identify

    _seed_meeting("Mtg")
    rng = np.random.default_rng(3)                      # _seed_meeting's vectors
    va, vb = rng.normal(size=256), rng.normal(size=256)
    monkeypatch.setattr(config, "PUNCTUATE", False)

    identify.enroll("Alice", va, source="Mtg")          # naming #1 -> pass A spawns

    real_one = relabel.relabel_one
    flag = config.PROJECT_DIR / relabel.PENDING_FLAG_NAME

    def mid_pass(base, **kw):
        ok = real_one(base, **kw)
        # naming #2 lands while pass A is still running: its spawned relabel
        # fails A's lock and queues the follow-up
        identify.enroll("Bob", vb, source="Mtg")
        flag.write_text("all")
        return ok

    monkeypatch.setattr(relabel, "relabel_one", mid_pass)
    monkeypatch.setattr(_sys, "argv", ["relabel.py", "--all"])
    assert relabel.main() == 0

    assert flag.exists()                                # the queued follow-up SURVIVED pass A
    names = {s["name"] for s in
             json.loads(mfile("Mtg", ".json").read_text())["speakers"]}
    assert "Alice" in names                             # name #1 landed in pass A
    assert "Bob" not in names                           # name #2 correctly still pending

    # the follow-up pass the panel kicks once the lock frees
    monkeypatch.setattr(relabel, "relabel_one", real_one)
    assert relabel.main() == 0
    assert not flag.exists()                            # consumed by the pass that covers it
    names = {s["name"] for s in
             json.loads(mfile("Mtg", ".json").read_text())["speakers"]}
    assert {"Alice", "Bob"} <= names
