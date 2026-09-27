"""Global registry of UNKNOWN speakers.

Without this, every meeting's strangers restart at "Speaker 1". Here, each unnamed
cluster's centroid is matched against previously-seen unknown voices: a returning
unknown keeps their global number ("Speaker 7" is the same person in every
meeting); a brand-new voice gets the next number. When you finally name Speaker 7
via the GUI, their samples move into the enrolled library and every past meeting
is relabeled.

Registry: voiceprints/unknowns.json + voiceprints/U###.npy sample stacks.
Open-set matching mirrors identify.py (threshold + margin, max-cosine over samples).

The linking rules, in one place:

* A global number is MONOTONIC. `next` is the true counter, healed on load to
  max(existing) + 1 whenever it reads lower, and a number freed by a naming, a
  merge or a deletion is never handed to the next stranger. Reuse made two
  different people share one number across the library's history, and made a
  stale reference in any other store silently point at somebody else.
* A returning unknown LINKS only on strong evidence: best at or above
  SPLIT_SIM, or best at or above LINK_MIN with the usual margin over the
  runner-up — AND, after tentatively appending this centroid, the minimum
  pairwise cosine over the stored stack stays at or above LINK_STACK_MIN.
  Sample drift is what the stack floor stops: every link appends a centroid,
  so a chain of barely-passing 0.62 matches walks the stack away from the
  voice it started on until one id spans two people (U050 did, at 0.48
  between its two clusters). A relabel of a meeting the unknown already
  lists appends nothing, so it skips the stack floor and keeps its link: a
  stack that drifted before the floor existed is split by the registry
  doctor, never forked one meeting at a time by a relabel.
* Every link records its evidence in `links`: the meeting, its id, the
  cluster, and the score that carried it. One entry per meeting-cluster pair,
  rewritten in place on a relabel.
* A near miss is not thrown away. A cluster that scored at or above MATCH_MIN
  against some unknown and still did not link mints its own number and carries
  `suggested` — the top three candidates by score — for the panel to offer as
  "possibly the same voice". It is refreshed on every relabel of that meeting.

The floor, the enrolled-fragment gate, the dropped tombstones and the
one-cluster-per-id claimed guard are unchanged by any of this.
"""
import json
import os
import sys
from datetime import datetime

import numpy as np

from . import config
from .identify import (_atomic_write, _l2, cosine, load_voiceprints,
                       lock_registry, score_against)

MATCH_MIN = float(os.environ.get("STT_UNKNOWN_MATCH_MIN", "0.60"))
MATCH_MARGIN = float(os.environ.get("STT_UNKNOWN_MATCH_MARGIN", "0.10"))
# the bar a RETURNING unknown must clear to claim an existing global number.
# Higher than MATCH_MIN on purpose: MATCH_MIN is now only the bar for being
# WORTH SUGGESTING, and a 0.60-0.70 match is exactly the band that drifted
# U050's stack across two people one appended centroid at a time.
LINK_MIN = float(os.environ.get("STT_UNKNOWN_LINK_MIN", "0.70"))
# after the append, every pair inside the stack must still be this close. A
# stack that no longer agrees with itself is not one voice.
LINK_STACK_MIN = float(os.environ.get("STT_UNKNOWN_LINK_STACK_MIN", "0.60"))
# how many near misses a freshly minted unknown carries for the panel
MAX_SUGGESTED = 3
MAX_SAMPLES = 5
# two clusters in one meeting may only share a global "Speaker N" when their own
# centroids are this close — i.e. one person over-segmented into two clusters, not
# two distinct strangers who merely resemble the same past unknown (mirrors
# refine.resolve_split_clusters' inter-cluster gate)
SPLIT_SIM = 0.75


def _path():
    return config.VOICEPRINTS_DIR / "unknowns.json"


def uid_num(uid) -> int:
    """The number inside a global id ('U007' -> 7), or 0 for anything else."""
    try:
        return int(str(uid)[1:]) if str(uid)[:1] == "U" else 0
    except ValueError:
        return 0


