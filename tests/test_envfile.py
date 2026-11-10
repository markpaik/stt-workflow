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

def _run_llm_step(tmp_path, cached: bool):
    """setup_local_llm() from a copy of init.sh, with a fake uv and a fake
    .venv-llm python that log their arguments. Nothing is installed or
    downloaded, and Ollama is not on the PATH."""
    shutil.copy(REPO / "init.sh", tmp_path / "init.sh")
    bin_ = tmp_path / "fakebin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    fake_py = ("#!/bin/bash\n"
               f'echo "py $*" >> "{log}"\n'
               + ('exit 0\n' if cached else
                  'case "$*" in *local_files_only*) exit 1 ;; esac\nexit 0\n'))
    (bin_ / "uv").write_text(
        "#!/bin/bash\n"
        f'echo "uv $*" >> "{log}"\n'
        'if [ "$1" = venv ]; then d="${@: -1}"; mkdir -p "$d/bin"; '
        f"cat > \"$d/bin/python\" <<'PYEOF'\n{fake_py}PYEOF\n"
        'chmod +x "$d/bin/python"; fi\n')
    (bin_ / "uv").chmod(0o755)
    sh = (f'source "{tmp_path}/init.sh"; '
          f'yes_no() {{ echo "ask $1" >> "{log}"; return 0; }}; setup_local_llm')
    r = subprocess.run(["/bin/bash", "-c", sh], capture_output=True, text=True,
                       env=_clean_env(HOME=str(tmp_path / "home"),
                                      PATH=f"{bin_}:/usr/bin:/bin"))
    assert r.returncode == 0, r.stderr
    return r.stdout, log.read_text().splitlines()


def test_init_downloads_the_fallback_weights_it_promises(tmp_path):
    """The prompt said '~4.5 GB, fully offline' but init.sh installed only
    the mlx-lm package: the Qwen3-8B weights came down on the first
    description. It now offers the download, of the model summarize.py
    runs, and names Ollama's model as the preferred one."""
    from stt import summarize
    out, calls = _run_llm_step(tmp_path, cached=False)
    assert any(c.startswith("uv venv --python 3.12") for c in calls)
    assert any("mlx-lm" in c for c in calls if c.startswith("uv pip install"))
    downloads = [c for c in calls if c.startswith("py ") and "local_files_only" not in c]
    assert len(downloads) == 1 and f"d('{summarize.MODEL}')" in downloads[0]
    assert summarize.OLLAMA_MODEL in out and "ollama pull" in out
    assert "fully offline" not in (REPO / "init.sh").read_text()


def test_init_skips_the_download_when_the_weights_are_there(tmp_path):
    out, calls = _run_llm_step(tmp_path, cached=True)
    assert [c for c in calls if c.startswith("py ")] == \
        [c for c in calls if "local_files_only" in c]
    assert not any("Download the Qwen3-8B" in c for c in calls)
    assert "already downloaded" in out
