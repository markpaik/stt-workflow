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

All person names are synthetic (an invented owner, Morgan Ellis, and five
invented colleagues), and so is every title, number, and line. Titles follow
the series catalog (stt/series.py), and each described meeting carries the
current description fields: a gist, Decisions, Open questions, and topic
tags. Three meetings are dated relative to the day the home is built, so Home
shows a title to set, draft titles, and a voice to name. Each meeting
carries its meeting_id and the store keys a real first write gives it, so a
fresh home is doctor-clean (`tools/doctor.py check`). The tool
REFUSES to run without --dir, and REFUSES any --dir that overlaps a real,
configured data folder — it can only ever write inside its own target.
"""
import argparse
import json
import math
import os
import plistlib
import shutil
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from wave import open as wave_open

# Run straight from a checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # a core repo dependency (identify.py); the real loaders need it

from stt import config, dates, output, speaker_text, unknowns  # real writers/helpers — never invent a schema

# --- synthetic cast (the ONLY person names that may appear) ---
OWNER = "Morgan Ellis"
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


def _store_keys(base: str, iso: str, meta: dict, wav: Path, spec: dict) -> dict:
    """The store keys the pipeline writes at a meeting's first write
    (provenance.KEYS, docs/store-contract.md). Without them a fresh demo
    home failed `tools/doctor.py check` with one missing_keys finding per
    meeting, so every screenshot and QA run started from findings.

    A source name that carries the meeting's date is a typed date. Any
    other arrived as a synced memo, whose recorder start time is the true
    start: the clip's length before it was processed, on the same day.

    A recorder name ('YYYY-MM-DD Recording HHMM', recorder.final_name)
    says when the capture started: provenance dates it 'recorder' at that
    time, and so does the seed. Derived from the processing time instead,
    Home showed 'Recording 1430' at 3:01 PM, and 'Recording 0915' read as
    a typed date with no time."""
    from stt import dupes, provenance
    src = meta["source_file"]
    stem = Path(src).stem
    native = provenance._NATIVE.match(stem)
    ds = spec.get("date_source") or (
        "recorder" if native else
        "typed" if dates.meeting_date(stem) == iso else "recorder")
    rec = None
    if ds == "recorder":
        done = datetime.fromisoformat(meta["processed_at"])
        start = done - timedelta(seconds=float(meta["duration_sec"]) + 120)
        if native:
            start = datetime.fromisoformat(f"{iso}T{native[1]}:{native[2]}:00")
        if start.date().isoformat() != iso:
            start = datetime.fromisoformat(f"{iso}T09:00:00")
        rec = start.astimezone().isoformat(timespec="seconds")
    return {"title": dates.title_of(base), "source_name": src,
            "audio_fp": dupes.fingerprint(wav), "date_source": ds,
            "recorded_at": rec}


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
        # turns run at a talking pace (every live meeting runs over a
        # minute), and a long-talk turn lets a voice cross the panel's
        # one-minute ask floor
        if not minor:   # a sub-second crumb stays sub-second (review.is_minor)
            dur = round(dur * float(spec.get("stretch", 8.0)), 2)
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
        # first key, as config.backfill_meeting_ids writes it (id_of reads
        # a bounded head of the file)
        "meeting_id": config.new_meeting_id(),
        "source_file": spec.get("source_file", f"{title}.m4a"),
        "source_mtime": src_mtime,
        "date": iso,
        "processed_at": spec.get("arrived_at") or f"{iso}T20:14:07",
        "duration_sec": total_dur,
        "asr_engine": spec.get("asr_engine", config.QWEN_ASR_MODEL),
        "diarizer": config.DIARIZATION_MODEL,
        "n_speakers": len(speakers),
        "strict": strict,
        "one_time_speakers": False,
        "punctuated": True,
        "verify_engine": spec.get("verify_engine"),
        "overlap_spans": [[s["start"], s["end"]] for s in segments if s["overlap"]],
        "refine_stats": {"reassigned": 0, "smoothed": 0, "flagged": sum(1 for s in segments if s["flags"])},
    }
    meta.update(_store_keys(base, iso, meta, wav, spec))
    if spec.get("category"):
        meta["category"] = spec["category"]
    meta["reviewed"] = bool(reviewed)
    for k in ("title_state", "title_source", "arrived_at"):
        if spec.get(k):
            meta[k] = spec[k]
    for k in ("ai_title", "ai_summary"):
        if spec.get(k):
            meta[k] = spec[k]
    if spec.get("gist"):
        # the current description shape (stt/summarize.py DESCRIPTION_KEYS):
        # gist, Decisions, Open questions, topic tags, and the series id.
        # ai_summary is their plain-text join, as store_description writes it.
        meta["ai_gist"] = spec["gist"]
        meta["ai_decisions"] = list(spec.get("decisions_list") or [])
        meta["ai_open_questions"] = list(spec.get("open_questions") or [])
        meta["ai_tags"] = list(spec.get("tags") or [])
        if spec.get("series"):
            meta["ai_series"] = spec["series"]
        meta["ai_summary"] = speaker_text.compose_summary(
            spec["gist"], meta["ai_decisions"], meta["ai_open_questions"])
        meta["ai_model"] = "qwen3.8:27b-mlx"
    if meta.get("ai_summary"):
        meta["ai_generated_at"] = f"{iso}T20:15:30"

    jpath = mdir / f"{base}.json"
    header = output.txt_header(output.header_name(base, meta), total_dur, speakers, strict,
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
    # "next" is the monotonic counter the real store keeps (unknowns.
    # heal_next, past the highest id as mint_uid keeps it): without it the
    # doctor reported registry_stale_next and registry_doctor reported
    # stale_next and exited 1 on every fresh demo home
    reg = {"next": unknowns.next_floor({"speakers": speakers}), "speakers": speakers}
    (vp / "unknowns.json").write_text(json.dumps(reg, indent=2))
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

    # the launchd agent, SANDBOXED: gui/server.py resolves its AGENT plist under
    # STT_HOME (and skips launchctl entirely there), so the panel's Automation
    # section and /api/schedule exercise THIS copy — never the operator's real
    # ~/Library/LaunchAgents agent. ProgramArguments is inert on purpose: launchd
    # never sees this file.
    la = home / "LaunchAgents"
    la.mkdir(exist_ok=True)
    (la / "com.stt-workflow.batch.plist").write_bytes(plistlib.dumps({
        "Label": "com.stt-workflow.batch",
        "ProgramArguments": ["/usr/bin/true"],
        "StartCalendarInterval": {"Hour": 2, "Minute": 0},
        "WatchPaths": [str(home / "source"), str(home / "recordings")],
        "RunAtLoad": True}))


# ------------------------------------------------------------- the content -

def meeting_specs(today=None):
    """Nine live meetings across five months over 2025-2026, three recent
    meetings dated from `today` (the build day), one archived meeting, and
    one un-named inbox meeting. Speaker ids are per-meeting SPEAKER_0N.

    Titles follow the series rule: "<series name> – <short topic>" for a
    recurring meeting, the topic alone for a one-off."""
    today = today or date.today()

    def ago(days):
        return (today - timedelta(days=days)).isoformat()

    def stamp_ago(days, hhmm="14:30:00"):
        return f"{ago(days)}T{hhmm}"

    def named(*names):
        # -> speaker defs for enrolled people, ids SPEAKER_00..
        out = []
        for i, nm in enumerate(names):
            out.append((f"SPEAKER_{i:02d}", nm, nm, None, round(0.86 + 0.03 * i, 2)))
        return out

    S = []

    # --- May 2025 ---
    S.append({
        "title": "LT Weekly Meeting – Attendance Trends", "date": "2025-05-21",
        "category": "work", "series": "lt_weekly",
        "speakers": named(OWNER, "Alex Rivera", "Jordan Lee", "Priya Shah"),
        "turns": [
            ("SPEAKER_00", "Let's start with attendance trends for the quarter.", []),
            ("SPEAKER_01", "Absence is down a little across the sample schools.", []),
            ("SPEAKER_02", "The middle grades are still the soft spot though.", []),
            ("SPEAKER_03", "I can pull the by-school breakdown before Thursday.", []),
            ("SPEAKER_00", "Good. Let's make that the headline for the report.", []),
        ],
        "gist": "Absence fell slightly across the sample schools, and the middle "
                "grades remain the weak spot.",
        "decisions_list": ["The by-school breakdown leads the next report."],
        "open_questions": ["Which middle-grade schools need a closer look first?"],
        "tags": ["attendance", "school-performance", "reporting-dashboards"],
    })
    S.append({
        "title": "Enrollment Data Review – Kindergarten Projections", "date": "2025-05-28",
        "category": "work", "series": "enrollment_review",
        "speakers": named(OWNER, "Priya Shah", "Sam Chen"),
        "turns": [
            ("SPEAKER_00", "Where did we land on kindergarten projections?", []),
            ("SPEAKER_01", "Slightly ahead of last year in the model.", []),
            ("SPEAKER_02", "The application data supports that read.", []),
            ("SPEAKER_01", "I'll reconcile it against the count file this week.", []),
        ],
        "gist": "Kindergarten projections run slightly ahead of last year, and the "
                "application data agrees.",
        "decisions_list": ["Priya reconciles the projection against the count file."],
        "open_questions": [],
        "tags": ["enrollment", "data-systems", "reporting-dashboards"],
    })

    # --- Sep 2025 ---  (the study session carries a hidden one-time voice, U012)
    S.append({
        "title": "Board Study Session – Graduation Story", "date": "2025-09-10",
        "category": "work",
        "speakers": named(OWNER, "Dana Fox", "Jordan Lee")
        + [("SPEAKER_03", None, "Speaker 12", "U012", None)],
        "turns": [
            ("SPEAKER_00", "We need the deck to open on the graduation trend.", []),
            ("SPEAKER_01", "The trend line is the story, not any one year.", []),
            ("SPEAKER_03", "I sat in from the facilities vendor for this part.", []),
            ("SPEAKER_02", "Let's keep the finance slide to a single number.", []),
            ("SPEAKER_00", "Agreed. One number, one takeaway.", []),
        ],
        # deliberately NO description: exercises the "no summary yet" state
    })
    S.append({
        "title": "Weekend Trip Planning", "date": "2025-09-27", "category": "personal",
        "speakers": named(OWNER, "Sam Chen"),
        "turns": [
            ("SPEAKER_00", "If we leave Friday night we beat the traffic.", []),
            ("SPEAKER_01", "I'll book the cabin for two nights then.", []),
            ("SPEAKER_00", "Perfect, I'll sort out the food.", []),
        ],
        "gist": "Leave Friday night; Sam books the cabin for two nights and "
                "Morgan handles food.",
        "decisions_list": ["Leave Friday night.", "Book the cabin for two nights."],
        "open_questions": [],
        "tags": ["personal"],
    })

    # --- Jan 2026 --- (the vendor demo carries an active unknown, U007)
    S.append({
        "title": "Assessment Platform Demo", "date": "2026-01-15",
        "speakers": named(OWNER, "Alex Rivera")
        + [("SPEAKER_02", None, "Speaker 7", "U007", None)],
        "turns": [
            ("SPEAKER_02", "Thanks for having us. I'll walk through the item bank first.", []),
            ("SPEAKER_00", "How does the platform handle standards alignment?", []),
            ("SPEAKER_02", "Every item is tagged to the state framework out of the box.", []),
            ("SPEAKER_01", "Can we export the results into our warehouse nightly?", []),
            ("SPEAKER_02", "Yes, there's a nightly CSV drop and an API.", []),
        ],
        # untagged category on purpose (some meetings stay uncategorized)
        "gist": "A vendor showed an assessment platform with standards tagging and "
                "a nightly export.",
        "decisions_list": [],
        "open_questions": ["Does the nightly export fit the warehouse load window?"],
        "tags": ["assessment", "partnerships", "data-systems"],
    })
    S.append({
        "title": "Priya Weekly Check-in – Caseload Dashboard", "date": "2026-01-22",
        "category": "work", "series": "weekly_checkin",
        "speakers": named(OWNER, "Priya Shah"),
        "turns": [
            ("SPEAKER_00", "How is the caseload dashboard coming along?", []),
            ("SPEAKER_01", "The caseload view is done, the compliance view is next.", []),
            ("SPEAKER_00", "Let's demo it at the next leadership meeting.", []),
        ],
        "gist": "The caseload view is done and the compliance view is next.",
        "decisions_list": ["Demo the caseload view at the next leadership meeting."],
        "open_questions": [],
        "tags": ["reporting-dashboards", "special-education", "team-operations"],
    })

    # --- Apr 2026 --- (THE review-queue meeting: flags, alt, minor crumbs) ---
    S.append({
        "title": "LT Weekly Meeting – Reserve Target", "date": "2026-04-08",
        "category": "work", "series": "lt_weekly",
        "source_file": "LT Weekly Meeting 04082026.m4a",
        # flags with a second engine's words: a cloud transcript checked
        # by Qwen3-ASR, the one pairing verify runs
        "asr_engine": "elevenlabs/scribe_v2",
        "verify_engine": config.QWEN_ASR_MODEL,
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
        "gist": "The reserve target is in dispute, and the facilities update moves "
                "to next week.",
        "decisions_list": ["The facilities update moves to next week."],
        "open_questions": ["Is the reserve target three percent or three million?",
                           "Does the figure come from the prior fiscal year?"],
        "tags": ["budget-finance", "team-operations", "goals-okrs"],
    })
    S.append({
        "title": "Enrollment Data Review – Count Day Readiness", "date": "2026-04-15",
        "series": "enrollment_review",
        "speakers": named(OWNER, "Sam Chen", "Priya Shah"),
        "turns": [
            ("SPEAKER_00", "Count day is three weeks out. Are we ready?", []),
            ("SPEAKER_01", "The validation scripts pass on last year's file.", []),
            ("SPEAKER_02", "I'll dry-run them against the live extract Monday.", []),
            ("SPEAKER_00", "Let's not repeat last year's late reconciliation.", []),
        ],
        # untagged category
        "gist": "The validation scripts pass on last year's file, with a dry run "
                "on the live extract next.",
        "decisions_list": ["Dry-run the validation scripts on the live extract Monday."],
        "open_questions": [],
        "tags": ["enrollment", "data-systems", "policy-compliance"],
    })

    # --- Jul 2026 ---
    S.append({
        "title": "Board Study Session – Capital Plan", "date": "2026-07-02",
        "category": "work", "series": "board_study",
        "speakers": named(OWNER, "Dana Fox", "Jordan Lee"),
        "turns": [
            ("SPEAKER_00", "Final prep before the summer session.", []),
            ("SPEAKER_01", "The capital plan slide is the one they'll push on.", []),
            ("SPEAKER_02", "I'll have the updated cost figures by end of day.", []),
            ("SPEAKER_00", "Great. Let's rehearse the open once more.", []),
        ],
        "gist": "Prep for the summer session centers on the capital plan slide.",
        "decisions_list": ["Rehearse the opening once more."],
        "open_questions": ["Will the updated cost figures change the capital plan slide?"],
        "tags": ["board-governance", "budget-finance"],
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
        "base": "2026-07-10 Recording 0915",     # recorder default name
        "reviewed": False,
        "source_file": "2026-07-10 Recording 0915.m4a",
        "speakers": [("SPEAKER_00", None, "Speaker 1", None, None),
                     ("SPEAKER_01", None, "Speaker 2", None, None)],
        "turns": [
            ("SPEAKER_00", "Okay, I think we should reset the budget planning cadence.", []),
            ("SPEAKER_01", "Monthly instead of quarterly would catch the swings sooner.", []),
            ("SPEAKER_00", "Let's pilot monthly for a term and see.", []),
        ],
        "ai_title": "Budget Planning Cadence",
        "gist": "Budget planning moves from quarterly to monthly for a one-term pilot.",
        "decisions_list": ["Pilot monthly budget planning for one term."],
        "open_questions": [],
        "tags": ["budget-finance", "team-operations"],
    })

    # --- recent: dated from the build day, so Home has work in it ---
    # a new recording with only the model's draft title: a Needs you card
    S.append({
        "title": "Recording", "date": ago(0),
        "base": f"{ago(0)} Recording 1430",
        "title_state": "needs_title", "title_source": "model",
        "arrived_at": stamp_ago(0, "15:05:00"), "date_source": "recorder",
        "source_file": f"{ago(0)} Recording 1430.m4a",
        "speakers": named(OWNER, "Sam Chen"),
        "turns": [
            ("SPEAKER_00", "Can we settle the spring schedule before the break?", []),
            ("SPEAKER_01", "Two sessions a week, with the lab on Thursdays.", []),
            ("SPEAKER_00", "That works. I'll send it to the group tonight.", []),
        ],
        "ai_title": "Spring Schedule Planning",
        "gist": "The spring schedule is two sessions a week, with the lab on Thursdays.",
        "decisions_list": ["Two sessions a week, with the lab on Thursdays."],
        "open_questions": ["Does the group need the schedule before the break?"],
        "tags": ["professional-learning", "team-operations"],
    })
    # a kept series title the model drafted: a Draft titles row
    S.append({
        "title": "Jordan Weekly Check-in – Hiring Plan", "date": ago(1),
        "category": "work", "series": "weekly_checkin",
        "title_state": "provisional", "title_source": "model",
        "arrived_at": stamp_ago(1, "11:40:00"), "date_source": "recorder",
        "speakers": named(OWNER, "Jordan Lee"),
        "turns": [
            ("SPEAKER_00", "Where are we on the two analyst roles?", []),
            ("SPEAKER_01", "Six applicants passed the screen, interviews start Monday.", []),
            ("SPEAKER_00", "Let's use the same work sample for both roles.", []),
            ("SPEAKER_01", "Agreed. I still need a second panelist for Tuesday.", []),
        ],
        "gist": "Six applicants passed the screen for two analyst roles, and "
                "interviews start Monday.",
        "decisions_list": ["Both roles use the same work sample."],
        "open_questions": ["Who joins the Tuesday interview panel?"],
        "tags": ["hiring-staffing", "team-operations"],
    })
    # a one-off with a new voice that talks over a minute: the voice card
    S.append({
        "title": "Reading Partner Kickoff", "date": ago(3),
        "category": "work",
        "title_state": "provisional", "title_source": "model",
        "arrived_at": stamp_ago(3, "10:15:00"), "date_source": "recorder",
        "stretch": 7.0,
        "speakers": named(OWNER, "Dana Fox")
        + [("SPEAKER_02", None, "Speaker 9", "U009", None)],
        "turns": [
            ("SPEAKER_02", "Thanks for the time. Our tutors work in small groups of three.", []),
            ("SPEAKER_00", "How do you match tutors to students in the first month?", []),
            ("SPEAKER_02", "We start from the screening scores and regroup every six weeks.", []),
            ("SPEAKER_01", "Can your team share session logs with us each week?", []),
            ("SPEAKER_02", "Yes, we send a weekly file with attendance and minutes.", []),
            ("SPEAKER_00", "Let's start with two pilot schools in January.", []),
            ("SPEAKER_02", "That works for us. I'll send the draft agreement Friday.", []),
        ],
        "gist": "A reading partner will pilot small-group tutoring in two schools "
                "starting in January.",
        "decisions_list": ["Pilot in two schools starting in January.",
                           "The partner sends weekly session logs."],
        "open_questions": ["Which two schools join the pilot?",
                           "Does the draft agreement cover data sharing?"],
        "tags": ["literacy", "partnerships", "research-evaluation"],
    })
    return S


def build(home: Path) -> dict:
    specs = meeting_specs()
    sources_seen: dict = {}
    built = [build_meeting(home, s, sources_seen) for s in specs]

    # unknown speakers: U007 active ("Who is this?"), U012 hidden (archived)
    unknown_meetings = {
        "U007": {"meetings": [b["base"] for b in built
                              if dates.title_of(b["base"]).startswith("Assessment Platform")],
                 "created": "2026-01-15T20:14:07"},
        "U012": {"meetings": [b["base"] for b in built
                              if b["base"].startswith("2025-09-10 Board Study Session")],
                 "created": "2025-09-10T20:14:07", "archived": True},
        # U009 talks over a minute in a meeting from this week: Home's voice card
        "U009": {"meetings": [b["base"] for b in built
                              if dates.title_of(b["base"]) == "Reading Partner Kickoff"],
                 "created": datetime.now().isoformat(timespec="seconds")},
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
    live = [b for b in built if not b["archived"] and b["base"] != "2026-07-10 Recording 0915"]
    history = []
    for b in sorted(live, key=lambda x: x["iso"], reverse=True):
        who = (" · " + ", ".join(b["identified"][:3])) if b["identified"] else ""
        history.append({
            "name": b["source_file"], "ok": True,
            "summary": f"{b['n_speakers']} speaker(s), "
                       f"{output.length_text(b['duration_sec'])}{who}",
            "at": f"{b['iso']}T20:14:07",
        })
    history.insert(0, {
        "name": "Vendor Webinar Recording.mp4", "ok": False,
        # the text the batch records for an unreadable file (audio._run_ffmpeg)
        "summary": "ffmpeg could not decode Vendor Webinar Recording.mp4: "
                   "moov atom not found",
        "at": "2026-07-09T02:41:55",
    })
    write_state(home, built, [waiting, held], [held], history)

    (home / MARKER).write_text(
        "Synthetic STT demo home built by tools/demo_seed.py. Safe to delete.\n"
        f"built_at={datetime.now().isoformat(timespec='seconds')}\n")

    return {"built": built, "registry": registry, "unknowns": unknowns,
            "history": history, "waiting": waiting, "held": held}


# ------------------------------------------------------------- CLI / safety -

# The folders the batch and the panel use, as config resolves them in a
# fresh interpreter with no STT_HOME, no STT_PROJECT_DIR and no STT_<X>_DIR
# override: launchd sets none of them, so this is the live configuration
# (the checkout's stt.env, else the defaults). This process's config cannot
# say it: under STT_HOME every path it holds is inside the scratch home.
# The Voice Memos library is left out on purpose: resolve() would stat a
# path inside another app's container, and the seeder never reads it.
_LIVE_DIRS_CODE = """
import json
from stt import config, voicememos
print(json.dumps([str(p) for p in (
    config.meetings_dir(), config.source_dir(), config.recordings_dir(),
    config.MEETINGS_DIR, config.ICLOUD_DIR, config.RECORDINGS_DIR,
    config.VOICEPRINTS_DIR, voicememos.staging_dir())]))
