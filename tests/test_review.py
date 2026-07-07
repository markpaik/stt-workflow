"""review: the accept/edit workflow against real files, plus voice-clip lookup
including the named-mid-batch fallback (transcripts not yet relabeled)."""
import json
import threading
import time

import numpy as np

from stt import config, diarcache, identify, review


def _make_meeting(sandbox, base="Mtg", flag_seg=1):
    speakers = [
        {"id": "SPEAKER_00", "name": "Mark", "display": "Mark", "match_score": 0.9},
        {"id": "SPEAKER_01", "name": None, "display": "Speaker 2", "match_score": None},
    ]
    segments = [
        {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "name": "Mark",
         "display": "Mark", "text": "Clean opening turn.", "flags": [], "overlap": False},
        {"start": 5.0, "end": 6.0, "speaker": "SPEAKER_01", "name": None,
         "display": "Speaker 2", "text": "Uncertain bit here.",
         "flags": ["overlap"], "overlap": True},
        {"start": 6.0, "end": 9.0, "speaker": "SPEAKER_00", "name": "Mark",
         "display": "Mark", "text": "Clean closing turn.", "flags": [], "overlap": False},
    ]
    words = ([{"start": 0.5 * i, "end": 0.5 * i + 0.4, "word": f"w{i}", "speaker": "SPEAKER_00"}
              for i in range(10)]
             + [{"start": 5.2, "end": 5.8, "word": "uncertain", "speaker": "SPEAKER_01"}])
    data = {"source_file": f"{base}.m4a", "duration_sec": 9.0, "strict": False,
            "speakers": speakers, "segments": segments, "words": words}
    (config.MEETINGS_DIR / f"{base}.json").write_text(json.dumps(data))
    (config.MEETINGS_DIR / f"{base}.txt").write_text("stub")
    return data


def test_list_flagged_only_unreviewed(sandbox):
    _make_meeting(sandbox)
    out = review.list_flagged("Mtg")
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["index"] == 1 and it["flags"] == ["overlap"]
    assert it["prev"].endswith("opening turn.") and it["next"].startswith("Clean closing")
    assert {s["id"] for s in out["speakers"]} == {"SPEAKER_00", "SPEAKER_01"}


def test_accept_clears_flag_and_persists(sandbox):
    _make_meeting(sandbox)
    r = review.apply("Mtg", 1, "accept", start=5.0)
    assert r == {"ok": True, "remaining": 0}
    d = json.loads((config.MEETINGS_DIR / "Mtg.json").read_text())
    seg = d["segments"][1]
    assert seg["flags"] == [] and seg["reviewed"] == "accepted"
    assert "Uncertain bit here." in (config.MEETINGS_DIR / "Mtg.txt").read_text()
    assert "[*]" not in (config.MEETINGS_DIR / "Mtg.txt").read_text()
    # nothing left to review
    assert review.list_flagged("Mtg")["items"] == []


def test_edit_text_and_reassign_speaker(sandbox):
    _make_meeting(sandbox)
    r = review.apply("Mtg", 1, "edit", start=5.0,
                     text="Corrected words.", speaker_id="SPEAKER_00")
    assert r["ok"]
    d = json.loads((config.MEETINGS_DIR / "Mtg.json").read_text())
    seg = d["segments"][1]
    assert seg["text"] == "Corrected words." and seg["text_edited"]
    assert seg["speaker"] == "SPEAKER_00" and seg["name"] == "Mark"
    # the word inside the segment follows the human's speaker call
    w = [w for w in d["words"] if w["word"] == "uncertain"][0]
    assert w["speaker"] == "SPEAKER_00"
    assert "Mark: Corrected words." in (config.MEETINGS_DIR / "Mtg.txt").read_text()


def test_stale_review_rejected(sandbox):
    _make_meeting(sandbox)
    r = review.apply("Mtg", 1, "edit", start=99.0, text="x")
    assert not r["ok"] and "changed" in r["error"]


