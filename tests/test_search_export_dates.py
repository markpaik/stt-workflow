"""dates: filename conventions; search: cross-meeting queries; export: docx/html."""
import json

import pytest

from stt import config, dates, export, search
from conftest import mfile


# ---------- dates ----------

def test_house_convention_mmddyyyy():
    assert dates.meeting_date("LT Meeting 05212026") == "2026-05-21"
    assert dates.meeting_date("Thrive ICD Sami 10242026") == "2026-10-24"
    assert dates.meeting_date("Jordan Thrive 03142025") == "2025-03-14"


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
    (mfile(base, ".json")).write_text(json.dumps(data))


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


def test_search_survives_meeting_vanishing_mid_request(sandbox, monkeypatch):
    """A meeting renamed/deleted between listing and the mtime sort must be
    skipped, not turn the whole search into a 500."""
    _mk("Mtg A", ["panorama survey today."], sandbox)
    monkeypatch.setattr(config, "meeting_bases",
                        lambda *a, **k: ["Mtg A", "Ghost Mtg"])  # ghost has no .json
    r = search.query("panorama")
    assert r["total"] == 1
    assert {h["base"] for h in r["hits"]} == {"Mtg A"}


def test_search_short_query_ignored(sandbox):
    _mk("Mtg A", ["hello there"], sandbox)
    r = search.query("he")
    assert (r["hits"], r["total"]) == ([], 0)
    assert r["note"] == search.STOP_NOTE.format(q="he")   # it says why


def test_search_cache_invalidates_on_change(sandbox):
    _mk("Mtg A", ["alpha topic"], sandbox)
    assert search.query("alpha")["total"] == 1
    import os, time
    time.sleep(0.02)
    _mk("Mtg A", ["beta topic"], sandbox)
    os.utime(mfile("Mtg A", ".json"))
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
    (mfile("Mtg 05212026", ".json")).write_text(json.dumps(data))


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


def test_export_labels_a_line_from_the_roster_never_the_raw_id(sandbox, tmp_path, monkeypatch):
    """A voice below the minting floor has a roster entry ("Voice 9") while
    its lines carry only the raw diarizer id. Word and PDF printed
    "SPEAKER_08" under a header that read "Voice 9"; the panel and the
    .txt read "Voice 9"."""
    data = {"date": "2026-03-01", "duration_sec": 60,
            "speakers": [{"id": "SPEAKER_08", "display": "Voice 9"}],
            "segments": [{"start": 1.0, "end": 2.0, "speaker": "SPEAKER_08",
                          "text": "hello"},
                         {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_03",
                          "text": "off the roster"}],
            "words": []}
    mfile("2026-03-01 Raw Label", ".json").write_text(json.dumps(data))
    h = export.to_html("2026-03-01 Raw Label")
    assert 'class="who">Voice 9 ' in h and 'class="who">Voice 4 ' in h
    assert "SPEAKER_" not in h
    monkeypatch.setattr(export.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "Downloads").mkdir()
    from docx import Document
    text = [p.text for p in Document(str(export.to_docx("2026-03-01 Raw Label"))).paragraphs]
    assert "Voice 9  [00:01]" in text and "Voice 4  [00:02]" in text
    assert not any("SPEAKER_" in t for t in text)


def test_a_control_character_in_a_line_never_fails_the_word_export(sandbox, tmp_path, monkeypatch):
    """python-docx refuses a string with a NUL or another XML-illegal
    character. One such character from a paste or an ASR artifact failed the
    Word export with a raw ValueError while the PDF of the same meeting
    printed. Both formats now drop the character and keep the line."""
    data = {"date": "2026-03-02", "duration_sec": 60,
            "speakers": [{"id": "SPEAKER_00", "display": "Dana\x07 Fox"}],
            "segments": [{"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00",
                          "text": "bad \x00\x0b char\ufffe here\ud800"},
                         {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_00",
                          "text": "\x00\x01"}],
            "words": []}
    mfile("2026-03-02 Ctrl\x1f Chars", ".json").write_text(json.dumps(data))
    monkeypatch.setenv("STT_EXPORT_DIR", str(tmp_path / "out"))
    from docx import Document
    text = [p.text for p in Document(str(export.to_docx("2026-03-02 Ctrl\x1f Chars"))).paragraphs]
    assert "bad  char here" in text and "Dana Fox  [00:01]" in text
    assert "2026-03-02 Ctrl Chars" in text
    # a line that held nothing else is dropped like an empty line
    assert sum(t.startswith("Dana Fox") for t in text) == 1
    h = export.to_html("2026-03-02 Ctrl\x1f Chars")
    assert "bad  char here" in h and "\x00" not in h and "\x0b" not in h
    h.encode("utf-8")  # no lone surrogate left to fail the PDF page write


