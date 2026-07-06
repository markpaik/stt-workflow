"""A tiny JSON manifest of processed files, for idempotent re-runs."""
import json
from datetime import datetime
from pathlib import Path

from . import config


def load() -> dict:
    if config.MANIFEST_PATH.exists():
        try:
            return json.loads(config.MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed": {}}


def save(m: dict):
    config.MANIFEST_PATH.write_text(json.dumps(m, indent=2))


def is_processed(m: dict, key: str, mtime: float) -> bool:
    rec = m["processed"].get(key)
    if rec is None or abs(rec.get("mtime", 0) - mtime) >= 1.0:
        return False
    # self-healing: if the transcript outputs were deleted, the work no longer
    # exists — treat the file as new so it can be reprocessed
    core = [o for o in rec.get("outputs", []) if o.endswith((".txt", ".json"))]
    if core and not all(Path(o).exists() for o in core):
        return False
    return True


def mark(m: dict, key: str, mtime: float, outputs: list):
    m["processed"][key] = {
        "mtime": mtime,
        "outputs": [str(o) for o in outputs],
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
