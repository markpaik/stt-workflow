#!/usr/bin/env python
"""Rebuild the voiceprint registry from meeting caches after registry loss.

For each meeting: map each NAMED speaker in the json to its diarization
cluster by word-time overlap against the cached raw turns, then enroll that
cluster's centroid embedding under the person's name. Read-only unless
--apply is passed.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stt import config, diarcache, identify  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--apply", action="store_true",
                help="actually enroll (default: dry-run, print the plan)")
APPLY = ap.parse_args().apply

for base in config.meeting_bases():
    j = json.loads(config.meeting_file(base, ".json").read_text())
    named = {s["id"]: s["name"] for s in j.get("speakers", []) if s.get("name")}
    if not named:
        continue
    emb_path = config.meeting_file(base, ".emb.npz")
    diar_path = config.meeting_file(base, ".diar.npz")
    if not emb_path.exists() or not diar_path.exists():
        print(f"{base}: MISSING caches — skipped")
        continue
    embs = dict(np.load(emb_path).items())
    raw_turns, _, _, _ = diarcache.load(diar_path)

    # cluster -> list of (start,end) spans
    spans = {}
    for t in raw_turns:
        spans.setdefault(t["cluster"], []).append((t["start"], t["end"]))

    def owner_overlap(word_times, cluster):
        s = 0.0
        for wm in word_times:
            for a, b in spans.get(cluster, []):
                if a <= wm <= b:
                    s += 1
                    break
        return s

    print(f"\n{base}:")
    for sid, name in named.items():
        mids = [(w["start"] + w["end"]) / 2 for w in j.get("words", [])
                if w.get("speaker") == sid]
        if not mids:
            print(f"  {name}: no words — skipped")
            continue
        scored = sorted(((owner_overlap(mids, c) / len(mids), c) for c in embs),
                        reverse=True)
        frac, cluster = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0.0
        ok = frac >= 0.6 and frac - runner >= 0.2
        print(f"  {name}: cluster={cluster} overlap={frac:.2f} "
              f"(runner-up {runner:.2f}) {'ENROLL' if ok else 'AMBIGUOUS — skipped'}")
        if ok and APPLY:
            identify.enroll(name, embs[cluster], source=base)

if APPLY:
    reg = identify.load_registry()
    print("\nRebuilt registry:")
    for n, m in sorted(reg.items()):
        print(f"  {n}: {m.get('n_samples')} sample(s), sources={m.get('sources')}")