def test_a_null_or_missing_field_never_fails_the_export(sandbox, tmp_path, monkeypatch):
    """Export indexed d["display"], seg["start"] and duration_sec directly
    and called .strip() on the text, so a hand-edited json with a null or
    missing value failed with a raw TypeError, KeyError or ValueError. The
    panel showed only its generic failure. Such a value now reads as
    absent: the line, its timestamp or the meta part is left out."""
    data = {"date": "Jan 5", "duration_sec": None,
            "speakers": [{"id": "SPEAKER_00"}, {"display": None}, "Dana Fox",
                         {"id": "SPEAKER_01", "display": "Sam Chen"}],
            "segments": [{"text": "no start", "speaker": "SPEAKER_01"},
                         {"start": None, "text": "null start", "speaker": "SPEAKER_01"},
                         {"start": 1, "text": None, "speaker": "SPEAKER_01"},
                         {"start": 2, "speaker": "SPEAKER_01"},
                         "not a line",
                         {"start": 3.0, "text": "kept", "speaker": "SPEAKER_01"}],
            "words": []}
    mfile("2026-03-03 Hand Edit", ".json").write_text(json.dumps(data))
    monkeypatch.setenv("STT_EXPORT_DIR", str(tmp_path / "out"))
    from docx import Document
    text = [p.text for p in Document(str(export.to_docx("2026-03-03 Hand Edit"))).paragraphs]
    # the bad date falls back to the folder name's date, the null duration
    # is left out, and only the speaker with a display is listed
    assert "March 3, 2026  ·  Sam Chen" in text
    assert text.count("Sam Chen") == 2       # two lines with no timestamp
    assert "Sam Chen  [00:03]" in text
    assert [t for t in text if t in ("no start", "null start", "kept")] == [
        "no start", "null start", "kept"]
    h = export.to_html("2026-03-03 Hand Edit")
    assert h.count('<div class="seg">') == 3 and '<span class="ts">00:03</span>' in h

    # nothing for the meta line at all: the Word file still writes
    mfile("Bare Hand Edit", ".json").write_text(json.dumps(
        {"duration_sec": "long", "speakers": None, "segments": None}))
    assert export.to_docx("Bare Hand Edit").exists()


@pytest.mark.parametrize("raw", ['{"segments": [{"start":', "[]", "null", "\udcff"])
def test_an_unreadable_meeting_json_fails_the_export_with_its_own_message(sandbox, raw):
    """A truncated json or one that is not an object failed with a raw
    JSONDecodeError or AttributeError. It is a UserError now, and the panel
    shows its text."""
    from stt.errors import UserError
    f = mfile("2026-03-04 Damaged", ".json")
    if raw == "\udcff":
        f.write_bytes(b"\xff\xfe{")
    else:
        f.write_text(raw)
    for fn in (export.to_docx, export.to_html):
        with pytest.raises(UserError, match="cannot be read"):
            fn("2026-03-04 Damaged")


def test_docx_no_clobber(sandbox, tmp_path, monkeypatch):
    _mk_full(sandbox)
    monkeypatch.setattr(export.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "Downloads").mkdir()
    a = export.to_docx("Mtg 05212026")
    b = export.to_docx("Mtg 05212026")
    assert a != b and b.exists()  # second export gets " (1)" suffix


@pytest.mark.parametrize("title", ["x" * 237, "é" * 118], ids=["ascii", "two-byte"])
def test_a_second_export_of_a_name_near_the_limit_still_fits(sandbox, tmp_path, monkeypatch, title):
    """A folder name of about 248 bytes fits its own .json, so the first
    export fit too. The " (1)" of the second export pushed the file name
    past 255 bytes, and the export failed with a raw OSError."""
    base = "2026-01-07 " + title
    mfile(base, ".json").write_text(json.dumps(
        {"segments": [{"start": 1, "text": "hi", "speaker": "S1"}], "speakers": []}))
    monkeypatch.setenv("STT_EXPORT_DIR", str(tmp_path / "out"))
    outs = [export.to_docx(base) for _ in range(3)]
    assert len(set(outs)) == 3 and all(o.exists() for o in outs)
    assert outs[0].name == base + ".docx"
    for n, o in enumerate(outs[1:], 1):
        assert len(o.name.encode()) <= export.NAME_MAX
        assert o.name.endswith(f" ({n}).docx") and o.name.startswith("2026-01-07 ")
        o.name.encode("utf-8").decode("utf-8")  # never cut inside a character


