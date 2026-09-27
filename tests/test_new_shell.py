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
    # the two callers that KNOW their meeting pass it
    assert re.search(r"openNamePanelByUid\('\$\{escJs\(uid\)\}','\$\{escJs\(row\.id\)\}'\)",
                     NEW_JS)
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


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_pill_counts_the_meetings_a_naming_is_walking():
    """The queue knows the target list, so the pill says how far along it is
    instead of an open-ended "applying names". A whole-library pass re-lists as
    it goes and reports no total -- that one keeps the old wording."""
    src = _js_fn("drawPill")
    i = src.index("}else if(s.relabel_running||s.relabel_pending){")
    branch = src[i:src.index("}else{show=false;}", i)]
    fixture = "\n".join([
        "function run(s){let cls='',html='';" + branch[len("}else if(s.relabel_running||s.relabel_pending){"):]
        + "return html;}",
        """
console.log(JSON.stringify([
  run({jobs:{running:{kind:'relabel',total:7,done:2}}}),
  run({jobs:{running:{kind:'relabel',total:7,done:7}}}),
  run({jobs:{running:{kind:'all',total:null,done:0}}}),
  run({jobs:{running:null}}),
  run({})]));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [
        "naming: 3 of 7",
        "naming: 7 of 7",          # the last meeting, never "8 of 7"
        "applying names&#8230;",
        "applying names&#8230;",
        "applying names&#8230;"]


def test_the_relabel_watcher_runs_before_the_meeting_view_early_return():
    body = _js_fn("render")
    assert "relabelWatch(S);" in body
    assert body.index("relabelWatch(S);") < body.index("route.view==='meeting'"), \
        "placed after the early return, the watcher never fires on a meeting page"
    # the pill takes BOTH signals: relabel_pending alone only ever meant "a
    # second pass is queued behind a first"
    assert "s.relabel_running||s.relabel_pending" in _js_fn("drawPill")


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
    accuracy chore, rarely the day's first work, so its tray block renders
    BELOW unknown voices. The order is the source order of the two blocks."""
    tray = re.search(r"function drawTray\(s\)\{.*?\n\}", NEW_JS, re.S).group(0)
    voices_at = tray.index("voices need names")
    reviews_at = tray.index("Flagged lines in")
    assert voices_at < reviews_at, "unknown voices render above flagged reviews"


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
    # rows are already tabindex=0 (the ring reuses :focus-visible); section
    # headers are buttons and ride the ring with them, so a folded month is
    # reachable. Synthetic upload rows (no tabindex) are still skipped.
    assert "'#timeline .row[tabindex], #timeline .ghead'" in NEW_JS
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
# The naming row is a review card (2026-08-06): the form keeps its place, and
# under it sit the three things a human names a meeting from -- the original
# filename (the user encodes date and topic there; the AI title loses it), the
# voices heard, and the summary. Unnamed voices open the ONE naming slide-over.
# ---------------------------------------------------------------------------
def _needs_name_case():
    return NEW_JS[NEW_JS.index("case 'needs_name'"):NEW_JS.index("case 'ready'")]


def test_needs_name_row_shows_the_source_filename_and_adopts_it():
    body = _needs_name_case()
    # the form is still first, prefilled, with Accept
    assert 'class="ntitle"' in body and "acceptMeeting(" in body
    # the filename line renders from the row's source_file, via the stem
    assert "row.source_file" in body and "srcStem(" in body
    assert 'class="nsrc"' in body and 'class="nsrcname"' in body
    assert "nsrcAdopt(this," in body
    # suppressed when the stem already equals the prefilled title (a recorder
    # default), trimmed and case-insensitively
    assert "trim().toLowerCase()!==" in body
    # the adopter fills the row's own name box, then focuses it
    m = re.search(r"function nsrcAdopt\(.*?\n\}", NEW_JS, re.S)
    assert m and "'.nameform .ntitle'" in m.group(0)
    assert "inp.value=stem" in m.group(0) and "inp.focus()" in m.group(0)


def test_needs_name_row_chips_reuse_the_one_naming_panel():
    body = _needs_name_case()
    assert "rowSpeakerChips(row)" in body
    m = re.search(r"function rowSpeakerChips\(.*?\n\}\n", NEW_JS, re.S)
    assert m, "rowSpeakerChips missing"
    chips = m.group(0)
    # both naming paths: the registry uid, and the cluster for a voice the
    # registry never tracked (minting floor / "not a real speaker" tombstone)
    assert "openNamePanelByUid(" in chips and "openNamePanelByCluster(" in chips
    assert "row.unnamed_clusters" in chips
    # the uid lookup is mUnknownUid parameterized by base, and hidden
    # (archived) unknowns are NOT filtered out of it
    m = re.search(r"function rowUnknownUid\(.*?\n\}", NEW_JS, re.S)
    assert m and "archived" not in m.group(0)
    assert "(x.meetings||[]).includes(base)" in m.group(0)
    # named people stay plain text; unnamed wear the "?" affix
    assert '<span class="nchip">' in chips and 'class="nchipq"' in chips


def test_needs_name_row_shows_the_summary_quietly():
    body = _needs_name_case()
    assert "row.summary" in body and 'class="nsum"' in body
    # one clamped, muted paragraph on the theme tokens (no hardcoded colors)
    m = re.search(r"(?m)^\.nsum\{[^}]*\}", NEW_CSS, re.S)
    assert m and "var(--sub)" in m.group(0) and "line-clamp:2" in m.group(0)


# The three tests above pin the SOURCE TEXT of the review-card logic (the right
# calls and operators appear), not its behavior. That leaves real regressions
# invisible -- a reversed comparison, a stem cut at the wrong dot, or a filter
# reintroduced on archived unknowns would all still contain the same literal
# substrings. These drive the actual functions through node, house-style (see
# test_stepper_orders_fabricated_out_of_order_flags_by_start_time above).
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
        _js_oneline("esc"), _js_oneline("escJs"), _js_fn("srcStem"),
        _js_fn("rowUnknownUid"), _js_fn("rowSpeakerChips"),
        "let S=null;", _js_fn("bodyAndSlot"),
        """
const same = bodyAndSlot({state:'needs_name', id:'b1',
  source_file:'Team Sync 0714.m4a',
  suggested_title:'  team sync 0714  ', title:'  team sync 0714  ',
  speakers:[], unnamed_clusters:{}});
const diff = bodyAndSlot({state:'needs_name', id:'b2',
  source_file:'Team.Sync 0714.m4a',
  suggested_title:'Different Title', title:'Different Title',
  speakers:[], unnamed_clusters:{}});
console.log(JSON.stringify({
  sameHasNsrc: same.includes('class="nsrc"'),
  diffHasNsrc: diff.includes('class="nsrc"'),
  diffKeepsInternalDot: diff.includes('Team.Sync 0714'),
}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["sameHasNsrc"] is False, \
        "equal stem (trimmed, case-insensitive) must suppress the from-file line"
    assert out["diffHasNsrc"] is True
    assert out["diffKeepsInternalDot"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_row_unknown_uid_matches_archived_voices_not_just_live_ones():
    # archiving an unknown was meant to stop the tray/drawer nagging about a
    # one-time voice, never to strip its naming affordance out of a row that
    # heard them -- a regression that starts filtering archived entries here
    # would silently turn a real, still-nameable person into a dead chip
    fixture = _js_fn("rowUnknownUid") + "\n" + """
S = {unknowns: [{uid:'U1', display:'Voice 9', meetings:['base1'], archived:true},
                {uid:'U2', display:'Voice 10', meetings:['base1'], archived:false}]};
console.log(JSON.stringify([
  rowUnknownUid('base1','Voice 9'),
  rowUnknownUid('base1','Voice 10'),
  rowUnknownUid('base1','Voice 99'),
  rowUnknownUid('otherbase','Voice 9')
]));
"""
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["U1", "U2", None, None]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_needs_name_row_escapes_hostile_filenames_and_speaker_names():
    # a meeting really can be named after a person with an apostrophe, and a
    # filename or a diarized display can carry quotes/angle brackets. Every
    # interpolation into the review card must survive that without breaking
    # out of its HTML attribute or its onclick JS-string context.
    fixture = "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"), _js_fn("srcStem"),
        _js_fn("rowUnknownUid"), _js_fn("rowSpeakerChips"),
        "let S=null;", _js_fn("bodyAndSlot"),
        r"""
const html = bodyAndSlot({state:'needs_name', id:"o'brien<b>",
  source_file: "Mark C. Ashby <sync> \"weird\".m4a",
  suggested_title: 'Something else', title: 'Something else',
  speakers: ["Ashby <script>alert(1)</script>"],
  unnamed_clusters: {"Ashby <script>alert(1)</script>": "SPEAKER_00"},
  summary: "A <b>bold</b> claim & an 'apostrophe'"});
console.log(JSON.stringify({html}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = json.loads(r.stdout)["html"]
    # never a raw, unescaped tag from user-controlled text
    assert "<script>alert(1)</script>" not in html
    assert "<b>bold</b>" not in html
    # the filename/display text renders HTML-escaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;sync&gt;" in html and "&quot;weird&quot;" in html
    # the onclick JS-string args are escJs'd: an embedded apostrophe is
    # backslash-escaped so it cannot close the enclosing '...' early and spill
    # into (and break) the surrounding double-quoted onclick="..." attribute
    assert "onclick=\"nsrcAdopt(this,'Mark C. O\\'Halloran" in html
    assert ("onclick=\"openNamePanelByCluster('o\\'brien&lt;b&gt;',"
            "'SPEAKER_00','O\\'Halloran &lt;script&gt;alert(1)&lt;/script&gt;')\"") in html


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
    # hover instead of a lift; attention items wear their colored left edge
    # (teal tray, accent naming row)
    assert re.search(r"(?m)^\.row\{[^}]*border-radius:6px", NEW_CSS)
    assert re.search(r"(?m)^\.row\{[^}]*margin:0 0 10px", NEW_CSS)
    assert ".row:hover{border-color:var(--line)}" in NEW_CSS
    assert re.search(r"(?m)^\.tray\{[^}]*border-left:4px solid var\(--attn\)", NEW_CSS)
    assert re.search(r'needs_name"\]\{[^}]*border-left:4px solid var\(--accent\)', NEW_CSS)


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
    bulk bar -- never on a row, the tray, or any of the in-flow card
    surfaces."""
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
                r"(?m)^\.tray\{[^}]*\}", r"(?m)^\.msection\{[^}]*\}",
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
    # both queue states carry it (a held file is still worth flagging)
    waiting = NEW_JS.split("case 'waiting':", 1)[1].split("case 'held':", 1)
    assert "dupNote(row)" in waiting[0] and "dupNote(row)" in waiting[1][:900]
    # ...and the flag is part of the row signature: the check hashes on a size
    # collision, so it lands a poll or two after the row first painted
    assert "r.dup_of,r.dup_reason" in _js_fn("sigOf")
    m = re.search(r"(?m)^\.dupflag\{[^}]*\}", NEW_CSS, re.S)
    # the house colour for a decision that is not an error: teal since Indigo
    assert m and "var(--attn)" in m.group(0) and "13px" in m.group(0)


def test_the_duplicate_sweep_is_a_two_step_and_deletes_only_copies():
    tray = _js_fn("drawTray")
    assert "t.kind==='dupe_files'" in tray and "t.kind==='dupe_meetings'" in tray
    # the armed flag rides the tray signature, or a 2s poll would disarm the
    # confirm under the user's cursor
    assert "DUPE_ARMED" in tray and "+DUPE_ARMED" in tray
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
    It is state now: part of the signature, rendered by drawTray itself."""
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
# duplicates drawer.
# ---------------------------------------------------------------------------
_TRAY_HARNESS = [
    _js_oneline("esc"), _js_oneline("escJs"),
    "const tray={hidden:true,dataset:{},innerHTML:''};",
    "function $(sel){return sel==='#tray'?tray:null;}",
    "function rowById(){return null;}",
    "function shortDate(){return '';}",
    "let trayOpen={review:false,voices:false},flaggedOnly=false;",
    "const TRAY_EXPAND_MAX=8;",
    "let DUPE_ARMED=false,DUPE_ERR='';",
    "let S=null;",
    _js_fn("_trayVerb"), _js_fn("_trayRow"), _js_fn("_trayAgg"), _js_fn("_traySub"),
    _js_oneline("dupeDisarm"),
    _js_fn("drawTray"),
]


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_the_dupe_sweep_disarms_when_its_tray_row_goes_away():
    """R1: arm "Delete copies", walk away without confirming, and the flagged
    file leaves the queue by another path. DUPE_ARMED survived, so a FUTURE
    duplicate row -- different files, days later -- rendered pre-armed and its
    first click fired the no-undo bulk delete with confirm:true."""
    fixture = "\n".join(_TRAY_HARNESS + ["""
const withDupes={tray:[{kind:'dupe_files',title:'Already processed',
  detail:'2 waiting files were already processed',target:'src:a.m4a',
  exact:2,count:2}]};
drawTray(withDupes);
DUPE_ARMED=true;                       // the user armed the sweep
tray.dataset.sig='';drawTray(withDupes);
const armed={html:tray.innerHTML.includes('dupeDeleteGo(this)'),flag:DUPE_ARMED};
// the flagged file leaves the queue some other way: no dupe_files row at all
drawTray({tray:[{kind:'review',title:'Board Prep',detail:'3 flagged',
  target:'Board Prep',count:3}]});
const gone={flag:DUPE_ARMED};
// and the NEXT duplicate row must render unarmed
drawTray(withDupes);
const later={html:tray.innerHTML.includes('dupeDeleteGo(this)'),
  arm:tray.innerHTML.includes('dupeArm(true)'),flag:DUPE_ARMED};
// an empty tray disarms too (the early return must not skip the reset)
DUPE_ARMED=true;drawTray({tray:[]});
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
    no longer on disk, so the click posted /api/run for nothing."""
    fixture = "\n".join(_TRAY_HARNESS + ["""
drawTray({tray:[{kind:'failed',title:'ghost.m4a',detail:'boom',
  target:'src:ghost.m4a',count:1,gone:true}]});
const gone=tray.innerHTML;
drawTray({tray:[{kind:'failed',title:'here.m4a',detail:'boom',
  target:'src:here.m4a',count:1,gone:false}]});
const here=tray.innerHTML;
console.log(JSON.stringify({
  goneVerb:gone.includes("trayAct('failed_gone','src:ghost.m4a')"),
  goneRetry:gone.includes("trayAct('failed','src:ghost.m4a')"),
  hereRetry:here.includes("trayAct('failed','src:here.m4a')"),
  hereDismiss:here.includes('failed_gone')}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"goneVerb": True, "goneRetry": False,
                                    "hereRetry": True, "hereDismiss": False}
    # and the dispatcher routes that verb to the dismissal, never to /api/run
    assert "if(kind==='failed_gone'){failDismiss(target);return;}" in _js_fn("trayAct")


@pytest.mark.skipif(NODE is None, reason="node not installed -- JS behavior gate skipped")
def test_a_sweep_that_deleted_nothing_says_why():
    """R9: every server-side skip answered {ok:true, deleted:[]}, which reads
    as full success -- no message at all, and the same Delete button
    re-rendered and silently no-opped on every further click."""
    fixture = "\n".join([
        "let DUPE_ARMED=true,DUPE_ERR='';let S={};",
        "let trays=0;function drawTray(){trays++;}",
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
    never rendered. Either render it or stop computing it; it renders."""
    fixture = "\n".join([
        _js_oneline("esc"), _js_oneline("escJs"), _js_fn("srcStem"),
        _js_fn("rowUnknownUid"), _js_fn("rowSpeakerChips"), _js_fn("dupNote"),
        _js_fn("slotActions"), "let S=null;", _js_fn("bodyAndSlot"),
        """
const withDet=bodyAndSlot({state:'waiting',id:'src:a.m4a',title:'a.m4a',
  size_mb:42.1,est_minutes:9,est_detail:'transcribe ~6m \\u00b7 speakers ~3m'});
const without=bodyAndSlot({state:'waiting',id:'src:b.m4a',title:'b.m4a',
  size_mb:1.0,est_minutes:1});
console.log(JSON.stringify({
  shown:withDet.includes('transcribe ~6m'),
  cls:withDet.includes('class="rmeta estdetail"'),
  quiet:without.includes('estdetail')}));
"""])
    r = subprocess.run([NODE, "-e", fixture], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"shown": True, "cls": True, "quiet": False}
    # the field folds into the row signature, or a late estimate never repaints
    assert "r.est_detail," in _js_fn("sigOf")
    m = re.search(r"(?m)^\.estdetail\{[^}]*\}", NEW_CSS, re.S)
    assert m and "13px" in m.group(0) and "var(--sub)" in m.group(0)


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
        "let flaggedOnly=" + flagged + ";",
        "let S=null;let DRAWS=0;function drawTimeline(){DRAWS++;}",
        "function esc(s){return (s??'').toString().replace(/[&<>\"]/g,"
        "c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));}",
        "function escJs(s){return esc(s).replace(/\\\\/g,'\\\\\\\\')"
        ".replace(/'/g,\"\\\\'\");}",
        "function rowHTML(r){return '<div class=\"row\" data-id=\"'+r.id+'\"></div>';}",
        "function afterRender(){}",
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
    assert "dateKey(b).localeCompare(dateKey(a))" in body
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
    i = NEW_JS.index("function kbRows(")
    rows = NEW_JS[i:NEW_JS.index("function ", i + 10)]
    assert ".ghead" in rows, "kbRows must collect the section headers too"
    assert ".row[tabindex]" in rows, "and the rows it always collected"
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
        "let FS=null,FS_CARRY=null,MR=null,S={timeline:[]},route={base:null};",
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
    assert 'class="doneline"' in out["carried"]["html"]
    assert out["carried"]["carry"] is None, "a carry is consumed once"


def _voices_env():
    return _finish_env() + "\n".join([
        _js_fn("fsKey"), _js_fn("fvUse"), _js_fn("fvSkip"), _js_fn("fvNext"),
        _js_fn("fvSave"), _js_fn("saveName"), _js_fn("namedToast"), _js_fn("fvBusy"),
        _js_fn("fvToken"), _js_oneline("_fsOpenVoices"), _js_fn("fsGo"),
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
