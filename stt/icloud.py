"""Force-download iCloud 'dataless' placeholder files before reading them.

Since macOS Sonoma, un-downloaded iCloud files are invisible dataless
placeholders (no more `.name.icloud` stubs): they report full size but zero
allocated blocks until read. A batch job that opens one without materializing
it gets empty/partial audio and a garbage transcript, with no obvious error.
"""
import subprocess
import time
from pathlib import Path


def is_dataless(path: Path) -> bool:
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return False
    # A dehydrated file reports a size but has no data blocks on disk yet.
    return st.st_size > 0 and st.st_blocks == 0


def _fully_present(path: Path) -> bool:
    """st_blocks > 0 passes on the FIRST CHUNK of a partial download — require the
    allocated blocks to cover the full reported size."""
    try:
        st = Path(path).stat()
    except FileNotFoundError:
        return False
    return st.st_size > 0 and (st.st_blocks * 512) >= st.st_size


def materialize(path: Path, timeout: float = 900.0, poll: float = 1.0) -> bool:
    """Trigger the iCloud download and block until ALL bytes are on disk.

    `brctl download` returns before the fetch completes, so we poll until the
    allocated blocks cover the file size AND (size, blocks) are stable across two
    consecutive polls — feeding a partially-downloaded file to ffmpeg yields a
    truncated wav and a silently short transcript.
    """
    path = Path(path)

    def _stat_sig():
        st = path.stat()
        return (st.st_size, st.st_blocks)

    if _fully_present(path):
        return True
    subprocess.run(["/usr/bin/brctl", "download", str(path)],
                   check=False, capture_output=True)
    # monotonic: a sleep/lid-close mid-download must not make wall-clock time
    # jump on wake and spuriously blow the deadline for a download that was
    # actually fine (the same bug class already fixed elsewhere — rates.py,
    # run_batch.py's report() — for the identical reason)
    deadline = time.monotonic() + timeout
    prev = None
    while time.monotonic() < deadline:
        if _fully_present(path):
            sig = _stat_sig()
            if sig == prev:
                return True
            prev = sig
        else:
            prev = None
        time.sleep(max(poll, 2.0) if prev else poll)
    return _fully_present(path)
