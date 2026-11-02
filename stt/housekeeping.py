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
from pathlib import Path

from . import config

LOG_MAX_BYTES = int(os.environ.get("STT_LOG_MAX_BYTES", str(10 * 1000 * 1000)))
LOG_KEEP = 3
CALIBRATION_MAX_BYTES = int(os.environ.get("STT_CALIBRATION_MAX_BYTES",
                                           str(10 * 1000 * 1000)))


def _gz_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    with open(src, "rb") as fi, gzip.open(tmp, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)


def _truncate(p: Path) -> None:
    with open(p, "r+b") as fh:
        fh.truncate(0)


def rotate(path, max_bytes: int, keep: int, dry_run=False) -> bool:
    """Rotate one file when it is at or over `max_bytes`. Returns whether
    it rotated (or would, on a dry run)."""
    p = Path(path)
    try:
        if p.stat().st_size < max_bytes:
            return False
    except OSError:
        return False
    if dry_run:
        return True
    gens = [p.with_name(f"{p.name}.{i}.gz") for i in range(1, keep + 1)]
    gens[-1].unlink(missing_ok=True)
    for i in range(keep - 1, 0, -1):
        if gens[i - 1].exists():
            os.replace(gens[i - 1], gens[i])
    _gz_copy(p, gens[0])
    _truncate(p)
    return True


def log_files(log_dir=None) -> list:
    d = Path(log_dir or config.LOG_DIR)
    try:
        return sorted(f for f in d.iterdir()
                      if f.is_file() and not f.name.endswith(".gz")
                      and not f.name.startswith("."))
    except OSError:
        return []


def rotate_logs(log_dir=None, dry_run=False) -> list:
    """The logs that rotated (or would)."""
    return [f.name for f in log_files(log_dir)
            if rotate(f, LOG_MAX_BYTES, LOG_KEEP, dry_run=dry_run)]


def cap_calibration(path=None, dry_run=False) -> bool:
    return rotate(Path(path or config.CALIBRATION_LOG), CALIBRATION_MAX_BYTES, 1,
                  dry_run=dry_run)


def rotate_all(dry_run=False) -> dict:
    """Never raises: housekeeping must not stop a batch."""
    out = {"logs": [], "calibration": False}
    try:
        out["logs"] = rotate_logs(dry_run=dry_run)
    except Exception:
        pass
    try:
        out["calibration"] = cap_calibration(dry_run=dry_run)
    except Exception:
        pass
    return out
