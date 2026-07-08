"""verify: cross-engine disagreement detection — alignment, filler tolerance,
region merging, segment flagging, and the sidecar that survives relabels."""
import json

from stt import config, verify
from conftest import mfile


def _w(items):
    """[(start, end, word)] -> word dicts."""
    return [{"start": s, "end": e, "word": w} for s, e, w in items]


PRIMARY = _w([(0.0, 0.4, "We"), (0.4, 0.8, "reviewed"), (0.8, 1.2, "the"),
              (1.2, 1.8, "dashboard"), (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])


def test_agreeing_streams_produce_no_regions():
    assert verify.regions(PRIMARY, PRIMARY) == []


def test_substitution_region_with_correct_span():
    secondary = _w([(0.0, 0.4, "We"), (0.4, 0.8, "renewed"), (0.8, 1.2, "the"),
                    (1.2, 1.8, "dashboard"), (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    regs = verify.regions(PRIMARY, secondary)
    assert len(regs) == 1
    r = regs[0]
    assert r["ours"] == "reviewed" and r["theirs"] == "renewed"
    assert r["start"] == 0.4 and r["end"] == 0.8  # primary word timing


def test_filler_only_difference_is_ignored():
    secondary = _w([(0.0, 0.4, "We"), (0.35, 0.4, "um"), (0.4, 0.8, "reviewed"),
                    (0.8, 1.2, "the"), (1.2, 1.8, "dashboard"),
                    (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    assert verify.regions(PRIMARY, secondary) == []


def test_insertion_anchors_between_neighbors():
    """Second engine heard an extra word the primary missed entirely."""
    secondary = _w([(0.0, 0.4, "We"), (0.4, 0.8, "reviewed"), (0.8, 1.2, "the"),
                    (1.2, 1.8, "dashboard"), (2.0, 2.4, "carefully"),
                    (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    regs = verify.regions(PRIMARY, secondary)
    assert len(regs) == 1
    r = regs[0]
    assert r["theirs"] == "carefully" and r["ours"] == ""
    assert 1.8 <= r["start"] <= r["end"] <= 5.0  # sits in the gap


def test_adjacent_regions_merge():
    secondary = _w([(0.0, 0.4, "He"), (0.4, 0.8, "renewed"), (0.8, 1.2, "the"),
                    (1.2, 1.8, "dashboard"), (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    regs = verify.regions(PRIMARY, secondary)
    assert len(regs) == 1  # "he/we" + "renewed/reviewed" 0s apart -> one item
    assert "we" in regs[0]["ours"] and "reviewed" in regs[0]["ours"]


def test_apply_flags_attaches_alt_and_skips_reviewed():
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "We reviewed the dashboard", "flags": []},
        {"start": 5.0, "end": 6.0, "speaker": "SPEAKER_01", "text": "numbers today.", "flags": [],
         "reviewed": "accepted"},
    ]
    regs = [{"start": 0.4, "end": 0.8, "ours": "reviewed", "theirs": "renewed"},
            {"start": 5.1, "end": 5.3, "ours": "numbers", "theirs": "number"}]
    n = verify.apply_flags(segments, regs)
    assert n == 1
    assert verify.FLAG in segments[0]["flags"]
    assert segments[0]["alt"][0]["theirs"] == "renewed"
    assert "alt" not in segments[1] and segments[1]["flags"] == []  # reviewed: untouched


def test_apply_flags_catches_zero_width_pure_insertion_region():
    """A zero-width disagreement region (a pure insertion with no inter-word
    gap, start==end) sitting squarely inside a segment computes overlap of
    EXACTLY 0 against it — a strict > 0 test let it escape flagging
    entirely, defeating the "flag every disagreement" guarantee."""
    segments = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00",
                "text": "We reviewed the dashboard", "flags": []}]
    regs = [{"start": 1.0, "end": 1.0, "ours": "", "theirs": "carefully"}]
    n = verify.apply_flags(segments, regs)
    assert n == 1
    assert verify.FLAG in segments[0]["flags"]
    assert segments[0]["alt"][0]["theirs"] == "carefully"


def test_apply_flags_still_excludes_regions_genuinely_outside_a_segment():
    """The >= 0 fix must not make apply_flags match everything — a region
    entirely outside a segment's span still correctly computes a NEGATIVE
    overlap and is excluded."""
    segments = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00",
                "text": "We reviewed the dashboard", "flags": []}]
    regs = [{"start": 10.0, "end": 10.0, "ours": "", "theirs": "unrelated"}]
    n = verify.apply_flags(segments, regs)
    assert n == 0
    assert segments[0]["flags"] == []


def test_secondary_engine_is_architecturally_different():
    assert verify.secondary_engine("mlx-community/parakeet-tdt-0.6b-v2") == ("mlxwhisper", "turbo")
    assert verify.secondary_engine("mlx-whisper/large-v3") == ("parakeet", None)


def test_sidecar_roundtrip(sandbox):
    regs = [{"start": 1.0, "end": 2.0, "ours": "a", "theirs": "b"}]
    verify.save_sidecar("Mtg", regs, "mlx-whisper/turbo")
    got = verify.load_sidecar("Mtg")
    assert got["engine"] == "mlx-whisper/turbo" and got["regions"] == regs
    assert verify.load_sidecar("Nothing") is None
    # a corrupt sidecar degrades to None, never a crash
    mfile("Bad", ".verify.json").write_text("{nope")
    assert verify.load_sidecar("Bad") is None


def test_verify_flag_is_reviewable(sandbox):
    """The flag round-trips through the review workflow like any other."""
    from stt import review
    segments = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "name": "Mark",
                 "display": "Mark", "text": "We reviewed the dashboard",
                 "flags": [verify.FLAG], "overlap": False,
                 "alt": [{"start": 0.4, "end": 0.8, "ours": "reviewed", "theirs": "renewed"}]}]
    data = {"source_file": "Mtg.m4a", "duration_sec": 2.0, "strict": False,
            "speakers": [{"id": "SPEAKER_00", "name": "Mark", "display": "Mark"}],
            "segments": segments, "words": []}
    (mfile("Mtg", ".json")).write_text(json.dumps(data))
    (mfile("Mtg", ".txt")).write_text("stub")

    out = review.list_flagged("Mtg")
    assert out["items"][0]["alt"][0]["theirs"] == "renewed"
    r = review.apply("Mtg", 0, "edit", start=0.0, text="We renewed the dashboard")
    assert r["ok"]
    d = json.loads((mfile("Mtg", ".json")).read_text())
    assert d["segments"][0]["flags"] == [] and "alt" not in d["segments"][0]


def test_failed_secondary_engine_flags_nothing(monkeypatch):
    """A second engine that returns almost nothing has failed — flagging the
    whole transcript as 'disagreement' would be noise. Drive verify.run()
    itself so the 30%-floor guard is exercised, not just the region math it
    protects: a Parakeet primary routes the second opinion through mlxwhisper,
    which we stub to return a single word (1 << 0.3*6)."""
    from stt import asr_mlxwhisper
    sec = [{"start": 0.0, "end": 0.4, "word": "ok"}]
    monkeypatch.setattr(asr_mlxwhisper, "transcribe",
                        lambda wav, progress=None: {"words": sec,
                                                    "engine": "mlx-whisper/turbo"})
    regs, engine = verify.run("dummy.wav", PRIMARY,
                              "mlx-community/parakeet-tdt-0.6b-v2")
    # below the floor: run() returns NO regions even though the raw math on the
    # same inputs would emit a giant spurious deletion — that gap is the guard.
    assert regs == [] and engine == "mlx-whisper/turbo"
    assert verify.regions(PRIMARY, sec) != []


def test_working_secondary_engine_flags_real_disagreement(monkeypatch):
    """The other side of the floor: a second engine that returns ENOUGH words
    (>= 30% of primary) which genuinely disagree must yield real regions — so a
    removed or inverted floor comparison fails one of these two tests."""
    from stt import asr_mlxwhisper
    sec = _w([(0.0, 0.4, "We"), (0.4, 0.8, "renewed"), (0.8, 1.2, "the"),
              (1.2, 1.8, "dashboard")])  # 4 words >= 0.3*6; "renewed" disagrees
    monkeypatch.setattr(asr_mlxwhisper, "transcribe",
                        lambda wav, progress=None: {"words": sec,
                                                    "engine": "mlx-whisper/turbo"})
    regs, engine = verify.run("dummy.wav", PRIMARY,
                              "mlx-community/parakeet-tdt-0.6b-v2")
    assert engine == "mlx-whisper/turbo"
    assert any(r["theirs"] == "renewed" for r in regs)
