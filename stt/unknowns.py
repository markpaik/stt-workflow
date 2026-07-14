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
import sys
from datetime import datetime

import numpy as np

from . import config
from .identify import (_atomic_write, _l2, cosine, load_voiceprints,
                       lock_registry, score_against)

MATCH_MIN = float(__import__("os").environ.get("STT_UNKNOWN_MATCH_MIN", "0.60"))
MATCH_MARGIN = float(__import__("os").environ.get("STT_UNKNOWN_MATCH_MARGIN", "0.10"))
MAX_SAMPLES = 5
# two clusters in one meeting may only share a global "Speaker N" when their own
# centroids are this close — i.e. one person over-segmented into two clusters, not
# two distinct strangers who merely resemble the same past unknown (mirrors
# refine.resolve_split_clusters' inter-cluster gate)
SPLIT_SIM = 0.75


def _path():
    return config.VOICEPRINTS_DIR / "unknowns.json"


def load() -> dict:
    p = _path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError as e:
            # loud, not a silent reset — every "Speaker N" would otherwise
            # renumber from scratch with no warning that it just happened
            print(f"WARNING: {p} is corrupt ({e}) — treating as empty. "
                  "Sample files on disk are untouched; fix or restore before "
                  "naming any unknown speaker.", file=sys.stderr)
    return {"speakers": {}}


def save(reg: dict):
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(_path(), json.dumps(reg, indent=2))


def rename_meeting_refs(old_base: str, new_base: str) -> int:
    """A meeting rename must follow into this registry: each unknown's
    `meetings` list drives its ▶ voice playback and 'heard in N meetings'
    line, so a stale name means a dead play button. Returns how many
    speakers were touched."""
    n = 0
    with lock_registry():
        reg = load()
        for meta in reg["speakers"].values():
            mts = meta.get("meetings", [])
            if old_base in mts:
                meta["meetings"] = [new_base if m == old_base else m for m in mts]
                n += 1
        if n:
            save(reg)
    return n


def forget_meeting_refs(base: str) -> int:
    """A permanently DELETED meeting must vanish from every unknown's 'heard in'
    list — the refs drive ▶ playback and the meetings count, and the audio no
    longer exists anywhere. Archiving does NOT call this: an archived meeting's
    refs stay put so a restore brings its voice clips straight back (the clip
    endpoints already skip non-live meetings gracefully in the meantime).
    The unknown itself is kept even at zero refs — its entry and .npy embedding
    still identify the voice, so assign() re-links it (same global number) the
    next time it is heard; the panel simply hides zero-ref unknowns instead of
    nagging "heard in 0 meetings". Reclaiming the entry and sample file is a
    deliberate GC step taken with the user, never automatic — mirroring the
    dropped/archived tombstones above. Returns speakers touched."""
    n = 0
    with lock_registry():
        reg = load()
        for meta in reg["speakers"].values():
            mts = meta.get("meetings", [])
            if base in mts:
                meta["meetings"] = [m for m in mts if m != base]
                n += 1
        if n:
            save(reg)
    return n


def _samples(reg, uid):
    f = config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"]
    if not f.exists():
        return None
    arr = np.load(f)
    return arr.reshape(1, -1) if arr.ndim == 1 else arr


def samples_of(uid: str):
    """This unknown's stored sample stack (n, dim), or None — the public read
    the enrollment quality gate uses before promote() folds these rows into
    an enrolled person's profile."""
    reg = load()
    if uid not in reg["speakers"]:
        return None
    return _samples(reg, uid)


def display(uid: str) -> str:
    """'U007' -> 'Speaker 7' (the stable global number)."""
    try:
        return f"Speaker {int(uid[1:])}"
    except (ValueError, TypeError):
        return str(uid)


def talk_stats(raw_turns) -> dict:
    """Per-cluster talk evidence, from the diarizer's raw turns:
    {label: {"talk_secs": total speech, "reliable_turns": turns long enough
    (>= REFINE_MIN_RELIABLE_DUR) to carry a usable voice embedding}}.
    Both call sites of assign() (pipeline and relabel) pass this so the
    minting floor below can tell a person from a noise floor."""
    out = {}
    for t in raw_turns:
        dur = t["end"] - t["start"]
        st = out.setdefault(t["cluster"], {"talk_secs": 0.0, "reliable_turns": 0})
        st["talk_secs"] += dur
        if dur >= config.REFINE_MIN_RELIABLE_DUR:
            st["reliable_turns"] += 1
    return out