def next_floor(reg: dict) -> int:
    """The lowest number `next` may legally hold: one past the highest id the
    registry actually contains."""
    nums = [uid_num(u) for u in reg.get("speakers", {})]
    return max(nums, default=0) + 1


def heal_next(reg: dict) -> dict:
    """Make `next` the true monotonic counter, in place.

    It reads 6 on the live registry while ids run to U058 — nothing has
    maintained it since the numbering moved to a lowest-free-number scan. A
    counter below the high-water mark would hand out a number that is already
    taken, so every load lifts it back to max + 1 and minting only ever moves
    it forward."""
    if not isinstance(reg.get("speakers"), dict):
        reg["speakers"] = {}
    try:
        nxt = int(reg.get("next", 1))
    except (TypeError, ValueError):
        nxt = 1
    reg["next"] = max(nxt, next_floor(reg))
    return reg


def load() -> dict:
    p = _path()
    if p.exists():
        try:
            return heal_next(json.loads(p.read_text()))
        except json.JSONDecodeError as e:
            # loud, not a silent reset — every "Speaker N" would otherwise
            # renumber from scratch with no warning that it just happened
            print(f"WARNING: {p} is corrupt ({e}) — treating as empty. "
                  "Sample files on disk are untouched; fix or restore before "
                  "naming any unknown speaker.", file=sys.stderr)
    return {"next": 1, "speakers": {}}


def mint_uid(reg: dict) -> str:
    """The next global id, and the counter moved past it. Never a number this
    registry has ever handed out: a freed U003 stays freed forever, so a
    dangling 'Speaker 3' in any other store can never come back as a
    different person."""
    n = max(int(reg.get("next", 1)), next_floor(reg))
    reg["next"] = n + 1
    return f"U{n:03d}"


def min_pairwise(samples) -> float:
    """The worst cosine between any two rows of a sample stack. A stack of one
    row has no pairs and scores 1.0 — nothing disagrees with itself."""
    arr = np.asarray(samples, float)
    arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
    if arr.shape[0] < 2:
        return 1.0
    return min(cosine(arr[i], arr[j])
               for i in range(arr.shape[0])
               for j in range(i + 1, arr.shape[0]))


def record_link(meta: dict, meeting: str, meeting_id, cluster: str, score: float):
    """Write this link's evidence onto the unknown, one entry per
    meeting-cluster pair. A relabel of the same meeting rewrites the entry in
    place instead of stacking another copy of it."""
    links = meta.setdefault("links", [])
    row = {"meeting": meeting, "meeting_id": meeting_id,
           "cluster": cluster, "score": float(score)}
    for i, ln in enumerate(links):
        if ln.get("cluster") != cluster:
            continue
        if ln.get("meeting") == meeting or (meeting_id
                                            and ln.get("meeting_id") == meeting_id):
            links[i] = row
            return
    links.append(row)


def near_misses(reg: dict, scored, exclude=None) -> list:
    """The top MAX_SUGGESTED unknowns this centroid resembles (at or above
    MATCH_MIN) without linking to them — what the panel offers as "possibly
    the same voice". `scored` is already best-first. A tombstone is never
    suggested: the user already said that voice is not a speaker."""
    out = []
    for s, u in scored:
        if u == exclude or s < MATCH_MIN:
            continue
        if reg["speakers"].get(u, {}).get("dropped"):
            continue
        out.append({"uid": u, "score": float(s)})
        if len(out) >= MAX_SUGGESTED:
            break
    return out


def save(reg: dict):
    config.VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(_path(), json.dumps(reg, indent=2))


