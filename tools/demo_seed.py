#!/usr/bin/env python3
"""Build a self-contained, SYNTHETIC data home for the STT control panel.

Every later UI phase is developed, tested, and screenshotted against the home
this builds — so no build/test agent ever has to touch a real meeting. The
home exercises every panel state: month-grouped meetings across two years,
mixed categories, a review queue with second-engine alternatives and minor
flags, an un-named meeting in the "Needs naming" inbox, an archived meeting,
enrolled + unknown + hidden speakers, processing history with a real failure,
and a live queue with a held file. Every ▶ control plays REAL audio: small sine
WAVs (stdlib `wave`), one pitch per speaker, with segment timestamps that fit
inside each clip.

Point the panel at the result with config.py's STT_HOME override:

    python3 tools/demo_seed.py --dir qa/demo_home
    # then, from the repo:
    STT_HOME=$PWD/qa/demo_home STT_PANEL_PORT=8747 .venv/bin/python -m gui.server

All person names are synthetic (Mark Paik + five invented colleagues). The tool
REFUSES to run without --dir, and REFUSES any --dir that overlaps a real,
configured data folder — it can only ever write inside its own target.
"""
import argparse
import json
import math
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from wave import open as wave_open

# Run straight from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # a core repo dependency (identify.py); the real loaders need it

from stt import config, dates, output  # real writers/helpers — never invent a schema

# --- synthetic cast (the ONLY person names that may appear) ---
OWNER = "Mark Paik"
COLLEAGUES = ["Alex Rivera", "Jordan Lee", "Priya Shah", "Sam Chen", "Dana Fox"]
ENROLLED = [OWNER] + COLLEAGUES

SAMPLE_RATE = 8000          # small, still plays everywhere; ffmpeg re-cuts to 22050
EMB_DIM = 192               # pyannote-style embedding width; contents are random
MARKER = ".demo_home"       # written at the home root; --wipe only removes a marked dir
PALETTE_HZ = [165.0, 196.0, 220.0, 262.0, 294.0, 330.0, 392.0, 440.0, 494.0, 587.0]


# ---------------------------------------------------------------- audio ----

