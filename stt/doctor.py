"""Registry and store checks, and the repairs that are safe to automate
(unified plan Phase 3 step 2, design-files.md section 12).

Built on stt/registry_doctor.py, whose rule it keeps: `check` never writes.
The batch runs check() at the end of every run and stores the counts (no
titles) in doctor.json for the panel.

`fix` writes, and every write is undoable:

1. It refuses to run while a batch is running.
2. It copies each file it will change into a run folder under
   config.migration_dir() (mode 700), and journals the copy with the file's
   hash before and after.
3. Undo restores a file from its copy only where the file still holds what
   the fix wrote. A file changed since is kept and reported.

What `fix` repairs (section 12, minus the centroid rebuild, which the plan
drops):
- registry findings that registry_doctor.fix() already repairs (the whole
  voiceprints folder is copied first);
- manifest records whose outputs no longer exist: a flat path is rewritten
  to the folder layout, a record is relinked to the meeting whose stored
  source time matches it, then to the one whose title words and date match;
  same-day twins that tie on title words are paired one to one by time
  (`relink_tie`); anything left is moved to manifest_retired.json;
- a live meeting with no manifest record gets one, keyed by its
  `source_name` with its `source_mtime`, so a re-dropped source is skipped;
- queue holds on files that no longer exist;
- failure dismissals whose name's latest recorded result is a success;
- a stopping.flag left behind when no batch is running;
- logs over their cap and calibration.jsonl over its cap.

What it only reports: a folder name that disagrees with its `title` and
date, meetings missing the store keys (tools/backfill_keys.py adds them),
scratch audio left in work/, an stt.env value that cannot be read and is
replaced by its default (`bad_setting`), and (with deep=True) a stored
duration longer than the audio.
"""
import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from . import config, dates, housekeeping, names, provenance, registry_doctor, status
from . import fieldjournal as fj

REPORT_PATH_NAME = "doctor.json"
RETIRED_NAME = "manifest_retired.json"
TITLE_MATCH_MIN = 0.5


class Finding(NamedTuple):
    kind: str
    subject: str
    detail: str
    fixable: bool


def _from_registry(f) -> Finding:
    return Finding("registry_" + f.kind, f.subject, f.detail, f.fixable)


# ---------- reading ----------

def _store(store):
    return Path(store) if store else config.meetings_dir()


def _manifest() -> dict:
    m = fj.read_json(config.MANIFEST_PATH) or {}
    m.setdefault("processed", {})
    return m


def _core(rec) -> list:
    return [o for o in (rec.get("outputs") or []) if str(o).endswith((".txt", ".json"))]


def _stale(rec) -> bool:
    core = _core(rec)
    return bool(core) and not all(Path(o).exists() for o in core)