def aligned_meeting_ids(meta: dict) -> list:
    """`meta["meeting_ids"]` padded (or truncated) to exactly len(meetings).

    Mirrors identify.aligned_source_ids: an entry written before meeting ids
    existed has no list at all and reads as "no id for any meeting", which is
    precisely the base-only behaviour it had before."""
    mts = meta.get("meetings", []) or []
    ids = list(meta.get("meeting_ids") or [])[:len(mts)]
    return ids + [None] * (len(mts) - len(ids))


def resolved_meetings(meta: dict) -> list:
    """This unknown's meetings as LIVE basenames where that is knowable: the
    stored name while it exists, else whatever the id resolves to now. A
    rename that outran the chase below no longer kills the ▶ clip."""
    out = []
    for base, mid in zip(meta.get("meetings", []) or [], aligned_meeting_ids(meta)):
        if mid and base and not config.meeting_file(base, ".json").exists():
            base = config.base_of(mid) or base
        out.append(base)
    return out


def rename_meeting_refs(old_base: str, new_base: str) -> int:
    """A meeting rename must follow into this registry: each unknown's
    `meetings` list drives its ▶ voice playback and 'heard in N meetings'
    line, so a stale name means a dead play button. Returns how many
    speakers were touched.

    Still best-effort, and no longer the only line of defence: the aligned
    `meeting_ids` survive this failing, and resolved_meetings reads through
    them."""
    n = 0
    new_id = config.id_of(new_base)
    with lock_registry():
        reg = load()
        for meta in reg["speakers"].values():
            mts = meta.get("meetings", [])
            touched = False
            if old_base in mts:
                ids = aligned_meeting_ids(meta)
                # heal while we are here: a ref with no id yet picks up the id
                # of the meeting it just followed
                meta["meeting_ids"] = [new_id if (m == old_base and i is None) else i
                                       for m, i in zip(mts, ids)]
                meta["meetings"] = [new_base if m == old_base else m for m in mts]
                touched = True
            for ln in meta.get("links", []):
                if ln.get("meeting") == old_base:
                    ln["meeting"] = new_base
                    ln["meeting_id"] = ln.get("meeting_id") or new_id
                    touched = True
            if touched:
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
            touched = False
            if base in mts:
                ids = aligned_meeting_ids(meta)
                meta["meeting_ids"] = [i for m, i in zip(mts, ids) if m != base]
                meta["meetings"] = [m for m in mts if m != base]
                touched = True
            links = meta.get("links")
            if links and any(ln.get("meeting") == base for ln in links):
                meta["links"] = [ln for ln in links if ln.get("meeting") != base]
                touched = True
            if touched:
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


