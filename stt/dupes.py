"""Duplicates, both kinds: the same recording sitting in the watched folder
again, and the same meeting transcribed twice.

PREVENTION (source files). A file that was already processed usually comes back
under a NEW name or a new mtime (an iCloud re-sync, a second export, a copy off
another Mac), and the manifest keys on name + mtime, so it reads as brand new
and gets transcribed all over again. Here the file is identified by its CONTENT:
size first (a free stat, and two different recordings essentially never share an
exact byte count), then a hash of the head and tail. Only a size collision is
ever hashed, so the common poll does no reading at all.

CURE (transcripts). Prevention only works going forward, and a library already
carries whatever slipped through. Two transcripts of the same audio are compared
as sets of word shingles (bottom-k sketches, the standard Jaccard estimator),
gated by duration so a short clip is never matched against a long meeting it
happens to share vocabulary with. The result is a REVIEW list, never an
automatic delete: only a human can say that two similar meetings are the same
meeting.

Deliberately stdlib-only: the panel imports this on its 2s poll and must never
pull the pipeline (torch, soundfile) in behind it. Every path is resolved per
call, never bound at import, so a sandboxed config.PROJECT_DIR is honored.
"""
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
from pathlib import Path

from . import config

# one lock for every cache/ignore write: the panel's poll thread, the scan
# worker, and a "Keep both" request can all write these files, and a
# read-modify-write that races another one silently drops a decision
_io_lock = threading.Lock()


def _atomic_write(p: Path, text: str):
    """tmp + os.replace with a UNIQUE tmp per writer, so two concurrent writers
    can never promote each other's half-written file. The prefix/suffix shape
    (name., random, .json.tmp) is load-bearing: .gitignore matches *.json.tmp,
    and these files carry meeting names."""
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.stem + ".",
                               suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ---------------------------------------------------------------- files -----

CHUNK = 1024 * 1024          # head and tail bytes folded into a fingerprint
_fp_cache = {}               # (path, size, mtime) -> fingerprint or None


def cache_path():
    return config.PROJECT_DIR / "dupes_cache.json"


def ignored_path():
    return config.PROJECT_DIR / "dupes_ignored.json"


