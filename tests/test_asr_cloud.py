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
    monkeypatch.setenv("STT_OPENAI_KEY", "from-process")
    assert asr_cloud.api_key("openai") == "from-process"
    (config.PROJECT_DIR / "stt.env").write_text("STT_OPENAI_KEY=from-file\n")
    assert asr_cloud.api_key("openai") == "from-file"
    assert asr_cloud.available("openai")
    assert not asr_cloud.available("scribe")
