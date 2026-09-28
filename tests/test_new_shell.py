"""The panel shell: the ONE composed page (gui/static/), assembled by the
marker/mtime composer and served at /.

Structure and regex over the composed page, plus live byte-compares proving
the routing serves exactly what the composer builds.
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from gui import server as srv
from conftest import SPLIT_VOICE_BASE, SPLIT_VOICE_UID, split_voice_meeting

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
               "ready", "failed"):
        assert f'data-state="{st}"' in page, f"missing state hook: {st}"
    # needs_name lost its tall row and its CSS hook (docs/ui-refresh phase 2):
    # the state is a compact To finish row in either sort, never a form
    assert "if(row.state==='needs_name')return finishRowHTML(row);" in page
    assert "r.state==='needs_name'||toFinish(r,now)" in page


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
# (trayExpand and acceptMeeting retired with the tray band and the tall naming
# row in docs/ui-refresh phase 2: the To finish actions and the finish strip's
# Title step replaced them)
SEAMS = ["toggleProcess", "scheduleSearch", "trayAct", "rowListen",
         "rowHold", "rowRelease", "rowProcess", "rowDelete", "rowRetry",
         "openMeeting", "rowMenu", "openReviewBadge",
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


def _narrow_block():
    """The shell's phone-width block: the @media(max-width:720px) rule set
    that opens with the body gutter (the meeting page keeps its own)."""
    a = NEW_CSS.index("@media(max-width:720px){\n  body{")
    return NEW_CSS[a:NEW_CSS.index("\n}\n", a)]


def test_phone_width_forces_no_sideways_scroll_and_cuts_no_word():
    """QA, phase 2: at 420px the two-segment pill ran 33px past the edge (a
    forced sideways scroll) and the search field cut its own hint to "Search
    titles and w". Measured in headless Chrome at a true 420px viewport after
    this block: scrollWidth 420, no element past the edge, no cut text."""
    b = _narrow_block()
    # a 16px gutter replaces the 94vw cap
    assert "body{max-width:none;padding:0 16px 64px}" in b
    # the search field is the list's own now (docs/ui-refresh phase 4), full
    # width at every size, so the header no longer gives it a line of its
    # own, and no phone rule narrows it back
    assert re.search(r"(?m)^\.search\{display:block;width:100%;", NEW_CSS)
    assert ".controls{" not in b and ".search{" not in b
    # the pill wraps; its two segments stack over a hairline, not side by side
    assert ".pill{max-width:100%;white-space:normal;" in b
    assert ".pill:has(.pseg){flex-direction:column;" in b
    assert re.search(r"\.pill \.pseg\+\.pseg\{[^}]*border-left:0;[^}]*"
                     r"border-top:1px solid var\(--line\)", b)
    # a Now row's detail wraps under its title; the To finish eyebrow wraps
    assert ".trayrow{flex-wrap:wrap;" in b
    assert ".trayrow .tw-detail{order:1;flex:1 1 100%;white-space:normal}" in b
    assert ".sechead{flex-wrap:wrap}" in b
    # a wide right rail drops under the text instead of squeezing the title
    assert ".row{flex-wrap:wrap;" in b and ".rbody{flex:1 1 160px}" in b
    # titles and meta lines wrap instead of ending in an ellipsis
    m = re.search(r"([^{}]+)\{\s*white-space:normal;overflow-wrap:break-word\}", b)
    assert m, "the wrap rule is gone"
    sels = {x.strip() for x in m.group(1).split(",")}
    assert {".rtitle", ".rmeta", ".lrow .ltitle", ".lrow .lmeta",
            ".trayrow .tw-title"} <= sels
    # the rail still hides, and the block adds no colour of its own
    assert ".rail{display:none!important}" in b
    assert not re.findall(r"#[0-9A-Fa-f]{3,6}\b", b)
    # the meeting page's find field: an empty count yields its width
    m620 = NEW_CSS[NEW_CSS.index("@media(max-width:620px){"):]
    assert m620[:m620.index("\n}\n")].count(".mfindn:empty{display:none}") == 1


def test_flagged_aggregate_filters_the_library_not_the_tray():
    # "Review flags" on the To finish eyebrow presses the "Has flags" chip, a
    # library filter (never a second, smaller list; the tray band that once
    # expanded one is gone). The header's "flagged only" chip, the selects,
    # and the flaggedOnly flag retired into the chip row (docs/ui-refresh
    # phase 4).
    assert "TRAY_EXPAND_MAX" not in NEW_JS and "trayExpand" not in NEW_JS
    assert 'onclick="flaggedToggle()"' in _js_fn("finishActsHTML")
    assert "function flaggedToggle(){chipGo('flags');}" in NEW_JS
    assert "flaggedOnly" not in NEW_JS and "flaggedClear" not in NEW_JS
    for gone in ('id="flagchip"', "flagged only", 'id="filter"', 'id="sort"',
                 'id="searchhits"'):
        assert gone not in NEW_PAGE, gone
    # ready rows without substantial flags drop out; pinned states are untouched
    assert "if(st.flags&&!(r.review_substantial>0))return false;" in _js_fn("chipKeeps")
    # the chip state folds into both signatures so polls preserve it
    draw = _js_fn("drawTimeline")
    assert "const lfsig=LF.cat+':'+LF.flags+':'+LF.voices;" in draw
    assert draw.count("lfsig") == 3


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
    for sel in (".rmeta", ".mgroup", ".rslot", ".mmeta", ".trayrow .tw-title",
                ".lrow .ltitle", ".sdot"):
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
    # track — sub-floor 'Voice N' scraps and tombstone-suppressed voices).
    # The identity is read off NP into locals BEFORE the await (see
    # test_save_captures_its_identity_before_the_await), so the payloads carry
    # those locals, never a post-await NP dereference.
    assert re.search(r"\{uid:uid,name:n,confirm:!!force\}", NEW_JS)
    assert re.search(r"\{meeting:meeting,speaker:speaker,name:n,confirm:!!force,local:!!local\}", NEW_JS)
    assert re.search(r"function\s+openNamePanelByCluster\s*\(", NEW_JS)
    # the meeting legend offers the cluster chip for unnamed, un-tracked voices
    assert "openNamePanelByCluster(" in NEW_JS and NEW_JS.count("openNamePanelByCluster(") >= 2
    # the uid is captured into a local BEFORE the await, like every other
    # panel action (W14): NP can be null or another voice by the time the
    # response lands
    assert re.search(r"api\('/api/forget',\{uid:uid\}\)", NEW_JS)
    assert re.search(r"const uid=NP\.uid,tok=npToken\(\);", _js_fn("npForget"))
    # the enrollment quality gate answers with a warning: the panel renders
    # the house two-step confirm (numbers shown), never a native dialog
    assert 'id="npconfirm"' in NEW_JS
    # the confirm strip calls back into whichever surface opened it: the
    # slide-over passes its own npSave, so "Save anyway" is npSave(true)
    assert "${o.again}(true)" in _js_fn("saveName")
    assert "again:'npSave'" in _js_fn("npSave")
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
    # BOTH modes offer "Not a real speaker" now. uid mode tombstones the
    # registry entry (/api/forget); cluster mode, which has no registry entry
    # to tombstone, dismisses the voice in THIS meeting only.
    assert NEW_JS.count("Not a real speaker") >= 2
    assert re.search(r"(?:async )?function\s+npDismiss\s*\(", NEW_JS)
    assert "onclick=\"npDismiss()\"" in NEW_JS


def test_naming_panel_shows_what_the_voice_said():
    # the whole point: noise is recognizable by READING, so a voice can be
    # named or dismissed without playing anything
    assert re.search(r"(?:async )?function\s+npLines\s*\(", NEW_JS)
    assert 'id="nplines"' in NEW_JS
    lines = _js_fn("npLines")
    assert "'/api/voice_lines?base='" in lines and "&speaker='" in lines
    # every rendered line is escaped, and the block is silent on FAILURE (an
    # error where the evidence goes helps nobody). A successful fetch with no
    # lines is different: "this voice said nothing" is itself evidence, so it
    # gets a dim note instead of a gap that reads as a block that failed to load
    assert "esc(l.text)" in lines
    assert "if(!lines.length){" in lines
    assert "No transcript text was attributed to this voice." in lines
    assert "catch(e){return;}" in lines
    # both panel modes fetch it: cluster mode from its own meeting, uid mode
    # from whatever meeting the caller was looking at (optional)
    panel = _js_fn("openNamePanel")
    assert "npLines(cluster.meeting,cluster.speaker)" in panel
    assert "npLines(base,display)" in panel
    assert re.search(r"function openNamePanelByUid\(uid,base\)", NEW_JS)
    # the caller that KNOWS its meeting passes it: the transcript legend (the
    # timeline row's "?" chip retired with the tall naming row in phase 2;
    # the finish strip's Voices step names inline through saveName instead)
    assert re.search(r"openNamePanelByUid\('\$\{escJs\(uid\)\}','\$\{escJs\(MP\.base\)\}'\)",
                     NEW_JS)
    # the CSS block: quiet, on the tokens, above the 13px type floor
    m = re.search(r"(?m)^\.npltot\{[^}]*\}", NEW_CSS, re.S)
    assert m and "var(--sub)" in m.group(0)
    assert re.search(r"(?m)^\.nplines\{[^}]*background:var\(--inset\)", NEW_CSS, re.S)


def test_dismiss_posts_the_meeting_scoped_call_not_the_registry_tombstone():
    body = _js_fn("npDismiss")
    # per MEETING: base + cluster id, never the uid path. meeting/speaker are
    # captured into locals BEFORE the await (not re-read off NP after it) --
    # a Cancel/Escape click (or reopening for another voice) while the request
    # is in flight would otherwise null out or repoint NP before this code
    # runs again, so a post-await `NP.meeting` read is a crash-or-drift bug.
    assert re.search(r"const meeting=NP\.meeting,speaker=NP\.speaker,tok=npToken\(\);",
                     body)
    assert re.search(r"api\('/api/dismiss_voice',\{base:meeting,speaker:speaker\}\)",
                     body)
    assert "/api/forget" not in body
    # closing is scoped to the voice this call acted on (W14): a late response
    # must not force-close a panel the user has since reopened for someone else
    assert "if(npToken()===tok)closeNamePanel();" in body
    assert "refresh();" in body
    # an open transcript for that meeting rebuilds its legend immediately: a
    # "?" that keeps asking until the next page load reads as a failed click
    assert "MP.base===meeting" in body and "mReloadSegs()" in body
    # the await boundary must never dereference NP again
    assert "NP.meeting" not in body.split("await api", 1)[1]


# ---------------------------------------------------------------------------
# The naming flyout, second pass (2026-08-15): the form comes FIRST, the save
# survives the panel closing under it, and a stale "?" chip never opens a dead
# panel. Same regex-over-the-file style, plus node for the behavior.
# ---------------------------------------------------------------------------
def test_naming_panel_puts_the_form_above_the_evidence():
    """The control you came to use must not sit below a scroll of clips and
    transcript lines. Name box, buttons, and the two messages those buttons
    produce (the quality-gate confirm, the error line) render before the player
    and the lines block."""
    panel = _js_fn("openNamePanel")
    frags = ('class="npfield"', 'class="npbtns"', 'id="npconfirm"', 'id="nperr"',
             'id="npclips"', 'id="nplines"')
    for f in frags:
        assert f in panel, f"panel template lost {f}"
    order = [panel.index(f) for f in frags]
    assert order == sorted(order), \
        f"panel template out of order: {list(zip(frags, order))}"
    # the autocomplete dropdown overlays whatever now sits under the field:
    # .npfield is the positioned parent, .npdd is above it in the stack, and
    # neither .npclips nor .nplines creates a stacking context of its own
    m = re.search(r"(?m)^\.npfield\{[^}]*\}", NEW_CSS, re.S)
    assert m and "position:relative" in m.group(0)
    m = re.search(r"(?m)^\.npdd\{[^}]*\}", NEW_CSS, re.S)
    assert m and "z-index:5" in m.group(0)


def test_stale_uid_chip_never_opens_a_dead_panel():
    body = _js_fn("openNamePanelByUid")
    # the transient notice is TEXT, never innerHTML: nothing user-derived can
    # arrive as markup through it
    assert "el.textContent=msg" in _js_fn("toast")
    assert "toast(" in body and "refresh();" in body
    m = re.search(r"(?m)^\.toast\{[^}]*\}", NEW_CSS, re.S)
    assert m and "var(--card)" in m.group(0) and "13px" in m.group(0)


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_save_captures_its_identity_before_the_await():
    """The reported bug: naming U049 from the transcript legend succeeded
    server-side while the panel showed nothing. The save must (1) post the uid
    it was OPENED for even if the panel closes or reopens for another voice
    mid-flight, and (2) rebuild the open transcript on success, so the "?" chip
    stops asking instead of surviving to be clicked again on a dead uid."""
    fixture = "\n".join([
        "const els={};",
        "for(const k of ['#npname','#nperr','#npconfirm','#npsave'])"
        "els[k]={value:'',hidden:true,textContent:'',innerHTML:'',disabled:false};",
        "els['#npname'].value='  Priya Shah  ';",
        "function $(sel){return els[sel]||null;}",
        "function esc(s){return s;}",
        "let posted=null,release=null;",
        "function api(p,body){posted={p,body};return new Promise(r=>{release=r;});}",
        "let reloads=0,refreshes=0,closed=0;",
        "function refresh(){refreshes++;}",
        "function mReloadSegs(){reloads++;}",
        "function closeNamePanel(){closed++;NP=null;}",
        "let NP={uid:'U049',base:'LT Meeting 05212026'};",
        "let MP={base:'LT Meeting 05212026'};",
        _js_fn("npToken"), _js_fn("npConfirmBtns"), _js_fn("npBusy"),
        "let NPBUSY=false;",
        _js_fn("npSave"), _js_fn("saveName"), _js_fn("namedToast"),
        """
