"""The order the queue runs in: the shortest meeting first.

A 20-minute check-in should not wait behind three multi-hour board meetings
that happen to sort first by name. run_batch picks the shortest file left
before every file it starts, and the panel lists the waiting files in that
same order, so both read their lengths from here.

A length is exact (ffprobe) when the file's bytes are local. A dataless iCloud
placeholder gets an estimate from its size (~1 MB/min for voice memos, ~6
MB/min for video): probing one would force its download. Lengths are cached
by path and mtime, so a re-scan after every file costs no probing.
"""
from pathlib import Path

from . import audio, config, icloud

_cache = {}


def duration(p):
    """Audio seconds for a queued file, exact or estimated (see above).
    None when the file is gone or ffprobe cannot read it."""
    p = Path(p)
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if key in _cache:
        return _cache[key]
    dur = None
    try:
        if icloud._fully_present(p):
            dur = audio.duration_sec(p)
        else:
            rate = 6.0 if p.suffix.lower() in config.VIDEO_EXTS else 1.0  # MB/min
            dur = (p.stat().st_size / 1e6) / rate * 60.0
    except Exception:
        pass
    _cache[key] = dur
    return dur


def key(p):
    """The run-order sort key: the length, then the name for ties. A file
    whose length is unknown runs after every file whose length is known."""
    d = duration(p)
    return (d if d is not None else float("inf"), Path(p).name)


def shortest_first(paths):
    return sorted(paths, key=key)