def fingerprint(path):
    """A content fingerprint for a media file, or None when one cannot be taken.

    Size plus the first, MIDDLE and last megabyte: two recordings that agree on
    all four are the same bytes for any practical purpose, and it costs two
    seeks instead of reading gigabytes. NEVER touches a dataless iCloud file --
    reading one would trigger a multi-gigabyte download from a status poll,
    which is exactly the surprise this feature is supposed to prevent.

    A fingerprint recorded by an older build (head and tail only) no longer
    equals the one taken here. That is a FALSE NEGATIVE and nothing worse: the
    file simply reads as not-yet-proven-identical, so the bulk sweep leaves it
    alone and the name signal still flags it."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), st.st_size, st.st_mtime)
    if key in _fp_cache:
        return _fp_cache[key]
    fp = None
    try:
        from . import icloud
        if st.st_size > 0 and icloud._fully_present(p):
            h = hashlib.sha256(struct.pack("<Q", st.st_size))
            with open(p, "rb") as fh:
                h.update(fh.read(CHUNK))
                # any file bigger than one chunk gets its tail hashed too. The
                # old cut-off was 2*CHUNK, which left everything past the first
                # megabyte of a 1-2 MB file OUT of the "byte-identical" proof
                # that gates the no-undo bulk delete; an overlapping tail read
                # is merely redundant, never wrong
                if st.st_size > CHUNK:
                    fh.seek(-CHUNK, os.SEEK_END)
                    h.update(fh.read(CHUNK))
                # and a MIDDLE megabyte. Head plus tail alone leaves every byte
                # between them out of the hash, so two files above 2*CHUNK that
                # differ only in the middle collided -- and this hash is the
                # "byte-identical" proof that gates a bulk delete with no undo.
                if st.st_size > 2 * CHUNK:
                    fh.seek((st.st_size - CHUNK) // 2)
                    h.update(fh.read(CHUNK))
            fp = h.hexdigest()
    except OSError:
        fp = None
    _fp_cache[key] = fp
    return fp


def _record_base(rec) -> str:
    """The meeting a manifest record produced, from its recorded outputs."""
    for o in rec.get("outputs") or []:
        if str(o).endswith(".json"):
            return Path(o).stem
    return ""


def source_duplicates(files, meetings, man=None, dest_dir=None, live=None) -> dict:
    """{filename: {"base": meeting, "reason": "identical"|"name"}} for the
    waiting files that were already processed.

    `files` are the candidate source paths (unprocessed queue entries),
    `meetings` the panel's meeting metas (base + source_file), `live` the set of
    bases that still exist (live plus archived). Two independent signals, and
    they are NOT equally strong:

      identical -- same size AND same head/tail hash as a meeting's stored
        audio (a plain copy of the original) or as the fingerprint the manifest
        recorded for a processed source. Proof enough to offer a bulk delete.
      name -- the file is named exactly like some meeting's source file. Common
        and useful, but a recurring export ("Weekly Sync.m4a") legitimately
        reuses one name for genuinely different meetings, so this one only ever
        gets a chip and the ordinary one-file delete.

    A manifest record whose meeting was DELETED matches nothing: the user
    deleted that transcript and delete_meeting promises re-transcription, so
    re-processing the source is legitimate work, and the bulk sweep must never
    be offered the only surviving copy of a recording. An ARCHIVED meeting
    still matches -- it is restorable, so re-transcribing it is exactly the
    waste this gate exists to prevent.
    """
    files = [Path(f) for f in files]
    if not files:
        return {}
    man = man if man is not None else {}
    by_size, by_name = {}, {}
    for meta in meetings:
        base = meta.get("base")
        if not base:
            continue
        src = meta.get("source_file")
        if src:
            by_name.setdefault(src, base)
        audio = config.meeting_audio(base, dest_dir)
        if audio:
            try:
                by_size.setdefault(audio.stat().st_size, []).append((base, audio, None))
            except OSError:
                pass
    # manifest records: the fingerprint of the ORIGINAL source, taken before the
    # pipeline moved (or converted) it. The only signal that survives a video
    # source, whose stored audio is a re-encode and shares no bytes with it.
    # Gated on liveness: records outlive their meetings (deletions before the
    # delete_meeting scrub existed left dead records behind).
    for key, rec in (man.get("processed") or {}).items():
        fp, size = rec.get("fp"), rec.get("size")
        base = _record_base(rec)
        if not base:
            continue
        if live is not None and base not in live:
            continue
        by_name.setdefault(key, base)
        if fp and size:
            by_size.setdefault(size, []).append((base, None, fp))

    out = {}
    for p in files:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        for base, audio, fp in by_size.get(size, []):
            mine = fingerprint(p)
            if mine is None:
                break            # dataless: the name check below is all we have
            theirs = fp if fp else fingerprint(audio)
            if theirs and theirs == mine:
                out[p.name] = {"base": base, "reason": "identical"}
                break
        if p.name not in out and p.name in by_name:
            out[p.name] = {"base": by_name[p.name], "reason": "name"}
    return out


# ---------------------------------------------------------- transcripts -----

# Jaccard over word shingles. Two ASR passes over the same audio agree far above
# this; genuinely different meetings by the same people, on the same agenda,
# land well below it. The threshold only decides what gets REVIEWED.
THRESHOLD = float(os.environ.get("STT_DUPE_THRESHOLD", "0.72"))
SHINGLE = 5           # words per shingle
SKETCH_K = 256        # bottom-k hashes kept per meeting
DUR_TOL = 0.20        # candidate pair durations within 20% of each other
MAX_PAIRS = 20000     # hard bound on the comparison loop
_WORD = re.compile(r"[a-z0-9']+")


def _tokens(text: str):
    return _WORD.findall((text or "").lower())


def sketch(text: str, k: int = SKETCH_K):
    """Bottom-k sketch of a transcript's word shingles: the k smallest 64-bit
    shingle hashes, sorted. Fixed size whatever the meeting's length, and the
    standard unbiased estimator for Jaccard (see similarity)."""
    words = _tokens(text)
    if len(words) < SHINGLE:
        return []
    seen = set()
    for i in range(len(words) - SHINGLE + 1):
        sh = " ".join(words[i:i + SHINGLE]).encode()
        seen.add(struct.unpack("<Q", hashlib.blake2b(sh, digest_size=8).digest())[0])
    return sorted(seen)[:k]


def similarity(a, b) -> float:
    """Jaccard estimate from two bottom-k sketches: re-merge them, keep the k
    smallest hashes of the union, and measure how many of those are in both."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    k = min(len(sa), len(sb))
    union = sorted(sa | sb)[:k]
    if not union:
        return 0.0
    return sum(1 for h in union if h in sa and h in sb) / len(union)


