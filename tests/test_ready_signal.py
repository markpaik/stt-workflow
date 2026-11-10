"""The ready count (unified plan, Phase 1 step 7, row 28): the menu bar
shows how many meetings became readable since the owner last looked at the
panel. It counts each meeting once, a Redo does not bring a seen meeting
back, and no banner is posted. Every name is synthetic."""
import json
import re
from datetime import datetime
from pathlib import Path

from gui import menubar
from gui import server as srv
from stt import config, ready
from conftest import mfile


def _m(base, processed_at, mid=None):
    d = {"meeting_id": mid or f"id-{abs(hash(base)) % 10**8}",
         "source_file": f"{base}.m4a", "processed_at": processed_at,
         "speakers": [], "segments": [], "words": []}
    mfile(base, ".json").write_text(json.dumps(d))
    return d


def test_no_look_on_record_counts_nothing(sandbox):
    _m("Budget Review 10012026", "2026-10-01T10:00:00")
    assert ready.new_count() == 0


def test_a_meeting_after_the_look_counts_once_and_the_look_clears_it(sandbox):
    _m("Budget Review 10012026", "2026-10-01T10:00:00")
    ready.mark_seen(now=datetime(2026, 10, 1, 12, 0, 0))
    assert ready.new_count() == 0
    _m("Team Sync 10022026", "2026-10-02T09:00:00")
    assert ready.new_since_seen() == ["Team Sync 10022026"]
    ready.mark_seen(now=datetime(2026, 10, 2, 9, 30, 0))
    assert ready.new_count() == 0


def test_a_redo_of_a_seen_meeting_does_not_count_again(sandbox):
    d = _m("Budget Review 10012026", "2026-10-01T10:00:00", mid="id-budget")
    ready.mark_seen(now=datetime(2026, 10, 1, 12, 0, 0))
    _m("Budget Review 10012026", "2026-10-03T08:00:00", mid=d["meeting_id"])
    assert ready.new_count() == 0


def test_a_meeting_without_a_stored_processed_at_never_counts(sandbox):
    ready.mark_seen(now=datetime(2026, 10, 1, 12, 0, 0))
    mfile("Old One", ".json").write_text(json.dumps(
        {"meeting_id": "id-old", "speakers": [], "segments": [], "words": []}))
    assert ready.new_count() == 0


def test_the_seen_file_holds_ids_and_a_time_only(sandbox):
    _m("Budget Review 10012026", "2026-10-01T10:00:00", mid="id-budget")
    ready.mark_seen(now=datetime(2026, 10, 1, 12, 0, 0))
    p = config.PROJECT_DIR / "panel_seen.json"
    assert json.loads(p.read_text()) == {"seen_at": "2026-10-01T12:00:00",
                                         "ids": ["id-budget"]}
    assert p.stat().st_mode & 0o777 == 0o600


def test_the_menu_bar_carries_the_count(sandbox):
    ready.mark_seen(now=datetime(2026, 10, 1, 12, 0, 0))
    _m("Team Sync 10022026", "2026-10-02T09:00:00")
    s = menubar.gather()
    assert s["new_count"] == 1
    assert menubar._new_title(1) == " 1 new" and menubar._new_title(0) == ""
    assert menubar._new_line(2) == "✓  2 new meetings since you last looked"
    # the count is part of the signature, so the dropdown redraws when it moves
    sig1 = menubar.STTMenuBar._signature(s)
    ready.mark_seen(now=datetime(2026, 10, 2, 10, 0, 0))
    assert menubar.STTMenuBar._signature(menubar.gather()) != sig1


def test_the_panel_records_a_look_through_post(sandbox):
    import http.client
    import threading
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/api/seen", body="{}",
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        body = json.loads(r.read())
        conn.close()
    finally:
        httpd.shutdown()
        t.join(timeout=2)
    assert body["ok"] and ready.load_seen()["seen_at"] == body["seen_at"]


def test_no_banner_is_posted_and_the_written_rule_stands():
    root = Path(srv.__file__).resolve().parent
    assert "rumps.notification" not in (root / "menubar.py").read_text()
    assert "No notification banners." in (root / "menubar.py").read_text()
    # the page marks the look only while it is visible: on load when the tab
    # is not hidden, and when a hidden tab comes back
    js = (root / "static" / "next.js").read_text()
    vis = re.search(r"addEventListener\('visibilitychange',\(\)=>\{.*?\n\}\);", js, re.S).group(0)
    shown = vis[vis.index("else{"):]
    assert "api('/api/seen',{})" in shown and "api('/api/seen'" not in vis[:vis.index("else{")]
    assert "if(!document.hidden)api('/api/seen',{})" in js


def test_a_look_while_the_library_is_missing_keeps_the_last_look(sandbox, tmp_path):
    """At login a library on another volume may not be mounted yet. A look
    then wrote ids [] and the time, so the next Redo of an old meeting
    counted it as new, and any meeting still waiting was cleared unseen.
    The last look stands now, and nothing is written."""
    d = _m("Budget Review 10012026", "2026-10-01T10:00:00", mid="id-budget")
    ready.mark_seen(now=datetime(2026, 10, 1, 12, 0, 0))
    _m("Team Sync 10022026", "2026-10-02T09:00:00", mid="id-sync")
    p = config.PROJECT_DIR / "panel_seen.json"
    before = p.read_text()
    r = ready.mark_seen(dest_dir=tmp_path / "not-mounted",
                        now=datetime(2026, 10, 2, 10, 0, 0))
    assert r["seen_at"] == "2026-10-01T12:00:00"
    assert p.read_text() == before
    # the library is back: the waiting meeting still counts, a Redo does not
    _m("Budget Review 10012026", "2026-10-03T08:00:00", mid=d["meeting_id"])
    assert ready.new_since_seen() == ["Team Sync 10022026"]


def test_a_first_look_while_the_library_is_missing_writes_nothing(sandbox, tmp_path):
    r = ready.mark_seen(dest_dir=tmp_path / "not-mounted",
                        now=datetime(2026, 10, 2, 10, 0, 0))
    assert r["seen_at"] == "2026-10-02T10:00:00"
    assert not (config.PROJECT_DIR / "panel_seen.json").exists()
    assert ready.new_count() == 0
