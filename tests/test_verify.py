"""verify: cross-engine disagreement detection — alignment, filler tolerance,
region merging, segment flagging, and the sidecar that survives relabels."""
import json

from stt import config, verify
from conftest import mfile


def _w(items):
    """[(start, end, word)] -> word dicts."""
    return [{"start": s, "end": e, "word": w} for s, e, w in items]


PRIMARY = _w([(0.0, 0.4, "We"), (0.4, 0.8, "reviewed"), (0.8, 1.2, "the"),
              (1.2, 1.8, "dashboard"), (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])


def test_agreeing_streams_produce_no_regions():
    assert verify.regions(PRIMARY, PRIMARY) == []


def test_substitution_region_with_correct_span():
    secondary = _w([(0.0, 0.4, "We"), (0.4, 0.8, "renewed"), (0.8, 1.2, "the"),
                    (1.2, 1.8, "dashboard"), (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    regs = verify.regions(PRIMARY, secondary)
    assert len(regs) == 1
    r = regs[0]
    assert r["ours"] == "reviewed" and r["theirs"] == "renewed"
    assert r["start"] == 0.4 and r["end"] == 0.8  # primary word timing


def test_filler_only_difference_is_ignored():
    secondary = _w([(0.0, 0.4, "We"), (0.35, 0.4, "um"), (0.4, 0.8, "reviewed"),
                    (0.8, 1.2, "the"), (1.2, 1.8, "dashboard"),
                    (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    assert verify.regions(PRIMARY, secondary) == []


def test_insertion_anchors_between_neighbors():
    """Second engine heard an extra word the primary missed entirely."""
    secondary = _w([(0.0, 0.4, "We"), (0.4, 0.8, "reviewed"), (0.8, 1.2, "the"),
                    (1.2, 1.8, "dashboard"), (2.0, 2.4, "carefully"),
                    (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    regs = verify.regions(PRIMARY, secondary)
    assert len(regs) == 1
    r = regs[0]
    assert r["theirs"] == "carefully" and r["ours"] == ""
    assert 1.8 <= r["start"] <= r["end"] <= 5.0  # sits in the gap


def test_adjacent_regions_merge():
    secondary = _w([(0.0, 0.4, "He"), (0.4, 0.8, "renewed"), (0.8, 1.2, "the"),
                    (1.2, 1.8, "dashboard"), (5.0, 5.4, "numbers"), (5.4, 5.9, "today.")])
    regs = verify.regions(PRIMARY, secondary)
    assert len(regs) == 1  # "he/we" + "renewed/reviewed" 0s apart -> one item
    assert "we" in regs[0]["ours"] and "reviewed" in regs[0]["ours"]


def test_apply_flags_attaches_alt_and_skips_reviewed():
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "We reviewed the dashboard", "flags": []},
        {"start": 5.0, "end": 6.0, "speaker": "SPEAKER_01", "text": "numbers today.", "flags": [],
         "reviewed": "accepted"},
    ]
    regs = [{"start": 0.4, "end": 0.8, "ours": "reviewed", "theirs": "renewed"},
            {"start": 5.1, "end": 5.3, "ours": "numbers", "theirs": "number"}]
    n = verify.apply_flags(segments, regs)
    assert n == 1
    assert verify.FLAG in segments[0]["flags"]
    assert segments[0]["alt"][0]["theirs"] == "renewed"
    assert "alt" not in segments[1] and segments[1]["flags"] == []  # reviewed: untouched


def test_apply_flags_catches_zero_width_pure_insertion_region():
    """A zero-width disagreement region (a pure insertion with no inter-word
    gap, start==end) sitting squarely inside a segment computes overlap of
    EXACTLY 0 against it — a strict > 0 test let it escape flagging
    entirely, defeating the "flag every disagreement" guarantee."""
    segments = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00",
                "text": "We reviewed the dashboard", "flags": []}]
    regs = [{"start": 1.0, "end": 1.0, "ours": "", "theirs": "carefully"}]
    n = verify.apply_flags(segments, regs)
    assert n == 1
    assert verify.FLAG in segments[0]["flags"]
    assert segments[0]["alt"][0]["theirs"] == "carefully"


def test_apply_flags_still_excludes_regions_genuinely_outside_a_segment():
    """The >= 0 fix must not make apply_flags match everything — a region
    entirely outside a segment's span still correctly computes a NEGATIVE
    overlap and is excluded."""
    segments = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00",
                "text": "We reviewed the dashboard", "flags": []}]
    regs = [{"start": 10.0, "end": 10.0, "ours": "", "theirs": "unrelated"}]
    n = verify.apply_flags(segments, regs)
    assert n == 0
    assert segments[0]["flags"] == []


CLOUD = "elevenlabs/scribe_v2"     # a cloud transcript: Parakeet is its second opinion


def test_the_second_opinion_is_a_local_engine_that_did_not_write_the_transcript():
    """Whisper was the second engine for a Parakeet transcript. With
    Parakeet the only local engine, a Parakeet transcript has none, and a
    cloud transcript still gets Parakeet. A cloud engine is never the
    second opinion: the pass would upload the whole meeting."""
    assert verify.LOCAL_ENGINES == ("parakeet",)
    for own in ("mlx-community/parakeet-tdt-0.6b-v3", "parakeet", config.PARAKEET_MODEL):
        assert verify.secondary_engine(own) is None, own
    for own in (CLOUD, "cloud:scribe", "openai/whisper-1", "mistral/voxtral-mini-latest"):
        assert verify.secondary_engine(own) == "parakeet", own


def test_a_second_local_engine_needs_one_line(monkeypatch):
    """The shape stays open for another local engine: listing it makes it
    the second opinion for a Parakeet transcript."""
    monkeypatch.setattr(verify, "LOCAL_ENGINES", ("parakeet", "other-local"))
    assert verify.secondary_engine(config.PARAKEET_MODEL) == "other-local"
    assert verify.secondary_engine(CLOUD) == "parakeet"


def test_run_with_no_second_engine_raises_and_loads_no_model():
    import pytest
    with pytest.raises(ValueError, match="no second local engine"):
        verify.run("dummy.wav", PRIMARY, config.PARAKEET_MODEL)


def test_sidecar_roundtrip(sandbox):
    regs = [{"start": 1.0, "end": 2.0, "ours": "a", "theirs": "b"}]
    verify.save_sidecar("Mtg", regs, config.PARAKEET_MODEL)
    got = verify.load_sidecar("Mtg")
    assert got["engine"] == config.PARAKEET_MODEL and got["regions"] == regs
    assert verify.load_sidecar("Nothing") is None
    # a corrupt sidecar degrades to None, never a crash
    mfile("Bad", ".verify.json").write_text("{nope")
    assert verify.load_sidecar("Bad") is None


def test_verify_flag_is_reviewable(sandbox):
    """The flag round-trips through the review workflow like any other."""
    from stt import review
    segments = [{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "name": "Mark",
                 "display": "Mark", "text": "We reviewed the dashboard",
                 "flags": [verify.FLAG], "overlap": False,
                 "alt": [{"start": 0.4, "end": 0.8, "ours": "reviewed", "theirs": "renewed"}]}]
    data = {"source_file": "Mtg.m4a", "duration_sec": 2.0, "strict": False,
            "speakers": [{"id": "SPEAKER_00", "name": "Mark", "display": "Mark"}],
            "segments": segments, "words": []}
    (mfile("Mtg", ".json")).write_text(json.dumps(data))
    (mfile("Mtg", ".txt")).write_text("stub")

    out = review.list_flagged("Mtg")
    assert out["items"][0]["alt"][0]["theirs"] == "renewed"
    r = review.apply("Mtg", 0, "edit", start=0.0, text="We renewed the dashboard")
    assert r["ok"]
    d = json.loads((mfile("Mtg", ".json")).read_text())
    assert d["segments"][0]["flags"] == [] and "alt" not in d["segments"][0]


def test_failed_secondary_engine_flags_nothing(monkeypatch):
    """A second engine that returns almost nothing has failed — flagging the
    whole transcript as 'disagreement' would be noise. Drive verify.run()
    itself so the 30%-floor guard is exercised, not just the region math it
    protects: a cloud primary routes the second opinion through Parakeet,
    which we stub to return a single word (1 << 0.3*6)."""
    from stt import asr_parakeet
    sec = [{"start": 0.0, "end": 0.4, "word": "ok"}]
    monkeypatch.setattr(asr_parakeet, "transcribe",
                        lambda wav, progress=None: {"words": sec,
                                                    "engine": config.PARAKEET_MODEL})
    regs, engine = verify.run("dummy.wav", PRIMARY, CLOUD)
    # below the floor: run() returns NO regions even though the raw math on the
    # same inputs would emit a giant spurious deletion — that gap is the guard.
    assert regs == [] and engine == config.PARAKEET_MODEL
    assert verify.regions(PRIMARY, sec) != []


def test_working_secondary_engine_flags_real_disagreement(monkeypatch):
    """The other side of the floor: a second engine that returns ENOUGH words
    (>= 30% of primary) which genuinely disagree must yield real regions — so a
    removed or inverted floor comparison fails one of these two tests."""
    from stt import asr_parakeet
    sec = _w([(0.0, 0.4, "We"), (0.4, 0.8, "renewed"), (0.8, 1.2, "the"),
              (1.2, 1.8, "dashboard")])  # 4 words >= 0.3*6; "renewed" disagrees
    monkeypatch.setattr(asr_parakeet, "transcribe",
                        lambda wav, progress=None: {"words": sec,
                                                    "engine": config.PARAKEET_MODEL})
    regs, engine = verify.run("dummy.wav", PRIMARY, CLOUD)
    assert engine == config.PARAKEET_MODEL
    assert any(r["theirs"] == "renewed" for r in regs)


def test_a_second_engine_that_stops_early_flags_no_tail(monkeypatch, capsys):
    """A second engine that crashed or dropped its last chunk still passed the
    30% floor, and the whole unheard tail became one region: every segment
    in it was flagged. Only the part it reached is compared now."""
    from stt import asr_parakeet
    words = "we will review the attendance numbers for every school this week and next".split()
    primary = _w([(2.0 * i, 2.0 * i + 1.5, w) for i, w in enumerate(words)])
    sec = [dict(w) for w in primary[:6]]          # it stopped after "numbers"
    sec[2]["word"] = "renew"                        # one real disagreement it heard
    monkeypatch.setattr(asr_parakeet, "transcribe",
                        lambda wav, progress=None: {"words": sec,
                                                    "engine": config.PARAKEET_MODEL})
    regs, _ = verify.run("dummy.wav", primary, CLOUD)
    assert regs == [{"start": 4.0, "end": 5.5, "ours": "review", "theirs": "renew"}]
    assert "not compared" in capsys.readouterr().out
    # the raw alignment still makes the tail one region: run() is the guard
    assert verify.regions(primary, sec)[-1]["ours"].startswith("for every school")
    # a few unheard words at the end are a disagreement, not a stopped engine
    assert verify.heard(primary, primary[:-2]) == primary
    assert verify.heard(primary, []) == primary


def test_apply_flags_skips_a_neighbour_that_only_touches_the_region():
    """The overlap test was >= 0 for every region, so a region that lies in
    one segment also flagged both neighbours that end or start at its edge.
    A region that lies in no segment still flags the ones it touches."""
    def segs():
        return [{"start": 0.0, "end": 5.0, "text": "a"},
                {"start": 5.0, "end": 9.0, "text": "b"},
                {"start": 9.0, "end": 12.0, "text": "c"}]
    s = segs()
    assert verify.apply_flags(s, [{"start": 5.0, "end": 9.0, "ours": "x", "theirs": "y"}]) == 1
    assert [x.get("flags") for x in s] == [None, [verify.FLAG], None]
    s = segs()                                      # zero width on a shared edge
    assert verify.apply_flags(s, [{"start": 5.0, "end": 5.0, "ours": "", "theirs": "y"}]) == 2
    assert [bool(x.get("flags")) for x in s] == [True, True, False]
    s = [{"start": 0.0, "end": 5.0, "text": "a"}, {"start": 6.0, "end": 9.0, "text": "b"}]
    assert verify.apply_flags(s, [{"start": 5.0, "end": 6.0, "ours": "", "theirs": "y"}]) == 2


def test_a_curly_apostrophe_or_a_digit_comma_is_the_same_word():
    """A curly apostrophe became a space, so "don’t" read as "don t" and
    flagged against "don't". "1,000" read as "1 000" against "1000"."""
    def W(text):
        return _w([(0.5 * i, 0.5 * i + 0.4, w) for i, w in enumerate(text.split())])
    for ours, theirs in [("we don't know", "we don’t know"),
                         ("we don’t know", "we don't know"),
                         ("paid 1,000 dollars", "paid 1000 dollars"),
                         ("paid 1,000,000 dollars", "paid 1000000 dollars"),
                         ("he said ‘fine’ twice", "he said fine twice"),
                         ("he said 'fine' twice", "he said fine twice")]:
        assert verify.regions(W(ours), W(theirs)) == [], (ours, theirs)
    assert verify._norm("1,2,3") == "1 2 3"          # a list keeps its commas apart
    assert verify.regions(W("we can't go"), W("we can go")) != []


def test_a_sidecar_of_the_wrong_shape_never_stops_a_relabel(sandbox):
    """load_sidecar caught only bad JSON. Bad UTF-8, a list, or a null
    "regions" raised in relabel, and that meeting's relabel stopped."""
    good = {"start": 0.4, "end": 0.8, "ours": "reviewed", "theirs": "renewed"}
    p = mfile("Mtg", ".verify.json")
    for raw, want in [(b"\xff\xfe{bad", None), (b"[1,2]", None), (b'"x"', None),
                      (b'{"engine":"x","regions":null}', []),
                      (b'{"engine":"x","regions":{"a":1}}', []),
                      (json.dumps({"engine": "x", "regions": [
                          good, 3, None, {"start": "a", "end": 1, "ours": "", "theirs": ""},
                          {"start": 1, "end": 2}]}).encode(), [good])]:
        p.write_bytes(raw)
        vc = verify.load_sidecar("Mtg")                     # what relabel does
        if want is None:
            assert vc is None, raw
            continue
        assert vc["regions"] == want, raw
        segs = [{"start": 0.0, "end": 2.0, "text": "We reviewed the dashboard", "flags": []}]
        assert verify.apply_flags(segs, vc.get("regions", [])) == len(want)