def write_wav(path: Path, spans, total_dur: float):
    """One mono 16-bit WAV. `spans` = [(freq_hz, start, end)]; each span is a
    steady sine of that pitch, silence in the gaps. So a meeting's WAV alternates
    pitch as the speaker changes and every ▶ actually plays something distinct."""
    n = int(math.ceil(total_dur * SAMPLE_RATE))
    t = np.arange(n) / SAMPLE_RATE
    sig = np.zeros(n, dtype=np.float64)
    for freq, s, e in spans:
        m = (t >= s) & (t < e)
        sig[m] = 0.32 * np.sin(2 * math.pi * freq * t[m])
    pcm = np.clip(sig * 32767.0, -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave_open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


# ------------------------------------------------------------- meetings ----

def _words_for(text: str, start: float, end: float, speaker_id: str):
    """One word entry per token, evenly spread across [start, end]. Uses the real
    key names ('word' for words, matched by merge.py / review.py)."""
    toks = text.split()
    if not toks:
        return []
    step = (end - start) / len(toks)
    out = []
    for i, tok in enumerate(toks):
        ws = round(start + i * step, 2)
        we = round(min(end, ws + step * 0.9), 2)
        out.append({"start": ws, "end": we, "word": tok, "speaker": speaker_id})
    return out


def build_meeting(home: Path, spec: dict, sources_seen: dict) -> dict:
    """Write one meeting folder (<base>/<base>.json/.txt/.wav [+ .reviews.json])
    through the REAL output writers. Returns bookkeeping for registries/history."""
    title, iso = spec["title"], spec["date"]
    base = spec.get("base") or dates.stamp(title, iso)
    reviewed = spec.get("reviewed", True)
    strict = spec.get("strict", False)
    parent = config.archive_dir(home / "meetings") if spec.get("archived") else (home / "meetings")
    mdir = parent / base
    mdir.mkdir(parents=True, exist_ok=True)

    speakers = [{"id": sid, "name": name, "global_id": gid,
                 "display": disp, "match_score": score}
                for (sid, name, disp, gid, score) in spec["speakers"]]
    freq_of = {sp["id"]: PALETTE_HZ[i % len(PALETTE_HZ)]
               for i, sp in enumerate(speakers)}
    disp_of = {sp["id"]: sp for sp in speakers}

    segments, words, wav_spans = [], [], []
    cursor = 0.5
    for turn in spec["turns"]:
        sid, text = turn[0], turn[1]
        flags = list(turn[2]) if len(turn) > 2 and turn[2] else []
        theirs = turn[3] if len(turn) > 3 else None
        wc = len(text.split())
        minor = bool(flags) and wc <= 3
        dur = 0.6 if minor else max(1.3, round(wc * 0.34, 2))
        start, end = round(cursor, 2), round(cursor + dur, 2)
        sp = disp_of[sid]
        seg = {"start": start, "end": end, "speaker": sid,
               "name": sp["name"], "display": sp["display"],
               "text": text, "flags": flags, "overlap": "overlap" in flags}
        if theirs is not None:
            if "possible:engine_disagreement" not in flags:
                flags.append("possible:engine_disagreement")
            seg["flags"] = flags
            seg["alt"] = [{"start": round(start + 0.4, 2), "end": round(end - 0.4, 2),
                           "ours": text, "theirs": theirs}]
        segments.append(seg)
        words.extend(_words_for(text, start, end, sid))
        wav_spans.append((freq_of[sid], start, end))
        cursor = end + 0.3
    total_dur = round(cursor + 0.7, 2)

    wav = mdir / f"{base}.wav"
    write_wav(wav, wav_spans, total_dur)
    src_mtime = round(os.path.getmtime(wav), 3)

    meta = {
        "source_file": spec.get("source_file", f"{title}.m4a"),
        "source_mtime": src_mtime,
        "date": iso,
        "processed_at": f"{iso}T20:14:07",
        "duration_sec": total_dur,
        "asr_engine": "parakeet",
        "diarizer": config.DIARIZATION_MODEL,
        "n_speakers": len(speakers),
        "strict": strict,
        "one_time_speakers": False,
        "punctuated": True,
        "verify_engine": spec.get("verify_engine"),
        "overlap_spans": [[s["start"], s["end"]] for s in segments if s["overlap"]],
        "refine_stats": {"reassigned": 0, "smoothed": 0, "flagged": sum(1 for s in segments if s["flags"])},
    }
    if spec.get("category"):
        meta["category"] = spec["category"]
    meta["reviewed"] = bool(reviewed)
    for k in ("ai_title", "ai_summary"):
        if spec.get(k):
            meta[k] = spec[k]
    if spec.get("next_steps"):
        meta["ai_next_steps"] = spec["next_steps"]
    if spec.get("ai_summary") or spec.get("next_steps"):
        meta["ai_generated_at"] = f"{iso}T20:15:30"

    jpath = mdir / f"{base}.json"
    header = output.txt_header(meta["source_file"], total_dur, speakers, strict,
                               meta["processed_at"])
    output.write_json(jpath, meta, speakers, segments, words)
    output.write_txt(mdir / f"{base}.txt", segments, header=header)

    if spec.get("decisions"):
        (mdir / f"{base}.reviews.json").write_text(
            json.dumps(spec["decisions"], indent=2))

    # track which live meetings each named / unknown speaker appears in
    if not spec.get("archived"):
        for sp in speakers:
            if sp["name"] in ENROLLED:
                sources_seen.setdefault(sp["name"], []).append(base)
    return {"base": base, "source_file": meta["source_file"],
            "duration_sec": total_dur, "n_speakers": len(speakers),
            "identified": [sp["name"] for sp in speakers if sp["name"]],
            "archived": bool(spec.get("archived")), "iso": iso,
            "jpath": str(jpath), "txtpath": str(mdir / f"{base}.txt")}


# ---------------------------------------------------------- speaker store --

def write_voiceprints(home: Path, sources_seen: dict, unknown_meetings: dict):
    """registry.json (enrolled) + unknowns.json (unknown/hidden), each with real
    .npy sample stacks the loaders can read. Embedding contents are random unit
    vectors — playback keys off transcripts, not these."""
    vp = home / "voiceprints"
    vp.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)

    registry = {}
    for name in ENROLLED:
        srcs = sources_seen.get(name, [])[:5] or ["(enrolled from a clip)"]
        n = max(1, len(srcs))
        arr = rng.standard_normal((n, EMB_DIM))
        arr /= np.linalg.norm(arr, axis=1, keepdims=True)
        fname = name.replace("/", "_") + ".npy"
        np.save(vp / fname, arr.astype(np.float32))
        registry[name] = {"file": fname, "dim": EMB_DIM,
                          "n_samples": int(n), "sources": srcs}
    (vp / "registry.json").write_text(json.dumps(registry, indent=2))

    speakers = {}
    for uid, info in unknown_meetings.items():
        mts = info["meetings"]
        arr = rng.standard_normal((max(1, len(mts)), EMB_DIM))
        arr /= np.linalg.norm(arr, axis=1, keepdims=True)
        np.save(vp / f"{uid}.npy", arr.astype(np.float32))
        entry = {"file": f"{uid}.npy", "meetings": mts,
                 "created": info.get("created", "2026-01-15T20:14:07")}
        if info.get("archived"):
            entry["archived"] = True
        speakers[uid] = entry
    (vp / "unknowns.json").write_text(json.dumps({"speakers": speakers}, indent=2))
    return registry, speakers