def test_apply_recovers_from_small_relabel_nudge_at_same_index(sandbox):
    """A relabel re-running diarization can nudge a segment's start by more
    than the strict 0.25s tolerance without changing which segment it is —
    the identity fallback should still accept the edit instead of forcing a
    reopen for a boundary tweak that didn't restructure anything."""
    _make_meeting(sandbox)
    jpath = config.MEETINGS_DIR / "Mtg.json"
    d = json.loads(jpath.read_text())
    d["segments"][1]["start"] = 5.4  # nudged by 0.4s: past STRICT, within WIDE
    jpath.write_text(json.dumps(d))
    r = review.apply("Mtg", 1, "edit", start=5.0, text="Recovered text.")
    assert r["ok"]
    assert json.loads(jpath.read_text())["segments"][1]["text"] == "Recovered text."


def test_apply_recovers_by_start_time_when_relabel_shifts_index(sandbox):
    """If a relabel restructures the segment list (e.g. inserts a new segment
    earlier), a stale index can point at the WRONG segment entirely. The
    fallback must recover the intended segment by start-time proximity
    across the whole list rather than silently editing whatever now sits at
    the old index."""
    _make_meeting(sandbox)
    jpath = config.MEETINGS_DIR / "Mtg.json"
    d = json.loads(jpath.read_text())
    new_seg = {"start": 0.0, "end": 0.3, "speaker": "SPEAKER_00", "name": "Mark",
               "display": "Mark", "text": "Uh,", "flags": [], "overlap": False}
    d["segments"].insert(0, new_seg)  # shifts every later index up by one
    jpath.write_text(json.dumps(d))
    # client still thinks the flagged segment is at index 1 with start=5.0 —
    # index 1 now really holds the old opening turn (start=0.0)
    r = review.apply("Mtg", 1, "edit", start=5.0, text="Recovered.")
    assert r["ok"]
    after = json.loads(jpath.read_text())["segments"]
    assert after[2]["text"] == "Recovered." and after[2]["speaker"] == "SPEAKER_01"
    assert after[1]["text"] == "Clean opening turn."  # untouched


def test_bad_index_and_speaker(sandbox):
    _make_meeting(sandbox)
    assert not review.apply("Mtg", 42, "accept")["ok"]
    assert not review.apply("Mtg", 1, "edit", start=5.0, speaker_id="SPEAKER_99")["ok"]


def test_find_voice_clip_by_transcript(sandbox):
    _make_meeting(sandbox)
    base, start, dur = review.find_voice_clip("Mark")
    assert base == "Mtg" and start == 0.0 and dur >= 2.0
    # by anonymous display too
    assert review.find_voice_clip("Speaker 2")[1] == 5.0


def test_find_voice_clip_voiceprint_fallback(sandbox):
    """Named mid-batch: transcript does NOT contain the new name yet, but the
    voiceprint + diar cache locate the right cluster anyway."""
    _make_meeting(sandbox)
    # transcript knows this cluster only as Speaker 2 (name=None)
    v = np.random.default_rng(7).normal(size=256)
    diarcache.save(config.MEETINGS_DIR / "Mtg.diar.npz",
                   [{"start": 5.0, "end": 6.0, "cluster": "SPEAKER_01"}],
                   [v], {"SPEAKER_00": np.random.default_rng(8).normal(size=256),
                         "SPEAKER_01": v})
    identify.enroll("Jane", v, source="Mtg")
    hit = review.find_voice_clip("Jane")
    assert hit is not None
    base, start, _ = hit
    assert base == "Mtg" and start == 5.0  # found HER cluster's segment


def test_find_voice_clip_voiceprint_below_threshold_returns_none(sandbox):
    """A voiceprint that doesn't clear the open-set threshold+margin bar must
    return None outright — not crash on a stale/undefined `label` left over
    from a failed match, and not bleed some other cluster's segment into the
    result just because it was the last one scored."""
    _make_meeting(sandbox)
    centroid_a = np.random.default_rng(1).normal(size=256)
    centroid_b = np.random.default_rng(2).normal(size=256)
    diarcache.save(config.MEETINGS_DIR / "Mtg.diar.npz",
                   [{"start": 5.0, "end": 6.0, "cluster": "SPEAKER_01"}],
                   [centroid_b], {"SPEAKER_00": centroid_a, "SPEAKER_01": centroid_b})
    unrelated = np.random.default_rng(99).normal(size=256)
    identify.enroll("Jane", unrelated, source="Mtg")
    assert review.find_voice_clip("Jane") is None


