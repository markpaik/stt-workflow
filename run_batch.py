#!/usr/bin/env python
"""Batch entrypoint: transcribe + diarize NEW meeting recordings (audio or video).

For each new file in the source (iCloud) folder:
  materialize -> transcribe+diarize+name -> write .txt/.json -> copy audio into the
  meetings store (video: extract the audio track) -> on success only, delete the
  iCloud original (move-after-success). A file that errors keeps its original.

Safety rails: single-instance flock; battery guard; pause flag (automatic triggers
no-op while paused; manual runs pass --ignore-pause); SIGTERM stops gracefully
(current file abandoned safely — original kept, outputs atomic, scratch cleaned
next start); scratch cleanup at start.

Selection & throughput: --files picks exact files (GUI picker); --parallel 2 runs
two files at once (each worker loads its own models; ~1.6-1.8x throughput on the
M5 Pro — diarization is CPU-bound at ~5 cores per file).

Order: the SHORTEST file left runs next (stt.queue_order), chosen before every
file rather than fixed when the run starts. A plain run re-reads the watched
folders at each pick, so a short recording that lands mid-run goes ahead of
queued multi-hour meetings; each --parallel worker takes the shortest file left.

Stop after the current file: `stop_after_file.flag` in the project folder
(stt.control.request_finish) is read before every file. Once it exists the run
starts nothing new, lets the file in flight finish, and ends cleanly (no
chained job). The pause flag, by contrast, only gates a run's START.
"""
import argparse
import errno
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path


def _env_file_dir(repo=None) -> Path:
    """The folder whose stt.env holds the settings: config.PROJECT_DIR's rule,
    worked out before config imports. STT_PROJECT_DIR, else STT_HOME, else
    this checkout. A sandbox (STT_HOME) must never load the checkout's file,
    which names the REAL library and iCloud folders."""
    for k in ("STT_PROJECT_DIR", "STT_HOME"):
        v = os.environ.get(k)
        if v:
            return Path(v).expanduser()
    return Path(repo) if repo else Path(__file__).resolve().parent


def _load_env_file(repo=None):
    """Load KEY=VALUE lines from stt.env BEFORE importing the package (config reads
    env at import). launchd does not read ~/.zshrc."""
    p = _env_file_dir(repo) / "stt.env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

from stt import (audio, config, control, icloud, jobs, manifest, output,  # noqa: E402
                 queue_order, rates, status)

# a plain run re-reads the Voice Memos library at most this often while it
# picks files, so a memo that finishes during a long run joins that run
VM_RESYNC_SECONDS = 60.0