def pooled_stats(label, cent_emb, stats, unnamed_labels):
    """The floor evidence for `label`, POOLED across its same-meeting split
    twins — unnamed clusters whose centroids sit at SPLIT_SIM to it.

    The floor used to measure each fragment alone, but the diarizer's
    over-splitting divides one person's talk BEFORE the floor can measure it:
    a participant with 79s of speech across three fragments read as three
    noise floors, and a real person became permanently unnameable 'Voice N'
    scraps (no registry entry, no naming affordance anywhere). A voice's
    evidence is the union of its fragments; once the first fragment of a
    passing group mints, the existing SPLIT_SIM matching attaches its twins
    to the same Speaker N."""
    if stats is None:
        return None
    # ALWAYS seed with the cluster's own evidence — never rely on the caller
    # listing the label among unnamed_labels. A caller with no roster to offer
    # (an older meeting json without a speakers list) must degrade to the
    # single-cluster floor, not to a zero that vetoes every voice.
    own = stats.get(label, {"talk_secs": 0.0, "reliable_turns": 0})
    total = {"talk_secs": own["talk_secs"], "reliable_turns": own["reliable_turns"]}
    v = np.asarray(cent_emb.get(label), float)
    for other in unnamed_labels:
        st = stats.get(other)
        if other == label or st is None or other not in cent_emb:
            continue
        w = np.asarray(cent_emb[other], float)
        if (not np.isfinite(w).all() or not np.linalg.norm(w)
                or cosine(v, w) < SPLIT_SIM):
            continue
        total["talk_secs"] += st["talk_secs"]
        total["reliable_turns"] += st["reliable_turns"]
    return total


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
    meeting_id = config.id_of(meeting)   # resolved once, outside the cluster loop
    with lock_registry():
        reg = load()
        out = {}
        claimed = {}  # uid -> centroid of the first cluster that claimed it this pass
        enrolled = load_voiceprints()  # gate below: enrolled voices never mint

        def _scored(v):
            """This centroid's registry matches, best first, against the
            registry AS IT STANDS NOW (a mint earlier in this pass counts)."""
            rows = []
            for uid in reg["speakers"]:
                s = _samples(reg, uid)
                if s is not None:
                    rows.append((score_against(v, s), uid))
            rows.sort(reverse=True)
            return rows

        # Confidence order, not label order: the cluster with the STRONGEST
        # match claims its unknown first. In label order a weak 0.69 match
        # took an id before the 1.000 cluster whose own centroid IS that id's
        # stored sample got its turn; the true owner then read as a distinct
        # stranger and minted an identical copy of the id it should have
        # reused (the U028/U040 pair, remade by every relabel). The initial
        # ordering is measured against the registry as loaded; each cluster
        # is rescored at its turn. Ties keep label order (stable sort).
        pending = []
        for label, vec in cent_emb.items():
            if cluster_names.get(label):
                continue  # named person, not an unknown
            v = np.asarray(vec, float)
            if not np.isfinite(v).all() or np.linalg.norm(v) == 0:
                continue
            initial = _scored(v)
            pending.append((label, v, initial[0][0] if initial else -1.0))
        pending.sort(key=lambda p: -p[2])
        for label, v, _initial_best in pending:
            scored = _scored(v)
            best, uid = scored[0] if scored else (-1.0, None)
            second = scored[1][0] if len(scored) > 1 else -1.0
            # duplicate guard: at split-fragment similarity the cluster IS the
            # best entry's voice — the margin rule exists to separate DISTINCT
            # strangers, and demanding a margin between two copies of the same
            # voice minted identical twins unboundedly on every relabel (the
            # twins scored 1.000 each, margin 0.000, so each pass registered
            # yet another copy).
            # the score gate. LINK_MIN, not MATCH_MIN: claiming an existing
            # global number now takes 0.70, and the 0.60 band survives only as
            # a SUGGESTION on the number this cluster mints for itself.
            matched = uid is not None and (
                best >= SPLIT_SIM
                or (best >= LINK_MIN and (best - second) >= MATCH_MARGIN))
            if matched and uid in claimed and cosine(v, claimed[uid]) < SPLIT_SIM:
                # a different cluster in THIS meeting already took this unknown, and
                # the two centroids are not close — these are two distinct strangers,
                # not one over-segmented voice. Don't collapse them into one Speaker N;
                # register this one as a new unknown instead (mirrors the greedy
                # each-id-used-once rule in identify.name_speakers).
                matched = False
            # the stack gate. A link appends this centroid to the stored stack,
            # and every append moves the stack: U050's two clusters score 0.48
            # against each other because a chain of barely-passing matches
            # walked it from one person to another. Try the append first and
            # refuse the link when the stack would stop agreeing with itself.
            # A relabel that adds no sample (this meeting is already stored)
            # skips the floor: the stack does not move, so there is nothing to
            # measure. Measuring the stack as it stands refused the link on a
            # legacy stack that drifted BEFORE this floor existed, and the
            # meeting went to a fresh number while the old one still listed it
            # in its meetings and links. A drifted stack is the doctor's to
            # split (stt/registry_doctor.py), not a relabel's to fork.
            stack_after = None
            if matched:
                already = meeting in (reg["speakers"][uid].get("meetings") or [])
                if not already:
                    stack_after = np.vstack([_samples(reg, uid),
                                             _l2(v)])[-MAX_SAMPLES:]
                    if min_pairwise(stack_after) < LINK_STACK_MIN:
                        matched = False
            if matched:
                claimed.setdefault(uid, v)
                # returning unknown: add this meeting's sample ONCE (a relabel of the
                # same meeting must not stack duplicate centroids)
                mts = reg["speakers"][uid].setdefault("meetings", [])
                if meeting not in mts:
                    np.save(config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"],
                            stack_after)
                    ids = aligned_meeting_ids(reg["speakers"][uid])
                    mts.append(meeting)
                    reg["speakers"][uid]["meeting_ids"] = ids + [meeting_id]
                record_link(reg["speakers"][uid], meeting, meeting_id, label, best)
                # a number that already carries near misses keeps them current:
                # this is the same meeting that minted it, relabelled, and the
                # registry it was measured against has moved on since.
                if "suggested" in reg["speakers"][uid]:
                    sugg = near_misses(reg, scored, exclude=uid)
                    if sugg:
                        reg["speakers"][uid]["suggested"] = sugg
                    else:
                        reg["speakers"][uid].pop("suggested", None)
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
                # Measured over the cluster's split-twin GROUP, not the lone
                # fragment — over-splitting must not starve a real person
                # below the floor one piece at a time.
                if stats is not None and floor_violation(pooled_stats(
                        label, cent_emb, stats,
                        [l for l in cent_emb if not cluster_names.get(l)])):
                    continue
                # a number nobody has ever held. Numbers used to restart at the
                # lowest free slot, so a freed U003 came back as a different
                # person and every stale reference to "Speaker 3" quietly
                # changed meaning.
                near = near_misses(reg, scored)
                uid = mint_uid(reg)
                fname = f"{uid}.npy"
                np.save(config.VOICEPRINTS_DIR / fname, _l2(v).reshape(1, -1))
                reg["speakers"][uid] = {"file": fname, "meetings": [meeting],
                                        "meeting_ids": [meeting_id],
                                        "created": datetime.now().isoformat(timespec="seconds")}
                # the near miss that did not clear the link bar is evidence,
                # not noise: the panel offers it as "possibly the same voice"
                # and the user decides.
                if near:
                    reg["speakers"][uid]["suggested"] = near
            out[label] = uid
        # prune ghosts: an unknown whose ONLY evidence is THIS meeting, whose
        # own voice a NAMED cluster in this pass now matches — the person got
        # named via a voiceprint match (promote() never ran), so nothing else
        # retires the stale "Speaker N" entry. Requiring the voice match keeps
        # this safe for partial-cluster calls; multi-meeting unknowns are
        # never touched here.
        # A DROPPED voice is exempt. Its entry is a tombstone: the whole point
        # is that deleting it lets the very next relabel re-register the same
        # voice under a fresh number. An unrelated named cluster in this
        # meeting resembling it above MATCH_MIN is not evidence that the
        # tombstoned person got named -- and pruning on that resemblance
        # resurrected suppressed voices during an ordinary background relabel.
        named_vecs = [np.asarray(v, float) for label, v in cent_emb.items()
                      if cluster_names.get(label)]
        for uid in [u for u, m in reg["speakers"].items()
                    if m.get("meetings") == [meeting] and u not in out.values()
                    and not m.get("dropped")]:
            s = _samples(reg, uid)
            if s is None or not named_vecs:
                continue
            if max(score_against(v, s) for v in named_vecs) >= MATCH_MIN:
                (config.VOICEPRINTS_DIR / reg["speakers"][uid]["file"]).unlink(missing_ok=True)
                del reg["speakers"][uid]
        save(reg)
        return out


