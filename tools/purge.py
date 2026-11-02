#!/usr/bin/env python
"""Remove one meeting from every store this project keeps (unified plan
Phase 2 step 3, and the purge scope in row 36).

Use it for a recording that must not stay: a personal memo that slipped in,
or one recorded by mistake. The panel's Delete keeps enrolled voice samples
and unknown-voice embeddings on purpose. This removes those too.

What it removes:

- the meeting folder, live or archived (transcript, audio, caches);
- its manifest records, its id-index entry, and its search.db rows (the
  index is then vacuumed and its log checkpointed, so no freed page keeps
  the text);
- every enrolled voice sample taken from it, and a person whose every
  sample came from it;
- every unknown voice heard only in it, with its embedding file, and the
  references to it from voices also heard elsewhere;
- its centroid-index rows (the index is refreshed without it);
- its rows in the duplicate review stores, the queue holds, the failure
  dismissals, and the run history;
- saved Ask threads that cite it and group overrides keyed on it, when
  those stores exist;
- the source file if it still sits in a watched folder (so the next run
  cannot bring it back), and, for a Voice Memos sync copy, a block on the
  memo's id so the sync never copies it again. The memo on the phone and
  in the Voice Memos library is NOT touched.

What it only reports: pages and notes outside this project that name the
meeting (STT_PURGE_SCAN_DIRS, colon-separated, default the folder above the
meeting store), and that encrypted backups still hold it until they age out.

  ./run.sh py tools/purge.py "<meeting folder name>"           # dry run
  ./run.sh py tools/purge.py "<meeting folder name>" --apply

The dry run writes nothing. The apply refuses a meeting the batch is
writing. There is no undo: that is the point of a purge.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config  # noqa: E402

BACKUP_NOTE = ("Encrypted backups (Time Machine or another copy) still hold this "
               "meeting until they age out.")


# ---------- finding the meeting ----------

def locate(base: str):
    """(folder, archived) or (None, None)."""
    if base in config.meeting_bases():
        return config.meeting_dir(base), False
    if base in config.archived_bases():
        return config.archive_dir() / base, True
    return None, None


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


# ---------- the plan ----------

def _manifest_keys(folder: Path, base: str, meta: dict) -> list:
    m = _read_json(config.MANIFEST_PATH) or {}
    keys = []
    for k, rec in (m.get("processed") or {}).items():
        outs = rec.get("outputs") or []
        if any(Path(o).parent == folder or Path(o).stem == base for o in outs):
            keys.append(k)
    for k in (meta.get("source_name"), meta.get("source_file")):
        if k and k in (m.get("processed") or {}) and k not in keys:
            rec = m["processed"][k]
            if any(Path(o).parent == folder for o in rec.get("outputs") or []):
                keys.append(k)
    return keys


def _enrolled_plan(base: str, mid) -> dict:
    """{person: [sample indexes from this meeting]}."""
    from stt import identify
    out = {}
    for name, meta in (identify.load_registry() or {}).items():
        srcs = meta.get("sources") or []
        ids = identify.aligned_source_ids(meta)
        hit = [i for i, (s, m) in enumerate(zip(srcs, ids))
               if s == base or (mid and m == mid)]
        if hit:
            out[name] = {"rows": hit, "total": int(meta.get("n_samples") or len(srcs))}
    return out


def _unknown_plan(base: str, mid) -> dict:
    """{'only': [uids heard only here], 'shared': [uids heard here and elsewhere]}."""
    from stt import unknowns
    reg = unknowns.load()
    only, shared = [], []
    for uid, meta in (reg.get("speakers") or {}).items():
        mts = meta.get("meetings") or []
        ids = unknowns.aligned_meeting_ids(meta)
        here = [i for i, (m, x) in enumerate(zip(mts, ids))
                if m == base or (mid and x == mid)]
        links = [ln for ln in meta.get("links") or []
                 if ln.get("meeting") == base or (mid and ln.get("meeting_id") == mid)]
        if not here and not links:
            continue
        rest = [m for i, m in enumerate(mts) if i not in here]
        (shared if rest else only).append(uid)
    return {"only": sorted(only), "shared": sorted(shared)}


def _watched_sources(meta: dict, keys=()) -> list:
    """The meeting's original audio still in a watched folder. A meeting from
    before source_name existed carries only source_file, and the manifest
    key is the arrival name, so all three are checked: a missed original
    would be transcribed again by the next batch."""
    from stt import voicememos
    names = {Path(n).name for n in (meta.get("source_name"), meta.get("source_file"),
                                    *keys) if n}
    out = []
    for d in (config.source_dir(), config.recordings_dir(), voicememos.staging_dir()):
        for n in names:
            p = Path(d) / n
            if p.exists():
                out.append(p)
    return out


def _search_rows(base: str) -> int:
    from stt import search
    p = search.db_path()
    if not p.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT id FROM files WHERE base = ?", (base,)).fetchone()
            if not row:
                return 0
            lo = row[0] * search._ROW
            n = conn.execute("SELECT count(*) FROM segs_raw WHERE rowid >= ? AND rowid < ?",
                             (lo, lo + search._ROW)).fetchone()[0]
            return int(n) + 1
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _threads(base: str, mid) -> list:
    d = config.PROJECT_DIR / "ask_threads"
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            text = p.read_text()
        except OSError:
            continue
        # threads are written with ensure_ascii=False (stt/ask.py), so a
        # name with an accent appears raw, not as \u escapes
        if ((mid and mid in text) or json.dumps(base) in text
                or json.dumps(base, ensure_ascii=False) in text):
            out.append(p)
    return out


def _overrides(base: str, mid) -> list:
    p = config.PROJECT_DIR / "group_overrides.json"
    d = _read_json(p)
    if not isinstance(d, dict):
        return []
    return [k for k in d if k in (mid, base)]


def _outside_mentions(base: str, mid) -> list:
    env = os.environ.get("STT_PURGE_SCAN_DIRS")
    dirs = ([Path(x).expanduser() for x in env.split(":") if x] if env
            else [config.meetings_dir().parent])
    store = config.meetings_dir().resolve()
    needles = [n for n in (base, mid) if n]
    hits = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.rglob("*.md"):
            try:
                if store in p.resolve().parents:
                    continue
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            if any(n in text for n in needles):
                hits.append(str(p))
            if len(hits) >= 200:
                return hits
    return hits


def plan(base: str) -> dict:
    """What a purge of `base` would remove. Reads only."""
    folder, archived = locate(base)
    if folder is None:
        return {"ok": False, "error": f"no meeting '{base}'"}
    meta = _read_json(folder / f"{base}.json") or {}
    mid = meta.get("meeting_id") or config.id_of(base, folder.parent)
    from stt import centroids
    cmeta = _read_json(centroids.meta_path()) or {}
    keys = _manifest_keys(folder, base, meta)
    return {
        "ok": True, "base": base, "meeting_id": mid, "archived": archived,
        "folder": str(folder),
        "manifest_keys": keys,
        "enrolled": _enrolled_plan(base, mid),
        "unknowns": _unknown_plan(base, mid),
        "centroid_row": base in cmeta,
        "search_rows": _search_rows(base),
        "sources": [str(p) for p in _watched_sources(meta, keys)],
        "vm_uuid": meta.get("vm_uuid"),
        "threads": [str(p) for p in _threads(base, mid)],
        "overrides": _overrides(base, mid),
        "outside": _outside_mentions(base, mid),
        "note": BACKUP_NOTE,
    }


# ---------- applying ----------

def _drop_enrolled(rows_by_person: dict) -> dict:
    import numpy as np

    from stt import identify
    done = {"samples": 0, "people": []}
    with identify.lock_registry():
        reg = identify.load_registry()
        for name, info in rows_by_person.items():
            meta = reg.get(name)
            if meta is None:
                continue
            f = config.VOICEPRINTS_DIR / meta["file"]
            srcs = meta.get("sources") or []
            ids = identify.aligned_source_ids(meta)
            drop = set(info["rows"])
            keep = [i for i in range(len(srcs)) if i not in drop]
            if not keep:
                f.unlink(missing_ok=True)
                del reg[name]
                done["people"].append(name)
                done["samples"] += len(drop)
                continue
            try:
                arr = np.load(f)
                arr = arr.reshape(1, -1) if arr.ndim == 1 else arr
                if arr.shape[0] == len(srcs):
                    np.save(f, arr[keep])
            except (OSError, ValueError):
                pass
            meta["sources"] = [srcs[i] for i in keep]
            meta["source_ids"] = [ids[i] for i in keep]
            meta["n_samples"] = len(keep)
            done["samples"] += len(drop)
        identify.save_registry(reg)
        # twice: the first save copies the old registry, which still names
        # the meeting, to registry.json.bak. The second replaces that copy.
        identify.save_registry(reg)
    return done


def _drop_unknowns(base: str, mid, only: list) -> int:
    from stt import unknowns
    with unknowns.lock_registry():
        reg = unknowns.load()
        for uid in only:
            meta = reg["speakers"].pop(uid, None)
            if meta and meta.get("file"):
                (config.VOICEPRINTS_DIR / meta["file"]).unlink(missing_ok=True)
        for meta in reg["speakers"].values():
            for k in ("suggested",):
                if isinstance(meta.get(k), list):
                    meta[k] = [s for s in meta[k]
                               if not (isinstance(s, dict) and s.get("uid") in only)]
            links = meta.get("links")
            if links:
                meta["links"] = [ln for ln in links if ln.get("meeting") != base
                                 and not (mid and ln.get("meeting_id") == mid)]
        unknowns.save(reg)
    unknowns.forget_meeting_refs(base)
    return len(only)


def _purge_search(base: str) -> None:
    from stt import search
    p = search.db_path()
    if not p.exists():
        return
    with search._lock:
        conn = search._open(create=False)
        if conn is None:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            search._forget(conn, base)
            conn.execute("COMMIT")
            # an FTS5 delete only marks the terms gone: 'optimize' merges
            # every index segment, so the deleted terms leave the b-tree
            for t in ("segs", "segs_raw", "docs"):
                conn.execute(f"INSERT INTO {t}({t}) VALUES('optimize')")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()
            search._secure(p)


def _purge_manifest(keys: list) -> None:
    from stt import manifest
    if not keys:
        return
    with manifest.locked():
        m = manifest.load()
        for k in keys:
            m["processed"].pop(k, None)
        manifest.save(m)


def _scrub_names(names: set) -> dict:
    """Holds, failure dismissals, the run history, and the duplicate stores."""
    from stt import dupes, holds, status
    out = {"holds": 0, "dismissals": 0, "history": 0, "pairs": 0}
    for n in names:
        if n in holds.items():
            holds.release(n)
            out["holds"] += 1
    dp = config.PROJECT_DIR / "dismissed_failures.json"
    d = _read_json(dp)
    if isinstance(d, dict):
        hit = [k for k in d if k in names]
        for k in hit:
            d.pop(k)
        if hit:
            tmp = dp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, dp)
            out["dismissals"] = len(hit)
    hp = Path(status.HISTORY_LOG)
    if hp.exists():
        kept, drop = [], 0
        for line in hp.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if isinstance(r, dict) and (r.get("name") in names or r.get("base") in names):
                drop += 1
                continue
            kept.append(line)
        if drop:
            tmp = hp.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""))
            os.replace(tmp, hp)
            out["history"] = drop
    ip = dupes.ignored_path()
    pairs = _read_json(ip)
    if isinstance(pairs, list):
        keep = [x for x in pairs if not (isinstance(x, list) and set(x) & names)]
        if len(keep) != len(pairs):
            ip.write_text(json.dumps(keep))
            out["pairs"] = len(pairs) - len(keep)
    dupes.cache_path().unlink(missing_ok=True)   # rebuilt on the next scan
    return out


def apply(base: str) -> dict:
    """Purge `base` from every store. Returns the plan plus what was done."""
    from stt import archive, centroids, review, status, voicememos
    p = plan(base)
    if not p.get("ok"):
        return p
    if not p["archived"] and status.meeting_active(base):
        return {"ok": False, "error": "the batch is writing this meeting now; try later"}
    folder = Path(p["folder"])
    meta = _read_json(folder / f"{base}.json") or {}
    mid = p["meeting_id"]
    names = {base} | {n for n in (meta.get("source_name"), meta.get("source_file")) if n}
    names |= set(p["manifest_keys"])

    with review.lock_meeting(base):
        archive.purge_meeting_dir(folder)
    _purge_manifest(p["manifest_keys"])
    _purge_search(base)
    enrolled = _drop_enrolled(p["enrolled"])
    n_unknowns = _drop_unknowns(base, mid, p["unknowns"]["only"])
    try:
        centroids.index(refresh=True)
    except Exception:
        pass
    scrub = _scrub_names(names)
    for s in p["sources"]:
        Path(s).unlink(missing_ok=True)
        from stt import provenance
        provenance.sidecar_path(s).unlink(missing_ok=True)
    blocked = voicememos.block(p["vm_uuid"]) if p["vm_uuid"] else False
    for t in p["threads"]:
        Path(t).unlink(missing_ok=True)
    if p["overrides"]:
        op = config.PROJECT_DIR / "group_overrides.json"
        d = _read_json(op) or {}
        for k in p["overrides"]:
            d.pop(k, None)
        tmp = op.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, op)
    p.update({"done": True, "enrolled_removed": enrolled,
              "unknowns_removed": n_unknowns, "scrubbed": scrub,
              "vm_blocked": blocked})
    return p


def _print(p: dict, applied: bool):
    if not p.get("ok"):
        print(p.get("error"))
        return
    verb = "removed" if applied else "would remove"
    print(f"{verb}: meeting folder ({'archived' if p['archived'] else 'live'})")
    print(f"{verb}: {len(p['manifest_keys'])} manifest record(s)")
    print(f"{verb}: {p['search_rows']} search row(s)")
    n = sum(len(v['rows']) for v in p['enrolled'].values())
    whole = [k for k, v in p['enrolled'].items() if len(v['rows']) >= v['total']]
    print(f"{verb}: {n} enrolled sample(s), {len(whole)} person(s) with no other source")
    print(f"{verb}: {len(p['unknowns']['only'])} unknown voice(s) heard only here; "
          f"references from {len(p['unknowns']['shared'])} other voice(s)")
    print(f"{verb}: centroid row: {'yes' if p['centroid_row'] else 'none'}")
    print(f"{verb}: {len(p['sources'])} source file(s) still in a watched folder")
    if p.get("vm_uuid"):
        print(f"{'blocked' if applied else 'would block'}: the Voice Memos id, so "
              "the sync never copies it again (the memo itself is not touched)")
    print(f"{verb}: {len(p['threads'])} saved thread(s), {len(p['overrides'])} override(s)")
    if p["outside"]:
        print(f"outside this project, {len(p['outside'])} file(s) name it; remove "
              "them by hand:")
        for o in p["outside"]:
            print("  " + o)
    print(p["note"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", help="the meeting folder name")
    ap.add_argument("--apply", action="store_true", help="remove it (no undo)")
    a = ap.parse_args(argv)
    if a.apply:
        p = apply(a.base)
        _print(p, True)
    else:
        p = plan(a.base)
        _print(p, False)
    return 0 if p.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