# ------------------------------------------------------------ state files --

def write_state(home: Path, built: list, queue_files: list, held: list,
                history: list):
    manifest = {"processed": {}}
    for b in built:
        if b["archived"]:
            continue
        manifest["processed"][b["source_file"]] = {
            "mtime": round(datetime.fromisoformat(b["iso"]).timestamp(), 3),
            "outputs": [b["jpath"], b["txtpath"]],
            "processed_at": f"{b['iso']}T20:14:07",
        }
    (home / "manifest.json").write_text(json.dumps(manifest, indent=2))

    (home / "results.jsonl").write_text(
        "".join(json.dumps(h) + "\n" for h in history))

    status = {
        "running": False, "pid": None, "pgid": None,
        "active": {}, "pending": [],
        "recent": history[:6],  # newest-first ring the menu bar/panel show
        "updated_at": "2026-07-11T08:32:10",
        "ended_at": "2026-07-11T05:03:44",
    }
    (home / "status.json").write_text(json.dumps(status, indent=2))

    (home / "holds.json").write_text(json.dumps(sorted(held), indent=2))
    (home / "queued_jobs.json").write_text("[]")   # empty: nothing auto-spawns
    (home / "stt.env").write_text(
        "# synthetic demo home — settings the panel reads via config._env_file()\n"
        "STT_PUNCTUATE=1\n")
    (home / "work").mkdir(exist_ok=True)
    (home / "logs").mkdir(exist_ok=True)


# ------------------------------------------------------------- the content -

