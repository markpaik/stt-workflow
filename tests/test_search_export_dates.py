"""dates: filename conventions; search: cross-meeting queries; export: docx/html."""
import json

from stt import config, dates, export, search


# ---------- dates ----------

def test_house_convention_mmddyyyy():
    assert dates.meeting_date("LT Meeting 05212026") == "2026-05-21"
    assert dates.meeting_date("Thrive ICD Omar 10242026") == "2026-10-24"
    assert dates.meeting_date("Chrishele Thrive 03142025") == "2025-03-14"


def test_yyyymmdd_accepted():
    assert dates.meeting_date("board notes 20260521") == "2026-05-21"


def test_no_date_returns_none():
    assert dates.meeting_date("Leadership Story") is None
    assert dates.meeting_date("room 12345678999") is None  # 11 digits: no 8-run
    assert dates.meeting_date("v 99999999") is None        # invalid both ways


def test_ambiguous_prefers_house_convention():
    # 12032026: valid as Dec 3 2026 (MMDDYYYY) — house convention wins
    assert dates.meeting_date("mtg 12032026") == "2026-12-03"


# ---------- search ----------

def _mk(base, texts, sandbox):
    data = {"source_file": f"{base}.m4a", "duration_sec": 60,
            "speakers": [{"id": "SPEAKER_00", "name": None, "display": "Speaker 1"}],
            "segments": [{"start": float(i * 10), "end": float(i * 10 + 9),
                          "speaker": "SPEAKER_00", "display": "Speaker 1",
                          "text": tx, "flags": []} for i, tx in enumerate(texts)],
            "words": []}
    (config.MEETINGS_DIR / f"{base}.json").write_text(json.dumps(data))


def test_search_across_meetings(sandbox):
    _mk("Mtg A", ["We discussed the Panorama survey today.", "Other things."], sandbox)
    _mk("Mtg B", ["Budget review.", "panorama results look strong."], sandbox)
    r = search.query("panorama")
    assert r["total"] == 2
    bases = {h["base"] for h in r["hits"]}
    assert bases == {"Mtg A", "Mtg B"}
    hit = next(h for h in r["hits"] if h["base"] == "Mtg B")
    assert hit["index"] == 1 and hit["start"] == 10.0
    assert "panorama" in hit["snippet"].lower()


def test_search_short_query_ignored(sandbox):
    _mk("Mtg A", ["hello there"], sandbox)
    assert search.query("he") == {"query": "he", "hits": [], "total": 0}


def test_search_cache_invalidates_on_change(sandbox):
    _mk("Mtg A", ["alpha topic"], sandbox)
    assert search.query("alpha")["total"] == 1
    import os, time
    time.sleep(0.02)
    _mk("Mtg A", ["beta topic"], sandbox)
    os.utime(config.MEETINGS_DIR / "Mtg A.json")
    assert search.query("alpha")["total"] == 0
    assert search.query("beta topic")["total"] == 1


# ---------- export ----------

def _mk_full(sandbox):
    data = {"source_file": "Mtg 05212026.m4a", "duration_sec": 120,
            "speakers": [{"id": "SPEAKER_00", "name": "Mark", "display": "Mark"}],
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00",
                          "display": "Mark", "text": "Hello & welcome <all>.",
                          "flags": []},
                         {"start": 5.0, "end": 6.0, "speaker": "SPEAKER_00",
                          "display": "Mark", "text": "Uncertain bit.",
                          "flags": ["overlap"]}],
            "words": []}
    (config.MEETINGS_DIR / "Mtg 05212026.json").write_text(json.dumps(data))


def test_html_export_escapes_and_marks(sandbox):
    _mk_full(sandbox)
    h = export.to_html("Mtg 05212026")
    assert "Hello &amp; welcome &lt;all&gt;." in h
    assert "uncertain" in h            # flagged segment marked
    assert "May 21, 2026" in h         # date parsed from the name
    assert "Mark" in h


def test_docx_export_writes_valid_file(sandbox, tmp_path, monkeypatch):
    _mk_full(sandbox)
    monkeypatch.setattr(export.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "Downloads").mkdir()
    out = export.to_docx("Mtg 05212026")
    assert out.exists() and out.suffix == ".docx"
    from docx import Document
    doc = Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Hello & welcome <all>." in text
    assert "Mtg 05212026" in text


def test_docx_no_clobber(sandbox, tmp_path, monkeypatch):
    _mk_full(sandbox)
    monkeypatch.setattr(export.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "Downloads").mkdir()
    a = export.to_docx("Mtg 05212026")
    b = export.to_docx("Mtg 05212026")
    assert a != b and b.exists()  # second export gets " (1)" suffix
