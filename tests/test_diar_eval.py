"""tools/diar_eval.py: the ground-truth extractors, the channel-energy
labeler (conservative margin + abstain band), the scoring math, and the
scoreboard/baseline flow — all against the conftest sandbox, so no test can
ever read or write a real meeting or voiceprint."""
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import diar_eval  # noqa: E402

from conftest import mfile  # noqa: E402
from stt import config, diarcache, identify  # noqa: E402


# ----------------------------------------------------------- band helper ----

def test_band_edges():
    assert diar_eval.band_label(0.05) == "<0.3s"
    assert diar_eval.band_label(0.29) == "<0.3s"
    assert diar_eval.band_label(0.3) == "0.3-0.6s"
    assert diar_eval.band_label(0.59) == "0.3-0.6s"
    assert diar_eval.band_label(0.6) == "0.6-1.0s"
    assert diar_eval.band_label(1.0) == "1.0-1.5s"
    assert diar_eval.band_label(1.5) == "1.5-2.5s"
    assert diar_eval.band_label(2.5) == ">=2.5s"
    assert diar_eval.band_label(60.0) == ">=2.5s"


# ------------------------------------------------------ spec -> name ---------

def test_spec_resolution_all_shapes():
    by_id = {"Mark Paik": {"id": "Mark Paik", "name": "Mark Paik"},
             "SPEAKER_01": {"id": "SPEAKER_01", "name": None}}
    # the panel's new-person form
    assert diar_eval._spec_to_name("name:Alice Fake", by_id) == "Alice Fake"
    # a named cluster's id IS the person's name in this codebase
    assert diar_eval._spec_to_name("Mark Paik", by_id) == "Mark Paik"
    # an unnamed cluster resolves to nobody (anonymous is not a name claim)
    assert diar_eval._spec_to_name("SPEAKER_01", by_id) is None
    # a superseded sidecar (no roster): cluster ids died with the old
    # diarization, but name-shaped specs still resolve
    assert diar_eval._spec_to_name("SPEAKER_03", None) is None
    assert diar_eval._spec_to_name("MANUAL_2", None) is None
    assert diar_eval._spec_to_name("U007", None) is None
    assert diar_eval._spec_to_name("Briana Kelly", None) == "Briana Kelly"
    assert diar_eval._spec_to_name("", by_id) is None
    assert diar_eval._spec_to_name("name:", by_id) is None


# ------------------------------------------------------------- extractor ----

def _seed_reviewed_meeting(base="Mtg"):
    """A meeting with BOTH sidecars: live decisions (edits with speaker,
    text-only edit, accept, delete, split) and a superseded sidecar (plain
    names + a dead cluster id)."""
    words = [{"start": 0.5 * i, "end": 0.5 * i + 0.4, "word": f"w{i}"}
             for i in range(60)]
    data = {"source_file": f"{base}.m4a", "duration_sec": 30.0, "strict": False,
            "speakers": [{"id": "Alice Fake", "name": "Alice Fake",
                          "display": "Alice Fake"},
                         {"id": "SPEAKER_01", "name": None,
                          "display": "Speaker 2"}],
            "segments": [], "words": words}
    mfile(base, ".json").write_text(json.dumps(data))
    decisions = [
        # speaker changed -> labeled turn (id-of-a-named-cluster form)
        {"start": 1.0, "end": 2.5, "action": "edit", "text": None,
         "speaker_id": "Alice Fake", "at": "t"},
        # speaker changed -> labeled turn (new-person form)
        {"start": 5.0, "end": 5.4, "action": "edit", "text": None,
         "speaker_id": "name:Bob Fake", "at": "t"},
        # text-only edit: not a speaker label
        {"start": 8.0, "end": 9.0, "action": "edit", "text": "fixed words",
         "speaker_id": None, "at": "t"},
        # accept / delete: counted, never labeled turns
        {"start": 10.0, "end": 10.5, "action": "accept", "text": None,
         "speaker_id": None, "at": "t"},
        {"start": 11.0, "end": 11.5, "action": "delete", "text": None,
         "speaker_id": None, "at": "t"},
        # split: half A explicitly reassigned, half B always a claim
        {"start": 12.0, "end": 14.0, "cut": 13.0, "action": "split",
         "text": "a", "text_b": "b", "speaker_id": "name:Carol Fake",
         "speaker_b": "Alice Fake", "at": "t"},
        # reassignment to an UNNAMED cluster: honest but unscorable
        {"start": 15.0, "end": 15.8, "action": "edit", "text": None,
         "speaker_id": "SPEAKER_01", "at": "t"},
    ]
    mfile(base, ".reviews.json").write_text(json.dumps(decisions))
    superseded = [
        {"start": 20.0, "end": 21.0, "action": "edit", "text": None,
         "speaker_id": "Dana Fake", "at": "t"},          # plain name: resolves
        {"start": 22.0, "end": 22.4, "action": "edit", "text": None,
         "speaker_id": "SPEAKER_07", "at": "t"},         # dead cluster id: skip
    ]
    mfile(base, ".reviews.superseded.json").write_text(json.dumps(superseded))
    return data


