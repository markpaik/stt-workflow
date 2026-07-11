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


def _fake_diar_sys(dim=8):
    """System-channel diarization: two remote speakers, turns 0-1 and 1-2."""
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


def _channel_fakes(monkeypatch, mic_matches=True, sanity=None, spans=None):
    """Stub the heavy channel-aware machinery; combine_turns runs for real."""
    from stt import channels, diarize, identify
    dim = 8
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr_ch())
    monkeypatch.setattr(diarize, "diarize", lambda *a, **k: _fake_diar_sys(dim))
    monkeypatch.setattr(audio, "to_wav16k", lambda src, wav: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "to_wav16k_channel",
                        lambda src, wav, ch: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "probe_channels", lambda src: 2)
    monkeypatch.setattr(audio, "duration_sec", lambda p: 120.0)
    monkeypatch.setattr(config, "PUNCTUATE", False)
    monkeypatch.setattr(channels, "sanity", lambda m, s: sanity or {
        "dual_mono": False, "sys_dead": False, "mic_dead": False,
        "mic_rms_db": -10.0, "sys_rms_db": -20.0})
    monkeypatch.setattr(channels, "mic_spans", lambda m, s: spans if spans is not None else [(3.0, 5.0)])
    # embedding matches Mark's voiceprint iff mic_matches
    vec = np.ones(dim) if mic_matches else -np.ones(dim)
    monkeypatch.setattr(diarize, "embed_spans", lambda wav, sp, **k: [vec for _ in sp])
    monkeypatch.setattr(identify, "load_voiceprints",
                        lambda: {"Mark Paik": np.ones((1, dim))})


def _fake_asr_ch():
    import types
    return types.SimpleNamespace(transcribe=lambda wav, progress=None: {
        "engine": "fake-asr", "text": "hi there my turn now",
        "words": [{"start": 0.2, "end": 0.5, "word": "hi"},       # SPEAKER_00
                  {"start": 1.2, "end": 1.5, "word": "there"},    # SPEAKER_01
                  {"start": 3.5, "end": 3.9, "word": "mine"},     # MIC (3-5s gap)
                  {"start": 4.2, "end": 4.6, "word": "now"}]})    # MIC


OPTS = {"channel_layout": "mic_left_system_right", "mic_speaker": "Mark Paik"}


def test_channel_aware_overlays_the_mic_speaker(sandbox, monkeypatch):
    import json
    _channel_fakes(monkeypatch)
    src = config.PROJECT_DIR / "Team Call 05012026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False, input_opts=OPTS)
    data = json.loads(res["json"].read_text())
    assert data["channel_mode"] == "stereo_channel_aware"
    assert data["mic_speaker"] == "Mark Paik"
    # Mark appears as a named speaker via the synthetic MIC id
    from stt import channels
    mark = [s for s in data["speakers"] if s["id"] == channels.MIC_ID]
    assert len(mark) == 1 and mark[0]["name"] == "Mark Paik"
    # and his words (in the 3-5s gap) are attributed to him
    mark_segs = [seg for seg in data["segments"] if seg.get("name") == "Mark Paik"]
    assert mark_segs and "mine" in " ".join(s["text"] for s in mark_segs)
    # the mark spans + mode round-trip into the diar cache for relabel
    from stt import diarcache
    ch = diarcache.load_channel(config.meeting_file("Team Call 05012026", ".diar.npz"))
    assert ch["mode"] == "stereo_channel_aware" and ch["mic_speaker"] == "Mark Paik"
    assert len(ch["spans"]) == 1


def test_channel_aware_falls_back_when_voice_does_not_match(sandbox, monkeypatch):
    import json
    _channel_fakes(monkeypatch, mic_matches=False)  # bleed / wrong voice
    src = config.PROJECT_DIR / "Bleed Day 05022026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False, input_opts=OPTS)
    data = json.loads(res["json"].read_text())
    assert data["channel_mode"] == "mono_fallback_bleed"
    from stt import channels
    assert not any(s["id"] == channels.MIC_ID for s in data["speakers"])


def test_dual_mono_falls_back_to_mono(sandbox, monkeypatch):
    import json
    _channel_fakes(monkeypatch, sanity={"dual_mono": True, "sys_dead": False,
                                        "mic_dead": False, "mic_rms_db": -10.0,
                                        "sys_rms_db": -10.0})
    src = config.PROJECT_DIR / "Mono Dressed 05032026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False, input_opts=OPTS)
    data = json.loads(res["json"].read_text())
    assert data["channel_mode"] == "mono_fallback_dual_mono"