def transcript_text(base: str, dest_dir=None) -> str:
    """The words only, from the .json segments -- never the .txt, whose lines
    are prefixed with speaker names. A rename or a relabel rewrites every one of
    those prefixes, which would shift every shingle in the file and quietly
    destroy the score for a pair that is still the same recording."""
    j = config.meeting_file(base, ".json", dest_dir)
    try:
        d = json.loads(j.read_text())
    except (OSError, ValueError):
        return ""
    return " ".join(str(s.get("text") or "") for s in d.get("segments") or [])


def library_signature(dest_dir=None) -> str:
    """What the pair list depends on: which meetings exist and when each was
    last written. Cheap (one stat per meeting), so the poll can ask every time
    whether the cached answer is still current."""
    parts = []
    for base in sorted(config.meeting_bases(dest_dir)):
        try:
            parts.append(f"{base}:{config.meeting_file(base, '.json', dest_dir).stat().st_mtime}")
        except OSError:
            continue
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _load_cache() -> dict:
    try:
        c = json.loads(cache_path().read_text())
        c.setdefault("sketches", {})
        c.setdefault("pairs", [])
        return c
    except (OSError, ValueError):
        return {"sketches": {}, "pairs": [], "sig": None}


def _save_cache(c: dict):
    try:
        with _io_lock:
            _atomic_write(cache_path(), json.dumps(c))
    except OSError:
        pass


def ignored_pairs() -> set:
    """Pairs a human looked at and kept ("these two are different meetings").
    Stored sorted so the key cannot depend on which side was shown first."""
    try:
        return {tuple(x) for x in json.loads(ignored_path().read_text()) if len(x) == 2}
    except (OSError, ValueError, TypeError):
        return set()


def ignore_pair(a: str, b: str) -> bool:
    # the read sits INSIDE the lock: two concurrent "Keep both" clicks each
    # read-modify-write this file, and the loser's pair would silently vanish
    try:
        with _io_lock:
            keep = sorted(ignored_pairs() | {tuple(sorted((a, b)))})
            _atomic_write(ignored_path(), json.dumps([list(x) for x in keep]))
        return True
    except OSError:
        return False


def rename_pair_refs(old_base: str, new_base: str) -> int:
    """A meeting rename must follow into the "keep both" list.

    A kept pair is stored as two literal base names, and a title edit or a date
    correction re-stamps one of them through the ordinary edit path. Without
    this, the next scan finds the same two transcripts under the new name,
    offers them again as a fresh 100 percent match, and the user's earlier
    decision is silently orphaned. Returns how many pairs were rewritten."""
    if not old_base or old_base == new_base:
        return 0
    n = 0
    try:
        with _io_lock:
            out = set()
            for a, b in ignored_pairs():
                na = new_base if a == old_base else a
                nb = new_base if b == old_base else b
                if (na, nb) != (a, b):
                    n += 1
                out.add(tuple(sorted((na, nb))))
            if n:
                _atomic_write(ignored_path(),
                              json.dumps([list(x) for x in sorted(out)]))
    except OSError:
        return 0
    return n


