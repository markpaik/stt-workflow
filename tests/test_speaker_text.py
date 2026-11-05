"""stt/speaker_text.py: the description re-renders when the speakers change,
with no model call.

The first test is the proof the design rests on: across a naming (a
voiceprint match), a person rename, and a meeting-local name, the roster
`id`, `global_id`, `name` and `display` all change, and the only key that
holds is the diarizer's cluster id in the .diar.npz cache. Every name is
synthetic."""
import json

import numpy as np
import pytest

from stt import config, diarcache, identify, review, speaker_text, summarize, unknowns
from conftest import mfile


def _unit(v):
    return v / np.linalg.norm(v)


def _seed(base="Sync 10012026"):
    """Two talk-rich clusters (12 turns of 3 s each), orthogonal voices."""
    rng = np.random.default_rng(11)
    a = _unit(rng.normal(size=256))
    b = rng.normal(size=256)
    b = _unit(b - (b @ a) * a)
    raw, words, tembs = [], [], []
    for i in range(24):
        c = "SPEAKER_00" if i % 2 == 0 else "SPEAKER_01"
        raw.append({"start": 3.0 * i, "end": 3.0 * i + 3.0, "cluster": c})
        tembs.append(a if c == "SPEAKER_00" else b)
        words.append({"start": 3.0 * i + 0.5, "end": 3.0 * i + 1.0, "word": f"w{i}"})
    data = {"meeting_id": "mid-sync", "source_file": f"{base}.m4a",
            "duration_sec": 72.0, "strict": False, "date": "2026-10-01",
            "speakers": [], "segments": [], "words": words}
    mfile(base, ".json").write_text(json.dumps(data))
    mfile(base, ".txt").write_text("stub")
    diarcache.save(mfile(base, ".diar.npz"), raw, tembs,
                   {"SPEAKER_00": a, "SPEAKER_01": b})
    return base, a, b


def _load(base):
    return json.loads(config.meeting_file(base, ".json").read_text())


def _roster(d):
    return {s["id"]: s for s in d["speakers"]}


def _by_cluster(d, base):
    labels = speaker_text.cluster_labels(speaker_text.load_raw_turns(base), d["segments"])
    return {c: _roster(d)[rid] for c, rid in labels.items()}


def _describe(base):
    """Store a description written against the roster as it stands now."""
    d = _load(base)
    disp = {c: s["display"] for c, s in _by_cluster(d, base).items()}
    a, b = disp["SPEAKER_00"], disp["SPEAKER_01"]
    desc = {"tmpl": speaker_text.make(
        {"gist": f"Pricing settled after {b} pushed back.",
         "decisions": [f"{a} owns the vendor shortlist."],
         "open_questions": [f"Can {b} check the budget before {a} signs?"]},
        d, speaker_text.load_raw_turns(base)),
        "title": "Vendor Pricing", "tags": ["budget-finance"], "series": "one_off"}
    summarize.store_description(base, desc)


def test_only_the_cluster_id_is_stable_across_naming_rename_and_local_names(sandbox, monkeypatch):
    import relabel
    monkeypatch.setattr(config, "PUNCTUATE", False)
    base, a, b = _seed()
    assert relabel.relabel_one(base)
    d0 = _load(base)
    c0 = _by_cluster(d0, base)
    # both clusters are unnamed and registered as unknowns
    assert c0["SPEAKER_00"]["id"] == "SPEAKER_00" and c0["SPEAKER_00"]["name"] is None
    gid_b = c0["SPEAKER_01"]["global_id"]
    assert gid_b

    # a naming: the voiceprint matches, and the roster id BECOMES the name
    identify.enroll("Alice Chen", a, source="Elsewhere")
    assert relabel.relabel_one(base)
    d1 = _load(base)
    c1 = _by_cluster(d1, base)
    assert "SPEAKER_00" not in _roster(d1)
    assert c1["SPEAKER_00"]["id"] == "Alice Chen"

    # a person rename: the id moves again
    assert identify.rename_person("Alice Chen", "Alice Wong")
    assert relabel.relabel_one(base)
    c2 = _by_cluster(_load(base), base)
    assert c2["SPEAKER_00"]["id"] == "Alice Wong"

    # a meeting-local name: global_id goes to None, name and display change
    assert summarize.set_local_name(base, "SPEAKER_01", "Jordan Lee")["ok"]
    assert relabel.relabel_one(base)
    c3 = _by_cluster(_load(base), base)
    assert c3["SPEAKER_01"]["global_id"] is None and gid_b is not None
    assert c3["SPEAKER_01"]["display"] == "Jordan Lee"

    # the cluster ids never moved: the cache is the same file throughout
    assert set(c0) == set(c1) == set(c2) == set(c3) == {"SPEAKER_00", "SPEAKER_01"}