def test_find_voice_clip_unknown_key(sandbox):
    _make_meeting(sandbox)
    assert review.find_voice_clip("Nobody Realname") is None


def test_review_decisions_survive_relabel(sandbox):
    """relabel rebuilds segments from the diar cache; recorded review decisions
    must be re-applied so human work is never lost."""
    _make_meeting(sandbox)
    review.apply("Mtg", 1, "edit", start=5.0, text="Human fixed this.",
                 speaker_id="SPEAKER_00")

    # simulate a relabel: rebuild data fresh (flags back, edit gone)
    data = _make_meeting(sandbox)
    assert data["segments"][1]["flags"] == ["overlap"]

    n = review.reapply_decisions("Mtg", data)
    assert n == 1
    seg = data["segments"][1]
    assert seg["text"] == "Human fixed this." and seg["speaker"] == "SPEAKER_00"
    assert seg["flags"] == [] and seg["reviewed"] == "edited"


def test_accept_decision_survives_relabel(sandbox):
    _make_meeting(sandbox)
    review.apply("Mtg", 1, "accept", start=5.0)
    data = _make_meeting(sandbox)
    assert review.reapply_decisions("Mtg", data) == 1
    assert data["segments"][1]["reviewed"] == "accepted"
    assert data["segments"][1]["flags"] == []


def test_reassign_to_person_not_in_meeting(sandbox):
    """Crosstalk misattribution: the real speaker was never diarized. 'name:'
    creates a manual speaker entry, updates words, and shows in the .txt."""
    _make_meeting(sandbox)
    r = review.apply("Mtg", 1, "edit", start=5.0, speaker_id="name:Louise")
    assert r["ok"]
    d = json.loads((config.MEETINGS_DIR / "Mtg.json").read_text())
    seg = d["segments"][1]
    assert seg["speaker"] == "MANUAL_1" and seg["display"] == "Louise"
    assert any(s["id"] == "MANUAL_1" and s["manual"] for s in d["speakers"])
    w = [w for w in d["words"] if w["word"] == "uncertain"][0]
    assert w["speaker"] == "MANUAL_1"
    assert "Louise: Uncertain bit here." in (config.MEETINGS_DIR / "Mtg.txt").read_text()
    # same name again resolves to the SAME entry, not MANUAL_2
    review.apply("Mtg", 0, "edit", start=0.0, speaker_id="name:Louise")
    d = json.loads((config.MEETINGS_DIR / "Mtg.json").read_text())
    assert sum(1 for s in d["speakers"] if str(s["id"]).startswith("MANUAL_")) == 1


def test_insert_and_delete_line(sandbox):
    _make_meeting(sandbox)
    r = review.insert_segment("Mtg", 5.0, 6.0, "name:Omar", "Quick interjection.")
    assert r["ok"] and r["index"] == 2  # after the 5.0s segment (ties sort stable)
    d = json.loads((config.MEETINGS_DIR / "Mtg.json").read_text())
    seg = d["segments"][r["index"]]
    assert seg["inserted"] and seg["display"] == "Omar" and seg["attribution"] == "manual"
    assert [s["start"] for s in d["segments"]] == sorted(s["start"] for s in d["segments"])
    assert "Omar: Quick interjection." in (config.MEETINGS_DIR / "Mtg.txt").read_text()
    # guards
    assert not review.insert_segment("Mtg", 1.0, 2.0, "name:X", "  ")["ok"]
    assert not review.insert_segment("Mtg", 1.0, 2.0, "SPEAKER_99", "hi")["ok"]

    # delete the flagged line: words detach, decision recorded
    r = review.delete_segment("Mtg", 1, start=5.0)
    assert r["ok"]
    d = json.loads((config.MEETINGS_DIR / "Mtg.json").read_text())
    assert all(s["text"] != "Uncertain bit here." for s in d["segments"])
    w = [w for w in d["words"] if w["word"] == "uncertain"][0]
    assert w["speaker"] is None
    assert not review.delete_segment("Mtg", 0, start=99.0)["ok"]  # stale guard


