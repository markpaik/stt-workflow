#!/usr/bin/env python
"""Build/update speaker voiceprints so recurring people get named automatically.

Recommended (enroll-from-transcript): after a meeting is processed, look at its
.txt, see which Speaker N is whom, and enroll from the saved embeddings sidecar:

  ./run.sh enroll --from-meeting "LT Meeting 05212026" --speaker SPEAKER_01 --name "Mark Paik"

Or from a short single-speaker clip:

  ./run.sh enroll --audio ~/samples/mark.m4a --name "Mark Paik"

List / inspect:

  ./run.sh enroll --list
  ./run.sh enroll --from-meeting "LT Meeting 05212026"   # lists that meeting's speakers
"""
import argparse
import sys
from pathlib import Path

import numpy as np

from stt import config, identify, unknowns


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
            raw_turns = diarcache.load(dpath)[0]
            veto = unknowns.floor_violation(
                unknowns.talk_stats(raw_turns).get(args.speaker))
            if veto:
                raise SystemExit(f"{args.speaker} in '{args.from_meeting}': {veto}")
    elif args.audio:
        from stt import audio as A, diarize
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        wav = config.WORK_DIR / "enroll.wav"
        A.to_wav16k(Path(args.audio), wav)
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
        talk = {}
        for t in diar["raw_turns"]:
            talk[t["cluster"]] = talk.get(t["cluster"], 0.0) + (t["end"] - t["start"])
        vec = embs[max(embs, key=lambda l: talk.get(l, 0.0))]
    else:
        raise SystemExit("Provide --from-meeting (+--speaker) or --audio")

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
    path = identify.enroll(args.name, vec, replace=args.replace, source=source)
    print(f"Enrolled '{args.name}' -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
