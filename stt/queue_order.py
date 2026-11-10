"""The order the queue runs in: the shortest meeting first.

A 20-minute check-in should not wait behind three multi-hour board meetings
that happen to sort first by name. run_batch picks the shortest file left
before every file it starts, and the panel lists the waiting files in that
same order, so both read their lengths from here.

A length is exact (ffprobe) when the file's bytes are local. A dataless iCloud
placeholder gets an estimate from its size (~1 MB/min for voice memos, ~6
MB/min for video): probing one would force its download. Lengths are cached
by path, mtime, size and whether the bytes are local, so a re-scan after every
file costs no probing. An iCloud download keeps the mtime, and the "local" part
of the key is what makes the exact length replace the estimate. A failed probe
is tried again after _RETRY_SEC: a cached None sorted the file last for the
life of the panel.
"""
import time
from pathlib import Path

from . import audio, config, icloud

_cache = {}            # path -> (signature, seconds or None, monotonic time)
_RETRY_SEC = 60.0      # a failed probe (None) is tried again after this
_MAX_ENTRIES = 4096    # the panel lives for weeks: one entry per path, capped


def duration(p):
    """Audio seconds for a queued file, exact or estimated (see above).
    None when the file is gone or ffprobe cannot read it."""
    p = Path(p)
    try:
        st = p.stat()
    except OSError:
        return None
    present = icloud._fully_present(p)
    sig = (st.st_mtime, st.st_size, present)
    hit = _cache.get(str(p))
    if hit and hit[0] == sig and (
            hit[1] is not None or time.monotonic() - hit[2] < _RETRY_SEC):
        return hit[1]
    dur = None
    try:
        if present:
            dur = audio.duration_sec(p)
        else:
            rate = 6.0 if p.suffix.lower() in config.VIDEO_EXTS else 1.0  # MB/min
            dur = (st.st_size / 1e6) / rate * 60.0
    except Exception:
        pass
    if len(_cache) >= _MAX_ENTRIES and str(p) not in _cache:
        _cache.clear()
    _cache[str(p)] = (sig, dur, time.monotonic())
    return dur


def key(p):
    """The run-order sort key: the length, then the name for ties. A file
    whose length is unknown runs after every file whose length is known."""
    d = duration(p)
    return (d if d is not None else float("inf"), Path(p).name)


def shortest_first(paths):
    return sorted(paths, key=key)