def meeting_specs():
    """Nine live meetings across five months over 2025-2026, plus one archived
    and one un-named inbox meeting. Speaker ids are per-meeting SPEAKER_0N."""
    def named(*names):
        # -> speaker defs for enrolled people, ids SPEAKER_00..
        out = []
        for i, nm in enumerate(names):
            out.append((f"SPEAKER_{i:02d}", nm, nm, None, round(0.86 + 0.03 * i, 2)))
        return out

    S = []

    # --- May 2025 ---
    S.append({
        "title": "Leadership Team Weekly", "date": "2025-05-21", "category": "work",
        "speakers": named(OWNER, "Alex Rivera", "Jordan Lee", "Priya Shah"),
        "turns": [
            ("SPEAKER_00", "Let's start with attendance trends for the quarter.", []),
            ("SPEAKER_01", "Chronic absence is down two points district wide.", []),
            ("SPEAKER_02", "The middle schools are still the soft spot though.", []),
            ("SPEAKER_03", "I can pull the by-school breakdown before Thursday.", []),
            ("SPEAKER_00", "Good. Let's make that the headline for the board.", []),
        ],
        "ai_title": "Leadership Team Weekly Attendance",
        "ai_summary": "The team reviewed quarterly attendance, noting chronic "
                      "absence fell two points district wide while middle schools "
                      "lagged. Priya will produce a by-school breakdown for the board.",
        "next_steps": ["[Priya Shah] will pull the by-school absence breakdown by Thursday",
                       "[Jordan Lee] will flag the three lowest middle schools by next week"],
    })
    S.append({
        "title": "Enrollment Data Review", "date": "2025-05-28", "category": "work",
        "speakers": named(OWNER, "Priya Shah", "Sam Chen"),
        "turns": [
            ("SPEAKER_00", "Where did we land on kindergarten projections?", []),
            ("SPEAKER_01", "Slightly ahead of last year, about one percent up.", []),
            ("SPEAKER_02", "The choice application data supports that read.", []),
            ("SPEAKER_01", "I'll reconcile it against the count-day file this week.", []),
        ],
        "ai_summary": "Kindergarten projections came in about one percent ahead of "
                      "last year, corroborated by choice-application data; Priya will "
                      "reconcile against the count-day file.",
        "next_steps": ["[Priya Shah] will reconcile projections against the count-day file this week"],
    })

    # --- Sep 2025 ---  (Board Prep carries a hidden one-time voice, U012)
    S.append({
        "title": "Board Prep Session", "date": "2025-09-10", "category": "work",
        "speakers": named(OWNER, "Dana Fox", "Jordan Lee")
        + [("SPEAKER_03", None, "Speaker 12", "U012", None)],
        "turns": [
            ("SPEAKER_00", "We need the deck to open on the graduation gain.", []),
            ("SPEAKER_01", "Ninety-one percent, the highest in a decade.", []),
            ("SPEAKER_03", "I sat in from the facilities vendor for this part.", []),
            ("SPEAKER_02", "Let's keep the finance slide to a single number.", []),
            ("SPEAKER_00", "Agreed. One number, one takeaway.", []),
        ],
        # deliberately NO ai_summary / next_steps: exercises the "no summary" state
    })
    S.append({
        "title": "Weekend Trip Planning", "date": "2025-09-27", "category": "personal",
        "speakers": named(OWNER, "Sam Chen"),
        "turns": [
            ("SPEAKER_00", "If we leave Friday night we beat the traffic.", []),
            ("SPEAKER_01", "I'll book the cabin for two nights then.", []),
            ("SPEAKER_00", "Perfect, I'll sort out the food.", []),
        ],
        "ai_summary": "A personal call settling a weekend trip: leave Friday night, "
                      "Sam books the cabin for two nights, Mark handles food.",
    })

    # --- Jan 2026 --- (Vendor Demo carries an active unknown, U007)
    S.append({
        "title": "Vendor Demo: Assessment Platform", "date": "2026-01-15",
        "speakers": named(OWNER, "Alex Rivera")
        + [("SPEAKER_02", None, "Speaker 7", "U007", None)],
        "turns": [
            ("SPEAKER_02", "Thanks for having us. I'll walk through the item bank first.", []),
            ("SPEAKER_00", "How does the platform handle our standards alignment?", []),
            ("SPEAKER_02", "Every item is tagged to the state framework out of the box.", []),
            ("SPEAKER_01", "Can we export the results into our warehouse nightly?", []),
            ("SPEAKER_02", "Yes, there's a nightly CSV drop and an API.", []),
        ],
        # untagged category on purpose (some meetings stay uncategorized)
        "ai_summary": "A vendor walked through an assessment platform: items tagged to "
                      "the state framework, with a nightly CSV export and an API into "
                      "the warehouse. No decision reached.",
    })
    S.append({
        "title": "Priya Shah 1:1", "date": "2026-01-22", "category": "work",
        "speakers": named(OWNER, "Priya Shah"),
        "turns": [
            ("SPEAKER_00", "How is the special-education dashboard coming along?", []),
            ("SPEAKER_01", "The caseload view is done, compliance is next.", []),
            ("SPEAKER_00", "Let's demo it at the next leadership meeting.", []),
        ],
        "next_steps": ["[Priya Shah] will demo the caseload view at the next leadership meeting"],
    })

    # --- Apr 2026 --- (THE review-queue meeting: flags, alt, minor crumbs) ---
    S.append({
        "title": "Leadership Team Weekly", "date": "2026-04-08", "category": "work",
        "source_file": "Leadership Team Weekly 04082026.m4a",
        "verify_engine": "mlxwhisper:turbo",
        "speakers": named(OWNER, "Alex Rivera", "Jordan Lee"),
        "decisions": [
            {"start": 0.5, "end": 2.2, "action": "accept", "text": None,
             "speaker_id": None, "at": "2026-04-08T21:20:11"},
        ],
        "turns": [
            ("SPEAKER_00", "Kicking off the weekly. Budget first, then facilities.", []),
            ("SPEAKER_01", "The reserve target we agreed on was three percent, not two.",
             ["possible:engine_disagreement"], "The reserve target we agreed on was three million, not two."),
            ("SPEAKER_02", "I think that number was actually from the prior fiscal year.",
             ["id_mismatch"]),
            ("SPEAKER_01", "Right.", ["overlap"]),
            ("SPEAKER_00", "The vendors overlapped the audio badly on this stretch here.",
             ["overlap"]),
            ("SPEAKER_02", "so yeah", ["overlap"]),
            ("SPEAKER_00", "Let's move the facilities update to next week and close out.", []),
        ],
        "ai_title": "Leadership Team Weekly Budget",
        "ai_summary": "The weekly opened on budget: a dispute over whether the reserve "
                      "target is three percent or three million was left for review, and "
                      "the facilities update slipped to next week.",
        "next_steps": ["[Alex Rivera] will confirm the reserve target figure before the next meeting"],
    })
    S.append({
        "title": "Enrollment Data Review", "date": "2026-04-15",
        "speakers": named(OWNER, "Sam Chen", "Priya Shah"),
        "turns": [
            ("SPEAKER_00", "Count day is three weeks out. Are we ready?", []),
            ("SPEAKER_01", "The validation scripts pass on last year's file.", []),
            ("SPEAKER_02", "I'll dry-run them against the live extract Monday.", []),
            ("SPEAKER_00", "Let's not repeat last year's late reconciliation.", []),
        ],
        # untagged; has next steps but no prose summary
        "next_steps": ["[Sam Chen] will dry-run the validation scripts on the live extract Monday"],
    })

    # --- Jul 2026 (recent) ---
    S.append({
        "title": "Board Prep Session", "date": "2026-07-02", "category": "work",
        "speakers": named(OWNER, "Dana Fox", "Jordan Lee"),
        "turns": [
            ("SPEAKER_00", "Final board prep before the summer meeting.", []),
            ("SPEAKER_01", "The capital plan slide is the one they'll push on.", []),
            ("SPEAKER_02", "I'll have the updated cost figures by end of day.", []),
            ("SPEAKER_00", "Great. Let's rehearse the open once more.", []),
        ],
        "ai_summary": "Final prep for the summer board meeting focused on the capital "
                      "plan slide; Jordan will supply updated cost figures by end of day.",
        "next_steps": ["[Jordan Lee] will send updated capital cost figures by end of day"],
    })

    # --- archived ---
    S.append({
        "title": "Old Sync Meeting", "date": "2025-03-04", "category": "work",
        "archived": True,
        "speakers": named(OWNER, "Alex Rivera"),
        "turns": [
            ("SPEAKER_00", "Quick sync, mostly logistics for the move.", []),
            ("SPEAKER_01", "I'll archive the old shared drive after this.", []),
        ],
        "ai_summary": "A short logistics sync about an office move; the old shared "
                      "drive is to be archived afterward.",
    })

    # --- un-named inbox meeting (reviewed=False, recorder's raw name) ---
    S.append({
        "title": "Recording", "date": "2026-07-10",
        "base": "Recording 07102026 0915",     # recorder default name; date in the middle
        "reviewed": False,
        "source_file": "Recording 07102026 0915.m4a",
        "speakers": [("SPEAKER_00", None, "Speaker 1", None, None),
                     ("SPEAKER_01", None, "Speaker 2", None, None)],
        "turns": [
            ("SPEAKER_00", "Okay, I think we should reset the budget planning cadence.", []),
            ("SPEAKER_01", "Monthly instead of quarterly would catch the swings sooner.", []),
            ("SPEAKER_00", "Let's pilot monthly for a term and see.", []),
        ],
        "ai_title": "Budget Planning Cadence",
        "ai_summary": "Two speakers weigh moving budget planning from quarterly to "
                      "monthly to catch swings sooner, agreeing to pilot monthly for a term.",
    })
    return S