def _restore_profile(name: str, entry, rows):
    """Put an enrolled profile back exactly as promote() found it -- the entry
    and the sample stack, or neither when the name was brand new."""
    from . import identify
    with lock_registry():
        reg = identify.load_registry()
        if entry is None:
            gone = reg.pop(name, None)
            if gone and gone.get("file"):
                (config.VOICEPRINTS_DIR / gone["file"]).unlink(missing_ok=True)
        else:
            reg[name] = entry
            if rows is not None:
                np.save(config.VOICEPRINTS_DIR / entry["file"], rows)
        identify.save_registry(reg)


def promote(uid: str, name: str) -> bool:
    """Name an unknown: move their samples into the enrolled library and retire the
    unknown id. Caller should then relabel past meetings.

    ALL of the rows or none of them. Enrolling row by row and deleting the
    source only at the end left a raising row (a zero-norm sample) with the
    name half-enrolled AND the unknown still live and nameable -- and every
    retry raised on the same row, so the state was stuck forever. On any
    failure the rows added here are removed and the unknown is kept, so the
    retry starts from exactly the state the user can see."""
    from . import identify
    with lock_registry():
        reg = load()
        if uid not in reg["speakers"]:
            return False
        s = _samples(reg, uid)
        if s is None:
            return False
        # the destination profile as it stands BEFORE any row is added
        before = identify.load_registry().get(name)
        rows_before = None
        if before is not None and before.get("file"):
            f0 = config.VOICEPRINTS_DIR / before["file"]
            if f0.exists():
                rows_before = np.load(f0)
        # carry meeting provenance along (samples were appended one per meeting;
        # align best-effort — the sample window and meeting list can drift apart)
        # The meeting's id travels with its base. Without it enroll()
        # re-derives the id from the base, and a base a rename chase missed
        # resolves to nothing, so the profile lost the one reference that
        # survives a rename. A legacy entry with no ids passes None, which is
        # exactly the base-only derivation it had before.
        meetings = reg["speakers"][uid].get("meetings", []) or []
        mids = aligned_meeting_ids(reg["speakers"][uid])
        try:
            for i, row in enumerate(s):
                k = min(i, len(meetings) - 1)
                src, mid = (meetings[k], mids[k]) if meetings else (None, None)
                identify.enroll(name, row, source=src, source_id=mid)
        except Exception as e:
            _restore_profile(name, before, rows_before)
            print(f"WARNING: naming {display(uid)} '{name}' failed ({e}) -- "
                  "nothing was enrolled and the voice is still nameable.",
                  file=sys.stderr)
            return False
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
        # a DROPPED voice is a tombstone and may be neither side of a merge.
        # Merging a dropped voice INTO a live one deletes the tombstone and
        # lets the suppressed voice resurface under the live id; merging a live
        # voice INTO a dropped one buries a real identity under the tombstone
        # with no way back. Undo the drop first (Restore), then merge.
        if (reg["speakers"][uid_src].get("dropped")
                or reg["speakers"][uid_dst].get("dropped")):
            return False
        s, d = _samples(reg, uid_src), _samples(reg, uid_dst)
        if d is None:
            return False
        if s is not None:
            arr = np.vstack([d, s])[-MAX_SAMPLES:]
            np.save(config.VOICEPRINTS_DIR / reg["speakers"][uid_dst]["file"], arr)
        mts = reg["speakers"][uid_dst].setdefault("meetings", [])
        ids = aligned_meeting_ids(reg["speakers"][uid_dst])
        src_ids = aligned_meeting_ids(reg["speakers"][uid_src])
        for m, i in zip(reg["speakers"][uid_src].get("meetings", []), src_ids):
            if m not in mts:
                mts.append(m)
                ids.append(i)
        reg["speakers"][uid_dst]["meeting_ids"] = ids
        # link evidence follows the voice: the merged number is gone, and its
        # record of which meeting and cluster carried each link is the only
        # account of how this stack was built.
        for ln in reg["speakers"][uid_src].get("links", []) or []:
            record_link(reg["speakers"][uid_dst], ln.get("meeting"),
                        ln.get("meeting_id"), ln.get("cluster"),
                        ln.get("score") or 0.0)
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
