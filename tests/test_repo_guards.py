"""The ignore rules and the pre-commit hook that keep private data out of git.

Every path here is synthetic. The hook tests run in a throwaway repository, so
they never touch this checkout's own hooks folder.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "tools" / "install_precommit_hook.sh"


def _git(cwd, *args, check=True):
    # a commit hook exports GIT_DIR and friends; they must not redirect these calls
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                           *args], cwd=cwd, env=env, check=check,
                          capture_output=True, text=True)


@pytest.mark.parametrize("path", [
    "centroid_index.json", "centroid_index.npz", "stop_after_file.flag",
    "search.db", "search.db-wal", "search.db-shm", "manifest_retired.json",
    "review_metrics.jsonl", "panel_seen.json", "group_overrides.json",
    "ask_threads/x.json", "doctor.json", "calibration.jsonl.1.gz",
    "calibration.jsonl.1.gz.tmp",
])
def test_root_state_files_are_ignored(path):
    assert _git(REPO, "check-ignore", "-q", "--no-index", path, check=False).returncode == 0


@pytest.mark.parametrize("path", [
    "stt.env.bak", "stt.env.old", ".env", "local.env", ".hf_token", "hf_token.txt",
    "exports/x.docx", "Meeting.docx", "Meeting.pdf", "recorder.log", "gui/x.log",
    "tuning/out.json", "status.json.bak", "manifest.json.bak", "meetings/x/x.txt",
    "transcripts/x.txt", ".venv", ".env.local", "secrets.txt", "id_rsa", "key.pem",
    "x.npy", "work2/Person A.npy", "credentials.json",
])
def test_secret_backup_export_and_log_names_are_ignored(path):
    """A copy of stt.env, a saved token, an export into the checkout or a
    log must not be staged by 'git add -A'."""
    assert _git(REPO, "check-ignore", "-q", "--no-index", path, check=False).returncode == 0


@pytest.mark.parametrize("path", ["tests/fixture.json", "gui/static/data.json",
                                  "tools/token_contrast.py", "docs/guide.pdf",
                                  "tuning/eval.py",
                                  "stt/notes.flag", "stt.env.example",
                                  "tests/test_secrets.py", "stt/credentials.py"])
def test_root_rules_stay_at_the_root(path):
    assert _git(REPO, "check-ignore", "-q", "--no-index", path, check=False).returncode == 1


def test_no_tracked_file_matches_a_root_rule():
    tracked = _git(REPO, "ls-files").stdout.splitlines()
    assert _git(REPO, "check-ignore", "--no-index", *tracked, check=False).stdout == ""


@pytest.fixture
def hooked(tmp_path):
    _git(tmp_path, "init", "-q")
    subprocess.run(["sh", str(INSTALLER)], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _commit(repo, name):
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"synthetic")
    _git(repo, "add", "-f", name)
    return _git(repo, "commit", "-q", "-m", "t", check=False)


def test_installer_writes_an_executable_hook(hooked):
    hook = hooked / ".git" / "hooks" / "pre-commit"
    assert hook.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize("name", [
    "x.npz", "sub/x.npz", "x.jsonl", "x.db", "x.db-wal", "x.db-shm", "sub/x.sqlite.db",
    "x.m4a", "X.M4A", "x.wav", "x.mp3", "x.aiff", "x.aif", "x.mp4", "x.mov", "x.caf",
    "x.flac", "x.ogg", "x.aac", "x y.m4a",
])
def test_hook_refuses_data_files(hooked, name):
    r = _commit(hooked, name)
    assert r.returncode == 1
    assert name in r.stderr
    assert _git(hooked, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode != 0


@pytest.mark.parametrize("name", ["x.py", "x.json", "sub/notes.md", "db/x.txt", "x.dmg"])
def test_hook_allows_other_files(hooked, name):
    assert _commit(hooked, name).returncode == 0


def test_hook_allows_removing_a_data_file(tmp_path):
    # a file committed before the guard existed must stay removable
    _git(tmp_path, "init", "-q")
    assert _commit(tmp_path, "old.npz").returncode == 0
    subprocess.run(["sh", str(INSTALLER)], cwd=tmp_path, check=True, capture_output=True)
    _git(tmp_path, "rm", "-q", "old.npz")
    assert _git(tmp_path, "commit", "-q", "-m", "rm", check=False).returncode == 0


def test_installer_is_repeatable(hooked):
    subprocess.run(["sh", str(INSTALLER)], cwd=hooked, check=True, capture_output=True)
    assert _commit(hooked, "x.npz").returncode == 1


def test_installer_keeps_a_foreign_hook(tmp_path):
    _git(tmp_path, "init", "-q")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    r = subprocess.run(["sh", str(INSTALLER)], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 1
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"
