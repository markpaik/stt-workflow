"""pipeline.process_file: scratch-cleanup guarantee on a failed conversion.
(The ASR/diarization stages themselves are exercised by tuning/qa, not here —
this only covers the fast, ML-free failure path.)"""
import numpy as np
import pytest

from stt import audio, config, pipeline


def test_process_file_cleans_scratch_wav_on_conversion_failure(sandbox, monkeypatch):
    """A failed/partial ffmpeg conversion (disk full, corrupt source, killed
    mid-write) must still hit the scratch cleanup — leaking the WAV would
    accumulate disk usage across a run with several bad files, worsening the
    very problem that caused the failure."""
    def _boom(src, dst):
        dst.write_bytes(b"partial garbage")  # ffmpeg wrote something, then died
        raise RuntimeError("ffmpeg crashed mid-conversion")
    monkeypatch.setattr(audio, "to_wav16k", _boom)
    monkeypatch.setattr(audio, "duration_sec", lambda p: 10.0)

    src = config.PROJECT_DIR / "bad.m4a"
    src.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False)

    leaked = list(config.WORK_DIR.glob("*.wav"))
    assert leaked == [], f"scratch wav leaked: {leaked}"


def _fake_diar(dim=8):
    """Minimal diarizer output with two clusters and real embedding vectors —
    enough to drive the save_embeddings branch that writes .emb.npz/.diar.npz."""
    return {
        "turns": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                  {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_01"}],
        "labels": ["SPEAKER_00", "SPEAKER_01"],
        "names": {"SPEAKER_00": {"name": None, "display": "Speaker 1", "score": 0.0},
                  "SPEAKER_01": {"name": None, "display": "Speaker 2", "score": 0.0}},
        "overlaps": [],
        "embeddings": {"SPEAKER_00": np.ones(dim), "SPEAKER_01": np.arange(dim, dtype=float)},
        "cluster_names": {"SPEAKER_00": None, "SPEAKER_01": None},
        "raw_turns": [{"start": 0.0, "end": 1.0, "cluster": "SPEAKER_00"},
                      {"start": 1.0, "end": 2.0, "cluster": "SPEAKER_01"}],
        "turn_embeddings": [np.ones(dim), np.arange(dim, dtype=float)],
        "refine_stats": {"spans": []},
    }


def test_diarized_run_writes_a_loadable_embeddings_cache(sandbox, monkeypatch):
    """The .emb.npz write goes through a temp-then-os.replace atomic path. A
    prior version passed the .tmp PATH to np.savez, which appends its own .npz
    and left os.replace chasing a file that never existed — crashing every real
    diarized run at the cache write. The whole ML-free test bank runs with
    do_diarize=False, so this branch (save_embeddings AND diar AND embeddings)
    was never exercised; drive it with a stubbed ASR + diarizer."""
    from stt import diarize
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr())
    monkeypatch.setattr(diarize, "diarize", lambda *a, **k: _fake_diar())
    monkeypatch.setattr(audio, "to_wav16k", lambda src, wav: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 120.0)
    monkeypatch.setattr(config, "PUNCTUATE", False)

    src = config.PROJECT_DIR / "Diarized Meeting 05012026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR,
                                do_diarize=True, do_verify=False, track_unknowns=False)

    emb_path = config.meeting_file("Diarized Meeting 05012026", ".emb.npz")
    assert res["emb"] == emb_path and emb_path.exists()
    with np.load(emb_path) as z:  # a truncated/mis-suffixed write would not load
        assert sorted(z.files) == ["SPEAKER_00", "SPEAKER_01"]
    # the diar cache landed too, and no temp artifact was left behind
    assert config.meeting_file("Diarized Meeting 05012026", ".diar.npz").exists()
    strays = (list(config.meeting_dir("Diarized Meeting 05012026").glob("*.tmp"))
              + list(config.meeting_dir("Diarized Meeting 05012026").glob("*.tmp.npz")))
    assert strays == [], f"atomic-write temp left behind: {strays}"


def _fake_asr():
    import types
    return types.SimpleNamespace(transcribe=lambda wav, progress=None: {
        "engine": "fake-asr", "text": "hello there friend now",
        "words": [{"start": 0.2, "end": 0.5, "word": "hello"},
                  {"start": 0.6, "end": 0.9, "word": "there"},
                  {"start": 1.2, "end": 1.5, "word": "friend"},
                  {"start": 1.6, "end": 1.9, "word": "now"}]})
