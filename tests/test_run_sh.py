"""run.sh must load stt.env without executing it as shell, so the default
folder paths (which contain spaces) don't abort it under set -euo pipefail."""
import os
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