"""


def _live_data_dirs():
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("STT_HOME", "STT_PROJECT_DIR", "STT_VOICEMEMOS_STAGING")
           and not (k.startswith("STT_") and k.endswith("_DIR"))}
    repo = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = str(repo)
    try:
        r = subprocess.run([sys.executable, "-c", _LIVE_DIRS_CODE], env=env,
                           cwd=str(repo), capture_output=True, text=True, timeout=60)
        return [Path(p) for p in json.loads(r.stdout.strip().splitlines()[-1])]
    except Exception as e:
        # no list means no guard: refuse rather than write blind
        sys.exit(f"refusing: could not read the live data folders ({e!r}).")


# the batch agent setup.sh installs: its WorkingDirectory is the checkout
# the live batch runs from
BATCH_PLIST_NAME = "Library/LaunchAgents/com.stt-workflow.batch.plist"
# the stt.env settings that name a data folder, and the folder each one
# defaults to inside its checkout (None: the default is outside it)
_CHECKOUT_FOLDERS = (("STT_MEETINGS_DIR", None), ("STT_ICLOUD_DIR", None),
                     ("STT_RECORDINGS_DIR", None),
                     ("STT_VOICEPRINTS_DIR", "voiceprints"),
                     ("STT_VOICEMEMOS_STAGING", "voicememos"))


def _other_checkouts(repo: Path) -> list:
    """The checkouts other than `repo` that can hold the live settings: the
    main checkout of this git repository, and the folder the batch agent
    runs from. A worktree has no stt.env of its own, so a guard that read
    only its own checkout saw the defaults and not the real library."""
    import subprocess
    found = []
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                           capture_output=True, text=True, timeout=30)
        common = r.stdout.strip()
        if r.returncode == 0 and common:
            common = (repo / common).resolve()
            found.append(common.parent if common.name == ".git" else common)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open(Path.home() / BATCH_PLIST_NAME, "rb") as f:
            wd = plistlib.load(f).get("WorkingDirectory")
        if isinstance(wd, str) and wd.strip():
            found.append(Path(wd).expanduser().resolve())
    except (OSError, ValueError, plistlib.InvalidFileException):
        pass
    out, seen = [], {_fold(repo.resolve())}
    for co in found:   # one folder on APFS, whatever its letter case
        if _fold(co) not in seen:
            seen.add(_fold(co))
            out.append(co)
    return out


def _other_checkout_dirs(repo: Path) -> list:
    """The data folders that the stt.env of each other checkout names
    (_other_checkouts), and the folders inside it that hold real state."""
    from stt import envfile
    dirs = []
    for co in _other_checkouts(repo):
        env = envfile.parse(co / "stt.env")
        for key, inside in _CHECKOUT_FOLDERS:
            for v in (env.get(key), inside and str(co / inside)):
                if v:
                    p = Path(v).expanduser()
                    dirs.append(p if p.is_absolute() else co / p)
    return dirs


def _real_data_dirs():
    """The real folders the seeder must never write into or under: the live
    ones (_live_data_dirs), the ones this process's config names, and the
    ones the other checkouts name (_other_checkout_dirs).

    The live list used to be this process's config alone, and the comment
    said it was read without STT_HOME. Nothing cleared STT_HOME, so with it
    exported every folder was inside the scratch home, and --dir inside the
    real library passed."""
    dirs = []
    for fn in (config.meetings_dir, config.source_dir, config.recordings_dir):
        try:
            dirs.append(Path(fn()).expanduser().resolve())
        except Exception:
            pass
    repo = Path(__file__).resolve().parent.parent
    for p in (config.MEETINGS_DIR, config.ICLOUD_DIR, config.RECORDINGS_DIR,
              config.VOICEPRINTS_DIR, *_live_data_dirs(),
              *_other_checkout_dirs(repo)):
        try:
            p = Path(p).expanduser().resolve()
        except Exception:
            continue
        if p not in dirs:
            dirs.append(p)
    return dirs


def _fold(p: Path) -> Path:
    """`p` as APFS compares names: letter case and Unicode composition do
    not matter. resolve() keeps the case it is given, so ~/projects/... and
    ~/Projects/... were two folders to the guard and one folder on disk."""
    return Path(unicodedata.normalize("NFC", str(p)).casefold())


def _overlaps(a: Path, b: Path) -> bool:
    """True when `a` is `b`, or one holds the other. On a case-sensitive
    volume the fold refuses a little more, never less."""
    a, b = _fold(a), _fold(b)
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
        "assets still load from this checkout, so the panel runs unchanged.\n"
        "SCHEDULING IS SANDBOXED TOO: the launchd plist resolves to the seeded\n"
        "LaunchAgents/ copy under STT_HOME and launchctl is NEVER called, so\n"
        "/api/schedule and /api/automation cannot touch the real agent.")


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
    inbox = [b for b in live if b["base"] == "2026-07-10 Recording 0915"]
    processed = [b for b in live if b not in inbox]
    months = sorted({b["iso"][:7] for b in result["built"]})
    hidden = [u for u, m in result["unknowns"].items() if m.get("archived")]

    print(f"Seeded synthetic demo home at: {target}\n")
    print("Contents (by UI state):")
    print(f"  processed meetings : {len(processed)}  across months {', '.join(months)}")
    print(f"  archived meetings  : {sum(1 for b in result['built'] if b['archived'])}")
    print(f"  inbox (Needs naming, reviewed=false): {len(inbox)}")
    print( "  review-flag meeting: 1  (2026-04-08 LT Weekly Meeting – Reserve Target: "
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
