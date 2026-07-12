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