def build(home: Path) -> dict:
    specs = meeting_specs()
    sources_seen: dict = {}
    built = [build_meeting(home, s, sources_seen) for s in specs]

    # unknown speakers: U007 active ("Who is this?"), U012 hidden (archived)
    unknown_meetings = {
        "U007": {"meetings": [b["base"] for b in built
                              if b["base"].startswith("Vendor Demo")],
                 "created": "2026-01-15T20:14:07"},
        "U012": {"meetings": [b["base"] for b in built
                              if b["base"].startswith("Board Prep Session 09")],
                 "created": "2025-09-10T20:14:07", "archived": True},
    }
    registry, unknowns = write_voiceprints(home, sources_seen, unknown_meetings)

    # queue: one plain waiting file + one held file, both real WAVs in source/
    src = home / "source"
    src.mkdir(parents=True, exist_ok=True)
    # real WAV bytes, so /api/queue_audio actually plays; .wav is a watched ext
    waiting = "Team Standup 07112026 0900.wav"
    held = "Draft Personal Memo 07112026.wav"
    write_wav(src / waiting, [(220.0, 0.3, 3.0), (330.0, 3.3, 6.0)], 6.5)
    write_wav(src / held, [(262.0, 0.3, 4.0)], 4.5)
    (home / "recordings").mkdir(parents=True, exist_ok=True)

    # history: successes for the live meetings + ONE real-looking failure
    live = [b for b in built if not b["archived"] and b["base"] != "Recording 07102026 0915"]
    history = []
    for b in sorted(live, key=lambda x: x["iso"], reverse=True):
        who = (" · " + ", ".join(b["identified"][:3])) if b["identified"] else ""
        history.append({
            "name": b["source_file"], "ok": True,
            "summary": f"{b['n_speakers']} speaker(s), "
                       f"{round(b['duration_sec'] / 60, 1)} min{who}",
            "at": f"{b['iso']}T20:14:07",
        })
    history.insert(0, {
        "name": "Vendor Webinar Recording.mp4", "ok": False,
        "summary": "RuntimeError: ffmpeg could not decode "
                   "'Vendor Webinar Recording.mp4' — moov atom not found (exit 1); "
                   "the MP4 container is truncated. Re-export the recording and "
                   "queue it again.",
        "at": "2026-07-09T02:41:55",
    })
    write_state(home, built, [waiting, held], [held], history)

    (home / MARKER).write_text(
        "Synthetic STT demo home built by tools/demo_seed.py. Safe to delete.\n"
        f"built_at={datetime.now().isoformat(timespec='seconds')}\n")

    return {"built": built, "registry": registry, "unknowns": unknowns,
            "history": history, "waiting": waiting, "held": held}