def test_extractor_labels_speaker_changes_only(sandbox):
    _seed_reviewed_meeting("Mtg")
    records, stats = diar_eval.extract_review_truth()
    # labeled: 2 edits + 2 split halves + 1 superseded plain name = 5
    assert stats["labeled"] == 5 and len(records) == 5
    assert stats["decisions"] == 9
    assert stats["accepts"] == 1 and stats["deletes"] == 1
    assert stats["text_only_edits"] == 1
    # unresolved: the SPEAKER_01 (unnamed cluster) edit + the dead SPEAKER_07
    assert stats["unresolved_speaker"] == 2
    by_span = {(r["start"], r["end"]): r for r in records}
    assert by_span[(1.0, 2.5)]["correct_speaker"] == "Alice Fake"
    assert by_span[(5.0, 5.4)]["correct_speaker"] == "Bob Fake"
    assert by_span[(12.0, 13.0)]["correct_speaker"] == "Carol Fake"
    assert by_span[(13.0, 14.0)]["correct_speaker"] == "Alice Fake"
    assert by_span[(20.0, 21.0)]["correct_speaker"] == "Dana Fake"
    assert by_span[(20.0, 21.0)]["source"] == "reviews_superseded"
    assert by_span[(1.0, 2.5)]["source"] == "reviews"


def test_extractor_survives_missing_and_corrupt_sidecars(sandbox):
    base = "Empty"
    mfile(base, ".json").write_text(json.dumps(
        {"speakers": [], "segments": [], "words": []}))
    mfile(base, ".reviews.json").write_text("{not json")
    records, stats = diar_eval.extract_review_truth()
    assert records == [] and stats["labeled"] == 0


# ------------------------------------------------------- channel labeler ----

def _tone(sr, secs, amp, f=300.0):
    t = np.arange(int(sr * secs)) / sr
    return amp * np.sin(2 * np.pi * f * t)


def test_channel_labeler_dominance_and_abstain(sandbox, tmp_path):
    sr = 16000
    # 0-2s: mic dominates (me). 2-4s: system dominates with mic bleed (them).
    # 4-6s: near-equal energy (must ABSTAIN). 6-8s: silence (must ABSTAIN).
    mic = np.concatenate([_tone(sr, 2, 0.5), _tone(sr, 2, 0.05),
                          _tone(sr, 2, 0.3), _tone(sr, 2, 0.0)])
    sysd = np.concatenate([_tone(sr, 2, 0.01), _tone(sr, 2, 0.5),
                           _tone(sr, 2, 0.25), _tone(sr, 2, 0.0)])
    m, s = tmp_path / "m.wav", tmp_path / "s.wav"
    sf.write(str(m), mic.astype(np.float32), sr)
    sf.write(str(s), sysd.astype(np.float32), sr)
    spans = [(0.2, 1.8), (2.2, 3.8), (4.2, 5.8), (6.2, 7.8)]
    labels = diar_eval.label_stereo_spans(m, s, spans)
    assert labels == ["mic", "sys", None, None]


