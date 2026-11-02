"""tools/review_metrics.py (unified plan, Phase 1 step 8): read-only daily
counts, one row a day, counts only, and named talk for public and non-public
meetings. Every name is synthetic."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import run_batch
from stt import config
from conftest import mfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import review_metrics  # noqa: E402

NOW = datetime.now()


def _m(base, *, days_ago=1, speakers, segments, **extra):
    d = {"meeting_id": f"id-{base[:6]}", "source_file": f"{base}.m4a",
         "duration_sec": 600.0, "date": NOW.date().isoformat(),
         "processed_at": (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds"),
         "speakers": speakers, "segments": segments, "words": []}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")
    return d


def _seg(sid, a, b):
    return {"start": a, "end": b, "speaker": sid, "text": "words"}


def _store():
    named = {"id": "SPEAKER_00", "display": "Avery Sample", "name": "Avery Sample"}
    _m("Team Sync 10012026", speakers=[named, {"id": "SPEAKER_01", "display": "Speaker 20"}],
       segments=[_seg("SPEAKER_00", 0, 2700), _seg("SPEAKER_01", 2700, 3600)],
       title_state="provisional", title_source="label", route="voice_memo", reviewed=False)
    _m("Board Session 10022026", speakers=[{"id": "SPEAKER_00", "display": "Speaker 82"}],
       segments=[_seg("SPEAKER_00", 0, 7200)], title_state="provisional",
       title_source="public_download", route="public_download", reviewed=False)
    _m("New Recording 7", speakers=[named], segments=[_seg("SPEAKER_00", 0, 60)],
       title_state="needs_title", title_source="model", route="voice_memo",
       reviewed=False, ai_title="Hiring Sync")
    _m("Quiet Room 10032026", speakers=[], segments=[], no_speech=True, route="native",
       reviewed=True)


def test_the_row_counts_titles_needs_you_and_named_talk(sandbox):
    _store()
    row = review_metrics.compute()
    assert row["meetings"] == 4
    assert row["title_state"] == {"missing": 1, "needs_title": 1, "provisional": 2}
    assert row["reviewed"] == {"false": 3, "true": 1}
    assert row["route"] == {"native": 1, "public_download": 1, "voice_memo": 2}
    ny = row["needs_you"]
    assert ny["title_rows"] == 1 and ny["other_rows"] == 2       # silent + voice
    assert ny["by_kind"] == {"needs_title": 1, "no_speech": 1, "voice": 1}
    assert row["arrivals_14d"] == 4 and row["no_speech"] == 1
    assert row["voices"] == {"asking": 1, "short_hidden": 0}
    assert row["named_talk"]["public"] == {"talk_hours": 2.0, "named_hours": 0.0,
                                           "named_share": 0.0}
    nonpub = row["named_talk"]["non_public"]
    assert nonpub["talk_hours"] == 1.02 and nonpub["named_share"] == 0.754


def test_the_row_holds_no_title_or_name(sandbox):
    _store()
    text = json.dumps(review_metrics.compute())
    for s in ("Team Sync", "Board Session", "Hiring Sync", "Avery", "Speaker 20",
              "id-", "Quiet Room"):
        assert s not in text, s


def test_compute_writes_nothing(sandbox):
    _store()
    before = sorted((p, p.stat().st_mtime_ns) for p in config.PROJECT_DIR.rglob("*")
                    if p.is_file())
    review_metrics.compute()
    after = sorted((p, p.stat().st_mtime_ns) for p in config.PROJECT_DIR.rglob("*")
                   if p.is_file())
    assert after == before


def test_one_row_a_day(sandbox):
    _store()
    assert review_metrics.record_today() is True
    assert review_metrics.record_today() is False
    p = config.PROJECT_DIR / "review_metrics.jsonl"
    assert len(p.read_text().splitlines()) == 1
    assert p.stat().st_mode & 0o777 == 0o600
    tomorrow = NOW + timedelta(days=1)
    assert review_metrics.record_today(now=tomorrow) is True
    assert len(p.read_text().splitlines()) == 2


def test_the_batch_end_records_the_day(sandbox):
    _store()
    assert run_batch.record_review_metrics() is True
    assert run_batch.record_review_metrics() is False
