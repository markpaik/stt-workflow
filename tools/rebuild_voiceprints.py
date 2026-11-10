#!/usr/bin/env python
"""Rebuild the voiceprint registry from meeting caches after registry loss.

For each meeting: map each NAMED speaker in the json to its diarization
cluster by word-time overlap against the cached raw turns, then enroll that
cluster's centroid embedding under the person's name. Read-only unless
--apply is passed.

Two kinds of named speaker are never enrolled, the same as /api/name and
enroll.py: a meeting-only label (`local: true` in the roster: a transcript
label, never a voiceprint), and a cluster below the quality floor
(unknowns.floor_violation, pooled across its unnamed split twins).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stt import config, diarcache, identify, unknowns  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--apply", action="store_true",
                help="actually enroll (default: dry-run, print the plan)")
APPLY = ap.parse_args().apply


def move_damaged_registry():
    """A damaged registry.json is never written over (identify.
    RegistryUnreadable), so a rebuild sets it aside first, under a name
    that keeps its bytes. Returns the new path, or None."""
    import time
    p = config.VOICEPRINTS_DIR / "registry.json"
    try:
        identify.load_registry_for_write()
        return None
    except identify.RegistryUnreadable:
        dst = p.with_suffix(f".json.corrupt-{int(time.time())}")
        p.rename(dst)
        return dst


if APPLY:
    moved = move_damaged_registry()
    if moved:
        print(f"registry.json could not be read: moved aside to {moved.name}")

for base in config.meeting_bases():
    j = json.loads(config.meeting_file(base, ".json").read_text())
    roster = j.get("speakers", [])
    named = {s["id"]: s["name"] for s in roster if s.get("name") and not s.get("local")}
    local = [s["name"] for s in roster if s.get("name") and s.get("local")]
    if not named:
        if local:
            print(f"\n{base}:")
            for name in local:
                print(f"  {name}: meeting-only label — skipped")
        continue
    emb_path = config.meeting_file(base, ".emb.npz")
    diar_path = config.meeting_file(base, ".diar.npz")
    if not emb_path.exists() or not diar_path.exists():
        print(f"{base}: MISSING caches — skipped")
        continue
    embs = dict(np.load(emb_path).items())
    try:
        raw_turns, _, cent_emb, _ = diarcache.load(diar_path)
    except (OSError, ValueError) as e:
        print(f"{base}: diarization cache unreadable ({e}) — skipped")
        continue
    stats = unknowns.talk_stats(raw_turns)
    unnamed = [s["id"] for s in roster if not s.get("name")]

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
    for name in local:
        print(f"  {name}: meeting-only label — skipped")
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
        verdict = "ENROLL" if ok else "AMBIGUOUS — skipped"
        if ok:
            veto = unknowns.floor_violation(unknowns.pooled_stats(
                cluster, cent_emb, stats, unnamed))
            if veto:
                ok, verdict = False, f"BELOW THE FLOOR — skipped ({veto})"
        print(f"  {name}: cluster={cluster} overlap={frac:.2f} "
              f"(runner-up {runner:.2f}) {verdict}")
        if ok and APPLY:
            identify.enroll(name, embs[cluster], source=base)

if APPLY:
    reg = identify.load_registry()
    print("\nRebuilt registry:")
    for n, m in sorted(reg.items()):
        print(f"  {n}: {m.get('n_samples')} sample(s), sources={m.get('sources')}")
