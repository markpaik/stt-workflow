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


# ---------- the generated launchd plists ----------

def _write_plists(base: Path, home: Path, env_text: str = ""):
    """Source setup.sh from a copy at `base` and write both plists into a
    throwaway HOME. launchctl is never called: only the writers run."""
    base.mkdir(parents=True)
    shutil.copy(REPO / "setup.sh", base / "setup.sh")
    if env_text:
        (base / "stt.env").write_text(env_text)
    sh = f'source "{base}/setup.sh"; write_batch_plist; write_gui_plist'
    r = subprocess.run(["/bin/bash", "-c", sh], capture_output=True, text=True,
                       env=_clean_env(HOME=str(home)))
    assert r.returncode == 0, r.stderr
    agents = home / "Library" / "LaunchAgents"
    return (agents / "com.stt-workflow.batch.plist",
            agents / "com.stt-workflow.menubar.plist")


def test_a_path_with_an_ampersand_makes_valid_plists(tmp_path):
    """setup.sh wrote paths into the plist XML unescaped. A checkout or a
    watched folder named 'R & D' made a plist that plutil -lint rejects
    ('unknown ampersand-escape sequence'), so launchctl would not load the
    nightly batch or the menu bar."""
    import plistlib
    base = tmp_path / "R & D <stt>"
    src = tmp_path / "Recordings & Notes"
    rec = tmp_path / "rec <1>"
    batch, gui = _write_plists(base, tmp_path / "home",
                               f"STT_ICLOUD_DIR={src}\nSTT_RECORDINGS_DIR={rec}\n")
    for p in (batch, gui):
        r = subprocess.run(["/usr/bin/plutil", "-lint", str(p)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
    b = plistlib.loads(batch.read_bytes())
    assert b["ProgramArguments"][3] == f"{base}/.venv/bin/python"
    assert b["WorkingDirectory"] == str(base)
    assert b["EnvironmentVariables"]["PYTHONPATH"] == str(base)
    assert b["WatchPaths"][:2] == [str(src), str(rec)]
    assert b["StandardErrorPath"] == f"{base}/logs/stt.err.log"
    g = plistlib.loads(gui.read_bytes())
    assert g["ProgramArguments"] == [f"{base}/.venv/bin/python", f"{base}/gui/menubar.py"]


def test_a_plain_path_still_makes_the_same_plists(tmp_path):
    import plistlib
    base = tmp_path / "stt"
    batch, gui = _write_plists(base, tmp_path / "home")
    b = plistlib.loads(batch.read_bytes())
    assert b["Label"] == "com.stt-workflow.batch"
    assert b["WorkingDirectory"] == str(base)
    assert b["WatchPaths"][0].endswith("com~apple~CloudDocs/Voice Recordings")
    assert plistlib.loads(gui.read_bytes())["Label"] == "com.stt-workflow.menubar"


# ---------- init.sh writes stt.env ----------

def _env_set(tmp_path, *pairs, start=""):
    shutil.copy(REPO / "init.sh", tmp_path / "init.sh")
    envf = tmp_path / "stt.env"
    envf.write_text(start)
    calls = "; ".join(f"env_set {_q(k)} {_q(v)}" for k, v in pairs)
    sh = f'source "{tmp_path}/init.sh"; ENVF={_q(str(envf))}; {calls}'
    r = subprocess.run(["/bin/bash", "-c", sh], capture_output=True, text=True,
                       env=_clean_env(HOME=str(tmp_path / "home")))
    assert r.returncode == 0, r.stderr
    return envf


def _q(s):
    import shlex
    return shlex.quote(s)


@pytest.mark.parametrize("value", ["/Users/x/R & D", "/a/b|c", "a\\nb",
                                   "/Users/x/%s 100%", "/a/$HOME/`x`"])
def test_init_writes_a_value_exactly_as_given(tmp_path, value):
    """env_set rebuilt the line with sed. On a second run (the key already
    there), '&' expanded to the matched text, '|' aborted sed with 'bad
    flag in substitute command', and a backslash-n became a newline."""
    envf = _env_set(tmp_path, ("STT_ICLOUD_DIR", "/a/old"), ("STT_ICLOUD_DIR", value),
                    start="# keep me\nHF_TOKEN=hf_synthetic\n")
    assert envf.read_text() == f"# keep me\nHF_TOKEN=hf_synthetic\nSTT_ICLOUD_DIR={value}\n"
    assert envfile.parse(envf)["STT_ICLOUD_DIR"] == value
    assert oct(envf.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.glob("stt.env*")) == ["stt.env"]


def test_init_updates_a_key_in_place_and_appends_a_new_one(tmp_path):
    envf = _env_set(tmp_path, ("STT_MEETINGS_DIR", "/new/meetings"), ("NEW_KEY", "v"),
                    start="A=1\nSTT_MEETINGS_DIR=/old\nSTT_MEETINGS_DIR_X=keep\nB=2")
    assert envf.read_text() == ("A=1\nSTT_MEETINGS_DIR=/new/meetings\n"
                                "STT_MEETINGS_DIR_X=keep\nB=2\nNEW_KEY=v\n")


# ---------- init.sh's local model step ----------

def _run_llm_step(tmp_path, venv: bool, cached: bool = False):
    """setup_local_llm() from a copy of init.sh, with a fake uv that logs
    its arguments and, when `venv`, a .venv-llm already in place whose fake
    python logs its own. Nothing is installed or downloaded, and Ollama is
    not on the PATH."""
    shutil.copy(REPO / "init.sh", tmp_path / "init.sh")
    bin_ = tmp_path / "fakebin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    log.write_text("")
    (bin_ / "uv").write_text(f'#!/bin/bash\necho "uv $*" >> "{log}"\n')
    (bin_ / "uv").chmod(0o755)
    if venv:
        py = tmp_path / ".venv-llm" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("#!/bin/bash\n"
                      f'echo "py $*" >> "{log}"\n'
                      + ('exit 0\n' if cached else
                         'case "$*" in *local_files_only*) exit 1 ;; esac\nexit 0\n'))
        py.chmod(0o755)
    sh = (f'source "{tmp_path}/init.sh"; '
          f'yes_no() {{ echo "ask $1" >> "{log}"; return 0; }}; setup_local_llm')
    r = subprocess.run(["/bin/bash", "-c", sh], capture_output=True, text=True,
                       env=_clean_env(HOME=str(tmp_path / "home"),
                                      PATH=f"{bin_}:/usr/bin:/bin"))
    assert r.returncode == 0, r.stderr
    return r.stdout, log.read_text().splitlines()


def test_init_never_sets_up_or_offers_the_optional_mlx_fallback(tmp_path):
    """The MLX fallback is optional and not installed by default: without
    .venv-llm the step installs nothing, asks nothing, and names no MLX
    setup. It names Ollama's model and how to pull it."""
    from stt import summarize
    out, calls = _run_llm_step(tmp_path, venv=False)
    assert calls == []                       # no uv call, no question asked
    assert not (tmp_path / ".venv-llm").exists()
    assert summarize.OLLAMA_MODEL in out and "ollama pull" in out
    for word in ("MLX", ".venv-llm", "Qwen3-8B", "4.5 GB"):
        assert word not in out, word


def test_init_downloads_the_weights_of_a_fallback_already_installed(tmp_path):
    """The prompt said '~4.5 GB, fully offline' but the mlx-lm install has
    no weights: they came down on the first description. A .venv-llm put
    in by hand still gets the offer, of the model summarize.py runs."""
    from stt import summarize
    out, calls = _run_llm_step(tmp_path, venv=True, cached=False)
    assert not any(c.startswith("uv ") for c in calls)   # it installs nothing
    downloads = [c for c in calls if c.startswith("py ") and "local_files_only" not in c]
    assert len(downloads) == 1 and f"d('{summarize.MODEL}')" in downloads[0]
    assert "Optional MLX fallback (Qwen3-8B) installed" in out
    assert "fully offline" not in (REPO / "init.sh").read_text()


def test_init_skips_the_download_when_the_weights_are_there(tmp_path):
    out, calls = _run_llm_step(tmp_path, venv=True, cached=True)
    assert [c for c in calls if c.startswith("py ")] == \
        [c for c in calls if "local_files_only" in c]
    assert not any("Download the Qwen3-8B" in c for c in calls)
    assert "already downloaded" in out


def test_every_example_setting_reads_as_its_value_once_uncommented(tmp_path):
    """stt.env.example put comments after the values ('1  # 0 to keep
    iCloud originals'). The reader keeps a '#' after a value, so a line
    with only its leading '# ' removed read as a bad value: the doctor
    named three as bad_setting, and STT_MOVE_AFTER_SUCCESS turned moves
    off without a word. Each comment now sits on its own line."""
    import re
    from stt import envfile
    example = (Path(__file__).resolve().parent.parent / "stt.env.example").read_text()
    lines = [ln[2:] for ln in example.splitlines()
             if re.match(r"# [A-Z][A-Z0-9_]*=", ln)]
    assert len(lines) >= 10
    f = tmp_path / "stt.env"
    f.write_text("\n".join(lines) + "\n")
    for key, value in envfile.parse(f).items():
        assert "#" not in value and value == value.strip(), (key, value)
    parsed = envfile.parse(f)
    assert parsed["STT_MOVE_AFTER_SUCCESS"] == "1"
    assert parsed["STT_SPEAKRS_AHC"] == "0.55"
    assert parsed["STT_DIARIZER"] == "speakrs"
