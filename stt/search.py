"""Full-text search across every meeting transcript.

At this scale (hundreds of meetings, a few MB of text) an in-memory scan beats
any index for simplicity: segment texts are cached per file mtime, and a query
is a case-insensitive scan returning hits with enough context to jump straight
to the moment in the transcript viewer.
"""
import json

from . import config

_cache = {}  # path -> (mtime, [(seg_index, start, who, text_lower, text)])


def _mtime(p):
    """Sort key that tolerates a meeting vanishing mid-request (concurrent
    rename/delete), mirroring _segments()'s own FileNotFoundError guard."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _segments(jpath):
    try:
        mtime = jpath.stat().st_mtime
    except FileNotFoundError:
        return []
    hit = _cache.get(str(jpath))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        d = json.loads(jpath.read_text())
        rows = [(i, s["start"],
                 s.get("display") or s.get("name") or s.get("speaker") or "?",
                 s.get("text", "").lower(), s.get("text", ""))
                for i, s in enumerate(d.get("segments", []))
                if s.get("text", "").strip()]
    except Exception:
        rows = []
    _cache[str(jpath)] = (mtime, rows)
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    return rows


def query(q: str, limit: int = 40) -> dict:
    """Case-insensitive substring search over all transcript segments.
    Returns hits newest-meeting-first with a highlighted snippet window."""
    q = (q or "").strip().lower()
    if len(q) < 3:
        return {"query": q, "hits": [], "total": 0}
    hits, total = [], 0
    files = sorted((config.meeting_file(b, ".json") for b in config.meeting_bases()),
                   key=_mtime, reverse=True)
    for jpath in files:
        base = jpath.stem
        for idx, start, who, low, text in _segments(jpath):
            pos = low.find(q)
            if pos < 0:
                continue
            total += 1
            if len(hits) < limit:
                a = max(0, pos - 60)
                b = min(len(text), pos + len(q) + 90)
                snippet = (("…" if a else "") + text[a:b] + ("…" if b < len(text) else ""))
                hits.append({"base": base, "index": idx, "start": start,
                             "who": who, "snippet": snippet})
    return {"query": q, "hits": hits, "total": total}