def floor_violation(st) -> str:
    """Plain-language refusal when a cluster is below the evidence floor a
    voice needs before it can identify anyone — used both by assign()'s
    minting gate and by the enrollment endpoints. None when the floor is met
    or when st is None (caller had no turn data: stay permissive)."""
    if st is None:
        return None
    if st["talk_secs"] < config.UNKNOWN_MIN_TALK_SECS:
        return (f"This voice has only {st['talk_secs']:.0f}s of speech, "
                "too little to identify anyone reliably.")
    if st["reliable_turns"] < config.UNKNOWN_MIN_RELIABLE_TURNS:
        return (f"This voice has only {st['reliable_turns']} clear turn(s) of "
                "speech, too little to identify anyone reliably.")
    return None


def _enrolled_fragment(v, enrolled: dict, cluster_names: dict, cent_emb: dict) -> bool:
    """Is this unnamed cluster really an ENROLLED person's voice (a split
    fragment), rather than a distinct stranger who merely resembles one?
    True when it scores at split-fragment level (SPLIT_SIM) against a person's
    enrolled samples, or when it resembles (>= MATCH_MIN) a person who is
    already named to a split-similar cluster in this same meeting."""
    for name, s in enrolled.items():
        if s is None:
            continue
        sc = score_against(v, s)
        if sc >= SPLIT_SIM:
            return True  # effectively the same voice as the enrolled samples
        if sc < MATCH_MIN:
            continue
        for lbl, nm in cluster_names.items():
            if nm != name or lbl not in cent_emb:
                continue
            c = np.asarray(cent_emb[lbl], float)
            if (np.isfinite(c).all() and np.linalg.norm(c)
                    and cosine(v, c) >= SPLIT_SIM):
                return True  # their named cluster here is this voice's twin
    return False


