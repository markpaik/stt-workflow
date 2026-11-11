"""config: stt.env parsing, and the settings that warn and keep a default."""
import os

import pytest

from stt import config


def test_env_file_parsing(sandbox):
    (sandbox / "stt.env").write_text(
        "# comment\n"
        "HF_TOKEN=abc\n"
        "  STT_ASR_BACKEND = qwen3 \n"
        "BROKEN LINE\n"
        "EMPTY=\n")
    kv = config._env_file()
    assert kv["HF_TOKEN"] == "abc"
    assert kv["STT_ASR_BACKEND"] == "qwen3"
    assert kv["EMPTY"] == ""
    assert "BROKEN LINE" not in kv


def test_dir_overrides_read_fresh(sandbox):
    (sandbox / "stt.env").write_text(f"STT_MEETINGS_DIR={sandbox}/elsewhere\n")
    assert str(config.meetings_dir()).endswith("/elsewhere")
    (sandbox / "stt.env").write_text("")  # cleared -> falls back to default
    assert config.meetings_dir() == config.MEETINGS_DIR


# ---------- a bad number in stt.env warns and keeps the default ----------

def _import_config(tmp_path, **env):
    """Import stt.config in a fresh interpreter with `env` set. Returns
    (returncode, stdout, stderr)."""
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items()
         if not k.startswith("STT_SPEAKRS") and k != "STT_DIARIZER"}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo), **env})
    r = subprocess.run([sys.executable, "-c",
                        "from stt import config as c; "
                        "print(c.SPEAKRS_AHC, c.SPEAKRS_TIMEOUT_SEC, c.DIARIZER)"],
                       capture_output=True, text=True, env=e, cwd=str(repo), timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr


@pytest.mark.parametrize("name, raw", [
    ("STT_SPEAKRS_AHC", "abc"), ("STT_SPEAKRS_AHC", "-1"), ("STT_SPEAKRS_AHC", "nan"),
    ("STT_SPEAKRS_AHC", "inf"), ("STT_SPEAKRS_TIMEOUT_SEC", "abc"),
    ("STT_SPEAKRS_TIMEOUT_SEC", "0"), ("STT_SPEAKRS_TIMEOUT_SEC", "-5"),
    ("STT_SPEAKRS_TIMEOUT_SEC", "12.5")])
def test_a_bad_speakrs_number_warns_and_keeps_the_default(tmp_path, name, raw):
    """float('') or int('abc') at import stopped every command that imports
    stt.config. -1 or nan passed, then every file fell back to pyannote, and
    a timeout of 0 timed every helper run out at once."""
    rc, out, err = _import_config(tmp_path, **{name: raw})
    assert rc == 0, err
    assert out == "0.55 3600 speakrs"
    assert name in err and "using" in err


def test_an_empty_or_good_speakrs_number_is_quiet(tmp_path):
    rc, out, err = _import_config(tmp_path, STT_SPEAKRS_AHC="", STT_SPEAKRS_TIMEOUT_SEC="")
    assert (rc, out) == (0, "0.55 3600 speakrs") and "warning" not in err
    rc, out, err = _import_config(tmp_path, STT_SPEAKRS_AHC=" 0.6 ", STT_SPEAKRS_TIMEOUT_SEC="900")
    assert (rc, out) == (0, "0.6 900 speakrs") and "warning" not in err



_TYPO_PRINT = ("from stt import config as c, unknowns as u, housekeeping as h; "
               "print(c.NAMING_THRESHOLD, c.REFINE_ID_MIN, c.RECLUSTER_ITERS, "
               "c.BATTERY_FLOOR, c.CHANNEL_FLOOR_DBFS, u.MATCH_MIN, h.LOG_MAX_BYTES)")
_TYPO_DEFAULTS = "0.6 0.6 2 20 -45.0 0.6 10000000"


@pytest.mark.parametrize("name, raw", [
    ("STT_NAMING_THRESHOLD", "abc"), ("STT_NAMING_THRESHOLD", "nan"),
    ("STT_REFINE_ID_MIN", ""), ("STT_RECLUSTER_ITERS", "2.5"),
    ("STT_BATTERY_FLOOR", "20%"), ("STT_CHANNEL_FLOOR_DBFS", "-45dB"),
    ("STT_UNKNOWN_MATCH_MIN", "O.6"), ("STT_LOG_MAX_BYTES", "10MB")])
def test_a_typo_in_any_number_setting_warns_and_keeps_the_default(tmp_path, name, raw):
    """Every other number setting parsed with a bare float() or int() at
    import. STT_NAMING_THRESHOLD=abc, an empty STT_REFINE_ID_MIN=, or
    STT_BATTERY_FLOOR=20% raised ValueError, and the batch, the panel and
    every tool that imports stt.config stopped."""
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo), name: raw})
    r = subprocess.run([sys.executable, "-c", _TYPO_PRINT], capture_output=True,
                       text=True, env=e, cwd=str(repo), timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == _TYPO_DEFAULTS
    if raw:
        assert name in r.stderr and "using" in r.stderr
    else:
        assert "warning" not in r.stderr


def test_a_good_number_setting_still_wins(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo),
              "STT_NAMING_THRESHOLD": " 0.7 ", "STT_RECLUSTER_ITERS": "4",
              "STT_REFINE_JUNK_REPAIR_MAX_CLUSTER_SECS": "1e9",
              "STT_UNKNOWN_MATCH_MIN": "0.65"})
    r = subprocess.run([sys.executable, "-c",
                        "from stt import config as c, unknowns as u; "
                        "print(c.NAMING_THRESHOLD, c.RECLUSTER_ITERS, "
                        "c.REFINE_JUNK_REPAIR_MAX_CLUSTER_SECS, u.MATCH_MIN)"],
                       capture_output=True, text=True, env=e, cwd=str(repo), timeout=60)
    assert (r.returncode, r.stdout.strip()) == (0, "0.7 4 1000000000.0 0.65"), r.stderr
    assert "warning" not in r.stderr


