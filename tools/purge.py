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
  dismissals, the run history, and the recent results in status.json;
- its lines in the undo journals and its rows in the rename plans in
  config.migration_dir() (backfill, amnesty, fix_dates, iso_rename,
  retitle_all): an undo cannot act on a purged meeting, and a journal line
  keeps its old names and titles;
- saved Ask threads that cite it and group overrides keyed on it, when
  those stores exist;
- the source file if it still sits in a watched folder (so the next run
  cannot bring it back), and, for a Voice Memos sync copy, a block on the
  memo's id so the sync never copies it again. The memo on the phone and
  in the Voice Memos library is NOT touched.

What it only reports: pages and notes outside this project that name the
meeting (STT_PURGE_SCAN_DIRS, colon-separated, default the folder above the
meeting store), any other file in config.migration_dir() that still names
it (a doctor fix run keeps whole copies of the files it changed), and that
encrypted backups still hold it until they age out.

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

def _outputs(rec) -> list:
    """The output paths of one manifest record. A record that is not an
    object, or outputs that are not a list of names, give none: the
    manifest keeps such a record for a person to repair (doctor names it
    manifest_bad_record), and the purge raised AttributeError on it."""
    outs = rec.get("outputs") if isinstance(rec, dict) else None
    return [o for o in outs if isinstance(o, str)] if isinstance(outs, list) else []


def _manifest_keys(folder: Path, base: str, meta: dict) -> list:
    m = _read_json(config.MANIFEST_PATH)
    processed = m.get("processed") if isinstance(m, dict) else None
    if not isinstance(processed, dict):
        processed = {}
    keys = []
    for k, rec in processed.items():
        if any(Path(o).parent == folder or Path(o).stem == base for o in _outputs(rec)):
            keys.append(k)
    for k in (meta.get("source_name"), meta.get("source_file")):
        if isinstance(k, str) and k in processed and k not in keys:
            if any(Path(o).parent == folder for o in _outputs(processed[k])):
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


# ---------- the migration folder ----------

# the keys a journal entry or a plan row names its meeting by
_NAME_KEYS = ("base", "old_base", "new_base", "planned_base", "renamed")
_PLAN_FILES = ("iso-rename-plan.json",)


def _needles(mid, names: set) -> set:
    """The meeting's names and id as bytes, raw and as JSON escapes them."""
    out = set()
    for n in set(names) | ({mid} if mid else set()):
        out |= {n.encode(), json.dumps(n).strip('"').encode()}
    return out


def _names_meeting(e, mid, names: set, line: str = "") -> bool:
    """An entry with a meeting id names the meeting only by that id: a name
    an older meeting gave up can belong to another meeting now. A line that
    is not JSON (a torn last line) names it when it holds one of its names."""
    if e is None and line:
        b = line.encode()
        return any(x in b for x in _needles(mid, names))
    if not isinstance(e, dict):
        return False
    if e.get("meeting_id"):
        return bool(mid) and e["meeting_id"] == mid
    return any(isinstance(e.get(k), str) and e[k] in names for k in _NAME_KEYS)


def _migration_docs() -> dict:
    """{path: [(line, entry or None)]} for each journal (.jsonl) at the top
    of config.migration_dir(), and {path: plan} for each rename plan."""
    d = config.migration_dir()
    out = {}
    if not d.is_dir():
        return out
    for p in sorted(d.iterdir()):
        if not p.is_file():
            continue
        if p.suffix == ".jsonl":
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except (OSError, ValueError):
                continue
            rows = []
            for ln in lines:
                try:
                    rows.append((ln, json.loads(ln)))
                except ValueError:
                    rows.append((ln, None))
            out[p] = rows
        elif p.name in _PLAN_FILES:
            doc = _read_json(p)
            if isinstance(doc, dict) and isinstance(doc.get("rows"), list):
                out[p] = doc
    return out


def _all_names(docs: dict, mid, names: set) -> set:
    """`names` plus every older or later name the journals give the meeting:
    an entry that names it carries the name it had then. Repeated until no
    new name turns up, so a chain of renames is followed to its start."""
    names = set(names)
    entries = []
    for v in docs.values():
        entries += v["rows"] if isinstance(v, dict) else [e for _, e in v]
    grew = True
    while grew:
        grew = False
        for e in entries:
            if _names_meeting(e, mid, names):
                for k in _NAME_KEYS:
                    if isinstance(e.get(k), str) and e[k] and e[k] not in names:
                        names.add(e[k])
                        grew = True
    return names


