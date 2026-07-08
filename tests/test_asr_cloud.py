"""Cloud ASR adapter: provider response parsing (mocked HTTP — no network),
key lookup, and the pipeline's privacy/fallback policy."""
import json
import types

import pytest

from stt import asr_cloud, config, pipeline


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _post_returning(payload, seen):
    def _post(url, headers=None, data=None, files=None, timeout=None):
        seen.append({"url": url, "headers": headers, "data": data})
        return _Resp(payload)
    return _post


@pytest.fixture
def fake_upload(monkeypatch, tmp_path):
    """Skip real ffmpeg compression — the adapter's parsing is what's under test."""
    up = tmp_path / "up.m4a"
    up.write_bytes(b"aac")
    monkeypatch.setattr(asr_cloud, "_compress_for_upload", lambda wav: up)
    return up


def test_scribe_parses_words_and_drops_spacing(sandbox, fake_upload, monkeypatch):
    import requests
    seen = []
    monkeypatch.setattr(requests, "post", _post_returning({
        "text": "hello there",
        "words": [
            {"type": "word", "text": "hello", "start": 0.1, "end": 0.5},
            {"type": "spacing", "text": " ", "start": 0.5, "end": 0.6},
            {"type": "word", "text": "there", "start": 0.6, "end": 1.0},
        ]}, seen))
    monkeypatch.setattr(config, "ASR_BACKEND", "cloud:scribe")
    monkeypatch.setenv("STT_ELEVENLABS_KEY", "k")
    out = asr_cloud.transcribe(sandbox / "x.wav")
    assert out["engine"].startswith("elevenlabs/")
    assert out["words"] == [{"start": 0.1, "end": 0.5, "word": "hello"},
                            {"start": 0.6, "end": 1.0, "word": "there"}]
    assert seen[0]["headers"]["xi-api-key"] == "k"
    assert "elevenlabs.io" in seen[0]["url"]


