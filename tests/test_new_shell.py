"""The new shell (?ui=new): a second composed page served by the SAME
marker/mtime mechanism as the old page, without disturbing the old one.

Mirrors test_frontend_js / test_layout -- structure and regex over the composed
page -- plus a live byte-compare proving the routing split leaves plain / (and
/?ui=old) exactly as they were.
"""
import http.client
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from gui import server as srv

NODE = shutil.which("node")
STATIC = Path(srv.__file__).resolve().parent / "static"
NEW = STATIC / "new"


def _compose(static_dir):
    """Independent re-implementation of the composer, so the test verifies the
    served bytes against the raw files rather than trusting server internals."""
    page = (static_dir / "page.html").read_text(encoding="utf-8")
    css = (static_dir / "app.css").read_text(encoding="utf-8")
    js = (static_dir / "app.js").read_text(encoding="utf-8")
    return page.replace("/*@APP_CSS@*/", css).replace("//@APP_JS@", js)


@pytest.fixture
def running_server(sandbox):
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    t.join(timeout=2)


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    status, ctype, body = r.status, r.getheader("Content-Type"), r.read()
    conn.close()
    return status, ctype, body


def test_ui_new_serves_the_composed_new_shell(running_server):
    status, ctype, body = _get(running_server, "/?ui=new")
    assert status == 200
    assert "text/html" in ctype
    assert body == _compose(NEW).encode()


def test_plain_root_is_byte_identical_to_the_old_composition(running_server):
    # the routing split must not change a single byte of the old page
    expected = _compose(STATIC).encode()
    for path in ("/", "/?ui=old"):
        status, _ctype, body = _get(running_server, path)
        assert status == 200
        assert body == expected, f"{path} drifted from the old file composition"


def test_new_shell_has_all_seven_state_hooks():
    page = _compose(NEW)
    for st in ("recording", "waiting", "held", "processing",
               "needs_name", "ready", "failed"):
        assert f'data-state="{st}"' in page, f"missing state hook: {st}"


def test_new_shell_carries_both_theme_blocks_and_the_prepaint_snippet():
    page = _compose(NEW)
    # light + dark via prefers-color-scheme AND the manual data-theme overrides
    assert "@media(prefers-color-scheme:dark)" in page
    assert ":root[data-theme=dark]" in page
    assert ":root[data-theme=light]" in page
    # the same pre-paint theme snippet convention as the old page
    assert 'localStorage.getItem("stt_theme")' in page
    assert "document.documentElement.dataset.theme" in page


def test_new_shell_has_no_em_dashes_in_ui_strings():
    # house rule: no em dashes anywhere in the shell's copy
    assert "—" not in _compose(NEW)


def test_old_app_js_gained_the_two_bridge_params():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "get('review')" in js
    assert "get('who')" in js
    # ...and still handles the pre-existing open= deep link
    assert "get('open')" in js


def test_old_naming_dialog_explains_deleted_sources_instead_of_a_dead_player():
    # /api/voice_clips returns reason:"sources_deleted" when every meeting a
    # voice was heard in is gone; the old dialog must show the plain sentence,
    # not fall through to an <audio> element that can never load
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "r.reason==='sources_deleted'" in js
    assert "No audio available. The source recordings were deleted." in js


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS syntax gate skipped")
def test_new_shell_js_parses():
    page = _compose(NEW)
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", page, re.S)
    assert scripts, "no <script> block in the new shell"
    for i, js in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
            path = f.name
        try:
            r = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        assert r.returncode == 0, f"new shell <script> {i} syntax error:\n{r.stderr}"


# ---------------------------------------------------------------------------
# Builder B: the interaction layer. Every seam A stubbed is now wired, against
# the existing endpoints, so these assert the wiring is real (no leftover stubs)
# and that the payload shapes still match the server's contract.
# ---------------------------------------------------------------------------
NEW_JS = (NEW / "app.js").read_text(encoding="utf-8")
OLD_JS = (STATIC / "app.js").read_text(encoding="utf-8")
OLD_PAGE = (STATIC / "page.html").read_text(encoding="utf-8")

