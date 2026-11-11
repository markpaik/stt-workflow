"""init.sh asks before every install, and a run with no terminal must not
read as a Yes to all of them. The test runs only the prompt helpers, cut
out of the script, so a regression can never start a real install."""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INIT = (REPO / "init.sh").read_text()


def _helpers(tmp_path):
    """The ask, yes_no and has_tty definitions from init.sh, in a file."""
    lines = [ln for ln in INIT.splitlines()
             if re.match(r"(ask|yes_no|has_tty)\(\)", ln)]
    assert len(lines) == 3, lines
    f = tmp_path / "helpers.sh"
    f.write_text("\n".join(lines) + "\n")
    return f


def _no_tty(tmp_path, body):
    """`body` in bash with no controlling terminal: a new session, stdin
    from /dev/null (ssh without -t, cron, CI)."""
    sh = f'source "{_helpers(tmp_path)}"; {body}'
    return subprocess.run(["/bin/bash", "-c", sh], capture_output=True, text=True,
                          start_new_session=True, stdin=subprocess.DEVNULL)


def test_with_no_terminal_a_question_is_a_no(tmp_path):
    """ask() read /dev/tty, the read failed, and yes_no() took the empty
    answer as the default Yes: brew installs, the model downloads and the
    LaunchAgents all went ahead unasked."""
    r = _no_tty(tmp_path, 'if yes_no "Install the thing?"; then echo YES; '
                          'else echo NO; fi; has_tty && echo TTY || echo NOTTY')
    assert r.stdout.split() == ["NO", "NOTTY"], (r.stdout, r.stderr)


def test_init_stops_before_step_one_without_a_terminal():
    """The guard sits after the sourcing guard (the tests source the
    helpers) and before the first setup step."""
    src = INIT.index('[ "${BASH_SOURCE[0]}" = "$0" ] || return 0')
    guard = INIT.index("if ! has_tty; then")
    step1 = INIT.index('bold "1/7')
    assert src < guard < step1
    block = INIT[guard:INIT.index("fi\n", guard)]
    assert "exit 1" in block and "ssh -t" in block
    assert "bold " not in INIT[src:guard] and "yes_no" not in INIT[src:guard]


def test_the_skipped_token_text_matches_what_the_batch_does():
    """init.sh said a skipped token meant 'transcription-only' and
    'transcripts will have no speaker labels'. run_batch.py prints
    'Diarization needs a HuggingFace token' and returns 2 at its start,
    and the README says the batch stops there."""
    batch = (REPO / "run_batch.py").read_text()
    gate = batch[batch.index("not config.resolve_hf_token()"):][:200]
    assert "Diarization needs a HuggingFace token" in gate and "return 2" in gate
    assert "without a token the batch stops at its start" in " ".join(
        (REPO / "README.md").read_text().split())
    for stale in ("transcription-only", "no speaker labels"):
        assert stale not in INIT
    assert "the batch does not run until it is set" in INIT
    assert "the batch stops at its start until HF_TOKEN is set" in INIT


# ---------- the Qwen3-ASR environment (steps 3 and 6) ----------

FAKE_UV = r'''#!/bin/bash
echo "uv $*" >> "$FAKE_LOG"
if [ "$1" = venv ]; then
  mkdir -p "$4/bin"
  cat > "$4/bin/python" <<'PYEOF'
#!/bin/bash
echo "python $*" >> "$FAKE_LOG"
if [ "$1" = "-V" ]; then echo "Python 3.12.13"; exit 0; fi
if [ "$1" = "-I" ] && [ "$2" = "-c" ]; then [ -f "$FAKE_MARK" ]; exit $?; fi
if [ "$1" = "-I" ] && [ "$2" = "-" ]; then
  echo "--- script" >> "$FAKE_LOG"; cat >> "$FAKE_LOG"; [ -f "$FAKE_MARK" ]; exit $?
fi
PYEOF
  chmod +x "$4/bin/python"
fi
if [ "$1" = pip ]; then touch "$FAKE_MARK"; fi
'''


def _scratch_init(tmp_path):
    """A copy of init.sh and requirements-qwen.txt in a scratch checkout, a
    fake uv on PATH, and the log both fakes write."""
    co = tmp_path / "checkout"
    co.mkdir()
    (co / "init.sh").write_text(INIT)
    (co / "requirements-qwen.txt").write_text((REPO / "requirements-qwen.txt").read_text())
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "uv").write_text(FAKE_UV)
    (bin_ / "uv").chmod(0o755)
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path),
           "FAKE_LOG": str(tmp_path / "log"), "FAKE_MARK": str(tmp_path / "installed")}
    return co, env


def _run(co, env, body):
    sh = f'source "{co}/init.sh"; {body}'
    return subprocess.run(["/bin/bash", "-c", sh], capture_output=True, text=True,
                          env=env, start_new_session=True, stdin=subprocess.DEVNULL)


def test_init_builds_the_qwen_environment_once_and_never_touches_venv(tmp_path):
    co, env = _scratch_init(tmp_path)
    r = _run(co, env, "setup_qwen_env; echo ONE; setup_qwen_env; echo TWO")
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "log").read_text().splitlines()
    uv = [ln for ln in log if ln.startswith("uv ")]
    assert uv == [f"uv venv --python 3.12 {co}/.venv-qwen",
                  f"uv pip install --python {co}/.venv-qwen/bin/python "
                  f"-r {co}/requirements-qwen.txt"]
    assert not (co / ".venv").exists()
    assert r.stdout.count("Qwen3-ASR environment ready") == 2


def test_init_fetches_both_models_with_mlx_audio_patterns(tmp_path):
    co, env = _scratch_init(tmp_path)
    r = _run(co, env, "setup_qwen_env >/dev/null; qwen_models_cached && echo CACHED; "
                      "fetch_qwen_models && echo FETCHED")
    assert r.returncode == 0, r.stderr
    assert "CACHED" in r.stdout and "FETCHED" in r.stdout
    log = (tmp_path / "log").read_text()
    from stt import config
    call = f"python -I - {config.QWEN_ASR_MODEL} {config.QWEN_ALIGNER_MODEL}"
    assert log.count(call) == 2
    scripts = log.split("--- script")[1:]
    assert "local_files_only=True" in scripts[0]
    for sc in scripts:
        assert "allow_patterns=DEFAULT_ALLOW_PATTERNS" in sc
        assert "from mlx_audio.utils import DEFAULT_ALLOW_PATTERNS" in sc


def test_a_failed_qwen_install_is_a_warning_not_a_stop(tmp_path):
    co, env = _scratch_init(tmp_path)
    (tmp_path / "bin" / "uv").write_text('#!/bin/bash\necho "uv $*" >> "$FAKE_LOG"; exit 1\n')
    r = _run(co, env, "setup_qwen_env; echo rc=$?; fetch_qwen_models; echo rc=$?")
    assert "rc=1" in r.stdout and "uses Parakeet" in r.stdout


def test_the_qwen_install_lines_name_only_the_qwen_environment():
    lines = [ln for ln in INIT.splitlines() if "requirements-qwen" in ln
             and not ln.lstrip().startswith("#")]
    assert lines and all("$QWEN_PY" in ln for ln in lines)
    assert "mlx-audio" not in (REPO / "requirements.txt").read_text()
    assert "mlx-audio==" in (REPO / "requirements-qwen.txt").read_text()