def test_no_enroll_caches_mic_spans_for_later_relabel(sandbox, monkeypatch):
    """C6: a stereo recording processed before the mic speaker is enrolled falls
    back to mono for THIS pass, but caches its (ungated) mic spans + embeddings
    so enrolling the speaker and running relabel recovers the attribution without
    a full re-transcription."""
    import json

    from stt import diarcache, identify
    _channel_fakes(monkeypatch)
    monkeypatch.setattr(identify, "load_voiceprints", lambda: {})  # not enrolled yet
    src = config.PROJECT_DIR / "Early Call 05052026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False, input_opts=OPTS)
    data = json.loads(res["json"].read_text())
    assert data["channel_mode"] == "mono_fallback_no_enroll"
    from stt import channels
    assert not any(s["id"] == channels.MIC_ID for s in data["speakers"])  # mono this pass
    # but the mic spans + embeddings survived into the cache for a future relabel
    ch = diarcache.load_channel(config.meeting_file("Early Call 05052026", ".diar.npz"))
    assert ch["mic_speaker"] == "Mark Paik" and len(ch["spans"]) == 1


def test_sys_dead_channel_falls_back_to_mono(sandbox, monkeypatch):
    """G4: a dead system channel (mic-only capture) can't be diarized as 'them',
    so the recording drops to the mono mix."""
    import json
    _channel_fakes(monkeypatch, sanity={"dual_mono": False, "sys_dead": True,
                                        "mic_dead": False, "mic_rms_db": -10.0,
                                        "sys_rms_db": -80.0})
    src = config.PROJECT_DIR / "Sys Dead 05062026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False, input_opts=OPTS)
    assert json.loads(res["json"].read_text())["channel_mode"] == "mono_fallback_sys_dead"


def test_no_mic_activity_falls_back_to_mono(sandbox, monkeypatch):
    """G4: an all-listening meeting where the mic owner never dominates their own
    channel yields no spans to overlay, so it processes as mono."""
    import json
    _channel_fakes(monkeypatch, spans=[])
    src = config.PROJECT_DIR / "All Listening 05072026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False, input_opts=OPTS)
    assert json.loads(res["json"].read_text())["channel_mode"] == "mono_fallback_no_me"


def test_channel_layout_read_from_an_on_disk_sidecar(sandbox, monkeypatch):
    """G4: the recorder drops a <base>.opts.json next to a fresh capture. The
    pipeline must pick the layout up from that on-disk sidecar, not only from an
    explicit input_opts (the batch reads it, but _resolve_channel is the path a
    Redo and a direct process_file rely on)."""
    import json
    _channel_fakes(monkeypatch)
    src = config.PROJECT_DIR / "Sidecar Call 05082026.m4a"
    src.write_bytes(b"x")
    src.with_suffix(".opts.json").write_text(json.dumps(OPTS))
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False)  # NO input_opts
    data = json.loads(res["json"].read_text())
    assert data["channel_mode"] == "stereo_channel_aware"
    assert data["channel_layout"] == "mic_left_system_right"


def test_no_sidecar_is_pure_mono(sandbox, monkeypatch):
    """A recording that never declared a layout must take the byte-identical
    mono path: no channel_* keys, no split helper called."""
    import json
    from stt import diarize
    monkeypatch.setattr(pipeline, "_load_asr", lambda strict=False: _fake_asr_ch())
    monkeypatch.setattr(diarize, "diarize", lambda *a, **k: _fake_diar_sys())
    monkeypatch.setattr(audio, "to_wav16k", lambda src, wav: wav.write_bytes(b"RIFF0000WAVE"))
    monkeypatch.setattr(audio, "duration_sec", lambda p: 120.0)
    monkeypatch.setattr(config, "PUNCTUATE", False)
    def _boom(*a, **k):
        raise AssertionError("mono path must never split channels")
    monkeypatch.setattr(audio, "to_wav16k_channel", _boom)
    src = config.PROJECT_DIR / "Plain 05042026.m4a"
    src.write_bytes(b"x")
    res = pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=True,
                                do_verify=False, track_unknowns=False)
    data = json.loads(res["json"].read_text())
    assert "channel_mode" not in data and "channel_layout" not in data


def test_meeting_date_rejects_a_future_filename_date(sandbox, monkeypatch):
    """A filename like '...10242026' parses to Oct 24 2026 (future). A recording
    can't be from the future, so the stamped date must fall back to the file's
    mtime instead of sorting above today's real meetings."""
    from datetime import date
    src = config.PROJECT_DIR / "Thrive ICD Omar 10242026.m4a"
    src.write_bytes(b"x")
    d = pipeline._meeting_date(src)
    assert d <= date.today().isoformat()
    assert d != "2026-10-24"
    # a PAST filename date is still trusted
    past = config.PROJECT_DIR / "LT Meeting 05212026.m4a"
    past.write_bytes(b"x")
    assert pipeline._meeting_date(past) == "2026-05-21"
