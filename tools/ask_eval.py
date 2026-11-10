#!/usr/bin/env python
"""The Phase 4F stop rule for library Ask, on synthetic meetings only.

The unified plan's gate: 80% of known-answer questions get an answer with a
working citation, and every "nothing found" question declines. If the gate
fails, keep search and per-meeting Ask, and stop.

This tool builds a synthetic store (12 meetings, filler talk, 25 planted
facts at known seconds, some near the end of a long meeting), asks 25
known-answer questions and 12 questions the store cannot answer, and scores:

- answered: the expected value appears in the answer;
- working citation: a citation names the fact's meeting, that meeting is
  live, and the cited second is within CITE_SLACK seconds of the fact line;
- declined: a nothing-found question gets the nothing-found answer.

Six nothing-found questions share no topic with the store, so retrieval
finds little or nothing and the model is rarely called. The other six are
NEAR MISSES: each shares topic words with a planted fact (the same reserve,
another campus) but the store cannot answer it. Retrieval returns the
planted fact, so these measure whether the model itself declines.

It also records the p90 answer time. It writes only inside STT_HOME, and it
refuses to run without STT_HOME or with a home that holds real data.

  STT_HOME=<scratch home> python tools/ask_eval.py --build
  STT_HOME=<scratch home> python tools/ask_eval.py --run [--oracle]

--run uses the local model through stt/summarize.py. --oracle replaces the
model with a simple extractive stand-in (the line with the most question
words, or NOT FOUND), which tests retrieval, citation, and decline without
the model. The report is JSON on stdout: counts and rates, plus each
question's verdict.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MARKER = ".ask_eval_home"
GATE = 0.80
CITE_SLACK = 60.0
STEP = 15.0               # seconds between synthetic lines

PEOPLE = ["Avery Stone", "Blake Rivera", "Casey Moore", "Devon Park", "Emery Lane"]

FILLER = [
    "Thanks for making the time today.",
    "Let me pull up the shared notes first.",
    "I think we covered most of that last time.",
    "Can everyone see the screen right now?",
    "Let us keep moving so we finish on time.",
    "That sounds reasonable to me.",
    "I can take a look at it this afternoon.",
    "We should loop back on that later.",
    "Good point, I had not thought of it that way.",
    "Let me check my calendar and get back to you.",
    "I agree with where this is heading.",
    "Could you say a little more about that?",
    "We had a similar conversation in the spring.",
    "I will send a short recap after this.",
    "Sorry, I was on mute for a second.",
    "That makes sense given the timeline.",
    "Let us park that and come back to it.",
    "I want to make sure we are aligned here.",
    "Nothing else from my side on this one.",
    "OK, moving on to the next item.",
]

# (meeting base, date, start hour, people, line count,
#  [(line index, speaker, fact sentence, question, expected value)])
MEETINGS = [
    ("Facilities Budget Review 10012026", "2026-10-01", 9, [0, 1], 60, [
        (12, 0, "The capital reserve for the north campus roof is 412 thousand dollars.",
         "How much is the capital reserve for the north campus roof?", "412"),
        (40, 1, "The boiler replacement at the annex slips to February.",
         "When is the boiler replacement at the annex?", "February")]),
    ("Transportation Sync 10022026", "2026-10-02", 10, [1, 2], 50, [
        (8, 2, "Route seventeen adds a stop at Maple and Fifth starting Monday.",
         "Which stop does route seventeen add?", "Maple"),
        (30, 1, "We need nine more substitute drivers before winter break.",
         "How many substitute drivers do we need before winter break?", "nine")]),
    ("Literacy Grant Planning 10052026", "2026-10-05", 13, [0, 3], 70, [
        (20, 3, "The literacy grant requires quarterly reports to the state by the fifteenth.",
         "When are the literacy grant quarterly reports due?", "fifteenth"),
        (55, 0, "Casey will lead the tutor training on the second Tuesday.",
         "Who will lead the tutor training?", "Casey")]),
    ("Data Team Standup 10062026", "2026-10-06", 8, [0, 4], 40, [
        (5, 4, "The attendance dashboard refresh moves to six in the morning.",
         "What time does the attendance dashboard refresh?", "six"),
        (25, 0, "Emery found that the enrollment extract drops students with a null grade.",
         "What did Emery find in the enrollment extract?", "null grade")]),
    ("Board Prep Session 10082026", "2026-10-08", 15, [1, 2, 3], 400, [
        (30, 1, "The superintendent wants the facilities slide moved to the front.",
         "Which slide does the superintendent want moved to the front?", "facilities"),
        (200, 2, "Public comment is capped at forty speakers for the November meeting.",
         "How many speakers is public comment capped at?", "forty"),
        (394, 3, "Final decision: the strategic plan vote happens on December ninth.",
         "When does the strategic plan vote happen?", "December")]),
    ("Hiring Committee 10092026", "2026-10-09", 11, [0, 2], 45, [
        (10, 2, "We are interviewing three paraprofessional candidates on Thursday.",
         "How many paraprofessional candidates are we interviewing?", "three"),
        (35, 0, "The data analyst posting closes on October thirtieth.",
         "When does the data analyst posting close?", "thirtieth")]),
    ("Technology Refresh 10132026", "2026-10-13", 14, [3, 4], 55, [
        (15, 4, "The Chromebook carts arrive at the east building in the second week.",
         "When do the Chromebook carts arrive?", "second week"),
        (45, 3, "Devon says the wireless upgrade costs eighty six thousand dollars.",
         "How much does the wireless upgrade cost?", "eighty six")]),
    ("Family Engagement Night 10142026", "2026-10-14", 17, [1, 4], 50, [
        (12, 1, "Translation headsets are booked for Arabic and Spanish families.",
         "Which languages are the translation headsets booked for?", "Arabic"),
        (38, 4, "Dinner for family night comes from the culinary arts program.",
         "Who provides dinner for family night?", "culinary")]),
    ("Safety Committee 10152026", "2026-10-15", 9, [0, 2], 48, [
        (18, 2, "The lockdown drill at the middle school ran four minutes long.",
         "How long over did the lockdown drill run?", "four minutes"),
        (40, 0, "Visitor badges will switch to the new kiosk in January.",
         "When do visitor badges switch to the new kiosk?", "January")]),
    ("Assessment Calibration 10192026", "2026-10-19", 13, [2, 3], 60, [
        (22, 3, "Raters agreed within one point on ninety two percent of the essays.",
         "What share of essays did raters agree on within one point?", "ninety two"),
        (50, 2, "The writing rubric drops the separate conventions row next year.",
         "Which row does the writing rubric drop next year?", "conventions")]),
    ("Garden Project Check-in 10202026", "2026-10-20", 16, [1, 3], 40, [
        (10, 3, "Raised beds go behind the cafeteria next to the loading dock.",
         "Where do the raised beds go?", "cafeteria"),
        (30, 1, "The seed order from the cooperative arrives in March.",
         "When does the seed order arrive?", "March")]),
    ("Science Fair Logistics 10222026", "2026-10-22", 12, [0, 4], 45, [
        (14, 4, "Judges score each poster on a twelve point rubric.",
         "How many points is the science fair judging rubric?", "twelve"),
        (33, 0, "The gym is reserved for the fair from noon until five.",
         "Where is the science fair held?", "gym")]),
]

# questions the store cannot answer: each must decline
NOT_FOUND = [
    "What did we decide about the swimming pool heater?",
    "Who is the new soccer coach?",
    "When does the planetarium reopen?",
    "What is the parking garage fee?",
    "Which vendor supplies the vending machines?",
    "How many tickets were sold for the gala?",
]
# near misses: retrieval finds a planted fact, but it answers another question
NEAR_MISS = [
    "How much is the capital reserve for the south campus gym?",
    "When is the boiler replacement at the main building?",
    "Which stop does route twenty add?",
    "How many substitute teachers do we need before winter break?",
    "Who will lead the parent training?",
    "What time does the enrollment dashboard refresh?",
]


# ------------------------------------------------------------------- build ---

def _guard():
    home = os.environ.get("STT_HOME")
    if not home:
        sys.exit("refused: set STT_HOME to a scratch home")
    h = Path(home).expanduser().resolve()
    # the real library: the checkout's own stt.env names it, else the
    # default transcripts folder. Read straight from the file: under
    # STT_HOME, config resolves every folder inside the scratch home.
    from stt import config, envfile
    repo = Path(__file__).resolve().parent.parent
    lib = envfile.parse(repo / "stt.env").get("STT_MEETINGS_DIR")
    real = [config.DEFAULT_MEETINGS_DIR, repo] + ([Path(lib).expanduser()] if lib else [])
    # demo_seed's compare folds letter case as APFS does: a home typed as
    # ~/projects/... passed this check while ~/Projects/... was refused
    tools = str(Path(__file__).resolve().parent)
    if tools not in sys.path:
        sys.path.append(tools)
    from demo_seed import _overlaps
    for r in real:
        r = r.resolve()
        if _overlaps(h, r):
            sys.exit(f"refused: STT_HOME overlaps a real folder ({r})")
    return h


def build(home: Path) -> dict:
    """Write the synthetic meetings into STT_HOME's meetings folder."""
    from stt import config
    md = config.meetings_dir()
    if md.exists() and any(md.iterdir()) and not (home / MARKER).exists():
        sys.exit(f"refused: {md} holds files and {home} has no {MARKER} marker")
    home.mkdir(parents=True, exist_ok=True)
    (home / MARKER).write_text("synthetic ask evaluation home\n")
    n = 0
    for i, (base, day, hour, ppl, count, facts) in enumerate(MEETINGS):
        at = {f[0]: f for f in facts}
        speakers = [{"id": f"SPEAKER_{k:02d}", "name": PEOPLE[p], "display": PEOPLE[p]}
                    for k, p in enumerate(ppl)]
        segs = []
        for li in range(count):
            if li in at:
                _, sp, text, _, _ = at[li]
                sid = f"SPEAKER_{ppl.index(sp):02d}" if sp in ppl else "SPEAKER_00"
            else:
                text = FILLER[(li * 7 + i * 3) % len(FILLER)]
                sid = f"SPEAKER_{li % len(ppl):02d}"
            disp = next(s["display"] for s in speakers if s["id"] == sid)
            segs.append({"start": li * STEP, "end": li * STEP + STEP - 1,
                         "speaker": sid, "display": disp, "text": text, "flags": []})
        d = {"meeting_id": f"eval{i:04d}", "source_file": f"{base}.m4a",
             "date": day, "recorded_at": f"{day}T{hour:02d}:00:00-04:00",
             "date_source": "typed", "route": "voice_memo",
             "title": base.rsplit(" ", 1)[0], "duration_sec": count * STEP,
             "speakers": speakers, "segments": segs, "words": []}
        folder = md / base
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{base}.json").write_text(json.dumps(d, indent=1))
        (folder / f"{base}.txt").write_text("\n".join(
            f"[{int(s['start']) // 60}:{int(s['start']) % 60:02d}] {s['display']}: {s['text']}"
            for s in segs))
        n += 1
    from stt import search
    search.rebuild()
    return {"meetings": n, "facts": sum(len(m[5]) for m in MEETINGS),
            "not_found": len(NOT_FOUND) + len(NEAR_MISS)}