def test_export_lands_under_stt_home_not_the_real_downloads(sandbox, tmp_path, monkeypatch):
    """A sandboxed panel (STT_HOME set) must never write a transcript copy to
    the real ~/Downloads, which is often cloud-synced."""
    _mk_full(sandbox)
    home = tmp_path / "home"
    fake_user = tmp_path / "user"
    (fake_user / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(export.Path, "home", staticmethod(lambda: fake_user))
    monkeypatch.delenv("STT_EXPORT_DIR", raising=False)
    monkeypatch.setattr(config, "STT_HOME", str(home))
    monkeypatch.setattr(config, "_HOME", home)
    out = export.to_docx("Mtg 05212026")
    assert out.parent == home / "exports" and out.exists()
    assert list((fake_user / "Downloads").iterdir()) == []

    # STT_EXPORT_DIR wins over both
    monkeypatch.setenv("STT_EXPORT_DIR", str(tmp_path / "chosen"))
    assert export.to_docx("Mtg 05212026").parent == tmp_path / "chosen"


def test_export_dir_reads_stt_env_and_anchors_a_relative_value(sandbox, monkeypatch, tmp_path):
    """STT_EXPORT_DIR in stt.env was ignored, and a relative value landed
    under whatever folder the panel started in."""
    fake_user = tmp_path / "user"
    fake_user.mkdir()
    monkeypatch.setattr(export.Path, "home", staticmethod(lambda: fake_user))
    monkeypatch.delenv("STT_EXPORT_DIR", raising=False)
    env = config.PROJECT_DIR / "stt.env"
    env.write_text(f"STT_EXPORT_DIR={tmp_path / 'from_env'}\n")
    assert export.export_dir() == tmp_path / "from_env"
    env.write_text("STT_EXPORT_DIR=rel/dir\n")
    monkeypatch.chdir(tmp_path)
    assert export.export_dir() == fake_user / "rel" / "dir"
    env.write_text("")
    monkeypatch.setenv("STT_EXPORT_DIR", "other")
    assert export.export_dir() == fake_user / "other"


# ---------- PDF: the file decides, not Chrome's exit code ----------

def _fake_chrome(tmp_path, monkeypatch, body: str, rc: int):
    """A stand-in for headless Chrome: writes `body` (printf format) to the
    --print-to-pdf path, records its arguments, and exits with `rc`."""
    script = tmp_path / "chrome"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/chrome.args"\n'
        'for a in "$@"; do case "$a" in --print-to-pdf=*) out="${a#--print-to-pdf=}";; esac; done\n'
        + (f"printf '{body}' > \"$out\"\n" if body is not None else "")
        + f"exit {rc}\n")
    script.chmod(0o755)
    monkeypatch.setattr(export, "CHROME", str(script))
    monkeypatch.setenv("STT_EXPORT_DIR", str(tmp_path / "out"))
    return tmp_path / "out"


GOOD_PDF = "%%PDF-1.4\\n1 0 obj\\n<<>>\\nendobj\\n%%%%EOF\\n"


def test_a_pdf_chrome_wrote_is_kept_when_chrome_crashes_on_exit(sandbox, tmp_path, monkeypatch):
    """Headless Chrome writes the PDF, then can crash on exit (crashpad,
    exit 2). check=True turned that into a failed export, and each retry
    left one more "name (n).pdf"."""
    _mk_full(sandbox)
    out_dir = _fake_chrome(tmp_path, monkeypatch, GOOD_PDF, rc=2)
    out = export.to_pdf("Mtg 05212026")
    assert out == out_dir / "Mtg 05212026.pdf"
    assert out.read_bytes().startswith(b"%PDF")
    assert sorted(p.name for p in out_dir.iterdir()) == ["Mtg 05212026.pdf"]
    args = (tmp_path / "chrome.args").read_text().splitlines()
    # its own throwaway profile: never the running Chrome's, which it can contend with
    assert any(a.startswith("--user-data-dir=") for a in args)
    # and that profile never asks the login keychain, which opens a macOS dialog
    assert "--use-mock-keychain" in args and "--password-store=basic" in args


@pytest.mark.parametrize("body, rc", [(None, 2), ("", 0), ("%%PDF-1.4\\npartial", 0),
                                      ("<html>not a pdf</html>", 0)])
def test_a_missing_or_broken_pdf_fails_and_leaves_no_file(sandbox, tmp_path, monkeypatch, body, rc):
    _mk_full(sandbox)
    out_dir = _fake_chrome(tmp_path, monkeypatch, body, rc=rc)
    with pytest.raises(RuntimeError, match="PDF"):
        export.to_pdf("Mtg 05212026")
    assert list(out_dir.iterdir()) == []