# ------------------------------------------------------------- CLI / safety -

def _real_data_dirs():
    """The real, currently-configured folders the seeder must never write into
    or under. Read WITHOUT STT_HOME, so these are the operator's live paths."""
    dirs = []
    for fn in (config.meetings_dir, config.source_dir, config.recordings_dir):
        try:
            dirs.append(Path(fn()).expanduser().resolve())
        except Exception:
            pass
    for p in (config.MEETINGS_DIR, config.ICLOUD_DIR, config.RECORDINGS_DIR,
              config.VOICEPRINTS_DIR):
        try:
            dirs.append(Path(p).expanduser().resolve())
        except Exception:
            pass
    return dirs


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def resolve_target(raw: str) -> Path:
    if not raw or not raw.strip():
        sys.exit("refusing: --dir is required and must be non-empty.")
    target = Path(raw).expanduser().resolve()
    for real in _real_data_dirs():
        if _overlaps(target, real):
            sys.exit(f"refusing: --dir {target} overlaps a real configured data "
                     f"folder ({real}). Pick a throwaway path such as qa/demo_home.")
    return target


def prepare_dir(target: Path, wipe: bool):
    if target.exists() and any(target.iterdir()):
        if not wipe:
            sys.exit(f"refusing: {target} already exists and is not empty. "
                     f"Re-run with --wipe to rebuild it from scratch.")
        if not (target / MARKER).exists():
            sys.exit(f"refusing to --wipe {target}: it has no {MARKER} marker, so "
                     f"it was not built by this tool. Delete it yourself if you are sure.")
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def _launch_instructions(target: Path, port: int) -> str:
    repo = Path(__file__).resolve().parent.parent
    venv_py = repo / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else "python3"
    return (
        "Launch the panel against this synthetic home (nothing real is touched):\n\n"
        f"    cd {repo}\n"
        f"    STT_HOME={target} STT_PANEL_PORT={port} {py} -m gui.server\n\n"
        f"Then open http://127.0.0.1:{port}/ in a browser.\n"
        "STT_HOME (config.py) redirects every data path — meetings, watched\n"
        "folders, voiceprints, manifest, status/history, holds, queue — under it;\n"
        "STT_PANEL_PORT (gui/server.py __main__) chooses the port. Code/static\n"
        "assets still load from this checkout, so the panel runs unchanged.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build a synthetic STT data home for panel demos/tests.")
    ap.add_argument("--dir", required=False, default=None,
                    help="target home directory (REQUIRED; e.g. qa/demo_home, "
                         "which is gitignored). Refused if it overlaps real data.")
    ap.add_argument("--wipe", action="store_true",
                    help="rebuild from scratch (only removes a home this tool built).")
    ap.add_argument("--port", type=int, default=8747,
                    help="port to print in the launch instructions (default 8747).")
    args = ap.parse_args(argv)

    if args.dir is None:
        ap.error("--dir is required; refusing to guess a location.")

    target = resolve_target(args.dir)
    prepare_dir(target, args.wipe)
    result = build(target)

    live = [b for b in result["built"] if not b["archived"]]
    inbox = [b for b in live if b["base"] == "Recording 07102026 0915"]
    processed = [b for b in live if b not in inbox]
    months = sorted({b["iso"][:7] for b in result["built"]})
    hidden = [u for u, m in result["unknowns"].items() if m.get("archived")]

    print(f"Seeded synthetic demo home at: {target}\n")
    print("Contents (by UI state):")
    print(f"  processed meetings : {len(processed)}  across months {', '.join(months)}")
    print(f"  archived meetings  : {sum(1 for b in result['built'] if b['archived'])}")
    print(f"  inbox (Needs naming, reviewed=false): {len(inbox)}")
    print( "  review-flag meeting: 1  (Leadership Team Weekly 04082026 — "
           "overlap + id_mismatch + engine-disagreement alt + minor crumbs)")
    cats = {}
    for b in processed:
        try:
            c = json.loads(Path(b["jpath"]).read_text()).get("category") or "untagged"
        except Exception:
            c = "untagged"
        cats[c] = cats.get(c, 0) + 1
    print(f"  categories         : {cats}")
    print(f"  enrolled speakers  : {len(result['registry'])}  ({', '.join(result['registry'])})")
    print(f"  unknown speakers   : {len(result['unknowns'])}  "
          f"(hidden/archived: {', '.join(hidden) or 'none'})")
    print(f"  history entries    : {len(result['history'])}  "
          f"(failures: {sum(1 for h in result['history'] if not h['ok'])})")
    print(f"  queue              : waiting='{result['waiting']}', held='{result['held']}'")
    print()
    print(_launch_instructions(target, args.port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
