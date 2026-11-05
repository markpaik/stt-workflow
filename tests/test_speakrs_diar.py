"""speakrs engine: output parsing, centroid rule, fallback, and ETA separation."""
import json

import numpy as np
import pytest

from stt import config, diarize, rates, speakrs_diar


def test_overlap_spans_marks_only_two_or_more_active():
    segs = [(0.0, 5.0, "SPEAKER_00"), (4.0, 6.0, "SPEAKER_01"),
            (6.0, 7.0, "SPEAKER_00"), (8.0, 9.0, "SPEAKER_01")]
    assert speakrs_diar.overlap_spans(segs) == [(4.0, 5.0)]


def test_overlap_spans_touching_segments_are_not_overlap():
    segs = [(0.0, 2.0, "SPEAKER_00"), (2.0, 3.0, "SPEAKER_01")]
    assert speakrs_diar.overlap_spans(segs) == []


def test_centroids_use_clean_rows_and_key_by_cluster_id():
    emb = np.zeros((3, 2, 4), dtype="<f4")
    emb[0, 0] = [1, 0, 0, 0]    # cluster 1, clean
    emb[1, 0] = [0, 1, 0, 0]    # cluster 1, not clean enough: ignored
    emb[2, 1] = [0, 0, 1, 0]    # cluster 0, never clean: falls back to all rows
    hc = np.array([[1, -2], [1, -2], [-2, 0]], dtype="<i4")
    many = speakrs_diar.MIN_CLEAN_SEC / speakrs_diar.FRAME_STEP_SEC + 1
    clean = np.array([[many, 0], [1, 0], [0, 1]], dtype="<f4")
    c = speakrs_diar.centroids(emb, hc, clean, ["SPEAKER_00", "SPEAKER_01"])
    assert np.allclose(c["SPEAKER_01"], [1, 0, 0, 0])
    assert np.allclose(c["SPEAKER_00"], [0, 0, 1, 0])


def _fake_helper(tmp_path, monkeypatch, result, exit_code=0):
    """A stand-in stt-speakrs that writes `result` plus tiny arrays."""
    models = tmp_path / "models"
    for f in speakrs_diar.model_files():
        (models / f).parent.mkdir(parents=True, exist_ok=True)
        (models / f).write_bytes(b"")
    emb = np.ones((1, 2, 3), dtype="<f4")
    hc = np.array([[0, 1]], dtype="<i4")
    clean = np.full((1, 2), 500, dtype="<f4")
    src = tmp_path / "src"
    src.mkdir()
    emb.tofile(src / "emb.f32")
    hc.tofile(src / "clusters.i32")
    clean.tofile(src / "clean.f32")
    (src / "result.json").write_text(json.dumps(result))
    binp = tmp_path / "stt-speakrs"
    binp.write_text(f"#!/bin/sh\ncp {src}/* \"$3\"/\nexit {exit_code}\n")
    binp.chmod(0o755)
    monkeypatch.setattr(config, "SPEAKRS_BIN", binp)
    monkeypatch.setattr(config, "SPEAKRS_MODELS", models)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")