def acquire_lock():
    """The single-instance lock. The holder writes its pid into batch.lock,
    and the panel finds this home's batch by that pid (control._parent_pids).
    The file is opened without truncating it ("a+"), and emptied only once
    the lock is held: a run that loses the race never blanks the pid of the
    batch that holds it."""
    fd = open(config.PROJECT_DIR / "batch.lock", "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def battery_ok() -> bool:
    try:
        out = subprocess.run(["/usr/bin/pmset", "-g", "batt"],
                             capture_output=True, text=True, timeout=10).stdout
        if "discharging" not in out:
            return True
        for tok in out.replace(";", " ").split():
            if tok.endswith("%"):
                return int(tok.rstrip("%")) >= config.BATTERY_FLOOR
    except Exception:
        pass
    return True


def clean_scratch():
    if config.WORK_DIR.exists():
        for p in config.WORK_DIR.glob("*.wav"):
            p.unlink(missing_ok=True)
        # speakrs helper output left by a killed run (its finally never ran)
        from stt import speakrs_diar
        for p in config.WORK_DIR.glob(speakrs_diar.TMP_PREFIX + "*"):
            shutil.rmtree(p, ignore_errors=True)
    for p in (list(config.MEETINGS_DIR.glob("*.tmp")) + list(config.MEETINGS_DIR.glob("*/*.tmp"))
              + list(config.MEETINGS_DIR.glob("*.part")) + list(config.MEETINGS_DIR.glob("*/*.part"))):
        p.unlink(missing_ok=True)
    _sweep_shells()


def _sweep_shells() -> int:
    """Drop meeting folders left behind with NO transcript json — a run that
    dies inside the write phase (process_file makes the folder just before it)
    strands an empty shell (or one holding just a stray .txt). They are invisible in the
    panel (meeting_bases requires the json) but they pile up, and they used to
    push every retry onto the next ' (N)' suffix.

    Safe by construction: this runs at batch start holding the single-instance
    lock, so nothing is mid-write, and a folder with no json is by definition not
    a meeting. Anything holding AUDIO is left alone — that would be real data.

    Removal goes through archive.purge_meeting_dir, the same path as the user's
    Delete: a run can die AFTER registering unknowns for a file but BEFORE the
    json write, so the shell's name may already sit in unknowns' 'heard in'
    lists — sweeping the folder without the scrub left speakers claiming
    meetings that no longer exist (U005/U007/U009)."""
    from stt import archive
    n = 0
    try:
        for p in sorted(config.MEETINGS_DIR.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            if (p / f"{p.name}.json").exists():
                continue
            if any(f.suffix.lower() in config.AUDIO_SUFFIXES for f in p.iterdir()):
                continue  # holds audio: never ours to delete
            archive.purge_meeting_dir(p)
            n += 1
    except OSError:
        pass
    if n:
        print(f"cleaned up {n} empty meeting folder(s) from interrupted runs", flush=True)
    return n


def job_spec_from_args(args, todo) -> dict:
    """The queued-job dict equivalent to this invocation's CLI args. Used to
    re-queue a --job run if it's interrupted before finishing (see
    _terminate() in main()) — manifest idempotency makes replaying it, files
    already done included, a safe no-op for anything that already succeeded."""
    spec = {"files": jobs.split_list(args.files) if args.files else [],
            "paths": jobs.split_list(args.paths) if args.paths else [],
            "force": args.force, "strict": args.strict,
            "verify": args.verify, "parallel": args.parallel,
            "onetime": getattr(args, "one_time_speakers", False),
            "label": (", ".join(s.name for s in todo[:2])
                     + (f" +{len(todo) - 2} more" if len(todo) > 2 else "")) or "resumed run"}
    # the CLI-only options too, so a replay runs exactly what was asked: a
    # --keep-original run must never delete originals on its replay, and a
    # --only run must never sweep the whole folder. Only set ones are kept.
    for key in ("no_diarize", "keep_original", "ignore_battery"):
        if getattr(args, key, False):
            spec[key] = True
    for key in ("only", "speakers"):
        if getattr(args, key, None):
            spec[key] = getattr(args, key)
    # a run into other folders replays into the same ones: without them a
    # sandbox or test run's replay wrote into the real library. The default
    # folders are left out, so a panel job stays as the panel queued it.
    for key, default in (("source", config.ICLOUD_DIR), ("dest", config.MEETINGS_DIR)):
        v = getattr(args, key, None)
        if v and Path(v) != Path(default):
            spec[key] = str(v)
    return spec


def spawn_chained_job(job: dict):
    """Start the next panel-queued job at the end of a run, logging to the
    same spawned.log the GUI's own _spawn() uses for the identical purpose —
    stdout/stderr to DEVNULL would leave zero trace if this child dies
    immediately (bad venv, a bad line in stt.env under set -euo pipefail)."""
    from stt import jobs
    logs_dir = config.PROJECT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    args = jobs.spawn_args(job)
    # `with` so the parent releases its copy of the log fd once the child dups it
    with open(logs_dir / "spawned.log", "a") as log:
        log.write(f"\n--- {' '.join(str(a) for a in args)}\n")
        log.flush()
        subprocess.Popen(args, start_new_session=True, stdout=log, stderr=log)


def summarize_one(key, base=None, track=None) -> bool:
    """Draft the AI title/summary for ONE just-finished meeting (local Qwen
    via .venv-llm). Runs right after the file completes, not at end-of-run:
    with a long backlog the end is hours away, and a Stop before it would
    lose the summaries for everything already finished. Quietly a no-op when
    the LLM venv isn't installed or the meeting already has a summary; a
    failure never breaks the run (the end-of-run sweep retries it).
    `base` is the RESOLVED meeting folder — date-stamped for plain-named
    sources, so it can differ from the source stem. The automatic title can
    rename that folder; a `track` dict gets the folder's final name under
    "base", so a later retry never reads the old, now-missing folder."""
    import json as _json

    from stt import status as st, summarize
    if not summarize.available():
        return False
    base = base or Path(key).stem
    done = False
    try:
        d = _json.loads(config.meeting_file(base, ".json").read_text())
        if d.get("ai_summary"):
            # e.g. a re-queued file that was already summarized. Its automatic
            # title may still be owed (an earlier attempt failed), so the
            # title chain's last step runs here too. It is a no-op once titled.
            base = _auto_title(base)
            if track is not None:
                track["base"] = base
            return False
        if not summarize.has_speech(base):
            # 0 segments: the model would invent a meeting. The pipeline
            # already set no_speech, and the panel says "No speech found".
            print(f"   no speech in {base}: no summary written", flush=True)
            return False
        st.set_stage(key, "summarizing", est_sec=st.summary_estimate())
        t0 = time.monotonic()
        summarize.suggest_title(base)
        st.note_summary(time.monotonic() - t0)
        done = True
    except Exception as e:
        print(f"   summary failed for {base}: {e}", file=sys.stderr, flush=True)
        return False
    finally:
        st.clear_stage(key)
    # the title chain's last steps, once the stage has cleared (a meeting in
    # a live stage refuses a folder move): a meeting with no typed title
    # takes the speakers plus topic, else the model's draft
    base = _auto_title(base)
    if track is not None:
        track["base"] = base
    return done


def _auto_title(base) -> str:
    """summarize.apply_auto_title for one meeting. Returns the meeting's
    folder name afterwards (the new one when the title renamed it). A failure
    is printed and never breaks the run."""
    from stt import summarize
    try:
        r = summarize.apply_auto_title(base)
        if r.get("ok") and r.get("base") and r["base"] != base:
            print(f"   titled: {r['base']}", flush=True)
            return r["base"]
    except Exception as e:
        print(f"   automatic title skipped for {base}: {e}", file=sys.stderr, flush=True)
    return base


# descriptions a Redo left out of date, regenerated per run once the queue
# is empty: a few at a time, so a burst of Redos never holds the batch (and
# the files that land meanwhile) for an hour
STALE_PER_RUN = 3


def regenerate_stale(limit: int = STALE_PER_RUN) -> int:
    """Regenerate up to `limit` descriptions marked out of date (ai_stale,
    set by a Redo), shortest transcript first. Runs only when the batch is
    idle: the caller calls it after the queue is done, and it stops early
    when a "stop after this file" is asked or a panel job is queued. The
    meeting's title is never touched. Only a description that was really
    stored counts. A meeting with no speech has its flag cleared. A failure
    backs the meeting off (summarize.stale_failed), so one bad meeting is
    not retried on every run. Returns how many were regenerated."""
    from stt import status as st, summarize
    if limit <= 0 or not summarize.available():
        return 0
    todo = summarize.stale_bases()
    n = 0
    for i, base in enumerate(todo):
        if n >= limit or control.finish_requested() or jobs.items():
            break
        if not summarize.has_speech(base):
            summarize.clear_stale(base)
            print(f"   no speech in {base}: out-of-date flag cleared", flush=True)
            continue
        print(f"   refreshing the out-of-date description of {base}", flush=True)
        t0 = time.monotonic()
        ok, why = False, ""
        try:
            st.set_stage(base, "summarizing", base=base, est_sec=st.summary_estimate())
            r = summarize.suggest_title(base)
            if isinstance(r, dict) and r.get("stored"):
                ok = True
            else:
                why = (r or {}).get("error") or "the description was not stored"
        except Exception as e:
            why = str(e)
        finally:
            st.clear_stage(base)
        if ok:
            n += 1
        else:
            fails = summarize.stale_failed(base, why)
            print(f"   description refresh failed for {base} ({fails} so far, "
                  f"backing off): {why}", file=sys.stderr, flush=True)
        # the duty cycle (stt/config.py): rest after each one, except the
        # last this run will do
        if n < limit and i + 1 < len(todo):
            config.llm_rest(time.monotonic() - t0, log=print)
    return n


DOCTOR_EVERY_SECONDS = 24 * 3600


def doctor_due(processed: int, failed: int) -> bool:
    """True when the end-of-run doctor check should run: a file finished or
    failed this run, or the last report is a day old or missing. The watch
    trigger starts an empty run every few minutes, and a full store check on
    each of them is wasted work."""
    if processed or failed:
        return True
    from stt import doctor
    try:
        age = time.time() - (config.PROJECT_DIR / doctor.REPORT_PATH_NAME).stat().st_mtime
    except OSError:
        return True
    return age >= DOCTOR_EVERY_SECONDS


def record_review_metrics() -> bool:
    """Append today's review counts (tools/review_metrics.py) once a day, at
    the end of a run. Counts only, never a title. A failure here never fails
    the run."""
    try:
        import importlib.util
        path = Path(__file__).resolve().parent / "tools" / "review_metrics.py"
        spec = importlib.util.spec_from_file_location("review_metrics", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return bool(mod.record_today())
    except Exception as e:
        print(f"  (review metrics skipped: {e})", file=sys.stderr, flush=True)
        return False


def auto_summarize(keys) -> int:
    """End-of-run sweep: summarize anything from this run still missing a
    description. Normally each file was summarized the moment it finished
    (see summarize_one in _record); this catches the ones whose inline pass
    failed mid-run."""
    from stt import summarize
    if not keys:
        return 0
    if not summarize.available():
        print("  (auto-summary skipped: local LLM not installed)")
        return 0
    n = 0
    keys = list(keys)
    for i, item in enumerate(keys):  # (key, resolved_base) pairs; bare keys tolerated
        key, base = item if isinstance(item, tuple) else (item, None)
        t0 = time.monotonic()
        did = bool(summarize_one(key, base))
        n += did
        if did and i + 1 < len(keys):
            # the duty cycle: rest after each summary the model wrote,
            # never after the last one
            config.llm_rest(time.monotonic() - t0, log=print)
    return n


def warn_if_registry_lost(dest: Path):
    """An empty voiceprint registry alongside meetings that HAVE named
    speakers means enrollments were lost (deleted file, moved folder) — every
    new meeting would silently come out anonymous. Say so loudly; the fix is
    one command."""
    import json as _json

    from stt import identify
    if identify.load_registry():
        return False
    for base in config.meeting_bases(dest):
        try:
            d = _json.loads(config.meeting_file(base, ".json", dest).read_text())
        except (OSError, ValueError):
            continue
        if any(s.get("name") for s in d.get("speakers", [])):
            print("WARNING: the voiceprint registry is EMPTY but existing "
                  "transcripts have named speakers — enrollments look lost. "
                  "New meetings will come out anonymous until it is restored:\n"
                  "  ./run.sh py tools/rebuild_voiceprints.py --apply\n"
                  "rebuilds every voiceprint from the meeting caches.",
                  file=sys.stderr, flush=True)
            try:
                subprocess.run(["/usr/bin/osascript", "-e",
                                'display notification "Voiceprint registry is empty — '
                                'speaker names will be missing. See logs." '
                                'with title "STT workflow"'],
                               capture_output=True, timeout=10)
            except Exception:
                pass
            return True
    return False


def preflight_source(source: Path) -> bool:
    try:
        next(source.iterdir(), None)
        return True
    except PermissionError:
        print(f"PERMISSION DENIED reading {source} — Full Disk Access is missing for "
              f"this interpreter ({sys.executable}).", file=sys.stderr)
        try:
            subprocess.run(["/usr/bin/osascript", "-e",
                            'display notification "STT batch: Full Disk Access missing" '
                            'with title "STT workflow"'], capture_output=True, timeout=10)
        except Exception:
            pass
        return False


def sync_voice_memos(dry_run=False) -> dict:
    """Copy finished meeting memos from the Voice Memos library into the
    staging folder (stt/voicememos.py). Reads the library and never writes
    to it. A schema change or a denied read copies nothing and is recorded
    for the doctor. Never fails the run."""
    try:
        from stt import voicememos
        r = voicememos.sync(dry_run=dry_run)
    except Exception as e:
        print(f"  (Voice Memos sync skipped: {e.__class__.__name__})",
              file=sys.stderr, flush=True)
        # a sync that raised still leaves a record, so the doctor reports it
        try:
            if not dry_run:
                voicememos.record_failure(e)
        except Exception:
            pass
        return {"status": "error"}
    st = r.get("status")
    if st == "ok":
        c = r.get("counts") or {}
        if c.get("copied") or c.get("would_copy"):
            print(f"Voice Memos: {'would copy' if dry_run else 'copied'} "
                  f"{c.get('copied') or c.get('would_copy')} memo(s)", flush=True)
    elif st == "schema":
        print("Voice Memos: the database schema changed, so sync stopped "
              f"({r.get('detail')}). Run the doctor.", file=sys.stderr, flush=True)
    elif st == "denied":
        print("Voice Memos: macOS denied the read. Grant Full Disk Access to "
              f"{os.path.realpath(sys.executable)}.", file=sys.stderr, flush=True)
    return r


def note_voice_memo_ingest(res: dict, dest) -> bool:
    """Tell the Voice Memos sync that the batch stored this staged memo
    (stt/voicememos.py note_ingested). Called right after the manifest
    records a success, for a run into the library only: the delete step
    needs this proof. Never fails the run."""
    uuid = res.get("vm_uuid")
    if not uuid:
        return False
    try:
        from stt import voicememos
        mid = config.id_of(res.get("base"), dest) if res.get("base") else None
        return voicememos.note_ingested(uuid, res.get("key"), res.get("base"), mid)
    except Exception as e:
        print(f"  (Voice Memos ingest note skipped: {e.__class__.__name__})",
              file=sys.stderr, flush=True)
        return False


def delete_voice_memos(live_store: bool) -> dict:
    """Once per run, after the descriptions: move each memo the batch has
    stored to Voice Memos' Recently Deleted (STT_VM_DELETE_AFTER_INGEST=1,
    off by default; stt/voicememos.py delete_pass states every rule). A
    run into a folder other than the library never deletes. One line per
    memo removed goes to the log. Never blocks or fails the run: the pass
    is bounded, and anything it raises is caught here."""
    if not live_store:
        return {"status": "not_library"}
    try:
        from stt import voicememos
        return voicememos.delete_pass(
            log=lambda s: print(s, flush=True),
            warn=lambda s: print(s, file=sys.stderr, flush=True))
    except Exception as e:
        print(f"  (Voice Memos delete skipped: {e.__class__.__name__}: {e})",
              file=sys.stderr, flush=True)
        return {"status": "error"}


def _empty_audio(p) -> bool:
    """A 0-byte file of an audio type in a watched folder: iter_audio skips
    it until it has bytes."""
    p = Path(p)
    try:
        return (p.suffix.lower() in config.AUDIO_EXTS and not p.name.startswith((".", "~$"))
                and p.is_file() and p.stat().st_size == 0)
    except OSError:
        return False


def iter_audio(folder: Path):
    if not Path(folder).is_dir():
        return
    for p in sorted(Path(folder).iterdir()):
        if config.is_source_audio(p):
            yield p


def iter_sources(source: Path):
    """Sweep the primary source folder AND the local recordings staging folder
    (the meeting recorder's output). Dot-prefixed in-progress captures are
    already skipped by iter_audio; distinct filenames keep the manifest dedupe
    unambiguous across folders."""
    seen = set()
    # the source, the recorder's staging folder, and the Voice Memos sync's
    # own staging copies (config.source_dirs, the one shared list). The Voice
    # Memos container itself is never a source, so the delete step cannot
    # reach it.
    for folder in config.source_dirs(source):
        for p in iter_audio(folder):
            if p.name in seen:
                # two files that merely SHARE a basename across the source and
                # recordings folders would collide on the same meeting folder
                # (base = stem) and the same manifest key: one silently overwrites
                # or hides the other. Process the first and say so, so the clash
                # is never invisible. (Recorder names are uniquified against the
                # store, so this is only a hand-named / re-exported collision.)
                print(f"   note: '{p.name}' appears in more than one watched "
                      f"folder — processing the first, skipping {p}",
                      file=sys.stderr, flush=True)
                continue
            seen.add(p.name)
            yield p


def _sha256(path: Path):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def safe_to_delete(src: Path, dest_audio: Path, src_fp, src_size):
    """(ok, reason): may the batch delete the source now that the meeting
    holds its copy (unified plan Phase 2 step 2)?

    The source must still be the file the run started on (same size and
    fingerprint as before processing), and the stored copy must hold the
    same bytes: equal size and equal SHA-256. A video's stored audio is an
    extracted track that shares no bytes with it, so for a video the check
    is the source being unchanged plus a non-empty extracted track. Any
    doubt keeps the source, which the manifest then skips."""
    from stt import dupes
    try:
        size_now = src.stat().st_size
        dsize = dest_audio.stat().st_size
    except OSError as e:
        return False, f"cannot read size ({e.__class__.__name__})"
    if src_size is not None and size_now != src_size:
        return False, "the source changed size during processing"
    if src_fp and dupes.fingerprint(src) != src_fp:
        return False, "the source changed during processing"
    if src.suffix.lower() in config.VIDEO_EXTS:
        return (dsize > 0, "ok" if dsize > 0 else "the extracted audio is empty")
    if dsize != size_now:
        return False, "the stored copy differs in size"
    a, b = _sha256(src), _sha256(dest_audio)
    if a is None or b is None or a != b:
        return False, "the stored copy differs in content"
    return True, "ok"


def process_one(src_str: str, dest_str: str, opts: dict) -> dict:
    """Process a single file end-to-end (runs in a worker process under --parallel).
    Returns {ok, key, mtime, outputs, summary, duration_sec, stage_secs}.
    Never touches the manifest."""
    import time as _time

    from stt import audio, icloud as ic, pipeline, status as st
    src, dest = Path(src_str), Path(dest_str)
    key = src.name
    mtime = src.stat().st_mtime

    # the meeting folder this file will land in — date-stamped for plain names,
    # so it can DIFFER from src.stem. Announced in the live status so the panel's
    # is-this-meeting-being-written-right-now guards (rename/archive/delete)
    # recognize the stamped name too, not just the source stem.
    resolved_base = pipeline.resolve_base(src, dest)

    # time each stage (monotonic: does not tick during sleep, so a lid-close
    # can't poison the speed calibration) and forward to the live status
    stage_secs = {}
    cur = {"stage": None, "t": None}

    def report(stage, progress=None, duration=None, k=key):
        now = _time.monotonic()
        if cur["stage"] and cur["stage"] != stage:
            stage_secs[cur["stage"]] = stage_secs.get(cur["stage"], 0.0) + (now - cur["t"])
        if cur["stage"] != stage:
            cur["stage"], cur["t"] = stage, now
        st.set_stage(k, stage, progress=progress, duration=duration, base=resolved_base,
                    diarize=opts["do_diarize"], verify=opts.get("verify", False))

    report("downloading")
    if src.stat().st_size == 0:
        # no bytes is no audio: one line, like any file ffmpeg cannot read,
        # never a traceback
        raise audio.UnreadableAudio(f"{key} is empty (0 bytes)")
    if not ic.materialize(src):
        raise RuntimeError("iCloud file did not fully materialize")
    # resolved again now the bytes are here: a Voice Memos date tag cannot
    # be read from a dataless placeholder, so the first answer may have
    # fallen back to the file day. process_file resolves the same way.
    resolved_base = pipeline.resolve_base(src, dest)

    # the meeting recorder drops a <base>.opts.json next to a stereo capture,
    # declaring its me/them channel layout; its contents are copied into the
    # meeting .json by process_file (so a later Redo needs no sidecar)
    import json as _json
    input_opts = None
    side = src.with_suffix(".opts.json")
    try:
        if side.exists():
            input_opts = _json.loads(side.read_text())
    except (OSError, ValueError):
        # unreadable, or a name past the 255-byte limit (no such sidecar)
        input_opts = None

    # the source's content fingerprint, taken NOW: after this returns the file
    # may have been moved into the meeting folder (or, for a video, replaced by
    # an extracted .m4a that shares none of its bytes). Recorded on the manifest
    # so the panel can recognize this exact recording if it ever comes back
    # under another name. Materialized above, so no iCloud download is forced.
    from stt import dupes
    src_fp = dupes.fingerprint(src)
    src_size = src.stat().st_size
    # a Voice Memos sync copy names its memo in the sidecar. Read it now:
    # the sidecar is deleted with the staged copy after a verified store,
    # and the delete-after-ingest step needs to know which memo this was
    from stt import provenance as _prov
    vm_uuid = _prov.read_sidecar(src).get("vm_uuid")

    res = pipeline.process_file(
        src, dest_dir=dest, do_diarize=opts["do_diarize"], strict=opts["strict"],
        do_verify=opts.get("verify", False),
        allowed_names=opts["allowed"], report=report,
        track_unknowns=opts.get("track_unknowns", True), input_opts=input_opts)
    if cur["stage"]:  # close out the final stage (writing ends when we return)
        stage_secs[cur["stage"]] = stage_secs.get(cur["stage"], 0.0) + (_time.monotonic() - cur["t"])
    outputs = [res["txt"], res["json"]] + ([res["emb"]] if res["emb"] else [])

    # everything after transcription keys off the base process_file RESOLVED —
    # which is date-stamped for plain-named sources, not src.stem. Deriving the
    # folder from the stem here targeted a folder that was never created: the
    # audio copy raised, the file was marked FAILED, and every subsequent run
    # re-transcribed it from scratch, forever.
    base = res["base"]
    if src.suffix.lower() in config.VIDEO_EXTS:
        dest_audio = config.meeting_file(base, ".m4a", dest)
        if not dest_audio.exists():
            audio.extract_audio(src, dest_audio)
    else:
        dest_audio = config.meeting_file(base, src.suffix, dest)
        if src.resolve() != dest_audio.resolve():
            # atomic: copy to a sibling .part then os.replace, so a crash mid-copy
            # (or a killed worker) can't leave a truncated audio that success=
            # dest_audio.exists() would count as done. Matches audio.extract_audio.
            # The meetings store is not a watched source, so the .part is inert;
            # clean_scratch (MEETINGS_DIR/*/*.part) sweeps any leftover next start.
            tmp = dest_audio.with_name(dest_audio.name + ".part")
            shutil.copy2(src, tmp)
            os.replace(tmp, dest_audio)

    success = res["txt"].exists() and res["json"].exists() and dest_audio.exists()
    if success and opts["do_move"] and src.resolve() != dest_audio.resolve():
        ok, why = safe_to_delete(src, dest_audio, src_fp, src_size)
        if ok:
            src.unlink()
            from stt import provenance
            # the layout now lives in the meeting .json. A sidecar name past
            # the 255-byte limit raises, and no such sidecar can exist
            for extra in (side, provenance.sidecar_path(src)):
                try:
                    extra.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            print(f"   kept the source {src.name}: {why}", file=sys.stderr, flush=True)

    who = ("  identified: " + ", ".join(res["identified"])) if res["identified"] else ""
    summary = f"{res['n_speakers']} speaker(s), {output.length_text(res['duration_sec'])}"
    return {"ok": success, "key": key, "base": base, "mtime": mtime,
            "fp": src_fp, "size": src_size, "vm_uuid": vm_uuid,
            "outputs": [str(o) for o in outputs + [dest_audio]],
            "summary": summary, "who": who,
            "duration_sec": res["duration_sec"], "stage_secs": stage_secs,
            "diarizer": res.get("diarizer")}


def _descendants(pid: int) -> list:
    """Every live descendant of `pid` (children, their children, ...), from
    one pass over the process table. Empty when ps cannot run."""
    try:
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    kids = {}
    for ln in out.splitlines():
        try:
            child, parent = (int(x) for x in ln.split())
        except ValueError:
            continue
        kids.setdefault(parent, []).append(child)
    found, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in found:
                found.append(c)
                todo.append(c)
    return found


def _stop_own_processes():
    """SIGTERM every process this batch started (the --parallel workers,
    ffmpeg, the speakrs helper), so none survives as a multi-GB orphan.

    When the batch leads its process group (launchd starts it so), the
    whole group goes, ourselves included. When it does NOT lead it, the
    group belongs to whoever started it: a shell or script that ran the
    batch as a background job, or caffeinate for a panel run. A group kill
    then also ended that caller, so only our own descendants are signalled,
    and the caller sees the batch exit."""
    me = os.getpid()
    try:
        pgid = os.getpgid(0)
    except OSError:
        pgid = None
    if pgid == me:
        os.killpg(pgid, signal.SIGTERM)  # includes ourselves
        return
    for pid in _descendants(me):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def _machine_at_fault(e) -> bool:
    """True when a failure is this Mac's, not the file's: a full disk, or a
    folder the batch may not write. It never counts toward setting the file
    aside, so the file runs again once space is freed."""
    if isinstance(e, audio.WriteFailed):
        return True
    return isinstance(e, OSError) and e.errno in (errno.ENOSPC, errno.EDQUOT,
                                                  errno.EROFS)


def _trace(e):
    """Print the traceback of a failed file, except for an ffmpeg failure:
    its one-line reason (audio.UnreadableAudio, audio.WriteFailed) says it
    all."""
    if not isinstance(e, (audio.UnreadableAudio, audio.WriteFailed)):
        traceback.print_exc()


def _requeue_if_unintended(claimed_job) -> bool:
    """Put a claimed job back on the queue when the run is interrupted, so a
    crash or system sleep never loses it — UNLESS the interruption was an
    intentional Stop (control.mark_stopping records that). Re-queuing a stopped
    run would make the panel's idle self-heal respawn the very thing the user
    just stopped, which reads as the run flickering back to 'Starting…'."""
    if claimed_job is None or control.stopping_recently():
        return False
    jobs.add(claimed_job)
    return True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(config.ICLOUD_DIR))
    ap.add_argument("--dest", default=str(config.MEETINGS_DIR),
                    help="the meetings folder to write into (default: the library). "
                         "A run into any other folder writes no descriptions or "
                         "automatic titles")
    ap.add_argument("--no-diarize", action="store_true")
    ap.add_argument("--keep-original", action="store_true")
    ap.add_argument("--only", help="only files whose name contains this substring")
    ap.add_argument("--files", help="comma-separated EXACT file names to process "
                                    "(the GUI picker uses this)")
    ap.add_argument("--paths", help="comma-separated ABSOLUTE paths to process from "
                                    "anywhere on disk (originals are never deleted)")
    ap.add_argument("--parallel", type=int, default=1, choices=[1, 2],
                    help="process up to 2 files at once")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="second-opinion pass: another engine transcribes too and "
                         "engine disagreements are flagged for review")
    ap.add_argument("--one-time-speakers", action="store_true",
                    help="focus groups etc.: never register this run's unnamed "
                         "voices in the global speaker list (transcript-local "
                         "Speaker N labels only; enroll-from-meeting still works)")
    ap.add_argument("--speakers", help="comma-separated attendee names to allow")
    ap.add_argument("--ignore-battery", action="store_true")
    ap.add_argument("--ignore-pause", action="store_true",
                    help="run even while automatic processing is paused (manual runs)")
    ap.add_argument("--job", type=float, default=None,
                    help="queued-job id (panel): claimed from the queue once the "
                         "lock is held, so a lost lock race never loses the job")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main():
    args = build_parser().parse_args()

    if control.is_paused() and not args.ignore_pause and not args.dry_run:
        print("Automatic processing is paused (resume from the menu bar); exiting.")
        return 0

    # a dry run writes nothing, so it previews even while a batch runs: it
    # takes no lock, and it never touches the running batch's batch.lock
    lock = None if args.dry_run else acquire_lock()
    if lock is None and not args.dry_run:
        print("Another batch is already running; exiting (this is normal)."
              + (" The queued job stays queued." if args.job else ""))
        return 0

    # A queued job (Redo, hand-picked files) is claimed — removed from the
    # queue — once every abort guard below passes. If a SIGTERM (Stop
    # processing) lands after that but before the run genuinely finishes, the
    # job would otherwise vanish: neither queued nor done. _terminate()
    # re-queues an equivalent job in that case; claimed_job is cleared once
    # run_todo() actually completes so a stray late signal can't re-queue
    # already-finished work.
    claimed_job = None

    # graceful stop: abandon in-flight work safely and record a clean status.
    # CRITICAL: take every process we started down with us — --parallel
    # workers are separate processes that would otherwise survive as multi-GB
    # orphans — but never the shell that started us (_stop_own_processes).
    def _terminate(signum, frame):
        print("Stop requested — aborting current file(s); originals preserved.",
              flush=True)
        if _requeue_if_unintended(claimed_job):
            print("  queued job re-added — it will run on the next kick.", flush=True)
        status.end_run()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            _stop_own_processes()
        except (ProcessLookupError, PermissionError):
            pass
        os._exit(130)
    signal.signal(signal.SIGTERM, _terminate)

    if not args.dry_run and not args.ignore_battery and not battery_ok():
        print(f"On battery below {config.BATTERY_FLOOR}% — skipping this run."
              + (" The queued job stays queued." if args.job else ""))
        return 0

    source, dest = Path(args.source), Path(args.dest)
    if not source.exists():
        print(f"Source folder not found: {source}", file=sys.stderr)
        return 2
    if not preflight_source(source):
        return 3
    dest.mkdir(parents=True, exist_ok=True)
    # descriptions and automatic titles resolve a meeting through the
    # library (config.meetings_dir()), never --dest. A run that writes
    # somewhere else must not read or rewrite the library's same-named
    # meeting, so it writes none.
    describe = config.is_live_store(dest)
    if not describe and not args.dry_run:
        print("note: --dest is not the meetings library, so this run writes no "
              "descriptions or automatic titles", flush=True)
    if not args.dry_run:
        clean_scratch()
        # size caps: logs/ at 10 MB keeping 3 gzips, calibration.jsonl at
        # 10 MB keeping 1 (stt/housekeeping.py). Never raises.
        from stt import housekeeping
        hk = housekeeping.rotate_all()
        if hk["logs"] or hk["calibration"]:
            print(f"rotated {len(hk['logs'])} log(s)"
                  + (", capped calibration.jsonl" if hk["calibration"] else ""))
        # one-time flat->folder layout migration (idempotent, cheap when done)
        n = config.migrate_flat_meetings(dest)
        if n:
            print(f"migrated {n} file(s) into per-meeting folders")
        # stable meeting ids: stamp anything that predates them, rebuild the id
        # index, sweep orphan locks. Idempotent and cheap once every meeting
        # carries one. The archive is stamped too so a restore keeps its id.
        try:
            k = config.backfill_meeting_ids(dest)
            k += config.backfill_meeting_ids(config.archive_dir(dest))
            if k:
                print(f"stamped {k} meeting(s) with a stable id")
        except Exception as e:
            print(f"meeting-id backfill skipped ({e})", file=sys.stderr)
        warn_if_registry_lost(dest)

    if not args.dry_run and not args.no_diarize and not config.resolve_hf_token():
        print("Diarization needs a HuggingFace token (see README).", file=sys.stderr)
        return 2

    sync_voice_memos(dry_run=args.dry_run)

    wanted = None
    if args.files:
        wanted = {f.strip() for f in jobs.split_list(args.files) if f.strip()}

    m = manifest.load()
    todo = []
    skipped = 0
    explicit_paths = set()
    if args.paths:
        for raw in jobs.split_list(args.paths):
            p = Path(raw.strip()).expanduser()
            if p.is_file():
                explicit_paths.add(p.resolve())
                if args.force or not manifest.is_processed(m, p.name, p.stat().st_mtime):
                    todo.append(p)
                else:
                    skipped += 1
            else:
                print(f"   skip (not a file): {p}", file=sys.stderr)
    from stt import holds
    held = holds.items()
    found = set()          # the --files names a source file carries
    for src in iter_sources(source):
        if args.paths and wanted is None:
            break  # explicit-paths run: don't also sweep the source folder
        if wanted is not None and src.name not in wanted:
            continue
        found.add(src.name)
        if args.only and args.only.lower() not in src.name.lower():
            continue
        # a held file is parked ON PURPOSE: every automatic sweep leaves it alone.
        # Naming it explicitly (--files / "Run selected") still processes it —
        # that is the user asking for it by name, which outranks the park.
        if src.name in held and wanted is None:
            print(f"   on hold, skipping: {src.name}", flush=True)
            skipped += 1
            continue
        try:
            mtime = src.stat().st_mtime
        except FileNotFoundError:
            continue
        if not args.force and manifest.is_processed(m, src.name, mtime):
            skipped += 1
            continue
        # a file that keeps failing is set aside until it changes
        # (manifest.record_failure). Naming it (Retry, --files) or --force
        # still runs it.
        if wanted is None and not args.force and manifest.is_parked(m, src.name, src):
            print(f"   set aside after failing, skipping: {src.name} (it runs again "
                  "once the file changes, or on Retry)", flush=True)
            skipped += 1
            continue
        todo.append(src)

    # a --files name no source file carries says so, as a bad --paths path
    # does: a Redo of a file that left the source folder printed nothing and
    # looked like a success. It counts as skipped.
    found |= {p.name for p in explicit_paths}
    missing = sorted(wanted - found) if wanted is not None else []
    empty = {p.name for folder in config.source_dirs(source) if Path(folder).is_dir()
             for p in Path(folder).iterdir() if p.name in missing and _empty_audio(p)}
    for name in missing:
        # an empty file is there, so "not found" sent the owner looking for it
        why = "is empty (0 bytes), skipped" if name in empty else None
        print(f"   {name} {why}" if why else f"   not found in source: {name}",
              file=sys.stderr, flush=True)

    # shortest first: a short recording never waits behind multi-hour board
    # meetings that happen to sort first by name (stt.queue_order)
    todo = queue_order.shortest_first(todo)
    if args.dry_run:
        for src in todo:
            print(f"[would process] {src.name}")
        # an empty file is skipped without a word on every run (it may be a
        # copy still arriving); the preview is where the owner asks why
        for folder in config.source_dirs(source):
            for p in (sorted(Path(folder).iterdir()) if Path(folder).is_dir() else []):
                if _empty_audio(p):
                    print(f"[skipped, 0 bytes] {p.name}")
        print(f"\n{len(todo)} to process, {skipped} already done"
              + (f", {len(missing)} not found." if missing else "."))
        return 0
    skipped += len(missing)

    if args.job is not None:
        # claim only now, with every abort guard (lock, battery, folders, token)
        # passed — an aborted run leaves the job queued for the panel to re-kick
        jobs.remove(args.job)
        # kept only long enough to re-queue on a SIGTERM (see _terminate above)
        claimed_job = job_spec_from_args(args, todo)

    # a plain run re-reads the watched folders before every file it picks, so
    # a recording that lands mid-run joins the queue by its length. A run over
    # its own list (--files/--paths/--only/--force) runs exactly that list.
    sweep = not (args.paths or args.files or args.only or args.force)
    control.clear_finish()  # a stale "stop after this file" never cuts this run short
    from stt import summarize as _summarize
    status.start_run([s.name for s in todo], sweep=sweep,
                     summarizes=bool(describe and _summarize.available()))
    base_opts = {"do_diarize": not args.no_diarize,
                 "strict": args.strict or config.STRICT,
                 "verify": args.verify or config.VERIFY,
                 "track_unknowns": not args.one_time_speakers,
                 "allowed": [s.strip() for s in args.speakers.split(",")] if args.speakers else None,
                 "do_move": config.MOVE_AFTER_SUCCESS and not args.keep_original}

    def opts_for(src: Path) -> dict:
        # hand-picked files from arbitrary locations are NEVER deleted
        if src.resolve() in explicit_paths:
            return {**base_opts, "do_move": False}
        return base_opts

    processed = failed = 0
    started = {}    # name -> the source path, for every file this run started
    succeeded = []  # keys of files that fully processed (for auto-summary)
    failed_names = set()  # files that failed THIS run — never retried in rescan
    _overridden_holds = set()   # parks the caller overrode by naming the file

    def hold_skips(src: Path) -> bool:
        """True when `src` must be skipped because it is parked RIGHT NOW.

        The todo list read the parks once, before the run started. A park
        placed on a later file in the same run therefore did not stop it -- and
        the run then released a hold it had never honored. The park file is
        re-read here, immediately before each file starts, so a hold placed
        mid-run still stops the file it names."""
        name = src.name
        if name not in holds.items():
            return False
        try:
            explicit = wanted is not None or src.resolve() in explicit_paths
        except OSError:
            explicit = wanted is not None
        if not explicit:
            return True            # an automatic sweep: the park wins
        _overridden_holds.add(name)  # asked for by name: the park is overridden
        return False

    def announce_hold(name):
        nonlocal skipped
        skipped += 1
        print(f"   on hold, skipping: {name}", flush=True)

    def _record(res, n_active):
        nonlocal processed, failed
        if res["ok"]:
            # a LOCKED read-modify-write against the file on disk, not a save of
            # the snapshot `m` taken hours ago at the start of this run: saving
            # the snapshot drops every record the GUI or a second run wrote in
            # between. The returned record keeps our own snapshot current.
            m["processed"][res["key"]] = manifest.record(
                res["key"], res["mtime"], res["outputs"],
                fp=res.get("fp"), size=res.get("size"))
            if describe:
                note_voice_memo_ingest(res, dest)
            if res["key"] in _overridden_holds:
                # the park was OVERRIDDEN by name, and the file it parked is
                # now processed, so clearing it cannot pin a stale name. A hold
                # placed DURING the run was never honored and must survive.
                holds.release(res["key"])
            rates.record(res.get("duration_sec"), res.get("stage_secs"),
                         rates.current_asr_key(), n_active=n_active,
                         diarizer=res.get("diarizer"))
            print(f"   done: {res['key']} — {res['summary']}.{res['who']}", flush=True)
            status.finish_file(res["key"], True, res["summary"] + (res["who"] or ""))
            processed += 1
            # the description lands NOW, not hours later when the whole
            # backlog ends (and never lost to a Stop pressed before then).
            # One file at a time, it runs here BEFORE the next file starts
            # (with --parallel, while the other workers transcribe), so the
            # stage counts down from recent summary times
            # (status.summary_estimate). The automatic title may rename the
            # folder, so the end-of-run sweep gets the folder's final name.
            track = {"base": res.get("base")}
            try:
                if describe:
                    summarize_one(res["key"], res.get("base"), track=track)
            finally:
                succeeded.append((res["key"], track.get("base")))
        else:
            print(f"   FAILED: {res['key']}", file=sys.stderr, flush=True)
            status.finish_file(res["key"], False, "failed")
            failed_names.add(res["key"])
            failed += 1
            note_failure(res["key"], "failed")

    queue = list(todo)       # files still to start, re-sorted before every pick
    in_flight = set()        # names started and not yet finished (--parallel)
    finish = {"asked": False}

    vm_synced = {"at": time.monotonic()}

    def arrivals():
        """Files in the watched folders that this run has not queued, started,
        finished, or failed: recordings that landed WHILE it ran. Their
        WatchPaths trigger hit our lock and exited, so the run takes them in
        itself (plain runs only). A Voice Memos memo that finished during the
        run is copied in first (at most once per VM_RESYNC_SECONDS)."""
        if not sweep:
            return []
        if time.monotonic() - vm_synced["at"] >= VM_RESYNC_SECONDS:
            vm_synced["at"] = time.monotonic()
            sync_voice_memos()
        have = {s.name for s in queue} | in_flight
        held = holds.items()    # re-read: a hold placed mid-run counts
        # re-read the manifest from disk, never the snapshot `m`: a meeting
        # this run finished and then renamed (the automatic title) moved its
        # folder, and manifest.retarget fixed only the file on disk. The stale
        # snapshot would call the source unprocessed, and with the original
        # kept (STT_MOVE_AFTER_SUCCESS=0, a refused delete) the run would
        # transcribe it again into a " (2)" folder, then again, in a loop.
        try:
            cur = manifest.load()
        except Exception:
            cur = m
        out = []
        for src in iter_sources(source):
            # never a file that already failed this run: nothing marks it
            # processed, so a permanently failing file (corrupt audio, a stuck
            # iCloud placeholder) would otherwise be retried forever
            if src.name in have or src.name in failed_names or src.name in held:
                continue
            try:
                if manifest.is_processed(cur, src.name, src.stat().st_mtime):
                    continue
                if manifest.is_parked(cur, src.name, src):
                    continue    # set aside after failing: an automatic sweep skips it
            except OSError:
                continue
            out.append(src)
        return out

    def pick():
        """The next file to start: the SHORTEST one left, chosen now, not when
        the run began, so a short recording that arrives mid-run runs before
        queued multi-hour meetings. None when nothing is left, or when a
        "stop after this file" flag asks the run to start nothing new."""
        while True:
            if control.finish_requested():
                if not finish["asked"]:
                    print("Stop after the current file requested — starting no "
                          "new files.", flush=True)
                finish["asked"] = True
                return None
            queue[:] = queue_order.shortest_first(queue + arrivals())
            if not queue:
                return None
            src = queue.pop(0)
            # status.json's pending is the rest, in the order they will run
            status.set_pending([s.name for s in queue])
            # the parks are re-read here, at the last moment before the file
            # starts -- never from the snapshot the todo list was built with
            if hold_skips(src):
                announce_hold(src.name)
                continue
            started[src.name] = src
            return src

    def run_one_at_a_time():
        while True:
            src = pick()
            if src is None:
                return
            print(f"[processing] {src.name}", flush=True)
            try:
                _record(process_one(str(src), str(dest), opts_for(src)), 1)
            except Exception as e:
                _trace(e)
                nonlocal_fail(src.name, e)

    def run_in_parallel():
        """Up to args.parallel files at once. Each free worker takes the
        shortest file left at that moment (pick), not a list fixed up front."""
        from concurrent.futures import ProcessPoolExecutor, as_completed
        from concurrent.futures.process import BrokenProcessPool
        while True:
            broken = []
            pool_ok = True
            with ProcessPoolExecutor(max_workers=args.parallel) as ex:
                # future -> [src, the most files in flight while it ran]: the
                # concurrency each rate sample is tagged with. A file that ran
                # alone (the last of the queue) records n_active=1.
                futs = {}
                while True:
                    while pool_ok and len(futs) < args.parallel:
                        src = pick()
                        if src is None:
                            break
                        print(f"[processing] {src.name}", flush=True)
                        try:
                            fut = ex.submit(process_one, str(src), str(dest), opts_for(src))
                        except BrokenProcessPool:
                            broken.append(src)
                            pool_ok = False
                            break
                        futs[fut] = [src, 0]
                        in_flight.add(src.name)
                        for v in futs.values():
                            v[1] = max(v[1], len(futs))
                    if not futs:
                        break
                    fut = next(iter(as_completed(list(futs))))
                    src, peak = futs.pop(fut)
                    in_flight.discard(src.name)
                    try:
                        _record(fut.result(), peak)
                    except BrokenProcessPool:
                        # a DIFFERENT file's worker crashed (segfault/OOM-kill)
                        # and took the shared pool down with it — this file was
                        # never actually attempted, so don't blame it for
                        # someone else's crash; retry it alone once the broken
                        # pool is fully torn down below, and start nothing
                        # more in this pool
                        broken.append(src)
                        pool_ok = False
                    except Exception as e:
                        nonlocal_fail(src.name, e)
            if broken:
                print(f"  process pool crashed mid-run — retrying {len(broken)} "
                      f"file(s) one at a time", file=sys.stderr, flush=True)
                for src in broken:
                    print(f"[processing] {src.name}", flush=True)
                    # a FRESH single-worker pool per file, NOT an in-process
                    # call: this workload just crashed a worker once. If the
                    # crash is deterministic (a corrupt file hitting a native
                    # bug in the ML libs, not a transient OOM-kill), an
                    # in-process retry would segfault run_batch itself —
                    # skipping the remaining retries and bypassing _terminate
                    # (SIGSEGV, not SIGTERM), which loses a claimed job.
                    try:
                        with ProcessPoolExecutor(max_workers=1) as rex:
                            _record(rex.submit(process_one, str(src), str(dest),
                                               opts_for(src)).result(), 1)
                    except BrokenProcessPool:
                        nonlocal_fail(src.name, RuntimeError(
                            "crashed its worker process twice — file skipped"))
                    except Exception as e:
                        _trace(e)
                        nonlocal_fail(src.name, e)
            if pool_ok:
                return
            # the pool broke: the files not yet started run in a fresh one

    def nonlocal_fail(name, e):
        nonlocal failed
        failed += 1
        failed_names.add(name)
        print(f"   FAILED: {name}: {e}  (original preserved)", file=sys.stderr, flush=True)
        status.finish_file(name, False, str(e))
        if not _machine_at_fault(e):
            note_failure(name, str(e), unreadable=isinstance(e, audio.UnreadableAudio))

    def note_failure(name, why, unreadable=False):
        """Count the failure against the unchanged file, and say so when it
        sets the file aside. Never fails the run."""
        src = started.get(name)
        if src is None:
            return
        try:
            rec = manifest.record_failure(name, src, why, unreadable=unreadable)
        except Exception:
            return
        if rec.get("parked"):
            print(f"   set aside: automatic runs skip {name} until the file changes "
                  "(Retry runs it now)", file=sys.stderr, flush=True)

    if args.parallel > 1:
        run_in_parallel()
    else:
        run_one_at_a_time()
    claimed_job = None  # the work is done — a stray late SIGTERM must not re-queue it

    if not args.dry_run and succeeded and describe:
        n_sum = auto_summarize(succeeded)
        if n_sum:
            print(f"  auto-summarized {n_sum} meeting(s)", flush=True)

    # apply any naming done WHILE this batch ran: the GUI's relabel couldn't take
    # the lock, so it queued a flag — honor it now (we still hold the lock)
    pending = config.PROJECT_DIR / "relabel_pending.flag"
    if pending.exists():
        print("Applying speaker names given during the run (queued relabel)…", flush=True)
        # consume BEFORE the pass (same reason as relabel.main): a naming that
        # lands while relabel_all runs re-queues the flag, and it must survive
        # this sweep for the panel's kick to honor it — unlinking afterwards
        # would silently cancel the follow-up.
        pending.unlink(missing_ok=True)
        try:
            import relabel as _relabel
            _relabel.relabel_all()
        except Exception as e:
            print(f"   queued relabel failed: {e}", file=sys.stderr)

    # idle now: the queue is done. Refresh descriptions a Redo left out of
    # date, unless a stop was asked or a panel job waits (it chains below)
    if not args.dry_run and describe and not finish["asked"] and not jobs.items():
        n_re = regenerate_stale()
        if n_re:
            print(f"  refreshed {n_re} out-of-date description(s)", flush=True)

    # the descriptions are written: move the memos this library now holds
    # to Voice Memos' Recently Deleted (off unless STT_VM_DELETE_AFTER_INGEST=1)
    if not args.dry_run:
        delete_voice_memos(describe)

    status.end_run()
    if not args.dry_run:
        record_review_metrics()
        # read-only store and registry check; counts (no titles) go to
        # doctor.json for the panel. Only after a run that did work, or once
        # a day (doctor_due). Never fails the run.
        if doctor_due(processed, failed):
            from stt import doctor
            dr = doctor.run_check_at_batch_end(dest)
            if dr.get("error"):
                print(f"  (doctor check skipped: {dr['error']})",
                      file=sys.stderr, flush=True)
    print(f"\nSummary: {processed} processed, {skipped} skipped, {failed} failed.")
    if finish["asked"]:
        control.clear_finish()  # honored: the next run starts normally
        print("Stopped after the current file, as asked.", flush=True)
        return 1 if failed else 0   # stopping means no chained job either

    # chain into the next panel-queued job (a Redo clicked during this run).
    # Release our lock FIRST so the child can take it; if something else wins
    # the race instead, the job stays queued and the panel re-kicks it.
    nxt = jobs.items()
    if nxt:
        lock.close()
        print(f"Starting queued job: {nxt[0].get('label') or 'run'}", flush=True)
        spawn_chained_job(nxt[0])
    return 1 if failed else 0


if __name__ == "__main__":
    config.private_umask()
    sys.exit(main())
