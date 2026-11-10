"""run.sh must load stt.env without executing it as shell, so the default
folder paths (which contain spaces) don't abort it under set -euo pipefail."""
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def run_sh(tmp_path):
    """A hermetic copy of run.sh in a tmp dir (symlinking the real .venv) so the
    test writes its own stt.env instead of clobbering the repo's."""
    if not (REPO / ".venv" / "bin" / "python").exists():
        pytest.skip("no .venv/bin/python to exec")
    script = tmp_path / "run.sh"
    script.write_text((REPO / "run.sh").read_text())
    script.chmod(0o755)
    (tmp_path / ".venv").symlink_to(REPO / ".venv")
    return tmp_path


def test_env_with_space_in_path_loads_and_dispatch_runs(run_sh):
    space_path = "/Users/x/Library/Mobile Documents/com~apple~CloudDocs/Voice Recordings"
    (run_sh / "stt.env").write_text(
        f"# a comment\nSTT_ICLOUD_DIR={space_path}\nHF_TOKEN=abc\n")
    r = subprocess.run(
        [str(run_sh / "run.sh"), "py", "-c",
         "import os,sys; sys.stdout.write(os.environ.get('STT_ICLOUD_DIR',''))"],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout == space_path


def test_under_stt_home_the_sandbox_stt_env_is_loaded_not_the_repo_one(run_sh, tmp_path):
    (run_sh / "stt.env").write_text("STT_ICLOUD_DIR=/real/icloud\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "stt.env").write_text("STT_ZZ_HOME=yes\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("STT_ICLOUD_DIR", "STT_PROJECT_DIR")}
    env["STT_HOME"] = str(home)
    r = subprocess.run(
        [str(run_sh / "run.sh"), "py", "-c",
         "import os,sys; sys.stdout.write(os.environ.get('STT_ICLOUD_DIR','-')"
         " + '|' + os.environ.get('STT_ZZ_HOME','-'))"],
        capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "-|yes"


def _bare_copy(tmp_path):
    """run.sh in a folder with no .venv: a fresh clone before init.sh."""
    script = tmp_path / "run.sh"
    script.write_text((REPO / "run.sh").read_text())
    script.chmod(0o755)
    return script


def test_a_fresh_clone_says_to_run_init_first(tmp_path):
    """Before init.sh there is no .venv, and run.sh failed with the shell's
    bare '.venv/bin/python: No such file or directory'."""
    env = {k: v for k, v in os.environ.items() if k not in ("STT_HOME", "STT_PROJECT_DIR")}
    r = subprocess.run([str(_bare_copy(tmp_path)), "batch", "--dry-run"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "run ./init.sh first" in r.stderr
    assert "No such file or directory" not in r.stderr


def test_an_unknown_command_still_prints_the_usage(tmp_path):
    r = subprocess.run([str(_bare_copy(tmp_path)), "nonsense"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and r.stdout.startswith("usage: run.sh")


def _reqs(name):
    return [ln.strip() for ln in (REPO / name).read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_requirements_txt_installs_the_pipeline_and_no_test_packages():
    """init.sh installs requirements.txt for every user. It carried pytest,
    and labeled anthropic 'Optional' while it installed it anyway. Test and
    accuracy packages are in requirements-dev.txt now, and the anthropic
    comment says why the client is there."""
    core = _reqs("requirements.txt")
    names = {re.split(r"[<>=!~ ]", ln, 1)[0].lower() for ln in core}
    assert not names & {"pytest", "jiwer", "meeteval"}
    assert "anthropic" in names
    text = (REPO / "requirements.txt").read_text()
    assert "Optional cloud" not in text
    dev = _reqs("requirements-dev.txt")
    assert dev[0] == "-r requirements.txt"
    assert any(ln.startswith("pytest") for ln in dev)


def test_run_sh_test_says_how_to_install_pytest(tmp_path):
    """With pytest left out of requirements.txt, ./run.sh test on a plain
    install names the dev requirements instead of 'No module named pytest'."""
    script = _bare_copy(tmp_path)
    fake = tmp_path / ".venv" / "bin" / "python"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/bash\n"
                    "[ \"$1\" = -c ] && [ \"$2\" = 'import pytest' ] && exit 1\n"
                    "echo ran-pytest\n")
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("STT_HOME", "STT_PROJECT_DIR")}
    r = subprocess.run([str(script), "test"], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "requirements-dev.txt" in r.stderr and "ran-pytest" not in r.stdout