(async()=>{
  const p=npSave();
  // the panel is reopened for a DIFFERENT voice while the request is in flight
  NP={uid:'U050',base:'Another Meeting'};
  release({ok:true,note:'relabeling all meetings in background'});
  await p;
  console.log(JSON.stringify({posted,reloads,refreshes,closed,
    label:els['#npsave'].textContent,disabled:els['#npsave'].disabled}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["posted"]["p"] == "/api/name"
    assert out["posted"]["body"] == {"uid": "U049", "name": "Priya Shah",
                                     "confirm": False,
                                     "meeting": "LT Meeting 05212026"}, \
        "the save posted the voice the panel was reopened for, not the one saved"
    # ...and it must NOT close the panel it found open, because that panel is
    # showing a DIFFERENT voice by now (W14). Closing it here wiped a name the
    # user was in the middle of typing.
    assert out["closed"] == 0, \
        "a late save closed a panel that had moved on to another voice"
    assert out["refreshes"] == 1
    assert out["reloads"] == 1, \
        "the open transcript must rebuild on success (the legend '?' must stop asking)"
    # the button never stays stuck on "Saving..."
    assert out["label"] == "Save name" and out["disabled"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_save_survives_a_fetch_that_never_answers():
    """A dropped connection used to leave the Save button disabled and reading
    "Saving..." forever, with no way to retry but a page reload."""
    fixture = "\n".join([
        "const els={};",
        "for(const k of ['#npname','#nperr','#npconfirm','#npsave'])"
        "els[k]={value:'',hidden:true,textContent:'',innerHTML:'',disabled:false};",
        "els['#npname'].value='Priya Shah';",
        "function $(sel){return els[sel]||null;}",
        "function esc(s){return s;}",
        "function api(){return Promise.reject(new Error('network down'));}",
        "let reloads=0,refreshes=0,closed=0;",
        "function refresh(){refreshes++;}",
        "function mReloadSegs(){reloads++;}",
        "function closeNamePanel(){closed++;NP=null;}",
        "let NP={uid:'U049',base:'B'};let MP=null;",
        _js_fn("npToken"), _js_fn("npConfirmBtns"), _js_fn("npBusy"),
        "let NPBUSY=false;",
        _js_fn("npSave"), _js_fn("saveName"), _js_fn("namedToast"),
        """
(async()=>{
  await npSave();
  console.log(JSON.stringify({err:els['#nperr'].textContent,
    errShown:!els['#nperr'].hidden,label:els['#npsave'].textContent,
    disabled:els['#npsave'].disabled,closed,reloads}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["err"] == "Could not reach the server." and out["errShown"] is True
    assert out["label"] == "Save name" and out["disabled"] is False
    assert out["closed"] == 0 and out["reloads"] == 0   # nothing was saved


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_uid_that_is_no_longer_unknown_refreshes_instead_of_opening():
    """A legend chip rendered before the voice was named still carries the old
    uid. Opening the panel on it gave a DEAD flyout (the raw uid as the title
    and the speaker key: no lines, no audio, a save that could only fail)."""
    fixture = "\n".join([
        "let opened=[];",
        "function openNamePanel(uid,display,cluster,base){opened.push([uid,display,base]);}",
        "let toasts=[];function toast(m){toasts.push(m);}",
        "let refreshes=0,reloads=0;",
        "function refresh(){refreshes++;}",
        "function mReloadSegs(){reloads++;}",
        "let route={view:'meeting',base:'B'};",
        "let S={unknowns:[{uid:'U050',display:'Speaker 50',meetings:['B']}],"
        "relabel_running:true};",
        _js_fn("openNamePanelByUid"),
        """
openNamePanelByUid('U049','B');                 // named already: gone from S
const stale={opened:opened.length,toasts:toasts.slice(),refreshes,reloads};
S.relabel_running=false;S.relabel_pending=false;
openNamePanelByUid('U049','B');                 // and once the relabel is done
const after={toasts:toasts.slice(-1)};
openNamePanelByUid('U050','B');                 // a voice that IS still unknown
console.log(JSON.stringify({stale,after,opened}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["stale"]["opened"] == 0, "a stale uid must not build the panel"
    assert out["stale"]["toasts"] == ["Already named. Names are being applied now."]
    assert out["stale"]["refreshes"] == 1 and out["stale"]["reloads"] == 1
    assert out["after"]["toasts"] == ["This voice was already named."]
    # a live unknown still opens, with its display name (never the raw uid)
    assert out["opened"] == [["U050", "Speaker 50", "B"]]


def _stamp_watch_fixture(body):
    """The real relabelWatch over a fake DOM and one open meeting page ('B').
    `body` drives the polls."""
    return "\n".join([
        "let reloads=0;",
        "function mReloadSegs(){reloads++;}",
        "let NP=null,route={view:'meeting',base:'B'};",
        "let MP={base:'B',built:true,stamp:'S1'};",
        "let card=null,inline=null,find=null;",
        "const document={getElementById:id=>(id==='mfind'?find:card),"
        "querySelector:()=>inline};",
        "const poll=(id,at)=>({timeline:[{id:id,generated_at:at}]});",
        _js_fn("relabelWatch"),
        body])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_finished_relabel_rebuilds_the_open_transcript():
    """A relabel rewrites the transcript being stared at, so that page is the
    LAST place the new name shows up. The watcher compares the open meeting's
    OWN row stamp against the one recorded when the page was built -- not a
    library-wide "a relabel is running" edge, which back-to-back passes kept
    high and which said nothing about whether this meeting had changed. It runs
    before render()'s meeting-view early return, the only place this matters."""
    fixture = _stamp_watch_fixture("""
// same stamp, poll after poll: nothing changed, nothing reloads
relabelWatch(poll('B','S1'));
relabelWatch(poll('B','S1'));
const quiet={reloads};
// ANOTHER meeting's row moved: not ours, still nothing
relabelWatch({timeline:[{id:'B',generated_at:'S1'},
                        {id:'C',generated_at:'S9'}]});
const other={reloads};
// our own stamp moved: reload once, and record it so the next poll is quiet
relabelWatch(poll('B','S2'));
const moved={reloads,stamp:MP.stamp};
relabelWatch(poll('B','S2'));
const settled={reloads};
// mid-edit: the change is HELD, not lost -- the stamp is not recorded, so a
// later tick does the reload
NP={uid:'U1'};
relabelWatch(poll('B','S3'));
const held={reloads,stamp:MP.stamp};
NP=null;
relabelWatch(poll('B','S3'));
const later={reloads,stamp:MP.stamp};
// a row with no stamp at all (an older transcript) never forces a reload
relabelWatch(poll('B',null));
relabelWatch({timeline:[]});
const missing={reloads};
console.log(JSON.stringify({quiet,other,moved,settled,held,later,missing}));
""")
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["quiet"] == {"reloads": 0}
    assert out["other"] == {"reloads": 0}, \
        "another meeting's relabel must not rebuild the open page"
    assert out["moved"] == {"reloads": 1, "stamp": "S2"}
    assert out["settled"] == {"reloads": 1}, \
        "the recorded stamp stops the very next poll from reloading again"
    assert out["held"] == {"reloads": 1, "stamp": "S2"}, \
        "a reload must not blow away an open naming panel"
    assert out["later"] == {"reloads": 2, "stamp": "S3"}, \
        "the held change must fire on a later tick, not be dropped"
    assert out["missing"] == {"reloads": 2}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_stamp_watcher_ignores_a_page_that_is_not_open():
    """render() calls the watcher on every poll, including from the timeline.
    With no meeting page built there is nothing to reload, and reading MP would
    throw -- which would take the whole render down with it."""
    fixture = "\n".join([
        "let reloads=0;",
        "function mReloadSegs(){reloads++;}",
        "let NP=null,route={view:'timeline'};",
        "let MP=null;",
        "const document={getElementById:()=>null,querySelector:()=>null};",
        _js_fn("relabelWatch"),
        """
relabelWatch({timeline:[{id:'B',generated_at:'S2'}]});
route={view:'meeting',base:'B'};            // deep link, page not built yet
MP={base:'B',pending:true};
relabelWatch({timeline:[{id:'B',generated_at:'S2'}]});
console.log(JSON.stringify({reloads}));
"""]) 
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"reloads": 0}


def _pill_env():
    return "\n".join([
        "function clock(x){return '0:'+String(x).padStart(2,'0');}",
        "function fmtEta(x){return Math.round(x/60)+' min';}",
        re.search(r"(?m)^const STAGE_NICE=\{[\s\S]*?\};$", NEW_JS).group(0),
        _js_fn("pillStage"), _js_fn("pillParts"),
        "const pill=s=>{const p=pillParts(s);return p?[p.cls,p.html]:null;};",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_pill_counts_the_meetings_a_naming_is_walking():
    """The queue knows the target list, so the pill says how far along it is
    instead of an open-ended "applying names". A whole-library pass re-lists as
    it goes and reports no total -- that one keeps the old wording. Alone, the
    naming segment renders alone (phase 2 dropped the colon)."""
    out = _run_node(_pill_env() + """
const nm=j=>pill({relabel_running:true,jobs:{running:j}});
console.log(JSON.stringify([
  nm({kind:'relabel',total:7,done:2}),
  nm({kind:'relabel',total:7,done:7}),
  nm({kind:'all',total:null,done:0}),
  nm(null),
  pill({relabel_pending:true}),
  pill({})]));
""")
    assert out == [
        ["pill", "naming 3 of 7"],
        ["pill", "naming 7 of 7"],          # the last meeting, never "8 of 7"
        ["pill", "applying names&#8230;"],
        ["pill", "applying names&#8230;"],
        ["pill", "applying names&#8230;"],
        None]                               # idle: the pill hides


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_pill_gives_naming_its_own_segment_beside_the_pipeline():
    """docs/ui-refresh phase 2: "naming 3 of 7" was the pill's lowest
    priority, so it vanished whenever a file transcribed. It is a second
    segment now: "transcribing 1 . ~6 min | naming 3 of 7". Either segment
    alone renders alone, and the pipeline keeps its own priority order."""
    out = _run_node(_pill_env() + """
const j={running:{kind:'relabel',total:7,done:2}};
const tl=[{state:'waiting'},{state:'held'},{state:'ready'}];
const tr={a:{stage:'transcribing'}};
console.log(JSON.stringify({
  both:pill({running:true,active:tr,overall_eta_sec:360,relabel_running:true,jobs:j}),
  runOnly:pill({running:true,active:tr,overall_eta_sec:360,queued_jobs:[{}]}),
  rec:pill({recording:{elapsed_secs:5},running:true,relabel_running:true,jobs:j}),
  recPaused:pill({recording:{elapsed_secs:5,paused:true}}),
  paused:pill({paused:true,timeline:tl,relabel_pending:true,jobs:{running:null}}),
  waiting:pill({timeline:tl,relabel_running:true,jobs:j}),
  held:pill({timeline:[{state:'held'}]}),
}));
""")
    assert out["both"] == ["pill accent",
                           '<span class="pseg">transcribing 1 &middot; &#8776;6 min</span>'
                           '<span class="pseg">naming 3 of 7</span>']
    assert out["runOnly"] == ["pill accent",
                              "transcribing 1 &middot; &#8776;6 min &middot; 1 queued"]
    # recording outranks transcribing inside the pipeline segment
    assert out["rec"][0] == "pill rec"
    assert out["rec"][1].count('class="pseg"') == 2
    assert 'REC <span id="pillClock">0:05</span></span>' in out["rec"][1]
    assert out["recPaused"] == ["pill rec", '&#9208; REC <span id="pillClock">0:05</span>']
    assert out["paused"] == ["pill amber",
                             '<span class="pseg">&#9208; paused &middot; 2 waiting</span>'
                             '<span class="pseg">applying names&#8230;</span>']
    assert out["waiting"] == ["pill", '<span class="pseg">1 waiting</span>'
                                      '<span class="pseg">naming 3 of 7</span>']
    assert out["held"] is None, "a held file alone is not pipeline news"
    # the divider is a hairline between segments, never a literal bar
    assert re.search(r"(?m)^\.pill \.pseg\+\.pseg\{[^}]*border-left:1px solid var\(--line\)",
                     NEW_CSS)
    assert "drawPill(S)" in _js_fn("render")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_pill_names_the_stage_the_active_file_is_in():
    """The pill said "transcribing 1" while the one active file was
    diarizing a multi-hour board recording. One active file names its real
    stage from STAGE_NICE; "transcribing" means the transcribing stage only.
    Several files in different stages, or a stage not reported yet, read
    "processing N"."""
    out = _run_node(_pill_env() + """
const run=(active,eta)=>pill({running:true,active,overall_eta_sec:eta});
console.log(JSON.stringify({
  diar:run({a:{stage:'diarizing'}},150900),
  trans:run({a:{stage:'transcribing'}},360),
  prep:run({a:{stage:'converting'}},null),
  sum:run({a:{stage:'summarizing'}},null),
  mixed:run({a:{stage:'transcribing'},b:{stage:'diarizing'}},720),
  same:run({a:{stage:'diarizing'},b:{stage:'diarizing'}},720),
  unreported:run({a:{}},null),
  empty:run({},null),
  odd:run({a:{stage:'mystery'}},null),
}));
""")
    assert out["diar"] == ["pill accent", "speakers 1 &middot; &#8776;2515 min"]
    assert out["trans"] == ["pill accent", "transcribing 1 &middot; &#8776;6 min"]
    assert out["prep"] == ["pill accent", "preparing 1"]
    assert out["sum"] == ["pill accent", "summary 1"]
    assert out["mixed"] == ["pill accent", "processing 2 &middot; &#8776;12 min"]
    assert out["same"] == ["pill accent", "speakers 2 &middot; &#8776;12 min"]
    # no stage yet, no active entry, or a stage STAGE_NICE does not know:
    # never a guess at "transcribing"
    for k in ("unreported", "empty", "odd"):
        assert out[k] == ["pill accent", "processing 1"], k


def test_the_relabel_watcher_runs_before_the_meeting_view_early_return():
    body = _js_fn("render")
    assert "relabelWatch(S);" in body
    assert body.index("relabelWatch(S);") < body.index("route.view==='meeting'"), \
        "placed after the early return, the watcher never fires on a meeting page"
    # the pill takes BOTH signals: relabel_pending alone only ever meant "a
    # second pass is queued behind a first"
    assert "s.relabel_running||s.relabel_pending" in _js_fn("pillParts")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_legend_drops_the_question_mark_for_a_dismissed_voice():
    # a regression that ignores `dismissed` would keep nagging about the cough
    # forever; one that over-reads it would strip a real person's only naming
    # affordance. Drive the real mLegend over both.
    fixture = "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"),
        "const box={innerHTML:''};",
        "function $(sel){return sel==='#mlegend'?box:null;}",
        "let S={unknowns:[]};let MP={base:'B',color:{}};",
        _js_fn("mUnknownUid"), _js_fn("mLegend"),
        """
mLegend({speakers:['Alex Rivera','Speaker 2','Speaker 3'],
  speaker_options:[
    {id:'SPEAKER_00',display:'Alex Rivera',named:true,dismissed:false},
    {id:'SPEAKER_01',display:'Speaker 2',named:false,dismissed:false},
    {id:'SPEAKER_02',display:'Speaker 3',named:false,dismissed:true}]});
const chips=box.innerHTML.split('<span class="msdot"').slice(1);
console.log(JSON.stringify(chips.map(c=>c.includes('mlgq'))));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # named: no "?"; unnamed: "?"; dismissed: no "?" (still on the page, still
    # attributed, just no longer asking to be named)
    assert json.loads(r.stdout) == [False, True, False]


def test_a_dismissed_voice_offers_the_way_back():
    """Dismissal silenced the nag AND stripped every path back: a human who
    changed their mind about a "not a real speaker" could never name the voice
    from the transcript again. The chip now carries a quiet undo (its own
    glyph class, never the amber ?) that posts the restore."""
    fixture = "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"),
        "const box={innerHTML:''};",
        "function $(sel){return sel==='#mlegend'?box:null;}",
        "let S={unknowns:[]};let MP={base:'B',color:{}};",
        _js_fn("mUnknownUid"), _js_fn("mLegend"),
        """
mLegend({speaker_options:[
    {id:'SPEAKER_02',display:'Voice 2',named:false,dismissed:true}]});
console.log(JSON.stringify({
  restore:box.innerHTML.includes("mRestoreVoice('B','SPEAKER_02')"),
  button:box.innerHTML.includes('<button'),
  nag:box.innerHTML.includes('mlgq'),
  undo:box.innerHTML.includes('mlgr')}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == {"restore": True, "button": True, "nag": False, "undo": True}
    assert re.search(r"api\('/api/dismiss_voice',\{base:base,speaker:sid,restore:true\}\)", NEW_JS)


def test_flagged_reviews_sit_last_in_the_tray():
    """Deprioritized by request (2026-08-21): flagged-line review is a per-line
    accuracy chore, rarely the day's first work, so it renders AFTER unknown
    voices. The tray band is gone (phase 2): on the To finish eyebrow, "Name N
    voices" comes first and "Review flags" last. The order is the source
    order of the two actions."""
    acts = _js_fn("finishActsHTML")
    assert acts.index("fsVoicesStart()") < acts.index("flaggedToggle()"), \
        "unknown voices render before flagged reviews"


def test_meeting_header_gains_the_row_menu_and_a_cyclable_dot():
    # everything a row can do, the page can do: the same fillRowMenu feeds both
    assert re.search(r"function\s+mMenu\s*\(", NEW_JS)
    assert NEW_JS.count("fillRowMenu(") >= 3   # definition + row + page header
    # the header dot cycles with the same optimistic /api/set_category as rows
    assert re.search(r"function\s+mCycleCat\s*\(", NEW_JS)
    assert NEW_JS.count("api('/api/set_category'") >= 2
    assert 'id="mcatdot"' in NEW_JS
    # page-owning actions leave the page when their meeting disappears (revised
    # in docs/ui-refresh phase 3): one column returns to the library as before,
    # and two panes open the default meeting, so the reader is never left empty
    assert NEW_JS.count(
        "if(route.view==='meeting'&&route.base===id)readerLeave(id);") == 2
    assert "if(!PANES){location.hash='';return;}" in _js_fn("readerLeave")


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
    # rows are already tabindex=0 (the ring reuses :focus-visible); section
    # headers are buttons and ride the ring with them, so a folded month is
    # reachable. Synthetic upload rows (no tabindex) are still skipped.
    # To finish rows (links, phase 2) ride the ring between the two
    assert "const KB_RING='#timeline .row[tabindex], #timeline .lrow, #timeline .ghead';" \
        in NEW_JS
    assert "closest(KB_RING)" in _js_fn("kbMove")
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
    # a To finish row is a link: Enter is the link's own (no Accept-from-the-
    # list any more; the finish strip's Title step names the meeting)
    assert "acceptMeeting" not in NEW_JS
    # waiting/held/failed: Enter hands focus to the first revealed action
    # (focus-within already shows the set)
    assert ".ractions .iact" in body
    assert "e.key==='e'&&st==='ready'" in body and "toggleExpand(id)" in body
    # Enter/e act on the focused ROW itself, never a focused button inside it
    assert "e.target.matches('#timeline .row[tabindex]')" in body


def _layer_env():
    """kbKey, kbRestore and mKey over a stub page: a timeline row that can take
    the ring, the header search field, and the two edit layers (the naming
    panel and the drawer) as plain flags."""
    return "\n".join([
        _js_oneline("kbLayerOpen"), _js_fn("kbKey"), _js_fn("kbRestore"), _js_fn("mKey"),
        _js_fn("keySheetKey"),
        "let NP=null,_popClose=null,MR=null,kbLast=null,KSHEET=false,SAID_TOP=null;",
        "function keySheetOpen(){KSHEET=true;log.push('keys');}",
        "function keySheetClose(){KSHEET=false;log.push('keys:close');}",
        "function openHit(b,t){log.push('hit:'+b+'@'+t);}",
        "const DRAWER={open:false};",
        "let route={view:'timeline'};",
        "class Element{}",
        "const CSS={escape:s=>s};",
        "const log=[];",
        "const se={tagName:'INPUT',value:'',focus(){log.push('search');},blur(){}};",
        "const mf={tagName:'INPUT',focus(){log.push('find');},select(){}};",
        "const row={tagName:'DIV',dataset:{id:'M1'},focus(){log.push('ring:M1');}};",
        "const save={tagName:'BUTTON'};",
        "const body={tagName:'BODY'};",
        "const document={activeElement:body,body,getElementById:()=>null,"
        "querySelector:()=>row};",
        "const location={hash:''};",
        "function $(q){return q==='#search'?se:(q==='#mfind'?mf:null);}",
        "function kbMove(d){log.push('move:'+d);}",
        "function kbRows(){return [row];}",
        "function kbFocusRow(){log.push('ring');}",
        "function render(){} function scheduleSearch(){}",
        "function openMeeting(){log.push('open');} function toggleExpand(){log.push('peek');}",
        "function fsKey(){log.push('fsKey');return false;}",
        "function reviewGo(d){log.push('reviewGo:'+d);} function reviewStep(){log.push('reviewStep');}",
        "function reviewApply(v){log.push('apply:'+v);} function reviewUseAltGo(){}",
        "function reviewExit(){log.push('exit');} function mCloseCard(){log.push('closeCard');}",
        "function press(fn,k,pre){let prevented=false;log.length=0;"
        "fn({key:k,metaKey:false,ctrlKey:false,altKey:false,target:save,"
        "defaultPrevented:!!pre,preventDefault(){prevented=true;}});"
        "return {log:log.slice(),prevented};}",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_an_open_naming_panel_or_drawer_keeps_the_list_keys_off_the_rows():
    """QA, phase 2: with the naming panel open and its Save button focused, j
    jumped focus off the panel onto a hidden timeline row (and the ring
    remembered it). While either layer is open, j/k, / and Enter stay put, and
    a poll's rebuild never puts the ring back under it."""
    out = _run_node(_layer_env() + """
const o={};
document.activeElement=save;
o.free={j:press(kbKey,'j'),k:press(kbKey,'k'),slash:press(kbKey,'/')};
NP={uid:'U007'};
o.panel={j:press(kbKey,'j'),k:press(kbKey,'k'),slash:press(kbKey,'/'),
  enter:press(kbKey,'Enter')};
NP=null;DRAWER.open=true;
o.drawer={j:press(kbKey,'j'),k:press(kbKey,'k'),slash:press(kbKey,'/')};
DRAWER.open=false;
o.closed=press(kbKey,'j');
// the ring's restore after a rebuild: never under a layer
document.activeElement=body;kbLast='M1';
NP={uid:'U007'};log.length=0;kbRestore();o.restorePanel=log.slice();
NP=null;DRAWER.open=true;log.length=0;kbRestore();o.restoreDrawer=log.slice();
DRAWER.open=false;log.length=0;kbRestore();o.restoreFree=log.slice();
console.log(JSON.stringify(o));
""")
    assert out["free"]["j"] == {"log": ["move:1"], "prevented": True}
    assert out["free"]["k"] == {"log": ["move:-1"], "prevented": True}
    assert out["free"]["slash"] == {"log": ["search"], "prevented": True}
    quiet = {"log": [], "prevented": False}
    for layer in ("panel", "drawer"):
        for k, v in out[layer].items():
            assert v == quiet, f"{k} reached the list under the {layer}: {v}"
    assert out["closed"] == {"log": ["move:1"], "prevented": True}
    assert out["restorePanel"] == [] and out["restoreDrawer"] == []
    assert out["restoreFree"] == ["ring:M1"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_an_open_layer_keeps_the_meeting_page_keys_and_its_escape_to_itself():
    """The same rule on the meeting page: while the naming panel (opened from
    the legend) or the drawer is open, n/p, /, the stepper's a and the finish
    flow's keys stay put. And one Escape that closed a layer (its handler runs
    before mKey and preventDefaults) never also exits the stepper under it."""
    out = _run_node(_layer_env() + """
const o={};
route={view:'meeting',base:'M1'};MR={active:true,items:[],i:0};
document.activeElement=body;
o.free={n:press(mKey,'n'),slash:press(mKey,'/'),a:press(mKey,'a'),esc:press(mKey,'Escape')};
NP={meeting:'M1',speaker:'SPEAKER_01'};
o.panel={n:press(mKey,'n'),p:press(mKey,'p'),slash:press(mKey,'/'),a:press(mKey,'a'),
  esc:press(mKey,'Escape')};
NP=null;DRAWER.open=true;
o.drawer={n:press(mKey,'n'),slash:press(mKey,'/'),a:press(mKey,'a'),esc:press(mKey,'Escape')};
DRAWER.open=false;
// the panel's (or the drawer's) own Escape already ran and closed it
o.consumed=press(mKey,'Escape',true);
console.log(JSON.stringify(o));
""")
    assert out["free"]["n"]["log"] == ["fsKey", "reviewGo:1"]
    assert out["free"]["slash"]["log"] == ["fsKey", "find"]
    assert out["free"]["a"]["log"] == ["fsKey", "apply:accept"]
    assert out["free"]["esc"] == {"log": ["fsKey", "exit"], "prevented": True}
    quiet = {"log": [], "prevented": False}
    for layer in ("panel", "drawer"):
        for k, v in out[layer].items():
            assert v == quiet, f"{k} reached the page under the {layer}: {v}"
    assert out["consumed"] == {"log": ["fsKey"], "prevented": False}


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
    # the setup check ends on the line (docs/ui-refresh phase 4)
    line = "Record from the menu bar, or drop an audio file here."
    assert line in _js_fn("setupHTML")
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
    # a FAILED note renders as its own Now row, ABOVE the stall lines
    body = _js_fn("nowTrayHTML")
    assert "s.recorder_note&&!s.recorder_note.ok" in body
    assert body.index('tw-title">Recorder') < body.index("trayAct('recorder_stall'"), \
        "the recorder note must outrank the stall line"
    # the note folds into the timeline signature so polls neither drop nor thrash it
    assert re.search(r"note\?note\.at", _js_fn("traySig"))
    # Fix permissions: same endpoint + in-flight label as the stall item...
    m = re.search(r"if\(kind==='recorder_note'\|\|kind==='recorder_stall'\)\{(.*?)\n  \}",
                  NEW_JS, re.S)
    assert m, "trayAct lost its recorder_note branch"
    assert "'/api/fix_recorder_permissions'" in m.group(1)
    assert "Resetting…" in m.group(1) and "Retry" in m.group(1)
    # ...kept as state (NOW_MSG), because the list's 2s rebuild wipes the DOM
    assert "NOW_MSG[kind]" in m.group(1) and "JSON.stringify(NOW_MSG)" in _js_fn("traySig")
    # ...shown behind the old strip's microphone/audio gate
    assert "/[Mm]icrophone|audio/" in body
    # the x dismisses with the old page's exact endpoint and payload
    assert "api('/api/recorder_note',{clear:true}).then(refresh)" in body


def test_saved_recorder_note_is_a_quiet_line_not_a_tray_ask():
    # the success line mounts right under the header, BEFORE the list (the
    # tray band that once sat between them is gone)
    assert 'id="recok"' in NEW_PAGE and 'id="tray"' not in NEW_PAGE
    assert NEW_PAGE.index('id="recok"') < NEW_PAGE.index('id="timeline"')
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
# The naming row was a review card (2026-08-06): a form, then the three things
# a human names a meeting from -- the original filename, the voices heard, and
# the summary. docs/ui-refresh phase 2 retired the tall row: a meeting waiting
# for its title is a compact To finish row, and the meeting page's finish strip
# names it. Each rule the card pinned moves to its new home here: the file
# name to the Title step, the voices to the Voices step and the legend (the
# same uid lookup, archived voices included), and the escaping to both.
# ---------------------------------------------------------------------------
def test_needs_name_row_shows_the_source_filename_and_adopts_it():
    # the Title step offers the stem one click away and fills its own box
    body = _js_fn("fsTitleHTML")
    assert "row.source_file" in body and "srcStem(" in body
    assert 'onclick="fsUseFile()"' in body and "Use the file name" in body
    # suppressed when the stem already equals the prefilled title (a recorder
    # default), trimmed and case-insensitively
    assert "stem.trim().toLowerCase()!==t.trim().toLowerCase()" in body
    use = _js_fn("fsUseFile")
    assert "t.value=srcStem(" in use and "t.focus()" in use
    # the list holds no form at all any more
    assert "nsrcAdopt" not in NEW_JS and 'class="ntitle"' not in NEW_JS


def test_needs_name_row_chips_reuse_the_one_naming_panel():
    # the unnamed voices of a meeting waiting for its title are named in its
    # Voices step (the shared saveName), and the legend's "?" chips still open
    # the ONE naming slide-over, by uid or by cluster
    assert "await saveName({" in _js_fn("fvSave")
    leg = _js_fn("mLegend")
    assert "openNamePanelByUid(" in leg and "openNamePanelByCluster(" in leg
    assert "rowSpeakerChips" not in NEW_JS
    # the Voices step reads the same set the legend's chips do
    assert "!o.named&&!o.dismissed&&o.id!==o.display" in _js_fn("fsVoicesFrom")


def test_needs_name_row_shows_the_summary_quietly():
    # the compact row is quiet on purpose: title, date, one meta line, the
    # step dots. The summary is one click away, on the meeting page.
    row = _js_fn("finishRowHTML")
    assert "row.summary" not in row
    assert 'class="lmeta"' in row and "finishDotsHTML(row)" in row
    m = re.search(r"(?m)^\.lrow \.lmeta\{[^}]*\}", NEW_CSS)
    assert m and "var(--sub)" in m.group(0) and "13px" in m.group(0)


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_srcstem_strips_only_the_final_extension():
    # a regression that cuts at the FIRST dot would silently corrupt every
    # multi-dot filename's adopted name: "Team.Sync 0714.m4a" must adopt as
    # "Team.Sync 0714", never "Team"
    fixture = _js_fn("srcStem") + "\n" + (
        "console.log(JSON.stringify(["
        "srcStem('Team.Sync 0714.m4a'),srcStem('noext'),"
        "srcStem(''),srcStem(null)]));")
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["Team.Sync 0714", "noext", "", ""]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_needs_name_row_equal_stem_is_actually_suppressed_end_to_end():
    fixture = "\n".join([
        _js_oneline("esc"), _js_fn("srcStem"), _js_fn("fsTitleHTML"),
        """
const same = fsTitleHTML({state:'needs_name', id:'b1',
  source_file:'Team Sync 0714.m4a',
  suggested_title:'  team sync 0714  ', title:'  team sync 0714  '});
const diff = fsTitleHTML({state:'needs_name', id:'b2',
  source_file:'Team.Sync 0714.m4a',
  suggested_title:'Different Title', title:'Different Title'});
console.log(JSON.stringify({
  sameHasFile: same.includes('Use the file name'),
  diffHasFile: diff.includes('Use the file name'),
  diffKeepsInternalDot: diff.includes('title="Team.Sync 0714"'),
}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["sameHasFile"] is False, \
        "equal stem (trimmed, case-insensitive) must suppress the file-name offer"
    assert out["diffHasFile"] is True
    assert out["diffKeepsInternalDot"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_row_unknown_uid_matches_archived_voices_not_just_live_ones():
    # archiving an unknown was meant to stop the drawer nagging about a
    # one-time voice, never to strip its naming affordance out of a meeting
    # that heard them -- a regression that starts filtering archived entries
    # would silently turn a real, still-nameable person into a dead chip. The
    # row's lookup retired with the tall row; the page's (the legend and the
    # Voices step both read it) keeps the rule.
    fixture = _js_fn("mUnknownUid") + "\n" + """
S = {unknowns: [{uid:'U1', display:'Voice 9', meetings:['base1'], archived:true},
                {uid:'U2', display:'Voice 10', meetings:['base1'], archived:false}]};
const at=(base,d)=>{MP={base};return mUnknownUid(d);};
let MP=null;
console.log(JSON.stringify([
  at('base1','Voice 9'),
  at('base1','Voice 10'),
  at('base1','Voice 99'),
  at('otherbase','Voice 9')
]));
"""
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["U1", "U2", None, None]
    assert "uid:mUnknownUid(o.display)" in _js_fn("fsVoicesFrom")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_needs_name_row_escapes_hostile_filenames_and_speaker_names():
    # a meeting really can be named after a person with an apostrophe, and a
    # filename or a diarized display can carry quotes/angle brackets. Every
    # interpolation into the compact row and the Title step must survive that
    # without breaking out of its HTML attribute or its href.
    fixture = "\n".join([
        _finish_env(), _js_fn("srcStem"), _js_fn("fsTitleHTML"),
        "function shortDate(d){return d;}",
        _js_fn("finishDotsHTML"), _js_fn("peopleMetaBits"), _js_fn("finishRowHTML"),
        r"""
const row={state:'needs_name', id:"o'brien<b>", date:'2026-09-26',
  source_file: "Mark C. Ashby <sync> \"weird\".m4a",
  suggested_title: 'Something <i>else</i>', title: 'Something else',
  speakers: ["Ashby <script>alert(1)</script>"],
  named: ["Ashby <script>alert(1)</script>"],
  finish: fin(false,1,0)};
console.log(JSON.stringify({row:finishRowHTML(row), title:fsTitleHTML(row)}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    for html in out.values():
        # never a raw, unescaped tag from user-controlled text
        assert "<script>alert(1)</script>" not in html
        assert "<i>else</i>" not in html
    row = out["row"]
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in row
    assert "Something &lt;i&gt;else&lt;/i&gt;" in row
    # the href is percent-encoded and the data-id attribute-escaped
    assert 'href="#m/o\'brien%3Cb%3E"' in row
    assert 'data-id="o\'brien&lt;b&gt;"' in row
    # the Title step: the file name only ever sits in an escaped attribute,
    # never inside an onclick string
    t = out["title"]
    assert 'title="Mark C. O\'Halloran &lt;sync&gt; &quot;weird&quot;"' in t
    assert 'onclick="fsUseFile()"' in t


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


def _js_oneline(name):
    """A top-level function written on a single line (esc/escJs), which the
    house \\n} convention above can't bound -- it would run on into whatever
    later function happens to close at column 0 next."""
    m = re.search(r"^function " + re.escape(name) + r"\(.*\)\{.*\}$", NEW_JS, re.M)
    assert m, f"missing one-line function: {name}"
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


def test_indigo_tokens_shipped_and_boardroom_retired():
    # DESIGN.md (2026-09-08): the Indigo Blueprint colorway supersedes
    # Boardroom Wide. Blueprint indigo on a pale cool ground; teal carries
    # every attention state; the Personal chip is magenta. Status/chip tokens
    # are the COMPUTED-contrast pairs (tools/token_contrast.py): the one that
    # missed on the spec's first draft, light --rec-text #C2362F at 4.43:1 on
    # its soft, is darkened hue-true to #BC342E (4.67:1) while --rec #C2362F
    # keeps the record-light and destructive FILLS.
    for tok in ("#2F4BD6", "#F4F6FB", "#101828", "#5D6B85", "#E1E6F2",
                "#C7D0E4", "#EEF1F8", "#E6EAFB", "#1E7B45", "#E3F4EA",
                "#0B7285", "#DDF2F6", "#C2362F", "#BC342E", "#FAE3E0",
                "#A8327A", "#F8E4F0",
                "#0F1320", "#161B2B", "#E8EBF5", "#8B9DFF", "#5FD0E3",
                "#E58BC4"):
        assert tok in NEW_CSS, f"Indigo token missing: {tok}"
    for old in ("#6161FF", "#4B4FD9", "#F6F7FB", "#2264D1", "#E8EFFC",
                "#A05E0C", "#FFF1E5", "#8F5F10", "#CC3F38", "#BE3A33",
                "#131417", "#1B1D23", "#8F94FF", "#E0A94F",
                "#4F5DE5", "#7B87FF", "#FAFAFC", "#0F1114",
                "#1E6B50", "#43B28A", "#FBFBF9", "#121416"):
        assert old not in NEW_CSS, f"retired Boardroom/Signal token: {old}"
    # --attn is the attention hue, and --amber survives ONLY as its alias, so
    # no rule that still says amber breaks
    assert NEW_CSS.count("--attn:#") == 4 and NEW_CSS.count("--attn-soft:#") == 4
    assert NEW_CSS.count("--amber:var(--attn); --amber-soft:var(--attn-soft);") == 4
    # the Signal glow touches stay retired: no glow shadows, no breathing pill;
    # the focus ring is the plain accent outline
    assert "0 0 0 3px color-mix" not in NEW_CSS
    assert "0 0 6px color-mix" not in NEW_CSS
    assert "pillbreathe" not in NEW_CSS
    assert "outline:2px solid var(--accent)" in NEW_CSS
    # states speak in pills, categories in chips...
    for cls in (".spill{", ".spill.ok{", ".spill.busy{", ".spill.rec{",
                ".catchip{", ".catchip.work{", ".catchip.personal{"):
        assert cls in NEW_CSS, f"missing state/chip class: {cls}"
    # ...and the primary button's Boardroom accent glow retired with the
    # shadows: geometry, not lift, separates the surfaces now
    assert "0 4px 14px color-mix(in srgb,var(--accent) 30%" not in NEW_CSS
    assert "var(--cardsh" not in NEW_CSS, "the card-shadow tokens must be gone"
    assert not re.search(r"--cardsh(?:-hov)?\s*:", NEW_CSS), \
        "the card-shadow tokens are still declared"
    # rows are bordered cards: 6px radius, 10px gaps, a border that FIRMS on
    # hover instead of a lift; the Now rows for the tray's urgent kinds wear
    # the teal attention edge the tray band wore. The naming row's accent
    # edge retired with the tall row (phase 2): a To finish row's teal Title
    # dot says the same thing.
    assert re.search(r"(?m)^\.row\{[^}]*border-radius:6px", NEW_CSS)
    assert re.search(r"(?m)^\.row\{[^}]*margin:0 0 10px", NEW_CSS)
    assert ".row:hover{border-color:var(--line)}" in NEW_CSS
    assert re.search(r"(?m)^\.trayrow\{[^}]*border-left:4px solid var\(--attn\)", NEW_CSS)
    assert 'needs_name"]' not in NEW_CSS
    assert re.search(r"(?m)^\.lrow\{[^}]*border-radius:6px", NEW_CSS)
    assert ".lrow:hover{border-color:var(--line)}" in NEW_CSS


def test_geometric_surfaces_carry_the_indigo_radii_and_no_stray_shadow():
    """DESIGN.md (Indigo Blueprint), Surfaces: cards 6px, chips and pills 4px,
    controls 6px, and shadows ONLY on the floating surfaces. Every radius in
    the sheet has to land in that vocabulary."""
    radii = set(re.findall(r"border(?:-\w+)*-radius:([\w.%]+)", NEW_CSS))
    # 6px cards/controls, 4px chips/pills, plus the shapes the rules do not
    # govern: circles (50%), the 99px switch track, the 2px progress hairline
    # and bubble tails, and the 3px inline search highlight
    assert radii <= {"6px", "4px", "2px", "3px", "50%", "99px"}, \
        f"radius outside the Indigo vocabulary: {sorted(radii)}"
    assert "6px" in radii and "4px" in radii
    # the switch track is the one 99px survivor: a switch is not a chip
    assert NEW_CSS.count("border-radius:99px") == 1
    assert re.search(r"(?m)^\.dtog\{[^}]*border-radius:99px", NEW_CSS)
    # the header's blueprint dot grid: --line dots, 1px on a 14px cell
    assert re.search(r"(?m)^\.hdr\{[\s\S]*?background-image:radial-gradient", NEW_CSS)
    assert "background-size:14px 14px" in NEW_CSS
    # the progress hairline is 2px of ACCENT along the card's bottom edge
    m = re.search(r'\.row\[data-state="processing"\] \.progress\{[^}]*\}', NEW_CSS)
    assert m and "height:2px" in m.group(0) and "background:var(--accent)" in m.group(0)


def test_no_shadow_rule_reaches_a_row_the_tray_or_a_card():
    """The border does the separating. Shadows survive only on the popovers,
    the drawer (and the naming slide-over, a drawer in all but name), and the
    bulk bar -- never on a row, a Now row, a To finish row, or any of the
    in-flow card surfaces."""
    # every rule that casts a DROP shadow, by selector. The inset edge markers
    # (.mseg.mnow / .mseg.mrev) are left edges drawn with box-shadow:inset,
    # not lift, so they are excluded by the inset check below.
    casters = []
    for sel, decls in re.findall(r"([^{}]+)\{([^}]*)\}", NEW_CSS):
        for decl in decls.split(";"):
            if not decl.strip().startswith("box-shadow:"):
                continue
            value = decl.split(":", 1)[1].strip()
            if value.startswith("inset") or value == "none":
                continue
            casters.append(sel.strip().splitlines()[-1].strip())
    assert casters, "no shadow rules found -- the audit regex broke"
    allowed = {".popover", ".bulkbar", ".npanel", ".npdd", ".drawer",
               ".dropveil .dv-t"}
    assert set(casters) <= allowed, f"unexpected shadow caster: {sorted(set(casters) - allowed)}"
    # named directly, so a future edit that re-adds a lift to any of the three
    # surfaces the spec calls out fails here rather than by eye
    for sel in (r"(?m)^\.row\{[^}]*\}", r"(?m)^\.row:hover\{[^}]*\}",
                r"(?m)^\.trayrow\{[^}]*\}", r"(?m)^\.lrow\{[^}]*\}",
                r"(?m)^\.sdot\{[^}]*\}", r"(?m)^\.msection\{[^}]*\}",
                r"(?m)^\.mcard\{[^}]*\}"):
        m = re.search(sel, NEW_CSS)
        assert m, f"rule missing: {sel}"
        assert "box-shadow" not in m.group(0), f"shadow back on {m.group(0)[:40]!r}"


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


# ---------------------------------------------------------------------------
# Duplicates (2026-08-15). Two surfaces: the "already processed" line on a
# waiting row (with the narrow bulk sweep in the tray), and the duplicate
# TRANSCRIPT review section in the drawer. The rules that keep both honest --
# identical bytes may be swept, a shared name may not, and no transcript is
# ever deleted without a human -- are pinned in tests/test_dupes.py; these pin
# that the panel actually shows them.
# ---------------------------------------------------------------------------
def test_a_waiting_row_says_when_it_was_already_processed():
    body = _js_fn("dupNote")
    assert "row.dup_of" in body
    # the two signals never read alike: bytes are proof, a name is a suggestion
    assert "identical to" in body and "same source name as" in body
    assert "dup_reason==='identical'" in body
    # everything user-derived is escaped, and the matched meeting is one click
    assert "esc(row.dup_title||row.dup_of)" in body
    assert "#m/${encodeURIComponent(row.dup_of)}" in body
    # both queue states carry it (a held file is still worth flagging): both
    # render through the one compact queue line, which carries the note
    assert re.search(r"case 'waiting':\s*case 'held':\s*return queueBodyAndSlot\(row\);",
                     _js_fn("bodyAndSlot"))
    assert "dupNote(row)" in _js_fn("queueBodyAndSlot")
    # ...and the flag is part of the row signature: the check hashes on a size
    # collision, so it lands a poll or two after the row first painted
    assert "r.dup_of,r.dup_reason" in _js_fn("sigOf")
    m = re.search(r"(?m)^\.dupflag\{[^}]*\}", NEW_CSS, re.S)
    # the house colour for a decision that is not an error: teal since Indigo
    assert m and "var(--attn)" in m.group(0) and "13px" in m.group(0)


def test_the_duplicate_sweep_is_a_two_step_and_deletes_only_copies():
    tray = _js_fn("nowTrayHTML")
    assert "t.kind==='dupe_files'" in tray and "t.kind==='dupe_meetings'" in tray
    # the armed flag rides the timeline signature (via traySig), or a 2s poll
    # would disarm the confirm under the user's cursor
    assert "DUPE_ARMED" in tray and "+DUPE_ARMED" in _js_fn("traySig")
    assert "'|'+tsig" in _js_fn("drawTimeline")
    # the count offered for deletion is the EXACT (byte-identical) subset,
    # never every flagged file
    assert "t.exact" in tray
    go = _js_fn("dupeDeleteGo")
    assert "api('/api/queue_delete_dupes',{confirm:true})" in go
    assert "DUPE_ARMED=false" in go
    # a failed sweep says so in the row instead of silently doing nothing
    assert "Could not delete those files." in go


def test_duplicate_transcripts_get_their_own_drawer_section():
    assert 'id="dsec-dupes"' in NEW_JS
    assert "['dupes','Duplicates'" in NEW_JS          # the tab, with its count
    assert "else if(sec==='dupes')dDupesLoad();" in NEW_JS
    assert "if(kind==='dupe_meetings'){openDrawer('dupes');return;}" in _js_fn("trayAct")
    load = _js_fn("dDupesLoad")
    assert "api('/api/dupes')" in load
    side = _js_fn("_dupeSide")
    # both meetings are shown the same way: neither is presented as "the
    # original", because the panel cannot know which one that is
    assert "esc(side.title||side.base)" in side
    assert "dDupeDelAsk(" in side and "#m/${encodeURIComponent(side.base)}" in side
    # deleting a transcript is the house two-step, with the full consequence
    assert "It cannot be undone." in side
    assert "api('/api/delete_meeting',{base,confirm:true})" in _js_fn("dDupeDelGo")
    # "keep both" is a real answer and it sticks
    assert "api('/api/dupe_ignore',{a,b})" in _js_fn("dDupeKeep")
    assert "Keep both" in _js_fn("dDupesRender")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_duplicate_line_survives_a_hostile_meeting_name():
    """Meeting titles carry apostrophes and worse, and this one renders inside
    an anchor next to an interpolated href."""
    fixture = "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"), _js_fn("dupNote"),
        r"""
console.log(JSON.stringify({html:dupNote({dup_of:"o'brien<b>",
  dup_title:"Ashby <script>alert(1)</script>", dup_reason:'identical'}),
  none:dupNote({state:'waiting'})}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["none"] == "", "a row with no duplicate renders nothing at all"
    html = out["html"]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "o'brien%3Cb%3E" in html   # the href is percent-encoded, not raw


# ---- review fix pass: the duplicate surfaces stay honest ----

def test_cancel_disarms_the_dupes_delete_confirm():
    """Cancel on the armed "Delete forever" card runs dConfirmClear, which used
    to repaint every section EXCEPT dupes: the flag cleared but the live danger
    button stayed rendered and clickable, and a second click deleted a meeting
    the user believed was disarmed. Two independent halves pin it: the clear
    repaints the section, and a click on a stale button re-checks the flag."""
    clear = re.search(r"function dConfirmClear\(\)\{.*?\n\}", NEW_JS, re.S).group(0)
    assert "dDupesRender()" in clear
    assert "if(DRAWER.dconfirm!=='dupdel:'+base){dDupesRender();return;}" in NEW_JS


def test_the_dupes_drawer_keeps_itself_current():
    """The section fetched /api/dupes exactly once on open: a user arriving
    mid-scan watched "Comparing transcripts..." forever while the tab count
    beside it updated, and the server's Rescan endpoint had no caller at all.
    The poll's drawer pass now gives the section a chance to re-fetch (only
    while a scan was running, or when the tab count disagrees with what is
    rendered -- self-stopping either way), and Rescan is a real button that
    also surfaces the truncation note."""
    draw = re.search(r"function drawDrawer\(s\)\{.*?\n\}", NEW_JS, re.S).group(0)
    assert "dDupesPoll(s)" in draw
    poll = re.search(r"function dDupesPoll\(s\)\{.*?\n\}", NEW_JS, re.S).group(0)
    assert "DRAWER.dupeScan" in poll and "dDupesLoad()" in poll
    assert "DRAWER.dupeBusy" in poll, "a fetch in flight must not stack another"
    assert "dn!==DRAWER.dupeSeen" in poll, \
        "the refetch keys on the tray count CHANGING, never on it disagreeing" \
        " with the rendered list, which can differ forever on unparseable metadata"
    assert "/api/dupe_scan" in NEW_JS and "dDupeRescan" in NEW_JS
    assert "Pair budget reached" in NEW_JS


def test_a_failed_sweep_survives_the_next_poll():
    """The failure message used to be written straight into tray DOM after the
    armed flag flipped -- the next poll's rebuild (signature mismatch) erased
    it within two seconds and the row snapped back as if nothing was clicked.
    It is state now: part of the signature, rendered by nowTrayHTML itself."""
    assert "let DUPE_ARMED=false,DUPE_ERR=''" in NEW_JS
    assert "+DUPE_ARMED+'|'+DUPE_ERR" in NEW_JS
    assert "DUPE_ERR&&!DUPE_ARMED?DUPE_ERR:t.detail" in NEW_JS
    assert re.search(r"DUPE_ERR=\(r&&r\.error\)\|\|'Could not delete", NEW_JS)


def test_voice_lines_error_is_not_read_as_silence():
    """/api/voice_lines answers errors as parseable JSON and api() never checks
    HTTP status, so a mid-relabel 404 landed in the same branch as a genuinely
    quiet voice and painted "No transcript text was attributed to this voice"
    -- inviting a "Not a real speaker" tombstone on a real person. Only a
    successful fetch carrying a real array may assert silence."""
    assert "if(!r||r.error||!Array.isArray(r.lines))return;" in NEW_JS


def test_the_floor_refusal_offers_the_meeting_only_save():
    """A sub-floor voice used to hit a dead error. The floor response now
    carries floor:true and the panel renders a confirm strip whose copy states
    the opposite of the panel's merge promise, plainly: the save is for this
    meeting only and the system does not learn the voice. The strip's button
    re-posts with local:true, and the save payload carries the flag."""
    assert "if(r.floor&&warn){" in NEW_JS
    assert "Save for this meeting only." in NEW_JS
    assert "does not learn this voice" in NEW_JS
    assert "${o.again}(false,true)" in _js_fn("saveName")
    assert "again:'npSave'" in _js_fn("npSave")
    assert re.search(r"local:!!local", NEW_JS), "meeting mode must carry the flag"


def test_a_locally_named_chip_stays_editable():
    """Once a cluster carries a name its ? chip used to vanish, so a typo in a
    meeting-local name was uncorrectable short of a full Redo. A local name
    keeps its chip clickable through the same floor-then-confirm flow."""
    assert "o.named&&o.local" in NEW_JS
    assert "Named for this meeting only. Click to change" in NEW_JS
    # chips are built per CLUSTER off speaker_options, never through a
    # display-keyed map: two speakers sharing one display name (a local name
    # matching an enrolled person) must keep their own ids on their own chips
    assert "opt[o.display]=o" not in NEW_JS
    leg = re.search(r"function mLegend\(d\)\{.*?\n\}", NEW_JS, re.S).group(0)
    assert "(d.speaker_options||[]).map(o=>" in leg


def test_a_failed_row_tells_the_truth_about_its_retry():
    """The failed-row note comes from the server's retry_note (gone vs still
    re-running are opposite situations), a gone source shows no Retry, and its
    X dismisses the failure record instead of calling the file-delete endpoint
    that refused on a missing file."""
    assert "row.retry_note||'original stays in the watched folder'" in NEW_JS
    assert "row.gone" in NEW_JS
    assert "failDismiss" in NEW_JS and "/api/dismiss_failure" in NEW_JS


# ---------------------------------------------------------------------------
# Fix pass 2 (2026-09-06): the tray, the bulk bar, the naming flyout, and the
# duplicates drawer. The tray band is gone since docs/ui-refresh phase 2: its
# urgent kinds are Now rows (traySig + nowTrayHTML, rendered inside the
# timeline), so the harness drives those. draw(s) is what drawTimeline does
# with them each poll: the signature first (it disarms), then the markup.
# ---------------------------------------------------------------------------
_TRAY_HARNESS = [
    _js_oneline("esc"), _js_oneline("escJs"),
    re.search(r"(?m)^const NOW_KINDS=.*;$", NEW_JS).group(0),
    "let DUPE_ARMED=false,DUPE_ERR='',NOW_MSG={};",
    "let S=null;",
    _js_fn("_trayVerb"), _js_fn("_trayRow"),
    _js_oneline("dupeDisarm"), _js_oneline("nowMsg"), _js_oneline("trayCount"),
    _js_fn("voiceQueueCount"), _js_fn("traySig"), _js_fn("nowTrayHTML"),
    "const tray={innerHTML:'',sig:''};",
    "function draw(s){tray.sig=traySig(s);tray.innerHTML=nowTrayHTML(s);}",
]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_dupe_sweep_disarms_when_its_tray_row_goes_away():
    """R1: arm "Delete copies", walk away without confirming, and the flagged
    file leaves the queue by another path. DUPE_ARMED survived, so a FUTURE
    duplicate row -- different files, days later -- rendered pre-armed and its
    first click fired the no-undo bulk delete with confirm:true. The row is a
    Now row now; the rule is the same."""
    fixture = "\n".join(_TRAY_HARNESS + ["""
const withDupes={tray:[{kind:'dupe_files',title:'Already processed',
  detail:'2 waiting files were already processed',target:'src:a.m4a',
  exact:2,count:2}]};
draw(withDupes);
DUPE_ARMED=true;                       // the user armed the sweep
draw(withDupes);
const armed={html:tray.innerHTML.includes('dupeDeleteGo(this)'),flag:DUPE_ARMED};
// the flagged file leaves the queue some other way: no dupe_files row at all
draw({tray:[{kind:'review',title:'Board Prep',detail:'3 flagged',
  target:'Board Prep',count:3}]});
const gone={flag:DUPE_ARMED};
// and the NEXT duplicate row must render unarmed
draw(withDupes);
const later={html:tray.innerHTML.includes('dupeDeleteGo(this)'),
  arm:tray.innerHTML.includes('dupeArm(true)'),flag:DUPE_ARMED};
// an empty tray disarms too
DUPE_ARMED=true;draw({tray:[]});
const emptied={flag:DUPE_ARMED};
console.log(JSON.stringify({armed,gone,later,emptied}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["armed"] == {"html": True, "flag": True}
    assert out["gone"]["flag"] is False, \
        "the armed sweep outlived the row that armed it"
    assert out["later"] == {"html": False, "arm": True, "flag": False}, \
        "a fresh duplicate row must render unarmed, behind the two-step"
    assert out["emptied"]["flag"] is False


def test_a_route_change_disarms_the_duplicate_sweep():
    """R1's other half: opening a meeting (or coming back) must not leave a
    no-undo delete armed behind the page you left."""
    body = _js_fn("applyRoute")
    assert "dupeDisarm();" in body
    assert re.search(r"function dupeDisarm\(\)\{DUPE_ARMED=false;DUPE_ERR='';\}", NEW_JS)


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_tray_offers_dismiss_not_retry_for_a_gone_failure():
    """R5, client half: the tray line kept offering Retry for a source that is
    no longer on disk, so the click posted /api/run for nothing. The tray's
    failed line is gone (phase 2): the failed ROW in Now is the one home, so
    the rule moves onto it -- dismiss for a gone source, Retry for one still
    in the folder -- and the Now rows never repeat a failed file."""
    fixture = "\n".join(_TRAY_HARNESS + [
        "function clock(){return '0:00';}", "function fmtEta(){return '';}",
        "const STAGE_NICE={};",
        "function dupNote(){return '';}", "function slotActions(x){return x;}",
        "function catDot(){return '';}", "function shortDate(){return '';}",
        "function meetingByBase(){return null;}",
        _js_fn("bodyAndSlot"),
        """
const gone=bodyAndSlot({state:'failed',id:'src:ghost.m4a',title:'ghost.m4a',
  error:'boom',gone:true,retry_note:'the original is gone'});
const here=bodyAndSlot({state:'failed',id:'src:here.m4a',title:'here.m4a',
  error:'boom',gone:false});
draw({tray:[{kind:'failed',title:'here.m4a',detail:'boom',target:'src:here.m4a',
  count:1,gone:false}]});
console.log(JSON.stringify({
  goneVerb:gone.includes("failDismiss('src:ghost.m4a')"),
  goneRetry:gone.includes("rowRetry('src:ghost.m4a')"),
  hereRetry:here.includes("rowRetry('src:here.m4a')"),
  hereDismiss:here.includes('failDismiss'),
  nowRepeats:tray.innerHTML.includes('here.m4a')}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"goneVerb": True, "goneRetry": False,
                                    "hereRetry": True, "hereDismiss": False,
                                    "nowRepeats": False}
    # the failed row sits in Now, above every section
    assert "recording:0,processing:1,failed:2" in NEW_JS


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_sweep_that_deleted_nothing_says_why():
    """R9: every server-side skip answered {ok:true, deleted:[]}, which reads
    as full success -- no message at all, and the same Delete button
    re-rendered and silently no-opped on every further click."""
    fixture = "\n".join([
        "let DUPE_ARMED=true,DUPE_ERR='';let S={};",
        "let trays=0;function nowRedraw(){trays++;}",
        "let refreshes=0;function refresh(){refreshes++;}",
        "let reply=null;function api(){return Promise.resolve(reply);}",
        _js_fn("dupeDeleteGo"),
        """
(async()=>{
  reply={ok:true,deleted:[],skipped:[{name:'a.m4a',reason:'it is held'},
    {name:'b.m4a',reason:'the meeting it copied is gone'},
    {name:'c.m4a',reason:'it is held'}]};
  await dupeDeleteGo(null);
  const none={err:DUPE_ERR,armed:DUPE_ARMED};
  DUPE_ARMED=true;
  reply={ok:true,deleted:['a.m4a'],skipped:[],freed_mb:12.5};
  await dupeDeleteGo(null);
  const some={err:DUPE_ERR};
  DUPE_ARMED=true;
  reply={ok:true,deleted:[],skipped:[]};
  await dupeDeleteGo(null);
  const quiet={err:DUPE_ERR};
  console.log(JSON.stringify({none,some,quiet,trays,refreshes}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    # every distinct reason, once each, in the row's own detail line
    assert out["none"]["err"] == \
        "Nothing was deleted: it is held; the meeting it copied is gone."
    assert out["none"]["armed"] is False, "a finished sweep is never left armed"
    assert out["some"]["err"] == "", "a sweep that DID delete says nothing extra"
    assert out["quiet"]["err"] == \
        "Nothing was deleted: no waiting file is still an exact copy."


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_bulk_reports_a_rejected_call_and_keeps_the_selection():
    """W24: bulk had no try/catch and no caller caught the rejection either, so
    a network failure closed the popover, left nothing on screen, never cleared
    the selection, and surfaced only as an unhandled console rejection."""
    fixture = "\n".join([
        _js_oneline("esc"),
        "const SEL=new Set(['A','B']);",
        "function _selReady(){return [...SEL];}",
        "let results=[];function bulkResult(t,n,e){results.push([t,n,e]);}",
        "let hints=0;function archHint(){hints++;}",
        "let refreshes=0;function refresh(){refreshes++;}",
        "let mode='reject';",
        "function api(){return mode==='reject'"
        "?Promise.reject(new Error('network down'))"
        ":Promise.resolve({ok:false,results:[{base:'A',ok:true},"
        "{base:'B',ok:false,error:'busy'}],dropped:3});}",
        _js_fn("bulk"),
        """
(async()=>{
  await bulk('archive');
  const failed={results:results.slice(),sel:SEL.size,refreshes,hints};
  results=[];mode='answer';
  await bulk('archive');
  const partial={results:results.slice(),sel:SEL.size,refreshes};
  console.log(JSON.stringify({failed,partial}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, "the rejection must never escape bulk()"
    assert len(out["failed"]["results"]) == 1
    assert out["failed"]["results"][0][0] == "Could not reach the server"
    assert out["failed"]["results"][0][2] is True
    assert out["failed"]["sel"] == 2, \
        "a failed call must keep the selection so it can be retried"
    assert out["failed"]["refreshes"] == 0 and out["failed"]["hints"] == 0
    # a partial answer reports the per-item failures AND the dropped tail (W34)
    note = out["partial"]["results"][0]
    assert note[0] == "4 of 2 could not be done" or "could not be done" in note[0]
    assert "B: busy" in note[1] and "3 were over the 500-item limit" in note[1]
    assert out["partial"]["sel"] == 0 and out["partial"]["refreshes"] == 1


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_bulk_confirm_follows_the_live_selection():
    """W7: every bulk popover set an empty rowid, and afterRender's
    reposition-or-close check only fired on a TRUTHY rowid, so it never touched
    one. A poll that changed the selection under an open delete confirm left
    the stated count untouched: the user confirmed one number and a different
    number was deleted."""
    fixture = "\n".join([
        "const pop={dataset:{},innerHTML:'',hidden:true};",
        "const bar={dataset:{},innerHTML:'',hidden:false};",
        "function $(sel){return sel==='#rowmenu'?pop:(sel==='#bulkbar'?bar:null);}",
        "let sel=['A','B'];function _selReady(){return sel;}",
        "let closes=0;function closePop(){closes++;pop.dataset.open='';_bulkPop=null;}",
        "let poses=0;function _posPop(){poses++;}",
        "function openPop(el,anchor,fill){el.dataset.open='1';el.hidden=false;fill();}",
        "let _bulkPop=null;",
        _js_fn("openBulkPop"), _js_fn("bulkPopSync"), _js_fn("bulkDelete"),
        """
bulkDelete({});                       // the anchor is a stub object
const opened=pop.innerHTML;
sel=['A'];                            // a poll drops one meeting
bulkPopSync();
const afterPoll=pop.innerHTML;
sel=[];                               // and then the last one
bulkPopSync();
console.log(JSON.stringify({
  opened2:opened.includes('Delete 2 meetings?'),
  repainted1:afterPoll.includes('Delete 1 meeting?'),
  stale2:afterPoll.includes('Delete 2 meetings?'),
  closes,open:pop.dataset.open}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["opened2"] is True
    assert out["repainted1"] is True and out["stale2"] is False, \
        "the confirm must restate the live count, not the count it opened with"
    assert out["closes"] == 1 and out["open"] == "", \
        "an empty selection must close the confirm outright"
    # and the poll actually calls it
    assert "bulkPopSync();" in _js_fn("afterRender")
    # a popover carrying a typed value never repaints under the user (it would
    # wipe the half-typed name), but still closes at zero
    assert re.search(r"\},false\);", _js_fn("bulkRename"))
    assert re.search(r"\},false\);", _js_fn("bulkDate"))


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_an_archived_duplicate_is_named_but_never_linked():
    """R7, client half: dup_of can name an ARCHIVED meeting, but the chip
    linked it into the meeting view, which renders live meetings only -- a dead
    link under a permanent "Loading meeting" spinner."""
    fixture = "\n".join([
        _js_oneline("esc"), _js_fn("dupNote"),
        """
const arch=dupNote({dup_of:'Board Prep 05012026',dup_title:'Board Prep',
  dup_reason:'identical',dup_archived:true});
const live=dupNote({dup_of:'Board Prep 05012026',dup_title:'Board Prep',
  dup_reason:'identical',dup_archived:false});
console.log(JSON.stringify({
  archLink:arch.includes('<a '),archTitle:arch.includes('Board Prep'),
  archSays:arch.includes('(archived)'),
  liveLink:live.includes('href="#m/Board%20Prep%2005012026"'),
  liveSays:live.includes('(archived)'),
  none:dupNote({})}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["archLink"] is False, "an archived match must not render a link"
    assert out["archTitle"] is True and out["archSays"] is True
    assert out["liveLink"] is True and out["liveSays"] is False
    assert out["none"] == ""


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_waiting_row_renders_the_stage_breakdown():
    """W27: est_detail was computed on every poll for every queued file and
    never rendered. Either render it or stop computing it; it renders. A
    waiting row is one compact line since 2026-09-28, so the split rides the
    file name's tooltip, under the whole name, instead of a second line."""
    fixture = "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"), _js_fn("dupNote"),
        _js_region("function fmtEta(", "const STAGE_NICE"),
        _js_fn("slotActions"), "let S=null;", _js_fn("queueName"), _js_fn("queueBodyAndSlot"),
        _js_fn("bodyAndSlot"),
        """
const withDet=bodyAndSlot({state:'waiting',id:'src:a.m4a',title:'a.m4a',
  size_mb:42.1,est_minutes:9,est_detail:'transcribe ~6m \\u00b7 speakers ~3m'});
const without=bodyAndSlot({state:'waiting',id:'src:b.m4a',title:'b.m4a',
  size_mb:1.0,est_minutes:1});
console.log(JSON.stringify({
  tip:withDet.includes('title="a.m4a&#10;42.1 MB&#10;transcribe ~6m \\u00b7 speakers ~3m"'),
  oneLine:!withDet.includes('rmeta'),
  quiet:without.includes('title="b.m4a&#10;1 MB"')}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"tip": True, "oneLine": True, "quiet": True}
    # the field folds into the row signature, or a late estimate never repaints
    assert "r.est_detail," in _js_fn("sigOf")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_relabel_never_wipes_a_half_typed_transcript_search():
    """W30: relabelWatch's guard covers the naming panel, an open edit card,
    and a rename or date field -- and the find box, which mReloadSegs clears. A
    background relabel landing mid-typing used to wipe the query with no
    warning. The guard list survived the move from the falling edge to the
    per-meeting stamp."""
    fixture = "\n".join([
        "let reloads=0;",
        "function mReloadSegs(){reloads++;}",
        "let NP=null,route={view:'meeting',base:'B'};",
        "let MP={base:'B',built:true,stamp:'S1'};",
        "const find={value:''};",
        "const document={getElementById:id=>(id==='mfind'?find:null),"
        "querySelector:()=>null};",
        "const poll=at=>({timeline:[{id:'B',generated_at:at}]});",
        _js_fn("relabelWatch"),
        """
find.value='budget';                     // mid-typing an unsaved query
relabelWatch(poll('S2'));
const held={reloads,query:find.value,stamp:MP.stamp};
find.value='   ';                        // whitespace is not a query
relabelWatch(poll('S2'));
const blank={reloads};
find.value='';
relabelWatch(poll('S2'));
console.log(JSON.stringify({held,blank,after:{reloads}}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["held"] == {"reloads": 0, "query": "budget", "stamp": "S1"}, \
        "a finished relabel must not rebuild over a half-typed search"
    assert out["blank"] == {"reloads": 1}, \
        "whitespace alone is not a query worth holding the rebuild for"
    assert out["after"] == {"reloads": 1}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_uniquified_restore_says_what_it_came_back_as():
    """W32: dRestore ignored the response's base, so a restore that landed on a
    taken name and came back uniquified gave no on-screen sign that the meeting
    is no longer called what the user clicked Restore on."""
    fixture = "\n".join([
        "const err={hidden:true,textContent:''};",
        "const document={getElementById:id=>(id==='darcherr'?err:null)};",
        "let reply=null;function api(){return Promise.resolve(reply);}",
        "function dErr(id,r){if(r&&r.ok===false){err.hidden=false;"
        "err.textContent=r.error||'failed';}else err.hidden=true;}",
        "let loads=0,refreshes=0;",
        "function dArchLoad(){loads++;}function refresh(){refreshes++;}",
        _js_fn("dRestore"),
        """
(async()=>{
  reply={ok:true,base:'Board Prep 05012026 (2)'};
  await dRestore('Board Prep 05012026',{disabled:false});
  const moved={text:err.textContent,shown:!err.hidden};
  err.textContent='';err.hidden=true;
  reply={ok:true,base:'Clean Mtg 05012026'};
  await dRestore('Clean Mtg 05012026',{disabled:false});
  const same={text:err.textContent,shown:!err.hidden};
  console.log(JSON.stringify({moved,same,loads,refreshes}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["moved"]["shown"] is True
    assert "Board Prep 05012026 (2)" in out["moved"]["text"]
    assert out["same"] == {"text": "", "shown": False}, \
        "an ordinary restore stays quiet"
    assert out["loads"] == 2 and out["refreshes"] == 2


_NP_HARNESS = [
    "const els={};",
    "for(const k of ['#npname','#nperr','#npconfirm','#npsave','#npclear'])"
    "els[k]={value:'',hidden:true,textContent:'',innerHTML:'',disabled:false};",
    "function $(sel){return els[sel]||null;}",
    "function esc(s){return s;}",
    "let refreshes=0,reloads=0,closed=0;",
    "function refresh(){refreshes++;}",
    "function mReloadSegs(){reloads++;}",
    "function closeNamePanel(){closed++;NP=null;}",
    "let NP=null,MP=null,NPBUSY=false;",
]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_save_for_the_voice_still_shown_does_close_the_panel():
    """W14's other side: the identity check must not break the ordinary case.
    When the panel still shows the voice that was saved, it closes as before."""
    fixture = "\n".join(_NP_HARNESS + [
        "els['#npname'].value='Priya Shah';",
        "function api(){return Promise.resolve({ok:true,note:'relabeling'});}",
        _js_fn("npToken"), _js_fn("npConfirmBtns"), _js_fn("npBusy"),
        _js_fn("npSave"), _js_fn("saveName"), _js_fn("namedToast"),
        """
(async()=>{
  NP={uid:'U049',base:'B'};MP={base:'B'};
  await npSave();
  console.log(JSON.stringify({closed,refreshes,reloads,np:NP}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["closed"] == 1 and out["refreshes"] == 1 and out["reloads"] == 1
    assert out["np"] is None


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_forget_and_dismiss_close_only_the_panel_they_acted_on():
    """W14: npForget and npDismiss closed the panel unconditionally on success,
    from their own captured data alone. A slow request landing after the user
    had opened a SECOND panel force-closed that one and wiped the name being
    typed in it."""
    fixture = "\n".join(_NP_HARNESS + [
        "let release=null,posted=[];",
        "function api(p,body){posted.push({p,body});"
        "return new Promise(r=>{release=r;});}",
        _js_fn("npToken"), _js_fn("npConfirmBtns"), _js_fn("npBusy"),
        _js_fn("npForget"), _js_fn("npDismiss"),
        """
(async()=>{
  // forget on U049, then reopen for U050 while the request is in flight
  NP={uid:'U049',base:'A'};
  const p1=npForget();
  NP={uid:'U050',base:'A'};
  release({ok:true});await p1;
  const moved={closed,posted:posted.slice(),np:NP&&NP.uid};
  // the same call with the panel unchanged still closes
  posted=[];NP={uid:'U050',base:'A'};
  const p2=npForget();release({ok:true});await p2;
  const stayed={closed};
  // dismiss, scoped to its own meeting+cluster
  posted=[];NP={meeting:'A',speaker:'SPEAKER_02'};MP={base:'A'};
  const p3=npDismiss();
  NP={meeting:'A',speaker:'SPEAKER_03'};      // a different voice in the same meeting
  release({ok:true});await p3;
  const dMoved={closed,posted:posted.slice()};
  console.log(JSON.stringify({moved,stayed,dMoved}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["moved"]["closed"] == 0, \
        "a late forget closed a panel that had moved on to another voice"
    assert out["moved"]["posted"] == [{"p": "/api/forget", "body": {"uid": "U049"}}]
    assert out["moved"]["np"] == "U050"
    assert out["stayed"]["closed"] == 1, "the unchanged panel still closes"
    assert out["dMoved"]["closed"] == 1, \
        "a late dismiss must not close the panel showing another cluster"
    assert out["dMoved"]["posted"] == [
        {"p": "/api/dismiss_voice", "body": {"base": "A", "speaker": "SPEAKER_02"}}]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_panel_token_includes_the_meeting_in_uid_mode():
    """W15: npToken keyed the stale-response guard on the voice id alone in uid
    mode. Reopening the SAME uid's panel from a second meeting, while the first
    meeting's lines fetch was still in flight, let the late wrong-meeting
    response pass the guard and overwrite the lines block being read."""
    fixture = "\n".join([
        "const box={innerHTML:''};",
        "function $(sel){return sel==='#nplines'?box:null;}",
        "function esc(s){return s;}",
        "let NP=null;",
        "const pend={};",
        "function api(p){return new Promise(r=>{pend[p]=r;});}",
        _js_fn("npToken"), _js_fn("npLines"),
        """
(async()=>{
  NP={uid:'U1',base:'Meeting A'};
  const tokA=npToken();
  const pA=npLines('Meeting A','Speaker 1');
  const relA=pend['/api/voice_lines?base=Meeting%20A&speaker=Speaker%201'];
  NP={uid:'U1',base:'Meeting B'};
  const tokB=npToken();
  const pB=npLines('Meeting B','Speaker 1');
  const relB=pend['/api/voice_lines?base=Meeting%20B&speaker=Speaker%201'];
  relB({lines:[{start:5,dur:9,text:'MEETING B LINE'}],n:1,talk_secs:9});
  await pB;
  const afterB=box.innerHTML;
  relA({lines:[{start:1,dur:4,text:'MEETING A LINE'}],n:1,talk_secs:4});
  await pA;
  console.log(JSON.stringify({differ:tokA!==tokB,
    afterB:afterB.includes('MEETING B LINE'),
    final:box.innerHTML.includes('MEETING B LINE'),
    leaked:box.innerHTML.includes('MEETING A LINE')}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["differ"] is True, \
        "the same uid opened from two meetings must not share one token"
    assert out["afterB"] is True
    assert out["final"] is True and out["leaked"] is False, \
        "a late response from the wrong meeting overwrote the visible lines"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_floor_and_warn_confirms_cannot_double_save():
    """W23: only the original Save button disabled itself during a request. The
    floor strip's "Name for this meeting" and the warn strip's "Save anyway"
    are separate buttons with no guard, so a double click on either fired two
    independent saves -- and the local-name and non-uid enroll paths have no
    registry lock server-side to stop the second one."""
    fixture = "\n".join(_NP_HARNESS + [
        "els['#npname'].value='Jordan Lee';",
        "let posts=0,release=null;",
        "function api(){posts++;return new Promise(r=>{release=r;});}",
        "els['#npconfirm'].querySelectorAll=()=>btns;",
        "const btns=[{disabled:false},{disabled:false}];",
        _js_fn("npToken"), _js_fn("npConfirmBtns"), _js_fn("npBusy"),
        _js_fn("npSave"), _js_fn("saveName"), _js_fn("namedToast"),
        """
(async()=>{
  NP={meeting:'A',speaker:'SPEAKER_01'};MP={base:'A'};
  const p1=npSave(false,true);            // the floor strip's local save
  const p2=npSave(false,true);            // ...double-clicked
  const during={posts,btns:btns.map(b=>b.disabled),
    clear:els['#npclear'].disabled};
  release({ok:true,local:true,applied:true});
  await p1;await p2;
  const after={posts,btns:btns.map(b=>b.disabled)};
  // the warn strip's "Save anyway" is guarded by the same flag (the panel
  // closed on the success above, so reopen it first)
  NP={meeting:'A',speaker:'SPEAKER_01'};
  const p3=npSave(true);const p4=npSave(true);
  const warned={posts};
  release({ok:true});await p3;await p4;
  console.log(JSON.stringify({during,after,warned,posts}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["during"]["posts"] == 1, "the second click posted a duplicate save"
    assert out["during"]["btns"] == [True, True], \
        "the confirm buttons must disable while the request is in flight"
    assert out["during"]["clear"] is True
    assert out["after"] == {"posts": 1, "btns": [False, False]}
    assert out["warned"]["posts"] == 2, "the warn confirm posted twice"
    assert out["posts"] == 2


def test_a_locally_named_voice_gets_clear_name_not_not_a_real_speaker():
    """W29 and R11 together: the cluster-mode dismiss button showed for a voice
    that is already named locally. The click succeeded server-side, but the
    legend has no named-and-dismissed branch, so nothing visibly changed and
    the flag sat inert on the meeting json. A locally named voice gets the
    documented undo instead."""
    panel = _js_fn("openNamePanel")
    assert "cluster.local" in panel
    assert 'onclick="npClearName()"' in panel
    assert re.search(r"id=\"npclear\"", panel)
    # the local branch comes BEFORE the dismiss branch, so a locally named
    # cluster can never fall through to "Not a real speaker"
    assert panel.index("npClearName()") < panel.index('onclick="npDismiss()"')
    # the legend chip for a local name passes the flag
    assert re.search(
        r"openNamePanelByCluster\('\$\{escJs\(MP\.base\)\}','\$\{escJs\(o\.id\)\}',"
        r"'\$\{escJs\(w\)\}',true\)", _js_fn("mLegend"))
    # and an UNnamed cluster chip does not
    assert re.search(r"function openNamePanelByCluster\(meeting,speaker,display,local\)",
                     NEW_JS)


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_clear_name_posts_an_empty_local_name():
    """R11: the documented undo for a meeting-local label is an EMPTY name in
    the local branch. It was unreachable from the panel -- npSave refuses an
    empty name client-side and /api/name asserted one server-side."""
    fixture = "\n".join(_NP_HARNESS + [
        "let posted=null,reply={ok:true};",
        "function api(p,body){posted={p,body};return Promise.resolve(reply);}",
        _js_fn("npToken"), _js_fn("npConfirmBtns"), _js_fn("npBusy"),
        _js_fn("npClearName"),
        """
(async()=>{
  NP={meeting:'A',speaker:'SPEAKER_01',local:true};MP={base:'A'};
  await npClearName();
  const ok={posted,closed,refreshes,reloads};
  // a refusal keeps the panel open and says why
  NP={meeting:'A',speaker:'SPEAKER_01',local:true};
  reply={ok:false,error:'that voice is dismissed'};
  await npClearName();
  const bad={closed,err:els['#nperr'].textContent,shown:!els['#nperr'].hidden};
  // uid mode has no local entry to clear, so the control never fires there
  posted=null;NP={uid:'U1',base:'A'};
  await npClearName();
  console.log(JSON.stringify({ok,bad,uidPosted:posted}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["ok"]["posted"] == {
        "p": "/api/name",
        "body": {"meeting": "A", "speaker": "SPEAKER_01", "name": "", "local": True}}
    assert out["ok"]["closed"] == 1 and out["ok"]["reloads"] == 1
    assert out["bad"]["closed"] == 1, "a refusal must leave the panel open"
    assert out["bad"]["shown"] is True and "dismissed" in out["bad"]["err"]
    assert out["uidPosted"] is None


def test_an_archived_only_voice_points_at_restore_instead_of_a_dead_player():
    """W31: an archived-only reference got no distinct reason from
    /api/voice_clips, so the client fell back to a bare audio tag with no
    meeting hint, which 404s: a silently broken player where a message
    pointing at Restore belongs."""
    panel = _js_fn("openNamePanel")
    assert "r.reason==='sources_archived'" in panel
    assert "is archived" in panel and "Restore one" in panel
    # the older contract survives unchanged
    assert "r.reason==='sources_deleted'" in panel
    assert "The source recordings were deleted." in panel


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_duplicates_drawer_separates_never_scanned_from_none_found():
    """R8: the drawer asserted "No duplicate transcripts found" when no scan
    had ever run. The scan is idle-only, so a long batch keeps it from ever
    running, and a cold cache is indistinguishable from a scanned empty one
    without the server's own flag."""
    fixture = "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"),
        "const box={innerHTML:''};",
        "const document={getElementById:id=>(id==='ddupelist'?box:null)};",
        "const DRAWER={dupes:[],dupeScan:false,dupeScanned:undefined,"
        "dupeTrunc:false,dconfirm:null};",
        "function _dupeSide(){return '';}",
        _js_fn("dDupesRender"),
        """
DRAWER.dupeScanned=false;dDupesRender();
const cold=box.innerHTML;
DRAWER.dupeScanned=true;dDupesRender();
const scanned=box.innerHTML;
DRAWER.dupeScan=true;dDupesRender();
const running=box.innerHTML;
console.log(JSON.stringify({
  coldSays:cold.includes('Not compared yet'),
  coldClaims:cold.includes('No duplicate transcripts found'),
  scannedSays:scanned.includes('No duplicate transcripts found'),
  runningSays:running.includes('Comparing transcripts')}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"coldSays": True, "coldClaims": False,
                                    "scannedSays": True, "runningSays": True}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_failed_dupes_fetch_stops_the_poll_loop():
    """W21: the catch branch never reset dupeScan, so dDupesPoll refetched
    every two seconds forever with no stopping condition, while the empty state
    showed a false "Comparing transcripts..." the whole time."""
    fixture = "\n".join([
        "const sec={hidden:false,dataset:{built:'1'}};",
        "const errEl={hidden:true,textContent:''};",
        "const document={getElementById:id=>(id==='dsec-dupes'?sec:"
        "(id==='ddupeerr'?errEl:null))};",
        "let calls=0,mode='ok';",
        "function api(){calls++;return mode==='ok'"
        "?Promise.resolve({pairs:[],scanning:true,scanned:true})"
        ":Promise.reject(new Error('down'));}",
        "let renders=0;function dDupesRender(){renders++;}",
        "function dErr(id,r){if(r&&r.ok===false){errEl.hidden=false;"
        "errEl.textContent=r.error;}else errEl.hidden=true;}",
        "const DRAWER={section:'dupes',dupes:null,dupeScan:false,"
        "dupeScanned:undefined,dupeBusy:false,dupeTrunc:false,dupeSeen:undefined};",
        _js_fn("dDupesLoad"), _js_fn("dDupesPoll"),
        """
(async()=>{
  dDupesLoad();await new Promise(r=>setTimeout(r,0));
  const first={calls,scan:DRAWER.dupeScan};      // a scan is running: keep polling
  mode='fail';
  for(let i=0;i<5;i++){dDupesPoll({tray:[]});await new Promise(r=>setTimeout(r,0));}
  console.log(JSON.stringify({first,calls,scan:DRAWER.dupeScan,
    errShown:!errEl.hidden,err:errEl.textContent}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "threw" not in out, out
    assert out["first"] == {"calls": 1, "scan": True}
    assert out["scan"] is False, \
        "a fetch failure must clear dupeScan, or the poll never stops"
    assert out["calls"] == 2, \
        f"the poll refetched forever: {out['calls']} calls for one failure"
    assert out["errShown"] is True and "could not be loaded" in out["err"]


# ---------------------------------------------------------------------------
# Indigo Blueprint (2026-09-08): the year-to-month accordion. Ready meetings
# still sort AND group by meeting date, but the month sections now fold, and
# they nest under year sections once the library spans two or more years. The
# pinned actionable cluster is above all of it and never folds.
#
# These drive the SHIPPED functions (grpStamp / grpNest / grpOpen / grpSet /
# accordionHTML) under node, with a stubbed localStorage, so a persisted fold
# can be carried from one process to the next exactly as a reload carries it.
# ---------------------------------------------------------------------------
ACC_TODAY = "2026-09-08"          # a fixed "today" so the defaults are stable


def _js_region(start, end):
    """The source between two comment markers, so a block of small helpers can
    be loaded whole instead of named one at a time."""
    i = NEW_JS.index(start)
    j = NEW_JS.index(end, i)
    return NEW_JS[i:j]


def _acc_env(store="{}", search="", flagged="false", today=ACC_TODAY):
    """The accordion's real source over a stubbed browser."""
    return "\n".join([
        "const STORE={v:" + json.dumps(store) + "};",
        "const localStorage={getItem:k=>(k==='stt_groups'?STORE.v:null),"
        "setItem:(k,v)=>{if(k==='stt_groups')STORE.v=v;}};",
        "const SEARCH=" + json.dumps(search) + ";",
        "function $(sel){return sel==='#search'?{value:SEARCH}:null;}",
        "let LF={cat:'',sort:'date',flags:" + flagged + ",voices:false};",
        "let S=null;let DRAWS=0;function drawTimeline(){DRAWS++;}",
        "function esc(s){return (s??'').toString().replace(/[&<>\"]/g,"
        "c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));}",
        "function escJs(s){return esc(s).replace(/\\\\/g,'\\\\\\\\')"
        ".replace(/'/g,\"\\\\'\");}",
        "function rowHTML(r){return '<div class=\"row\" data-id=\"'+r.id+'\"></div>';}",
        "function afterRender(){}",
        # one column: a rail jump scrolls the window (two panes: the list pane)
        "let PANES_ON=false,paneJumps=[];function paned(){return PANES_ON;}",
        "function paneReveal(el,start){paneJumps.push([el.id,!!start]);}",
        "const TODAY=" + json.dumps(today) + ";",
        # curMonths() derives the current month from _isoLocal(new Date());
        # freezing _isoLocal at the stubbed TODAY freezes the whole default
        # rule, whatever day the suite runs on
        "function _isoLocal(d){return TODAY;}",
        "const BUCKET={today:'Today',yesterday:'Yesterday',undated:'Undated'};",
        "function dateKey(r){return r.date||((r.when||'').slice(0,10));}",
        _js_region("/* ==================== the year-to-month accordion",
                   "/* ============================ status pill "),
        _js_fn("ghead"),
        _js_fn("accordionHTML"),
        # the driver's own scaffolding: build groups the way drawTimeline does
        """
function mk(key,dates){return {key,rows:dates.map((d,n)=>({id:key+'#'+n,date:d}))};}
function build(groups){
  grpStamp(groups);
  const nest=grpNest(groups);
  const forced=grpForced();
  return {html:accordionHTML(groups,nest,forced),nest,forced};
}
function headers(html){return (html.match(/<button class="ghead [^"]*"/g)||[])
  .map(s=>s.replace('<button class="ghead ','').replace('"',''));}
function rowIds(html){return (html.match(/data-id="[^"]*"/g)||[])
  .map(s=>s.slice(9,-1));}
""",
    ])


def _run_node(js):
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_single_year_library_gets_months_and_no_year_headers():
    """A year with nothing to fold against earns no header line."""
    out = _run_node(_acc_env() + """
const g=build([mk('Today',['2026-09-08']),
               mk('September 2026',['2026-09-02']),
               mk('August 2026',['2026-08-14']),
               mk('March 2026',['2026-03-02'])]);
console.log(JSON.stringify({nest:g.nest,heads:headers(g.html),
  hasYearLine:g.html.includes('yhead')}));
""")
    assert out["nest"] is False
    assert out["hasYearLine"] is False, "a single-year library must show no year header"
    assert out["heads"] == ["mgroup", "mgroup", "mgroup", "mgroup"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_two_years_nest_months_under_year_sections():
    out = _run_node(_acc_env() + """
const g=build([mk('Today',['2026-09-08']),
               mk('August 2026',['2026-08-14']),
               mk('November 2025',['2025-11-03']),
               mk('May 2025',['2025-05-06'])]);
console.log(JSON.stringify({nest:g.nest,heads:headers(g.html),
  html:g.html}));
""")
    assert out["nest"] is True
    # the current year opens (its months follow it, indented); 2025 is folded,
    # so its month headers never render at all
    assert out["heads"] == ["yhead", "mgroup inyear", "mgroup inyear", "yhead"]
    assert "2025" in out["html"] and "NOVEMBER" not in out["html"].upper()
    # the year count is the sum of the months it holds
    assert '<span class="gcount mono">2</span>' in out["html"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_defaults_open_this_month_and_last_and_fold_the_rest():
    """DESIGN.md: the current month and the previous month open, every older
    month collapsed, the current year open, every prior year collapsed."""
    out = _run_node(_acc_env() + """
console.log(JSON.stringify({
  today:grpOpen('m-today'),yesterday:grpOpen('m-yesterday'),
  cur:grpOpen('m-2026-09'),prev:grpOpen('m-2026-08'),
  older:grpOpen('m-2026-07'),undated:grpOpen('m-undated'),
  curYear:grpOpen('y-2026'),priorYear:grpOpen('y-2025')}));
""")
    assert out == {"today": True, "yesterday": True, "cur": True, "prev": True,
                   "older": False, "undated": False,
                   "curYear": True, "priorYear": False}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_fold_persists_under_stt_groups_and_survives_a_rebuild():
    """The state is written per SECTION ID (date-derived, never the render
    index) under stt_groups, so it survives a poll, a reload, and a month
    arriving above it."""
    first = _run_node(_acc_env() + """
grpToggle('m-2026-09');            // fold the current month
grpToggle('m-2026-07');            // unfold an old one
console.log(JSON.stringify({store:STORE.v,draws:DRAWS}));
""")
    assert json.loads(first["store"]) == {"m-2026-09": "closed", "m-2026-07": "open"}
    assert first["draws"] == 0, "no S yet, so the toggle must not try to redraw"
    # a NEW process reads that store back: the folds come back exactly
    second = _run_node(_acc_env(store=first["store"]) + """
const g=build([mk('September 2026',['2026-09-02']),
               mk('August 2026',['2026-08-14']),
               mk('July 2026',['2026-07-09'])]);
console.log(JSON.stringify({open:{sep:grpOpen('m-2026-09'),
  aug:grpOpen('m-2026-08'),jul:grpOpen('m-2026-07')},rows:rowIds(g.html)}));
""")
    assert second["open"] == {"sep": False, "aug": True, "jul": True}
    # ...and the render obeys them: the folded month contributes no rows
    assert second["rows"] == ["August 2026#0", "July 2026#0"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_collapsed_section_renders_its_header_and_nothing_else():
    """The rows of a folded section are not in the DOM at all -- that is what
    keeps a 400-meeting library cheap."""
    out = _run_node(_acc_env(store='{"m-2026-03":"closed"}') + """
const g=build([mk('March 2026',['2026-03-02','2026-03-11','2026-03-20'])]);
console.log(JSON.stringify({heads:headers(g.html),rows:rowIds(g.html),
  count:g.html.includes('>3</span>'),chev:g.html.includes('&#9656;')}));
""")
    assert out["rows"] == [], "a collapsed section put its rows in the DOM"
    assert out["heads"] == ["mgroup"], "the header line must still render"
    assert out["count"] is True, "the header keeps its count while folded"
    assert out["chev"] is True, "a folded section shows the right-pointing chevron"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_search_and_the_flagged_filter_expand_every_section_then_restore():
    """A match must never hide behind a fold -- and the folds the user left
    must come back untouched when the filter clears."""
    store = '{"m-2026-03":"closed","y-2025":"closed"}'
    groups = """
const g=build([mk('March 2026',['2026-03-02']),
               mk('May 2025',['2025-05-06'])]);
console.log(JSON.stringify({forced:g.forced,rows:rowIds(g.html),
  heads:headers(g.html),store:STORE.v}));
"""
    folded = _run_node(_acc_env(store=store) + groups)
    assert folded["forced"] is False
    assert folded["rows"] == [], "both sections were folded"

    searching = _run_node(_acc_env(store=store, search="rivera") + groups)
    assert searching["forced"] is True
    assert searching["rows"] == ["March 2026#0", "May 2025#0"]
    assert searching["heads"] == ["yhead", "mgroup inyear", "yhead", "mgroup inyear"]
    # the store is untouched while filtering, which is what makes the restore
    # a restore rather than a re-guess
    assert json.loads(searching["store"]) == json.loads(store)

    flagging = _run_node(_acc_env(store=store, flagged="true") + groups)
    assert flagging["forced"] is True
    assert flagging["rows"] == ["March 2026#0", "May 2025#0"]

    restored = _run_node(_acc_env(store=searching["store"]) + groups)
    assert restored["forced"] is False and restored["rows"] == [], \
        "clearing the filter must restore the persisted folds"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_rail_click_opens_the_target_month_and_its_year_before_scrolling():
    """The rail must land on rows, not on a folded header."""
    fixture = "\n".join([
        _acc_env(store='{"m-2025-05":"closed","y-2025":"closed"}'),
        "let scrolled=null;let redraws=0;",
        "const document={getElementById:id=>({id,"
        "scrollIntoView(){scrolled=id;}})};",
        "S={};",
        "const LAST_GROUPS=grpStamp([{key:'',pinned:true,rows:[{id:'p0'}]},"
        "mk('March 2026',['2026-03-02']),mk('May 2025',['2025-05-06'])]);",
        "let LAST_NEST=true;",       # this fixture spans two years, so it nests
        _js_fn("railJump"),
        """
railJump(2);
console.log(JSON.stringify({scrolled,
  month:grpOpen('m-2025-05'),year:grpOpen('y-2025'),
  store:JSON.parse(STORE.v),drew:DRAWS>0}));
""",
    ])
    out = _run_node(fixture)
    assert out["month"] is True and out["year"] is True, \
        "the rail must unfold the target month AND the year holding it"
    assert out["store"] == {"m-2025-05": "open", "y-2025": "open"}, \
        "the unfold the rail just performed has to survive the next poll"
    assert out["drew"] is True, "the timeline must rebuild before the scroll"
    assert out["scrolled"] == "grp-2"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_rail_click_plants_no_year_key_without_year_sections():
    """A single-year library has no year headers, so a y-2026 key would be a
    fold nobody can ever see or undo."""
    out = _run_node("\n".join([
        _acc_env(),
        "let scrolled=null;",
        "const document={getElementById:id=>({id,scrollIntoView(){scrolled=id;}})};",
        "S={};",
        "const LAST_GROUPS=grpStamp([mk('March 2026',['2026-03-02']),"
        "mk('February 2026',['2026-02-04'])]);",
        "let LAST_NEST=false;",
        _js_fn("railJump"),
        "railJump(0);console.log(JSON.stringify({store:JSON.parse(STORE.v)}));",
    ]))
    assert out["store"] == {"m-2026-03": "open"}, \
        "with no year sections on screen, only the month key may be written"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_folded_prior_year_never_swallows_last_month_or_yesterday():
    """On the fifth of January the previous month lives in the PREVIOUS year.
    A year that defaults closed would hide December and Yesterday with it, so
    a year opens by default when it is current OR when it holds last month."""
    out = _run_node(_acc_env(today="2026-01-05") + """
const g=build([mk('Today',['2026-01-05']),mk('Yesterday',['2026-01-04']),
               mk('December 2025',['2025-12-18']),
               mk('November 2025',['2025-11-03'])]);
console.log(JSON.stringify({rows:rowIds(g.html),heads:headers(g.html),
  yearPrior:grpOpen('y-2025'),dec:grpOpen('m-2025-12'),
  nov:grpOpen('m-2025-11')}));
""")
    assert out["yearPrior"] is True, "2025 holds last month, so it opens"
    assert out["dec"] is True and out["nov"] is False
    assert out["rows"] == ["Today#0", "Yesterday#0", "December 2025#0"], \
        "December and Yesterday must be on screen; November stays folded"
    assert out["heads"].count("mgroup inyear") == 4, \
        "November keeps its header line while folded"

    # ...and Yesterday itself can sit in the prior year: Jan 1st
    out2 = _run_node(_acc_env(today="2026-01-01") + """
const g=build([mk('Today',['2026-01-01']),mk('Yesterday',['2025-12-31'])]);
console.log(JSON.stringify({rows:rowIds(g.html)}));
""")
    assert out2["rows"] == ["Today#0", "Yesterday#0"], \
        "a Yesterday dated into the prior year must still render"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_click_on_a_filter_forced_header_stores_nothing():
    """Every section is open under a search, so the click has nothing to
    change on screen -- and must not quietly rewrite the folds underneath."""
    out = _run_node(_acc_env(search="rivera") + """
S={};
grpToggle('m-2026-09');
grpToggle('y-2026');
console.log(JSON.stringify({store:STORE.v,draws:DRAWS}));
""")
    assert json.loads(out["store"]) == {}, \
        "a forced-open header must write nothing to stt_groups"
    assert out["draws"] == 0, "nothing changed on screen, so nothing redraws"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_pinned_cluster_sits_above_every_section_and_never_folds():
    """A failed April file belongs at the top, not inside a folded April."""
    out = _run_node(_acc_env(store='{"m-2026-09":"closed","y-2025":"closed"}') + """
const g=build([{key:'',pinned:true,rows:[{id:'FAILED-APRIL'},{id:'WAITING'}]},
               mk('September 2026',['2026-09-02']),
               mk('May 2025',['2025-05-06'])]);
console.log(JSON.stringify({rows:rowIds(g.html),heads:headers(g.html),
  firstIsRow:g.html.indexOf('data-id="FAILED-APRIL"')<g.html.indexOf('<button')}));
""")
    assert out["firstIsRow"] is True, "the pinned cluster must precede every header"
    assert out["rows"] == ["FAILED-APRIL", "WAITING"], \
        "the pinned rows render unfolded while both date sections stay folded"
    # 2026 is open so its (folded) month line shows; 2025 is folded, so its
    # month line does not. Neither header belongs to the pinned cluster.
    assert out["heads"] == ["yhead", "mgroup inyear", "yhead"], \
        "the pinned cluster contributes no header of its own"


def test_the_accordion_keeps_the_sort_and_group_by_meeting_date_rule():
    """Sorting by one key while grouping by another is what fragmented the
    months; the accordion changes the presentation, never the ordering."""
    body = _js_fn("drawTimeline")
    assert "dateKey(b).localeCompare(dateKey(a))" in _js_fn("timelineSections")
    assert "for(const r of sec.rest){" in body
    assert "const key=dateBucket(dateKey(r));" in body
    # the fold state is part of the render signature, or a toggle would be
    # undone by the very next 2s poll
    assert "const gsig=" in body and "'|'+gsig" in body
    # section ids are DATE-derived, never the render index
    assert "'m-'+(k.slice(0,7)" in _js_fn("grpMonthId")
    assert "'y-'+g.year" in _js_fn("accordionHTML")
    # the persisted key the spec names
    assert "const GRP_KEY='stt_groups'" in NEW_JS


def test_a_fold_takes_its_orphans_with_it():
    """Folding a section removes rows from the DOM. Whatever was hanging off
    one of those rows -- the clip player, the selection, the focus ring --
    has to be reconciled in the same breath, so both fold paths run the
    post-render hook and a homeless clip stops instead of playing on blind."""
    for name in ("grpToggle", "railJump"):
        body = _js_fn(name)
        assert "drawTimeline(S);afterRender();" in body, \
            f"{name} rebuilt the list without afterRender()"
    mount = _js_fn("mountClip")
    assert "if(!row){stopClip();return;}" in mount, \
        "a clip whose row is gone must stop, not linger"


def test_the_selection_never_reaches_a_row_a_fold_hides():
    """"N selected" has to equal the checked boxes a user can see, and a bulk
    delete must not reach a row hidden inside a folded month. A search or
    category filter is NOT a fold: the selection survives it, as before."""
    out = _run_node("\n".join([
        "let S={timeline:[{id:'a',state:'ready'},{id:'b',state:'ready'},"
        "{id:'c',state:'ready'},{id:'d',state:'ready'}]};",
        "const SELECTABLE=new Set(['ready']);",
        "let SEL=new Set(['a','b','c','d']);",
        # a (open month), b (folded month), c (open month in a folded year),
        # d (filtered out of the DOM by a search, but its group is open)
        "let LAST_GROUPS=[{pinned:true,rows:[]},"
        "{id:'m-1',open:true,yearOpen:true,rows:[{id:'a'}]},"
        "{id:'m-2',open:false,yearOpen:true,rows:[{id:'b'}]},"
        "{id:'m-3',open:true,yearOpen:false,rows:[{id:'c'}]},"
        "{id:'m-4',open:true,yearOpen:true,rows:[{id:'d'}]}];",
        "const document={querySelectorAll:()=>[],body:{classList:{toggle(){}}}};",
        "function drawBulkBar(){}",
        _js_fn("applySel"),
        "applySel();console.log(JSON.stringify([...SEL].sort()));",
    ]))
    assert out == ["a", "d"], "folded rows leave the selection; filtered rows stay"
    # the shift-click range is DOM-based ON PURPOSE, and says so
    assert "folded" in _js_fn("toggleSel").lower()
    # "select all shown" is DOM-based and its label already promises only that
    assert "querySelectorAll('#timeline .row[data-state=\"ready\"] .chk')" \
        in _js_fn("selAllShown")


def test_the_ring_reaches_a_folded_section_header():
    """j/k must be able to land on a section header, or a folded month is
    unreachable without the mouse. Enter there is the button's own click."""
    ring = re.search(r"(?m)^const KB_RING=.*;$", NEW_JS).group(0)
    assert "KB_RING" in _js_oneline("kbRows")
    assert ".ghead" in ring, "kbRows must collect the section headers too"
    assert ".row[tabindex]" in ring, "and the rows it always collected"
    body = _kb_body()
    # the header branch returns BEFORE the row handling and never preventDefaults
    head = body[body.index("matches('#timeline .ghead')"):]
    assert head.startswith("matches('#timeline .ghead'))return;"), \
        "Enter on a header must return early, so the native click fires"
    assert body.index(".ghead") < body.index("e.target.matches('#timeline .row[tabindex]')")


def test_the_bucket_labels_are_named_once():
    """dateBucket produces the three special labels and the accordion matches
    on them; two copies of the literal is how they drift apart."""
    assert "const BUCKET={today:'Today',yesterday:'Yesterday',undated:'Undated'};" \
        in NEW_JS
    month = _js_fn("grpMonthId")
    assert "'Today'" not in month and "'Yesterday'" not in month \
        and "'Undated'" not in month, "grpMonthId still compares a literal label"
    assert "BUCKET.today" in month and "BUCKET.undated" in month
    assert "BUCKET.undated" in _js_fn("grpYear")
    assert "BUCKET.today" in _js_fn("drawRail")
    # ...and the rail reads the stamped year instead of re-parsing the label
    assert "const yr=g.year||''" in _js_fn("drawRail")


def test_the_open_state_is_computed_once_per_render():
    """The open rule lived in five places; it now lives in grpStates, and both
    the render signature and the markup read the two flags it stamps."""
    states = _js_fn("grpStates")
    assert "g.open=forced||grpOpen(g.id)" in states
    assert "g.yearOpen=" in states and "'y-'+g.year" in states
    draw = _js_fn("drawTimeline")
    assert "grpStates(groups,nest,forced)" in draw
    assert "g.id+':'+(g.open?1:0)+':'+(g.yearOpen?1:0)" in draw, \
        "the signature must read the stamped flags, not re-derive the rule"
    acc = _js_fn("accordionHTML")
    assert "grpOpen(" not in acc, "accordionHTML must not re-derive the open rule"
    assert "g.open" in acc and "g.yearOpen" in acc
    # a direct caller (the node harness, and any future one) still works
    assert "g.open===undefined" in acc


def test_the_section_header_line_carries_the_whole_anatomy():
    """chevron, eyebrow, hairline rule, count -- and the whole line is the
    click target (DESIGN.md, Indigo Blueprint)."""
    head = _js_fn("ghead")
    assert head.lstrip().startswith("function ghead(")
    for part in ('class="gchev"', 'class="glabel"', 'class="grule"',
                 'class="gcount mono"'):
        assert part in head, f"missing header part: {part}"
    assert "&#9662;" in head and "&#9656;" in head, "down / right chevrons"
    assert 'aria-expanded="${open?' in head
    # the whole line is one button, and the label is escaped
    assert "<button class=" in head and "esc(label)" in head
    assert "escJs(id)" in head
    # the CSS: the rule fills the width, --line under a year and --hair under
    # a month, and the year eyebrow is heavier and inked
    assert re.search(r"(?m)^\.grule\{[^}]*flex:1[^}]*background:var\(--hair\)", NEW_CSS)
    assert ".yhead .grule{background:var(--line)}" in NEW_CSS
    assert re.search(r"\.yhead \.glabel\{font:700 14px[^}]*color:var\(--ink\)", NEW_CSS)
    assert re.search(r"(?m)^\.mgroup\{font:600 13px[^}]*color:var\(--sub\)", NEW_CSS)


# ---------------------------------------------------------------------------
# The contrast gate. tools/token_contrast.py reads the four token blocks out
# of the shipped sheet and measures every pair the design paints as text on a
# ground. Running it here means a future token edit cannot quietly drop a pair
# below 4.5:1 -- the promise every colorway round has made since Signal.
# ---------------------------------------------------------------------------
def test_every_token_text_pair_clears_4_5_to_1_in_both_themes():
    from tools.token_contrast import AA, audit

    rows = audit()
    assert rows, "the contrast audit measured nothing -- its block regex broke"
    # all four token blocks were found and measured
    assert len({r.block for r in rows}) == 4
    bad = [r.line() for r in rows if not r.ok]
    assert not bad, "token pairs below %.1f:1:\n%s" % (AA, "\n".join(bad))
    # the one pair that missed on the spec's first draft is recorded in the
    # sheet as a hue-true darkening, not a hue swap
    assert "hue-true" in NEW_CSS.lower()
    light = [r for r in rows if r.block == "light :root"]
    rec = next(r for r in light if (r.fg, r.bg) == ("rec-text", "rec-soft"))
    assert rec.fg_hex == "#BC342E" and rec.ratio >= AA


def test_the_audit_measures_the_pairs_the_sheet_actually_paints():
    """The hand-written PAIRS list had drifted: two pairs the stylesheet
    paints as text on a ground were never measured. The audit now scans the
    sheet for them, so a new rule cannot add an unmeasured pair."""
    from tools.token_contrast import audit, painted_pairs

    painted = painted_pairs(NEW_CSS)
    assert ("accent-text", "accent-soft") in painted
    assert ("ink", "rec-soft") in painted

    measured = {(r.fg, r.bg) for r in audit()}
    assert ("accent-text", "accent-soft") in measured, \
        "a painted pair the hand list missed is still unmeasured"
    assert ("ink", "rec-soft") in measured
    # every block measures the same set, and nothing is measured twice
    per_block = {}
    for r in audit():
        per_block.setdefault(r.block, []).append((r.fg, r.bg))
    for block, pairs in per_block.items():
        assert len(pairs) == len(set(pairs)), f"{block} measured a pair twice"


def test_the_dark_token_block_stops_at_its_own_closing_brace():
    """The @media pattern opens TWO braces. Counting from the start of the
    match closed the @media wrapper instead of the :root inside it, so every
    later rule in that media query was read as a token declaration."""
    from tools.token_contrast import read_tokens

    css = "@media(prefers-color-scheme:dark){:root{--a:#000;} .x{--b:#fff;}}"
    tokens = read_tokens(css, r"(?m)^@media\(prefers-color-scheme:dark\)\{:root\{")
    assert tokens == {"a": "#000"}, \
        "the slice must end at :root's brace, not the media query's"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_save_says_how_many_other_meetings_are_updating():
    """A naming used to answer with a note the panel threw away, and the user
    watched a spinner for seven minutes with no idea what was happening. The
    response carries the target list, and the save says what it did and
    where. "here" only when the server APPLIED the meeting the save was made
    from: a uid save used to toast "Named here." while the open page was
    merely queued."""
    out = _run_node("\n".join([
        "let out=[];function toast(m){out.push(m);}",
        _js_fn("namedToast"),
        """
namedToast({ok:true,applied:'A',targets:['B','C','D']},'A');
namedToast({ok:true,applied:'A',targets:['B']},'A');
namedToast({ok:true,applied:'A',targets:[]},'A');
namedToast({ok:true,applied:null,targets:['A','B']},'A');
namedToast({ok:true,applied:null,targets:['A']},'A');
namedToast({ok:true,applied:null,targets:['B','C']},'');
namedToast({ok:true,applied:'A',targets:['B']},'Z');
namedToast({ok:true,applied:null,targets:[]},'');
namedToast({ok:true,applied:'A',targets:['--all']},'A');
namedToast({ok:true,applied:null,targets:['--all']},'A');
namedToast({ok:true},'A');
namedToast(null,'A');
console.log(JSON.stringify(out));
"""]))
    assert out == [
        "Named here. Updating 3 other meetings.",
        "Named here. Updating 1 other meeting.",
        "Named here.",
        "Named. Updating 2 meetings.",
        "Named. Updating 1 meeting.",
        "Named. Updating 2 meetings.",
        "Named. Updating 1 meeting.",
        "Named.",
        "Named here. Updating all meetings.",
        "Named. Updating all meetings.",
    ], "no targets field says nothing; an applied meeting that is not the " \
       "one open is not 'here'; the fallback pass is 'all meetings'"


def _save_env(reply):
    """The real saveName and namedToast, with the slide-over's elements and
    a server that answers `reply` to every post."""
    return "\n".join([
        "const els={};",
        "for(const k of ['#npname','#nperr','#npconfirm','#npsave'])"
        "els[k]={value:'',hidden:true,textContent:'',innerHTML:'',disabled:false};",
        "els['#npname'].value='Priya Shah';",
        "function $(sel){return els[sel]||null;}",
        "function esc(s){return s;}",
        "let posted=[],toasts=[],MP=null;",
        "function toast(m){toasts.push(m);}",
        "function api(p,b){posted.push(b);return Promise.resolve("
        + json.dumps(reply) + ");}",
        "function busy(){}",
        "const ui={name:els['#npname'],err:els['#nperr'],warn:els['#npconfirm'],"
        "warnId:'npconfirm',btn:els['#npsave'],label:'Save name'};",
        _js_fn("saveName"), _js_fn("namedToast"),
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_uid_save_sends_the_open_meeting_and_says_here_only_when_applied():
    """A uid save sent no meeting, so the server relabeled nothing inside the
    response, and the page the user had open was only queued while the toast
    said "Named here." The open page now rides along, and the toast reads the
    response: "here" only when `applied` IS that page."""
    out = _run_node(_save_env({"ok": True, "applied": "M1",
                               "targets": ["M2", "M3"]}) + """
(async()=>{
  const o={};
  // a meeting page is open: the uid save carries it
  MP={base:'M1'};
  const p=saveName({uid:'U058',meeting:'',speaker:undefined,busy,again:'npSave',ui});
  MP={base:'Elsewhere'};                 // navigated away mid-flight
  o.r=await p;
  o.withPage={posted:posted.slice(),toasts:toasts.slice()};
  // the drawer with no page open: no meeting, and never "here"
  posted=[];toasts=[];MP=null;
  await saveName({uid:'U058',busy,again:'npSave',ui});
  o.noPage={posted:posted.slice(),toasts:toasts.slice()};
  // a cluster save is unchanged: its own meeting, which the server applies
  posted=[];toasts=[];MP={base:'M1'};
  await saveName({uid:null,meeting:'M1',speaker:'SPEAKER_01',busy,again:'npSave',ui});
  o.cluster={posted:posted.slice(),toasts:toasts.slice()};
  console.log(JSON.stringify(o));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
""")
    assert "threw" not in out, out
    assert out["r"]["applied"] == "M1", "saveName still resolves to the response"
    assert out["withPage"]["posted"] == [{"uid": "U058", "name": "Priya Shah",
                                          "confirm": False, "meeting": "M1"}], \
        "the page open when Save was pressed, captured before the await"
    assert out["withPage"]["toasts"] == ["Named here. Updating 2 other meetings."]
    assert out["noPage"]["posted"] == [{"uid": "U058", "name": "Priya Shah",
                                        "confirm": False}]
    assert out["noPage"]["toasts"] == ["Named. Updating 2 meetings."], \
        "no page open: nothing was applied here, so the toast must not say so"
    assert out["cluster"]["posted"] == [{"meeting": "M1", "speaker": "SPEAKER_01",
                                         "name": "Priya Shah", "confirm": False,
                                         "local": False}]
    assert out["cluster"]["toasts"] == ["Named here. Updating 2 other meetings."]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_uid_save_whose_open_meeting_was_only_queued_does_not_say_here():
    """The relabel lock was busy: the server queued the open page with the
    rest (applied null, the page first in targets). The toast counts it among
    the meetings updating, and never calls it done."""
    out = _run_node(_save_env({"ok": True, "applied": None,
                               "targets": ["M1", "M2"]}) + """
(async()=>{
  MP={base:'M1'};
  await saveName({uid:'U058',busy,again:'npSave',ui});
  console.log(JSON.stringify({posted,toasts}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
""")
    assert "threw" not in out, out
    assert out["posted"][0]["meeting"] == "M1"
    assert out["toasts"] == ["Named. Updating 2 meetings."]


# ---------------------------------------------------------------------------
# Phase 5 (docs/relabel-redesign.md): the naming preview, the drawer's
# suggestion lines, and the per-row flash when a relabel lands. Same
# regex-over-the-files style, plus node for the behavior.
# ---------------------------------------------------------------------------

def test_the_naming_panel_asks_for_a_preview_without_waiting_for_it():
    """The box is focused and typeable in the same tick the panel opens; the
    line arrives behind it. A preview that never answers must leave a working
    panel, not a blocked one."""
    panel = _js_fn("openNamePanel")
    assert '<div id="nppreview" class="nppreview" hidden></div>' in panel
    # the field comes first, then the fire-and-forget call: NOT awaited
    assert panel.index('id="npname"') < panel.index('id="nppreview"')
    assert re.search(r"\$\('#npname'\)\.focus\(\);\s*\n(?:\s*//[^\n]*\n)*\s*npPreview\(\);",
                     panel), "npPreview must be called, unawaited, after focus"
    assert "await npPreview()" not in NEW_JS
    prev = _js_fn("npPreview")
    assert "'/api/name_preview'" in prev
    assert "{uid:NP.uid}" in prev and "{meeting:NP.meeting,speaker:NP.speaker}" in prev
    # the same identity guard every other async block in this panel carries
    assert "npToken()!==tok" in prev
    # a failure paints NOTHING: no catch branch writes into the box
    assert "catch(e){return;}" in prev


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_preview_line_counts_the_other_meetings_not_this_one():
    """"here" is not an "other meeting", and a voice heard nowhere else says so
    plainly rather than counting to zero."""
    out = _run_node("\n".join([
        _js_fn("npPreviewLine"),
        """
const lines=[
  npPreviewLine({here:'A',targets:['A']}),
  npPreviewLine({here:'A',targets:[]}),
  npPreviewLine({here:'A',targets:['A','B']}),
  npPreviewLine({here:'A',targets:['A','B','C','D','E']}),
  npPreviewLine({here:null,targets:['A','B']}),
];
console.log(JSON.stringify(lines));
"""]))
    assert out == [
        "Names this voice here only",
        "Names this voice here only",
        "Names this voice here and in 1 other meeting",
        "Names this voice here and in 4 other meetings",
        "Names this voice here and in 2 other meetings",
    ]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_looks_like_chip_fills_the_name_box_and_saves_nothing():
    """The margin rule refused to name these clusters automatically, and that
    is right. The chip hands the guess to the human and stops there."""
    out = _run_node("\n".join([
        _js_oneline("esc"), _js_oneline("escJs"),
        "let box={value:'',focused:0,focus(){this.focused++;}};",
        "let closed=0;",
        "const $=q=>(q==='#npname'?box:null);",
        "function pnClose(){closed++;}",
        "let posted=[];",
        "async function api(p,b){posted.push([p,b]);return {};}",
        _js_fn("npPreviewLine"), _js_fn("npPreviewHTML"), _js_fn("npUseName"),
        """
const html=npPreviewHTML({here:'A',targets:['A','B'],
  looks_like:[{name:'Riley Stone',score:0.8412},{name:"O'Hara",score:0.63}]});
npUseName('Riley Stone');
console.log(JSON.stringify({html,value:box.value,focused:box.focused,
  closed,posted}));
"""]))
    # the chip carries the name and the score, in the existing chip class
    assert 'class="nchip"' in out["html"]
    assert "Looks like Riley Stone &middot; " in out["html"]
    assert '<span class="mono">0.84</span>' in out["html"]
    assert 'class="nchips nppchips"' in out["html"]
    # an apostrophe in a name must not break out of the onclick attribute
    assert "npUseName('O\\'Hara')" in out["html"]
    # the click fills the box, focuses it, closes the autocomplete, posts NOTHING
    assert out["value"] == "Riley Stone"
    assert out["focused"] == 1 and out["closed"] == 1
    assert out["posted"] == [], "a chip is evidence, never a save"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_link_merges_the_two_unknown_ids_and_says_what_it_reached():
    """The drawer's "possibly the same voice" line, clicked: one merge by uid
    on each side, and the save's own sentence from the response's targets."""
    out = _run_node("\n".join([
        "let posted=[],toasts=[],refreshes=0;",
        "let DRAWER={spkErr:'old error'};",
        "function dSpeakersForce(){}",
        "function toast(m){toasts.push(m);}",
        "function refresh(){refreshes++;}",
        "async function api(p,b){posted.push([p,b]);"
        "return {ok:true,targets:['B','C','D']};}",
        _js_fn("dLinkUnknown"), _js_fn("namedToast"),
        """
dLinkUnknown('U050','U012').then(()=>{
  console.log(JSON.stringify({posted,toasts,refreshes,err:DRAWER.spkErr}));
});
"""]))
    assert out["posted"] == [["/api/merge_speakers",
                              {"src": "uid:U050", "dst": "uid:U012"}]]
    assert out["toasts"] == ["Named. Updating 3 meetings."], \
        "the same sentence a save gets, and never 'here': a merge applies " \
        "no meeting inside the request"
    assert out["refreshes"] == 1
    assert out["err"] is None


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_link_that_fails_says_so_in_the_drawer_and_toasts_nothing():
    out = _run_node("\n".join([
        "let toasts=[],refreshes=0,redraws=0;",
        "let DRAWER={spkErr:null};",
        "function dSpeakersForce(){redraws++;}",
        "function toast(m){toasts.push(m);}",
        "function refresh(){refreshes++;}",
        "async function api(p,b){return {ok:false};}",
        _js_fn("dLinkUnknown"),
        """
dLinkUnknown('U050','U012').then(()=>{
  console.log(JSON.stringify({toasts,refreshes,redraws,err:DRAWER.spkErr}));
});
"""]))
    assert out["toasts"] == [] and out["refreshes"] == 0
    assert out["redraws"] == 1
    assert "Could not link" in out["err"]


def test_the_drawer_suggestion_lines_render_one_candidate_each():
    """One line per kind, best candidate only: a wall of guesses is a decision,
    and this is a glance."""
    sugg = _js_fn("dSuggHTML")
    assert "Possibly the same voice as" in sugg and "Looks like" in sugg
    assert ">Link</button>" in sugg and ">Name</button>" in sugg
    assert "dLinkUnknown(" in sugg and "dNameUnknownAs(" in sugg
    assert "[0];" in sugg, "best candidate only, both kinds"
    # a row with neither renders nothing at all
    assert "if(!sug&&!look)return '';" in sugg
    # the Name button opens the panel with the name already in the box
    name = _js_fn("dNameUnknownAs")
    assert "openNamePanelByUid(uid);" in name and "npUseName(name);" in name
    # and both fields ride the drawer's change signature, or a suggestion that
    # lands a poll later never appears on a row already on screen
    sig = _js_fn("dSpeakersSig")
    assert "u.suggested" in sig and "u.looks_like" in sig
    # the unknown row renders its suggestions right under itself
    assert "</div>${dSuggHTML(u)}`" in _js_fn("dSpeakersHTML")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_row_flashes_only_when_its_own_stamp_moved():
    """A relabel rewrites transcripts nobody is looking at, and in a hundred
    rows a name changing three cards down is invisible. The flash marks exactly
    the meetings whose bytes moved between two polls: not a row that merely
    appeared, not a row that stood still, and not a row that lost its stamp."""
    out = _run_node("\n".join([
        "let FLASH_MS=15;",     # the real 2000 is asserted against the source below
        "const rows=['A','B','C','NEW'].map(id=>({dataset:{id},cls:[],"
        "classList:{add(c){this.__o.cls.push('+'+c);},"
        "remove(c){this.__o.cls.push('-'+c);}}}));",
        "rows.forEach(r=>{r.classList.__o=r;});",
        "const document={querySelectorAll:()=>rows};",
        _js_fn("stampsMoved"), _js_fn("flashRows"),
        """
const prev={timeline:[{id:'A',generated_at:'S1'},{id:'B',generated_at:'S1'},
                      {id:'C',generated_at:null}]};
const next={timeline:[{id:'A',generated_at:'S2'},{id:'B',generated_at:'S1'},
                      {id:'C',generated_at:'S9'},{id:'NEW',generated_at:'S9'}]};
const moved=stampsMoved(prev,next);
const firstPoll=stampsMoved(null,next);
flashRows(moved);
const added=rows.map(r=>r.cls.join(''));
setTimeout(()=>{
  console.log(JSON.stringify({moved,firstPoll,added,
    after:rows.map(r=>r.cls.join(''))}));
},60);
"""]))
    assert out["moved"] == ["A"], \
        "B stood still, C had no stamp to move from, NEW was not there before"
    assert out["firstPoll"] == [], "the first poll of a session flashes nothing"
    assert out["added"] == ["+updated", "", "", ""]
    assert out["after"] == ["+updated-updated", "", "", ""], \
        "the class comes off on its own"


def test_the_flash_reads_the_previous_state_and_never_the_signature():
    """It must not be possible for a flash to force a list rebuild: the stamps
    are diffed against the state object being REPLACED, and the class goes on
    after render() has already run."""
    src = _js_fn("refresh")
    assert "const prev=S;" in src
    assert src.index("render();") < src.index("flashRows(stampsMoved(prev,S))")
    # the meeting page answers a moved stamp by reloading itself instead
    assert "route.view!=='meeting'" in src
    # generated_at is NOT in the row change signature (sigOf), and must not be:
    # every relabel would rebuild the whole list
    assert "generated_at" not in _js_fn("sigOf")
    assert re.search(r"(?m)^const FLASH_MS=2000;", NEW_JS), "two seconds"


def test_the_updated_row_tint_uses_an_existing_token_and_honors_reduced_motion():
    m = re.search(r"(?m)^\.row\.updated\{[^}]*\}", NEW_CSS)
    assert m, "no .row.updated rule"
    assert "var(--accent-soft)" in m.group(0), "no new colors (DESIGN.md Indigo)"
    # the tint fades through the row's own transition...
    row = re.search(r"(?m)^\.row\{[^}]*\}", NEW_CSS)
    assert row and "background-color" in row.group(0)
    # ...and there is no fade at all under reduced motion
    blocks = re.findall(r"@media\(prefers-reduced-motion:reduce\)\{[^}]*\{[^}]*\}[\s\S]*?\n\}",
                        NEW_CSS)
    covering = [b for b in blocks if ".row.updated" in b]
    assert covering, "the reduced-motion block must name .row.updated"
    assert "transition:none" in covering[0]


# ---------------------------------------------------------------------------
# The finish flow (docs/ui-refresh/proposal.md, phase 1): the step strip on
# the meeting page, the inline Voices step, and the hand-off to the next
# meeting to finish. Pure functions and key handling run through node over
# the extracted source; placement is regex over the files.
# ---------------------------------------------------------------------------
def _js_const(name):
    m = re.search(r"(?m)^const " + re.escape(name) + r"=.*;$", NEW_JS)
    assert m, f"missing const: {name}"
    return m.group(0)


def _finish_env():
    return "\n".join([
        _js_const("FINISH_DAYS"), _js_const("FS_LABEL"),
        _js_oneline("esc"),
        _js_fn("finishSteps"), _js_oneline("fsCur"), _js_fn("toFinish"),
        _js_fn("nextToFinish"), _js_fn("fsStripHTML"), _js_fn("_fsNextBtn"),
        _js_fn("fsDoneHTML"),
        "const NOW=Date.parse('2026-09-27T12:00:00');",
        "const fin=(title,voices,flags)=>({title,voices,flags,"
        "done:title&&!voices&&!flags});",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_finish_steps_walk_title_voices_flags_in_order():
    out = _run_node(_finish_env() + """
const st=(f,past)=>finishSteps({finish:f},past).map(s=>[s.key,s.state,s.count]);
console.log(JSON.stringify({
  title:st(fin(false,2,6)),
  voices:st(fin(true,2,6)),
  flags:st(fin(true,0,6)),
  done:st(fin(true,0,0)),
  skipped:st(fin(true,1,6),['voices']),
  skippedLast:st(fin(true,1,0),['voices']),
  none:st(undefined),
}));
""")
    assert out["title"] == [["title", "cur", 0], ["voices", "open", 2],
                            ["flags", "open", 6], ["done", "open", 0]]
    assert out["voices"] == [["title", "done", 0], ["voices", "cur", 2],
                             ["flags", "open", 6], ["done", "open", 0]]
    assert out["flags"] == [["title", "done", 0], ["voices", "done", 0],
                            ["flags", "cur", 6], ["done", "open", 0]]
    assert out["done"] == [["title", "done", 0], ["voices", "done", 0],
                           ["flags", "done", 0], ["done", "done", 0]]
    # a skipped voice keeps Voices open, and the next open step is current
    assert out["skipped"][1] == ["voices", "open", 1]
    assert out["skipped"][2] == ["flags", "cur", 6]
    # nothing left to do but the skipped voice: Done is current, not done
    assert out["skippedLast"][3] == ["done", "cur", 0]
    assert out["none"][3] == ["done", "done", 0], "no record means nothing to finish"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_next_to_finish_wraps_skips_the_current_and_honors_fourteen_days():
    out = _run_node(_finish_env() + """
const rows=[
  {id:'rec:x',state:'recording'},
  {id:'A',state:'needs_name',when:'2026-01-01T09:00:00',finish:fin(false,0,0)},
  {id:'B',state:'ready',when:'2026-09-26T09:00:00',finish:fin(true,0,0)},
  {id:'C',state:'ready',when:'2026-09-25T09:00:00',finish:fin(true,2,0)},
  {id:'D',state:'ready',when:'2026-08-01T09:00:00',finish:fin(true,0,9)},
  {id:'E',state:'ready',when:'2026-09-20T09:00:00',finish:fin(true,0,3)},
];
const id=r=>r?r.id:null;
console.log(JSON.stringify({
  fromA:id(nextToFinish(rows,'A',1,NOW)),
  fromC:id(nextToFinish(rows,'C',1,NOW)),
  fromE:id(nextToFinish(rows,'E',1,NOW)),
  backFromC:id(nextToFinish(rows,'C',-1,NOW)),
  backFromA:id(nextToFinish(rows,'A',-1,NOW)),
  unknown:id(nextToFinish(rows,'gone',1,NOW)),
  unknownBack:id(nextToFinish(rows,'gone',-1,NOW)),
  alone:id(nextToFinish([rows[3]],'C',1,NOW)),
  empty:id(nextToFinish([],'C',1,NOW)),
  oldTitle:toFinish(rows[1],NOW), oldFlags:toFinish(rows[4],NOW),
  finished:toFinish(rows[2],NOW), noWhen:toFinish({finish:fin(true,1,0)},NOW),
}));
""")
    # B is finished and D is backlog (flags only, processed in August)
    assert out["fromA"] == "C"
    assert out["fromC"] == "E"
    assert out["fromE"] == "A", "the walk wraps past the end"
    assert out["backFromC"] == "A" and out["backFromA"] == "E"
    assert out["unknown"] == "A" and out["unknownBack"] == "E"
    assert out["alone"] is None and out["empty"] is None
    # an open title is always to finish; other open steps only within 14 days
    assert out["oldTitle"] is True and out["oldFlags"] is False
    assert out["finished"] is False and out["noWhen"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_finish_strip_markup_in_each_state():
    out = _run_node(_finish_env() + """
const strip=f=>fsStripHTML(finishSteps({finish:f}));
const cls=h=>[...h.matchAll(/class="fstep (\\w+)"/g)].map(m=>m[1]);
console.log(JSON.stringify({
  title:strip(fin(false,2,6)), voices:strip(fin(true,2,6)),
  flags:strip(fin(true,0,6)), done:strip(fin(true,0,0)),
  cls:[fin(false,2,6),fin(true,2,6),fin(true,0,6),fin(true,0,0)].map(f=>cls(strip(f))),
  doneNext:fsDoneHTML({id:'LT Weekly 09262026',title:'LT Weekly'}),
  doneLast:fsDoneHTML(null),
}));
""")
    assert out["cls"] == [["cur", "open", "open", "open"],
                          ["done", "cur", "open", "open"],
                          ["done", "done", "cur", "open"],
                          ["done", "done", "done", "done"]]
    t = out["title"]
    # the numbered steps in order, the counts in mono, the current step marked
    assert t.index(">Title") < t.index(">Voices") < t.index(">Flags") < t.index(">Done")
    assert '<span class="c mono">2</span>' in t and '<span class="c mono">6</span>' in t
    assert 'data-step="title" aria-current="step"' in t
    assert t.count('class="fsep"') == 3
    # a done step shows a check instead of its number, and drops its count
    v = out["voices"]
    assert '<span class="no">&#10003;</span>Title' in v
    assert '<span class="no">2</span>Voices <span class="c mono">2</span>' in v
    f = out["flags"]
    assert 'Voices</span>' in f and '<span class="c mono">2</span>' not in f
    # Finished: the green line, with the hand-off when there is somewhere to go
    assert 'class="doneline"' in out["doneNext"]
    assert "Finished: title, voices, and flags are done." in out["doneNext"]
    assert 'onclick="fsGo(1)"' in out["doneNext"] and "Next to finish &#8594;" in out["doneNext"]
    assert 'title="LT Weekly (])"' in out["doneNext"]
    assert "fsGo" not in out["doneLast"] and "Nothing else to finish." in out["doneLast"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_strip_stays_away_from_a_meeting_already_finished():
    """A finished meeting looks as it does today: no strip at all. An open one
    renders the strip with the current step's body, and a flow carried across
    a re-stamp keeps its Finished line even though the row now reads done."""
    out = _run_node(_finish_env() + "\n".join([
        _js_fn("fsInit"), _js_fn("fsShow"), _js_fn("fsRender"), _js_fn("fsTitleHTML"),
        _js_fn("fsFlagsHTML"), _js_fn("fsLeftHTML"), _js_fn("fsNext"),
        "function srcStem(n){return n.replace(/\\.[^.]+$/,'');}",
        "let FS=null,FS_CARRY=null,MR=null,S={timeline:[]},route={base:null},RQUIET=null;",
        "const box={hidden:true,innerHTML:''};",
        "const document={getElementById:id=>id==='mfinish'?box:null};",
        "let focused=0;function $(q){return q==='#fstitle'?{focus(){focused++;}}:null;}",
        "function fvOpen(){} function reviewStart(){}",
        "let ROWS={};function rowById(id){return ROWS[id];}",
        """
const out={};
ROWS.Done={id:'Done',when:'2026-09-26T09:00:00',finish:fin(true,0,0)};
fsInit('Done');out.done={fs:FS,hidden:box.hidden,html:box.innerHTML};
ROWS.New={id:'New',state:'needs_name',when:new Date().toISOString().slice(0,19),
  suggested_title:'Budget Review',suggested_date:'2026-09-26',
  source_file:'Recording 0926.m4a',finish:fin(false,1,0)};
fsInit('New');out.open={cur:FS.cur,live:FS.live,hidden:box.hidden,html:box.innerHTML,focused};
// two panes: a j/k walk mounts the meeting quietly -- the strip, no focus
RQUIET='New';fsInit('New');out.quiet={cur:FS.cur,live:FS.live,hidden:box.hidden,focused,
  cleared:RQUIET};
FS_CARRY={base:'Done',past:[],live:true,keep:true};
fsInit('Done');out.carried={hidden:box.hidden,html:box.innerHTML,carry:FS_CARRY};
console.log(JSON.stringify(out));
"""]))
    assert out["done"] == {"fs": None, "hidden": True, "html": ""}
    o = out["open"]
    assert o["cur"] == "title" and o["hidden"] is False and o["live"] is True
    assert '<div class="finish"><div class="fstrip"' in o["html"]
    # the title step: the inline rename and date inputs, prefilled
    assert 'class="renameinput" id="fstitle" type="text" value="Budget Review"' in o["html"]
    assert 'class="dateinput" id="fsdate" type="date" value="2026-09-26"' in o["html"]
    assert 'onclick="fsAccept()"' in o["html"] and "Use the file name" in o["html"]
    assert o["focused"] == 1, "a meeting to finish takes the keyboard on open"
    # docs/ui-refresh phase 3: a quiet mount (a j/k walk, the default open)
    # renders the same strip but leaves the keyboard with the list
    assert out["quiet"] == {"cur": "title", "live": False, "hidden": False,
                            "focused": 1, "cleared": None}
    assert 'class="doneline"' in out["carried"]["html"]
    assert out["carried"]["carry"] is None, "a carry is consumed once"


def _voices_env():
    return _finish_env() + "\n".join([
        _js_fn("fsKey"), _js_fn("fvUse"), _js_fn("fvSkip"), _js_fn("fvNext"),
        _js_fn("fvSave"), _js_fn("saveName"), _js_fn("namedToast"), _js_fn("fvBusy"),
        _js_fn("fvToken"), _js_oneline("_fsOpenVoices"), _js_fn("fsGo"),
        _js_oneline("kbLayerOpen"), "const DRAWER={open:false};let KSHEET=false;",
        "let NP=null,_popClose=null,MR=null,NPBUSY=false,MP={base:'M1',audio:null};",
        "let route={view:'meeting',base:'M1'};",
        "const location={hash:'#m/M1'};",
        "const els={};",
        "for(const k of ['#fvname','#fverr','#fvconfirm','#fvsave'])"
        "els[k]={value:'',hidden:true,textContent:'',innerHTML:'',disabled:false,"
        "tagName:'INPUT',focus(){document.activeElement=this;}};",
        "function $(q){return els[q]||null;}",
        "const body={tagName:'BODY'};",
        "const document={activeElement:body,getElementById:()=>null};",
        "let posted=[],reply={ok:true,applied:'M1',targets:['X','Y']},toasts=[],shows=[],reloads=0,refreshes=0;",
        "function api(p,b){posted.push({p,b});return Promise.resolve(reply);}",
        "function toast(m){toasts.push(m);}",
        "function refresh(){refreshes++;}",
        "function mReloadSegs(){reloads++;}",
        "function mSyncPP(){}",
        "function fsShow(auto){shows.push([auto,FS.vi,FS.past.slice()]);FS.cur='voices';}",
        "function fsStripSync(){}",
        "function key(k){let prevented=false;"
        "const handled=fsKey({key:k,metaKey:false,ctrlKey:false,altKey:false,"
        "preventDefault(){prevented=true;}});return {handled,prevented};}",
        "let S={timeline:[]};",
        "let FS_CARRY=null;",
        "let FS={base:'M1',cur:'voices',past:[],vi:0,live:true,"
        "fin:{title:true,voices:2,flags:0},"
        "looks:[{name:'Grace Lindqvist',score:.84},{name:'Elena Ruiz',score:.71}],"
        "voices:[{id:'SPEAKER_01',display:'Speaker 58',uid:'U058',st:''},"
        "{id:'SPEAKER_02',display:'Speaker 61',uid:null,st:''}]};",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_voices_step_keys_stand_back_while_the_drawer_is_open():
    """The drawer opens over the meeting page from the gear: while it is open
    the Voices step's keys (and ] [) belong to it, so s never skips a voice,
    Enter never saves one, and ] never leaves the page under it."""
    out = _run_node(_voices_env() + """
DRAWER.open=true;document.activeElement=body;
const o={};
for(const k of [']','[','s','Enter',' ','1'])o[k]=key(k);
o.state={vi:FS.vi,past:FS.past,posted:posted.length,shows:shows.length,
  hash:location.hash};
DRAWER.open=false;
o.after=key('s');
console.log(JSON.stringify(o));
""")
    for k in ("]", "[", "s", "Enter", " ", "1"):
        assert out[k] == {"handled": False, "prevented": False}, k
    assert out["state"] == {"vi": 0, "past": [], "posted": 0, "shows": 0, "hash": "#m/M1"}
    assert out["after"] == {"handled": True, "prevented": True}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_voices_step_keys_pick_save_and_skip():
    out = _run_node(_voices_env() + """
(async()=>{
  const o={};
  o.k2=key('2');o.box2=els['#fvname'].value;
  o.k3=key('3');                      // only two chips: not handled
  // typing in the name box: no key fires, not even s
  document.activeElement=els['#fvname'];
  o.typingS=key('s');o.typingEnter=key('Enter');
  document.activeElement=body;
  o.k1=key('1');o.box1=els['#fvname'].value;
  document.activeElement=body;
  o.enter=key('Enter');
  await new Promise(r=>setTimeout(r,0));
  o.posted=posted.slice();o.toasts=toasts.slice();
  o.afterSave={vi:FS.vi,st:FS.voices[0].st,shows:shows.slice(),reloads,refreshes,
    label:els['#fvsave'].textContent,busy:NPBUSY};
  document.activeElement=body;
  o.s=key('s');
  o.afterSkip={vi:FS.vi,st:FS.voices[1].st,past:FS.past,voices:FS.fin.voices,
    shows:shows.slice(-1)};
  // the floor refusal offers the meeting-only save back through fvSave
  FS.vi=1;FS.voices[1].st='';FS.past=[];
  els['#fvname'].value='Tom Keller';reply={ok:false,floor:true,error:'Too little speech.'};
  document.activeElement=body;
  key('Enter');await new Promise(r=>setTimeout(r,0));
  o.floor={post:posted[posted.length-1],warn:els['#fvconfirm'].innerHTML,
    shown:!els['#fvconfirm'].hidden,vi:FS.vi};
  console.log(JSON.stringify(o));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
""")
    assert "threw" not in out, out
    assert out["k2"] == {"handled": True, "prevented": True}
    assert out["box2"] == "Elena Ruiz"
    assert out["k3"] == {"handled": False, "prevented": False}
    assert out["typingS"]["handled"] is False and out["typingEnter"]["handled"] is False
    assert out["box1"] == "Grace Lindqvist"
    assert out["enter"] == {"handled": True, "prevented": True}
    # the save goes through the shared saveName, by uid for a registry voice
    assert out["posted"] == [{"p": "/api/name",
                              "b": {"uid": "U058", "name": "Grace Lindqvist",
                                    "confirm": False, "meeting": "M1"}}], \
        "a uid save carries the open page, so the server relabels it first"
    assert out["toasts"] == ["Named here. Updating 2 other meetings."]
    s = out["afterSave"]
    assert s["st"] == "named" and s["vi"] == 1, "the strip advances to the next voice"
    assert s["shows"] == [[True, 1, []]]
    assert s["reloads"] == 1 and s["refreshes"] == 1
    assert s["label"] == "Save and next" and s["busy"] is False
    # s skips the last voice: Voices stays open, the strip moves past it
    assert out["s"] == {"handled": True, "prevented": True}
    k = out["afterSkip"]
    assert k["st"] == "skip" and k["vi"] == 2 and k["past"] == ["voices"]
    assert k["voices"] == 1 and k["shows"] == [[True, 2, ["voices"]]]
    # a cluster voice posts by meeting and speaker; the floor strip calls fvSave
    f = out["floor"]
    assert f["post"]["b"] == {"meeting": "M1", "speaker": "SPEAKER_02",
                              "name": "Tom Keller", "confirm": False, "local": False}
    assert f["shown"] is True and 'onclick="fvSave(false,true)"' in f["warn"]
    assert "$('#fvconfirm').hidden=true" in f["warn"]
    assert f["vi"] == 1, "a refused save holds the voice on screen"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_hand_off_keys_open_the_next_meeting_to_finish():
    out = _run_node(_voices_env() + """
const now=new Date(Date.now()-3600e3).toISOString().slice(0,19);
S.timeline=[
  {id:'M0',when:now,finish:fin(true,1,0)},
  {id:'M1',when:now,finish:fin(true,2,0)},
  {id:'M2',when:now,finish:fin(true,0,0)},
  {id:'M3',when:now,finish:fin(true,0,4)},
];
const o={};
o.next=key(']');o.hashNext=location.hash;o.carry=FS_CARRY;
location.hash='#m/M1';
o.prev=key('[');o.hashPrev=location.hash;
location.hash='#m/M1';
document.activeElement=els['#fvname'];
o.typing=key(']');o.hashTyping=location.hash;
document.activeElement=body;NP={uid:'U1'};
o.panel=key(']');NP=null;
_popClose=()=>{};o.popover=key(']');_popClose=null;
// a finished meeting page (no strip) still hands off
FS=null;o.noStrip=key(']');o.hashNoStrip=location.hash;
// nothing left to finish: the key is inert
S.timeline=[{id:'M1',when:now,finish:fin(true,0,0)}];location.hash='#m/M1';
o.nothing=key(']');o.hashNothing=location.hash;
console.log(JSON.stringify(o));
""")
    assert out["next"] == {"handled": True, "prevented": True}
    assert out["hashNext"] == "#m/M3", "M2 is finished, so ] skips it"
    assert out["carry"] == {"base": "M3", "past": [], "live": True, "keep": False}
    assert out["hashPrev"] == "#m/M0"
    assert out["typing"]["handled"] is False and out["hashTyping"] == "#m/M1"
    assert out["panel"]["handled"] is False and out["popover"]["handled"] is False
    assert out["hashNoStrip"] == "#m/M3"
    assert out["nothing"] == {"handled": True, "prevented": False}
    assert out["hashNothing"] == "#m/M1"


def test_the_finish_strip_sits_under_the_audio_and_above_the_summary():
    build = _js_fn("buildMeeting")
    i_audio = build.index("_mAudioBar()")
    i_fin = build.index('<div id="mfinish" class="mfinish" hidden></div>')
    i_sum = build.index("_mSummary(m)")
    assert i_audio < i_fin < i_sum
    assert "fsInit(base);" in build and "fsSegsLoaded();" in build
    # the page keys hand the flow its own keys first; the voice save shares
    # the panel's save path; teardown drops the flow with the page
    assert "if(fsKey(e))return;" in _js_fn("mKey")
    assert "await saveName({" in _js_fn("fvSave") and "again:'fvSave'" in _js_fn("fvSave")
    assert "FS=null;" in _js_fn("teardownMeeting")
    assert "fsFlagsSync();" in _js_fn("mSyncFlagStrip")
    assert "fsSegsLoaded();" in _js_fn("mReloadSegs")
    # the flags step is the existing verdict bar; the clip is span playback
    assert "reviewStart(0)" in _js_fn("fsShow")
    assert "mPlaySpan(FS.clip.start,FS.clip.end)" in _js_fn("fvPlay")
    assert "'/api/name_preview'" in _js_fn("fvPreview")
    assert "'/api/accept_meeting'" in _js_fn("fsAccept")
    # the strip's classes live in the finish flow section, on existing tokens
    sec = NEW_CSS[NEW_CSS.index("Finish flow (docs/ui-refresh"):]
    for cls in (".finish{", ".fstrip{", ".fstep{", ".fbody{", ".vhead{", ".vq{",
                ".vgrid{", ".namebox{", ".preview{", ".looks{", ".lchip{",
                ".vbtns{", ".clip{", ".said{", ".doneline{"):
        assert cls in sec, f"missing {cls}"
    assert not re.findall(r"#[0-9A-Fa-f]{3,6}\b", sec), "no new colors"


# ---------------------------------------------------------------------------
# docs/ui-refresh phase 2: the To finish section and status. The by-date list
# opens with "Now" and "To finish" above the accordion; the tray band folds
# into them; the pill gains its naming segment; the tab title counts. These
# drive the shipped functions under node, house-style.
# ---------------------------------------------------------------------------
def _sections_env():
    return "\n".join([
        _finish_env(),
        re.search(r"(?m)^const PIN_ORDER=.*;$", NEW_JS).group(0),
        re.search(r"(?m)^const NOW_STATES=.*;$", NEW_JS).group(0),
        "function dateKey(r){return r.date||((r.when||'').slice(0,10));}",
        "function shortDate(d){return 'D'+d;}",
        _js_fn("timelineSections"), _js_fn("finishDotsHTML"), _js_fn("peopleMetaBits"),
        _js_fn("finishRowHTML"),
        _js_fn("secHeadHTML"), _js_fn("finishActsHTML"), _js_fn("headSectionsHTML"),
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_section_builder_sorts_rows_into_now_to_finish_and_the_library():
    out = _run_node(_sections_env() + """
const recent=new Date(NOW-2*864e5).toISOString().slice(0,19);
const rows=[
  {id:'W',state:'waiting',when:'2026-09-27T08:00:00'},
  {id:'F',state:'failed',when:'2026-04-01T08:00:00'},
  {id:'P',state:'processing',when:'2026-09-27T09:00:00'},
  {id:'R',state:'recording',when:'2026-09-27T10:00:00'},
  {id:'H',state:'held',when:'2026-09-20T08:00:00'},
  {id:'N',state:'needs_name',when:'2026-01-02T08:00:00',date:'2026-01-02',finish:fin(false,0,0)},
  {id:'V',state:'ready',when:recent,date:'2026-09-25',finish:fin(true,2,0)},
  {id:'D',state:'ready',when:recent,date:'2026-09-24',finish:fin(true,0,0)},
  {id:'O',state:'ready',when:'2026-08-01T08:00:00',date:'2026-08-01',finish:fin(true,1,4)},
  {id:'X',state:'ready',when:'2026-09-01T08:00:00',date:'2026-09-01'},
];
const s=timelineSections(rows,NOW),ids=a=>a.map(r=>r.id);
console.log(JSON.stringify({now:ids(s.now),fin:ids(s.fin),rest:ids(s.rest),
  empty:timelineSections([],NOW)}));
""")
    # Now: the pipeline's live rows in the fixed order; a failed April file
    # sits at the top, never buried in April
    assert out["now"] == ["R", "P", "F", "H", "W"]
    # To finish: a title waiting (however old), and a recent open step, in
    # the timeline's own order (the order ] walks)
    assert out["fin"] == ["N", "V"]
    # the library: the finished meeting, the older backlog (flags and a voice
    # left, processed in August: a filter, never a to-do), and a row with no
    # finish record at all, newest meeting date first
    assert out["rest"] == ["D", "X", "O"]
    assert out["empty"] == {"now": [], "fin": [], "rest": []}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_to_finish_row_is_a_compact_link_with_its_step_dots():
    out = _run_node(_sections_env() + """
console.log(JSON.stringify({
  named:finishRowHTML({id:'Budget Review 09262026',state:'ready',title:'Budget Review',
    date:'2026-09-26',minutes:47,speakers:['Grace Lindqvist','Speaker 58'],
    named:['Grace Lindqvist'],finish:fin(true,2,6)}),
  waiting:finishRowHTML({id:'Recording 0926',state:'needs_name',title:'Recording 0926',
    suggested_title:'Literacy pilot',finish:fin(false,1,0)}),
  bare:finishRowHTML({id:'Recording 0927',state:'needs_name',title:'Recording 0927',
    finish:fin(false,0,0)}),
  dots:[fin(false,2,6),fin(true,0,3),fin(true,0,0)].map(f=>finishDotsHTML({finish:f})),
}));
""")
    n = out["named"]
    # a link to the meeting page, where the finish strip takes over
    assert n.startswith('<a class="lrow" href="#m/Budget%20Review%2009262026"')
    assert 'data-id="Budget Review 09262026"' in n and 'data-state="ready"' in n
    assert '<span class="ltitle">Budget Review</span><span class="ldate">D2026-09-26</span>' in n
    assert ('<span class="lmeta">47 min &middot; Grace Lindqvist &middot; '
            '<span class="unn">2 unnamed</span></span>') in n
    # a meeting waiting for its title shows the suggested one; no date, and
    # with no duration or named person the meta line is its unnamed count
    w = out["waiting"]
    assert '<span class="ltitle">Literacy pilot</span></span>' in w
    assert "ldate" not in w
    assert '<span class="lmeta"><span class="unn">1 unnamed</span></span>' in w
    # nothing to say: no meta line at all
    assert "lmeta" not in out["bare"]
    # the dots: done green with a check, open teal with a mono count; Title
    # carries no count, and the strip's Done step never becomes a dot
    assert out["dots"][0] == ('<span class="steps"><span class="sdot">Title</span>'
                              '<span class="sdot">Voices <span class="mono">2</span></span>'
                              '<span class="sdot">Flags <span class="mono">6</span></span></span>')
    assert out["dots"][1] == ('<span class="steps"><span class="sdot done">&#10003; Title</span>'
                              '<span class="sdot done">&#10003; Voices</span>'
                              '<span class="sdot">Flags <span class="mono">3</span></span></span>')
    assert out["dots"][2].count('class="sdot done"') == 3
    # the dot colours are the token pairs the design names
    assert re.search(r"(?m)^\.sdot\{[^}]*color:var\(--attn\);background:var\(--attn-soft\)", NEW_CSS)
    assert ".sdot.done{color:var(--ok);background:var(--ok-soft)}" in NEW_CSS


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_to_finish_eyebrow_carries_its_actions_only_when_they_apply():
    out = _run_node(_sections_env() + """
const row={id:'V',state:'ready',title:'V',finish:fin(true,1,0)};
console.log(JSON.stringify({
  none:finishActsHTML(0,0,false),
  both:finishActsHTML(31,12,false),
  one:finishActsHTML(1,0,false),
  on:finishActsHTML(0,0,true),
  nothing:headSectionsHTML('',[],''),
  nowOnly:headSectionsHTML('<div>R</div>',[],''),
  finOnly:headSectionsHTML('',[row],''),
  actsOnly:headSectionsHTML('',[],finishActsHTML(3,0,false)),
}));
""")
    assert out["none"] == ""
    b = out["both"]
    assert 'onclick="fsVoicesStart()"' in b and ">Name 31 voices</button>" in b
    assert 'onclick="flaggedToggle()"' in b and 'aria-pressed="false"' in b
    assert ">Review flags</button>" in b
    assert ">Name 1 voice</button>" in out["one"] and "flaggedToggle" not in out["one"]
    # the filter on keeps its toggle, pressed, even with nothing left to count
    assert 'aria-pressed="true"' in out["on"] and "Review flags &#10005;" in out["on"]
    # a section with nothing in it renders nothing
    assert out["nothing"] == ""
    assert out["nowOnly"] == ('<div class="mgroup sechead" id="sec-now"><span class="glabel">Now'
                              '</span><span class="grule" aria-hidden="true"></span></div><div>R</div>')
    assert 'id="sec-fin"' in out["finOnly"] and 'class="lrow"' in out["finOnly"]
    assert "sec-now" not in out["finOnly"]
    # the eyebrow stays for its actions when no row is waiting: "Name N
    # voices" and "Review flags" must never become unreachable
    assert 'id="sec-fin"' in out["actsOnly"] and "Name 3 voices" in out["actsOnly"]
    assert "lrow" not in out["actsOnly"]


def _timeline_env(sort="date", search="", flagged="false"):
    """drawTimeline over stubbed DOM nodes, with the real section, tray, and
    accordion helpers. rowHTML is stubbed to a marker per state."""
    return "\n".join([
        _sections_env(),
        "const els={'#timeline':{dataset:{},innerHTML:'',contains:()=>false},"
        "'#rail':{hidden:true,innerHTML:''},"
        "'#search':{value:" + json.dumps(search) + "}};",
        "function $(q){return els[q]||null;}",
        "const document={activeElement:null};",
        "const localStorage={getItem:()=>null,setItem:()=>{}};",
        "let LF={cat:'',sort:" + json.dumps(sort) + ",flags:" + flagged + ",voices:false};",
        "let HITS=null,SAID_TOP=null;",
        _js_fn("chipKeeps"), _js_fn("chipCounts"), _js_fn("saidGroups"), _js_fn("hitClock"),
        _js_fn("saidInHTML"), _js_fn("libraryEmpty"),
        _js_fn("setupCheck"), _js_fn("setupHTML"),
        "let S=null,LAST_GROUPS=[],LAST_NEST=false;",
        "const OPEN=new Set();function meetingByBase(){return null;}",
        "function _isoLocal(){return '2026-09-27';}",
        "const BUCKET={today:'Today',yesterday:'Yesterday',undated:'Undated'};",
        "function dateBucket(iso){return iso?iso.slice(0,7):'Undated';}",
        "function nameBucket(t){return (t||'#')[0].toUpperCase();}",
        "function escJs(s){return esc(s);}",
        _js_region("/* ==================== the year-to-month accordion",
                   "/* ============================ status pill "),
        _js_fn("ghead"), _js_fn("accordionHTML"), _js_fn("sigOf"),
        "function rowHTML(r){return r.state==='needs_name'?finishRowHTML(r)"
        ":'<div class=\"row\" data-id=\"'+r.id+'\"></div>';}",
        "function drawRail(){}",
        "const UPLOADS=[];function uploadsPrune(){}function uploadsSig(){return '[]';}"
        "function uploadRowHTML(){return '';}",
        re.search(r"(?m)^const NOW_KINDS=.*;$", NEW_JS).group(0),
        "let DUPE_ARMED=false,DUPE_ERR='',NOW_MSG={};",
        _js_fn("_trayVerb"), _js_fn("_trayRow"), _js_oneline("dupeDisarm"),
        _js_oneline("nowMsg"), _js_oneline("trayCount"), _js_fn("voiceQueueCount"),
        _js_fn("traySig"),
        _js_fn("nowTrayHTML"),
        _js_region("function fmtEta(", "const STAGE_NICE"),
        re.search(r"(?m)^const QUEUE_SHOW=.*;$", NEW_JS).group(0), _js_fn("nowRowsHTML"),
        _js_fn("drawTimeline"),
        "const html=()=>els['#timeline'].innerHTML;",
        "const order=h=>(h.match(/id=\"sec-\\w+\"|data-id=\"[^\"]*\"|class=\"ghead [^\"]*\"|"
        "class=\"trayrow\"/g)||[]).map(x=>x.replace(/^(id|data-id|class)=/,''));",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_list_opens_with_now_and_to_finish_above_the_accordion():
    out = _run_node(_timeline_env() + """
const recent=new Date(Date.now()-864e5).toISOString().slice(0,19);
const st={timeline:[
  {id:'P',state:'processing',when:recent,pct:40},
  {id:'N',state:'needs_name',when:recent,date:'2026-09-26',finish:fin(false,1,0)},
  {id:'V',state:'ready',when:recent,date:'2026-09-25',finish:fin(true,2,0)},
  {id:'D',state:'ready',when:recent,date:'2026-09-24',finish:fin(true,0,0)},
],tray:[
  {kind:'recorder_stall',title:'Recording',detail:'no audio',target:'rec:x',count:1},
  {kind:'unknown_voice',title:'Speaker 58',detail:'',target:'U58',count:2},
  {kind:'review',title:'V',detail:'',target:'V',count:3},
]};
drawTimeline(st);
const full=order(html());
const noTray={timeline:[st.timeline[3]],tray:[]};
els['#timeline'].dataset.sig='';drawTimeline(noTray);
const quiet=order(html());
els['#timeline'].dataset.sig='';
drawTimeline({timeline:[st.timeline[3]],tray:[{kind:'dupe_meetings',title:'Duplicate transcripts',
  detail:'1 pair',target:'dupes',count:1}]});
const dupes=order(html());
console.log(JSON.stringify({full,quiet,dupes,h:html()}));
""")
    # Now (the stall row, then the processing row), To finish (its two
    # rows), then the library; the finished meeting lives only there
    assert out["full"] == ['"sec-now"', '"trayrow"', '"P"', '"sec-fin"', '"N"', '"V"',
                           '"ghead mgroup"', '"D"']
    # a section with nothing in it renders nothing
    assert out["quiet"] == ['"ghead mgroup"', '"D"']
    assert out["dupes"] == ['"sec-now"', '"trayrow"', '"ghead mgroup"', '"D"']
    # the chronic kinds are the eyebrow's actions, never rows of their own
    h = out["h"]
    assert "Speaker 58" not in h


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_now_and_the_to_finish_actions_survive_a_search_and_the_name_sort():
    """The tray band showed its items whatever the list was doing. Its items
    moved into the list, so a search with no match, an empty library, and the
    by-name sort must all keep them reachable."""
    stall = ("{kind:'recorder_stall',title:'Recording',detail:'no audio',"
             "target:'rec:x',count:1}")
    voice = "{kind:'unknown_voice',title:'Speaker 58',detail:'',target:'U58',count:2}"
    out = _run_node(_timeline_env(search="zzz") + """
drawTimeline({timeline:[{id:'D',state:'ready',when:'2026-09-01T00:00:00',
  title:'Budget',finish:fin(true,0,0)}],tray:[""" + stall + "," + voice + """]});
const nomatch=html();
els['#search'].value='';els['#timeline'].dataset.sig='';
drawTimeline({timeline:[],tray:[""" + stall + "," + voice + """]});
const empty=html();
LF.sort='name';els['#timeline'].dataset.sig='';
const recent=new Date(Date.now()-864e5).toISOString().slice(0,19);
drawTimeline({timeline:[{id:'B',state:'ready',title:'Budget',when:recent,finish:fin(true,1,0)},
  {id:'A',state:'needs_name',title:'Alpha',when:recent,finish:fin(false,0,0)}],
  tray:[""" + stall + "," + voice + """]});
const byName=order(html());
console.log(JSON.stringify({nomatch,empty,byName}));
""")
    for key in ("nomatch", "empty"):
        h = out[key]
        assert 'id="sec-now"' in h and 'class="trayrow"' in h, key
        assert "Name 1 voice" in h, key
    assert "No matches." in out["nomatch"]
    assert "Record from the menu bar, or drop an audio file here." in out["empty"]
    assert 'class="setup"' in out["empty"]
    # by name: Now's urgent row and the To finish actions head the letter
    # groups; the rows themselves stay alphabetical, and a meeting waiting for
    # its title is the compact row there too
    assert out["byName"] == ['"sec-now"', '"trayrow"', '"sec-fin"', '"A"', '"B"']


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_list_signature_follows_the_dots_and_the_now_rows():
    """A To finish row's dots move while nothing else on the row does (a voice
    named), and an armed sweep or a Fix outcome changes only the Now rows:
    each must still rebuild the list, and an unchanged poll must not."""
    out = _run_node(_timeline_env() + """
const recent=new Date(Date.now()-864e5).toISOString().slice(0,19);
const row={id:'V',state:'ready',when:recent,date:'2026-09-25',finish:fin(true,2,0)};
const dup={kind:'dupe_files',title:'Already processed',detail:'2 files',target:'src:a',exact:2,count:2};
drawTimeline({timeline:[row],tray:[dup]});
const s0=els['#timeline'].dataset.sig;
drawTimeline({timeline:[row],tray:[dup]});
const same=els['#timeline'].dataset.sig===s0;
drawTimeline({timeline:[{...row,finish:fin(true,1,0)}],tray:[dup]});
const s1=els['#timeline'].dataset.sig;
DUPE_ARMED=true;
drawTimeline({timeline:[{...row,finish:fin(true,1,0)}],tray:[dup]});
const s2=els['#timeline'].dataset.sig;
console.log(JSON.stringify({same,dots:s1!==s0,armed:s2!==s1,
  armedHtml:html().includes('dupeDeleteGo(this)')}));
""")
    assert out == {"same": True, "dots": True, "armed": True, "armedHtml": True}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_voice_queue_starts_with_the_most_talk_time_and_never_revisits():
    out = _run_node(_finish_env() + "\n".join([
        _js_fn("nextVoiceMeeting"), _js_fn("fsVoiceOrder"), """
const f=(v,secs)=>({title:true,voices:v,flags:0,done:!v,voice_secs:secs});
const tl=[
  {id:'rec:x',state:'recording'},
  {id:'A',state:'ready',finish:f(1,12)},
  {id:'B',state:'needs_name',finish:{...f(2,40),title:false}},
  {id:'C',state:'ready',finish:f(0,0)},
  {id:'D',state:'ready',finish:f(3,40)},
  {id:'E',state:'ready',finish:f(1,5)},
];
const id=r=>r?r.id:null;
const voices=[{id:'S1',uid:'U1',st:''},{id:'S2',uid:null,st:''},{id:'S3',uid:'U3',st:''}];
const segs=[{speaker:'S1',start:0,end:3},{speaker:'S2',start:3,end:13},
  {speaker:'S3',start:13,end:30},{speaker:'S1',start:30,end:31}];
console.log(JSON.stringify({
  first:id(nextVoiceMeeting(tl,[])),
  next:id(nextVoiceMeeting(tl,['B'])),
  then:id(nextVoiceMeeting(tl,['B','D'])),
  last:id(nextVoiceMeeting(tl,['B','D','A'])),
  none:id(nextVoiceMeeting(tl,['B','D','A','E'])),
  empty:id(nextVoiceMeeting([],[])),
  order:fsVoiceOrder(voices,segs,[]).map(v=>v.id),
  skipResolved:fsVoiceOrder(voices,segs,['U3']).map(v=>v.id),
}));
"""]))
    # B and D tie at 40s: the timeline's order keeps B first
    assert out["first"] == "B" and out["next"] == "D"
    assert out["then"] == "A" and out["last"] == "E"
    # C has nothing left, the recording row has no finish record
    assert out["none"] is None and out["empty"] is None
    # inside one meeting: the most talk time first; a voice this run already
    # named (its relabel still in flight) is never asked about again
    assert out["order"] == ["S3", "S2", "S1"]
    assert out["skipResolved"] == ["S2", "S1"]


def _voice_queue_env():
    return _finish_env() + "\n".join([
        _js_fn("fsInit"), _js_fn("fsShow"), _js_fn("fsRender"), _js_fn("fsTitleHTML"),
        _js_fn("fsFlagsHTML"), _js_fn("fsLeftHTML"), _js_fn("fsNext"),
        _js_fn("nextVoiceMeeting"), _js_fn("fsVoicesStart"), _js_fn("fsVoiceCarry"),
        _js_fn("fsVoiceGo"),
        "function srcStem(n){return (n||'').replace(/\\.[^.]+$/,'');}",
        "let FS=null,FS_CARRY=null,MR=null,S={timeline:[],tray:[]},route={base:null},"
        "RQUIET=null;",
        "const box={hidden:true,innerHTML:''};",
        "const document={getElementById:id=>id==='mfinish'?box:null};",
        "function $(q){return null;}",
        "const location={hash:''};",
        "let reviews=0,panels=[],toasts=[],opens=0;",
        "function fvOpen(){opens++;} function reviewStart(){reviews++;}",
        "function toast(m){toasts.push(m);}",
        "function openNamePanelByUid(u){panels.push(u);}",
        "function rowById(id){return S.timeline.find(r=>r.id===id);}",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_name_n_voices_walks_the_voices_step_across_meetings():
    """"Name N voices" opens the meeting holding the unnamed voice with the
    most talk time, starts the flow in its Voices step (never the Title or
    Flags steps), and after that meeting's last voice hands off to the next
    meeting with an unnamed voice. The end of the queue drops back to the
    ordinary strip and starts nothing on its own."""
    out = _run_node(_voice_queue_env() + """
const f=(t,v,fl,secs)=>({title:t,voices:v,flags:fl,done:t&&!v&&!fl,voice_secs:secs});
const recent=new Date(Date.now()-864e5).toISOString().slice(0,19);
S.timeline=[
  {id:'A',state:'ready',when:recent,finish:f(true,1,2,12)},
  {id:'B',state:'needs_name',when:recent,suggested_title:'Budget',finish:f(false,2,3,40)},
  {id:'C',state:'ready',when:recent,finish:f(true,0,0,0)},
];
S.tray=[{kind:'unknown_voice',target:'U58'}];
const o={};
fsVoicesStart();
o.start={hash:location.hash,carry:FS_CARRY};
fsInit('B');
o.b={cur:FS.cur,mode:FS.mode,seen:FS.seen,live:FS.live,opens,
  titleCur:box.innerHTML.includes('data-step="title" aria-current'),
  voicesCur:box.innerHTML.includes('data-step="voices" aria-current')};
// the last voice here is named: the hand-off, never the Title or Flags steps
FS.fin.voices=0;FS.resolved.push('U1');
fsShow(true);
o.handoff={hash:location.hash,carry:FS_CARRY,reviews};
fsInit('A');
o.a={cur:FS.cur,seen:FS.seen,resolved:FS.resolved};
// A's last voice: nothing left to hand off to (B was visited, C is done)
FS.fin.voices=0;
fsShow(true);
o.end={mode:FS.mode,cur:FS.cur,reviews,toasts,hash:location.hash,
  flagsBody:box.innerHTML.includes('id="fsflags"')};
// no live meeting holds an unnamed voice: the naming panel, as the tray did
S.timeline=[S.timeline[2]];location.hash='';FS_CARRY=null;
fsVoicesStart();
o.fallback={panels,hash:location.hash,carry:FS_CARRY};
console.log(JSON.stringify(o));
""")
    assert out["start"]["hash"] == "#m/B"
    assert out["start"]["carry"] == {"base": "B", "past": ["title", "flags"], "live": True,
                                     "keep": True, "mode": "voices", "seen": ["B"],
                                     "resolved": [], "passed": []}
    b = out["b"]
    # the title is open on B, but the queue starts in Voices
    assert b["cur"] == "voices" and b["mode"] == "voices" and b["seen"] == ["B"]
    assert b["voicesCur"] is True and b["titleCur"] is False
    assert b["live"] is True and b["opens"] == 1
    h = out["handoff"]
    assert h["hash"] == "#m/A" and h["reviews"] == 0, "no flag review on the way out"
    assert h["carry"]["seen"] == ["B", "A"] and h["carry"]["resolved"] == ["U1"]
    assert h["carry"]["mode"] == "voices"
    assert out["a"] == {"cur": "voices", "seen": ["B", "A"], "resolved": ["U1"]}
    e = out["end"]
    assert e["mode"] is None and e["hash"] == "#m/A"
    assert e["cur"] == "flags" and e["flagsBody"] is True
    assert e["reviews"] == 0, "the end of the queue starts nothing on its own"
    assert e["toasts"] == ["No more voices to name."]
    assert out["fallback"] == {"panels": ["U58"], "hash": "", "carry": None}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_named_voice_is_remembered_by_the_queue():
    out = _run_node(_voices_env() + """
(async()=>{
  FS.mode='voices';FS.resolved=[];
  els['#fvname'].value='Grace Lindqvist';document.activeElement=body;
  key('Enter');await new Promise(r=>setTimeout(r,0));
  console.log(JSON.stringify({resolved:FS.resolved,st:FS.voices[0].st}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
""")
    assert out == {"resolved": ["U058"], "st": "named"}
    assert "if(v.uid)(FS.resolved||(FS.resolved=[])).push(v.uid);" in _js_fn("fvForget")
    assert "fsVoiceOrder(fresh,MP.segs,FS.resolved,FS.passed)" in _js_fn("fsSegsLoaded")


# ---------------------------------------------------------------------------
# "Name N voices" promises scope (phase 2 QA, 2026-09-27). N used to be the
# tray's unknown_voice count, one per visible registry voice, while the queue
# walked every meeting with an unnamed voice: transcript-local voices and
# hidden registry voices too. The seeded demo read "Name 1 voice" and asked
# about 4. N is now voiceQueueCount over the finish records, and the queue
# asks about each voice once, a skipped one included.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_voice_queue_count_counts_each_voice_once():
    out = _run_node(_js_fn("voiceQueueCount") + """
const f=(v,uids)=>({title:true,voices:v,flags:0,done:!v,voice_uids:uids});
console.log(JSON.stringify({
  // U7 heard in A and B is one voice; A's second and C's two are local
  mixed:voiceQueueCount([
    {id:'A',finish:f(2,['U7'])},{id:'B',finish:f(2,['U7','U12'])},
    {id:'C',finish:f(2,[])},{id:'D',finish:f(1,['U12'])},
    {id:'E',finish:f(0,[])},{id:'rec:x',state:'recording'}]),
  // a record from before voice_uids: every voice counts as local
  old:voiceQueueCount([{id:'A',finish:{title:true,voices:3,flags:0}}]),
  none:voiceQueueCount([]),
  missing:voiceQueueCount(undefined),
}));
""")
    assert out == {"mixed": 5, "old": 3, "none": 0, "missing": 0}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_voice_queue_passes_by_voices_it_already_asked_about():
    out = _run_node(_js_fn("nextVoiceMeeting") + _js_fn("fsVoiceOrder") + """
const f=(v,uids,secs)=>({title:true,voices:v,flags:0,done:!v,voice_secs:secs,voice_uids:uids});
const tl=[{id:'B',finish:f(1,['U7'],40)},{id:'D',finish:f(2,['U7','U12'],30)},
  {id:'E',finish:f(1,[],5)}];
const id=r=>r?r.id:null;
const voices=[{id:'S1',uid:'U7',st:''},{id:'S2',uid:null,st:''},{id:'S3',uid:'U12',st:''}];
const segs=[{speaker:'S1',start:0,end:30},{speaker:'S2',start:30,end:40},
  {speaker:'S3',start:40,end:41}];
console.log(JSON.stringify({
  first:id(nextVoiceMeeting(tl,[],[])),
  // B's one voice is U7, already asked about: B is passed by, not opened
  asked:id(nextVoiceMeeting(tl,[],['U7'])),
  // D still holds U12
  one:id(nextVoiceMeeting(tl,['E'],['U7'])),
  both:id(nextVoiceMeeting(tl,['E'],['U7','U12'])),
  // skipped earlier in the run: still open here, but already skipped
  passed:fsVoiceOrder(voices,segs,[],['U7']).map(v=>v.id+':'+v.st),
  named:fsVoiceOrder(voices,segs,['U7'],['U12']).map(v=>v.id+':'+v.st),
  untouched:voices.map(v=>v.st),
}));
""")
    assert out["first"] == "B" and out["asked"] == "D"
    assert out["one"] == "D" and out["both"] is None
    assert out["passed"] == ["S1:skip", "S2:", "S3:"]
    assert out["named"] == ["S2:", "S3:skip"]
    assert out["untouched"] == ["", "", ""], "the page's own voice list is never mutated"


def _voice_walk_env():
    """The voice queue end to end, over a five-meeting library the way the
    page drives it: fsVoicesStart, then per meeting fsInit and the transcript
    load (fsSegsLoaded), the user answering each voice the step shows, and
    the hand-off. walk(policy) returns every voice asked about and the
    meetings opened. The relabel after a save lands only when `land` is set."""
    return _voice_queue_env() + "\n".join([
        _js_fn("voiceQueueCount"), _js_fn("finishActsHTML"), _js_fn("fsVoiceOrder"),
        _js_fn("fsSegsLoaded"), _js_fn("fsVoicesFrom"), _js_fn("mUnknownUid"),
        _js_oneline("_fsOpenVoices"), _js_fn("_fsSubstantial"), _js_fn("fsSync"),
        _js_fn("fsStripSync"), _js_fn("fvSkip"), _js_fn("fvNext"), _js_fn("fsVoiceHTML"),
        "function mIsMinor(){return false;}function mSyncPP(){}",
        "let MP=null,NPBUSY=false;",
        """
const unk=[{uid:'U007',display:'Speaker 7',meetings:['A','B']},
  {uid:'U012',display:'Speaker 12',archived:true,meetings:['B','D']}];
function lib(){
  // who each meeting heard, unnamed, and for how long
  return {
    A:[['S7','Speaker 7',50],['S1','Speaker 1',20]],
    B:[['S7','Speaker 7',10],['S12','Speaker 12',40]],
    C:[['S1','Speaker 1',30],['S2','Speaker 2',25]],
    D:[['S12','Speaker 12',20]],
    E:[]};
}
function rowOf(base,heard){
  const uids=[...new Set(heard.map(h=>(unk.find(u=>u.display===h[1]
    &&u.meetings.includes(base))||{}).uid).filter(Boolean))].sort();
  return {id:base,state:'ready',when:'2026-01-01T00:00:00',finish:{title:true,
    voices:heard.length,flags:0,done:!heard.length,voice_uids:uids,
    voice_secs:Math.max(0,...heard.map(h=>h[2]))}};
}
function walk(policy,land){
  const L=lib();
  const sync=()=>{S.timeline=Object.keys(L).map(b=>rowOf(b,L[b]));
    S.unknowns=unk.filter(u=>Object.keys(L).some(b=>L[b].some(h=>h[1]===u.display)));};
  sync();
  S.tray=[{kind:'unknown_voice',target:'U007'}];
  const label=finishActsHTML(voiceQueueCount(S.timeline),0,false,false);
  const asked=[],opened=[];
  FS=null;FS_CARRY=null;toasts.length=0;location.hash='';
  fsVoicesStart();
  for(let guard=0;guard<20&&FS_CARRY;guard++){
    const base=decodeURIComponent(location.hash.slice(3));
    opened.push(base);route={view:'meeting',base};
    MP={base,built:true,spkOpts:L[base].map(h=>({id:h[0],display:h[1],named:false,dismissed:false})),
      segs:L[base].map(h=>({speaker:h[0],start:0,end:h[2],text:'x'}))};
    fsInit(base);
    fsSegsLoaded();
    while(FS&&!FS_CARRY&&FS.cur==='voices'&&FS.voices[FS.vi]&&!FS.voices[FS.vi].st){
      const v=FS.voices[FS.vi];
      asked.push(base+':'+v.display);
      if(policy==='skip'){fvSkip(false);continue;}
      // Save and next, as fvSave does on success
      v.st='named';
      if(v.uid)FS.resolved.push(v.uid);
      if(land){
        for(const b of Object.keys(L))
          L[b]=L[b].filter(h=>v.uid?h[1]!==v.display:!(b===base&&h[0]===v.id));
        sync();
      }
      fvNext();
    }
  }
  return {label,asked,opened,toasts:toasts.slice(),mode:FS&&FS.mode};
}
""",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_name_n_voices_asks_about_exactly_n_voices():
    """The label's N is the number of voices the queue asks about, whether the
    user names each one (with the relabel landing at once, or still on its
    way) or skips each one. Only one voice here is a visible registry voice,
    so the old label read "Name 1 voice"; the sum of the meetings' counts is
    7, because Speaker 7 and Speaker 12 are each heard in two meetings."""
    out = _run_node(_voice_walk_env() + """
console.log(JSON.stringify({named:walk('name',true),lagging:walk('name',false),
  skipped:walk('skip',false)}));
""")
    every = ["A:Speaker 7", "A:Speaker 1", "B:Speaker 12", "C:Speaker 1", "C:Speaker 2"]
    for key in ("named", "lagging", "skipped"):
        w = out[key]
        assert ">Name 5 voices</button>" in w["label"], key
        assert w["asked"] == every, key
        assert len(w["asked"]) == 5, key
        assert w["toasts"] == ["No more voices to name."], key
        assert w["mode"] is None, key
    # named with the relabel landed, D has nothing left and never opens; while
    # the relabel is still on its way, or after a skip, the queue passes D by
    # on the uid it already asked about
    for key in ("named", "lagging", "skipped"):
        assert out[key]["opened"] == ["A", "B", "C"], key


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_meeting_whose_every_voice_was_skipped_is_not_asked_again():
    """A stale row can still lead the queue into a meeting whose only open
    voice the run already skipped. The step never asks about it again: Voices
    is moved past, the queue ends, and the strip leaves the voice for later."""
    out = _run_node(_voice_walk_env() + """
const L=lib();
S.timeline=Object.keys(L).map(b=>rowOf(b,L[b]));S.unknowns=unk;
FS_CARRY={base:'D',past:['title','flags'],live:true,keep:true,mode:'voices',
  seen:['A','B','C','D'],resolved:['U007'],passed:['U012']};
route={view:'meeting',base:'D'};
MP={base:'D',built:true,spkOpts:[{id:'S12',display:'Speaker 12',named:false,dismissed:false}],
  segs:[{speaker:'S12',start:0,end:20,text:'x'}]};
fsInit('D');fsSegsLoaded();
console.log(JSON.stringify({cur:FS.cur,mode:FS.mode,past:FS.past,toasts,
  st:FS.voices.map(v=>v.st),asking:box.innerHTML.includes('Who is'),
  left:box.innerHTML.includes('Left for later: 1 voice.')}));
""")
    assert out["asking"] is False and out["st"] == ["skip"]
    assert out["mode"] is None and out["toasts"] == ["No more voices to name."]
    assert out["past"] == ["voices"] and out["left"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_skipped_registry_voice_is_remembered_by_the_queue():
    out = _run_node(_voices_env() + """
document.activeElement=body;
key('s');const first=(FS.passed||[]).slice();
document.activeElement=body;
key('s');                        // Speaker 61 is transcript-local: no uid
console.log(JSON.stringify({first,then:FS.passed,st:FS.voices.map(v=>v.st)}));
""")
    assert out == {"first": ["U058"], "then": ["U058"], "st": ["skip", "skip"]}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_eyebrow_counts_the_voices_the_queue_will_ask_about():
    """drawTimeline labels "Name N voices" with voiceQueueCount, never the
    tray's count of visible registry voices. With no live meeting holding a
    voice, the tray's voices open one at a time in the naming panel, and the
    button's title says so. A row whose voice becomes a registry voice
    rebuilds the list even though its own voice count did not move."""
    out = _run_node(_timeline_env() + """
const recent=new Date(Date.now()-864e5).toISOString().slice(0,19);
const f=(v,uids)=>({title:true,voices:v,flags:0,done:!v,voice_secs:1,voice_uids:uids});
const voice={kind:'unknown_voice',title:'Speaker 7',detail:'',target:'U007',count:2};
const tl=[{id:'A',state:'ready',when:recent,date:'2026-09-26',finish:f(2,['U007'])},
  {id:'B',state:'ready',when:recent,date:'2026-09-25',finish:f(2,['U007','U012'])},
  {id:'C',state:'ready',when:recent,date:'2026-09-24',finish:f(1,[])}];
drawTimeline({timeline:tl,tray:[voice]});
const queue=html(),s0=els['#timeline'].dataset.sig;
drawTimeline({timeline:[tl[0],tl[1],{...tl[2],finish:f(1,['U007'])}],tray:[voice]});
const s1=els['#timeline'].dataset.sig,merged=html();
els['#timeline'].dataset.sig='';
drawTimeline({timeline:[{id:'D',state:'ready',when:recent,date:'2026-09-24',finish:f(0,[])}],
  tray:[voice,{...voice,title:'Speaker 9',target:'U009'}]});
const panel=html();
console.log(JSON.stringify({queue,panel,merged,moved:s1!==s0}));
""")
    # U007 (heard in A and B), U012, A's local voice, and C's: four. Never
    # the tray's 1 (U007 alone is a visible registry voice) and never the
    # meetings' sum of 5
    assert ">Name 4 voices</button>" in out["queue"]
    assert "the most talk time first" in out["queue"]
    # C's voice turns out to be Speaker 7 too: one voice fewer to ask about,
    # though no meeting's own count moved, so the list must still rebuild
    assert out["moved"] is True and ">Name 3 voices</button>" in out["merged"]
    assert ">Name 2 voices</button>" in out["panel"]
    assert "Heard only in archived meetings: name them one at a time" in out["panel"]
    assert 'onclick="fsVoicesStart()"' in out["panel"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_tab_title_counts_meetings_that_joined_to_finish_while_hidden():
    listener = re.search(r"document\.addEventListener\('visibilitychange',[\s\S]*?\n\}\);",
                         NEW_JS).group(0)
    out = _run_node(_finish_env() + "\n".join([
        "const handlers={};",
        "const document={hidden:true,title:'Meetings',"
        "addEventListener:(k,f)=>{handlers[k]=f;}};",
        "let TAB_SEEN=null;const TAB_JOINED=new Set();",
        _js_fn("finishIds"), _js_fn("tabJoins"), _js_oneline("tabTitle"), _js_fn("tabWatch"),
        listener, """
const recent=new Date(Date.now()-864e5).toISOString().slice(0,19);
const done={id:'D',state:'ready',when:recent,finish:fin(true,0,0)};
const t=[];
tabWatch({timeline:[{id:'OLD',state:'needs_name',when:recent,finish:fin(false,0,0)},done]});
t.push(document.title);                        // the first poll is the baseline
tabWatch({timeline:[{id:'OLD',state:'needs_name',finish:fin(false,0,0)},
  {id:'N1',state:'needs_name',when:recent,finish:fin(false,1,0)},done]});
t.push(document.title);
// N1 is named and leaves; N2 joins: two meetings joined since hidden
tabWatch({timeline:[{id:'N2',state:'ready',when:recent,finish:fin(true,1,0)},done]});
t.push(document.title);
tabWatch({timeline:[{id:'N2',state:'ready',when:recent,finish:fin(true,1,0)},done]});
t.push(document.title);                        // an unchanged poll adds nothing
document.hidden=false;handlers.visibilitychange();
t.push(document.title);
// while visible nothing counts, and hiding again starts from zero
tabWatch({timeline:[{id:'N3',state:'needs_name',when:recent,finish:fin(false,0,0)}]});
t.push(document.title);
document.hidden=true;handlers.visibilitychange();
tabWatch({timeline:[{id:'N3',state:'needs_name',when:recent,finish:fin(false,0,0)},
  {id:'N4',state:'needs_name',when:recent,finish:fin(false,0,0)}]});
t.push(document.title);
console.log(JSON.stringify({t,joins:[tabJoins(null,['a']),tabJoins(['a'],['a','b'])],
  titles:[tabTitle(0),tabTitle(3)]}));
"""]))
    assert out["t"] == ["Meetings", "(1) Meetings", "(2) Meetings", "(2) Meetings",
                        "Meetings", "Meetings", "(1) Meetings"]
    assert out["joins"] == [[], ["b"]]
    assert out["titles"] == ["Meetings", "(3) Meetings"]
    assert "tabWatch(S);" in _js_fn("refresh")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_fix_outcome_on_a_now_row_survives_the_rebuild():
    """The tray wrote "Done" and the reset message into its own DOM, which
    only rebuilt when the tray changed. A Now row lives in the list, and the
    list rebuilds on every processing tick, so the outcome is state now."""
    out = _run_node("\n".join(_TRAY_HARNESS + [
        "let reply={ok:true,message:'Permissions reset. Try recording again.'};",
        "function api(){return Promise.resolve(reply);}",
        "let draws=0,refreshes=0;function nowRedraw(){draws++;}",
        "function refresh(){refreshes++;}",
        "function openDrawer(){}",
        _js_fn("trayAct"), """
(async()=>{
  S={recorder_note:{ok:false,at:'t1',text:'The recorder captured no audio. Check microphone access.'},
     tray:[{kind:'recorder_stall',title:'Recording',detail:'no audio',target:'rec:x',count:1}]};
  const p=trayAct('recorder_note','');
  draw(S);const busy=tray.innerHTML;
  await p;draw(S);const done=tray.innerHTML;const sig1=tray.sig;
  reply={ok:false,error:'tccutil failed'};
  await trayAct('recorder_stall','rec:x');draw(S);const failed=tray.innerHTML;
  reply={ok:true};
  await trayAct('recorder_stall','rec:x');draw(S);const cleared=tray.innerHTML;
  // a NEW failed note starts clean
  S.recorder_note={ok:false,at:'t2',text:'No audio again.'};draw(S);const fresh=tray.innerHTML;
  console.log(JSON.stringify({busy,done,failed,cleared,fresh,draws,refreshes,
    sigMoved:sig1!==tray.sig}));
})().catch(e=>{console.log(JSON.stringify({threw:String(e)}));});
"""]))
    assert "threw" not in out, out
    assert ">Resetting…</button>" in out["busy"] and "disabled>Resetting" in out["busy"]
    assert "Permissions reset. Try recording again." in out["done"]
    assert "disabled>Done ✓</button>" in out["done"]
    assert "tccutil failed" in out["failed"] and ">Retry</button>" in out["failed"]
    assert ">Fix</button>" in out["cleared"] and "tccutil failed" not in out["cleared"]
    assert ">Fix permissions</button>" in out["fresh"] and "No audio again." in out["fresh"]
    assert out["draws"] == 6 and out["refreshes"] == 2 and out["sigMoved"] is True


def test_phase_two_styles_ride_the_tokens():
    """The section rules add no colour of their own, keep the 13px floor, and
    give the To finish rows the focus ring and the reduced-motion kill."""
    sec = NEW_CSS[NEW_CSS.index("/* ------------------------------------------------ Now and To finish"):
                  NEW_CSS.index("/* ------------------------------------------------- timeline + rail")]
    for cls in (".trayrow{", ".sechead{", ".secact{", ".lrow{", ".steps{", ".sdot{",
                ".sdot.done{"):
        assert cls in sec, f"missing {cls}"
    assert not re.findall(r"#[0-9A-Fa-f]{3,6}\b", sec), "no new colors"
    assert ":where(button,select,input,.row,.lrow,.railbtn):focus-visible" in NEW_CSS
    assert ".lrow,.lrow.updated," in NEW_CSS     # the reduced-motion transition kill
    assert re.search(r"(?m)^\.lrow \.ltitle\{[^}]*font:600 15px", NEW_CSS)


# ---------------------------------------------------------------------------
# docs/ui-refresh phase 3: two panes. At 900px and wider the list keeps the
# left pane and the meeting page mounts beside it, in the reader; below 900px
# the page is today's one column. The deciders and the selection machinery
# run under node over the extracted source, with fake timers and a location
# that records every push and replace (hashchange fires on flush(), after the
# handler that moved the hash returns, as a browser fires it). Placement and
# type sizes are regex over the files.
# ---------------------------------------------------------------------------
def _panes_env():
    """The TWO PANES section, the route, and the section builder it reads,
    over a stubbed browser. Everything runs inside one function scope, so the
    fake setTimeout never replaces node's own."""
    return "\n".join([
        _js_const("FINISH_DAYS"), _js_fn("toFinish"),
        _js_const("PIN_ORDER"), _js_const("NOW_STATES"),
        re.search(r"(?m)^function dateKey\(.*$", NEW_JS).group(0),
        _js_fn("timelineSections"), _js_fn("parseHash"), _js_fn("applyRoute"),
        _js_region("const PANES_MIN=900;",
                   "/* ------------------------------------------------------ build the page"),
        """
let route={view:'timeline',base:null},S=null,MP=null,FS_CARRY=null,kbLast=null,NP=null;
let LAYER=false;function kbLayerOpen(){return LAYER;}
const log=[];
let NOW_T=0;const timers=[];
function setTimeout(f,ms){const t={f,at:NOW_T+ms,live:true};timers.push(t);return t;}
function clearTimeout(t){if(t)t.live=false;}
function tick(ms){NOW_T+=ms;for(const t of timers.slice())if(t.live&&t.at<=NOW_T){t.live=false;t.f();}}
const nav=[];let pending=0;
const location={_h:'',get hash(){return this._h;},
  set hash(v){const n=(v===''||v==='#')?'':(v[0]==='#'?v:'#'+v);if(n===this._h)return;
    this._h=n;nav.push('push:'+decodeURIComponent(n));pending++;},
  replace(v){this._h=v;nav.push('replace:'+decodeURIComponent(v));pending++;}};
function flush(){while(pending){pending--;route=parseHash();applyRoute();}}
function closeNamePanel(){} function dupeDisarm(){}
function enterMeeting(b){log.push('mount:'+b+(RQUIET===b?':quiet':''));
  RQUIET=null;MP={base:b,built:true};}
function exitMeeting(){log.push('exit');MP=null;}
function mStickyTop(){} let renders=0;function render(){renders++;}
function fsWake(){log.push('wake');return true;}
const CSS={escape:s=>s};
const PANE={scrollTop:0,getBoundingClientRect:()=>({top:64,bottom:1000})};
function mkRow(id,state,top){const cls=new Set();return {dataset:{id,state},cls,
  classList:{add:c=>cls.add(c),remove:c=>cls.delete(c),contains:c=>cls.has(c)},
  attrs:{},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},
  closest:q=>q==='.wrap'?PANE:(q==='#timeline'?{}:null),blur(){log.push('blur:'+id);},
  getBoundingClientRect:()=>({top:top||0,bottom:(top||0)+60})};}
let ROWS=[];
const bodyCls=new Set();
const document={activeElement:null,
  body:{classList:{toggle:(c,on)=>{on?bodyCls.add(c):bodyCls.delete(c);},contains:c=>bodyCls.has(c)}},
  querySelector(q){const m=q.match(/data-id="([^"]*)"/);
    return m?ROWS.find(r=>r.dataset.id===m[1])||null:null;},
  querySelectorAll(q){return q==='#timeline .sel'?ROWS.filter(r=>r.cls.has('sel')):[];}};
const window={innerWidth:1440};
const sel=()=>ROWS.filter(r=>r.cls.has('sel')).map(r=>r.dataset.id);
const ago=h=>new Date(Date.now()-h*3600e3).toISOString().slice(0,19);
const fin=(title,voices,flags)=>({title,voices,flags,done:title&&!voices&&!flags});
""",
    ])


def _panes_run(body):
    return _run_node("(function(){\n" + _panes_env() + "\n" + body + "\n})();")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_two_panes_from_900px_and_the_narrow_fallback_below():
    """The narrow fallback decision: two panes from 900px up, one column
    below. One column keeps today's page: no row wears --sel, a walk never
    mounts the reader, no hash opens nothing, and a meeting that leaves
    returns to the list."""
    out = _panes_run("""
const o={};
o.edge=[420,899,900,1100,1440].map(w=>twoPanes(w));
S={timeline:[{id:'A',state:'ready',date:'2026-09-01',when:ago(500),finish:fin(true,0,0)}]};
ROWS=[mkRow('A','ready',100)];
// wide: the class goes on, the list redraws beside the reader
window.innerWidth=1440;paneSync();
o.wide={PANES,cls:[...bodyCls],renders};
// no hash in two panes: the default opens, quietly, replacing the empty entry
o.wideSync=readerSync();flush();
o.wideNav=nav.slice();o.wideLog=log.slice();o.wideSel=sel();
// narrowing: the class comes off, a pending walk dies, --sel goes
selectRow('A',true);
window.innerWidth=800;paneSync();
o.narrow={PANES,cls:[...bodyCls],sel:sel(),walkLive:timers.some(t=>t.live)};
// one column: no hash opens nothing, and a meeting that leaves goes to the list
route={view:'timeline',base:null};nav.length=0;log.length=0;
o.narrowSync=readerSync();
route={view:'meeting',base:'A'};location._h='#m/A';
readerLeave('A');flush();
o.narrowLeave={nav:nav.slice(),log:log.slice()};
console.log(JSON.stringify(o));
""")
    assert out["edge"] == [False, False, True, True, True]
    assert out["wide"]["PANES"] is True and out["wide"]["cls"] == ["panes"]
    assert out["wide"]["renders"] == 1, "the list comes back beside the reader"
    assert out["wideSync"] is True
    assert out["wideNav"] == ["replace:#m/A"], "the default replaces, never pushes"
    assert out["wideLog"] == ["mount:A:quiet"]
    assert out["wideSel"] == ["A"]
    assert out["narrow"] == {"PANES": False, "cls": [], "sel": [], "walkLive": False}
    assert out["narrowSync"] is False
    assert out["narrowLeave"] == {"nav": ["push:"], "log": ["exit"]}


def test_the_css_lays_out_two_panes_only_while_a_meeting_is_open():
    """The list pane is 380px (330px under 1100px) with its own scroll, sticky
    under the header; the reader is the meeting page on the card ground.
    One column hides the list behind a meeting and keeps the back link."""
    sec = NEW_CSS[NEW_CSS.index("   Two panes (docs/ui-refresh/proposal.md, phase 3)"):]
    assert "body.panes{--listw:380px}" in sec
    m = re.search(r"@media\(max-width:1099px\)\{([^@]*)\n\}", sec)
    assert m and "body.panes{--listw:330px}" in m.group(1)
    assert re.search(r"body\.panes\.route-meeting\{[^}]*display:grid;[^}]*"
                     r"grid-template-columns:var\(--listw\) minmax\(0,1fr\)", sec)
    wrap = re.search(r"body\.panes\.route-meeting \.wrap\{[^}]*\}", sec).group(0)
    for frag in ("position:sticky", "top:var(--hdrh,64px)", "overflow-y:auto",
                 "height:calc(100vh - var(--hdrh,64px))", "border-right:1px solid var(--hair)"):
        assert frag in wrap, frag
    assert re.search(r"body\.panes\.route-meeting \.mpage\{[^}]*background:var\(--card\)", sec)
    assert "body.panes .mback{display:none}" in sec
    # the ask bar and the bulk bar keep to their own panes
    assert re.search(r"body\.panes\.route-meeting \.maskbar\{left:var\(--listw\);right:0;", sec)
    assert re.search(r"body\.panes\.route-meeting \.bulkbar\{[^}]*max-width:calc\(var\(--listw\)", sec)
    # one column: the list and its floating surfaces hide behind a meeting
    assert "body.route-meeting:not(.panes) .wrap{display:none}" in NEW_CSS
    assert "body.route-meeting:not(.panes) .bulkbar{display:none!important}" in NEW_CSS
    assert "body.route-meeting .wrap{display:none}" not in NEW_CSS
    # the back link still renders; the width decides whether it shows
    assert 'class="mback"' in _js_fn("_mHeader")
    # the header height feeds the pane's sticky top, from the live header
    assert "setProperty('--hdrh',top+'px')" in _js_fn("mStickyTop")
    assert "paneSync();" in NEW_JS[NEW_JS.index("addEventListener('resize'"):][:80]
    # the section adds no colour of its own
    assert not re.findall(r"#[0-9A-Fa-f]{3,6}\b", sec), "no new colors"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_reader_opens_the_first_to_finish_row_else_the_newest():
    out = _panes_run("""
const tl=[
  {id:'P',state:'processing',when:ago(1)},
  {id:'Old',state:'ready',date:'2026-06-01',when:ago(900),finish:fin(true,2,0)},
  {id:'Name',state:'needs_name',date:'2026-09-20',when:ago(30),finish:fin(false,0,0)},
  {id:'Voices',state:'ready',date:'2026-09-25',when:ago(10),finish:fin(true,1,0)},
  {id:'New',state:'ready',date:'2026-09-26',when:ago(5),finish:fin(true,0,0)},
  {id:'Mid',state:'ready',date:'2026-09-10',when:ago(400),finish:fin(true,0,0)}];
const done=tl.map(r=>r.finish?Object.assign({},r,{state:'ready',finish:fin(true,0,0)}):r);
console.log(JSON.stringify({
  first:readerDefault(tl),
  skip:readerDefault(tl,undefined,'Name'),
  newest:readerDefault(done),
  newestSkip:readerDefault(done,undefined,'New'),
  nowOnly:readerDefault([{id:'P',state:'processing'},{id:'W',state:'waiting'}]),
  empty:readerDefault(null),
  opens:['ready','needs_name','processing','waiting','held','failed','recording'].map(rowOpens),
  lost:[readerLost(tl,'New'),readerLost(tl,'Gone'),readerLost(null,'New')],
}));
""")
    assert out["first"] == "Name", "the first To finish row, in timeline order"
    assert out["skip"] == "Voices", "a meeting that just left is never the default"
    assert out["newest"] == "New", "no To finish row: the newest meeting"
    assert out["newestSkip"] == "Voices", "the next newest by meeting date"
    assert out["nowOnly"] is None and out["empty"] is None
    assert out["opens"] == [True, True, False, False, False, False, False]
    assert out["lost"] == [False, True, True]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_two_panes_is_the_narrow_fallback_decision_at_900px():
    """twoPanes(width) alone: 900px and wider is two panes, anything
    narrower keeps the one column."""
    out = _run_node("\n".join([
        "const PANES_MIN=900;", _js_oneline("twoPanes"),
        "console.log(JSON.stringify([0,375,420,720,899,899.5,900,901,1099,1100,1440,2560]"
        ".map(w=>twoPanes(w))));"]))
    assert out == [False] * 6 + [True] * 6
    assert "const PANES_MIN=900;" in NEW_JS


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_list_is_a_pane_only_beside_a_meeting_in_the_reader():
    """paned() alone: the list is a pane (its own scroll, --sel, the ring
    scrolling it) only with two panes AND a meeting in the reader. Two panes
    over an empty library, or one column with a meeting open, are not."""
    out = _run_node("\n".join([
        _js_oneline("paned"), "let PANES=false,route={view:'timeline'};",
        "const o=[];",
        "for(const two of [false,true])for(const view of ['timeline','meeting']){",
        "  PANES=two;route={view,base:view==='meeting'?'M1':null};o.push(paned());}",
        "console.log(JSON.stringify(o));"]))
    assert out == [False, False, False, True]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_only_a_meeting_row_opens_in_the_reader():
    """rowOpens(state) alone: a ready meeting or one waiting for its title
    opens in the reader. A live, queued, held, failed, recording, or upload
    row never does, and neither does a row without a state."""
    out = _run_node("\n".join([
        _js_oneline("rowOpens"),
        "console.log(JSON.stringify(['ready','needs_name','processing','waiting','held',"
        "'failed','recording','uploading','done','',null,undefined,'READY'].map(rowOpens)));"]))
    assert out == [True, True] + [False] * 11


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_reader_is_lost_only_when_the_library_drops_its_meeting():
    """readerLost(timeline, base) alone: true only when no timeline row
    carries the reader's id. A missing or empty timeline counts as lost, and
    null holes in the list are skipped, never read."""
    out = _run_node("\n".join([
        _js_oneline("readerLost"),
        "const tl=[null,{id:'A'},{id:'B',state:'ready'},{}];",
        "console.log(JSON.stringify([readerLost(tl,'A'),readerLost(tl,'B'),",
        "  readerLost(tl,'C'),readerLost(tl,undefined),readerLost([],'A'),",
        "  readerLost(null,'A'),readerLost(undefined,'A')]));"]))
    assert out == [False, False, True, False, True, True, True]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_click_selects_at_once_and_a_walk_mounts_the_reader_when_it_pauses():
    """Selection: the row wears --sel the moment it is chosen. A click mounts
    the reader at once; j/k mount it once the walk pauses for about 150ms, so
    holding j never rebuilds the reader on every row, and the walk's mount is
    quiet (no focus, no clip)."""
    out = _panes_run("""
const o={};
window.innerWidth=1440;paneSync();
ROWS=['A','B','C','D','E'].map((id,i)=>mkRow(id,'ready',100+i*60));
location._h='#m/A';route={view:'meeting',base:'A'};MP={base:'A',built:true};RSEL='A';
// a click: at once, one push, one mount, not quiet
selectRow('B');flush();
o.click={sel:sel(),nav:nav.slice(),log:log.slice(),aria:ROWS[1].attrs['aria-current']};
nav.length=0;log.length=0;
// a held j: three rows inside 150ms, the highlight follows each at once
selectRow('C',true);o.sel1=sel();tick(40);
selectRow('D',true);tick(40);
selectRow('E',true);o.sel3=sel();
tick(149);flush();o.early={nav:nav.slice(),log:log.slice()};
tick(1);flush();o.paused={nav:nav.slice(),log:log.slice(),sel:sel()};
nav.length=0;log.length=0;
// walking back to the reader's own row before the pause mounts nothing
selectRow('D',true);tick(60);selectRow('E',true);tick(300);flush();
o.back={nav:nav.slice(),log:log.slice(),sel:sel()};
// a click on the reader's own row changes nothing in the route
selectRow('E');flush();o.same={nav:nav.slice(),log:log.slice()};
console.log(JSON.stringify(o));
""")
    assert out["click"] == {"sel": ["B"], "nav": ["push:#m/B"], "log": ["mount:B"],
                            "aria": "true"}
    assert out["sel1"] == ["C"] and out["sel3"] == ["E"]
    assert out["early"] == {"nav": [], "log": []}, "no mount while the walk is moving"
    assert out["paused"] == {"nav": ["push:#m/E"], "log": ["mount:E:quiet"], "sel": ["E"]}
    assert out["back"] == {"nav": [], "log": [], "sel": ["E"]}
    assert out["same"] == {"nav": [], "log": []}
    # the walk's pause is the declared constant, about 150ms
    assert "const WALK_MS=150;" in NEW_JS


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_hash_moves_the_selection_and_a_lost_meeting_opens_the_default():
    """Hash-to-selection sync: a deep link, the back button, and ] or [ move
    --sel to the reader's row and cancel a pending walk. A walk that is still
    moving past the step whose mount just landed keeps its own row. A reader
    meeting that leaves the library opens the default, skipping it."""
    out = _panes_run("""
const o={};
window.innerWidth=1440;paneSync();
ROWS=['A','B','C','D'].map((id,i)=>mkRow(id,'ready',100+i*60));
// a deep link (or the back button): the row follows the hash
location.hash='#m/B';flush();
o.deep={sel:sel(),RSEL,log:log.slice()};
// ] mid-walk: the route wins and the walk dies
selectRow('C',true);location.hash='#m/D';flush();tick(400);flush();
o.bracket={sel:sel(),nav:nav.slice(-1)};
// a walk still moving when its own earlier mount lands keeps its row
nav.length=0;log.length=0;
selectRow('A',true);tick(150);        // A's mount is on its way...
selectRow('B',true);flush();          // ...and j already moved on to B
o.moving={sel:sel(),log:log.slice()};
tick(150);flush();
o.settled={sel:sel(),nav:nav.slice(),log:log.slice()};
// the reader's meeting leaves the library: the default opens in its place
S={timeline:[{id:'C',state:'ready',date:'2026-09-01',when:ago(500),finish:fin(true,0,0)},
             {id:'D',state:'ready',date:'2026-09-02',when:ago(400),finish:fin(true,0,0)}]};
nav.length=0;log.length=0;
FS_CARRY={base:'Z'};readerKeep();o.carry=nav.slice();FS_CARRY=null;
LAYER=true;readerKeep();o.layer=nav.slice();LAYER=false;
readerKeep();flush();o.kept={nav:nav.slice(),log:log.slice(),sel:sel()};
// archive from the reader: replace with the default, skipping the one leaving
nav.length=0;log.length=0;
readerLeave('D');flush();o.leave={nav:nav.slice(),log:log.slice()};
// nothing left to read: back to the list alone
S={timeline:[]};nav.length=0;log.length=0;
readerLeave('C');flush();o.empty={nav:nav.slice(),log:log.slice(),sel:sel()};
// a list edit re-stamped the reader's meeting: the reader follows it
location._h='#m/C';route={view:'meeting',base:'C'};nav.length=0;
readerFollow('C','C2');readerFollow('D','D2');readerFollow('C',null);
o.follow=nav.slice();
console.log(JSON.stringify(o));
""")
    assert out["deep"] == {"sel": ["B"], "RSEL": "B", "log": ["mount:B"]}
    assert out["bracket"] == {"sel": ["D"], "nav": ["push:#m/D"]}
    assert out["moving"] == {"sel": ["B"], "log": ["mount:A:quiet"]}
    assert out["settled"] == {"sel": ["B"], "nav": ["push:#m/A", "push:#m/B"],
                              "log": ["mount:A:quiet", "mount:B:quiet"]}
    assert out["carry"] == [] and out["layer"] == []
    assert out["kept"] == {"nav": ["replace:#m/D"], "log": ["mount:D:quiet"], "sel": ["D"]}
    assert out["leave"] == {"nav": ["replace:#m/C"], "log": ["mount:C:quiet"]}
    assert out["empty"] == {"nav": ["push:"], "log": ["exit"], "sel": []}
    assert out["follow"] == ["replace:#m/C2"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_list_pane_scrolls_itself_and_never_the_reader():
    out = _panes_run("""
const o={};
const at=(top)=>{PANE.scrollTop=100;const r=mkRow('X','ready',top);return r;};
let r=at(300);paneReveal(r);o.inside=PANE.scrollTop;
r=at(20);paneReveal(r);o.above=PANE.scrollTop;
r=at(980);paneReveal(r);o.below=PANE.scrollTop;
r=at(500);paneReveal(r,true);o.start=PANE.scrollTop;
paneReveal(null);paneReveal({closest:()=>null});
console.log(JSON.stringify(o));
""")
    assert out["inside"] == 100, "a row already in view stays put"
    assert out["above"] == 100 - (64 + 8 - 20)
    assert out["below"] == 100 + (980 + 60 - 1000 + 8)
    assert out["start"] == 100 + (500 - 64 - 8), "a rail jump lands the header at the top"
    # the ring and the rail use it in two panes; one column keeps the window's scroll
    assert "if(paned())paneReveal(el);" in _js_fn("kbFocusRow")
    assert "scrollIntoView({block:'nearest'})" in _js_fn("kbFocusRow")
    assert "paneReveal(el,true)" in _js_fn("railJump")


def _panes_keys_env():
    """kbKey, kbMove, and the real mKey / fsKey over a stub page in two panes:
    a focused list row, the reader's stepper active, and recorders for what
    each handler did."""
    return "\n".join([
        _js_oneline("kbLayerOpen"), _js_fn("kbKey"), _js_fn("kbMove"), _js_fn("kbLand"),
        _js_fn("mKey"), _js_fn("fsKey"), _js_fn("rowOpens"), _js_fn("keySheetKey"),
        "let NP=null,_popClose=null,kbLast=null,FS=null,PANES=true,RSEL='M1';",
        "let KSHEET=false,SAID_TOP=null;function keySheetOpen(){KSHEET=true;log.push('keys');}"
        "function keySheetClose(){KSHEET=false;}function openHit(){}",
        "let MR={active:true,items:[{}],i:0};",
        "const DRAWER={open:false};",
        "let route={view:'meeting',base:'M1'};",
        "const log=[];",
        "class Element{closest(q){return this._closest(q);}matches(q){return this._matches(q);}}",
        "const CSS={escape:s=>s};",
        "const KB_RING='#timeline .row[tabindex], #timeline .lrow, #timeline .ghead';",
        "function mkEl(tag,id,state,inList,isRow){const e=new Element();e.tagName=tag;"
        "e.dataset={id,state};e._closest=q=>q==='.wrap'?(inList?{}:null)"
        ":(q.includes('.row[tabindex]')&&isRow?e:(q===KB_RING&&isRow?e:null));"
        "e._matches=q=>isRow&&q.includes('.row[tabindex]');"
        "e.querySelector=q=>q==='.chk:not(.spacer)'?{}:null;"
        "e.focus=()=>{document.activeElement=e;log.push('ring:'+id);};"
        "e.getBoundingClientRect=()=>({top:100,bottom:160});return e;}",
        "const row1=mkEl('DIV','M1','ready',true,true),row2=mkEl('DIV','M2','ready',true,true);",
        "const body=new Element();body.tagName='BODY';body._closest=()=>null;body._matches=()=>false;",
        "const se={tagName:'INPUT',value:'',focus(){log.push('search');},blur(){}};",
        "const mf={tagName:'INPUT',focus(){log.push('find');},select(){}};",
        "const document={activeElement:body,body,getElementById:()=>null,"
        "querySelectorAll:()=>[row1,row2],querySelector:()=>row1};",
        "const location={hash:'#m/M1'};",
        "function $(q){return q==='#search'?se:(q==='#mfind'?mf:(q==='.hdr'?{offsetHeight:64}:null));}",
        "function kbRows(){return [row1,row2];}",
        "function kbFocusRow(el){if(el){el.focus();kbLast=el.dataset.id;}}",
        "function selRowEl(){return RSEL==='M1'?row1:row2;}",
        "function render(){} function scheduleSearch(){}",
        "function openMeeting(){log.push('open');} function toggleExpand(){log.push('peek');}",
        "function readerOpen(id){log.push('readerOpen:'+id);}",
        "function selectRow(id,walk){log.push('select:'+id+(walk?':walk':''));}",
        "const SEL=new Set();function toggleSel(id,on){log.push('bulk:'+id+':'+on);on?SEL.add(id):SEL.delete(id);}",
        "function reviewGo(d){log.push('reviewGo:'+d);} function reviewStep(){log.push('reviewStep');}",
        "function reviewApply(v){log.push('apply:'+v);} function reviewUseAltGo(){}",
        "function reviewExit(){log.push('exit');} function mCloseCard(){}",
        "function fsGo(d){log.push('fsGo:'+d);return true;}",
        # both listeners see the SAME event, in registration order, as a browser runs them
        "function press(k,target){log.length=0;const ev={key:k,metaKey:false,ctrlKey:false,"
        "altKey:false,target:target||document.activeElement,defaultPrevented:false,"
        "preventDefault(){this.defaultPrevented=true;}};kbKey(ev);mKey(ev);"
        "return {log:log.slice(),prevented:ev.defaultPrevented};}",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_list_keys_in_two_panes_never_reach_the_reader():
    """Two panes put the list's keys and the reader's on one page. A key the
    list takes (its Enter, x, j, k, and / from the list) is never also the
    reader's: Enter on a focused row selects it and never accepts a flag or
    saves a voice. The reader's own keys (n, a, ]) still work from the list,
    Escape in the reader puts the ring on the reader's row, and one column
    keeps today's Escape back to the list."""
    out = _run_node(_panes_keys_env() + """
const o={};
document.activeElement=row2;
o.enter=press('Enter',row2);
o.x=press('x',row2);o.x2=press('x',row2);
o.j=press('j');
o.slashList=press('/',row2);
document.activeElement=body;
o.slashReader=press('/',body);
o.n=press('n',body);o.a=press('a',body);o.bracket=press(']',body);
MR=null;o.esc=press('Escape',body);
PANES=false;o.escNarrow=press('Escape',body);o.jNarrow=press('j',body);
console.log(JSON.stringify(o));
""")
    assert out["enter"] == {"log": ["readerOpen:M2"], "prevented": True}, \
        "the list's Enter must never also accept the flag under it"
    assert out["x"] == {"log": ["bulk:M2:true"], "prevented": True}
    assert out["x2"] == {"log": ["bulk:M2:false"], "prevented": True}
    assert out["j"]["log"][-1] == "select:M2:walk" and out["j"]["prevented"] is True
    assert out["slashList"] == {"log": ["search"], "prevented": True}
    assert out["slashReader"] == {"log": ["find"], "prevented": True}
    assert out["n"] == {"log": ["reviewGo:1"], "prevented": True}
    assert out["a"] == {"log": ["apply:accept"], "prevented": True}
    assert out["bracket"] == {"log": ["fsGo:1"], "prevented": True}
    assert out["esc"] == {"log": ["ring:M1"], "prevented": True}
    assert out["escNarrow"]["prevented"] is True and out["escNarrow"]["log"] == []
    assert out["jNarrow"] == {"log": [], "prevented": False}, \
        "one column: the meeting page owns the keys, as before"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_an_open_popover_keeps_the_list_keys_and_the_readers_under_it():
    """QA, phase 3: with the Process popover (or a ⋯ menu) open, x put the
    focused row into the bulk selection under it, and j, k, Enter and / sat
    behind the same guard, which checked only the naming panel and the
    drawer. An open popover owns the plain keys as those layers do, in either
    layout: nothing under it moves the ring, the bulk selection, or the
    reader, and a key the list holds back never lands in the reader instead.
    Its own Escape (registered by openPop, not here) closes it, a button in
    it keeps its own Enter and Space, and a chord still reaches the page.
    Once it closes, every key works again."""
    out = _run_node(_panes_keys_env() + """
const o={};
const KEYS=['x','j','k','Enter','e','/','n','p','a','u',']','[','Escape'];
const run=t=>Object.fromEntries(KEYS.map(k=>[k,press(k,t)]));
const popBtn=new Element();popBtn.tagName='BUTTON';popBtn.dataset={};
popBtn._closest=()=>null;popBtn._matches=()=>false;
_popClose=()=>{};
// two panes: the ring on a list row, then focus in the reader
document.activeElement=row2;o.wideList=run(row2);
document.activeElement=body;o.wideReader=run(body);
// a button inside the popover keeps its native Enter and Space
document.activeElement=popBtn;o.popEnter=press('Enter',popBtn);o.popSpace=press(' ',popBtn);
// a chord is a deliberate reach past the menu: Cmd-F is still the find
document.activeElement=body;log.length=0;
const cmdF={key:'f',metaKey:true,ctrlKey:false,altKey:false,target:body,
  defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};
kbKey(cmdF);mKey(cmdF);o.cmdF={log:log.slice(),prevented:cmdF.defaultPrevented};
// one column, on the meeting page and on the list
PANES=false;document.activeElement=body;o.narrowMeeting=run(body);
route={view:'timeline',base:null};document.activeElement=row2;o.narrowList=run(row2);
o.sel=[...SEL];
// the popover closes: the list's keys and the reader's come back
_popClose=null;
o.closedNarrow={x:press('x',row2),enter:press('Enter',row2),e:press('e',row2)};
SEL.clear();PANES=true;route={view:'meeting',base:'M1'};
o.closedWide={x:press('x',row2),enter:press('Enter',row2)};
document.activeElement=body;o.closedWide.n=press('n',body);
console.log(JSON.stringify(o));
""")
    quiet = {"log": [], "prevented": False}
    for where in ("wideList", "wideReader", "narrowMeeting", "narrowList"):
        for k, v in out[where].items():
            assert v == quiet, f"{k} fired under the popover ({where}): {v}"
    assert out["sel"] == [], "x under a popover must not touch the bulk selection"
    assert out["popEnter"] == quiet and out["popSpace"] == quiet, \
        "a popover button's own Enter and Space are never swallowed"
    assert out["cmdF"] == {"log": ["find"], "prevented": True}
    assert out["closedNarrow"] == {"x": {"log": ["bulk:M2:true"], "prevented": True},
                                   "enter": {"log": ["open"], "prevented": True},
                                   "e": {"log": ["peek"], "prevented": True}}
    assert out["closedWide"] == {"x": {"log": ["bulk:M2:true"], "prevented": True},
                                 "enter": {"log": ["readerOpen:M2"], "prevented": True},
                                 "n": {"log": ["reviewGo:1"], "prevented": True}}
    # the guards themselves: the list's sits with the layers', and the
    # reader's lets a Cmd or Ctrl chord through
    assert "if(kbLayerOpen()||_popClose)return;" in _js_fn("kbKey")
    assert "if(_popClose&&!e.metaKey&&!e.ctrlKey)return;" in _js_fn("mKey")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_walk_mounts_quietly_and_enter_on_the_readers_row_takes_the_keyboard():
    """A j/k walk (and the default open) mounts a meeting to finish without
    taking focus or playing its clip, so the next j walks on. Enter or a
    click on the reader's own row hands the keyboard to the reader: the ring
    lets go and the step's input takes focus, the voice clip plays, or the
    flags start stepping."""
    out = _run_node("\n".join([
        _js_fn("fsWake"), _js_region("function readerTake(){", "\n}\n") + "\n}",
        "let FS=null,MR=null,kbLast='M1';const log=[];",
        "const els={'#fstitle':{focus(){log.push('focus:title');}},"
        "'#fvname':{focus(){log.push('focus:name');}}};",
        "function $(q){return els[q]||null;}",
        "function fvPlay(){log.push('clip');} function reviewStart(){log.push('stepping');}",
        "const row={closest:q=>q==='#timeline'?{}:null,blur(){log.push('blur');}};",
        "const document={activeElement:row};",
        """
const o={};
o.none=fsWake();
FS={cur:'title',live:false};readerTake();o.title={log:log.splice(0),live:FS.live,kbLast};
FS={cur:'voices',live:false};fsWake();o.voices=log.splice(0);
FS={cur:'flags',live:false};fsWake();o.flags=log.splice(0);
MR={active:true};fsWake();o.stepping=log.splice(0);
console.log(JSON.stringify(o));
"""]))
    assert out["none"] is False
    assert out["title"] == {"log": ["blur", "focus:title"], "live": True, "kbLast": None}
    assert out["voices"] == ["focus:name", "clip"]
    assert out["flags"] == ["stepping"]
    assert out["stepping"] == [], "an active stepper is left alone"
    # the quiet mount: fsInit reads the walk's flag and clears it
    init = _js_fn("fsInit")
    assert "const quiet=RQUIET===base;" in init and "RQUIET=null;" in init
    assert "(toFinish(row)&&!quiet)" in init
    # the walk sets it, the default open and a lost meeting's default too
    assert "if(walk)RQUIET=id;" in _js_fn("selectRow")
    assert "RQUIET=id;" in _js_fn("readerSync") and "RQUIET=next;" in _js_fn("readerLeave")


def test_the_reader_keeps_its_menu_and_the_list_keeps_drawing_beside_it():
    """Two panes rebuild the list on every poll while a meeting is open. The
    header's ⋯ menu must not be re-anchored to (or closed for) the list row,
    the row flash reaches the list, and render() draws it beside the reader."""
    assert "pop.dataset.page='1';" in _js_fn("mMenu")
    assert "&&!rm.dataset.page){" in _js_fn("afterRender")
    assert "el.dataset.page='';" in _js_fn("openPop")
    body = _js_fn("render")
    assert "if(route&&route.view==='meeting'){maybeBuildPending();if(!PANES){applySel();return;}}" \
        in body
    assert "else if(PANES)readerSync();" in body
    ref = _js_fn("refresh")
    assert "if(!route||route.view!=='meeting'||PANES)flashRows(stampsMoved(prev,S));" in ref
    assert "readerKeep();" in ref
    # the route: the selection follows every hash change, after the mount
    route = _js_fn("applyRoute")
    assert route.index("enterMeeting(route.base)") < route.index("selFollow();")
    assert "else if(!readerSync())exitMeeting();" in route
    # a click on a row: two panes select into the reader; one column peeks
    click = NEW_JS[NEW_JS.index("$('#timeline').addEventListener('click'"):]
    click = click[:click.index("\n});")]
    assert "if(PANES){" in click and "readerOpen(el.dataset.id)" in click
    assert click.index("readerOpen") < click.index("toggleExpand(row.dataset.id)")
    # the title and date: the first click selects, a second edits in place
    assert "if(inList&&listPick(id))return;" in _js_fn("rowTitleEdit")
    assert "if(listPick(id))return;" in _js_fn("rowDateEdit")
    assert "const onPage=!inRow&&route.view==='meeting'&&route.base===id;" in _js_fn("rmRename")


def test_list_rows_are_denser_and_the_reader_title_is_22px():
    """docs/ui-refresh phase 3: list rows carry a 15px title, a 13px meta
    line, and ONE 13px summary line; the 17.5px row title moved to the
    reader header at 22px. The 13px floor holds (test_css_type_floor_is_13px)."""
    assert re.search(r"(?m)^\.rtitle\{font:600 15px/", NEW_CSS)
    assert re.search(r"(?m)^\.rmeta\{font:400 13px/", NEW_CSS)
    rs = re.search(r"(?m)^\.rsummary\{[^}]*\}", NEW_CSS).group(0)
    assert "font:400 13px/" in rs and "white-space:nowrap" in rs \
        and "text-overflow:ellipsis" in rs
    assert "line-clamp" not in rs, "one line, not two clamped ones"
    assert re.search(r"(?m)^\.lrow \.ltitle\{[^}]*font:600 15px", NEW_CSS)
    assert re.search(r"(?m)^\.lrow \.lmeta\{font:400 13px", NEW_CSS)
    assert re.search(r"(?m)^\.mtitle\{font:600 22px/", NEW_CSS)


def test_the_selected_row_wears_the_sel_token_in_both_themes():
    """--sel is the selected row's ground (accent-soft, lightened hue-true in
    light so --sub clears 4.5:1 on it) with a 3px inset accent edge: an edge
    marker, not a lift, so the no-shadow audit still passes."""
    from tools.token_contrast import BLOCKS, audit, read_tokens

    for label, pattern in BLOCKS:
        assert "sel" in read_tokens(NEW_CSS, pattern), f"{label} lacks --sel"
    m = re.search(r"(?m)^\.row\.sel,\.lrow\.sel\{[^}]*\}", NEW_CSS)
    assert m and "background:var(--sel)" in m.group(0)
    assert "box-shadow:inset 3px 0 0 var(--accent)" in m.group(0)
    measured = {(r.fg, r.bg) for r in audit()}
    assert {("ink", "sel"), ("sub", "sel"), ("attn", "sel")} <= measured
    assert all(r.ok for r in audit() if r.bg == "sel")
    # the painter keys the one selected row and says so to assistive tech
    paint = _js_fn("selPaint")
    assert "classList.add('sel')" in paint and "setAttribute('aria-current','true')" in paint


# ---------------------------------------------------------------------------
# Search chips and first run (docs/ui-refresh/proposal.md, phase 4): the
# search field and one row of filter chips at the top of the list, the "Said
# in" group, the setup check an empty library shows, and the ? key sheet.
# Pure functions and key handling run through node over the extracted
# source; placement is regex over the files.
# ---------------------------------------------------------------------------
def _chips_env():
    return "\n".join([
        _js_oneline("esc"), _js_const("CHIP_CATS"),
        _js_fn("chipLoad"), _js_fn("chipNext"), _js_fn("chipKeeps"),
        _js_fn("chipCounts"), _js_fn("chipList"), _js_fn("chipsHTML"), _js_fn("chipGo"),
        "const STORE={};let renders=0;function render(){renders++;}",
        "const localStorage={getItem:k=>(k in STORE?STORE[k]:null),"
        "setItem:(k,v)=>{STORE[k]=String(v);}};",
        "let LF=chipLoad(k=>localStorage.getItem(k));",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_chips_read_and_write_the_keys_the_selects_used():
    """The category and the grouping persist in stt_mcat and stt_msort, the
    keys the two selects wrote, so a reload keeps them. Has flags and Has
    voices last for the session, as the flagged-only filter did."""
    out = _run_node(_chips_env() + """
const o={};
o.fresh=LF;
o.saved=chipLoad(k=>({stt_mcat:'personal',stt_msort:'name'})[k]);
o.junk=chipLoad(k=>({stt_mcat:'bogus',stt_msort:'bogus'})[k]);
chipGo('work');chipGo('az');chipGo('flags');
o.after={LF,STORE:{...STORE},renders};
o.reload=chipLoad(k=>localStorage.getItem(k));
chipGo('work');chipGo('az');
o.cleared={...STORE};
console.log(JSON.stringify(o));
""")
    assert out["fresh"] == {"cat": "", "sort": "date", "flags": False, "voices": False}
    assert out["saved"] == {"cat": "personal", "sort": "name", "flags": False, "voices": False}
    assert out["junk"] == out["fresh"], "an unknown saved value falls back, never sticks"
    assert out["after"]["LF"] == {"cat": "work", "sort": "name", "flags": True, "voices": False}
    assert out["after"]["STORE"] == {"stt_mcat": "work", "stt_msort": "name"}
    assert out["after"]["renders"] == 3
    # a reload keeps the category and the grouping, not the session filters
    assert out["reload"] == {"cat": "work", "sort": "name", "flags": False, "voices": False}
    # clearing writes the selects' own defaults back
    assert out["cleared"] == {"stt_mcat": "", "stt_msort": "date"}


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_each_pressed_chip_clears_with_one_click_and_all_clears_the_filters():
    out = _run_node(_chips_env() + """
const base={cat:'',sort:'date',flags:false,voices:false};
const busy={cat:'work',sort:'name',flags:true,voices:true};
console.log(JSON.stringify({
  work:chipNext(base,'work'),
  workAgain:chipNext(chipNext(base,'work'),'work'),
  swap:chipNext(chipNext(base,'work'),'personal'),
  flags:chipNext(chipNext(base,'flags'),'flags'),
  voices:chipNext(base,'voices'),
  az:chipNext(base,'az'),
  azAgain:chipNext(chipNext(base,'az'),'az'),
  all:chipNext(busy,'all'),
  untouched:busy,
}));
""")
    assert out["work"]["cat"] == "work"
    assert out["workAgain"]["cat"] == "", "a pressed chip clears itself"
    assert out["swap"]["cat"] == "personal", "one category at a time, as the select was"
    assert out["flags"]["flags"] is False and out["voices"]["voices"] is True
    assert out["az"]["sort"] == "name" and out["azAgain"]["sort"] == "date"
    # All clears the three filters; the grouping is not a filter
    assert out["all"] == {"cat": "", "sort": "name", "flags": False, "voices": False}
    assert out["untouched"]["flags"] is True, "chipNext never mutates the state it reads"


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_chip_row_lists_all_the_categories_the_counts_and_a_to_z():
    out = _run_node(_chips_env() + """
const tl=[
  {id:'R',state:'recording'},
  {id:'N',state:'needs_name',review_substantial:4,finish:{voices:2}},
  {id:'A',state:'ready',category:'work',review_substantial:3,finish:{voices:1}},
  {id:'B',state:'ready',category:'personal',review_substantial:0,finish:{voices:2}},
  {id:'C',state:'ready',review_substantial:2,finish:{voices:0}},
  {id:'D',state:'ready'},
];
const st={cat:'work',sort:'name',flags:true,voices:false};
const keys=l=>l.map(c=>c.key+(c.on?'*':'')+(c.n!=null?'='+c.n:''));
const keep=f=>tl.filter(r=>chipKeeps(r,f)).map(r=>r.id);
console.log(JSON.stringify({
  counts:chipCounts(tl),
  idle:keys(chipList({cat:'',sort:'date',flags:false,voices:false},chipCounts(tl))),
  busy:keys(chipList(st,chipCounts(tl))),
  zero:keys(chipList({cat:'',sort:'date',flags:false,voices:false},{flags:0,voices:0})),
  pressedAtZero:keys(chipList({cat:'',sort:'date',flags:true,voices:true},{flags:0,voices:0})),
  html:chipsHTML(chipList(st,chipCounts(tl))),
  keepAll:keep({cat:'',flags:false,voices:false}),
  keepWork:keep({cat:'work',flags:false,voices:false}),
  keepFlags:keep({cat:'',flags:true,voices:false}),
  keepVoices:keep({cat:'',flags:false,voices:true}),
  keepBoth:keep({cat:'personal',flags:true,voices:false}),
}));
""")
    # the counts are the ready meetings each chip keeps
    assert out["counts"] == {"flags": 2, "voices": 2}
    assert out["idle"] == ["all*", "work", "personal", "flags=2", "voices=2", "az"]
    assert out["busy"] == ["all", "work*", "personal", "flags*=2", "voices=2", "az*"]
    # a count chip with nothing to keep steps aside, unless it is pressed
    assert out["zero"] == ["all*", "work", "personal", "az"]
    assert out["pressedAtZero"] == ["all", "work", "personal", "flags*=0", "voices*=0", "az"]
    h = out["html"]
    assert ('<button class="fchip" type="button" data-k="flags" aria-pressed="true"'
            in h)
    assert "onclick=\"chipGo('flags')\">Has flags <span class=\"mono\">2</span>" in h
    # a pressed chip wears the quiet x; All never does
    assert h.count('class="fx"') == 3
    assert '<button class="fchip az" type="button" data-k="az" aria-pressed="true"' in h
    assert ">A&#8211;Z " in h
    # the chips filter ready meetings only: Now and a title waiting stay
    assert out["keepAll"] == ["R", "N", "A", "B", "C", "D"]
    assert out["keepWork"] == ["R", "N", "A"]
    assert out["keepFlags"] == ["R", "N", "A", "C"]
    assert out["keepVoices"] == ["R", "N", "A", "B"]
    assert out["keepBoth"] == ["R", "N"]


def test_the_search_and_the_chips_sit_at_the_top_of_the_list():
    """The search field left the header for the top of the list pane, and the
    chip row replaced the two selects and the flagged-only chip."""
    hdr = NEW_PAGE[NEW_PAGE.index('<header class="hdr">'):NEW_PAGE.index("</header>")]
    assert 'id="search"' not in hdr
    wrap = NEW_PAGE[NEW_PAGE.index('<main class="wrap">'):NEW_PAGE.index("</main>")]
    assert wrap.index('id="search"') < wrap.index('id="fchips"') < wrap.index('id="timeline"')
    assert 'role="group" aria-label="Filter the list"' in wrap
    # the chip row redraws with every poll, only when a chip or count moved
    assert "drawChips(S);" in _js_fn("render")
    draw = _js_fn("drawChips")
    assert "if(box.dataset.sig===html)return;" in draw
    assert "b.focus({preventScroll:true})" in draw, "a focused chip keeps its focus"
    # the chip styles: 4px, 13px, the accent pair when pressed; the count is
    # mono through the .mono class, never a rule of its own
    fchip = re.search(r"(?m)^\.fchip\{[^}]*\}", NEW_CSS).group(0)
    assert "border-radius:4px" in fchip and "font:500 13px/" in fchip
    assert re.search(r'(?m)^\.fchip\[aria-pressed="true"\]\{color:var\(--accent-text\);'
                     r'background:var\(--accent-soft\)', NEW_CSS)
    assert "box-shadow" not in fchip


def _said_env():
    return "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"),
        _js_fn("secHeadHTML"), _js_fn("saidGroups"), _js_fn("hitClock"), _js_fn("saidInHTML"),
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_said_in_groups_the_hits_by_meeting_with_mono_timestamps():
    out = _run_node(_said_env() + """
const hits=[
  {base:'B1',start:754.2,who:'Grace',snippet:'the budget line for coaches'},
  {base:'A1',start:65,who:'',snippet:'a budget question'},
  {base:'B1',start:3725,who:'Tom',snippet:'Budget again'},
  {base:'GONE',start:5,who:'X',snippet:'budget in an unknown meeting'},
  {base:'HID',start:9,who:'Y',snippet:'budget in a filtered meeting'},
];
const titles={B1:'Budget Review',A1:'Literacy pilot',HID:null};
const titleOf=b=>(b in titles?titles[b]:b);
const g=saidGroups(hits,titleOf);
console.log(JSON.stringify({
  groups:g.map(x=>[x.base,x.title,x.hits.map(h=>h.start)]),
  html:saidInHTML(g,'budget',null),
  capped:saidInHTML(g,'budget',[40,212]),
  none:saidInHTML([],'budget',null),
  clocks:[0,59.9,60,754.2,3725].map(hitClock),
}));
""")
    # grouped by meeting in the server's order; a filtered meeting drops its
    # lines, an unknown one keeps them under its folder name
    assert out["groups"] == [["B1", "Budget Review", [754.2, 3725]],
                             ["A1", "Literacy pilot", [65]],
                             ["GONE", "GONE", [5]]]
    h = out["html"]
    # the house eyebrow, with the line count in mono, heads the group
    assert h.startswith('<div class="mgroup sechead" id="sec-said"><span class="glabel">Said in'
                        '</span><span class="grule" aria-hidden="true"></span>'
                        '<span class="gcount mono">4</span></div>')
    assert h.index('<div class="said-t">Budget Review</div>') \
        < h.index('<div class="said-t">Literacy pilot</div>')
    # a line is a button that opens the meeting at its moment (openHit, then
    # mSeek); the timestamp is mono, the match marked, the speaker named
    assert ('<button class="hit" type="button" title="Open the meeting at this moment"'
            ' onclick="openHit(\'B1\',754.2)"><span class="hit-t mono">12:34</span>') in h
    assert '<span class="hit-w">Grace</span> the <mark>budget</mark> line' in h
    assert "<mark>Budget</mark> again" in h, "the match is marked case-blind"
    assert '<span class="hit-t mono">1:05</span><span class="hit-x">a <mark>budget</mark>' in h
    assert "said-more" not in h
    assert ('<p class="said-more">The newest 40 of 212 lines. Add a word to narrow them.</p>'
            in out["capped"])
    # a section with nothing in it renders nothing
    assert out["none"] == ""
    assert out["clocks"] == ["0:00", "0:59", "1:00", "12:34", "62:05"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_said_in_heads_the_list_and_only_an_empty_search_reads_no_matches():
    """The group is the list's first section, above Now. A query whose words
    were said but match no title shows the group, not "No matches."; a stale
    answer for an earlier query shows nothing; the chips filter the lines."""
    stall = ("{kind:'recorder_stall',title:'Recording',detail:'no audio',"
             "target:'rec:x',count:1}")
    out = _run_node(_timeline_env(search="coach") + """
const o={};
const tl=[{id:'P',state:'processing',when:'2026-09-27T09:00:00'},
  {id:'W1',state:'ready',title:'Budget Review',category:'work',date:'2026-09-01',when:'2026-09-01T09:00:00',finish:fin(true,0,0)},
  {id:'P1',state:'ready',title:'Dentist',category:'personal',date:'2026-08-01',when:'2026-08-01T09:00:00',finish:fin(true,0,0)}];
HITS={query:'coach',total:3,hits:[{base:'W1',start:30,who:'Grace',snippet:'the coach said'},
  {base:'P1',start:40,who:'',snippet:'coach at noon'},{base:'W1',start:90,who:'',snippet:'coach two'}]};
drawTimeline({timeline:tl,tray:[""" + stall + """]});
o.said=order(html());o.top=SAID_TOP;o.h=html();
LF.cat='work';els['#timeline'].dataset.sig='';
drawTimeline({timeline:tl,tray:[]});
o.work=html();
LF.cat='';HITS={query:'coa',total:1,hits:[{base:'W1',start:1,who:'',snippet:'coa'}]};
els['#timeline'].dataset.sig='';
drawTimeline({timeline:tl,tray:[]});
o.stale=html();o.staleTop=SAID_TOP;
HITS={query:'coach',total:0,hits:[]};els['#timeline'].dataset.sig='';
drawTimeline({timeline:tl,tray:[]});
o.nothing=html();
console.log(JSON.stringify(o));
""")
    # Said in first, then Now (the stall row, the live row); no title matched,
    # yet the words were said, so no "No matches."
    assert out["said"][:2] == ['"sec-said"', '"sec-now"']
    assert "No matches." not in out["h"]
    assert out["top"] == {"base": "W1", "start": 30, "who": "Grace", "snippet": "the coach said"}
    # the Work chip drops the personal meeting's line from the group
    assert "openHit('W1',30)" in out["work"] and "openHit('P1',40)" not in out["work"]
    assert '<span class="gcount mono">2</span>' in out["work"]
    # an answer for an earlier query never paints under the new one
    assert "sec-said" not in out["stale"] and out["staleTop"] is None
    assert "No matches." in out["stale"]
    # a search with no line said and no title reads "No matches."
    assert "sec-said" not in out["nothing"] and "No matches." in out["nothing"]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_enter_in_the_search_field_opens_the_first_said_in_line():
    out = _run_node(_layer_env() + """
const o={};
document.activeElement=se;
o.none=press(kbKey,'Enter');
SAID_TOP={base:'B1',start:754.2};
o.top=press(kbKey,'Enter');
o.typed=press(kbKey,'j');
console.log(JSON.stringify(o));
""")
    assert out["none"] == {"log": [], "prevented": False}
    assert out["top"] == {"log": ["hit:B1@754.2"], "prevented": True}
    assert out["typed"] == {"log": [], "prevented": False}, "j in the field is a character"
    # the scheduler feeds HITS and redraws; the stale-answer guard stays
    sched = _js_fn("scheduleSearch")
    assert "HITS=r;render();" in sched
    assert "$('#search').value.trim().toLowerCase()!==r.query" in sched
    assert "if(q.length<3){if(HITS){HITS=null;render();}return;}" in sched


def _setup_env():
    return "\n".join([_js_oneline("esc"), _js_fn("libraryEmpty"), _js_fn("setupCheck"),
                      _js_fn("setupHTML")])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_setup_check_reads_three_lines_from_the_state():
    """Recorder ready (recorder_ready), watched folder set (paths.source), and
    your own voice enrolled (mic_speaker against enrolled), each with the
    drawer button that fixes it. The voice line has three states: not set,
    set but not enrolled, enrolled."""
    out = _run_node(_setup_env() + """
const line=it=>[it.key,it.ok,it.label,it.detail,it.btn,it.go];
console.log(JSON.stringify({
  bare:setupCheck({}).map(line),
  unset:setupCheck({recorder_ready:false,paths:{source:''},mic_speaker:null,enrolled:[]}).map(line),
  notEnrolled:setupCheck({recorder_ready:true,paths:{source:'/Users/mark/Recordings'},
    mic_speaker:'Mark Paik',enrolled:[{name:'Grace Lindqvist'}]}).map(line),
  ready:setupCheck({recorder_ready:true,paths:{source:'/Volumes/X/in'},
    mic_speaker:'Mark Paik',enrolled:[{name:'Mark Paik'}]}).map(line),
  html:setupHTML(setupCheck({recorder_ready:true,paths:{source:'/Users/mark/in'},mic_speaker:null})),
  empty:[libraryEmpty([]),libraryEmpty([{state:'processing'},{state:'failed'}]),
    libraryEmpty([{state:'needs_name'}]),libraryEmpty([{state:'ready'}])],
}));
""")
    assert out["bare"] == out["unset"], "missing keys read as not ready, never throw"
    rec, folder, voice = out["unset"]
    assert rec == ["recorder", False, "Recorder not installed",
                   "Build it with ./setup.sh, then record from the menu bar.",
                   "Recorder settings", ["settings", "dg-recorder"]]
    assert folder[:3] == ["folder", False, "No watched folder"]
    assert folder[4:] == ["Folder settings", ["settings", "drow-source"]]
    assert voice == ["voice", False, "Your voice is not set",
                     "Pick your name, so recorded calls separate you from the others.",
                     "Pick your voice", ["settings", "dg-recorder"]]
    rec, folder, voice = out["notEnrolled"]
    assert rec[:3] == ["recorder", True, "Recorder ready"]
    assert folder[:4] == ["folder", True, "Watched folder set", "~/Recordings"]
    assert voice == ["voice", False, "Your voice is not enrolled yet",
                     "Name Mark Paik in your first meeting to enroll the voice.",
                     "Speakers", ["speakers", ""]]
    rec, folder, voice = out["ready"]
    assert folder[3] == "/Volumes/X/in"
    assert voice == ["voice", True, "Your voice is enrolled", "Mark Paik",
                     "Speakers", ["speakers", ""]]
    h = out["html"]
    assert h.startswith('<section class="setup" aria-label="Setup check">')
    assert h.count('class="setrow') == 3 and h.count('class="setrow ok"') == 2
    assert "onclick=\"openDrawerAt('settings','dg-recorder')\">Recorder settings</button>" in h
    assert "onclick=\"openDrawerAt('settings','drow-source')\">Folder settings</button>" in h
    assert h.endswith('<p class="setdrop">Record from the menu bar, or drop an audio file'
                      ' here.</p></section>')
    # a file in Now is not a meeting: the library is empty until one lands
    assert out["empty"] == [True, True, False, False]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_an_empty_library_shows_the_setup_check_in_the_list():
    out = _run_node(_timeline_env() + """
const o={};
const st={recorder_ready:true,paths:{source:'/x'},mic_speaker:null,enrolled:[],tray:[]};
drawTimeline({...st,timeline:[]});
o.empty=html();
els['#timeline'].dataset.sig='';
drawTimeline({...st,timeline:[{id:'P',state:'processing',when:'2026-09-27T09:00:00'}]});
o.nowOnly=order(html());o.nowHtml=html();
const s0=els['#timeline'].dataset.sig;
drawTimeline({...st,recorder_ready:false,timeline:[{id:'P',state:'processing',when:'2026-09-27T09:00:00'}]});
o.moved=els['#timeline'].dataset.sig!==s0;
els['#timeline'].dataset.sig='';
drawTimeline({...st,timeline:[{id:'D',state:'ready',date:'2026-09-01',when:'2026-09-01T09:00:00',finish:fin(true,0,0)}]});
o.library=html();
console.log(JSON.stringify(o));
""")
    assert 'class="setup"' in out["empty"] and "No matches." not in out["empty"]
    # a file processing on the first run: Now, then the check under it
    assert out["nowOnly"][:2] == ['"sec-now"', '"P"']
    assert out["nowHtml"].index('id="sec-now"') < out["nowHtml"].index('class="setup"')
    assert out["moved"], "a line turning ready or not must rebuild the list"
    assert "setup" not in out["library"]


def test_the_setup_buttons_open_the_drawer_at_the_row_that_fixes_them():
    at = _js_fn("openDrawerAt")
    assert "openDrawer(section);" in at and "el.scrollIntoView({block:'start'});" in at
    assert "c.focus({preventScroll:true})" in at
    # the anchors the check names exist in the drawer's settings
    settings = _js_fn("dSettingsHTML")
    assert '<div class="dgroup" id="dg-recorder">Recorder</div>' in settings
    assert "'drow-source');" in settings
    assert "id=\"${id}\"" in _js_fn("_drow")
    # a setup mark is a circle in the house pairs: teal asks, green is done
    mark = re.search(r"(?m)^\.setmark\{[^}]*\}", NEW_CSS).group(0)
    assert "color:var(--attn);background:var(--attn-soft)" in mark
    assert ".setrow.ok .setmark{color:var(--ok);background:var(--ok-soft)}" in NEW_CSS


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_key_sheet_opens_on_question_mark_and_never_while_typing():
    out = _run_node(_layer_env() + """
const o={};
const k=(key,mods)=>Object.assign({key,metaKey:false,ctrlKey:false,altKey:false},mods||{});
o.guard={
  free:keySheetKey(k('?'),false,false),
  typing:keySheetKey(k('?'),true,false),
  layer:keySheetKey(k('?'),false,true),
  cmd:keySheetKey(k('?',{metaKey:true}),false,false),
  ctrl:keySheetKey(k('?',{ctrlKey:true}),false,false),
  alt:keySheetKey(k('?',{altKey:true}),false,false),
  slash:keySheetKey(k('/'),false,false),
};
document.activeElement=se;o.inSearch=press(kbKey,'?');
document.activeElement={tagName:'TEXTAREA'};o.inTextarea=press(kbKey,'?');
document.activeElement={tagName:'DIV',isContentEditable:true};o.inEditable=press(kbKey,'?');
document.activeElement=body;
NP={uid:'U1'};o.underPanel=press(kbKey,'?');NP=null;
DRAWER.open=true;o.underDrawer=press(kbKey,'?');DRAWER.open=false;
_popClose=()=>{};o.underPopover=press(kbKey,'?');_popClose=null;
o.open=press(kbKey,'?');o.isOpen=KSHEET;
// open, it owns every key: j stays put, Tab stays on its control, the
// meeting page's keys stand back, Escape closes it
o.j=press(kbKey,'j');o.tab=press(kbKey,'Tab');
route={view:'meeting',base:'M1'};MR={active:true,items:[],i:0};
o.pageN=press(mKey,'n');o.pageA=press(mKey,'a');
o.esc=press(kbKey,'Escape');o.closed=KSHEET;
o.again=press(kbKey,'?');o.q=press(kbKey,'?');o.closedByQ=KSHEET;
console.log(JSON.stringify(o));
""")
    assert out["guard"] == {"free": True, "typing": False, "layer": False, "cmd": False,
                            "ctrl": False, "alt": False, "slash": False}
    quiet = {"log": [], "prevented": False}
    for where in ("inSearch", "inTextarea", "inEditable", "underPanel", "underDrawer",
                  "underPopover"):
        assert out[where] == quiet, f"? opened the sheet {where}: {out[where]}"
    assert out["open"] == {"log": ["keys"], "prevented": True} and out["isOpen"] is True
    assert out["j"] == {"log": [], "prevented": False}
    assert out["tab"] == {"log": [], "prevented": True}
    assert out["pageN"] == quiet and out["pageA"] == quiet
    assert out["esc"] == {"log": ["keys:close"], "prevented": True} and out["closed"] is False
    # from the meeting page too, and ? again closes it
    assert out["again"] == {"log": ["keys"], "prevented": True}
    assert out["q"] == {"log": ["keys:close"], "prevented": True} and out["closedByQ"] is False


def test_the_key_sheet_lists_the_keys_phases_one_to_three_shipped():
    sheet = NEW_PAGE[NEW_PAGE.index('<aside id="keysheet"'):]
    sheet = sheet[:sheet.index("</aside>")]
    keys = re.findall(r"<dt>(.*?)</dt>", sheet)
    caps = [re.findall(r"<kbd>(.*?)</kbd>", k) for k in keys]
    assert caps == [["/"], ["j", "k"], ["x"], ["Enter"], ["Space"], ["1", "3"], ["s"],
                    ["a", "u", "n", "p"], ["[", "]"], ["Esc"]]
    # a card over the drawer's veil; a click outside closes it
    assert '<div id="keyveil" class="dveil" hidden onclick="keySheetClose()"></div>' in NEW_PAGE
    assert 'class="popover keysheet" aria-labelledby="kstitle" hidden' in sheet
    # its caps are sans on the inset well, never mono (mono is true data)
    kbd = re.search(r"(?m)^\.kslist kbd\{[^}]*\}", NEW_CSS).group(0)
    assert "var(--sans)" in kbd and "var(--mono)" not in kbd
    # the sheet is the one place key caps show; it opens only on ?
    assert "keySheetOpen();" in _kb_body()
    assert NEW_JS.count("keySheetOpen();") == 1


# ---------------------------------------------------------------------------
# UI follow-ups (2026-09-28): the four problems Mark's real library showed and
# the demo data never did. A board recording's split voice, a queue of long
# recordings in Now, 22 raw labels on a To finish row, and the pill.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_strip_counts_a_split_voice_once_as_the_row_does(running_server):
    """The strip read "Voices 22" while the To finish row read "Voices 21" on
    the same board meeting: fsVoicesFrom made one voice per CLUSTER, and two
    clusters carry the one global id U068 ("Speaker 68"). Naming that id
    names both, so they are one voice. The meeting, its speaker options, and
    the unknown registry come from the server itself (/api/transcript and
    /api/state over tests/conftest.py's split_voice_meeting), and the strip's
    count is FS.fin.voices after the transcript loads."""
    split_voice_meeting()
    from urllib.parse import quote
    _, _, body = _get(running_server, "/api/transcript?base=" + quote(SPLIT_VOICE_BASE))
    tr = json.loads(body)
    _, _, body = _get(running_server, "/api/state")
    st = json.loads(body)
    row = next(r for r in st["timeline"] if r["id"] == SPLIT_VOICE_BASE)
    out = _run_node("\n".join([
        _js_fn("mUnknownUid"), _js_fn("fsVoicesFrom"), _js_fn("fsVoiceOrder"),
        _js_oneline("_fsOpenVoices"), _js_fn("_fsSubstantial"), _js_fn("fsSegsLoaded"),
        "function mIsMinor(){return false;}function fsSync(){}function reviewStart(){}",
        "let S=" + json.dumps({"unknowns": st["unknowns"]}) + ";",
        "let MR=null,MP=" + json.dumps({"base": SPLIT_VOICE_BASE,
                                         "spkOpts": tr["speaker_options"],
                                         "segs": tr["segments"]}) + ";",
        "let FS={base:MP.base,voices:null,mode:null,past:[],first:true,hand:false,"
        "fin:" + json.dumps({"title": True, "voices": row["finish"]["voices"],
                             "flags": 0}) + "};",
        "const before=FS.fin.voices;fsSegsLoaded();",
        "const split=FS.voices.find(v=>v.uid===" + json.dumps(SPLIT_VOICE_UID) + ");",
        "console.log(JSON.stringify({before,strip:FS.fin.voices,voices:FS.voices.length,",
        "  clusters:FS.voices.reduce((a,v)=>a+(v.ids||[v.id]).length,0),split:split.ids||null,",
        "  display:split.display,",
        "  queued:fsVoiceOrder(FS.voices,MP.segs,[],[]).length}));",
    ]))
    assert len(tr["speaker_options"]) == 22
    assert row["finish"]["voices"] == 21
    # the strip, after the transcript loaded, agrees with the row it came from
    assert out["before"] == out["strip"] == 21
    assert out["voices"] == 21 and out["clusters"] == 22
    assert out["split"] == ["SPEAKER_00", "SPEAKER_01"]
    assert out["display"] == "Speaker 68"
    assert out["queued"] == 21


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_split_voice_collapses_by_uid_else_by_display():
    """fsVoicesFrom keys a voice by its registry uid, or with no uid by its
    display. A named, a dismissed, and a raw-id cluster never ask."""
    out = _run_node("\n".join([
        _js_fn("mUnknownUid"), _js_fn("fsVoicesFrom"),
        "let MP={base:'M'};",
        "let S={unknowns:[{uid:'U068',display:'Speaker 68',meetings:['M']},",
        "  {uid:'U070',display:'Speaker 70',meetings:['OTHER']}]};",
        "const o=(id,display,x)=>Object.assign({id,display,named:false,dismissed:false},x||{});",
        "const v=fsVoicesFrom([o('A','Speaker 68'),o('B','Voice 2'),o('C','Speaker 68'),",
        "  o('D','Speaker 70'),o('E','Speaker 70'),o('F','Grace',{named:true}),",
        "  o('G','Voice 2',{dismissed:true}),o('SPEAKER_09','SPEAKER_09'),o('H','Voice 3')]);",
        "console.log(JSON.stringify(v.map(x=>[x.display,x.uid,x.ids,x.id])));",
    ]))
    assert out == [
        ["Speaker 68", "U068", ["A", "C"], "A"],
        ["Voice 2", None, ["B"], "B"],                 # G dismissed: never asks
        ["Speaker 70", None, ["D", "E"], "D"],         # U070 not heard here: by display
        ["Voice 3", None, ["H"], "H"],
    ]


def _split_voice_step_env():
    return "\n".join([
        _js_oneline("esc"), _js_fn("mFmtT"), _js_fn("fvToken"), _js_fn("fvLines"),
        _js_fn("fvBusy"), _js_fn("fvForget"), _js_oneline("_fsOpenVoices"),
        "let NPBUSY=false;",
        "const els={'#fvclip':{innerHTML:''},'#fvsaid':{innerHTML:''},'#fverr':{hidden:true}};",
        "function $(q){return els[q]||null;}",
        "let MP={base:'M',hasAudio:true},plays=0;function fvPlay(){plays++;}",
        "function refresh(){}function mReloadSegs(){}function fvNext(){}function fsStripSync(){}",
        "let FS={base:'M',vi:0,live:true,cur:'voices',fin:{voices:1},",
        "  voices:[{id:'A',ids:['A','C'],display:'Speaker 68',uid:null,st:''}]};",
        "const LINES={A:[{start:40,dur:3,text:'short one'}],",
        "  C:[{start:900,dur:12.5,text:'the long turn'},{start:10,dur:1,text:'yes'}]};",
        "let posted=[];",
        "function api(p,b){posted.push(b?[p,b]:[p]);",
        "  const m=/speaker=([^&]*)/.exec(p);",
        "  return Promise.resolve(m?{lines:LINES[decodeURIComponent(m[1])]}:{ok:true});}",
    ])


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_split_voice_plays_and_quotes_from_all_its_clusters():
    """The Voices step's clip and its "said" lines draw from every cluster
    of the voice: the longest turn here sits in the second cluster, and the
    lines of both clusters merge. "Not a real speaker" dismisses every
    cluster, or the one left over keeps the voice counted."""
    out = _run_node(_split_voice_step_env() + """
(async()=>{
  await fvLines(FS.voices[0],fvToken());
  const fetched=posted.map(x=>x[0]);
  const clip=FS.clip,said=els['#fvsaid'].innerHTML;
  posted=[];
  await fvForget();
  const dismissed=posted.filter(x=>x[0]==='/api/dismiss_voice').map(x=>x[1].speaker);
  FS.voices=[{id:'A',ids:['A','C'],display:'Speaker 68',uid:'U068',st:''}];FS.vi=0;
  posted=[];
  await fvForget();
  const byUid=posted.map(x=>[x[0],x[1].uid||x[1].speaker]);
  console.log(JSON.stringify({fetched,clip,said,plays,dismissed,byUid}));
})();
""")
    assert out["fetched"] == ["/api/voice_lines?base=M&speaker=A",
                              "/api/voice_lines?base=M&speaker=C"]
    # the longest turn of the whole voice, which only the second cluster had
    assert out["clip"] == {"start": 900, "end": 912.5}
    assert out["plays"] == 1
    said = out["said"]
    assert "the long turn" in said and "short one" in said and "yes" in said
    # in reading order, as before: 0:10, 0:40, 15:00
    assert said.index("yes") < said.index("short one") < said.index("the long turn")
    assert out["dismissed"] == ["A", "C"]
    assert out["byUid"] == [["/api/forget", "U068"], ["/api/dismiss_voice", "A"],
                            ["/api/dismiss_voice", "C"]]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_to_finish_row_names_three_people_and_counts_the_unnamed():
    """A board meeting's To finish row listed 22 labels ("Speaker 77,
    Speaker 70, Voice 5, ..."). The meta line reads: the duration, up to
    three named people (the server's `named`, most talk first), "+N" for
    the rest, then "N unnamed" in the teal attention colour, the same
    finish.voices the Voices dot counts. A raw label never appears."""
    raw = [f"Speaker {n}" for n in range(60, 78)] + [f"Voice {n}" for n in range(1, 5)]
    out = _run_node(_sections_env() + """
const RAW=""" + json.dumps(raw) + """;
const row=(named,voices,x)=>finishRowHTML(Object.assign({id:'B',state:'ready',title:'Board',
  date:'2026-09-23',minutes:214,speakers:RAW.concat(named),named,
  finish:fin(true,voices,0)},x||{}));
console.log(JSON.stringify({
  board:row(['Ana Ortiz','Ben Cole','Cy Diaz','Dee Park','Eli Moss'],21),
  three:row(['Ana Ortiz','Ben Cole','Cy Diaz'],0),
  none:row([],21),
  bare:row([],0),
  legacy:row(undefined,2),
  waiting:row(['Ana Ortiz'],1,{state:'needs_name',suggested_title:'Board'}),
}));
""")
    meta = {k: re.search(r'<span class="lmeta">(.*?)</span>(?=<span class="steps">)', v)
            for k, v in out.items()}
    got = {k: (m.group(1) if m else None) for k, m in meta.items()}
    assert got["board"] == ('214 min &middot; Ana Ortiz, Ben Cole, Cy Diaz +2 &middot; '
                            '<span class="unn">21 unnamed</span>')
    assert got["three"] == "214 min &middot; Ana Ortiz, Ben Cole, Cy Diaz"
    assert got["none"] == '214 min &middot; <span class="unn">21 unnamed</span>'
    assert got["bare"] == "214 min"
    # a row from before the server sent `named` falls back to no people at
    # all, never to the raw roster
    assert got["legacy"] == '214 min &middot; <span class="unn">2 unnamed</span>'
    assert got["waiting"] == ('214 min &middot; Ana Ortiz &middot; '
                              '<span class="unn">1 unnamed</span>')
    for k, html in out.items():
        assert "Speaker " not in html and "Voice " not in html, k
    # the count wears the attention teal (measured on the card and on --sel)
    assert re.search(r"(?m)^\.lrow \.unn\{[^}]*color:var\(--attn\)", NEW_CSS)


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_waiting_or_held_file_is_one_compact_line():
    """Five multi-hour board recordings, each a full-height row, pushed To
    finish below the fold at 1440x1000. A waiting or held file is one line:
    the file name, then its size and estimate in mono at the right, and the
    hover actions it always had. Recording, processing, and failed rows keep
    their own markup."""
    out = _run_node("\n".join([
        _js_oneline("esc"), _js_oneline("escJs"), _js_fn("dupNote"),
        _js_region("function fmtEta(", "const STAGE_NICE"),
        "function clock(){return '0:00';}const STAGE_NICE={diarizing:'Speakers'};",
        "function catDot(){return '';}function shortDate(d){return d;}",
        "function meetingByBase(){return null;}function finishRowHTML(){return '';}",
        "const OPEN=new Set();",
        _js_fn("slotActions"), _js_fn("gutter"), _js_fn("queueName"), _js_fn("queueBodyAndSlot"),
        _js_fn("bodyAndSlot"), _js_fn("rowHTML"),
        """
const w=rowHTML({state:'waiting',id:'src:Board 0923.mp4',title:'Board 0923.mp4',
  size_mb:812.4,est_minutes:2515,est_detail:'transcribe ~1540m \\u00b7 speakers ~975m'});
const h=rowHTML({state:'held',id:'src:Budget.m4a',title:'Budget.m4a',size_mb:40.2,est_minutes:9});
const bare=rowHTML({state:'waiting',id:'src:x.m4a',title:'x.m4a'});
const dup=rowHTML({state:'waiting',id:'src:y.m4a',title:'y.m4a',size_mb:3,est_minutes:2,
  dup_of:'Board 09232026',dup_title:'Board',dup_reason:'identical'});
const p=rowHTML({state:'processing',id:'P',title:'P',stage:'diarizing',pct:40,eta:600});
const f=rowHTML({state:'failed',id:'src:f.m4a',title:'f.m4a',error:'boom'});
const long=queueName('Zoom recording_2026-09-10_board-session.mp4');
console.log(JSON.stringify({w,h,bare,dup,p,f,long}));
"""]))
    w, h = out["w"], out["h"]
    assert w.startswith('<div class="row qrow" data-state="waiting"')
    # one line: the name, and the mono size and estimate as the rail's state
    assert '<div class="rtitle" title="Board 0923.mp4&#10;812.4 MB&#10;transcribe ~1540m' in w
    # the head takes the ellipsis, the last word (the date) stays whole
    assert ('<span class="qhead">Board</span><span class="qtail">&nbsp;0923.mp4</span>'
            '</div>') in w
    assert '<span class="qhead">Budget.m4a</span></div>' in h
    # a long last word is no date: the whole name takes the one ellipsis
    assert out["long"] == '<span class="qhead">Zoom recording_2026-09-10_board-session.mp4</span>'
    assert ('<span class="rstate qinfo yields"><span class="mono"><span class="qsize">'
            '812.4 MB &middot; </span>&#8776;41h 55m</span></span>') in w
    assert "rmeta" not in w and "spill" not in w
    # the hover actions it always had
    for a in ("rowListen('src:Board 0923.mp4')", "rowHold('src:Board 0923.mp4')",
              "rowProcess('src:Board 0923.mp4')", "rowDelete('src:Board 0923.mp4',event)"):
        assert a in w, a
    assert h.startswith('<div class="row qrow" data-state="held"')
    assert ('<span class="rstate qinfo yields"><span class="qheld">&#10073;&#10073; held</span> '
            '<span class="mono"><span class="qsize">40.2 MB &middot; </span>&#8776;9 min'
            '</span></span>') in h
    # the size rides the tooltip too: the narrow list pane shows the estimate alone
    assert 'title="Budget.m4a&#10;40.2 MB&#10;Held: automatic runs skip' in h
    assert re.search(r"(?m)^body\.panes\.route-meeting \.row\.qrow \.qsize\{display:none\}",
                     NEW_CSS)
    assert "rowRelease('src:Budget.m4a')" in h and "rowHold" not in h
    # what held means moved from a second line to the name's tooltip
    assert "Held: automatic runs skip this until you release it" in h
    assert "rmeta" not in h
    assert '<span class="rstate qinfo yields">waiting</span>' in out["bare"]
    # a duplicate still says so, on the same line
    assert "already processed" in out["dup"] and 'class="rmeta dupnote"' in out["dup"]
    # the live and failed rows are untouched
    for k in ("p", "f"):
        assert "qrow" not in out[k] and "spill" in out[k], k
    # the line's CSS: compact, the name and a duplicate's note side by side
    assert re.search(r"(?m)^\.row\.qrow\{min-height:0;", NEW_CSS)
    assert re.search(r"(?m)^\.row\.qrow \.rbody\{flex-direction:row", NEW_CSS)
    assert ".estdetail" not in NEW_CSS


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_past_three_queued_files_the_rest_fold_under_one_line():
    """With five long recordings waiting, Now shows the first three and one
    line, "2 more waiting · ≈Xh", which a click opens. The fold persists as
    the accordion's do (stt_groups, id q-more, folded by default), a search
    forces it open, and three files get no fold line at all."""
    mk = ("const q=(id,state,est)=>({id,title:id,state,est_minutes:est,"
          "when:'2026-09-'+(29-id.slice(-1))+'T08:00:00'});"
          "const LIVE=[{id:'R',state:'recording',when:'2026-09-28T09:00:00'},"
          "{id:'P',state:'processing',when:'2026-09-28T08:00:00'},"
          "{id:'F',state:'failed',when:'2026-09-01T08:00:00'}];")
    out = _run_node(_timeline_env() + mk + """
const five=LIVE.concat([q('H1','held',30),q('W2','waiting',2515),q('W3','waiting',600),
  q('W4','waiting',61),q('W5','waiting',20)]);
drawTimeline({timeline:five,tray:[]});
const folded=order(html()),line=html().match(/<button class="ghead qmore"[\\s\\S]*?<\\/button>/)[0];
const defaultOpen=grpDefaultOpen('q-more');
grpToggle('q-more');
const stored=GRP['q-more'];
drawTimeline({timeline:five,tray:[]});
const opened=order(html()),openLine=html().match(/<button class="ghead qmore"[\\s\\S]*?<\\/button>/)[0];
grpToggle('q-more');drawTimeline({timeline:five,tray:[]});
const refolded=order(html());
els['#search'].value='W';els['#timeline'].dataset.sig='';
drawTimeline({timeline:five,tray:[]});
const searched=order(html());
els['#search'].value='';els['#timeline'].dataset.sig='';
const three=LIVE.concat([q('W2','waiting',30),q('W3','waiting',30),q('W4','waiting',30)]);
drawTimeline({timeline:three,tray:[]});
const noFold=html().includes('qmore');
console.log(JSON.stringify({folded,line,defaultOpen,stored,opened,openLine,refolded,searched,noFold}));
""")
    # the live rows stay whole and ahead; three queue rows, then the line
    assert out["folded"] == ['"sec-now"', '"R"', '"P"', '"F"', '"H1"', '"W2"', '"W3"',
                             '"ghead qmore"']
    line = out["line"]
    assert 'id="qmore"' in line and 'aria-expanded="false"' in line
    assert "onclick=\"grpToggle('q-more')\"" in line
    # the count and the two hidden files' summed estimate (61 + 20 min)
    assert ('<span class="glabel"><span class="mono">2</span> more waiting &middot; '
            '<span class="mono">&#8776;1h 21m</span></span>') in line
    assert out["defaultOpen"] is False
    # a click opens it and stores the choice where the accordion's folds live
    assert out["stored"] == "open"
    assert out["opened"] == ['"sec-now"', '"R"', '"P"', '"F"', '"H1"', '"W2"', '"W3"',
                             '"ghead qmore"', '"W4"', '"W5"']
    assert 'aria-expanded="true"' in out["openLine"]
    assert out["refolded"] == out["folded"]
    # a search never hides a match behind the fold: four W rows match, so
    # the line stands, forced open, and the fourth file shows under it
    assert out["searched"] == ['"sec-now"', '"W2"', '"W3"', '"W4"', '"ghead qmore"', '"W5"']
    assert out["noFold"] is False


def test_a_folded_queue_file_leaves_the_selection():
    """A queue file the fold hides is not in the DOM, so it leaves the
    selection, as a row inside a folded month does."""
    out = _run_node("\n".join([
        "let S={timeline:[{id:'W1',state:'waiting'},{id:'W4',state:'waiting'},"
        "{id:'W5',state:'held'}]};",
        "const SELECTABLE=new Set(['ready','waiting','held']);",
        "let SEL=new Set(['W1','W4','W5']);",
        "let LAST_GROUPS=[{pinned:true,rows:[],folded:['W4','W5']}];",
        "const document={querySelectorAll:()=>[],body:{classList:{toggle(){}}}};",
        "function drawBulkBar(){}",
        _js_fn("applySel"),
        "applySel();console.log(JSON.stringify([...SEL].sort()));",
    ]))
    assert out == ["W1"]
