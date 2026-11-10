"""Size caps for the files that only ever grow (unified plan, Phase 3
step 7, and design-files.md section 5).

- Every file in logs/ rotates at LOG_MAX_BYTES and keeps LOG_KEEP gzipped
  generations: name.1.gz (newest) to name.3.gz.
- calibration.jsonl is capped at CALIBRATION_MAX_BYTES and keeps one gzip,
  calibration.jsonl.1.gz. No production code reads it. It grew 28 MB in
  four months.

Rotation is copy-then-truncate, never a rename. launchd opens a job's
StandardOutPath and StandardErrorPath once and keeps writing to that inode,
so a renamed log would keep growing under its new name. The writers append,
so after the truncate their next line lands at the start of the file. A
line written between the copy and the truncate can be lost, which is the
known cost of this method.

The batch calls rotate_all() at start. The doctor's `fix` calls it too.
Each function takes a dry_run flag and then only reports.
"""
import gzip
import os
import shutil
import sys
from pathlib import Path

from . import config

LOG_MAX_BYTES = config.env_int("STT_LOG_MAX_BYTES", 10 * 1000 * 1000)
LOG_KEEP = 3
CALIBRATION_MAX_BYTES = config.env_int("STT_CALIBRATION_MAX_BYTES", 10 * 1000 * 1000)


def _gz_write(src: Path, tmp: Path) -> None:
    """Gzip `src` into `tmp` (mode 600). A failure leaves no `tmp`."""
    try:
        with open(src, "rb") as fi, gzip.open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        os.chmod(tmp, 0o600)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def rotate(path, max_bytes: int, keep: int, dry_run=False) -> bool:
    """Rotate one file when it is at or over `max_bytes`. Returns whether
    it rotated (or would, on a dry run).

    Every step that can fail comes before the generations move: the file
    is opened for writing (a log the batch cannot write fails here) and
    copied to a temporary gzip (a full disk fails here). The old order
    shifted the generations first and then failed at the truncate. The log
    stayed over the cap, the next run shifted again, and after `keep` runs
    every generation held the same copy and the older history was gone."""
    p = Path(path)
    try:
        if p.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    if dry_run:
        return True
    gens = [p.with_name(f"{p.name}.{i}.gz") for i in range(1, keep + 1)]
    tmp = gens[0].with_name(gens[0].name + ".tmp")
    with open(p, "r+b") as fh:
        _gz_write(p, tmp)
        gens[-1].unlink(missing_ok=True)
        for i in range(keep - 1, 0, -1):
            if gens[i - 1].exists():
                os.replace(gens[i - 1], gens[i])
        os.replace(tmp, gens[0])
        fh.truncate(0)
    return True


def log_files(log_dir=None) -> list:
    """The logs in `log_dir`: not a gzip generation, not a dotfile, and not
    the temporary gzip rotate() writes. A process killed mid-copy left
    name.1.gz.tmp, which was listed as a log and grew generations of its
    own (c.log.1.gz.tmp.1.gz). The next rotation of that log replaces it."""
    d = Path(log_dir or config.LOG_DIR)
    try:
        return sorted(f for f in d.iterdir()
                      if f.is_file() and not f.name.endswith((".gz", ".gz.tmp"))
                      and not f.name.startswith("."))
    except OSError:
        return []


def _warn(what, e) -> None:
    print(f"warning: could not rotate {what}: {e}", file=sys.stderr, flush=True)


def rotate_logs(log_dir=None, dry_run=False) -> list:
    """The logs that rotated (or would). One log that fails is reported
    and skipped. The first failure ended the loop, rotate_all swallowed it,
    and every later log in sorted order went unrotated with no sign."""
    out = []
    for f in log_files(log_dir):
        try:
            if rotate(f, LOG_MAX_BYTES, LOG_KEEP, dry_run=dry_run):
                out.append(f.name)
        except Exception as e:
            _warn(f"logs/{f.name}", e)
    return out


def cap_calibration(path=None, dry_run=False) -> bool:
    return rotate(Path(path or config.CALIBRATION_LOG), CALIBRATION_MAX_BYTES, 1,
                  dry_run=dry_run)


def rotate_all(dry_run=False) -> dict:
    """Never raises: housekeeping must not stop a batch."""
    out = {"logs": [], "calibration": False}
    try:
        out["logs"] = rotate_logs(dry_run=dry_run)
    except Exception as e:
        _warn("logs/", e)
    try:
        out["calibration"] = cap_calibration(dry_run=dry_run)
    except Exception as e:
        _warn("calibration.jsonl", e)
    return out