def _meetings(store) -> dict:
    out = {}
    for b in config.meeting_bases(store):
        d = fj.read_json(config.meeting_file(b, ".json", store))
        if d is not None:
            out[b] = d
    return out


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(name: str) -> set:
    s = os.path.splitext(str(name or ""))[0]
    s = dates._UNIQ_SUFFIX.sub("", s)
    s = re.sub(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", " ", s)   # ISO date stamps
    s = re.sub(r"(?<!\d)\d{8}(?!\d)", " ", s)          # older 8-digit stamps
    s = re.sub(r"\d{1,2}[.\-:/]\d{1,2}([.\-:/]\d{2,4})?", " ", s)   # short dates
    return {w for w in _WORD.findall(s.lower()) if len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a and b) else 0.0


def _rebase(outputs, store: Path, old_base: str, new_base: str) -> list:
    out = []
    for o in outputs:
        name = Path(o).name
        if name.startswith(old_base + "."):
            name = new_base + name[len(old_base):]
        out.append(str(store / new_base / name))
    return out


def _linked(m: dict) -> set:
    """Every json path some manifest record points at and that exists."""
    return {o for rec in m["processed"].values() for o in _core(rec)
            if o.endswith(".json") and Path(o).exists()}


def manifest_plan(store=None) -> list:
    """The manifest repairs, as actions. Reads only.

    [{"op": "flat"|"relink_mtime"|"relink_title"|"relink_tie"|"retire"|"link",
      "key": manifest key, "outputs": new outputs (not for retire),
      "meeting": base (not for retire), "mtime": (link only)}]"""
    store = _store(store)
    m = _manifest()
    meets = _meetings(store)
    linked = _linked(m)
    jpath = {b: str(config.meeting_file(b, ".json", store)) for b in meets}
    free = {b for b in meets if jpath[b] not in linked}
    acts = []
    ties = []
    for key, rec in sorted(m["processed"].items()):
        if not _stale(rec):
            continue
        outs = rec.get("outputs") or []
        old_json = next((o for o in _core(rec) if o.endswith(".json")), _core(rec)[0])
        old_base = Path(old_json).stem
        # R4: a flat <store>/<base>.json from before the folder layout
        if Path(old_json).parent == store and old_base in meets:
            new = _rebase(outs, store, old_base, old_base)
            if all(Path(o).exists() for o in _core({"outputs": new})):
                acts.append({"op": "flat", "key": key, "outputs": new,
                             "meeting": old_base})
                free.discard(old_base)
                continue
        # R1 by time: the meeting whose stored source time is this record's
        try:
            rm = float(rec.get("mtime"))
        except (TypeError, ValueError):
            rm = None
        hits = [b for b in free if rm is not None
                and meets[b].get("source_mtime") is not None
                and abs(float(meets[b]["source_mtime"]) - rm) < 1.0]
        if len(hits) == 1:
            b = hits[0]
            acts.append({"op": "relink_mtime", "key": key, "meeting": b,
                         "outputs": _rebase(outs, store, old_base, b)})
            free.discard(b)
            continue
        # R1 by title words and date
        day = dates.meeting_date(os.path.splitext(key)[0])
        if day is None and rm is not None:
            day = date.fromtimestamp(rm).isoformat()
        kt = _tokens(key) | _tokens(old_base)
        hits = [b for b in free if meets[b].get("date") == day
                and _jaccard(kt, _tokens(b)) >= TITLE_MATCH_MIN]
        if len(hits) == 1:
            b = hits[0]
            acts.append({"op": "relink_title", "key": key, "meeting": b,
                         "outputs": _rebase(outs, store, old_base, b)})
            free.discard(b)
            continue
        if len(hits) > 1:
            ties.append((key, rec, old_base, hits))
            continue
        acts.append({"op": "retire", "key": key})
    acts += _pair_ties(ties, free, meets, store)
    # R3: a live meeting with no record, rebuilt from its own keys
    for b in sorted(free):
        d = meets[b]
        key, mt = d.get("source_name"), d.get("source_mtime")
        if not key or mt is None or key in m["processed"]:
            continue
        acts.append({"op": "link", "key": key, "meeting": b, "mtime": float(mt),
                     "outputs": [jpath[b], str(config.meeting_file(b, ".txt", store))]})
    return acts


def _rec_time(rec) -> str:
    """A manifest record's processing time, for ordering twins."""
    if rec.get("processed_at"):
        return str(rec["processed_at"])
    try:
        return datetime.fromtimestamp(float(rec.get("mtime"))).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _meet_time(d) -> str:
    """When a meeting was first processed: `arrived_at`, else `processed_at`."""
    return str(d.get("arrived_at") or d.get("processed_at") or "")


def _pair_ties(ties, free: set, meets: dict, store: Path) -> list:
    """Relink records that tie on title words and date with two or more free
    meetings (same-day twins).

    Records are grouped by the set of meetings they tie with. A group pairs
    one to one only when it holds as many records as meetings and every
    meeting is still free: records in processing order against meetings in
    arrival order. Any other group is retired, as before."""
    groups = {}
    for key, rec, old_base, hits in ties:
        groups.setdefault(frozenset(hits), []).append((key, rec, old_base))
    acts = []
    for bases, recs in sorted(groups.items(), key=lambda g: sorted(g[0])):
        if len(recs) != len(bases) or not bases <= free:
            acts += [{"op": "retire", "key": k} for k, _, _ in recs]
            continue
        recs = sorted(recs, key=lambda r: (_rec_time(r[1]), r[0]))
        order = sorted(bases, key=lambda b: (_meet_time(meets[b]), b))
        for (key, rec, old_base), b in zip(recs, order):
            acts.append({"op": "relink_tie", "key": key, "meeting": b,
                         "outputs": _rebase(rec.get("outputs") or [], store, old_base, b)})
            free.discard(b)
    return acts


def _stale_holds() -> list:
    from . import holds
    names = []
    try:
        cur = json.loads(holds.PATH.read_text())
    except (OSError, ValueError):
        return names
    dirs = config.source_dirs()
    for n in cur or []:
        if not any((d / n).exists() for d in dirs):
            names.append(n)
    return sorted(names)


def _dismissals_path() -> Path:
    return config.PROJECT_DIR / "dismissed_failures.json"


def _stale_dismissals() -> list:
    """Dismissed names whose latest recorded result is a success: the
    failure they silenced is over. A name whose source is gone, or that has
    no result at all, keeps its dismissal, because clearing it could bring
    an old failure row back into the panel."""
    d = fj.read_json(_dismissals_path())
    if not isinstance(d, dict):
        return []
    latest = {}
    for r in status.history():          # newest first
        latest.setdefault(r.get("name"), r)
    return sorted(n for n in d if n in latest and latest[n].get("ok") is True)


def _batch_running() -> bool:
    return bool(status.read().get("running"))


def _stop_flag():
    from . import control
    return Path(control.STOP_FLAG)


def check(store=None, voiceprints_dir=None, deep=False, registry=True) -> list:
    """Every finding, registry and store. Reads only."""
    store = _store(store)
    out = []
    if registry:
        out += [_from_registry(f) for f in registry_doctor.check(voiceprints_dir, store)]
    meets = _meetings(store)
    for b, d in meets.items():
        missing = [k for k in provenance.KEYS if k not in d]
        if missing:
            out.append(Finding("missing_keys", b, "lacks " + ", ".join(missing), False))
        # the folder holds the title in its safe form: '1:1' is '1-1' on
        # disk. A folder from before the sanitizer may still hold the title
        # as typed, colon and all, and that is no mismatch either.
        # A long title's folder name is cut by bytes (config.cap_name).
        raw = d.get("title") or dates.title_of(b)
        stamped = {(dates.stamp(t, d["date"]) if d.get("date") else t)
                   for t in (names.file_title(raw) or raw, raw)}
        wants = {n + dates.twin_suffix(b)
                 for n in stamped | {config.cap_name(n) for n in stamped}}
        if b not in wants:
            why = "folder name differs from its title and date"
            if not dates.is_iso_form(b) and dates.meeting_date(b):
                why += " (older MMDDYYYY form: tools/iso_rename.py moves it)"
            out.append(Finding("name_mismatch", b, why, False))
        if deep:
            a = config.meeting_audio(b, store)
            if a is not None and d.get("duration_sec"):
                real = provenance.probe_tags(a).get("duration")
                if real and float(d["duration_sec"]) > real + 60:
                    out.append(Finding("duration_mismatch", b,
                                       "stored length is longer than the audio", False))
    for a in manifest_plan(store):
        kind = {"flat": "manifest_flat", "relink_mtime": "manifest_stale",
                "relink_title": "manifest_stale", "relink_tie": "manifest_stale",
                "retire": "manifest_stale",
                "link": "manifest_unlinked"}[a["op"]]
        out.append(Finding(kind, a["key"], a["op"], True))
    for n in _stale_holds():
        out.append(Finding("stale_hold", n, "held file no longer exists", True))
    for n in _stale_dismissals():
        out.append(Finding("stale_dismissal", n, "latest result is a success", True))
    if _stop_flag().exists() and not _batch_running():
        out.append(Finding("stale_stop_flag", _stop_flag().name,
                           "left behind with no batch running", True))
    if not _batch_running() and config.WORK_DIR.exists():
        n = sum(1 for p in config.WORK_DIR.glob("*.wav"))
        if n:
            out.append(Finding("work_scratch", "work", f"{n} scratch audio file(s)", False))
    from . import voicememos
    for kind, detail in voicememos.doctor_findings():
        out.append(Finding(kind, "voicememos", detail, False))
    for name in housekeeping.rotate_logs(dry_run=True):
        out.append(Finding("log_oversize", name, "over the log cap", True))
    loose = config.loose_private_paths()
    if loose:
        out.append(Finding("loose_mode", "permissions",
                           f"{len(loose)} private file(s) or folder(s) other "
                           "accounts can read (written before umask 077)", True))
    if housekeeping.cap_calibration(dry_run=True):
        out.append(Finding("calibration_oversize", "calibration.jsonl",
                           "over its cap", True))
    for name, why in config.setting_problems():
        out.append(Finding("bad_setting", name, why, False))
    return out


def summary(findings, store=None) -> dict:
    """Counts only, never a title: what the batch stores for the panel."""
    store = _store(store)
    c = Counter(f.kind for f in findings)
    try:
        du = shutil.disk_usage(store)
        free_gb = round(du.free / 1e9, 1)
    except OSError:
        free_gb = None
    return {"at": datetime.now().isoformat(timespec="seconds"),
            "findings": dict(sorted(c.items())),
            "fixable": sum(1 for f in findings if f.fixable),
            "meetings": len(config.meeting_bases(store)),
            "free_gb": free_gb}


def write_report(findings, store=None) -> Path:
    p = config.PROJECT_DIR / REPORT_PATH_NAME
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary(findings, store), indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


# ---------- fix, with a journal and an undo ----------

def _sha(p: Path):
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def _dir_hashes(d: Path) -> dict:
    if not d.is_dir():
        return {}
    return {str(p.relative_to(d)): _sha(p) for p in sorted(d.rglob("*")) if p.is_file()
            and not p.name.endswith(".lock")}


class _Run:
    """One fix run: its folder, its journal, and the file copies."""

    def __init__(self, run_id, journal_dir):
        self.id = run_id
        self.root = fj.private_dir(Path(journal_dir or config.migration_dir()) / run_id)
        self.journal = fj.Journal(fj.journal_path(run_id, journal_dir))
        self.n = 0
        self.pending = {}

    def before(self, path: Path):
        """Copy `path` before its first change in this run."""
        path = Path(path)
        if str(path) in self.pending:
            return
        self.n += 1
        copy = self.root / f"{self.n:03d}-{path.name}"
        existed = path.exists()
        if existed:
            shutil.copy2(path, copy)
            os.chmod(copy, 0o600)
        self.pending[str(path)] = {"file": str(path), "copy": str(copy) if existed else None,
                                   "sha_before": _sha(path)}

    def before_dir(self, d: Path):
        d = Path(d)
        if str(d) in self.pending:
            return
        copy = self.root / "voiceprints"
        if d.is_dir():
            shutil.copytree(d, copy, ignore=shutil.ignore_patterns("*.lock"))
        self.pending[str(d)] = {"dir": str(d), "copy": str(copy) if d.is_dir() else None,
                                "hashes_before": _dir_hashes(d)}

    def close(self):
        for k, e in self.pending.items():
            if "dir" in e:
                e["hashes_after"] = _dir_hashes(Path(e["dir"]))
                if e["hashes_after"] == e["hashes_before"]:
                    continue
            else:
                e["sha_after"] = _sha(Path(e["file"]))
                if e["sha_after"] == e["sha_before"]:
                    continue
            self.journal.write({"run": self.id, **e})
        self.journal.close()


def fix(findings=None, store=None, dry_run=True, journal_dir=None, run_id=None,
        registry=True) -> dict:
    """Apply the repairs the findings authorize. dry_run=True (the default)
    lists the actions and writes nothing."""
    store = _store(store)
    findings = check(store, registry=registry) if findings is None else findings
    kinds = Counter(f.kind for f in findings if f.fixable)
    reg = [f for f in findings if f.kind.startswith("registry_") and f.fixable]
    acts = manifest_plan(store)
    plan = {"registry": len(reg),
            "manifest": dict(Counter(a["op"] for a in acts)),
            "holds": kinds["stale_hold"], "dismissals": kinds["stale_dismissal"],
            "stop_flag": kinds["stale_stop_flag"],
            "logs": kinds["log_oversize"], "calibration": kinds["calibration_oversize"],
            "permissions": kinds["loose_mode"]}
    if dry_run:
        return {"dry_run": True, "plan": plan, "run": None, "done": []}
    if _batch_running():
        return {"dry_run": False, "plan": plan, "run": None,
                "done": [], "error": "a batch is running; try again when it ends"}
    run = _Run(run_id or datetime.now().strftime("doctor-%Y%m%dT%H%M%S"), journal_dir)
    done = []
    try:
        if reg:
            run.before_dir(config.VOICEPRINTS_DIR)
            done += registry_doctor.fix(
                [registry_doctor.Finding(f.kind[len("registry_"):], f.subject,
                                         f.detail, f.fixable) for f in reg],
                meetings_dir=store)
        if acts:
            from . import manifest
            run.before(config.MANIFEST_PATH)
            retired_p = config.PROJECT_DIR / RETIRED_NAME
            retired = fj.read_json(retired_p) or {}
            with manifest.update() as m:
                for a in acts:
                    rec = m["processed"].get(a["key"])
                    if a["op"] == "retire":
                        if rec is not None and _stale(rec):
                            retired[a["key"]] = {**rec, "retired_at":
                                                 datetime.now().isoformat(timespec="seconds")}
                            m["processed"].pop(a["key"], None)
                            done.append("manifest: retired one stale record")
                    elif a["op"] == "link":
                        if a["key"] not in m["processed"]:
                            m["processed"][a["key"]] = {
                                "mtime": a["mtime"], "outputs": a["outputs"],
                                "processed_at": datetime.now().isoformat(timespec="seconds"),
                                "relinked": "doctor"}
                            done.append("manifest: linked one meeting")
                    elif rec is not None:
                        rec["outputs"] = a["outputs"]
                        rec["relinked"] = a["op"]
                        done.append(f"manifest: {a['op']} one record")
                        if a["op"] == "relink_tie":
                            # a pairing by order is a judgment: keep it on
                            # record so a wrong pair can be found and swapped
                            run.journal.write({"run": run.id, "tie": a["key"],
                                               "meeting": a["meeting"]})
            if retired:
                run.before(retired_p)
                tmp = retired_p.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(retired, indent=2))
                os.chmod(tmp, 0o600)
                os.replace(tmp, retired_p)
        stale_h = _stale_holds()
        if stale_h:
            from . import holds
            run.before(holds.PATH)
            for n in stale_h:
                holds.release(n)
            done.append(f"cleared {len(stale_h)} stale hold(s)")
        stale_d = _stale_dismissals()
        if stale_d:
            p = _dismissals_path()
            run.before(p)
            d = fj.read_json(p) or {}
            for n in stale_d:
                d.pop(n, None)
            fj.atomic_json(p, d)
            done.append(f"cleared {len(stale_d)} stale dismissal(s)")
        flag = _stop_flag()
        if flag.exists() and not _batch_running():
            run.before(flag)
            flag.unlink(missing_ok=True)
            done.append("removed a stale stopping.flag")
        # logs and the calibration log are capped, not journaled: the old
        # lines stay in the gzip generations beside them
        hk = housekeeping.rotate_all()
        if hk["logs"]:
            done.append(f"rotated {len(hk['logs'])} log(s)")
        if hk["calibration"]:
            done.append("capped calibration.jsonl")
        # a mode change is not journaled: it only takes away other accounts'
        # access, and `chmod` puts it back if ever wanted
        if kinds["loose_mode"]:
            n = config.tighten_private_paths()
            if n:
                done.append(f"made {n} private file(s) or folder(s) owner-only")
    finally:
        run.close()
    return {"dry_run": False, "plan": plan, "run": run.id,
            "journal": str(run.journal.path), "done": done}


