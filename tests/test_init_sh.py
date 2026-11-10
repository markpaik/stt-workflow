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
