"""stt.env has one set of rules (stt/envfile.py), and every reader keeps
them: config._env_file, run_batch's loader, run.sh, setup.sh and init.sh.
A hand-written FOO="quoted val" kept its quotes in all of them, so a quoted
path named a folder with quote marks in it. Every value here is synthetic."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stt import config, envfile

REPO = Path(__file__).resolve().parent.parent

# (stt.env line, the value its key must read as)
CASES = [
    ('Q_DOUBLE="quoted val"', "quoted val"),
    ("Q_SINGLE='single q'", "single q"),
    ("Q_PLAIN=/Users/x/Voice Recordings", "/Users/x/Voice Recordings"),
    ("Q_MISMATCH=\"half'", "\"half'"),
    ('Q_ONE="', '"'),
    ('Q_EMPTY=""', ""),
    ("  Q_PAD  =   padded value  ", "padded value"),
    ("Q_EQ=a=b", "a=b"),
    ("Q_SPECIAL=R & D | x \\n y", "R & D | x \\n y"),
    ('Q_INNER=say "hi" there', 'say "hi" there'),
    ("Q_TWICE=first", None),          # a later line wins (below)
]
TEXT = "\n".join(["# a comment", "", "NOEQUALS"] + [c[0] for c in CASES]
                 + ["Q_TWICE=second"]) + "\n"
WANT = {line.split("=", 1)[0].strip(): v for line, v in CASES if v is not None}
WANT["Q_TWICE"] = "second"


def test_the_python_parser_keeps_the_rules(tmp_path):
    p = tmp_path / "stt.env"
    p.write_text(TEXT)
    assert envfile.parse(p) == WANT
    assert envfile.parse(tmp_path / "missing.env") == {}


def test_config_reads_stt_env_by_the_rules(sandbox):
    (sandbox / "stt.env").write_text(TEXT)
    assert config._env_file() == WANT


def test_run_batch_loads_stt_env_by_the_rules(tmp_path, monkeypatch):
    import run_batch
    (tmp_path / "stt.env").write_text(TEXT)
    for k in WANT:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("STT_PROJECT_DIR", raising=False)
    monkeypatch.setenv("STT_HOME", str(tmp_path))
    run_batch._load_env_file()
    assert {k: os.environ.get(k) for k in WANT} == WANT
    for k in WANT:
        monkeypatch.delenv(k, raising=False)


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("Q_") and k not in ("STT_HOME", "STT_PROJECT_DIR")}
    env.update(extra)
    return env


def test_run_sh_exports_stt_env_by_the_rules(tmp_path):
    if not (REPO / ".venv" / "bin" / "python").exists() and \
            not Path(sys.executable).exists():
        pytest.skip("no python to exec")
    (tmp_path / "run.sh").write_text((REPO / "run.sh").read_text())
    (tmp_path / "run.sh").chmod(0o755)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").symlink_to(sys.executable)
    (tmp_path / "stt.env").write_text(TEXT + "BAD KEY=x\n9LEAD=x\n")
    code = ("import json, os, sys; sys.stdout.write(json.dumps("
            "{k: v for k, v in os.environ.items() if k.startswith('Q_')}))")
    r = subprocess.run([str(tmp_path / "run.sh"), "py", "-c", code],
                       capture_output=True, text=True, env=_clean_env())
    assert r.returncode == 0, r.stderr
    import json
    assert json.loads(r.stdout) == WANT


@pytest.mark.parametrize("script", ["setup.sh", "init.sh"])
def test_setup_and_init_read_stt_env_by_the_rules(tmp_path, script):
    """Both scripts can be sourced for their helpers: the guard stops them
    before any command (setup.sh) or setup step (init.sh) runs."""
    shutil.copy(REPO / script, tmp_path / script)
    (tmp_path / "stt.env").write_text(TEXT)
    keys = " ".join(WANT)
    sh = (f'source "{tmp_path / script}"; '
          f'for k in {keys}; do printf "%s\\t%s\\n" "$k" "$(env_get "$k")"; done')
    r = subprocess.run(["/bin/bash", "-c", sh], capture_output=True, text=True,
                       env=_clean_env(HOME=str(tmp_path / "home")), cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    got = dict(ln.split("\t", 1) for ln in r.stdout.splitlines())
    assert got == WANT
    assert not (tmp_path / "home").exists(), "sourcing ran no command"


def test_setup_sh_reads_a_quoted_watched_folder(tmp_path):
    shutil.copy(REPO / "setup.sh", tmp_path / "setup.sh")
    (tmp_path / "stt.env").write_text('STT_ICLOUD_DIR="/Users/x/My Recordings"\n')
    r = subprocess.run(["/bin/bash", "-c", f'source "{tmp_path}/setup.sh"; source_dir'],
                       capture_output=True, text=True,
                       env=_clean_env(HOME=str(tmp_path / "home")))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "/Users/x/My Recordings"
