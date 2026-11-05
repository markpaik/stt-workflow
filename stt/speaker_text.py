"""Keep a meeting's description in step with its speaker names, with no
model call.

The description (gist, decisions, open questions) names people
as the transcript labeled them when the model read it: "Speaker 3",
"Jordan Lee". When a voice is named, renamed, merged, or relabeled later,
that text goes stale. This module stores the description once as a
template, with each speaker name replaced by a token, and renders it again
from the current roster whenever the speakers change.

Which key is stable. A roster entry's `id` is NOT: once a voiceprint
matches, refine labels the cluster's turns with the person's NAME, so
"SPEAKER_01" becomes "Jordan Lee", and a rename changes it again.
`global_id` is not either: naming sets it to None and a registry merge
changes it. `name` and `display` are what change. The one key that holds
across a relabel, a naming, a rename, a merge, a meeting-local name and a
registry change is the diarizer's own CLUSTER id in the .diar.npz cache.
Only a Redo rewrites that cache, and a Redo marks the description out of
date instead (pipeline._title_fields). tests/test_speaker_text.py proves
this through relabel_one.

A token names a cluster: "⟦s:SPEAKER_01⟧" for the full label and
"⟦f:SPEAKER_01⟧" for a first name the model used alone. At render
time a cluster resolves to the roster label its turns carry now (the
largest time overlap of its raw turns with the current segments), and that
label resolves to the roster entry's display. A meeting with no diar cache
keys tokens by roster id instead.

The template lives in the meeting json as `ai_tmpl`:
  {"v": 1, "fields": {...}, "names": {cluster: display then},
   "labels": {cluster: roster id last seen}}
`fields` holds the text with tokens: str or list of str per field.
"""
import re

from . import config

TMPL_VERSION = 1
_TOK = re.compile("⟦([sf]):([^⟧]{1,80})⟧")
_GENERIC = re.compile(r"^(speaker|voice)\s+\d+$", re.I)
# the description fields a template may hold, and the json key each renders
# to. A stored template with a field not listed here (an old "next_steps")
# renders nothing for it.
FIELD_KEYS = {"gist": "ai_gist", "decisions": "ai_decisions",
              "open_questions": "ai_open_questions",
              "summary": "ai_summary"}


def _tok(kind: str, key: str) -> str:
    return f"⟦{kind}:{key}⟧"


def roster(data: dict) -> dict:
    """{roster id: display} for the meeting's speakers, in roster order."""
    out = {}
    for s in data.get("speakers") or []:
        if isinstance(s, dict) and s.get("id") is not None:
            out[str(s["id"])] = str(s.get("display") or s.get("name") or s["id"])
    return out


def _named(data: dict) -> set:
    return {str(s["id"]) for s in data.get("speakers") or []
            if isinstance(s, dict) and s.get("name")}


def cluster_labels(raw_turns, segments) -> dict:
    """{cluster: roster id} by the largest time overlap of each cluster's
    raw turns with the segments' speakers. A cluster that overlaps no
    segment is left out."""
    segs = sorted(((float(s.get("start") or 0), float(s.get("end") or 0),
                    str(s.get("speaker"))) for s in segments or []
                   if s.get("speaker") is not None), key=lambda x: x[0])
    turns = sorted(((float(t["start"]), float(t["end"]), str(t["cluster"]))
                    for t in raw_turns or [] if t.get("cluster") is not None),
                   key=lambda x: x[0])
    acc = {}
    j0 = 0
    for ts, te, c in turns:
        while j0 < len(segs) and segs[j0][1] <= ts:
            j0 += 1
        j = j0
        while j < len(segs) and segs[j][0] < te:
            ov = min(te, segs[j][1]) - max(ts, segs[j][0])
            if ov > 0:
                acc.setdefault(c, {})
                acc[c][segs[j][2]] = acc[c].get(segs[j][2], 0.0) + ov
            j += 1
    return {c: max(v.items(), key=lambda kv: (kv[1], kv[0]))[0] for c, v in acc.items()}


def load_raw_turns(base: str):
    """The cached raw turns of a meeting, or None without a diar cache."""
    p = config.meeting_file(base, ".diar.npz")
    if not p.exists():
        return None
    try:
        from . import diarcache
        return diarcache.load(p)[0]
    except Exception:
        return None


def labels_for(data: dict, raw_turns=None) -> dict:
    """{key: roster id}: clusters when the raw turns are known, else each
    roster id keyed by itself. Every roster id gets a key, so a manual
    speaker or an id no cluster carries still templates."""
    out = cluster_labels(raw_turns, data.get("segments")) if raw_turns else {}
    have = set(out.values())
    for rid in roster(data):
        if rid not in have:
            out[rid] = rid
    return out


def _first(display: str) -> str:
    return (display.split() or [""])[0]


