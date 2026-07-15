"""The panel shell: the ONE composed page (gui/static/), assembled by the
marker/mtime composer and served at /.

Structure and regex over the composed page, plus live byte-compares proving
the routing serves exactly what the composer builds.
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


def test_root_serves_the_shell_and_ignores_the_ui_param(running_server):
    # the shell is the only page. The retired ui query flag is ignored no
    # matter its value, so a stale bookmark still gets the shell, never a 404.
    expected = _compose(STATIC).encode()
    for path in ("/", "/?ui=stale-bookmark", "/?anything=else"):
        status, ctype, body = _get(running_server, path)
        assert status == 200
        assert "text/html" in ctype
        assert body == expected, f"{path} is not the shell"


def test_new_shell_has_all_seven_state_hooks():
    page = _compose(STATIC)
    for st in ("recording", "waiting", "held", "processing",
               "needs_name", "ready", "failed"):
        assert f'data-state="{st}"' in page, f"missing state hook: {st}"


def test_new_shell_carries_both_theme_blocks_and_the_prepaint_snippet():
    page = _compose(STATIC)
    # light + dark via prefers-color-scheme AND the manual data-theme overrides
    assert "@media(prefers-color-scheme:dark)" in page
    assert ":root[data-theme=dark]" in page
    assert ":root[data-theme=light]" in page
    # the pre-paint theme snippet (no flash of the wrong theme)
    assert 'localStorage.getItem("stt_theme")' in page
    assert "document.documentElement.dataset.theme" in page


def test_new_shell_has_no_em_dashes_in_ui_strings():
    # house rule: no em dashes anywhere in the shell's copy
    assert "—" not in _compose(STATIC)


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS syntax gate skipped")
def test_new_shell_js_parses():
    page = _compose(STATIC)
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
NEW_JS = (STATIC / "app.js").read_text(encoding="utf-8")

# the seams A left named in the read-only shell, all owned by Builder B now
SEAMS = ["toggleProcess", "scheduleSearch", "trayAct", "trayExpand", "rowListen",
         "rowHold", "rowRelease", "rowProcess", "rowDelete", "rowRetry",
         "openMeeting", "rowMenu", "openReviewBadge", "acceptMeeting",
         "cycleCat", "toggleSel"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS syntax gate skipped")
def test_new_shell_app_js_passes_node_check():
    # the app.js file itself (not just the composed page) is valid JS
    r = subprocess.run([NODE, "--check", str(STATIC / "app.js")],
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
    # the retired page never persisted the four run options (they reset on
    # each load); the shell remembers them across its own reloads.
    keys = ["stt_run_par2", "stt_run_strict", "stt_run_verify", "stt_run_onetime"]
    for k in keys:
        assert k in NEW_JS, f"missing run-option persistence key: {k}"
        # written once (optSet) and read back at least once (runOpts) => >= 2
        assert NEW_JS.count(k) >= 2, f"{k} is not both written and read"
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
    # the edit layer removed the LAST per-meeting bridges: review, naming, and
    # opening all happen in the shell now -- zero deep-link params remain
    for bridge in ("?review=", "?who=", "?open="):
        assert bridge not in NEW_JS, f"stale deep-link bridge in the shell: {bridge}"


# ---------------------------------------------------------------------------
# Builder A: THE MEETING PAGE. A hash route #m/<base> renders one scrollable
# document (header, docked audio, summary, flagged strip, transcript, pinned
# ask bar) in place of the old Read + Summary + Ask surfaces. These assert the
# route wiring, the ported reader/ask semantics, and the read endpoints -- with
# the same regex-over-the-composed-page style as the rest of this file.
# ---------------------------------------------------------------------------
def test_meeting_page_is_a_hash_route_with_a_container_and_handler():
    page = _compose(STATIC)
    # the #m/<base> route, its markup mount, and a live hashchange handler
    assert "#m/" in page
    assert 'id="meetingpage"' in page
    assert "addEventListener('hashchange'" in NEW_JS
    for fn in ("parseHash", "applyRoute", "enterMeeting", "exitMeeting",
               "buildMeeting", "maybeBuildPending"):
        assert re.search(r"function\s+" + fn + r"\s*\(", NEW_JS), f"missing route fn: {fn}"
    # openMeeting now stays in the shell (sets the hash) instead of navigating away
    assert re.search(r"function\s+openMeeting[^}]*location\.hash\s*=\s*'#m/'", NEW_JS), \
        "openMeeting must set the #m/ hash, not navigate away"


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


def test_review_stepper_is_wired_not_a_seam():
    # the flagged strip's seam is a real controller now: the first click starts
    # on the first flag, later clicks advance, and the walk happens in place
    m = re.search(r"function\s+reviewStep\s*\(\)\s*\{([^}]*)\}", NEW_JS)
    assert m and m.group(1).strip(), "reviewStep is still an empty seam"
    for fn in ("reviewStart", "reviewGo", "reviewExit", "renderReviewCard",
               "reviewApply", "reviewAcceptMinor", "reviewUseAlt", "reviewPlay"):
        assert re.search(r"(?:async )?function\s+" + fn + r"\s*\(", NEW_JS), \
            f"missing stepper fn: {fn}"
    # a row's review count / a tray review verb arms the stepper through the
    # same pendingOpen pattern the ask focus and search-hit seek use
    assert "pendingOpen={base:id,review:true}" in NEW_JS
    assert "MP.pendingReview" in NEW_JS


# ---------------------------------------------------------------------------
# Live-review corrections (2026-07-12): type floor, the tray's >8 flagged
# aggregate filtering the library, ready-row click-to-expand, and Ask
# reachability (row menu + in-shell search hits).
# ---------------------------------------------------------------------------
NEW_CSS = (STATIC / "app.css").read_text(encoding="utf-8")
NEW_PAGE = (STATIC / "page.html").read_text(encoding="utf-8")


def test_css_type_floor_is_13px():
    # DESIGN.md (2026-07-12): no type below 13px anywhere in the shell
    sizes = [float(m) for m in re.findall(
        r"font(?:-size)?:[^;{}]*?(\d+(?:\.\d+)?)px", NEW_CSS)]
    assert sizes, "no font sizes found -- the audit regex broke"
    assert min(sizes) >= 13, f"type floor broken: {min(sizes)}px < 13px"


def test_column_cap_is_fluid_boardroom_wide():
    # use-the-space rule (2026-07-12): ONE fluid cap -- min(1360px, 94vw) --
    # shared by body, the pinned ask bar, and the bulk bar; no surface keeps
    # a private width, and the fixed 968px reading column is retired
    assert "--colcap:min(1360px,94vw)" in NEW_CSS
    assert NEW_CSS.count("var(--colcap)") >= 3
    assert "968px" not in NEW_CSS and "908px" not in NEW_CSS
    assert "max-width:min(920px" not in NEW_CSS


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
    # the click guard: buttons/links/inputs/the review count/the click-to-edit
    # title and date never toggle
    assert re.search(
        r"closest\('button,a,input,select,textarea,label,\.rev,\.rname,\.rdate'\)",
        NEW_JS)
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
    # the hit opens in the shell; nothing navigates away to a deep link
    assert "onclick=\"location.href='/?open='" not in NEW_JS


def test_serif_retired_and_mono_demoted_to_true_data():
    # DESIGN.md Type (revised 2026-07-12): one neutral sans, hierarchy from
    # weight and size. No serif anywhere in the composed shell, and the unused
    # --serif token is gone from the sheet.
    page = _compose(STATIC)
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


# ---------------------------------------------------------------------------
# Builder C: THE EDIT LAYER. The inline flag stepper, segment editing
# everywhere, the voice-naming slide-over, and the removal of the last
# per-meeting bridges to the old page. Same regex-over-the-files style.
# ---------------------------------------------------------------------------
def test_np_keys_step_flags_and_guard_typing():
    # n/p walk the flagged segments from the page's document keydown handler,
    # but never while typing in an input/textarea/select (the same guard the
    # old review dialog used for its arrow keys)
    assert re.search(r"typing=ae&&.*INPUT\|TEXTAREA\|SELECT", NEW_JS)
    assert "e.key==='n'" in NEW_JS and "e.key==='p'" in NEW_JS
    assert re.search(r"!typing&&\(e\.key==='n'\|\|e\.key==='p'\)", NEW_JS), \
        "n/p must be gated on the typing guard"


def test_review_posts_carry_the_original_segment_index():
    # /api/transcript segments carry the ORIGINAL json index, not the array
    # position -- every review/edit POST sends that index plus the segment's
    # start as the server's cross-check. The stepper plumbs it.index from GET
    # /api/review; the editor plumbs g.index from the segment itself.
    assert "{base:MP.base,index:it.index,start:it.start,action}" in NEW_JS
    assert "index:g.index,start:g.start,action:'edit'" in NEW_JS
    assert "action:'delete',index:g.index,start:g.start" in NEW_JS
    assert "action:'split',index:g.index,start:g.start" in NEW_JS
    assert "action:'insert',start,end:start+3" in NEW_JS
    assert "action:'accept_minor'" in NEW_JS
    # the array position appears only in DOM ids (mseg<i>), never in a POST body
    assert not re.search(r"index:\s*i\b", NEW_JS)


def test_flag_strip_counts_update_optimistically_and_clear_at_zero():
    # the strip re-derives its counts from the client's own segments after
    # every resolving action (accept/save/accept-minor), and disappears at zero
    assert re.search(r"function\s+mSyncFlagStrip\s*\(", NEW_JS)
    assert 'id="mflagbar"' in NEW_JS and 'id="mflagminor"' in NEW_JS
    assert "bar.remove()" in NEW_JS
    # the client's minor test mirrors the server's review.is_minor exactly
    assert re.search(r"function\s+mIsMinor\s*\([^)]*\)\{return \(g\.end-g\.start\)<1\.0", NEW_JS)
    # resolved items leave the stepper's local list before any poll lands
    assert "MR.items.splice(MR.i,1)" in NEW_JS


def test_segment_editing_everywhere_with_house_confirms():
    # every segment gets the quiet pencil; gaps and the audio bar insert missed
    # lines; remove/split/re-transcribe live in the same inline card
    for fn in ("mEdit", "mEditSave", "mSplitUI", "mSplitSave", "mInsertAt",
               "mInsertSave", "mAddAtPlayhead", "mRetrans", "mDeleteAsk",
               "mDeleteGo", "mReloadSegs", "mPlaySpan", "mWireNew"):
        assert re.search(r"(?:async )?function\s+" + fn + r"\s*\(", NEW_JS), \
            f"missing editor fn: {fn}"
    assert "add line" in NEW_JS            # the gap affordance copy
    assert "line at playhead" in NEW_JS    # the audio bar affordance
    assert "/api/retranscribe" in NEW_JS
    assert "mlxwhisper:large-v3" in NEW_JS  # the engine choice is real
    # a structural change (insert/split/delete/merge) reloads the transcript so
    # fresh ORIGINAL indexes replace the stale ones
    assert NEW_JS.count("mReloadSegs(") >= 5
    # confirmations are the house two-step; native dialogs never appear
    for native in ("alert(", "confirm(", "prompt("):
        assert native not in NEW_JS, f"native dialog in the new shell: {native}"


def test_naming_panel_replaces_the_who_bridge():
    # the in-shell slide-over: per-meeting voice clips (with the
    # sources_deleted contract), enrolled-name autocomplete, Save name,
    # "Not a real speaker", Cancel
    assert 'id="namepanel"' in NEW_PAGE and 'id="nameveil"' in NEW_PAGE
    for fn in ("openNamePanel", "openNamePanelByUid", "closeNamePanel",
               "npSave", "npForget", "pnFilter", "pnRender", "pnPick", "pnKey"):
        assert re.search(r"(?:async )?function\s+" + fn + r"\s*\(", NEW_JS), \
            f"missing naming fn: {fn}"
    assert "'/api/voice_clips?speaker='" in NEW_JS
    # the save posts to /api/name in BOTH panel modes: by registry uid, and by
    # this-meeting cluster (the path that names a voice the registry does not
    # track — sub-floor 'Voice N' scraps and tombstone-suppressed voices)
    assert re.search(r"\{uid:NP\.uid,name:n,confirm:!!force\}", NEW_JS)
    assert re.search(r"\{meeting:NP\.meeting,speaker:NP\.speaker,name:n,confirm:!!force\}", NEW_JS)
    assert re.search(r"function\s+openNamePanelByCluster\s*\(", NEW_JS)
    # the meeting legend offers the cluster chip for unnamed, un-tracked voices
    assert "openNamePanelByCluster(" in NEW_JS and NEW_JS.count("openNamePanelByCluster(") >= 2
    assert re.search(r"api\('/api/forget',\{uid:NP\.uid\}\)", NEW_JS)
    # the enrollment quality gate answers with a warning: the panel renders
    # the house two-step confirm (numbers shown), never a native dialog
    assert 'id="npconfirm"' in NEW_JS and "npSave(true)" in NEW_JS
    # the dead-player case says WHY instead of rendering a player that can't load
    assert "r.reason==='sources_deleted'" in NEW_JS
    assert "No audio available. The source recordings were deleted." in NEW_JS
    # keyboard semantics ported from the old dialog's autocomplete
    assert "e.key==='ArrowDown'||e.key==='ArrowUp'" in NEW_JS
    # unknown voices in a meeting's legend open the panel (the U-label path)
    assert re.search(r"function\s+mUnknownUid\s*\(", NEW_JS)
    # after a save, the quiet relabel note rides the polled state as the
    # lowest-priority pill
    assert "s.relabel_pending" in NEW_JS


def test_meeting_header_gains_the_row_menu_and_a_cyclable_dot():
    # everything a row can do, the page can do: the same fillRowMenu feeds both
    assert re.search(r"function\s+mMenu\s*\(", NEW_JS)
    assert NEW_JS.count("fillRowMenu(") >= 3   # definition + row + page header
    # the header dot cycles with the same optimistic /api/set_category as rows
    assert re.search(r"function\s+mCycleCat\s*\(", NEW_JS)
    assert NEW_JS.count("api('/api/set_category'") >= 2
    assert 'id="mcatdot"' in NEW_JS
    # page-owning actions return to the library when their meeting disappears
    assert "if(route.view==='meeting'&&route.base===id)location.hash=''" in NEW_JS


# ---------------------------------------------------------------------------
# Builder D: THE DRAWER. The last surface -- one right slide-over holding
# Settings, Speakers, History, and Archive behind a pinned section nav.
# Same regex-over-the-files style as every block above.
# ---------------------------------------------------------------------------
def test_drawer_mounts_with_the_four_section_nav():
    # the slide-over and its veil are in the page; the nav and the four
    # lazy-rendered sections are built by app.js
    assert 'id="drawer"' in NEW_PAGE and 'id="drawerveil"' in NEW_PAGE
    for fn in ("openDrawer", "closeDrawer", "drawerGo", "drawDrawer",
               "drawerNavSync", "dSettingsDraw", "dSpeakersDraw",
               "dHistLoad", "dHistRender", "dArchLoad", "dArchRender"):
        assert re.search(r"(?:async )?function\s+" + fn + r"\s*\(", NEW_JS), \
            f"missing drawer fn: {fn}"
    for sec in ("dsec-settings", "dsec-speakers", "dsec-history", "dsec-archive"):
        assert sec in NEW_JS, f"missing drawer section mount: {sec}"
    for tab in ("Settings", "Speakers", "History", "Archive"):
        assert tab in NEW_JS, f"missing drawer nav tab: {tab}"
    # the Archive tab carries the "Archived · N" count when nonzero
    assert "archived_count" in NEW_JS


def test_gear_opens_the_drawer():
    assert re.search(r"\$\('#gear'\)\.onclick=\(\)=>openDrawer\(\)", NEW_JS)


def test_drawer_survives_polls_and_closes_the_house_ways():
    # the 2s poll enters through drawDrawer (called by render), which rebuilds
    # a section only when its signature changed and never under a focused field
    assert "drawDrawer(S)" in NEW_JS
    assert re.search(r"function\s+dFocusGuard\s*\(", NEW_JS)
    for fn in ("dSettingsSig", "dSpeakersSig"):
        assert re.search(r"function\s+" + fn + r"\s*\(", NEW_JS), f"missing sig fn: {fn}"
    assert NEW_JS.count("dataset.sig") >= 8   # tray + timeline + nav + drawer sections
    # closed by the veil, the x button, and Escape (deferring to the naming
    # panel and any open popover, which own the key first)
    assert 'onclick="closeDrawer()"' in NEW_PAGE
    assert "if(e.defaultPrevented)return" in NEW_JS
    assert "if(_popClose)return" in NEW_JS


def test_master_switch_gates_the_two_indented_triggers():
    # ONE master switch over the same /api/pause -- /api/resume state the pill
    # and the Process popover read (dTogMaster reuses ppPause's endpoints)
    assert "Automatic runs" in NEW_JS
    assert re.search(r"function\s+dTogMaster[^}]*api\(S\.paused\?'/api/resume':'/api/pause'",
                     NEW_JS), "the master switch must be the shared pause state"
    # watch + nightly sit indented beneath it and read visibly inert when off
    assert "off while automatic runs are paused" in NEW_JS
    assert "dindent" in NEW_JS and ".dindent.inert" in NEW_CSS
    # both triggers still POST /api/automation; the time picker is an in-drawer
    # subview posting /api/schedule (no dialog)
    assert "api('/api/automation',{watch:!S.schedule.watch})" in NEW_JS
    assert "api('/api/automation',{nightly:!S.schedule.nightly})" in NEW_JS
    assert re.search(r"api\('/api/schedule',\{hour:\+\$\('#dsh'\)\.value,minute:\+\$\('#dsm'\)\.value\}\)",
                     NEW_JS)
    assert "dialog" not in NEW_PAGE.lower(), "the new shell must not grow a <dialog>"


def test_cloud_keys_subview_never_renders_key_values():
    # mirror of test_server_security's never-echoed contract, client-side: the
    # server only sends presence booleans, and the drawer's password fields
    # never carry a value attribute -- so no key can be rendered anywhere
    m = re.search(r'<input type="password"[^>]*>', NEW_JS)
    assert m, "cloud-keys subview lost its password fields"
    assert "value=" not in m.group(0), "a password field must never render a value"
    # no template interpolation ever feeds cloud_keys state into an input value
    assert not re.search(r'value="[^"]*cloud_keys', NEW_JS)
    # presence booleans drive the saved tick and the placeholder only
    assert "saved: paste to replace" in NEW_JS
    assert re.search(r"const has=!!\(\(s\.cloud_keys\|\|\{\}\)\[prov\]\)", NEW_JS)
    # save and clear reuse the exact endpoint; the response's fresh presence
    # map replaces local state (never the keys themselves)
    assert "api('/api/cloud_keys',{scribe:" in NEW_JS
    assert "api('/api/cloud_keys',{clear:[prov]})" in NEW_JS
    assert "S.cloud_keys=r.set" in NEW_JS
    # cloud engines are listed in the model picker only when keyed
    assert re.search(r"filter\(c=>!c\.cloud\|\|\(s\.cloud_keys\|\|\{\}\)\[c\.cloud\]\)", NEW_JS)


def test_settings_ports_the_remaining_flyout_rows():
    # model + assistant pickers, punctuation toggle, update check, calibration
    # note, and both folder Change... rows -- all the old flyout's endpoints
    for ep in ("/api/model", "/api/llm_backend", "/api/punctuate",
               "/api/check_updates", "/api/pick_folder", "/api/mic_speaker",
               "/api/fix_recorder_permissions"):
        assert ep in NEW_JS, f"settings section never calls {ep}"
    # the recorder your-voice picker is an inline enrolled-name select, not the
    # old page's native prompt (native dialogs are banned shell-wide above)
    assert 'id="dmicsel"' in NEW_JS
    assert "All models are current." in NEW_JS
    assert "Estimates use factory measurements until a few runs complete." in NEW_JS


def test_speaker_management_moves_into_the_drawer():
    for ep in ("/api/rename_speaker", "/api/merge_speakers", "/api/remove_speaker",
               "/api/remove_sample", "/api/reassign_sample", "/api/hide_unknown",
               "/api/snippet"):
        assert ep in NEW_JS, f"speakers section never calls {ep}"
    # "Who is this?" reuses the EXISTING naming slide-over, never a duplicate:
    # tray voices + meeting legend + the drawer all call the one opener
    assert NEW_JS.count("openNamePanelByUid(") >= 4
    # openNamePanel + ...ByUid + ...ByCluster (the untracked-voice path)
    assert NEW_JS.count("function openNamePanel") == 3
    # hidden unknowns fold behind an "N hidden" expandable with Restore
    assert "} hidden" in NEW_JS and "Restore" in NEW_JS
    # the person Remove is the house two-step (armed confirm, then the POST)
    assert re.search(r"function\s+dRemoveAsk\s*\(", NEW_JS)
    assert re.search(r"async function\s+dRemoveGo\s*\(", NEW_JS)
    # snippet playback is exclusive and keyed so rebuilds keep the stop glyph
    assert re.search(r"function\s+dvPlay\s*\(", NEW_JS) and "dvSync()" in NEW_JS
    # edits refresh() so the relabel spawned server-side rides the quiet pill
    assert "Applying names to all transcripts" in NEW_JS


def test_history_section_ports_the_flyout_semantics():
    # day groups newest first, name filter + all/processed/failed select,
    # failures keep their FULL error text, capped at 400 with a note
    assert "DHIST_CAP=400" in NEW_JS
    assert "Showing the first ${DHIST_CAP}" in NEW_JS
    assert 'id="dhistq"' in NEW_JS and 'id="dhistok"' in NEW_JS
    assert '<option value="ok">processed</option>' in NEW_JS
    assert '<option value="fail">failed</option>' in NEW_JS
    assert "api('/api/history')" in NEW_JS
    # the failure text renders whole (word-broken, never truncated)
    assert "dhsum" in NEW_JS and ".dhsum{word-break:break-word" in NEW_CSS


def test_archive_section_restores_and_deletes_with_house_confirms():
    assert "api('/api/archived')" in NEW_JS
    assert "api('/api/restore_meeting',{base})" in NEW_JS
    assert "api('/api/delete_meeting',{base,confirm:true})" in NEW_JS
    assert "Nothing archived." in NEW_JS
    # delete is a two-step armed inside the drawer (never a native dialog --
    # banned shell-wide by the editor test above)
    assert re.search(r"function\s+dArchDelAsk\s*\(", NEW_JS)
    assert "Delete forever" in NEW_JS
    # bulk/row Archive actions leave the quiet "archived · view" hint that
    # opens the drawer's Archive section
    assert re.search(r"function\s+archHint\s*\(", NEW_JS)
    assert NEW_JS.count("archHint()") >= 2      # row menu + bulk bar
    assert "openDrawer('archive')" in NEW_JS


# ---------------------------------------------------------------------------
# Finale: the keyboard layer and drag-and-drop queueing -- the last build pass
# before the default flips. Same regex-over-the-files style as every block
# above; the upload extension allowlist is asserted EQUAL client and server.
# ---------------------------------------------------------------------------
def _kb_body():
    a = NEW_JS.index("function kbKey(e){")
    b = NEW_JS.index("document.addEventListener('keydown',kbKey)")
    return NEW_JS[a:b]


def test_keyboard_layer_is_one_dispatcher_behind_the_typing_guard():
    # ONE dispatcher, registered ONCE; every new shortcut lives inside it
    assert NEW_JS.count("addEventListener('keydown',kbKey)") == 1
    body = _kb_body()
    # the typing guard: no shortcut fires while any input has focus...
    assert re.search(r"typing=ae&&\(/\^\(INPUT\|TEXTAREA\|SELECT\)\$/", body)
    assert "if(typing)return" in body
    # ...except the search-field Escape, handled BEFORE the guard
    assert body.index("ae===se") < body.index("if(typing)return")
    # the handlers: / focuses search, j/k walk, Enter acts, e peeks
    for frag in ("e.key==='/'", "e.key==='j'||e.key==='k'",
                 "e.key==='Enter'", "e.key==='e'"):
        assert frag in body, f"missing shortcut: {frag}"


def test_dispatcher_defers_the_meeting_page_keys_to_mkey():
    body = _kb_body()
    # on the meeting page the dispatcher handles ONLY the Escape cascade's
    # last step and returns before any letter key: n/p stepping (and the
    # page's slash) stay mKey's alone, so nothing double-fires
    m = re.search(r"if\(route\.view==='meeting'\)\{(.*?)\n  \}", body, re.S)
    assert m, "the dispatcher lost its meeting-page bow-out"
    assert "'n'" not in m.group(1) and "'p'" not in m.group(1)
    assert body.index("route.view==='meeting'") < body.index("e.key==='/'")
    # back-to-list fires only with the WHOLE cascade idle (popover, naming
    # panel, drawer, stepper, edit card)
    for guard in ("_popClose", "NP", "DRAWER.open", "MR&&MR.active", "mcard"):
        assert guard in m.group(1), f"Escape-to-list must defer to {guard}"
    assert "location.hash=''" in m.group(1)


def test_slash_focuses_search_and_escape_clears_back_to_the_list():
    body = _kb_body()
    assert "se.focus()" in body            # '/' hands focus to the search field
    # Escape IN the field clears it, re-renders, and puts the ring on the list
    esc = body[body.index("ae===se"):body.index("const typing")]
    assert "se.value=''" in esc and "render()" in esc and "scheduleSearch()" in esc
    assert "kbFocusRow(kbRows()[0])" in esc


def test_jk_walk_rows_with_a_ring_that_survives_polls():
    # rows are already tabindex=0 (the ring reuses :focus-visible); group
    # headers are skipped by construction -- only .row[tabindex] is collected,
    # so synthetic upload rows (no tabindex) are skipped too
    assert "querySelectorAll('#timeline .row[tabindex]')" in NEW_JS
    assert re.search(r"function\s+kbMove\s*\(", NEW_JS)
    # the focused row scrolls into view through the page's own scroll-behavior
    # convention (html smooth, auto under reduced motion): no explicit
    # 'smooth' that would override the reduced-motion kill
    m = re.search(r"function kbFocusRow.*?\n\}", NEW_JS, re.S)
    assert m and "scrollIntoView({block:'nearest'})" in m.group(0)
    # the ring clears the sticky header and the floating bulk bar
    assert re.search(r"\.row\{scroll-margin-top:84px", NEW_CSS)
    # a 2s rebuild wipes the DOM: afterRender puts the ring back from the
    # remembered id, and a real click clears the memory
    assert re.search(r"function\s+kbRestore\s*\(", NEW_JS)
    after = NEW_JS[NEW_JS.index("function afterRender"):]
    assert "kbRestore()" in after[:200]
    assert "addEventListener('mousedown',()=>{kbLast=null;})" in NEW_JS


def test_enter_acts_by_row_state_and_e_peeks():
    body = _kb_body()
    assert "if(st==='ready')openMeeting(id)" in body
    assert "else if(st==='needs_name')acceptMeeting(id)" in body
    # waiting/held/failed: Enter hands focus to the first revealed action
    # (focus-within already shows the set)
    assert ".ractions .iact" in body
    assert "e.key==='e'&&st==='ready'" in body and "toggleExpand(id)" in body
    # Enter/e act on the focused ROW itself, never a focused button inside it
    assert "e.target.matches('#timeline .row[tabindex]')" in body


def test_drop_overlay_markup_rides_the_theme_tokens():
    assert 'id="dropveil"' in NEW_PAGE
    assert "Drop audio to queue it" in NEW_PAGE
    # dashed hairline inset on the accent; pointer-events none so the drop
    # lands on the window handler; tokens carry both themes (no raw colors)
    m = re.search(r"\.dropveil\{[^}]*\}", NEW_CSS, re.S)
    assert m
    assert "dashed var(--accent)" in m.group(0)
    assert "pointer-events:none" in m.group(0)
    assert "#" not in m.group(0).replace("color-mix", ""), \
        "the overlay must use theme tokens, not hardcoded colors"
    # dragenter/over show it, leave/drop hide it; dragover preventDefaults
    for ev in ("dragenter", "dragover", "dragleave", "drop"):
        assert f"addEventListener('{ev}'" in NEW_JS, f"missing window {ev} handler"


def test_upload_wiring_posts_the_raw_file_body_sequentially():
    # POST /api/upload?name=<filename> with the raw File as the body
    assert "fetch('/api/upload?name='+encodeURIComponent(file.name)" in NEW_JS
    assert "{method:'POST',body:file}" in NEW_JS
    # sequential: one awaited upload per file
    assert "for(const f of audio)await uploadOne(f)" in NEW_JS
    # the synthetic row is keyed and yields to the REAL waiting row: a done
    # entry is pruned the moment src:<final name> appears in the polled state
    # (the same rebuild adds one and drops the other, so no flicker)
    assert "const UPLOADS=[]" in NEW_JS
    assert "rowById('src:'+u.name)" in NEW_JS
    for fn in ("uploadRowHTML", "uploadsPrune", "uploadDismiss", "uploadFiles"):
        assert re.search(r"(?:async )?function\s+" + fn + r"\s*\(", NEW_JS), \
            f"missing upload fn: {fn}"
    # uploads fold into the timeline signature so their rows survive rebuilds
    assert "uploadsSig()" in NEW_JS
    # an error row shows the server's reason with a dismiss x; non-audio drops
    # get the inline dismissible note instead
    assert "esc(u.error)" in NEW_JS
    assert 'id="dropnote"' in NEW_PAGE
    assert re.search(r"function\s+dropNote\s*\(", NEW_JS)


def test_upload_ext_const_mirrors_the_server_allowlist():
    # the client's accepted-extension const IS the server's (config.AUDIO_EXTS,
    # the folder watcher's own list): widening one without the other fails here
    from stt import config
    m = re.search(r"const UPLOAD_EXTS=\[([^\]]+)\]", NEW_JS, re.S)
    assert m, "UPLOAD_EXTS const missing from the new shell"
    exts = set(re.findall(r"'(\.[a-z0-9]+)'", m.group(1)))
    assert exts == config.AUDIO_EXTS, \
        f"client/server allowlists diverge: {sorted(exts ^ config.AUDIO_EXTS)}"
    # the rejection note names the accepted list from this same const
    assert "UPLOAD_EXTS.join(', ')" in NEW_JS


def test_library_empty_state_regains_the_drop_line():
    line = "Record a meeting from the menu bar, or drop an audio file here."
    assert line in NEW_JS
    # DESIGN.md's empty-state sentence reached its final form with the endpoint
    design = (Path(srv.__file__).resolve().parent / "DESIGN.md").read_text(encoding="utf-8")
    assert "or drop an audio file here." in design
    assert "the drop line returns when the upload endpoint ships" not in design


# ---------------------------------------------------------------------------
# Deploy-sweep fixes (2026-07-12): the recorder outcome note and queued panel
# runs -- both already carried by /api/state and previously ignored by the
# shell. Same regex-over-the-files style as every block above.
# ---------------------------------------------------------------------------
def test_failed_recorder_note_is_the_top_tray_item():
    # a FAILED note renders as its own tray kind, ABOVE the stall lines
    body = NEW_JS[NEW_JS.index("function drawTray"):NEW_JS.index("tray.innerHTML=h")]
    assert "s.recorder_note&&!s.recorder_note.ok" in body
    assert body.index('tw-title">Recorder') < body.index("trayAct('recorder_stall'"), \
        "the recorder note must outrank the stall line"
    # the note folds into the tray signature so polls neither drop nor thrash it
    assert re.search(r"note\?note\.at", body)
    # Fix permissions: same endpoint + in-flight label as the stall item...
    m = re.search(r"if\(kind==='recorder_note'\)\{(.*?)\n  \}", NEW_JS, re.S)
    assert m, "trayAct lost its recorder_note branch"
    assert "'/api/fix_recorder_permissions'" in m.group(1)
    assert "Resetting…" in m.group(1) and "Retry" in m.group(1)
    # ...shown behind the old strip's microphone/audio gate
    assert "/[Mm]icrophone|audio/" in body
    # the x dismisses with the old page's exact endpoint and payload
    assert "api('/api/recorder_note',{clear:true}).then(refresh)" in body


def test_saved_recorder_note_is_a_quiet_line_not_a_tray_ask():
    # the success line mounts right under the header, BEFORE the tray
    assert 'id="recok"' in NEW_PAGE
    assert NEW_PAGE.index('id="recok"') < NEW_PAGE.index('id="tray"')
    # rendered only for an OK note, with the old strip's check prefix
    m = re.search(r"function drawRecOk[\s\S]*?\n\}", NEW_JS)
    assert m, "drawRecOk missing"
    assert "rn&&rn.ok" in m.group(0)
    assert "'✓ '+rn.text" in m.group(0)
    # sub-styled, never amber; render() draws it each poll so the server-side
    # TTL (stt/status.py) is what makes it disappear -- no client timer, no x
    css = re.search(r"\.recok\{[^}]*\}", NEW_CSS)
    assert css and "var(--sub)" in css.group(0) and "--amber" not in css.group(0)
    assert "drawRecOk(S)" in NEW_JS
    assert not re.search(r"setTimeout\([^)]*recorder_note", NEW_JS), \
        "a success note expires server-side, never by a client timer"


def test_queued_runs_render_in_the_popover_with_unqueue():
    # the Process popover's block: one row per job, both status copies per the
    # old page's logic (running -> waits; idle -> the kick is imminent)
    assert "Queued runs" in NEW_JS
    assert "starts after the current run" in NEW_JS
    assert "starting&#8230;" in NEW_JS
    # cancel POSTs the same {at} payload the server's /api/unqueue reads,
    # after an optimistic removal (the row and the pill count drop pre-poll)
    assert re.search(r"api\('/api/unqueue',\{at\}\)", NEW_JS)
    m = re.search(r"function ppUnqueue[\s\S]*?\n\}", NEW_JS)
    assert m and re.search(r"S\.queued_jobs=\(S\.queued_jobs\|\|\[\]\)\.filter", m.group(0))
    # the pill carries the queued count while a run is active
    assert re.search(r"const qj=\(s\.queued_jobs\|\|\[\]\)\.length", NEW_JS)
    assert "${qj} queued" in NEW_JS


def test_process_popover_refreshes_with_the_poll_while_open():
    # the drawer's signature pattern: render() enters through drawProcessPop,
    # which rebuilds only on a changed signature, never under a focused field,
    # and never by closing the popover or moving focus
    assert "drawProcessPop(S)" in NEW_JS
    for fn in ("ppSig", "drawProcessPop", "ppUnqueue"):
        assert re.search(r"function\s+" + fn + r"\s*\(", NEW_JS), f"missing fn: {fn}"
    m = re.search(r"function drawProcessPop[\s\S]*?\n\}", NEW_JS)
    assert "dataset.sig===sig" in m.group(0)
    assert "dFocusGuard(pop)" in m.group(0)
    # queued jobs fold into the signature so a new job redraws an open popover
    sig = re.search(r"function ppSig[\s\S]*?\n\}", NEW_JS).group(0)
    assert "queued_jobs" in sig


# ---------------------------------------------------------------------------
# UI bug fixes (2026-07-12): hover actions must never overlay row text, and
# the old page's click-to-edit title/date returns on ready rows.
# ---------------------------------------------------------------------------
def test_hover_actions_join_the_flow_instead_of_overlaying():
    # the action cluster is display-gated INTO the row's flow when shown --
    # never an absolutely positioned overlay that can sit on top of the meta
    m = re.search(r"(?m)^\.ractions\{[^}]*\}", NEW_CSS, re.S)
    assert m, ".ractions rule missing"
    assert "display:none" in m.group(0)
    assert "position:absolute" not in m.group(0)
    assert re.search(
        r"\.row:hover \.ractions,\.row:focus-within \.ractions,"
        r"\.row\.acting \.ractions\{\s*display:flex", NEW_CSS)
    # the resting state text fully yields its SPACE (display, not visibility)
    assert re.search(
        r"\.row:hover \.yields,\.row:focus-within \.yields,"
        r"\.row\.acting \.yields\{display:none\}", NEW_CSS)
    # a row keeps its actions while its ⋯ menu / delete confirm is open (the
    # pointer is in the popover, so :hover alone would drop them and zero the
    # open menu's anchor rect); the poll's re-anchor restores the class first
    assert re.search(r"function\s+rowActing\s*\(", NEW_JS)
    assert "rowActing(rm.dataset.rowid)" in NEW_JS


def test_click_to_edit_title_and_date_on_ready_rows():
    # the quick path is back: clicking the title / the meta's date edits inline
    for fn in ("rowTitleEdit", "rowDateEdit"):
        assert re.search(r"function\s+" + fn + r"\s*\(", NEW_JS), f"missing: {fn}"
    # payload shapes match the server contract (the old page's inline editors)
    assert "api('/api/rename',{base:id,new:nm})" in NEW_JS
    assert re.search(r"api\('/api/set_date',\{base:id,date:d\}\)", NEW_JS)
    # failures surface inline; native dialogs stay banned shell-wide
    for native in ("alert(", "confirm(", "prompt("):
        assert native not in NEW_JS, f"native dialog in the new shell: {native}"
    # the targets read as editable (edit cursor + subtle underline), per the
    # old page's .mtitle/.mdate hover treatment translated to Signal tokens
    assert re.search(r"\.rname,\.rdate\{cursor:text\}", NEW_CSS)
    assert re.search(r"\.rname:hover,\.rdate:hover\{text-decoration:underline", NEW_CSS)
    # the meeting page header title renames inline through the same path
    assert "rowTitleEdit(event,'${escJs(base)}')" in NEW_JS


# ---------------------------------------------------------------------------
# Flag review, second pass (2026-07-12): the sticky verdict bar (stepping
# controls that never travel with the inline card), the Enter/a/u fast path,
# the honest accept-all two-step, and the conveyor/workspace rule -- BAR
# verdicts advance, CARD work holds position. Same regex-over-the-files style.
# ---------------------------------------------------------------------------
def _js_fn(name):
    """A top-level function's full source (house convention: bodies close with
    a brace at column 0, nested closers are indented)."""
    m = re.search(r"(?:async )?function " + re.escape(name) + r"\([\s\S]*?\n\}",
                  NEW_JS)
    assert m, f"missing function: {name}"
    return m.group(0)


def test_review_bar_docks_sticky_with_the_verdict_controls():
    for fn in ("_mRevBar", "reviewBarMount", "reviewBarUnmount", "reviewBarSync",
               "reviewSave", "reviewUseAltGo", "reviewAcceptAllAsk",
               "reviewAcceptAllGo"):
        assert re.search(r"(?:async )?function\s+" + fn + r"\s*\(", NEW_JS), \
            f"missing fn: {fn}"
    # the bar is sticky; mStickyTop docks it under the audio bar from the LIVE
    # header/audio heights (under the shell header when the meeting is audioless)
    m = re.search(r"(?m)^\.mrevbar\{[^}]*\}", NEW_CSS, re.S)
    assert m and "position:sticky" in m.group(0)
    sticky = _js_fn("mStickyTop")
    assert "mrevbar" in sticky and "offsetHeight" in sticky
    mount = _js_fn("reviewBarMount")
    assert "maudio" in mount and ".mhead" in mount
    # mounting swaps the strip for the bar; exiting restores the strip
    assert "strip.hidden=true" in mount
    assert "strip.hidden=false" in _js_fn("reviewBarUnmount")
    bar = _js_fn("_mRevBar")
    # Accept is the ONE primary accent button in the bar...
    assert bar.count("btn primary mini") == 1
    assert "onclick=\"reviewApply('accept')\"" in bar
    # ...alongside the arrows, play, use-alt, skip, exit, and the two bulk verbs
    for frag in ('onclick="reviewGo(-1)"', 'onclick="reviewGo(1)"',
                 'onclick="reviewPlay()"', 'onclick="reviewUseAltGo()"',
                 'onclick="reviewExit()"', 'id="mrbcount"', 'id="mrbalt"',
                 'id="mrbminor"', 'id="mrball"'):
        assert frag in bar, f"bar lost a control: {frag}"
    # key hints ride the title attributes only, never visible clutter
    for hint in ("(p)", "(n)", "(Enter or a)", "(u)", "(Escape)"):
        assert hint in bar, f"missing title-attribute key hint: {hint}"
    # the bar re-derives its counts after every resolution; Use alt enables
    # only when the current flag carries second-engine text
    sync = _js_fn("reviewBarSync")
    assert "alt.disabled=!(it&&it.alt&&it.alt.length)" in sync
    assert "Accept ${minors} minor" in sync
    assert "Accept all ${n} remaining" in sync
    # stepping wraps at both ends instead of silently exiting past the last
    assert "%MR.items.length" in _js_fn("reviewGo")


def test_stepping_fast_path_keys_guard_typing_and_popovers():
    mkey = _js_fn("mKey")
    # Enter / a / u live behind the typing guard, modifier-free, and defer to
    # any open popover (the two-step confirms own their own keys)
    assert re.search(
        r"!typing&&!e\.metaKey&&!e\.ctrlKey&&!e\.altKey&&MR&&MR\.active&&!_popClose&&!NP"
        r"\s*&&\(e\.key==='Enter'\|\|e\.key==='a'\|\|e\.key==='u'\)", mkey), \
        "the fast path must sit behind the typing/modifier/popover/panel guards"
    # Enter defers to a focused button; Enter and a accept-and-advance
    assert "e.key==='Enter'&&ae&&ae.tagName==='BUTTON'" in mkey
    assert "reviewApply('accept')" in mkey
    # u fires only when the current flag carries second-engine text
    assert "it.alt&&it.alt.length" in mkey and "reviewUseAltGo()" in mkey
    # Cmd/Ctrl+Enter commits the open card from anywhere in it (plain Enter in
    # the textarea stays a newline, kept there by the typing guard)
    assert re.search(r"\(e\.metaKey\|\|e\.ctrlKey\)&&e\.key==='Enter'"
                     r"&&document\.getElementById\('mcard'\)", mkey)
    # Escape still exits stepping ahead of the page cascade, yielding to the
    # naming panel and any open popover first
    assert re.search(r"e\.key==='Escape'&&!typing&&!NP&&!_popClose", mkey)
    assert "reviewExit()" in mkey


def test_accept_all_remaining_is_an_honest_house_two_step():
    ask = _js_fn("reviewAcceptAllAsk")
    # the confirm is the house popover two-step, never a native dialog (those
    # are banned shell-wide by the editor test above)
    assert "openPop(pop,btn" in ask and "ppconfirm" in ask
    # the copy is honest about what accepting unheard lines means
    assert "kept exactly as transcribed" in ask
    assert "trusted as-is" in ask
    go = _js_fn("reviewAcceptAllGo")
    # every remaining flag resolves through the EXISTING accept action, one
    # awaited POST per item, the bar's count dropping as each lands
    assert "action:'accept'" in go and "await api('/api/review'" in go
    assert "reviewBarSync()" in go
    # a failure surfaces in the popover and leaves the rest flagged
    assert "The rest stay flagged." in go
    # done: stepping exits (the strip returns, empty) and the poll follows
    assert "reviewExit()" in go and "refresh()" in go


def test_bar_verdicts_advance_and_card_work_holds_position():
    # the card's Save is reviewSave (hold), and the card sheds every control
    # the bar now owns: no prev/next/skip/accept duplicates, no counter
    card = _js_fn("renderReviewCard")
    assert 'onclick="reviewSave()"' in card
    for gone in ("reviewGo(", "reviewApply(", "Accept as-is", "Skip", "mcpos"):
        assert gone not in card, f"the card still carries a bar control: {gone}"
    # the card keeps its workspace: flag reason, alt preview, speaker + text
    for kept in ("mcflag", "mcalt", "mSpkSelect", "mctext"):
        assert kept in card, f"the card lost its workspace piece: {kept}"
    # Save / split / remove / insert never call the advance path
    for fn in ("reviewSave", "mEditSave", "mSplitSave", "mDeleteGo",
               "mInsertSave"):
        body = _js_fn(fn)
        assert "reviewGo(" not in body and "renderReviewCard(" not in body, \
            f"{fn} must hold position, never advance"
    # a card save resolves the item WITHOUT moving: the hold flag arms and
    # MR.i comes to name the next unresolved item for the bar to take later
    save = _js_fn("reviewSave")
    assert "MR.hold=true" in save and "MR.items.splice(k,1)" in save
    # bar verdicts DO advance, wrapping to the first when the last resolves
    apply_ = _js_fn("reviewApply")
    assert "renderReviewCard()" in apply_ and "MR.items.splice(MR.i,1)" in apply_
    assert re.search(r"if\(MR\.i>=MR\.items\.length\)MR\.i=0", apply_)
    # one review POST in flight at a time: a second Enter (or an n mid-save)
    # can never double-resolve or shift MR.i under the splice
    assert "MR.busy" in apply_ and "MR.busy" in save
    assert "MR.busy" in _js_fn("reviewGo")
    # accept-minor from the bar never rebuilds a surviving card mid-edit
    assert "NEVER rebuild" in _js_fn("reviewAcceptMinor")


def test_structural_reload_restores_scroll_and_stepping_position():
    body = _js_fn("mReloadSegs")
    # scroll: captured before the rebuild, restored after (no jump)
    assert "window.scrollY" in body and "_instantScroll(y)" in body
    # stepping: the review list is refetched (a structural change shifted its
    # snapshot indexes) and the position re-anchors by START TIME -- the next
    # flag at or below the held spot in document order -- never auto-advanced
    assert "api('/api/review?base='" in body
    assert "x.start>=a-0.25" in body
    assert "MR.hold=true" in body
    # the editor and the gap insert no longer kill stepping (still one card
    # at a time: mCardEl swaps the open card)
    for fn in ("mEdit", "mInsertAt"):
        assert "reviewExit()" not in _js_fn(fn), f"{fn} must not exit stepping"
    # an edit that resolves the current flag drops it from the walk in place
    assert "MR.hold=true" in _js_fn("mEditSave")


def test_stepper_walks_flags_in_document_order():
    # the server's list fronts substantial flags (grouped by weight); the
    # stepper re-sorts to DOCUMENT order -- start time, original index as the
    # tiebreak -- immediately after the fetch and after every reload, so
    # prev/next and the counter read the transcript top to bottom
    order = _js_fn("_revOrder")
    assert re.search(r"a\.start-b\.start\)\|\|\(a\.index-b\.index", order)
    assert "_revOrder(d.items)" in _js_fn("reviewStart")
    assert "_revOrder(rv.items)" in _js_fn("mReloadSegs")
    # entering from the strip/chip starts at the FIRST flag on the page
    assert "reviewStart(0)" in NEW_JS


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_stepper_orders_fabricated_out_of_order_flags_by_start_time():
    # fabricate a server-style list (substantial first, minors last: NOT
    # document order, with a same-start tie) and assert the walk sequence
    # comes back sorted by start time, original index breaking the tie
    src = _js_fn("_revOrder")
    fixture = ("const items=[{start:41.0,index:9},{start:7.5,index:2},"
               "{start:7.5,index:1},{start:19.2,index:5}];"
               "console.log(JSON.stringify(_revOrder(items).map(x=>x.index)));")
    r = subprocess.run([NODE, "-e", src + "\n" + fixture],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[1,2,5,9]", \
        f"stepper order is not document order: {r.stdout.strip()}"


def test_boardroom_tokens_shipped_and_signal_retired():
    # DESIGN.md (2026-07-12): the Boardroom Wide colorway. Monday violet-blue
    # accent on a cool gray ground; white cards; states in colored pills.
    # Status/chip tokens are the COMPUTED-contrast pairs: ok and personal
    # darkened hue-true from the spec's first draft (3.06:1 and 3.82:1 on
    # their softs), and accent TEXT gets its own darker token (#6161FF is
    # 4.21:1 as text on the ground) while #6161FF keeps fills/edges/focus.
    for tok in ("#6161FF", "#F6F7FB", "#4B4FD9", "#1E7B45", "#E8F6EE",
                "#2264D1", "#E8EFFC", "#A05E0C", "#FFF1E5", "#8F5F10",
                "#CC3F38", "#BE3A33", "#131417", "#1B1D23", "#8F94FF"):
        assert tok in NEW_CSS, f"Boardroom token missing: {tok}"
    for old in ("#4F5DE5", "#7B87FF", "#FAFAFC", "#0F1114",
                "#1E6B50", "#43B28A", "#FBFBF9", "#121416"):
        assert old not in NEW_CSS, f"retired Signal/Newsprint token: {old}"
    # the Signal glow touches retired with their colorway: no glow shadows,
    # no breathing pill; the focus ring is the plain accent outline
    assert "0 0 0 3px color-mix" not in NEW_CSS
    assert "0 0 6px color-mix" not in NEW_CSS
    assert "pillbreathe" not in NEW_CSS
    assert "outline:2px solid var(--accent)" in NEW_CSS
    # states speak in pills, categories in chips, and the primary button
    # is pill-shaped under the soft accent shadow
    for cls in (".spill{", ".spill.ok{", ".spill.busy{", ".spill.rec{",
                ".catchip{", ".catchip.work{", ".catchip.personal{"):
        assert cls in NEW_CSS, f"missing Boardroom class: {cls}"
    assert "0 4px 14px color-mix(in srgb,var(--accent) 30%" in NEW_CSS
    # rows are floating cards: 14px radius, 10px gaps, hover lift; attention
    # items wear their colored left edge (amber tray, accent naming row)
    assert re.search(r"(?m)^\.row\{[^}]*border-radius:14px", NEW_CSS)
    assert re.search(r"(?m)^\.row\{[^}]*margin:0 0 10px", NEW_CSS)
    assert ".row:hover{box-shadow:var(--cardsh-hov)}" in NEW_CSS
    assert re.search(r"(?m)^\.tray\{[^}]*border-left:4px solid var\(--amber\)", NEW_CSS)
    assert re.search(r'needs_name"\]\{[^}]*border-left:4px solid var\(--accent\)', NEW_CSS)


# ---------------------------------------------------------------------------
# Boardroom Wide (2026-07-12): the vendored UI face. Figtree (OFL) is served
# by the panel itself from gui/static/fonts/ -- never a CDN -- behind a
# strict basename allowlist.
# ---------------------------------------------------------------------------
FONTS = STATIC / "fonts"


def test_figtree_is_vendored_with_its_license():
    names = sorted(p.name for p in FONTS.glob("*.woff2"))
    assert "figtree-latin.woff2" in names, "the latin variable woff2 is missing"
    assert (FONTS / "OFL.txt").exists(), "OFL.txt must ship alongside the font"
    for name in names:  # every vendored file is a real woff2
        assert (FONTS / name).read_bytes()[:4] == b"wOF2", f"{name} is not woff2"


def test_font_route_serves_woff2_and_refuses_traversal(running_server):
    for name in sorted(p.name for p in FONTS.glob("*.woff2")):
        status, ctype, body = _get(running_server, f"/static/fonts/{name}")
        assert status == 200
        assert ctype == "font/woff2"
        assert body == (FONTS / name).read_bytes()
    # strict allowlist: traversal names, encoded traversal, and anything not
    # a vendored woff2 (even the license that IS in the directory) all 404
    for bad in ("../app.css", "..%2Fapp.css", "%2e%2e/app.css",
                "OFL.txt", "nope.woff2", ""):
        status, _ctype, _body = _get(running_server, "/static/fonts/" + bad)
        assert status == 404, f"font route must refuse {bad!r}"


def test_figtree_fontface_and_stack_ride_the_composed_page():
    page = _compose(STATIC)
    assert "@font-face" in page
    assert "font-family:'Figtree'" in page
    assert "font-weight:300 900" in page          # the variable axis, whole
    assert "font-display:swap" in page
    assert "/static/fonts/figtree-latin.woff2" in page
    # Figtree fronts the UI stack; -apple-system stays the fallback
    assert "--sans:'Figtree',-apple-system" in NEW_CSS