def test_openai_parses_verbose_json_words(sandbox, fake_upload, monkeypatch):
    import requests
    seen = []
    monkeypatch.setattr(requests, "post", _post_returning({
        "text": "hi all",
        "words": [{"word": "hi", "start": 0.0, "end": 0.3},
                  {"word": "all", "start": 0.4, "end": 0.8}]}, seen))
    monkeypatch.setattr(config, "ASR_BACKEND", "cloud:openai")
    monkeypatch.setenv("STT_OPENAI_KEY", "sk-test")
    out = asr_cloud.transcribe(sandbox / "x.wav")
    assert out["engine"] == "openai/whisper-1"
    assert [w["word"] for w in out["words"]] == ["hi", "all"]
    assert seen[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_voxtral_synthesizes_words_from_segments(sandbox, fake_upload, monkeypatch):
    """A segments-only response still yields per-word timings inside each
    segment's bounds, monotonic, so the diarization merge works unchanged."""
    import requests
    monkeypatch.setattr(requests, "post", _post_returning({
        "text": "one two three four",
        "segments": [{"start": 0.0, "end": 2.0, "text": "one two"},
                     {"start": 2.5, "end": 4.5, "text": "three four"}]}, []))
    monkeypatch.setattr(config, "ASR_BACKEND", "cloud:voxtral")
    monkeypatch.setenv("STT_MISTRAL_KEY", "m")
    out = asr_cloud.transcribe(sandbox / "x.wav")
    ws = out["words"]
    assert [w["word"] for w in ws] == ["one", "two", "three", "four"]
    assert ws[0]["start"] == 0.0 and ws[1]["end"] <= 2.0
    assert ws[2]["start"] == 2.5 and ws[3]["end"] <= 4.5
    starts = [w["start"] for w in ws]
    assert starts == sorted(starts)


def test_missing_key_is_a_clear_error_not_a_crash(sandbox, monkeypatch):
    monkeypatch.setattr(config, "ASR_BACKEND", "cloud:scribe")
    monkeypatch.delenv("STT_ELEVENLABS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        asr_cloud.transcribe(sandbox / "x.wav")


def test_empty_words_from_provider_raises(sandbox, fake_upload, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post",
                        _post_returning({"text": "", "words": []}, []))
    monkeypatch.setattr(config, "ASR_BACKEND", "cloud:scribe")
    monkeypatch.setenv("STT_ELEVENLABS_KEY", "k")
    with pytest.raises(RuntimeError, match="no words"):
        asr_cloud.transcribe(sandbox / "x.wav")


# ---------- pipeline policy ----------

def test_strict_mode_forces_local_engine(sandbox, monkeypatch):
    """The privacy contract: with a cloud engine selected globally, a strict
    run STILL loads a local engine — sensitive audio never uploads."""
    monkeypatch.setattr(config, "ASR_BACKEND", "cloud:scribe")
    asr = pipeline._load_asr(strict=True)
    assert not getattr(asr, "IS_CLOUD", False)
    asr = pipeline._load_asr(strict=False)
    assert getattr(asr, "IS_CLOUD", False)


def test_cloud_failure_falls_back_to_local(sandbox, monkeypatch):
    from stt import asr_parakeet
    cloudish = types.SimpleNamespace(
        IS_CLOUD=True,
        transcribe=lambda wav, progress=None: (_ for _ in ()).throw(
            RuntimeError("quota exceeded")))
    local_out = {"engine": "parakeet", "text": "ok", "words": [
        {"start": 0.0, "end": 0.5, "word": "ok"}]}
    monkeypatch.setattr(asr_parakeet, "transcribe",
                        lambda wav, progress=None: local_out)
    out = pipeline._transcribe_with_fallback(cloudish, sandbox / "x.wav")
    assert out == local_out


def test_local_failure_still_propagates(sandbox):
    localish = types.SimpleNamespace(
        transcribe=lambda wav, progress=None: (_ for _ in ()).throw(
            RuntimeError("model broken")))
    with pytest.raises(RuntimeError, match="model broken"):
        pipeline._transcribe_with_fallback(localish, sandbox / "x.wav")


def test_api_key_prefers_env_file_over_process_env(sandbox, monkeypatch):
    # hermetic against the HOST: importing run_batch loads the machine's real
    # stt.env into os.environ, so a genuinely-configured provider key (the
    # panel writes one when the user adds theirs) must not leak in here
    monkeypatch.delenv("STT_ELEVENLABS_KEY", raising=False)
    monkeypatch.setenv("STT_OPENAI_KEY", "from-process")
    assert asr_cloud.api_key("openai") == "from-process"
    (config.PROJECT_DIR / "stt.env").write_text("STT_OPENAI_KEY=from-file\n")
    assert asr_cloud.api_key("openai") == "from-file"
    assert asr_cloud.available("openai")
    assert not asr_cloud.available("scribe")


def test_openai_size_cap_is_a_clear_error(sandbox, monkeypatch, tmp_path):
    """A compressed upload over OpenAI's 25 MB cap must raise a readable
    error BEFORE any HTTP call (the pipeline turns it into a local
    fallback) — not surface as an opaque 413."""
    import requests

    big = tmp_path / "big.mp3"
    with open(big, "wb") as f:
        f.seek(asr_cloud.OPENAI_MAX_BYTES + 1)
        f.write(b"\0")
    monkeypatch.setattr(asr_cloud, "_compress_for_upload", lambda wav: big)
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("HTTP call should not happen")))
    monkeypatch.setattr(config, "ASR_BACKEND", "cloud:openai")
    monkeypatch.setenv("STT_OPENAI_KEY", "sk-test")
    with pytest.raises(RuntimeError, match="25 MB"):
        asr_cloud.transcribe(sandbox / "x.wav")


def test_compress_for_upload_does_not_leak_fds(sandbox, tmp_path, monkeypatch):
    """mkstemp's fd must be closed: repeated cloud transcriptions in one worker
    process must not accumulate open descriptors (256-fd limit on macOS)."""
    import os
    import subprocess

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))

    def fd_count():
        return len(os.listdir("/dev/fd"))

    wav = tmp_path / "t.wav"
    wav.write_bytes(b"pcm")
    baseline = fd_count()
    for _ in range(20):
        out = asr_cloud._compress_for_upload(wav)
        out.unlink(missing_ok=True)
    # the reserved temp path is fine to leak; an open fd per call is the bug
    assert fd_count() - baseline <= 2


def test_compress_for_upload_produces_small_mp3(sandbox, tmp_path):
    """Real ffmpeg round-trip: the upload artifact is an MP3 (the one format
    all three providers document) far smaller than the source WAV."""
    import subprocess

    from stt.audio import FFMPEG
    wav = tmp_path / "t.wav"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=10", "-ar", "16000",
                    "-ac", "1", str(wav)], check=True, capture_output=True)
    out = asr_cloud._compress_for_upload(wav)
    try:
        assert out.suffix == ".mp3"
        assert 0 < out.stat().st_size < wav.stat().st_size / 3
    finally:
        out.unlink(missing_ok=True)
