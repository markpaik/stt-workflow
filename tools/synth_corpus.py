#!/usr/bin/env python3
"""Deterministic synthetic-conversation corpus with EXACT speaker ground truth,
plus the sandboxed STT_HOME stages that enroll its voices and run the REAL
pipeline over it.

The corpus is the third ground-truth source for tools/diar_eval.py: five
scripted conversations (2-4 speakers each) rendered with clearly distinct
macOS `say` voices, one track per speaker, mixed with ffmpeg into the 16 kHz
mono WAVs the pipeline accepts. Because WE placed every turn on the timeline,
the truth (who spoke, exactly when) is known to the millisecond — including
sub-second fillers ("Yeah", "Right"), rapid alternation, and two conversations
with deliberate OVERLAP segments.

Determinism: the scripts, voices, rates and gaps are fixed constants below;
`say` renders byte-identically for fixed (voice, rate, text) on one machine
(verified), so the whole corpus regenerates to identical files. The manifest
records the script fingerprint AND the rendered WAV hashes; if a macOS voice
update ever breaks byte-stability, the script fingerprint + turn durations
remain the determinism contract (tests assert those, not raw bytes).

Safety: generation only ever writes inside its --corpus target (refused if it
overlaps a real data folder, marker-gated like tools/demo_seed.py). The
enroll/process/rescore stages REFUSE to run unless STT_HOME points at a
sandbox home built by build_home() — the real registry and real meetings are
unreachable from them by construction.

Stages (run by tools/diar_eval.py in a child process with STT_HOME set):
    --stage generate  build/refresh the corpus (no STT_HOME needed)
    --stage enroll    enroll the six synthetic voices in the SANDBOX registry
    --stage process   full pipeline (ASR + diarize + refine + merge) per file
    --stage rescore   cache-only relabel of the sandbox meetings (fast path
                      for attribution-constant experiments)
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Run straight from a checkout without installation.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import numpy as np
import soundfile as sf

SAMPLE_RATE = 16000
MARKER = ".synth_corpus"      # corpus dirs we built (safe to wipe)
HOME_MARKER = ".eval_home"    # sandbox homes we built (stages demand it)
TRIM_THRESHOLD = 0.004        # |sample| above this counts as speech (say has ~0 lead)
TRACK_GAIN = 0.5              # per-track scale so two overlapped voices can't clip
DEFAULT_GAP = 0.35            # seconds between turns unless the script says otherwise
TAIL_SEC = 0.7                # silence after the last turn

# --- the cast: synthetic names -> clearly distinct built-in voices ----------
# (US female, GB male, AU female, IE female, IN male, ZA female)
CAST = {
    "Ava Sterling": "Samantha",
    "Ben Whitfield": "Daniel",
    "Cora Nash": "Karen",
    "Dana Ferris": "Moira",
    "Eli Prakash": "Rishi",
    "Faye Linden": "Tessa",
}

# Enrollment monologues (~20s each): what the sandbox voiceprints are built from.
ENROLL_LINES = {
    "Ava Sterling":
        "Hello, this is a voice enrollment sample. I like to walk in the park "
        "early in the morning when the paths are still quiet. On weekends I "
        "usually bake bread and read by the window. My favorite season is "
        "autumn because the light turns gold in the afternoon. I always keep "
        "a notebook nearby to write down small ideas before they escape.",
    "Ben Whitfield":
        "Good afternoon, this is a voice enrollment sample. I spend most "
        "evenings repairing old radios in the garage and listening to long "
        "wave stations. In summer I cycle along the canal to the old mill and "
        "back. I prefer tea to coffee, strong and without sugar. A good map "
        "and a compass still beat any application, in my honest opinion.",
    "Cora Nash":
        "Hi there, this is a voice enrollment sample. I grow tomatoes and "
        "basil on the balcony and give most of them away to the neighbours. "
        "Every Thursday I swim thirty laps at the local pool before work. I "
        "collect postcards from little towns nobody has heard of. If it "
        "rains on a Sunday, I consider that a perfect excuse to do nothing.",
    "Dana Ferris":
        "Hello again, this is a voice enrollment sample. I play the fiddle "
        "badly but with tremendous enthusiasm on Friday nights. My bookshelf "
        "is sorted by colour, which drives my sister absolutely mad. I once "
        "walked the entire coast road in a single summer. Porridge with "
        "honey is the only sensible way to begin a morning, I find.",
    "Eli Prakash":
        "Good morning, this is a voice enrollment sample. I teach chess at "
        "the community centre every second Saturday of the month. I am "
        "slowly learning to identify birds by their calls alone. My desk "
        "always has three pens, one notebook, and far too many cables. Long "
        "train journeys are, to me, the best place in the world to think.",
    "Faye Linden":
        "Hello, this is a voice enrollment sample. I run a small pottery "
        "studio and fire the kiln twice a week, weather permitting. My dog "
        "insists on the same walking route every single evening. I make a "
        "point of trying one new recipe every weekend, with mixed results. "
        "The sea is a short drive away, and I visit it far more than I admit.",
}

# --- the scripts -------------------------------------------------------------
# Turn = (speaker, text [, gap [, rate]]).
#   gap:  seconds between the end of the LATEST previous speech and this turn's
#         start; NEGATIVE = deliberate overlap (this voice starts while the
#         previous one is still talking).
#   rate: `say -r` words-per-minute override (fillers get boosted so the corpus
#         has genuinely sub-half-second turns).
# Content is synthetic and neutral on purpose (no real people, no district
# business): gardening, a book club, a hiking trip, cooking, and a quiz night.
CONVERSATIONS = [
    {
        "name": "garden_planning",
        "speakers": ["Ava Sterling", "Ben Whitfield"],
        "turns": [
            ("Ava Sterling", "So I sketched out the garden beds last night, and I think we can fit four raised beds along the south fence if we keep the paths narrow."),
            ("Ben Whitfield", "Four seems ambitious. The far corner barely gets any sun after three in the afternoon."),
            ("Ava Sterling", "Right, but that corner would be the leafy greens bed. Lettuce actually bolts less with a bit of shade."),
            ("Ben Whitfield", "Fair point."),
            ("Ava Sterling", "The tomatoes and peppers take the two middle beds, and the fourth one is herbs and strawberries."),
            ("Ben Whitfield", "What about the soil? The old beds were basically clay with delusions of grandeur."),
            ("Ava Sterling", "Yeah.", 0.2, 240),
            ("Ava Sterling", "We'd need to bring in compost. I priced it out, and a cubic yard delivered is cheaper than the bagged stuff once you need more than ten bags."),
            ("Ben Whitfield", "How much do we need in total?"),
            ("Ava Sterling", "About two and a half yards if we fill all four beds to eight inches."),
            ("Ben Whitfield", "Okay.", 0.2, 240),
            ("Ben Whitfield", "And the watering? I refuse to stand out there with a hose every evening like last summer."),
            ("Ava Sterling", "Drip lines on a timer. One circuit per bed, and we can adjust the flow per line."),
            ("Ben Whitfield", "That I can support. What's the damage for the whole setup?"),
            ("Ava Sterling", "Timber, soil, compost, drip kit and the timer, somewhere around six hundred if we do the assembly ourselves."),
            ("Ben Whitfield", "Hmm. That's less than I feared, honestly."),
            ("Ava Sterling", "And it amortizes. The beds should last ten years if we seal the timber properly."),
            ("Ben Whitfield", "Right.", 0.2, 240),
            ("Ben Whitfield", "When would we actually build them? I'm away the first two weekends of the month."),
            ("Ava Sterling", "The weekend after you're back. Two days, one for assembly and one for soil and planting."),
            ("Ben Whitfield", "Deal. But I want it on record that I predicted the squirrels will win in the end."),
            ("Ava Sterling", "Noted. I'll get netting for the strawberries, and we can revisit the squirrel treaty in July."),
            ("Ben Whitfield", "One more thing. Do we keep the old compost bin, or start fresh with the new beds?"),
            ("Ava Sterling", "Keep it. It's ugly but it works, and moving it would upset the worms."),
            ("Ben Whitfield", "The worms have a lobby now."),
            ("Ava Sterling", "They've always had a lobby. What about seed starting? The kitchen windowsill was a disaster last year."),
            ("Ben Whitfield", "Yep.", 0.2, 300),
            ("Ben Whitfield", "A small shelf with a grow light in the garage. Two trays, nothing fancy."),
            ("Ava Sterling", "Two trays covers the tomatoes and the peppers, with room left for basil."),
            ("Ben Whitfield", "Then we're agreed. I'll order the light this week."),
            ("Ava Sterling", "Sure.", 0.15, 320),
        ],
    },
    {
        "name": "book_club",
        "speakers": ["Cora Nash", "Dana Ferris", "Eli Prakash"],
        "turns": [
            ("Cora Nash", "Alright, chapter twelve. I want to start with the lighthouse scene, because I think it changes how you read everything before it."),
            ("Dana Ferris", "Oh, completely. The keeper knew the whole time. You can see it in how he never asks her name."),
            ("Eli Prakash", "See, I read that differently. I think he suspects, but the letter in chapter nine is what confirms it for him."),
            ("Cora Nash", "The letter he burns?"),
            ("Eli Prakash", "Yes.", 0.15, 240),
            ("Eli Prakash", "He burns it before reading the second page. You only burn a letter like that if the first page already told you everything."),
            ("Dana Ferris", "Hmm, maybe."),
            ("Cora Nash", "I love that we're three people with three different books, apparently."),
            ("Dana Ferris", "That's the mark of a good one, though. My sister read it and thought it was a straightforward ghost story."),
            ("Eli Prakash", "It is partly a ghost story. The harbor scenes are doing double duty, literal and not."),
            ("Cora Nash", "Right, the fog is practically a character."),
            ("Dana Ferris", "True.", 0.15, 240),
            ("Cora Nash", "Can we talk about the ending? Because I have grievances."),
            ("Eli Prakash", "Air them."),
            ("Cora Nash", "It's abrupt. Two hundred pages of slow tide, and then the resolution happens in four paragraphs while she's making tea."),
            ("Dana Ferris", "I actually loved that. The whole book argues that the big moments happen inside ordinary ones. Ending it over tea is the thesis."),
            ("Eli Prakash", "Agreed.", 0.15, 240),
            ("Eli Prakash", "If it ended with a storm, it would betray itself. The quiet ending is the honest one."),
            ("Cora Nash", "Fine, outvoted. But I maintain the epilogue was unnecessary."),
            ("Dana Ferris", "Oh, the epilogue is indefensible, we're with you there."),
            ("Eli Prakash", "Unanimous."),
            ("Cora Nash", "Next month is the mountain memoir. Dana, you're picking the discussion questions."),
            ("Dana Ferris", "Already drafted. Question one has a footnote."),
            ("Eli Prakash", "Of course it does."),
            ("Dana Ferris", "Before we close, ratings. Out of ten."),
            ("Eli Prakash", "Eight. The middle dragged but the last third earned it."),
            ("Cora Nash", "Seven and a half, and the half is purely for the fog."),
            ("Dana Ferris", "Nine from me, and I'll defend it."),
            ("Cora Nash", "No.", 0.15, 300),
            ("Cora Nash", "You defend everything with a lighthouse in it."),
            ("Dana Ferris", "That is a fair and hurtful observation."),
            ("Eli Prakash", "So the average puts it in our top three for the year."),
            ("Dana Ferris", "Top two."),
            ("Cora Nash", "Top three."),
            ("Eli Prakash", "We'll vote in December like civilized people."),
        ],
    },
    {
        "name": "trail_trip",
        "speakers": ["Ava Sterling", "Cora Nash", "Ben Whitfield", "Faye Linden"],
        "turns": [
            ("Ava Sterling", "Okay, trip logistics. The forecast for Saturday is clear until about four, then a chance of showers on the ridge."),
            ("Cora Nash", "So we start early. If we're on the trail by seven we're off the exposed section before two."),
            ("Ben Whitfield", "Seven at the trailhead means leaving town at six. I'll say it now: I will not be a pleasant person at six."),
            ("Faye Linden", "You're not a pleasant person at ten, so nothing is lost."),
            ("Cora Nash", "Which route are we committing to? The north loop is longer but the creek crossing on the south one might be high."),
            ("Ava Sterling", "I called the ranger station yesterday. They said the crossing is passable but you'll get wet feet."),
            ("Ben Whitfield", "Wet feet, no. North loop, yes."),
            ("Faye Linden", "Agreed.", 0.15, 240),
            ("Cora Nash", "North loop it is. That's fourteen miles, about three thousand feet of gain."),
            ("Faye Linden", "Who's carrying the stove? I have the filter and the first aid kit already."),
            ("Ava Sterling", "I'll take the stove and fuel. Ben, you're on food because last time you outsourced it to a vending machine."),
            ("Ben Whitfield", "One time."),
            ("Cora Nash", "It was twice."),
            ("Ben Whitfield", "Fine, twice. I'll do a proper shop on Thursday. Any dietary vetoes?"),
            ("Faye Linden", "No cilantro."),
            ("Ava Sterling", "Nothing with peanuts for me."),
            ("Ben Whitfield", "Noted.", 0.2, 240),
            ("Cora Nash", "Car situation: one vehicle or two? The trailhead lot fills up on Saturdays."),
            ("Ava Sterling", "One. I'll drive, my rack fits all four packs."),
            ("Faye Linden", "And if the weather turns early, what's the bail option?"),
            ("Cora Nash", "There's a junction at mile nine that drops back to the road in two miles. We pass it around noon, so that's the decision point."),
            ("Ben Whitfield", "Sensible. And dinner after? The diner by the highway does that enormous pie."),
            ("Faye Linden", "The pie is the actual reason I agreed to this trip."),
            ("Ava Sterling", "Then it's settled. Six o'clock pickup, north loop, decision at mile nine, pie regardless of outcome."),
            ("Cora Nash", "Pie regardless of outcome should be on a t-shirt."),
            ("Faye Linden", "Last thing, headlamps. If the showers slow us down we could be finishing in the dark."),
            ("Ben Whitfield", "Mine's dead. The battery hatch is held on with tape and optimism."),
            ("Cora Nash", "I have a spare you can borrow."),
            ("Ben Whitfield", "Yep.", 0.2, 300),
            ("Ava Sterling", "Then we're set. I'll send the packing list tonight so nobody improvises."),
            ("Faye Linden", "No promises."),
            ("Cora Nash", "She means you, Ben."),
            ("Ben Whitfield", "I know exactly who she means."),
        ],
    },
    {
        # OVERLAP conversation 1: backchannels and interruptions land ON TOP of
        # the other speaker's turns (negative gaps). Also the stereo-validation
        # source: Dana is the "mic owner" in the L/R render.
        "name": "recipe_swap",
        "speakers": ["Dana Ferris", "Eli Prakash"],
        "turns": [
            ("Dana Ferris", "So the trick with the flatbread is that the pan has to be properly hot before the first one goes in, hotter than feels reasonable."),
            ("Eli Prakash", "Mm, okay.", -1.2, 240),
            ("Dana Ferris", "If it puffs within thirty seconds, you're at the right temperature. If it just sits there looking sad, wait another minute."),
            ("Eli Prakash", "Mine always come out like roof tiles, so clearly I've been at roof-tile temperature."),
            ("Dana Ferris", "Yeah.", -0.8, 240),
            ("Eli Prakash", "And you don't use any yeast at all? Just the yogurt?"),
            ("Dana Ferris", "Just yogurt, flour, and a little baking powder. The yogurt does the heavy lifting."),
            ("Eli Prakash", "Right.", -0.6, 240),
            ("Dana Ferris", "Rest the dough for twenty minutes, no more. Past half an hour it gets sticky and you'll hate me."),
            ("Eli Prakash", "Twenty minutes. And rolling thickness?"),
            ("Eli Prakash", "Mm.", -0.6, 320),
            ("Dana Ferris", "Coin thick. Thinner and they crisp, thicker and they stay doughy in the middle."),
            ("Eli Prakash", "My grandmother used to slap them between her hands instead of rolling. I've never once managed it without throwing dough on the floor."),
            ("Dana Ferris", "Ha, the slapping takes years. Rolling pin, no shame."),
            ("Eli Prakash", "What do you brush them with at the end? Yours had something green on them."),
            ("Dana Ferris", "Melted butter with garlic and chopped chives. Do it the second they come off the pan."),
            ("Eli Prakash", "Okay, trading you my lime pickle recipe for this. Fair warning, it takes three weeks and a sunny windowsill."),
            ("Dana Ferris", "Three weeks?", -0.5),
            ("Eli Prakash", "The lime has to soften in the salt before anything else happens. Rushing it is the classic mistake."),
            ("Dana Ferris", "Alright, patience it is. Send me the quantities and I'll start a jar on Sunday."),
            ("Eli Prakash", "Done. And I want a full report on the first batch of roof tiles."),
            ("Dana Ferris", "First batch is always roof tiles. It's the law."),
        ],
    },
    {
        # OVERLAP conversation 2: quiz night — rapid alternation, people
        # talking over the question master, sub-second answers.
        "name": "trivia_night",
        "speakers": ["Ben Whitfield", "Faye Linden", "Ava Sterling"],
        "turns": [
            ("Ben Whitfield", "Round three, geography. Question one: which river flows through five capital cities, more than any other?"),
            ("Faye Linden", "The Danube.", 0.15),
            ("Ava Sterling", "Danube.", -0.4, 240),
            ("Ben Whitfield", "Correct, the Danube. Vienna, Bratislava, Budapest, Belgrade, and I'll accept four of those."),
            ("Faye Linden", "You just said five."),
            ("Ben Whitfield", "The card says five, my knowledge says four, we move on. Question two: what is the only country that borders both the Atlantic and the Indian Ocean on the African mainland?"),
            ("Ava Sterling", "South Africa."),
            ("Faye Linden", "Yeah.", -0.5, 340),
            ("Ben Whitfield", "Correct again. You two are insufferable when you're winning."),
            ("Ava Sterling", "We're always winning, so."),
            ("Ben Whitfield", "Question three, and this one is worth two points: name the largest lake entirely within one country."),
            ("Faye Linden", "Entirely within one, so not the Caspian."),
            ("Ava Sterling", "Lake Michigan?", 0.15),
            ("Faye Linden", "No, wait.", -0.7, 240),
            ("Faye Linden", "Michigan is the answer people always say. I think it's actually one of the Russian ones."),
            ("Ava Sterling", "Baikal is the deepest, not the largest."),
            ("Faye Linden", "Then I'll say Michigan after all."),
            ("Ben Whitfield", "Final answer, Michigan?"),
            ("Ava Sterling", "Yes.", 0.15, 240),
            ("Faye Linden", "Sure.", -0.3, 240),
            ("Ben Whitfield", "Correct, two points. Lake Michigan, the only Great Lake entirely inside one country."),
            ("Faye Linden", "See, the trick is to doubt yourself back to your first answer."),
            ("Ava Sterling", "That's not a trick, that's just anxiety with extra steps."),
            ("Ben Whitfield", "Last question of the round: which mountain range separates Europe from Asia, at least according to the people who draw the lines?"),
            ("Faye Linden", "The Urals."),
            ("Ava Sterling", "Urals.", -0.4, 240),
            ("Ben Whitfield", "The Urals. That's the round, and yes, you're still ahead."),
            ("Faye Linden", "Yep.", -0.3, 300),
            ("Ava Sterling", "We know."),
            ("Ben Whitfield", "Insufferable, both of you. Refill round before the picture round, back in five."),
        ],
    },
]

STEREO_VALIDATION = {"conversation": "recipe_swap", "mic_speaker": "Dana Ferris",
                     "bleed": 0.10}


# ------------------------------------------------------------- fingerprint --

def script_fingerprint() -> str:
    """Deterministic hash of everything that DEFINES the corpus (cast, scripts,
    layout constants). Two checkouts with the same scripts agree on this even
    if `say` renders ever change."""
    blob = json.dumps({"cast": CAST, "conversations": CONVERSATIONS,
                       "enroll": ENROLL_LINES, "stereo": STEREO_VALIDATION,
                       "sr": SAMPLE_RATE, "gap": DEFAULT_GAP, "tail": TAIL_SEC,
                       "trim": TRIM_THRESHOLD},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _norm_turn(turn):
    """(speaker, text[, gap[, rate]]) -> (speaker, text, gap, rate)."""
    speaker, text = turn[0], turn[1]
    gap = turn[2] if len(turn) > 2 else DEFAULT_GAP
    rate = turn[3] if len(turn) > 3 else None
    return speaker, text, float(gap), rate


# ------------------------------------------------------------------ layout --

def layout_turns(items):
    """items: [(speaker, dur, gap)] -> truth turns [{speaker,start,end}].
    start = max(0, cursor + gap) where cursor is the end of the latest speech
    so far; a NEGATIVE gap makes this turn overlap the previous one. Pure —
    unit-testable with fake durations."""
    turns, cursor = [], 0.0
    for speaker, dur, gap in items:
        start = max(0.0, cursor + gap)
        end = start + dur
        turns.append({"speaker": speaker, "start": round(start, 3),
                      "end": round(end, 3), "_start_exact": start})
        cursor = max(cursor, end)
    return turns


def truth_overlaps(turns):
    """[[s,e]] where two truth turns overlap (>50 ms)."""
    out = []
    for i, a in enumerate(turns):
        for b in turns[i + 1:]:
            s = max(a["start"], b["start"])
            e = min(a["end"], b["end"])
            if e - s > 0.05:
                out.append([round(s, 3), round(e, 3)])
    return sorted(out)


# --------------------------------------------------------------- rendering --

def _render_say(voice: str, text: str, rate, out_wav: Path):
    """Render one utterance to 16 kHz mono WAV via macOS `say` (byte-stable
    for fixed inputs on one machine)."""
    cmd = ["say", "-v", voice]
    if rate:
        cmd += ["-r", str(int(rate))]
    cmd += ["-o", str(out_wav), f"--data-format=LEI16@{SAMPLE_RATE}", text]
    subprocess.run(cmd, check=True, capture_output=True)


def _load_trimmed(path: Path) -> np.ndarray:
    """Load a rendered clip and trim leading/trailing silence so truth spans
    measure SPEECH, not `say`'s padding."""
    x, sr = sf.read(str(path), dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SAMPLE_RATE:
        raise RuntimeError(f"{path}: expected {SAMPLE_RATE} Hz, got {sr}")
    idx = np.where(np.abs(x) > TRIM_THRESHOLD)[0]
    if len(idx) == 0:
        return x
    return x[idx[0]:idx[-1] + 1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wav(path: Path, x: np.ndarray, channels=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), x.astype(np.float32), SAMPLE_RATE,
             subtype="PCM_16")


def _mix_tracks(track_paths, out_path: Path):
    """ffmpeg amix (normalize=0: our tracks are pre-scaled) -> 16k mono PCM."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for p in track_paths:
        cmd += ["-i", str(p)]
    n = len(track_paths)
    cmd += ["-filter_complex", f"amix=inputs={n}:duration=longest:normalize=0",
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)


def _build_conversation(spec: dict, corpus: Path, tmp: Path) -> dict:
    """Render one conversation: per-turn clips -> per-speaker tracks -> ffmpeg
    mix. Returns its manifest entry (file, truth turns, overlaps, hashes)."""
    clips = []
    for i, turn in enumerate(spec["turns"]):
        speaker, text, gap, rate = _norm_turn(turn)
        raw = tmp / f"{spec['name']}.{i:03d}.wav"
        _render_say(CAST[speaker], text, rate, raw)
        clips.append((speaker, text, gap, _load_trimmed(raw)))

    items = [(spk, len(x) / SAMPLE_RATE, gap) for spk, _, gap, x in clips]
    turns = layout_turns(items)
    total = max(t["end"] for t in turns) + TAIL_SEC
    n_samples = int(round(total * SAMPLE_RATE))

    tracks = {spk: np.zeros(n_samples) for spk in spec["speakers"]}
    for (spk, _, _, x), t in zip(clips, turns):
        at = int(round(t["_start_exact"] * SAMPLE_RATE))
        tracks[spk][at:at + len(x)] += x * TRACK_GAIN

    track_dir = corpus / "tracks" / spec["name"]
    track_paths = []
    for spk in spec["speakers"]:
        p = track_dir / f"{spk}.wav"
        _write_wav(p, tracks[spk])
        track_paths.append(p)

    out = corpus / f"{spec['name']}.wav"
    _mix_tracks(track_paths, out)

    truth = [{"speaker": t["speaker"], "start": t["start"], "end": t["end"],
              "text": clips[i][1]}
             for i, t in enumerate(turns)]
    return {"name": spec["name"], "file": out.name,
            "duration": round(total, 3), "speakers": spec["speakers"],
            "turns": truth, "overlaps": truth_overlaps(turns),
            "wav_sha256": _sha256(out)}


def _build_stereo_validation(corpus: Path) -> dict:
    """Stereo render of one conversation: L = mic owner's track (+ a controlled
    bleed of the others), R = everyone else — the recorder's me/them layout
    with KNOWN truth, used to validate the channel-energy labeler."""
    conv = STEREO_VALIDATION["conversation"]
    owner = STEREO_VALIDATION["mic_speaker"]
    bleed = STEREO_VALIDATION["bleed"]
    track_dir = corpus / "tracks" / conv
    own, _ = sf.read(str(track_dir / f"{owner}.wav"), dtype="float64")
    others = np.zeros_like(own)
    for p in sorted(track_dir.glob("*.wav")):
        if p.stem != owner:
            x, _ = sf.read(str(p), dtype="float64")
            others[:len(x)] += x
    left = own + bleed * others
    out = corpus / f"{conv}.stereo.wav"
    _write_wav(out, np.stack([left, others], axis=1))
    return {"file": out.name, "conversation": conv, "mic_speaker": owner,
            "bleed": bleed, "wav_sha256": _sha256(out)}


def generate(corpus: Path) -> dict:
    """Build the whole corpus under `corpus` and return the manifest."""
    import tempfile
    corpus.mkdir(parents=True, exist_ok=True)
    (corpus / MARKER).write_text(
        "Synthetic eval corpus built by tools/synth_corpus.py. Safe to delete.\n")
    entries = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for spec in CONVERSATIONS:
            entries.append(_build_conversation(spec, corpus, tmp))
        enroll = {}
        for name, text in ENROLL_LINES.items():
            raw = tmp / f"enroll.{name}.wav"
            _render_say(CAST[name], text, None, raw)
            x = _load_trimmed(raw)
            p = corpus / "enroll" / f"{name}.wav"
            _write_wav(p, x)
            enroll[name] = str(p.relative_to(corpus))
    stereo = _build_stereo_validation(corpus)
    manifest = {"script_sha": script_fingerprint(), "sample_rate": SAMPLE_RATE,
                "cast": CAST, "conversations": entries, "enroll": enroll,
                "stereo_validation": stereo,
                "total_speech_sec": round(sum(
                    t["end"] - t["start"] for e in entries for t in e["turns"]), 1),
                "total_audio_sec": round(sum(e["duration"] for e in entries), 1)}
    (corpus / "manifest.json").write_text(json.dumps(manifest, indent=2,
                                                     ensure_ascii=False))
    return manifest


def load_manifest(corpus: Path):
    p = corpus / "manifest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def ensure_corpus(corpus: Path) -> dict:
    """Return a manifest for an up-to-date corpus, regenerating when the
    scripts changed or files are missing. Never regenerates needlessly — the
    WAV hashes are how runs stay comparable across experiments."""
    m = load_manifest(corpus)
    if m and m.get("script_sha") == script_fingerprint() and all(
            (corpus / e["file"]).exists() for e in m["conversations"]) and all(
            (corpus / f).exists() for f in m["enroll"].values()):
        return m
    _refuse_real_dirs(corpus)
    if corpus.exists() and any(corpus.iterdir()) and not (corpus / MARKER).exists():
        sys.exit(f"refusing to rebuild {corpus}: it has no {MARKER} marker, so "
                 "it was not built by this tool.")
    if corpus.exists():
        shutil.rmtree(corpus)
    return generate(corpus)


# ------------------------------------------------------- sandbox + stages ---

def _refuse_real_dirs(target: Path):
    """Never create eval state inside (or above) a real data folder — same
    rails as tools/demo_seed.py."""
    sys.path.insert(0, str(REPO / "tools"))
    import demo_seed
    target = target.expanduser().resolve()
    for real in demo_seed._real_data_dirs():
        if demo_seed._overlaps(target, real):
            sys.exit(f"refusing: {target} overlaps a real configured data "
                     f"folder ({real}).")


def build_home(home: Path) -> Path:
    """A self-contained STT_HOME for the eval pipeline runs: empty registry,
    empty meetings store, its own stt.env. Marker-gated so the stages can
    verify they're pointed at a sandbox this tool built."""
    _refuse_real_dirs(home)
    for sub in ("source", "meetings", "recordings", "voiceprints", "work", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    env = home / "stt.env"
    if not env.exists():
        # punctuation off: irrelevant to attribution, skips an ONNX model load
        env.write_text("# synthetic eval home — built by tools/synth_corpus.py\n"
                       "STT_PUNCTUATE=0\n")
    (home / HOME_MARKER).write_text(
        "Synthetic eval STT_HOME built by tools/synth_corpus.py. Safe to delete.\n")
    return home


def _assert_sandboxed():
    """The enroll/process/rescore stages mutate registries and meeting stores —
    hard-verify every config path is inside a marker-carrying STT_HOME before
    touching anything."""
    from stt import config
    raw = os.environ.get("STT_HOME")
    if not raw:
        sys.exit("refusing: this stage must run with STT_HOME set to a sandbox home")
    home = Path(raw).expanduser().resolve()
    if not (home / HOME_MARKER).exists():
        sys.exit(f"refusing: {home} has no {HOME_MARKER} marker — not an eval sandbox")
    for label, p in (("PROJECT_DIR", config.PROJECT_DIR),
                     ("VOICEPRINTS_DIR", config.VOICEPRINTS_DIR),
                     ("MEETINGS_DIR", config.MEETINGS_DIR),
                     ("RECORDINGS_DIR", config.RECORDINGS_DIR),
                     ("ICLOUD_DIR", config.ICLOUD_DIR)):
        rp = Path(p).expanduser().resolve()
        if not (rp == home or rp.is_relative_to(home)):
            sys.exit(f"refusing: config.{label} = {rp} escapes the sandbox {home} "
                     "(is a STT_<X>_DIR env var overriding STT_HOME?)")
    return home


def stage_enroll(corpus: Path):
    """Enroll each synthetic voice in the SANDBOX registry from its
    enrollment monologue (same embedder the pipeline matches with)."""
    _assert_sandboxed()
    from stt import diarize, identify
    manifest = load_manifest(corpus) or sys.exit(f"no manifest under {corpus}")
    reg = identify.load_registry()
    for name, rel in manifest["enroll"].items():
        if name in reg:
            continue
        wav = corpus / rel
        dur = sf.info(str(wav)).duration
        embs = diarize.embed_spans(wav, [(0.2, dur - 0.2)])
        if not embs or embs[0] is None:
            sys.exit(f"enrollment embedding failed for {name}")
        identify.enroll(name, embs[0], source="synth_corpus")
        print(f"  enrolled {name}")


def stage_process(corpus: Path, force=False):
    """Run the REAL full pipeline (ASR -> diarize -> refine -> merge) on every
    corpus conversation, into the sandbox meetings store. Writes
    eval_processed.json (conversation -> meeting base) for the scorer."""
    home = _assert_sandboxed()
    stage_enroll(corpus)
    from stt import config, pipeline
    manifest = load_manifest(corpus) or sys.exit(f"no manifest under {corpus}")
    processed_path = home / "eval_processed.json"
    processed = {}
    if processed_path.exists() and not force:
        try:
            processed = json.loads(processed_path.read_text())
        except json.JSONDecodeError:
            processed = {}
    for entry in manifest["conversations"]:
        src = corpus / entry["file"]
        base = processed.get(entry["name"])
        if base and config.meeting_file(base, ".json").exists() and not force:
            print(f"  {entry['name']}: already processed as '{base}'")
            continue
        print(f"  processing {entry['name']} "
              f"({entry['duration']:.0f}s) ...", flush=True)
        res = pipeline.process_file(src, do_verify=False)
        processed[entry["name"]] = res["base"]
        processed_path.write_text(json.dumps(processed, indent=2))
    print("  done:", json.dumps(processed))


def stage_rescore():
    """Cache-only re-attribution of the sandbox meetings (relabel machinery):
    picks up attribution-constant changes in seconds, no ASR/diarization."""
    _assert_sandboxed()
    import relabel
    for base in relabel.all_bases():
        relabel.relabel_one(base)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", required=True,
                    choices=["generate", "enroll", "process", "rescore"])
    ap.add_argument("--corpus", required=True, help="corpus directory")
    ap.add_argument("--force", action="store_true",
                    help="process: redo conversations that already have output")
    args = ap.parse_args(argv)
    corpus = Path(args.corpus).expanduser().resolve()
    if args.stage == "generate":
        _refuse_real_dirs(corpus)
        m = ensure_corpus(corpus)
        print(f"corpus ready at {corpus}: {len(m['conversations'])} conversations, "
              f"{m['total_audio_sec']:.0f}s audio (script {m['script_sha']})")
    elif args.stage == "enroll":
        stage_enroll(corpus)
    elif args.stage == "process":
        stage_process(corpus, force=args.force)
    elif args.stage == "rescore":
        stage_rescore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