def test_insert_delete_and_manual_speaker_survive_relabel(sandbox):
    """All three new decision types re-apply onto rebuilt segments."""
    _make_meeting(sandbox)
    review.apply("Mtg", 1, "edit", start=5.0, speaker_id="name:Louise")
    review.insert_segment("Mtg", 6.5, 7.5, "name:Omar", "Missed line.")
    review.delete_segment("Mtg", 0, start=0.0)

    data = _make_meeting(sandbox)  # simulate relabel rebuild
    n = review.reapply_decisions("Mtg", data)
    assert n == 3
    texts = [s["text"] for s in data["segments"]]
    assert "Clean opening turn." not in texts           # delete re-applied
    assert "Missed line." in texts                       # insert re-applied
    ins = next(s for s in data["segments"] if s.get("inserted"))
    assert ins["display"] == "Omar"
    seg = next(s for s in data["segments"] if s["text"] == "Uncertain bit here.")
    assert seg["display"] == "Louise"                    # manual person recreated
    assert any(str(s["id"]).startswith("MANUAL_") for s in data["speakers"])
    assert [s["start"] for s in data["segments"]] == sorted(s["start"] for s in data["segments"])


def test_redo_archives_decisions(sandbox):
    """A redo re-diarizes: old cluster ids are meaningless, so decisions are
    archived (renamed .superseded), never half-applied and never deleted."""
    _make_meeting(sandbox)
    review.apply("Mtg", 1, "edit", start=5.0, text="Human work.")
    assert review.count_decisions("Mtg") == 1
    assert review.archive_decisions("Mtg") is True
    assert review.count_decisions("Mtg") == 0
    assert (config.MEETINGS_DIR / "Mtg.reviews.superseded.json").exists()
    # rebuilt data gets nothing reapplied — clean slate
    data = _make_meeting(sandbox)
    assert review.reapply_decisions("Mtg", data) == 0
    assert review.archive_decisions("Mtg") is False  # nothing left to archive


def test_retranscribe_engine_selection(sandbox):
    from stt import retranscribe
    import stt.asr_parakeet, stt.asr_mlxwhisper, os
    assert retranscribe._asr_module("parakeet") is stt.asr_parakeet
    assert retranscribe._asr_module("mlxwhisper:turbo") is stt.asr_mlxwhisper
    assert os.environ["STT_WHISPER_MLX_MODEL"] == "turbo"
    assert retranscribe._asr_module("mlxwhisper:large-v3") is stt.asr_mlxwhisper
    assert os.environ["STT_WHISPER_MLX_MODEL"] == "large-v3"
    # unknown engine -> clean error dict, not a crash
    assert "unknown engine" in retranscribe.retranscribe("x", 0, 1, "bogus")["error"]


def test_minor_triage_and_bulk_accept(sandbox):
    """Substantial items sort first; sub-second crumbs bulk-accept in one call
    and stay accepted across relabels."""
    data = _make_meeting(sandbox)
    # add a minor crumb ("so", 0.4s) alongside the substantial flagged segment
    data["segments"].append({"start": 9.0, "end": 9.4, "speaker": "SPEAKER_01",
                             "name": None, "display": "Speaker 2", "text": "so",
                             "flags": ["id_mismatch"], "overlap": False})
    import json as _json
    from stt import config as _config
    (_config.MEETINGS_DIR / "Mtg.json").write_text(_json.dumps(data))

    out = review.list_flagged("Mtg")
    assert [it["minor"] for it in out["items"]] == [False, True]  # major first
    assert out["n_minor"] == 1

    r = review.accept_minor("Mtg")
    assert r == {"ok": True, "accepted": 1, "remaining": 1}  # major one remains
    # decision persisted -> survives a rebuild
    rebuilt = _make_meeting(sandbox)
    rebuilt["segments"].append({"start": 9.0, "end": 9.4, "speaker": "SPEAKER_01",
                                "name": None, "display": "Speaker 2", "text": "so",
                                "flags": ["id_mismatch"], "overlap": False})
    assert review.reapply_decisions("Mtg", rebuilt) == 1
    assert rebuilt["segments"][-1]["reviewed"] == "accepted"