# the seams A left named in the read-only shell, all owned by Builder B now
SEAMS = ["toggleProcess", "scheduleSearch", "trayAct", "trayExpand", "rowListen",
         "rowHold", "rowRelease", "rowProcess", "rowDelete", "rowRetry",
         "openMeeting", "rowMenu", "openReviewBadge", "acceptMeeting",
         "cycleCat", "toggleSel"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS syntax gate skipped")
def test_new_shell_app_js_passes_node_check():
    # the app.js file itself (not just the composed page) is valid JS
    r = subprocess.run([NODE, "--check", str(NEW / "app.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_every_builder_b_seam_is_defined_and_not_a_stub():
    # the no-op placeholder is gone entirely...
    assert "_todo" not in NEW_JS, "a Builder B seam is still the no-op stub"
    # ...and every seam is a real function definition with a non-trivial body
    for name in SEAMS:
        m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{",
                      NEW_JS)
        assert m, f"seam not defined: {name}"
        # a body of more than a few chars before the next seam / section comment
        tail = NEW_JS[m.end():m.end() + 40].strip()
        assert tail and not tail.startswith("}"), f"seam looks empty: {name}"


def test_run_option_keys_persist_in_the_new_shell():
    # NOTE: the old page never persisted the four run options (they reset on each
    # load), so there is no matching key set to agree with -- the keys live ONLY
    # in the new shell, which now remembers them across its own reloads.
    keys = ["stt_run_par2", "stt_run_strict", "stt_run_verify", "stt_run_onetime"]
    for k in keys:
        assert k in NEW_JS, f"missing run-option persistence key: {k}"
        # written once (optSet) and read back at least once (runOpts) => >= 2
        assert NEW_JS.count(k) >= 2, f"{k} is not both written and read"
    # the divergence is real and intentional: the byte-frozen old page has none
    for k in keys:
        assert k not in OLD_JS and k not in OLD_PAGE, \
            f"{k} unexpectedly present in the old page"
    # runOpts() maps them onto the /api/run body the server already accepts
    assert re.search(r"function\s+runOpts\s*\(", NEW_JS)
    for field in ("parallel", "strict", "verify", "onetime"):
        assert field in NEW_JS


def test_bulk_action_payloads_match_the_server_contract():
    # /api/bulk is called with the bases/action/value body the server switch reads
    assert re.search(r"api\('/api/bulk',\{bases,action,value", NEW_JS)
    # every action string the server's /api/bulk switch handles is emitted here
    for action in ("category", "date", "rename", "archive", "drop_audio", "delete"):
        assert re.search(r"bulk\('" + action + r"'", NEW_JS), \
            f"bulk bar never emits action '{action}'"
    # destructive bulk ops carry the confirmation the server requires
    assert "{confirm:true}" in NEW_JS


def test_row_and_queue_actions_hit_the_expected_endpoints():
    # the queue/meeting endpoints the seams reuse, all unchanged server-side
    for ep in ("/api/queue_hold", "/api/queue_delete", "/api/queue_audio",
               "/api/audio", "/api/run", "/api/accept_meeting", "/api/set_category",
               "/api/stop", "/api/pause", "/api/resume", "/api/pick_files",
               "/api/fix_recorder_permissions", "/api/search", "/api/export",
               "/api/txt", "/api/rename", "/api/edits", "/api/archive_meeting",
               "/api/delete_meeting"):
        assert ep in NEW_JS, f"seam layer never calls {ep}"
    # tray + search bridge to the old page's additive deep links
    assert "'/?review='" in NEW_JS and "'/?who='" in NEW_JS and "'/?open='" in NEW_JS


# ---------------------------------------------------------------------------
# Builder A: THE MEETING PAGE. A hash route #m/<base> renders one scrollable
# document (header, docked audio, summary, flagged strip, transcript, pinned
# ask bar) in place of the old Read + Summary + Ask surfaces. These assert the
# route wiring, the ported reader/ask semantics, and the read endpoints -- with
# the same regex-over-the-composed-page style as the rest of this file.
# ---------------------------------------------------------------------------
def test_meeting_page_is_a_hash_route_with_a_container_and_handler():
    page = _compose(NEW)
    # the #m/<base> route, its markup mount, and a live hashchange handler
    assert "#m/" in page
    assert 'id="meetingpage"' in page
    assert "addEventListener('hashchange'" in NEW_JS
    for fn in ("parseHash", "applyRoute", "enterMeeting", "exitMeeting",
               "buildMeeting", "maybeBuildPending"):
        assert re.search(r"function\s+" + fn + r"\s*\(", NEW_JS), f"missing route fn: {fn}"
    # openMeeting now stays in the shell (sets the hash) instead of navigating away
    assert re.search(r"function\s+openMeeting[^}]*location\.hash\s*=\s*'#m/'", NEW_JS), \
        "openMeeting must set the #m/ hash, not navigate to the old page"


def test_meeting_page_defines_the_ported_reader_functions():
    # audio seek + playing-line highlight, and the in-page find (occurrence
    # count / prev-next / highlight), ported from the old transcript viewer
    for fn in ("mSeek", "mHighlight", "mPlayPause", "mCycleRate", "mToggleFollow",
               "mFindRun", "mFindNav", "mFindMark", "mFindShow", "mFindClear"):
        assert re.search(r"function\s+" + fn + r"\s*\(", NEW_JS), f"missing reader fn: {fn}"
    # the find field is focusable by slash or Cmd-F, like the old reader
    assert "metaKey" in NEW_JS and "e.key==='/'" in NEW_JS


def test_meeting_page_uses_the_read_endpoints_verbatim():
    # transcript is fetched (GET, no body) from /api/transcript, as the old reader
    assert "api('/api/transcript?base='" in NEW_JS
    # summary generation reuses the old GET /api/suggest call + its persistence
    assert "api('/api/suggest?base='" in NEW_JS
    # the audio src is the byte-range /api/audio?base= endpoint
    assert "'/api/audio?base='" in NEW_JS


def test_meeting_page_ask_stays_a_post_with_the_server_contract():
    # /api/ask is POSTed (api(path, body)) with the base/question/history body,
    # the same shape the server's /api/ask reads
    assert re.search(r"api\('/api/ask',\{base,question:q,history:hist\}", NEW_JS)
    assert "function mAskSend" in NEW_JS
    # last few successful turns ride along (old askSend semantics)
    assert "filter(h=>h.a&&!h.err).slice(-3)" in NEW_JS


def test_meeting_page_leaves_the_review_stepper_seam_for_builder_b():
    # the flagged strip renders only; stepping/acting is Builder B's, via this seam
    assert re.search(r"function\s+reviewStep\s*\(", NEW_JS)


# ---------------------------------------------------------------------------
# Live-review corrections (2026-07-12): type floor, the tray's >8 flagged
# aggregate filtering the library, ready-row click-to-expand, and Ask
# reachability (row menu + in-shell search hits).
# ---------------------------------------------------------------------------
NEW_CSS = (NEW / "app.css").read_text(encoding="utf-8")
NEW_PAGE = (NEW / "page.html").read_text(encoding="utf-8")


def test_css_type_floor_is_13px():
    # DESIGN.md (2026-07-12): no type below 13px anywhere in the shell
    sizes = [float(m) for m in re.findall(
        r"font(?:-size)?:[^;{}]*?(\d+(?:\.\d+)?)px", NEW_CSS)]
    assert sizes, "no font sizes found -- the audit regex broke"
    assert min(sizes) >= 13, f"type floor broken: {min(sizes)}px < 13px"


def test_shared_reading_column_is_920():
    # ONE column cap (920 content + two 24px gutters) shared by body, the
    # pinned ask bar, and the bulk bar -- no surface keeps a private width
    assert "--colcap:968px" in NEW_CSS
    assert NEW_CSS.count("var(--colcap)") >= 3
    assert "908px" not in NEW_CSS and "max-width:min(920px" not in NEW_CSS


def test_flagged_aggregate_filters_the_library_not_the_tray():
    # >8 flagged meetings: the tray line toggles a library filter (never a
    # second in-tray list); the chip beside the category filter clears it
    assert "TRAY_EXPAND_MAX=8" in NEW_JS
    assert re.search(r"function\s+flaggedToggle\s*\(", NEW_JS)
    assert re.search(r"function\s+flaggedClear\s*\(", NEW_JS)
    # ready rows without substantial flags drop out; pinned states are untouched
    assert re.search(r"flaggedOnly&&r\.state==='ready'&&!\(r\.review_substantial>0\)", NEW_JS)
    # the chip markup lives beside the #filter select in the header
    assert 'id="flagchip"' in NEW_PAGE
    assert "flagged only" in NEW_PAGE
    # the filter state folds into both signatures so polls preserve it
    assert NEW_JS.count("flaggedOnly") >= 6


def test_ready_row_click_expands_the_summary_in_place():
    # the expansion: full summary + committed next steps + open-transcript link,
    # toggled by the row body (controls guarded via closest), keyed by id
    assert re.search(r"function\s+toggleExpand\s*\(", NEW_JS)
    assert "const OPEN=new Set()" in NEW_JS
    assert "Open transcript" in NEW_JS
    assert "next_steps" in NEW_JS
    # the click guard: buttons/links/inputs/the review count never toggle
    assert re.search(r"closest\('button,a,input,select,textarea,label,\.rev'\)", NEW_JS)
    # open state folds into the row signature so expansions survive polls
    assert "OPEN.has(r.id)" in NEW_JS
    # height animates ~180ms, and reduced motion kills the transition
    assert re.search(r"\.rexp\{[^}]*transition:grid-template-rows \.18s ease", NEW_CSS)
    assert re.search(r"prefers-reduced-motion[^}]*\{[^{]*\*\{scroll-behavior", NEW_CSS)
    assert ".rexp," in NEW_CSS  # listed in the reduced-motion transition kill


def test_ask_is_reachable_from_the_row_menu_and_search_hits():
    # the per-row menu gained "Ask a question" -> meeting page, ask input focused
    assert "Ask a question" in NEW_JS
    assert re.search(r"function\s+rmAsk\s*\(", NEW_JS)
    # focus-after-build flag, not a timeout hack
    assert "pendingOpen" in NEW_JS
    assert not re.search(r"setTimeout\([^)]*maskq", NEW_JS)
    # search hits open the in-shell meeting page and seek to the hit's moment
    assert re.search(r"function\s+openHit\s*\(", NEW_JS)
    assert re.search(r"openHit\('\$\{escJs\(h\.base\)\}',\$\{Number\(h\.start\)", NEW_JS)
    # the hit no longer bridges to the old page (the tray fallback still may)
    assert "onclick=\"location.href='/?open='" not in NEW_JS


def test_serif_retired_and_mono_demoted_to_true_data():
    # DESIGN.md Type (revised 2026-07-12): one neutral sans, hierarchy from
    # weight and size. No serif anywhere in the composed shell, and the unused
    # --serif token is gone from the sheet.
    page = _compose(NEW)
    assert "ui-serif" not in page and "New York" not in page
    assert "--serif" not in NEW_CSS
    # mono survives ONLY as true data: the .mono timer/timestamp utility and
    # the status pill. Every other rule that names a font family is sans.
    mono_rules = re.findall(r"([^{}]+)\{[^}]*var\(--mono\)", NEW_CSS)
    survivors = {r.strip().splitlines()[-1].strip() for r in mono_rules}
    assert survivors == {".mono", ".pill"}, f"unexpected mono rules: {survivors}"
    # structural text is sans: row meta, group headers, tray header, right slot
    # (line-start anchored so `.rslot` hits its own rule, not `.row.open .rslot`)
    for sel in (".rmeta", ".mgroup", ".rslot", ".mmeta", ".tray .trayhdr"):
        m = re.search(r"(?m)^" + re.escape(sel) + r"\{[^}]*\}", NEW_CSS)
        assert m and "var(--sans)" in m.group(0), f"{sel} must be the sans stack"
    # in the markup, .mono rides only on timestamps/clocks/timers: the ticking
    # recording clock keeps it; the demoted rate/find-count/ask-note lost it
    assert 'id="recRowClock" class="mono"' in NEW_JS
    for demoted in ('"mrate mono"', '"mfindn mono"', '"masknote mono"'):
        assert demoted not in NEW_JS, f"demoted element still mono: {demoted}"