def test_run_reads_helper_output(tmp_path, monkeypatch):
    _fake_helper(tmp_path, monkeypatch, {
        "emb_shape": [1, 2, 3], "pipeline_run_sec": 1.0,
        "segments": [[0.0, 2.0, "SPEAKER_00"], [1.5, 3.0, "SPEAKER_01"]],
        "exclusive_segments": [[1.5, 3.0, "SPEAKER_01"], [0.0, 1.5, "SPEAKER_00"]]})
    r = speakrs_diar.run(tmp_path / "x.wav")
    assert r["labels"] == ["SPEAKER_00", "SPEAKER_01"]
    assert [t["cluster"] for t in r["raw_turns"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert r["overlaps"] == [(1.5, 2.0)]
    assert set(r["centroids"]) == {"SPEAKER_00", "SPEAKER_01"}
    assert r["engine"].startswith("speakrs")
    assert not list((tmp_path / "work").iterdir())  # temp output cleaned up


def test_run_failure_raises_unavailable(tmp_path, monkeypatch):
    _fake_helper(tmp_path, monkeypatch, {}, exit_code=1)
    with pytest.raises(speakrs_diar.SpeakrsUnavailable):
        speakrs_diar.run(tmp_path / "x.wav")


def test_run_malformed_output_raises_unavailable(tmp_path, monkeypatch):
    # a segment row with the wrong arity must fall back, not fail the file
    _fake_helper(tmp_path, monkeypatch, {
        "emb_shape": [1, 2, 3], "segments": [[0.0, 2.0]],
        "exclusive_segments": [[0.0, 2.0, "SPEAKER_00"]]})
    with pytest.raises(speakrs_diar.SpeakrsUnavailable):
        speakrs_diar.run(tmp_path / "x.wav")


def test_missing_models_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SPEAKRS_BIN", tmp_path / "nope")
    ok, why = speakrs_diar.available()
    assert not ok and "not built" in why


def test_partial_model_bundle_is_unavailable(tmp_path, monkeypatch):
    binp = tmp_path / "stt-speakrs"
    binp.write_text("")
    models = tmp_path / "models"
    (models / "segmentation-3.0.mlmodelc").mkdir(parents=True)  # download cut short
    monkeypatch.setattr(config, "SPEAKRS_BIN", binp)
    monkeypatch.setattr(config, "SPEAKRS_MODELS", models)
    ok, why = speakrs_diar.available()
    assert not ok and "incomplete" in why


def test_missing_helper_estimates_at_pyannote_speed(tmp_path, monkeypatch):
    monkeypatch.setattr(rates, "RATES_LOG", tmp_path / "rates.jsonl")
    monkeypatch.setattr(config, "DIARIZER", "speakrs")
    monkeypatch.setattr(speakrs_diar, "available", lambda: (False, "not built"))
    rates._cache["sig"] = None
    assert rates.effective_diarizer() == "pyannote"
    assert rates.diarize_rate(1) == config.EST_RATES["diarize"]["pyannote"]
    rows = [{"duration": 3600, "n": 1, "diar": "pyannote", "secs": {"diarizing": 1600}}]
    assert rates._samples(rows)["diarize"]["1"] == [(3600, 1600)]


def test_diarize_falls_back_to_pyannote(monkeypatch):
    monkeypatch.setattr(config, "DIARIZER", "speakrs")
    monkeypatch.setattr(speakrs_diar, "available", lambda: (True, ""))
    monkeypatch.setattr(diarize, "_get_pipeline", lambda: object())

    def boom(*a, **k):
        raise speakrs_diar.SpeakrsUnavailable("no helper")
    monkeypatch.setattr(speakrs_diar, "run", boom)
    called = {}

    def fake_pyannote(pipe, wav, *a):
        called["yes"] = True
        return {"labels": [], "overlaps": [], "centroids": {}, "raw_turns": [],
                "engine": config.DIARIZATION_MODEL}
    monkeypatch.setattr(diarize, "_pyannote_raw", fake_pyannote)
    monkeypatch.setattr(diarize, "_embed_turns", lambda *a, **k: ([], 0))
    out = diarize.diarize("x.wav", voiceprints={})
    assert called and out["engine"] == config.DIARIZATION_MODEL


def test_speaker_count_hints_use_pyannote(monkeypatch):
    monkeypatch.setattr(config, "DIARIZER", "speakrs")
    monkeypatch.setattr(diarize, "_get_pipeline", lambda: object())
    monkeypatch.setattr(speakrs_diar, "run",
                        lambda *a: pytest.fail("speakrs cannot honor a speaker count"))
    monkeypatch.setattr(diarize, "_pyannote_raw", lambda *a: {
        "labels": [], "overlaps": [], "centroids": {}, "raw_turns": [],
        "engine": "pyannote"})
    monkeypatch.setattr(diarize, "_embed_turns", lambda *a, **k: ([], 0))
    assert diarize.diarize("x.wav", voiceprints={}, num_speakers=3)["engine"] == "pyannote"


def test_pyannote_samples_do_not_teach_the_speakrs_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(rates, "RATES_LOG", tmp_path / "rates.jsonl")
    rates._cache["sig"] = None
    monkeypatch.setattr(config, "DIARIZER", "speakrs")
    monkeypatch.setattr(speakrs_diar, "available", lambda: (True, ""))
    rows = [{"duration": 3600, "n": 1, "secs": {"diarizing": 1600}},           # old pyannote row
            {"duration": 3600, "n": 1, "diar": "speakrs", "secs": {"diarizing": 120}}]
    b = rates._samples(rows)
    assert b["diarize"]["1"] == [(3600, 120)]
    assert rates._samples(rows, "pyannote")["diarize"]["1"] == [(3600, 1600)]


def test_record_tags_the_engine_that_ran(tmp_path, monkeypatch):
    monkeypatch.setattr(rates, "RATES_LOG", tmp_path / "rates.jsonl")
    rates.record(600, {"diarizing": 300}, "parakeet", diarizer="pyannote/speaker-diarization-community-1")
    rates.record(600, {"diarizing": 20}, "parakeet", diarizer="speakrs-0.7.0-coreml ahc=0.55")
    rows = [json.loads(x) for x in (tmp_path / "rates.jsonl").read_text().splitlines()]
    assert [r["diar"] for r in rows] == ["pyannote", "speakrs"]