def _length_signal(d: dict) -> tuple:
    """(duration_sec, word count) for one transcript.

    duration_sec is the gate's real signal, but it is MISSING on
    pre-migration transcripts, and a missing one used to skip the gate
    outright -- which let a short clip match an hour-long meeting at a full
    100 percent (bottom-k Jaccard reads a contained subset as identical).
    The word count is always available and stands in when the duration is
    not."""
    dur = 0.0
    try:
        dur = float(d.get("duration_sec") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        # the transcript's own timing, when it has any
        for key, field in (("segments", "end"), ("words", "end")):
            for item in reversed(d.get(key) or []):
                try:
                    dur = max(dur, float(item.get(field) or 0.0))
                except (AttributeError, TypeError, ValueError):
                    continue
                if dur > 0:
                    break
            if dur > 0:
                break
    words = len(_tokens(" ".join(str(s.get("text") or "")
                                 for s in d.get("segments") or [])))
    return max(0.0, dur), words


def _length_gate(da, db, wa, wb) -> bool:
    """True when two meetings are too different in length to be the same
    recording. Duration when both sides have one, word count otherwise."""
    if da > 0 and db > 0:
        return abs(da - db) > DUR_TOL * max(da, db)
    if wa > 0 and wb > 0:
        return abs(wa - wb) > DUR_TOL * max(wa, wb)
    return False


def similar_meetings(threshold: float = None, dest_dir=None) -> list:
    """[{"a": base, "b": base, "score": float}] for every live pair that looks
    like the same recording, best score first.

    Sketches are cached per meeting (keyed on the transcript's mtime), so the
    steady-state cost is one new meeting's shingles. The pair walk is gated on
    duration first: same meeting means same length, and it keeps the comparison
    loop off O(N^2) Jaccards on a big library."""
    threshold = THRESHOLD if threshold is None else threshold
    cache = _load_cache()
    old = cache.get("sketches") or {}
    sketches, durs, lens = {}, {}, {}
    for base in sorted(config.meeting_bases(dest_dir)):
        j = config.meeting_file(base, ".json", dest_dir)
        try:
            mtime = j.stat().st_mtime
        except OSError:
            continue
        prev = old.get(base)
        # "words" is required: an entry cached by an older build carries no
        # word count, and the length gate below needs one whenever the
        # duration is missing
        if prev and abs(prev.get("mtime", 0) - mtime) < 1.0 and "words" in prev:
            sketches[base] = prev["hashes"]
            durs[base] = prev.get("dur") or 0.0
            lens[base] = prev.get("words") or 0
            continue
        try:
            d = json.loads(j.read_text())
        except (OSError, ValueError):
            continue
        text = " ".join(str(s.get("text") or "") for s in d.get("segments") or [])
        sk = sketch(text)
        dur, nwords = _length_signal(d)
        sketches[base], durs[base], lens[base] = sk, dur, nwords
        old[base] = {"mtime": mtime, "dur": dur, "words": nwords, "hashes": sk}
    # drop cache entries for meetings that are gone (deleted, archived, renamed)
    cache["sketches"] = {b: v for b, v in old.items() if b in sketches}

    skip = ignored_pairs()
    bases = sorted(sketches)
    # gather every duration-eligible pair FIRST, then compare the closest
    # lengths first: same recording means same length, so when the budget
    # cannot cover everything it is the far-apart pairs that go unchecked
    cand = []
    for i, a in enumerate(bases):
        for b in bases[i + 1:]:
            if (a, b) in skip:
                continue
            da, db = durs.get(a, 0.0), durs.get(b, 0.0)
            if _length_gate(da, db, lens.get(a, 0), lens.get(b, 0)):
                continue          # different lengths: not the same recording
            # unknown durations sort LAST (1.0), never first: a gated real pair
            # is within DUR_TOL, so zero-duration noise must not outrank it and
            # eat the budget
            close = abs(da - db) / max(da, db) if max(da, db) > 0 else 1.0
            cand.append((close, a, b))
    cand.sort()
    truncated = len(cand) > MAX_PAIRS
    pairs = []
    for _, a, b in cand[:MAX_PAIRS]:
        s = similarity(sketches[a], sketches[b])
        if s >= threshold:
            pairs.append({"a": a, "b": b, "score": round(s, 3)})
    pairs.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    cache["pairs"] = pairs
    # a truncated run says so instead of posing as the whole answer. The sig is
    # stamped EITHER WAY: leaving it unstamped would make cache_is_current stay
    # False and re-kick this identical truncated scan on every idle poll forever
    cache["truncated"] = truncated
    cache["sig"] = library_signature(dest_dir)
    _save_cache(cache)
    return pairs


def cache_truncated() -> bool:
    """Did the last scan hit MAX_PAIRS and leave pairs uncompared?"""
    return bool(_load_cache().get("truncated"))


def cached_pairs(dest_dir=None) -> list:
    """The last computed pair list, with pairs whose meetings have since been
    deleted or ignored dropped. Never computes: this is what the 2s poll reads."""
    cache = _load_cache()
    live = set(config.meeting_bases(dest_dir))
    skip = ignored_pairs()
    return [p for p in cache.get("pairs") or []
            if p.get("a") in live and p.get("b") in live
            and (p["a"], p["b"]) not in skip]


def cache_is_current(dest_dir=None) -> bool:
    return (_load_cache().get("sig") or None) == library_signature(dest_dir)
