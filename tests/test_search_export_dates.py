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


def test_docx_no_clobber(sandbox, tmp_path, monkeypatch):
    _mk_full(sandbox)
    monkeypatch.setattr(export.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / "Downloads").mkdir()
    a = export.to_docx("Mtg 05212026")
    b = export.to_docx("Mtg 05212026")
    assert a != b and b.exists()  # second export gets " (1)" suffix


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