def test_the_description_follows_a_naming_and_a_rename_with_no_model_call(sandbox, monkeypatch):
    import relabel
    monkeypatch.setattr(config, "PUNCTUATE", False)
    monkeypatch.setattr(summarize, "_generate",
                        lambda *a, **k: pytest.fail("no model call on a speaker change"))
    base, a, b = _seed()
    assert relabel.relabel_one(base)
    _describe(base)
    d = _load(base)
    old_a = _by_cluster(d, base)["SPEAKER_00"]["display"]
    assert d["ai_decisions"][0] == f"{old_a} owns the vendor shortlist."

    identify.enroll("Alice Chen", a, source="Elsewhere")
    assert relabel.relabel_one(base)
    d = _load(base)
    assert d["ai_decisions"] == ["Alice Chen owns the vendor shortlist."]
    assert "Alice Chen owns the vendor shortlist." in d["ai_summary"]

    assert identify.rename_person("Alice Chen", "Alice Wong")
    assert relabel.relabel_one(base)
    d = _load(base)
    assert d["ai_decisions"] == ["Alice Wong owns the vendor shortlist."]

    assert summarize.set_local_name(base, "SPEAKER_01", "Jordan Lee")["ok"]
    assert relabel.relabel_one(base)
    d = _load(base)
    assert d["ai_gist"] == "Pricing settled after Jordan Lee pushed back."
    assert d["ai_open_questions"] == ["Can Jordan Lee check the budget before Alice Wong signs?"]


def test_a_registry_merge_renumbers_the_unknown_and_the_text_follows(sandbox, monkeypatch):
    import relabel
    monkeypatch.setattr(config, "PUNCTUATE", False)
    base, a, b = _seed()
    assert relabel.relabel_one(base)
    _describe(base)
    d = _load(base)
    sb = _by_cluster(d, base)["SPEAKER_01"]
    # fold this meeting's unknown into a freshly minted one: its display changes
    reg = unknowns.load()
    other = unknowns.mint_uid(reg)
    reg["speakers"][other] = dict(reg["speakers"][sb["global_id"]])
    unknowns.save(reg)
    assert unknowns.merge(sb["global_id"], other)
    assert relabel.relabel_one(base)
    d2 = _load(base)
    nb = _by_cluster(d2, base)["SPEAKER_01"]
    assert nb["display"] != sb["display"]
    assert d2["ai_gist"] == f"Pricing settled after {nb['display']} pushed back."


def test_a_description_from_before_templates_is_templated_once_on_relabel(sandbox, monkeypatch):
    import relabel
    monkeypatch.setattr(config, "PUNCTUATE", False)
    base, a, b = _seed()
    assert relabel.relabel_one(base)
    d = _load(base)
    old_a = _by_cluster(d, base)["SPEAKER_00"]["display"]
    d["ai_summary"] = f"{old_a} walked through the budget."
    config.meeting_file(base, ".json").write_text(json.dumps(d))
    identify.enroll("Alice Chen", a, source="Elsewhere")
    assert relabel.relabel_one(base)
    d = _load(base)
    assert d["ai_summary"] == "Alice Chen walked through the budget."
    assert d["ai_tmpl"]["v"] == speaker_text.TMPL_VERSION


def test_a_stale_description_is_never_re_rendered(sandbox):
    d = {"ai_stale": True, "ai_summary": "Speaker 1 spoke.",
         "speakers": [{"id": "SPEAKER_00", "name": "Alice Chen", "display": "Alice Chen"}]}
    before = {"speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 1"}],
              "segments": []}
    assert speaker_text.rerender(d, before=before) is False
    assert d["ai_summary"] == "Speaker 1 spoke."


def test_templatize_matches_whole_labels_longest_first_and_unique_first_names():
    d = {"speakers": [{"id": "S1", "name": None, "display": "Speaker 1"},
                      {"id": "S12", "name": None, "display": "Speaker 12"},
                      {"id": "Dana Fox", "name": "Dana Fox", "display": "Dana Fox"},
                      {"id": "Robin Ash", "name": "Robin Ash", "display": "Robin Ash"},
                      {"id": "Robin Cole", "name": "Robin Cole", "display": "Robin Cole"}],
         "segments": []}
    labels = speaker_text.labels_for(d)
    t = speaker_text.templatize(
        "Speaker 12 and Speaker 1 met Dana Fox; Dana agreed, Robin did not. Danaher stays.",
        d, labels)
    assert t.count("⟦s:S12⟧") == 1 and t.count("⟦s:S1⟧") == 1
    assert "⟦f:Dana Fox⟧ agreed" in t
    assert "Robin did not" in t            # two Robins: ambiguous, left as written
    assert "Danaher stays" in t
    # a first name never matches inside a token that holds a full name
    d2 = {"speakers": [{"id": "Jordan Lee", "name": "Jordan Lee", "display": "Jordan Lee"},
                       {"id": "Lee Smith", "name": "Lee Smith", "display": "Lee Smith"}],
          "segments": []}
    t2 = speaker_text.templatize("Jordan Lee met Lee.", d2, speaker_text.labels_for(d2))
    assert t2 == "⟦s:Jordan Lee⟧ met ⟦f:Lee Smith⟧."


def test_a_review_edit_keeps_the_description_rendered(sandbox):
    base = "Edit 10012026"
    d = {"source_file": f"{base}.m4a", "duration_sec": 5.0, "strict": False,
         "speakers": [{"id": "SPEAKER_00", "name": "Alice Chen", "display": "Alice Chen"}],
         "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "name": "Alice Chen",
                       "display": "Alice Chen", "text": "hello", "flags": [], "overlap": False}],
         "words": []}
    d["ai_tmpl"] = speaker_text.make({"gist": "Alice Chen opened."}, d)
    d.update(speaker_text.render(d["ai_tmpl"], d))
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    r = review.apply(base, 0, "edit", start=0.0, text="hello there")
    assert r["ok"]
    after = _load(base)
    assert after["ai_gist"] == "Alice Chen opened."
    assert after["ai_summary"] == "Alice Chen opened."