def _migration_plan(mid, names: set) -> dict:
    docs = _migration_docs()
    every = _all_names(docs, mid, names)
    files, n = {}, 0
    for p, v in docs.items():
        rows = v["rows"] if isinstance(v, dict) else v
        hit = sum(1 for r in rows if (_names_meeting(r, mid, every) if isinstance(v, dict)
                                      else _names_meeting(r[1], mid, every, r[0])))
        if hit:
            files[str(p)] = hit
            n += hit
    return {"names": sorted(every), "files": files, "lines": n}


def _write_private(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".purge.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _purge_migration(mid, names: set) -> int:
    """Drop the meeting's journal lines and plan rows. A journal left with
    no line is removed. The tsv beside a plan loses the same rows. Returns
    the number of lines and rows dropped."""
    docs = _migration_docs()
    every = _all_names(docs, mid, names)
    n = 0
    for p, v in docs.items():
        if isinstance(v, dict):
            keep = [r for r in v["rows"] if not _names_meeting(r, mid, every)]
            if len(keep) == len(v["rows"]):
                continue
            n += len(v["rows"]) - len(keep)
            _write_private(p, json.dumps({**v, "rows": keep}, ensure_ascii=False, indent=1))
            tsv = p.with_suffix(".tsv")
            try:
                lines = tsv.read_text(encoding="utf-8").splitlines()
            except (OSError, ValueError):
                continue
            # old_base and new_base are its first two columns
            lines = [ln for i, ln in enumerate(lines)
                     if i == 0 or not set(ln.split("\t")[:2]) & every]
            _write_private(tsv, "\n".join(lines) + "\n")
            continue
        keep = [ln for ln, e in v if not _names_meeting(e, mid, every, ln)]
        if len(keep) == len(v):
            continue
        n += len(v) - len(keep)
        if any(ln.strip() for ln in keep):
            _write_private(p, "\n".join(keep) + "\n")
        else:
            p.unlink(missing_ok=True)
    return n


def _migration_leftovers(mid, names: set) -> list:
    """Files in config.migration_dir() that the purge does not rewrite (a
    doctor run's whole-file copies, anything unknown) whose bytes still hold
    one of the meeting's names or its id. The journals and plans it rewrote
    are not listed: a line left in one belongs to another meeting."""
    d = config.migration_dir()
    if not d.is_dir():
        return []
    done = set()
    for p in _migration_docs():
        done |= {p, p.with_suffix(".tsv")} if p.name in _PLAN_FILES else {p}
    needles = _needles(mid, names)
    out = []
    for p in sorted(d.rglob("*")):
        try:
            if (p.is_file() and p not in done
                    and any(x in p.read_bytes() for x in needles)):
                out.append(str(p))
        except OSError:
            continue
    return out


def _names(base: str, meta: dict, keys) -> set:
    return ({base} | {n for n in (meta.get("source_name"), meta.get("source_file")) if n}
            | set(keys))


def plan(base: str) -> dict:
    """What a purge of `base` would remove. Reads only."""
    from stt import status
    folder, archived = locate(base)
    if folder is None:
        return {"ok": False, "error": f"no meeting '{base}'"}
    meta = _read_json(folder / f"{base}.json") or {}
    mid = meta.get("meeting_id") or config.id_of(base, folder.parent)
    from stt import centroids
    cmeta = _read_json(centroids.meta_path()) or {}
    keys = _manifest_keys(folder, base, meta)
    names = _names(base, meta, keys)
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
        "recent_rows": status.recent_naming(names),
        "migration": _migration_plan(mid, names),
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
        reg = unknowns.load_for_write()
        if reg is None:
            # damaged: nothing in it can be read, so nothing in it can be
            # dropped, and a save would put an empty registry over it
            print("   unknowns.json cannot be read: no unknown voice was "
                  "dropped from it.", file=sys.stderr)
            return 0
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
            for t in ("segs", "segs_raw", "docs", "docs_raw"):
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
    names = _names(base, meta, p["manifest_keys"])

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
    scrub["recent"] = status.forget_recent(names)
    every = set(p["migration"]["names"]) | names
    scrub["migration"] = _purge_migration(mid, every)
    p.update({"done": True, "enrolled_removed": enrolled,
              "unknowns_removed": n_unknowns, "scrubbed": scrub,
              "vm_blocked": blocked,
              "migration_left": _migration_leftovers(mid, every)})
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
    print(f"{verb}: {p['recent_rows']} recent result(s) in status.json")
    print(f"{verb}: {p['migration']['lines']} line(s) in "
          f"{len(p['migration']['files'])} undo journal(s) and rename plan(s)")
    if p.get("migration_left"):
        print(f"in the migration folder, {len(p['migration_left'])} other file(s) "
              "still name it; remove them by hand once their undo is not needed:")
        for o in p["migration_left"]:
            print("  " + o)
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