def assign(cent_emb: dict, cluster_names: dict, meeting: str, stats: dict = None) -> dict:
    """For each UNNAMED cluster with a usable centroid, return {label: global_uid},
    matching returning unknowns and registering new ones.

    `stats` (optional): per-cluster talk evidence from talk_stats(). When
    given, a cluster below the minting floor (UNKNOWN_MIN_TALK_SECS /
    UNKNOWN_MIN_RELIABLE_TURNS) never registers a NEW unknown — noise floors
    are not people. Matching an EXISTING unknown stays unrestricted."""
    with lock_registry():
        reg = load()
        out = {}
        claimed = {}  # uid -> centroid of the first cluster that claimed it this pass
        enrolled = load_voiceprints()  # gate below: enrolled voices never mint
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
            # duplicate guard: at split-fragment similarity the cluster IS the
            # best entry's voice — the margin rule exists to separate DISTINCT
            # strangers, and demanding a margin between two copies of the same
            # voice minted identical twins unboundedly on every relabel (the
            # twins scored 1.000 each, margin 0.000, so each pass registered
            # yet another copy).
            matched = uid is not None and (
                best >= SPLIT_SIM
                or (best >= MATCH_MIN and (best - second) >= MATCH_MARGIN))
            if matched and uid in claimed and cosine(v, claimed[uid]) < SPLIT_SIM:
                # a different cluster in THIS meeting already took this unknown, and
                # the two centroids are not close — these are two distinct strangers,
                # not one over-segmented voice. Don't collapse them into one Speaker N;
                # register this one as a new unknown instead (mirrors the greedy
                # each-id-used-once rule in identify.name_speakers).
                matched = False
            if matched:
                claimed.setdefault(uid, v)
                # returning unknown: add this meeting's sample ONCE (a relabel of the
                # same meeting must not stack duplicate centroids)
                mts = reg["speakers"][uid].setdefault("meetings", [])
                if meeting not in mts:
                    s = _samples(reg, uid)
                    arr = np.vstack([s, _l2(v)])[-MAX_SAMPLES:]
                    np.save(config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"], arr)
                    mts.append(meeting)
                if reg["speakers"][uid].get("dropped"):
                    # the tombstone did its job: recognized, suppressed — the
                    # cluster keeps its transcript-local label and never
                    # resurfaces in the Speakers panel (the sample update above
                    # still ran, so the tombstone keeps getting BETTER at
                    # recognizing this voice)
                    continue
            else:
                # never mint a NEW unknown for a voice that IS an enrolled
                # person. Naming an unknown moves its samples into the enrolled
                # profile verbatim and deletes the unknown entry — but a meeting
                # where the diarizer SPLIT that person into two clusters can
                # name only one of them (open-set one-name-per-meeting rule),
                # so the loser cluster matched nothing here and resurrected as
                # a fresh "Speaker 1" seconds after the naming. That gate used
                # to fire on mere RESEMBLANCE (>= MATCH_MIN), which orphaned
                # real strangers: once their unknown entry is gone (hard-
                # deleted in a registry GC), a distinct voice scoring 0.6
                # against somebody enrolled could never register again — no
                # registry entry, nothing in the panel to name, the cluster's
                # raw label unresolvable forever. Suppress only a genuine
                # same-voice match: split-fragment-level similarity (SPLIT_SIM,
                # the same "one person over-segmented" bar used above), to the
                # enrolled samples themselves or to a cluster in THIS meeting
                # that already carries that person's name.
                if _enrolled_fragment(v, enrolled, cluster_names, cent_emb):
                    continue
                # minting quality floor: a NEW unknown needs enough talk to
                # ever be named (played, recognized, enrolled). A cluster the
                # caller measured below the floor stays transcript-local (a
                # centroid with NO turns at all is definitionally junk).
                if stats is not None and floor_violation(
                        stats.get(label, {"talk_secs": 0.0, "reliable_turns": 0})):
                    continue
                # lowest free number: after unknowns get named, new voices start
                # back at Speaker 1 instead of counting up forever
                taken = {int(u[1:]) for u in reg["speakers"] if u[1:].isdigit()}
                n = 1
                while n in taken:
                    n += 1
                uid = f"U{n:03d}"
                fname = f"{uid}.npy"
                np.save(config.VOICEPRINTS_DIR / fname, _l2(v).reshape(1, -1))
                reg["speakers"][uid] = {"file": fname, "meetings": [meeting],
                                        "created": datetime.now().isoformat(timespec="seconds")}
            out[label] = uid
        # prune ghosts: an unknown whose ONLY evidence is THIS meeting, whose
        # own voice a NAMED cluster in this pass now matches — the person got
        # named via a voiceprint match (promote() never ran), so nothing else
        # retires the stale "Speaker N" entry. Requiring the voice match keeps
        # this safe for partial-cluster calls; multi-meeting unknowns are
        # never touched here.
        named_vecs = [np.asarray(v, float) for label, v in cent_emb.items()
                      if cluster_names.get(label)]
        for uid in [u for u, m in reg["speakers"].items()
                    if m.get("meetings") == [meeting] and u not in out.values()]:
            s = _samples(reg, uid)
            if s is None or not named_vecs:
                continue
            if max(score_against(v, s) for v in named_vecs) >= MATCH_MIN:
                (config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"]).unlink(missing_ok=True)
                del reg["speakers"][uid]
        save(reg)
        return out


def promote(uid: str, name: str) -> bool:
    """Name an unknown: move their samples into the enrolled library and retire the
    unknown id. Caller should then relabel past meetings."""
    from . import identify
    with lock_registry():
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
    with lock_registry():
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


def archive(uid: str) -> bool:
    """Hide a one-time voice (focus-group participant, walk-in) from the
    Speakers list WITHOUT forgetting it: samples and meeting links stay, so
    matching keeps working (their number never gets reassigned to someone
    else) and a later Restore brings them back for naming."""
    with lock_registry():
        reg = load()
        if uid not in reg["speakers"]:
            return False
        reg["speakers"][uid]["archived"] = True
        save(reg)
        return True


def restore(uid: str) -> bool:
    with lock_registry():
        reg = load()
        if uid not in reg["speakers"]:
            return False
        reg["speakers"][uid].pop("archived", None)
        save(reg)
        return True


def drop(uid: str) -> bool:
    """'Not a real speaker': suppress this voice for good. The entry and its
    samples are KEPT as a tombstone — deleting them let the very next relabel
    (and one runs after every naming) re-register the same voice from the
    meeting caches under the next free number, seconds after the user removed
    it. A dropped voice keeps matching its tombstone in assign() and is simply
    never surfaced or given a global label again."""
    with lock_registry():
        reg = load()
        if uid not in reg["speakers"] or reg["speakers"][uid].get("dropped"):
            return False
        reg["speakers"][uid]["dropped"] = datetime.now().isoformat(timespec="seconds")
        save(reg)
        return True