def test_the_doctor_names_a_number_typo(sandbox, monkeypatch):
    (sandbox / "stt.env").write_text("STT_BATTERY_FLOOR=20%\n")
    monkeypatch.delenv("STT_BATTERY_FLOOR", raising=False)
    assert [p[0] for p in config.setting_problems()] == ["STT_BATTERY_FLOOR"]

@pytest.mark.parametrize("raw, want, warns", [
    ("speakr", "speakrs", True), ("pyanote", "speakrs", True), ("cuda", "speakrs", True),
    ("pyannote", "pyannote", False), (" PyAnnote ", "pyannote", False),
    ("speakrs", "speakrs", False), ("", "speakrs", False)])
def test_an_unknown_diarizer_warns_and_keeps_the_default(tmp_path, raw, want, warns):
    """diarize() tests only == "speakrs", so STT_DIARIZER=speakr (a typo)
    switched every file to the slow CPU engine with no message."""
    rc, out, err = _import_config(tmp_path, STT_DIARIZER=raw)
    assert rc == 0, err
    assert out.split()[-1] == want
    assert ("STT_DIARIZER" in err and "speakrs" in err and "pyannote" in err) is warns


def _import_asr(tmp_path, raw):
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    e = {k: v for k, v in os.environ.items() if k != "STT_ASR_BACKEND"}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo), "STT_ASR_BACKEND": raw})
    r = subprocess.run([sys.executable, "-c",
                        "from stt import config as c, pipeline as p; "
                        "print(c.ASR_BACKEND, p._load_asr(False).__name__)"],
                       capture_output=True, text=True, env=e, cwd=str(repo), timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr


@pytest.mark.parametrize("raw, want, mod, warns", [
    ("mlx-whisper", "qwen3", "stt.asr_qwen3", True),
    ("whisper", "qwen3", "stt.asr_qwen3", True),
    ("whisperx", "qwen3", "stt.asr_qwen3", True),
    ("cloud:typo", "qwen3", "stt.asr_qwen3", True),
    (" MLXWhisper ", "qwen3", "stt.asr_qwen3", True),   # the removed engine
    ("mlxwhisper", "qwen3", "stt.asr_qwen3", True),
    ("parakeet", "qwen3", "stt.asr_qwen3", True),       # the removed engine
    (" Parakeet ", "qwen3", "stt.asr_qwen3", True),
    ("qwen3", "qwen3", "stt.asr_qwen3", False),
    ("qwen", "qwen3", "stt.asr_qwen3", True),
    ("cloud:scribe", "cloud:scribe", "stt.asr_cloud", False),
    ("", "qwen3", "stt.asr_qwen3", False)])
def test_an_unknown_asr_backend_warns_and_runs_the_default(tmp_path, raw, want, mod, warns):
    """A typo ('mlx-whisper', 'whisper', or the 'whisperx' stt.env.example
    named) ran the default engine with no message. "mlxwhisper" and
    "parakeet" named engines that are gone: each warns the same way. The
    default is qwen3, the one local engine."""
    rc, out, err = _import_asr(tmp_path, raw)
    assert rc == 0, err
    assert out.split() == [want, mod]
    assert ("STT_ASR_BACKEND" in err and "using qwen3" in err) is warns


@pytest.mark.parametrize("old, removed", [("mlxwhisper", "the Whisper engine was removed"),
                                          ("parakeet", "the Parakeet engine was removed")])
def test_a_removed_engine_left_in_stt_env_warns_once_and_runs_the_default(
        tmp_path, old, removed):
    """An stt.env written before Whisper or Parakeet was removed still says
    STT_ASR_BACKEND=mlxwhisper or =parakeet (run.sh exports it). One
    process warns once, names the removal, and runs the default, qwen3.
    With no Qwen environment here it still estimates as qwen3, the engine
    that will run, and a Qwen3-ASR run has no second opinion."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "stt.env").write_text(
        f"STT_ASR_BACKEND={old}\nSTT_WHISPER_MLX_MODEL=turbo\n")
    e = {k: v for k, v in os.environ.items()
         if k not in ("STT_ASR_BACKEND", "STT_WHISPER_MLX_MODEL")}
    e.update({"STT_HOME": str(tmp_path), "PYTHONPATH": str(repo),
              "STT_ASR_BACKEND": old, "STT_WHISPER_MLX_MODEL": "turbo",
              "STT_QWEN_ASR_PY": str(tmp_path / "no-venv-qwen" / "python")})
    code = ("from stt import config as c, pipeline as p, rates as r, verify as v; "
            "mods = {p._load_asr(False).__name__ for _ in range(3)}; "
            "print(c.ASR_BACKEND, *mods, r.current_asr_key(), "
            "v.secondary_engine(r.current_asr_key()))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=e, cwd=str(repo), timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["qwen3", "stt.asr_qwen3", "qwen3", "None"]
    warned = [ln for ln in r.stderr.splitlines() if "STT_ASR_BACKEND" in ln]
    assert len(warned) == 1, r.stderr
    assert removed in warned[0]
    assert "using qwen3" in warned[0]


def test_the_doctor_names_an_unknown_asr_backend(sandbox, monkeypatch):
    from stt import config
    (sandbox / "stt.env").write_text("STT_ASR_BACKEND=whisper\n")
    monkeypatch.delenv("STT_ASR_BACKEND", raising=False)
    assert [p[0] for p in config.setting_problems()] == ["STT_ASR_BACKEND"]
    (sandbox / "stt.env").write_text("STT_ASR_BACKEND=mlxwhisper\n")
    [(name, why)] = config.setting_problems()
    assert name == "STT_ASR_BACKEND"
    assert "the Whisper engine was removed" in why and "using qwen3" in why
    (sandbox / "stt.env").write_text("STT_ASR_BACKEND=parakeet\n")
    [(name, why)] = config.setting_problems()
    assert name == "STT_ASR_BACKEND"
    assert "the Parakeet engine was removed" in why and "using qwen3" in why
    (sandbox / "stt.env").write_text("STT_ASR_BACKEND=qwen3\n")
    assert config.setting_problems() == []
    from stt import asr_cloud
    assert set(config.CLOUD_ASR) == set(asr_cloud.PROVIDERS)


@pytest.mark.parametrize("value, mod, warns", [
    ("garbage", "stt.asr_qwen3", True),
    ("cloud:bogus", "stt.asr_qwen3", True),
    (" MLXWhisper ", "stt.asr_qwen3", True),
    ("parakeet", "stt.asr_qwen3", True),
    ("qwen3", "stt.asr_qwen3", False)])
def test_a_backend_set_after_load_is_checked_too(sandbox, monkeypatch, capsys,
                                                value, mod, warns):
    """The warning fired only when the config loaded. Code that set
    config.ASR_BACKEND afterward ran the default with no word, and
    'cloud:bogus' reached the cloud module."""
    from stt import pipeline
    monkeypatch.setattr(config, "ASR_BACKEND", value)
    assert pipeline._load_asr().__name__ == mod
    err = capsys.readouterr().err
    assert ("STT_ASR_BACKEND" in err and "using qwen3" in err) is warns
