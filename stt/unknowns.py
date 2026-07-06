"""Global registry of UNKNOWN speakers.

Without this, every meeting's strangers restart at "Speaker 1". Here, each unnamed
cluster's centroid is matched against previously-seen unknown voices: a returning
unknown keeps their global number ("Speaker 7" is the same person in every
meeting); a brand-new voice gets the next number. When you finally name Speaker 7
via the GUI, their samples move into the enrolled library and every past meeting
is relabeled.

Registry: voiceprints/unknowns.json + voiceprints/U###.npy sample stacks.
Open-set matching mirrors identify.py (threshold + margin, max-cosine over samples).
"""
import json
from datetime import datetime

import numpy as np

from . import config
from .identify import _l2, score_against

MATCH_MIN = float(__import__("os").environ.get("STT_UNKNOWN_MATCH_MIN", "0.60"))
MATCH_MARGIN = float(__import__("os").environ.get("STT_UNKNOWN_MATCH_MARGIN", "0.10"))
MAX_SAMPLES = 5


def _path():
    return config.VOICEPRINTS_DIR / "unknowns.json"


def load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"next": 1, "speakers": {}}


def save(reg: dict):
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(reg, indent=2))


def _samples(reg, uid):
    f = config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"]
    if not f.exists():
        return None
    arr = np.load(f)
    return arr.reshape(1, -1) if arr.ndim == 1 else arr


def display(uid: str) -> str:
    """'U007' -> 'Speaker 7' (the stable global number)."""
    try:
        return f"Speaker {int(uid[1:])}"
    except (ValueError, TypeError):
        return str(uid)


def assign(cent_emb: dict, cluster_names: dict, meeting: str) -> dict:
    """For each UNNAMED cluster with a usable centroid, return {label: global_uid},
    matching returning unknowns and registering new ones."""
    reg = load()
    out = {}
    for label, vec in cent_emb.items():
        if cluster_names.get(label):
            continue  # named person, not an unknown
        v = np.asarray(vec, float)
        if not np.isfinite(v).all() or np.linalg.norm(v) == 0:
            continue
        scored = []
        for uid in reg["speakers"]:
            s = _samples(reg, uid)
            if s is not None:
                scored.append((score_against(v, s), uid))
        scored.sort(reverse=True)
        best, uid = scored[0] if scored else (-1.0, None)
        second = scored[1][0] if len(scored) > 1 else -1.0
        if uid is not None and best >= MATCH_MIN and (best - second) >= MATCH_MARGIN:
            # returning unknown: add this meeting's sample ONCE (a relabel of the
            # same meeting must not stack duplicate centroids)
            mts = reg["speakers"][uid].setdefault("meetings", [])
            if meeting not in mts:
                s = _samples(reg, uid)
                arr = np.vstack([s, _l2(v)])[-MAX_SAMPLES:]
                np.save(config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"], arr)
                mts.append(meeting)
        else:
            # lowest free number: after unknowns get named, new voices start
            # back at Speaker 1 instead of counting up forever
            taken = {int(u[1:]) for u in reg["speakers"] if u[1:].isdigit()}
            n = 1
            while n in taken:
                n += 1
            uid = f"U{n:03d}"
            reg["next"] = max(reg.get("next", 1), n + 1)
            fname = f"{uid}.npy"
            np.save(config.VOICEPRINTS_DIR / fname, _l2(v).reshape(1, -1))
            reg["speakers"][uid] = {"file": fname, "meetings": [meeting],
                                    "created": datetime.now().isoformat(timespec="seconds")}
        out[label] = uid
    save(reg)
    return out


def promote(uid: str, name: str) -> bool:
    """Name an unknown: move their samples into the enrolled library and retire the
    unknown id. Caller should then relabel past meetings."""
    from . import identify
    reg = load()
    if uid not in reg["speakers"]:
        return False
    s = _samples(reg, uid)
    if s is None:
        return False
    # carry meeting provenance along (samples were appended one per meeting;
    # align best-effort — the sample window and meeting list can drift apart)
    meetings = reg["speakers"][uid].get("meetings", [])
    for i, row in enumerate(s):
        src = meetings[i] if i < len(meetings) else (meetings[-1] if meetings else None)
        identify.enroll(name, row, source=src)
    f = config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"]
    f.unlink(missing_ok=True)
    del reg["speakers"][uid]
    save(reg)
    return True


def merge(uid_src: str, uid_dst: str) -> bool:
    """Two unknown numbers that are really one person: fold src's samples and
    meeting history into dst, retire src."""
    reg = load()
    if uid_src not in reg["speakers"] or uid_dst not in reg["speakers"] or uid_src == uid_dst:
        return False
    s, d = _samples(reg, uid_src), _samples(reg, uid_dst)
    if d is None:
        return False
    if s is not None:
        arr = np.vstack([d, s])[-MAX_SAMPLES:]
        np.save(config.VOICEPRINTS_DIR / reg["speakers"][uid_dst]["file"], arr)
    mts = reg["speakers"][uid_dst].setdefault("meetings", [])
    for m in reg["speakers"][uid_src].get("meetings", []):
        if m not in mts:
            mts.append(m)
    (config.VOICEPRINTS_DIR / reg["speakers"][uid_src]["file"]).unlink(missing_ok=True)
    del reg["speakers"][uid_src]
    save(reg)
    return True


def drop(uid: str) -> bool:
    """Forget an unknown (e.g. background voice you never want tracked)."""
    reg = load()
    if uid not in reg["speakers"]:
        return False
    (config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"]).unlink(missing_ok=True)
    del reg["speakers"][uid]
    save(reg)
    return True