def questions() -> list:
    out = []
    for base, _, _, _, _, facts in MEETINGS:
        for li, _, _, q, want in facts:
            out.append({"q": q, "base": base, "start": li * STEP, "want": want})
    out += [{"q": q, "base": None} for q in NOT_FOUND]
    out += [{"q": q, "base": None, "near_miss": True} for q in NEAR_MISS]
    return out


# ------------------------------------------------------------------ oracle ---

def oracle(prompt: str) -> str:
    """A stand-in for the model: the excerpt line sharing the most content
    words with the question, cited. NOT FOUND when that line shares under two
    words, misses more than one of the question's words, or holds under 75%
    of them (a near miss names a campus or a route the line does not)."""
    from stt import ask
    q = prompt.rsplit("QUESTION:", 1)[-1].strip()
    want = set(ask.terms(q))
    best, score = None, 0
    sid = None
    for line in prompt.split("EXCERPTS:", 1)[-1].split("QUESTION:", 1)[0].splitlines():
        m = re.match(r"^\[(S\d+)\]", line)
        if m:
            sid = m.group(1)
            continue
        words = set(re.findall(r"\w+", line.lower()))
        s = len(want & words)
        if sid and s > score:
            best, score = (sid, line.split(": ", 1)[-1]), s
    if not best or score < 2 or len(want) - score > 1 or score < 0.75 * len(want):
        return "NOT FOUND"
    return f"{best[1]} [{best[0]}]"


