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
"""
import argparse
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import traceback
from pathlib import Path


def _load_env_file():
    """Load KEY=VALUE lines from stt.env BEFORE importing the package (config reads
    env at import). launchd does not read ~/.zshrc."""
    p = Path(__file__).resolve().parent / "stt.env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

from stt import config, control, icloud, jobs, manifest, rates, status  # noqa: E402


def acquire_lock():
    fd = open(config.PROJECT_DIR / "batch.lock", "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        return None
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
    for p in (list(config.MEETINGS_DIR.glob("*.tmp")) + list(config.MEETINGS_DIR.glob("*/*.tmp"))
              + list(config.MEETINGS_DIR.glob("*.part")) + list(config.MEETINGS_DIR.glob("*/*.part"))):
        p.unlink(missing_ok=True)


def job_spec_from_args(args, todo) -> dict:
    """The queued-job dict equivalent to this invocation's CLI args. Used to
    re-queue a --job run if it's interrupted before finishing (see
    _terminate() in main()) — manifest idempotency makes replaying it, files
    already done included, a safe no-op for anything that already succeeded."""
    return {"files": jobs.split_list(args.files) if args.files else [],
            "paths": jobs.split_list(args.paths) if args.paths else [],
            "force": args.force, "strict": args.strict,
            "verify": args.verify, "parallel": args.parallel,
            "onetime": getattr(args, "one_time_speakers", False),
            "label": (", ".join(s.name for s in todo[:2])
                     + (f" +{len(todo) - 2} more" if len(todo) > 2 else "")) or "resumed run"}


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


def summarize_one(key) -> bool:
    """Draft the AI title/summary for ONE just-finished meeting (local Qwen
    via .venv-llm). Runs right after the file completes, not at end-of-run:
    with a long backlog the end is hours away, and a Stop before it would
    lose the summaries for everything already finished. Quietly a no-op when
    the LLM venv isn't installed or the meeting already has a summary; a
    failure never breaks the run (the end-of-run sweep retries it)."""
    import json as _json

    from stt import status as st, summarize
    if not summarize.available():
        return False
    base = Path(key).stem
    try:
        d = _json.loads(config.meeting_file(base, ".json").read_text())
        if d.get("ai_summary"):
            return False  # e.g. a re-queued file that was already summarized
        st.set_stage(key, "summarizing")
        summarize.suggest_title(base)
        return True
    except Exception as e:
        print(f"   summary failed for {base}: {e}", file=sys.stderr, flush=True)
        return False
    finally:
        st.clear_stage(key)


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
    return sum(1 for key in keys if summarize_one(key))


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


def iter_audio(folder: Path):
    if not Path(folder).is_dir():
        return
    for p in sorted(Path(folder).iterdir()):
        if p.is_file() and p.suffix.lower() in config.AUDIO_EXTS and not p.name.startswith("."):
            yield p


def iter_sources(source: Path):
    """Sweep the primary source folder AND the local recordings staging folder
    (the meeting recorder's output). Dot-prefixed in-progress captures are
    already skipped by iter_audio; distinct filenames keep the manifest dedupe
    unambiguous across folders."""
    seen = set()
    folders = [Path(source)]
    rec = config.recordings_dir()
    if rec not in folders:
        folders.append(rec)
    for folder in folders:
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


def process_one(src_str: str, dest_str: str, opts: dict) -> dict:
    """Process a single file end-to-end (runs in a worker process under --parallel).
    Returns {ok, key, mtime, outputs, summary, duration_sec, stage_secs}.
    Never touches the manifest."""
    import time as _time

    from stt import audio, icloud as ic, pipeline, status as st
    src, dest = Path(src_str), Path(dest_str)
    key = src.name
    mtime = src.stat().st_mtime

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
        st.set_stage(k, stage, progress=progress, duration=duration,
                    diarize=opts["do_diarize"], verify=opts.get("verify", False))

    report("downloading")
    if not ic.materialize(src):
        raise RuntimeError("iCloud file did not fully materialize")

    # the meeting recorder drops a <base>.opts.json next to a stereo capture,
    # declaring its me/them channel layout; its contents are copied into the
    # meeting .json by process_file (so a later Redo needs no sidecar)
    import json as _json
    input_opts = None
    side = src.with_suffix(".opts.json")
    if side.exists():
        try:
            input_opts = _json.loads(side.read_text())
        except (OSError, ValueError):
            input_opts = None

    res = pipeline.process_file(
        src, dest_dir=dest, do_diarize=opts["do_diarize"], strict=opts["strict"],
        do_verify=opts.get("verify", False),
        allowed_names=opts["allowed"], report=report,
        track_unknowns=opts.get("track_unknowns", True), input_opts=input_opts)
    if cur["stage"]:  # close out the final stage (writing ends when we return)
        stage_secs[cur["stage"]] = stage_secs.get(cur["stage"], 0.0) + (_time.monotonic() - cur["t"])
    outputs = [res["txt"], res["json"]] + ([res["emb"]] if res["emb"] else [])

    if src.suffix.lower() in config.VIDEO_EXTS:
        dest_audio = config.meeting_file(src.stem, ".m4a", dest)
        if not dest_audio.exists():
            audio.extract_audio(src, dest_audio)
    else:
        dest_audio = config.meeting_file(src.stem, src.suffix, dest)
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
        src.unlink()
        side.unlink(missing_ok=True)  # the layout now lives in the meeting .json

    who = ("  identified: " + ", ".join(res["identified"])) if res["identified"] else ""
    summary = f"{res['n_speakers']} speaker(s), {round(res['duration_sec'] / 60, 1)} min"
    return {"ok": success, "key": key, "mtime": mtime,
            "outputs": [str(o) for o in outputs + [dest_audio]],
            "summary": summary, "who": who,
            "duration_sec": res["duration_sec"], "stage_secs": stage_secs}


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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=str(config.ICLOUD_DIR))
    ap.add_argument("--dest", default=str(config.MEETINGS_DIR))
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
    args = ap.parse_args()

    if control.is_paused() and not args.ignore_pause and not args.dry_run:
        print("Automatic processing is paused (resume from the menu bar); exiting.")
        return 0

    lock = acquire_lock()
    if lock is None:
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
    # CRITICAL: take the whole process group down with us — --parallel workers
    # are separate processes that would otherwise survive as multi-GB orphans.
    def _terminate(signum, frame):
        print("Stop requested — aborting current file(s); originals preserved.",
              flush=True)
        if _requeue_if_unintended(claimed_job):
            print("  queued job re-added — it will run on the next kick.", flush=True)
        status.end_run()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            os.killpg(os.getpgid(0), signal.SIGTERM)  # includes ourselves
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
    if not args.dry_run:
        clean_scratch()
        # one-time flat->folder layout migration (idempotent, cheap when done)
        n = config.migrate_flat_meetings(dest)
        if n:
            print(f"migrated {n} file(s) into per-meeting folders")
        warn_if_registry_lost(dest)

    if not args.dry_run and not args.no_diarize and not config.resolve_hf_token():
        print("Diarization needs a HuggingFace token (see README).", file=sys.stderr)
        return 2

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
    for src in iter_sources(source):
        if args.paths and wanted is None:
            break  # explicit-paths run: don't also sweep the source folder
        if wanted is not None and src.name not in wanted:
            continue
        if args.only and args.only.lower() not in src.name.lower():
            continue
        try:
            mtime = src.stat().st_mtime
        except FileNotFoundError:
            continue
        if not args.force and manifest.is_processed(m, src.name, mtime):
            skipped += 1
            continue
        todo.append(src)

    if args.dry_run:
        for src in todo:
            print(f"[would process] {src.name}")
        print(f"\n{len(todo)} to process, {skipped} already done.")
        return 0

    if args.job is not None:
        # claim only now, with every abort guard (lock, battery, folders, token)
        # passed — an aborted run leaves the job queued for the panel to re-kick
        jobs.remove(args.job)
        # kept only long enough to re-queue on a SIGTERM (see _terminate above)
        claimed_job = job_spec_from_args(args, todo)

    status.start_run([s.name for s in todo])
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
    succeeded = []  # keys of files that fully processed (for auto-summary)
    failed_names = set()  # files that failed THIS run — never retried in rescan

    def _record(res, n_active):
        nonlocal processed, failed
        if res["ok"]:
            manifest.mark(m, res["key"], res["mtime"], res["outputs"])
            manifest.save(m)
            rates.record(res.get("duration_sec"), res.get("stage_secs"),
                         rates.current_asr_key(), n_active=n_active)
            print(f"   done: {res['key']} — {res['summary']}.{res['who']}", flush=True)
            status.finish_file(res["key"], True, res["summary"] + (res["who"] or ""))
            succeeded.append(res["key"])
            processed += 1
            # the description lands NOW, while the next file transcribes —
            # not hours later when the whole backlog ends (and never lost to
            # a Stop pressed before then)
            summarize_one(res["key"])
        else:
            print(f"   FAILED: {res['key']}", file=sys.stderr, flush=True)
            status.finish_file(res["key"], False, "failed")
            failed_names.add(res["key"])
            failed += 1

    def run_todo(batch):
        # the concurrency actually used for THIS batch — a solo/short batch runs
        # single-worker even under --parallel 2, so rate samples must be tagged
        # with the real n_active, not the outer run's initial worker count.
        n_used = args.parallel if (args.parallel > 1 and len(batch) > 1) else 1
        if args.parallel > 1 and len(batch) > 1:
            from concurrent.futures import ProcessPoolExecutor, as_completed
            from concurrent.futures.process import BrokenProcessPool
            broken = []
            with ProcessPoolExecutor(max_workers=args.parallel) as ex:
                futs = {ex.submit(process_one, str(s), str(dest), opts_for(s)): s for s in batch}
                for fut in as_completed(futs):
                    src = futs[fut]
                    print(f"[processing] {src.name}", flush=True)
                    try:
                        _record(fut.result(), n_used)
                    except BrokenProcessPool:
                        # a DIFFERENT file's worker crashed (segfault/OOM-kill)
                        # and took the shared pool down with it — this file was
                        # never actually attempted, so don't blame it for
                        # someone else's crash; retry it alone once the broken
                        # pool is fully torn down below
                        broken.append(src)
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
                        traceback.print_exc()
                        nonlocal_fail(src.name, e)
        else:
            for src in batch:
                print(f"[processing] {src.name}", flush=True)
                try:
                    _record(process_one(str(src), str(dest), opts_for(src)), n_used)
                except Exception as e:
                    traceback.print_exc()
                    nonlocal_fail(src.name, e)

    def nonlocal_fail(name, e):
        nonlocal failed
        failed += 1
        failed_names.add(name)
        print(f"   FAILED: {name}: {e}  (original preserved)", file=sys.stderr, flush=True)
        status.finish_file(name, False, str(e))

    run_todo(todo)
    claimed_job = None  # the work is done — a stray late SIGTERM must not re-queue it

    # recordings that landed WHILE we processed: their WatchPaths trigger hit our
    # lock and exited, so sweep again until the folder is quiet (plain runs only)
    if not (args.paths or args.files or args.only or args.force):
        while True:
            # exclude files that already failed this run: nothing marks them
            # processed, so without this a permanently-failing file (corrupt
            # audio, stuck iCloud placeholder) would loop the rescan forever.
            more = [src for src in iter_sources(source)
                    if src.name not in failed_names
                    and not manifest.is_processed(m, src.name, src.stat().st_mtime)]
            if not more:
                break
            print(f"[rescan] {len(more)} new recording(s) arrived during the run", flush=True)
            status.start_run([s.name for s in more])
            run_todo(more)

    if not args.dry_run and succeeded:
        n_sum = auto_summarize(succeeded)
        if n_sum:
            print(f"  auto-summarized {n_sum} meeting(s)", flush=True)

    # apply any naming done WHILE this batch ran: the GUI's relabel couldn't take
    # the lock, so it queued a flag — honor it now (we still hold the lock)
    pending = config.PROJECT_DIR / "relabel_pending.flag"
    if pending.exists():
        print("Applying speaker names given during the run (queued relabel)…", flush=True)
        try:
            import relabel as _relabel
            _relabel.relabel_all()
            pending.unlink(missing_ok=True)
        except Exception as e:
            print(f"   queued relabel failed: {e}", file=sys.stderr)

    status.end_run()
    print(f"\nSummary: {processed} processed, {skipped} skipped, {failed} failed.")

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
    sys.exit(main())