def test_channel_labeler_margin_is_conservative(sandbox, tmp_path):
    """A 6 dB edge (which would pass the pipeline's own hysteresis gates in
    spirit) sits INSIDE the abstain band here — truth demands more."""
    sr = 16000
    mic = _tone(sr, 2, 0.4)
    sysd = _tone(sr, 2, 0.2)   # mic ahead by ~6 dB — not confident enough
    m, s = tmp_path / "m.wav", tmp_path / "s.wav"
    sf.write(str(m), mic.astype(np.float32), sr)
    sf.write(str(s), sysd.astype(np.float32), sr)
    assert diar_eval.label_stereo_spans(m, s, [(0.1, 1.9)]) == [None]


# ---------------------------------------------------------- span scoring ----

def test_span_speaker_majority_overlap():
    segs = [{"start": 0.0, "end": 2.0, "name": "A"},
            {"start": 2.0, "end": 3.0, "name": "B"},
            {"start": 3.0, "end": 6.0, "name": None}]
    assert diar_eval.span_speaker(segs, 0.5, 1.5) == "A"
    assert diar_eval.span_speaker(segs, 1.5, 2.9) == "B"      # 0.9s B vs 0.5s A
    assert diar_eval.span_speaker(segs, 3.5, 5.0) is None      # unnamed wins span
    assert diar_eval.span_speaker(segs, 10.0, 11.0) is None    # no overlap at all


def test_aggregate_rows_bands_and_confusions():
    rows = [
        {"meeting": "M", "dur": 0.2, "truth": "A", "predicted": "B", "correct": False},
        {"meeting": "M", "dur": 0.4, "truth": "A", "predicted": "A", "correct": True},
        {"meeting": "N", "dur": 3.0, "truth": "B", "predicted": None, "correct": False},
        {"meeting": "N", "dur": 3.0, "truth": "B", "predicted": "B", "correct": True},
    ]
    agg = diar_eval.aggregate_rows(rows)
    assert agg["n"] == 4 and agg["correct"] == 2 and agg["accuracy"] == 0.5
    assert agg["by_band"]["<0.3s"] == {"n": 1, "correct": 0, "accuracy": 0.0}
    assert agg["by_band"][">=2.5s"]["n"] == 2
    assert ["A", "B", 1] in agg["confusions"]
    assert ["B", "(unnamed)", 1] in agg["confusions"]
    assert agg["per_meeting"]["M"]["accuracy"] == 0.5


# ------------------------------------- runner smoke on a 30s fixture --------

def _seed_scorable_meeting(base="Scored"):
    """A ~30s meeting whose .diar.npz + an enrolled voiceprint make the
    cache-only rebuild name cluster SPEAKER_00 'Alice Fake' with certainty:
    the enrolled sample IS that cluster's centroid."""
    words = ([{"start": 0.4 * i, "end": 0.4 * i + 0.3, "word": f"w{i}"}
              for i in range(37)]        # 0.0 .. ~15s
             + [{"start": 15.0 + 0.4 * i, "end": 15.3 + 0.4 * i, "word": f"x{i}"}
                for i in range(37)])     # 15.0 .. ~30s
    data = {"source_file": f"{base}.m4a", "duration_sec": 30.0, "strict": False,
            "speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 1"},
                         {"id": "SPEAKER_01", "name": None, "display": "Speaker 2"}],
            "segments": [], "words": words}
    mfile(base, ".json").write_text(json.dumps(data))
    rng = np.random.default_rng(11)
    c0 = rng.normal(size=256)
    c1 = rng.normal(size=256)
    raw_turns = [{"start": 0.0, "end": 15.0, "cluster": "SPEAKER_00"},
                 {"start": 15.0, "end": 30.0, "cluster": "SPEAKER_01"}]
    diarcache.save(mfile(base, ".diar.npz"), raw_turns, [None, None],
                   {"SPEAKER_00": c0, "SPEAKER_01": c1})
    identify.enroll("Alice Fake", c0, source=base)
    decisions = [
        # the rebuild names 0-15s Alice -> this correction is now SATISFIED
        {"start": 2.0, "end": 4.0, "action": "edit", "text": None,
         "speaker_id": "name:Alice Fake", "at": "t"},
        # 15-30s stays an unnamed cluster -> this one is still a MISS
        {"start": 16.0, "end": 16.4, "action": "edit", "text": None,
         "speaker_id": "name:Bob Fake", "at": "t"},
    ]
    mfile(base, ".reviews.json").write_text(json.dumps(decisions))
    return data


