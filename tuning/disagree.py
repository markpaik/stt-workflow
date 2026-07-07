#!/usr/bin/env python
"""Cross-engine disagreement analysis: is a dual-model setup worth it?

Aligns Parakeet vs Whisper transcripts of the same audio (from the stt-stage
cache), splits tokens into agree/disagree regions, then asks which side Scribe
takes in each disagreement. Three numbers decide the architecture question:

  - agree%          — how much of the transcript needs no second opinion
  - agree-match%    — when engines agree, how often Scribe agrees too
                      (high => agreement is a reliable confidence signal)
  - sides-with      — in disagreements, who Scribe backs (parakeet/whisper/
                      neither/tie). "neither" is where a third opinion could
                      help; a lopsided split means "just prefer engine X".

Also prints the oracle WER: the error rate if an ideal arbiter always picked
the better engine per disagreement — the ceiling any dual/tri-model rig can hit.

Usage:  ./run.sh py tuning/disagree.py [--engines parakeet mlxwhisper_large-v3]
"""
import argparse
import difflib
import json
import sys
from pathlib import Path

from stt import config
from tuning import eval as E
from tuning.sweep import CACHE, find_refs


def _tokens(words):
    """Normalized token list + map back to raw indices (empty tokens dropped)."""
    toks = []
    for w in words:
        t = E._text_norm(w if isinstance(w, str) else w["word"])
        if t:
            toks.extend(t.split())
    return toks


def _match_mask(hyp_toks, ref_toks):
    """mask[i]=True where hyp token i sits in an 'equal' block vs the reference."""
    mask = [False] * len(hyp_toks)
    sm = difflib.SequenceMatcher(a=hyp_toks, b=ref_toks, autojunk=False)
    for op, a1, a2, _, _ in sm.get_opcodes():
        if op == "equal":
            for i in range(a1, a2):
                mask[i] = True
    return mask


def analyze(base, ref, tag_a, tag_b):
    fa, fb = CACHE / f"stt.{base}.{tag_a}.json", CACHE / f"stt.{base}.{tag_b}.json"
    if not (fa.exists() and fb.exists()):
        return None
    A = _tokens(json.loads(fa.read_text())["words"])
    B = _tokens(json.loads(fb.read_text())["words"])
    S = _tokens(ref["words"])
    a_ok, b_ok = _match_mask(A, S), _match_mask(B, S)

    stats = {"meeting": base, "agree_toks": 0, "agree_match": 0,
             "regions": 0, "sides_a": 0, "sides_b": 0, "neither": 0, "tie": 0,
             "oracle_bad": 0, "a_bad": 0, "b_bad": 0, "total_a": len(A)}
    sm = difflib.SequenceMatcher(a=A, b=B, autojunk=False)
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            stats["agree_toks"] += a2 - a1
            stats["agree_match"] += sum(a_ok[a1:a2])
            continue
        stats["regions"] += 1
        ka = sum(a_ok[a1:a2])          # tokens Scribe confirms on each side
        kb = sum(b_ok[b1:b2])
        na, nb = max(a2 - a1, 1), max(b2 - b1, 1)
        ra, rb = ka / na, kb / nb
        if ka == 0 and kb == 0:
            stats["neither"] += 1
        elif abs(ra - rb) < 1e-9:
            stats["tie"] += 1
        elif ra > rb:
            stats["sides_a"] += 1
        else:
            stats["sides_b"] += 1
        stats["a_bad"] += na - ka
        stats["b_bad"] += nb - kb
        stats["oracle_bad"] += min(na - ka, nb - kb)
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engines", nargs=2, default=["parakeet", "mlxwhisper_large-v3"])
    args = ap.parse_args()
    tag_a, tag_b = args.engines

    rows = []
    for base, ref, _ in find_refs():
        st = analyze(base, ref, tag_a, tag_b)
        if st is None:
            print(f"  skip {base}: missing cached transcripts (run sweep.py stt first)",
                  file=sys.stderr)
            continue
        rows.append(st)
        ag = st["agree_toks"] / max(st["total_a"], 1)
        am = st["agree_match"] / max(st["agree_toks"], 1)
        print(f"\n{base}")
        print(f"  engines agree on {ag:6.1%} of tokens; Scribe matches {am:6.1%} of those")
        print(f"  {st['regions']} disagreement regions: "
              f"Scribe sides {tag_a} {st['sides_a']}, {tag_b} {st['sides_b']}, "
              f"neither {st['neither']}, tie {st['tie']}")
        print(f"  disagreement tokens off-reference: {tag_a}={st['a_bad']} "
              f"{tag_b}={st['b_bad']} oracle-pick={st['oracle_bad']}")
    if not rows:
        return 1

    T = {k: sum(r[k] for r in rows) for k in rows[0] if k != "meeting"}
    ag = T["agree_toks"] / T["total_a"]
    am = T["agree_match"] / max(T["agree_toks"], 1)
    print("\n" + "=" * 72)
    print(f"OVERALL ({len(rows)} meetings, {tag_a} vs {tag_b})")
    print(f"  agreement: {ag:.1%} of tokens; {am:.1%} of agreed tokens match Scribe")
    print(f"  disagreements ({T['regions']}): {tag_a} {T['sides_a']} | "
          f"{tag_b} {T['sides_b']} | neither {T['neither']} | tie {T['tie']}")
    print(f"  err tokens in disagreements: {tag_a}={T['a_bad']}  {tag_b}={T['b_bad']}  "
          f"oracle={T['oracle_bad']}  "
          f"(oracle removes {1 - T['oracle_bad']/max(min(T['a_bad'],T['b_bad']),1):.0%} "
          f"of the better engine's disagreement errors)")
    (CACHE / "disagree_results.json").write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
