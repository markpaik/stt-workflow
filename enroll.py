#!/usr/bin/env python
"""Build/update speaker voiceprints so recurring people get named automatically.

Recommended (enroll-from-transcript): after a meeting is processed, look at its
.txt, see which Speaker N is whom, and enroll from the saved embeddings sidecar:

  ./run.sh enroll --from-meeting "2026-05-21 Team Sync" --speaker SPEAKER_01 --name "Ana Ortiz"

Or from a short single-speaker clip:

  ./run.sh enroll --audio ~/samples/ana.m4a --name "Ana Ortiz"

List / inspect:

  ./run.sh enroll --list
  ./run.sh enroll --from-meeting "2026-05-21 Team Sync"   # lists that meeting's speakers

It refuses, and writes nothing, for a name the panel would refuse (empty,
over 200 characters, a line break or an invisible control, or a name like
U7 that an unknown voice holds), a speaker with less than 30 s of speech or
fewer than 10 clear turns in that meeting or clip, and a registry.json it
cannot read. A sample that looks like the wrong person needs --confirm.
"""
import argparse
import sys
from pathlib import Path

import numpy as np

from stt import config, identify, names, unknowns


def _meeting_embeddings(base: str) -> dict:
    p = config.meeting_file(base, ".emb.npz")
    if not p.exists():
        raise SystemExit(
            f"No embeddings sidecar at {p}.\n"
            "Process the meeting first (with diarization); it writes <base>.emb.npz."
        )
    data = np.load(p)
    return {k: data[k] for k in data.files}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-meeting", help="basename of a processed meeting (no extension)")
    ap.add_argument("--speaker", help="speaker label to enroll, e.g. SPEAKER_01")
    ap.add_argument("--audio", help="single-speaker audio clip to enroll from")
    ap.add_argument("--name", help="person's name to store")
    ap.add_argument("--replace", action="store_true", help="overwrite instead of averaging")
    ap.add_argument("--confirm", action="store_true",
                    help="enroll even when the sample looks like the wrong "
                         "person (low cosine to their existing samples, or a "
                         "better match to somebody else)")
    ap.add_argument("--list", action="store_true", help="list enrolled voiceprints")
    args = ap.parse_args()

    if args.list:
        reg = identify.load_registry()
        if not reg:
            print("No voiceprints enrolled yet.")
        for name, meta in reg.items():
            print(f"  {name}  (dim={meta['dim']}, samples={meta.get('n_samples', 1)})")
        return 0

    # List a meeting's speakers when --speaker/--name omitted
    if args.from_meeting and not (args.speaker and args.name):
        embs = _meeting_embeddings(args.from_meeting)
        print(f"Speakers with embeddings in '{args.from_meeting}': "
              + ", ".join(sorted(embs)))
        print("Re-run with --speaker <LABEL> --name \"<Person>\" to enroll one.")
        return 0

    if not args.name:
        raise SystemExit("--name is required")
    try:
        # '   ' was enrolled as '   .npy', and a name was never trimmed
        args.name = names.person_name(args.name)
    except names.BadName as e:
        raise SystemExit(f"--name: {e}")
    # identify.enroll refuses both of these and writes nothing, but here
    # they ended in a traceback, and an --audio clip was diarized first
    if identify.reserved_name(args.name):
        raise SystemExit(f"--name: {identify.RESERVED_ERROR}")
    try:
        reg = identify.load_registry_for_write()
    except identify.RegistryUnreadable as e:
        raise SystemExit(str(e))
    # 'jordan lee' for an enrolled 'Jordan Lee' is her: the check below and
    # every message name the stack the sample would join
    args.name = identify.canonical_name(args.name, reg)

    if args.from_meeting:
        embs = _meeting_embeddings(args.from_meeting)
        if args.speaker not in embs:
            raise SystemExit(f"{args.speaker} not found. Available: " + ", ".join(sorted(embs)))
        vec = embs[args.speaker]
        # quality floor (same bar as /api/name): a cluster with seconds of
        # speech can't identify anyone — measured from the relabel cache when
        # one exists (it always does for diarized meetings)
        dpath = config.meeting_file(args.from_meeting, ".diar.npz")
        if dpath.exists():
            from stt import diarcache
            try:
                raw_turns = diarcache.load(dpath)[0]
            except diarcache.Unreadable as e:
                raise SystemExit(f"'{args.from_meeting}': {e} -- Redo the "
                                 "meeting to rebuild it")
            veto = unknowns.floor_violation(
                unknowns.talk_stats(raw_turns).get(args.speaker))
            if veto:
                raise SystemExit(f"{args.speaker} in '{args.from_meeting}': {veto}")
    elif args.audio:
        from stt import audio as A, diarize
        # a mistyped path reached ffmpeg and ended in a traceback
        src = Path(args.audio).expanduser()
        if not src.is_file():
            raise SystemExit(f"audio file not found: {args.audio}")
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        wav = config.WORK_DIR / "enroll.wav"
        try:
            A.to_wav16k(src, wav)
        except (A.UnreadableAudio, A.WriteFailed) as e:
            wav.unlink(missing_ok=True)
            raise SystemExit(str(e))
        try:
            diar = diarize.diarize(wav)
        finally:
            wav.unlink(missing_ok=True)
        embs = diar["embeddings"]
        if not embs:
            raise SystemExit("No speaker embedding was produced from that clip.")
        # embs is keyed by raw diarization cluster label, so tally talk time by
        # the same raw label (raw_turns), not turns' post-refine speaker name,
        # which is the enrolled NAME once a cluster matches a voiceprint.
        stats = unknowns.talk_stats(diar["raw_turns"])
        label = max(embs, key=lambda l: stats.get(l, {}).get("talk_secs", 0.0))
        # the quality floor --from-meeting and /api/name apply: a 0.3 s clip
        # was enrolled, and a voiceprint that thin identifies nobody
        veto = unknowns.floor_violation(
            stats.get(label, {"talk_secs": 0.0, "reliable_turns": 0}))
        if veto:
            raise SystemExit(f"{args.audio}: {veto} Use a longer clip of one speaker.")
        vec = embs[label]
    else:
        raise SystemExit("Provide --from-meeting (+--speaker) or --audio")

    # identify.enroll raises a plain ValueError for a zero or NaN embedding,
    # and np.vstack for a sample whose size differs from the person's stored
    # samples. Both ended in a traceback here, and a size mismatch first
    # asked for --confirm, which then crashed.
    vec = np.asarray(vec, dtype=float).reshape(-1)
    who = (f"{args.speaker} in '{args.from_meeting}'" if args.from_meeting
           else args.audio)
    if not np.isfinite(vec).all() or not np.linalg.norm(vec):
        hint = " Redo the meeting to rebuild it." if args.from_meeting else ""
        raise SystemExit(f"{who}: the voice embedding is empty or damaged (zero "
                         f"or not a number), so it cannot identify anyone.{hint}")
    have = (reg.get(args.name) or {}).get("dim")
    if have and int(have) != vec.size and not args.replace:
        raise SystemExit(
            f"{who}: this sample has {vec.size} dimensions and {args.name}'s "
            f"voiceprint has {have}, so they come from different voice models. "
            "Re-run with --replace to start the voiceprint over from this sample.")
    others = {int(m["dim"]) for n, m in reg.items()
              if n != args.name and isinstance(m, dict) and m.get("dim")}
    if others and vec.size not in others:
        print(f"Note: this sample has {vec.size} dimensions and the other "
              f"voiceprints have {', '.join(map(str, sorted(others)))}. It matches "
              "only meetings embedded by the same voice model.", file=sys.stderr)

    # a suspect sample joining an EXISTING stack needs an explicit --confirm:
    # a low cosine to the person's own samples (or a better match to somebody
    # else entirely) is how a wrong-cluster enrollment poisons a profile
    if not (args.replace or args.confirm):
        w = identify.sample_check(args.name, vec)
        if w:
            msg = (f"This sample scores {w['own']:.2f} against {args.name}'s "
                   f"existing samples")
            if w["cross_name"] is not None and w["cross"] > w["own"]:
                msg += f" and {w['cross']:.2f} against {w['cross_name']}"
            raise SystemExit(msg + " — it may be the wrong person. "
                                   "Re-run with --confirm to enroll anyway.")

    source = args.from_meeting or (Path(args.audio).stem if args.audio else None)
    try:
        path = identify.enroll(args.name, vec, replace=args.replace, source=source)
    except identify.RegistryUnreadable as e:   # damaged since the check above
        raise SystemExit(str(e))
    except ValueError as e:   # any other refusal: one line, nothing written
        raise SystemExit(f"Cannot enroll '{args.name}': {e}")
    print(f"Enrolled '{args.name}' -> {path}")
    return 0


if __name__ == "__main__":
    config.private_umask()
    sys.exit(main())