def test_reviews_scoring_end_to_end_is_readonly(sandbox):
    data = _seed_scorable_meeting("Scored")
    before_json = mfile("Scored", ".json").read_text()
    before_reg = json.dumps(identify.load_registry(), sort_keys=True)

    res = diar_eval.run_reviews()
    assert res["n"] == 2 and res["correct"] == 1 and res["accuracy"] == 0.5
    # the satisfied correction fell in the 1.5-2.5s band; the miss in 0.3-0.6
    assert res["by_band"]["1.5-2.5s"] == {"n": 1, "correct": 1, "accuracy": 1.0}
    assert res["by_band"]["0.3-0.6s"] == {"n": 1, "correct": 0, "accuracy": 0.0}
    assert res["per_meeting"]["Scored"]["n"] == 2
    assert ["Bob Fake", "(unnamed)", 1] in res["confusions"]

    # READ-ONLY on the library and the registry: scoring rewrites nothing
    assert mfile("Scored", ".json").read_text() == before_json
    assert json.dumps(identify.load_registry(), sort_keys=True) == before_reg
    assert json.loads(mfile("Scored", ".json").read_text()) == data


def test_predict_segments_missing_cache_returns_none(sandbox):
    mfile("NoCache", ".json").write_text(json.dumps(
        {"speakers": [], "segments": [], "words": []}))
    assert diar_eval.predict_segments("NoCache") is None


# ------------------------------------------------- stereo run: no captures --

def test_stereo_run_reports_empty_library_honestly(sandbox):
    res = diar_eval.run_stereo()
    assert res["n"] == 0 and res["accuracy"] is None
    assert res["n_stereo_meetings"] == 0
    assert res["loose_captures_unscored"] == []
    assert set(res["by_band"]) == set(diar_eval.BAND_LABELS)


# ----------------------------------------------- scoreboard + baselines -----

def test_scoreboard_schema_and_baseline_deltas(sandbox, tmp_path):
    _seed_scorable_meeting("Scored")
    out_root = tmp_path / "evalout"
    sources = {"reviews": diar_eval.run_reviews()}

    run_dir, board, deltas = diar_eval.write_scoreboard(
        out_root, "2026-07-13", sources, baseline_name="test-base")
    assert deltas is None                       # nothing stored before this run
    sb = json.loads((run_dir / "scoreboard.json").read_text())
    assert sb["run"]["date"] == "2026-07-13"
    assert sb["run"]["git_sha"]
    assert "NAMING_THRESHOLD" in sb["run"]["config"]
    assert sb["headline"]["turn_accuracy_overall"] == 0.5
    assert sb["headline"]["n_labeled_turns"] == 2
    assert sb["sources"]["reviews"]["by_band"]["0.3-0.6s"]["n"] == 1
    assert "rows" not in sb["sources"]["reviews"]   # per-row detail lives aside
    assert (run_dir / "rows.json").exists()
    md = (run_dir / "scoreboard.md").read_text()
    assert "| duration band |" in md and "0.3-0.6s" in md
    assert (out_root / "baselines" / "test-base.json").exists()
    assert (out_root / "baselines" / "CURRENT").read_text().strip() == "test-base"

    # a second run prints deltas against the stored baseline
    run_dir2, board2, deltas2 = diar_eval.write_scoreboard(
        out_root, "2026-07-14", {"reviews": diar_eval.run_reviews()})
    assert run_dir2 != run_dir
    assert deltas2 is not None and deltas2["baseline_name"] == "test-base"
    assert deltas2["metrics"]["overall"] == (0.5, 0.5)
    md2 = (run_dir2 / "scoreboard.md").read_text()
    assert "vs baseline" in md2
    sb2 = json.loads((run_dir2 / "scoreboard.json").read_text())
    assert sb2["vs_baseline"]["name"] == "test-base"


def test_scoreboard_handles_empty_sources(tmp_path):
    out_root = tmp_path / "evalout"
    empty = diar_eval.aggregate_rows([])
    run_dir, board, _ = diar_eval.write_scoreboard(
        out_root, "2026-07-13", {"stereo": empty})
    assert board["headline"]["turn_accuracy_overall"] is None
    assert board["headline"]["n_labeled_turns"] == 0
    assert (run_dir / "scoreboard.md").exists()