def undo(run_id: str, journal_dir=None, dry_run=True) -> dict:
    """Restore every file the run changed, where the file still holds what
    the run wrote. A file changed since is kept and reported."""
    restored, kept = [], []
    for e in reversed(fj.entries(run_id, journal_dir)):
        if "dir" not in e and "file" not in e:
            continue                  # a note (a tie pairing), not a change
        if "dir" in e:
            d = Path(e["dir"])
            if _dir_hashes(d) != e["hashes_after"]:
                kept.append(e["dir"])
                continue
            if not dry_run:
                from . import identify
                with identify.lock_registry():
                    for rel in e["hashes_after"]:
                        if rel not in e["hashes_before"]:
                            (d / rel).unlink(missing_ok=True)
                    for rel in e["hashes_before"]:
                        dst = d / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(Path(e["copy"]) / rel, dst)
            restored.append(e["dir"])
            continue
        p = Path(e["file"])
        if _sha(p) != e.get("sha_after"):
            kept.append(e["file"])
            continue
        if not dry_run:
            if e["copy"] is None:
                p.unlink(missing_ok=True)
            else:
                tmp = p.with_name(p.name + ".undo.tmp")
                shutil.copy2(e["copy"], tmp)
                os.replace(tmp, p)
        restored.append(e["file"])
    return {"run": run_id, "restored": restored, "kept": kept, "dry_run": dry_run}


def run_check_at_batch_end(store=None):
    """Called by run_batch at the end of a run. Never raises."""
    try:
        t0 = time.monotonic()
        f = check(store)
        write_report(f, store)
        return {"findings": len(f), "secs": round(time.monotonic() - t0, 1)}
    except Exception as e:
        return {"error": str(e)}