def templatize(text: str, data: dict, labels: dict) -> str:
    """`text` with every speaker label of `data`'s roster replaced by a
    token. Full labels first (longest first, so 'Speaker 12' wins over
    'Speaker 1'), then first names that only one named speaker carries."""
    if not text:
        return text
    disp = roster(data)
    named = _named(data)
    # one key per roster id: the first cluster that carries it (a split
    # voice is two clusters under one label, and either one resolves)
    key_of = {}
    for k, rid in labels.items():
        key_of.setdefault(rid, k)
    pats = []
    for rid, d in disp.items():
        k = key_of.get(rid)
        if k is None or not d.strip():
            continue
        pats.append((d, _tok("s", k)))
    firsts = {}
    for rid in named:
        d = disp.get(rid, "")
        f = _first(d)
        if len(f) >= 3 and f != d and f[0].isupper():
            firsts.setdefault(f, set()).add(rid)
    full_firsts = {_first(d) for rid, d in disp.items() if rid not in named}
    for f, rids in firsts.items():
        # a first name two speakers share, or one an unnamed label starts
        # with ('Speaker'), is ambiguous: it stays as written
        if len(rids) == 1 and f not in full_firsts:
            k = key_of.get(next(iter(rids)))
            if k is not None:
                pats.append((f, _tok("f", k)))
    if not pats:
        return text
    pats.sort(key=lambda p: -len(p[0]))
    table = dict(pats)
    # ONE pass over the text with every label in one alternation, longest
    # first: a token written by an earlier label is never scanned again, so
    # a first name can never match inside a token that holds a full name
    rx = re.compile(r"(?<!\w)(" + "|".join(re.escape(p[0]) for p in pats) + r")(?!\w)")
    return rx.sub(lambda m: table[m.group(1)], text)


def make(fields: dict, data: dict, raw_turns=None) -> dict:
    """The template of a description. `fields` maps a FIELD_KEYS name to a
    string or a list of strings, written against `data`'s roster."""
    labels = labels_for(data, raw_turns)
    disp = roster(data)
    tf = {}
    for k, v in fields.items():
        if isinstance(v, list):
            tf[k] = [templatize(str(x), data, labels) for x in v]
        elif v is not None:
            tf[k] = templatize(str(v), data, labels)
    used = set()
    for v in tf.values():
        for s in (v if isinstance(v, list) else [v]):
            used.update(m.group(2) for m in _TOK.finditer(s))
    return {"v": TMPL_VERSION, "fields": tf,
            "names": {k: disp.get(labels.get(k, k), k) for k in sorted(used)},
            "labels": {k: labels.get(k, k) for k in sorted(used)}}


def _render_str(s: str, tmpl: dict, data: dict, labels_now: dict) -> str:
    disp = roster(data)
    named = _named(data)

    def sub(m):
        kind, key = m.group(1), m.group(2)
        rid = labels_now.get(key) or (tmpl.get("labels") or {}).get(key) or key
        d = disp.get(rid)
        if d is None:
            d = (tmpl.get("names") or {}).get(key, key)
            return _first(d) if kind == "f" and not _GENERIC.match(d) else d
        if kind == "f" and rid in named and not _GENERIC.match(d):
            return _first(d)
        return d
    return _TOK.sub(sub, s)


def render(tmpl: dict, data: dict, labels_now: dict | None = None) -> dict:
    """{json key: rendered value} for every field of the template."""
    labels_now = labels_now if labels_now is not None else dict(tmpl.get("labels") or {})
    out = {}
    for k, v in (tmpl.get("fields") or {}).items():
        jk = FIELD_KEYS.get(k)
        if not jk:
            continue
        if isinstance(v, list):
            out[jk] = [_render_str(x, tmpl, data, labels_now) for x in v]
        else:
            out[jk] = _render_str(v, tmpl, data, labels_now)
    if "gist" in (tmpl.get("fields") or {}):
        out["ai_summary"] = compose_summary(out.get("ai_gist"), out.get("ai_decisions"),
                                            out.get("ai_open_questions"))
    return out


def compose_summary(gist, decisions=None, open_questions=None) -> str:
    """ai_summary for readers that know one text field: the gist, then the
    decisions and the open questions as short lists."""
    parts = [str(gist or "").strip()]
    if decisions:
        parts.append("Decisions:\n" + "\n".join(f"- {d}" for d in decisions))
    if open_questions:
        parts.append("Open questions:\n" + "\n".join(f"- {q}" for q in open_questions))
    return "\n\n".join(p for p in parts if p)


def legacy_fields(data: dict) -> dict:
    """The template fields of a description written before templates: the
    prose summary. (Next steps were removed, so they are never templated.)"""
    return {"summary": str(data["ai_summary"])} if data.get("ai_summary") else {}


def rerender(data: dict, raw_turns=None, before: dict | None = None) -> bool:
    """Re-render `data`'s description from its template against its current
    roster, in place. Returns True when a description field changed.

    `raw_turns` (the diar cache's turns) maps each cluster to the label it
    carries now. Without them the labels last seen are used, which is right
    for an edit that changed no speaker label. `before` is the meeting json
    as it stood before the speakers changed: a description from before
    templates is templated against that roster first, once.

    A description marked out of date (`ai_stale`, after a Redo) is left
    alone: its names belong to clusters the Redo replaced."""
    if data.get("ai_stale"):
        return False
    tmpl = data.get("ai_tmpl")
    if not isinstance(tmpl, dict) or tmpl.get("v") != TMPL_VERSION:
        if before is None:
            return False
        fields = legacy_fields(data)
        if not fields:
            return False
        tmpl = make(fields, before, raw_turns)
    labels_now = (labels_for(data, raw_turns) if raw_turns
                  else dict(tmpl.get("labels") or {}))
    # a key whose cluster no longer overlaps any segment keeps its last label
    for k, rid in (tmpl.get("labels") or {}).items():
        labels_now.setdefault(k, rid)
    new = render(tmpl, data, labels_now)
    tmpl = {**tmpl, "labels": {k: labels_now.get(k, v)
                               for k, v in (tmpl.get("labels") or {}).items()}}
    changed = any(data.get(k) != v for k, v in new.items())
    data.update(new)
    data["ai_tmpl"] = tmpl
    return changed