def test_insert_negative_time_clamped(sandbox):
    _make_meeting(sandbox)
    r = review.insert_segment("Mtg", -3.0, -2.0, "name:X", "early words")
    assert r["ok"]
    d = json.loads((config.MEETINGS_DIR / "Mtg.json").read_text())
    seg = d["segments"][r["index"]]
    assert seg["start"] == 0.0 and seg["end"] > seg["start"]


def test_lock_meeting_serializes_same_meeting(sandbox):
    """The P0-3 fix: a GUI edit and a relabel writing the SAME meeting must
    never run their read-modify-write concurrently. Proven with real OS-level
    flock contention (a blocking acquire on another thread), not a mock."""
    events = []

    def holder():
        with review.lock_meeting("Mtg"):
            events.append(("holder_in", time.monotonic()))
            time.sleep(0.3)
            events.append(("holder_out", time.monotonic()))

    t = threading.Thread(target=holder)
    t.start()
    time.sleep(0.05)  # let the holder acquire first

    with review.lock_meeting("Mtg"):
        events.append(("waiter_in", time.monotonic()))
    t.join()

    order = [e[0] for e in events]
    assert order == ["holder_in", "holder_out", "waiter_in"], order


def test_lock_meeting_does_not_block_a_different_meeting(sandbox):
    """Per-meeting, not global: editing 'Mtg' must never block 'Other'."""
    holder_in = threading.Event()
    release = threading.Event()

    def holder():
        with review.lock_meeting("Mtg"):
            holder_in.set()
            release.wait(timeout=2)

    t = threading.Thread(target=holder)
    t.start()
    assert holder_in.wait(timeout=2)

    start = time.monotonic()
    with review.lock_meeting("Other"):
        elapsed = time.monotonic() - start
    assert elapsed < 0.2  # did not wait for Mtg's lock at all

    release.set()
    t.join()


def test_relabel_blocks_on_a_concurrent_gui_edit_of_the_same_meeting(sandbox):
    """End-to-end: hold the SAME lock relabel_one() uses (simulating a GUI
    request mid-write) and confirm relabel_one() genuinely waits for it
    rather than racing it. relabel_one()'s own work has real (non-mocked)
    compute in it — a plain "did relabel finish after the holder released"
    ordering check can pass by coincidence if that compute alone takes longer
    than the hold, so this asserts a hard minimum elapsed time instead: with
    the lock actually enforced, relabel_one cannot return before HOLD_SEC has
    passed, no matter how fast (or slow) its own work is."""
    from test_relabel import _seed_meeting
    import relabel

    _seed_meeting("Mtg")
    HOLD_SEC = 1.5  # comfortably longer than relabel_one's own ~0.8s baseline

    def gui_holder(release):
        with review.lock_meeting("Mtg"):
            release.wait(HOLD_SEC)

    release = threading.Event()
    t = threading.Thread(target=gui_holder, args=(release,))
    t.start()
    time.sleep(0.05)  # let the holder acquire first

    start = time.monotonic()
    assert relabel.relabel_one("Mtg") is True
    elapsed = time.monotonic() - start
    release.set()
    t.join()

    assert elapsed >= HOLD_SEC - 0.1, (
        f"relabel_one returned after {elapsed:.2f}s while the same meeting's "
        f"lock was held for {HOLD_SEC}s — it did not wait for it")