# -------------------------------------------------------------------- score ---

def evaluate(generate=None) -> dict:
    from stt import ask, config
    live = set(config.meeting_bases())
    rows, times = [], []
    for item in questions():
        t0 = time.monotonic()
        r = ask.ask(item["q"], generate=generate)
        dt = time.monotonic() - t0
        times.append(dt)
        row = {"q": item["q"], "seconds": round(dt, 1)}
        if item["base"] is None:
            row["kind"] = "nothing_found"
            row["near_miss"] = bool(item.get("near_miss"))
            row["declined"] = bool(r.get("nothing_found"))
            row["pass"] = row["declined"]
        else:
            row["kind"] = "known"
            ans = (r.get("answer") or "").lower()
            row["answered"] = item["want"].lower() in ans and not r.get("nothing_found")
            row["cited"] = any(c.get("base") == item["base"] and c["base"] in live
                               and abs(float(c.get("start") or 0) - item["start"]) <= CITE_SLACK
                               for c in r.get("citations") or [])
            row["pass"] = row["answered"] and row["cited"]
        rows.append(row)
    known = [r for r in rows if r["kind"] == "known"]
    nf = [r for r in rows if r["kind"] == "nothing_found"]
    rate = sum(r["pass"] for r in known) / len(known) if known else 0.0
    times.sort()
    p90 = times[min(len(times) - 1, int(round(0.9 * (len(times) - 1))))] if times else 0.0
    return {"known": len(known), "known_pass": sum(r["pass"] for r in known),
            "known_rate": round(rate, 3),
            "nothing_found": len(nf), "declined": sum(r["pass"] for r in nf),
            "near_miss": sum(r["near_miss"] for r in nf),
            "near_miss_declined": sum(r["pass"] for r in nf if r["near_miss"]),
            "p90_seconds": round(p90, 1),
            "gate": rate >= GATE and all(r["pass"] for r in nf),
            "rows": rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="write the synthetic store")
    ap.add_argument("--run", action="store_true", help="ask every question and score")
    ap.add_argument("--oracle", action="store_true",
                    help="use the extractive stand-in instead of the model")
    a = ap.parse_args(argv)
    home = _guard()
    if a.build:
        print(json.dumps(build(home)))
    if a.run:
        if not (home / MARKER).exists():
            sys.exit("refused: build the synthetic store first (--build)")
        r = evaluate(oracle if a.oracle else None)
        print(json.dumps(r, indent=2))
        return 0 if r["gate"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
