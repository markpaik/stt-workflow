"""The panel's frame (plan Phase 4C), served at / since the 4D swap. A
request for /classic, the deleted old page, redirects to /.

The routes, the read-only search endpoint, the token contrast, the type
floor, the 20-surface table in gui/DESIGN.md, and the frame's pure logic run
in Node against a stub DOM. All data here is synthetic.
"""
import http.client
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from gui import server as srv
from stt import config, search

NODE = shutil.which("node")
STATIC = Path(srv.__file__).resolve().parent / "static"
DESIGN = Path(srv.__file__).resolve().parent / "DESIGN.md"


# the new shell's script: three plain files joined in this fixed order
NEXT_JS_FILES = ("next.js", "next-meeting.js", "next-voice.js", "next-library.js")


def _next_js():
    return "\n".join((STATIC / f).read_text(encoding="utf-8") for f in NEXT_JS_FILES)


def _compose_next():
    """Independent re-implementation of the composer for the new shell's parts."""
    page = (STATIC / "next.html").read_text(encoding="utf-8")
    css = (STATIC / "next.css").read_text(encoding="utf-8")
    return page.replace("/*@NEXT_CSS@*/", css).replace("//@NEXT_JS@", _next_js())


@pytest.fixture
def running_server(sandbox):
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    t.join(timeout=2)


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    out = (r.status, r.getheader("Content-Type"), r.read())
    conn.close()
    return out


def _meeting(base, text, **extra):
    from conftest import mfile
    d = {"source_file": f"{base}.m4a", "duration_sec": 600.0, "date": "2026-10-20",
         "speakers": [{"id": "SPEAKER_00", "display": "Speaker 1"}],
         "segments": [{"start": 1.0, "end": 4.0, "speaker": "SPEAKER_00", "text": text}],
         "ai_summary": "The group planned the synthetic orchard survey."}
    d.update(extra)
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text(text)


# ------------------------------------------------------------------ routes ---

def _location(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    out = (r.status, r.getheader("Location"), r.read())
    conn.close()
    return out


def test_root_serves_the_shell_and_classic_redirects_to_it(running_server):
    # / is the panel, and the 4C preview address /next still answers with it
    for path in ("/", "/next", "/?ui=stale-bookmark"):
        status, ctype, body = _get(running_server, path)
        assert status == 200 and ctype.startswith("text/html"), path
        assert body == _compose_next().encode(), path
    html = _get(running_server, "/")[2].decode()
    assert "@NEXT_CSS@" not in html and "@NEXT_JS@" not in html
    # the old page is deleted: a bookmark to /classic lands on /, never a 404,
    # and the redirect carries no page of its own
    for path in ("/classic", "/classic?ui=stale-bookmark"):
        status, where, body = _location(running_server, path)
        assert (status, where, body) == (302, "/", b""), path


def test_the_shell_never_points_at_the_deleted_classic_page():
    # the sidebar foot held "Open the classic page", and the narrow menu
    # sheet's foot held nothing else. Both are gone with the page itself.
    page = _compose_next()
    assert "/classic" not in page
    assert "classic" not in page.lower()
    js = _next_js()
    assert '<div class="side-foot"><a href="#" data-act="keys">Keys</a></div>' in js
    assert js.count('class="side-foot"') == 1


def test_next_frame_reloads_from_disk_without_a_restart(sandbox, tmp_path, monkeypatch):
    d = tmp_path / "static"
    d.mkdir()
    for f in ("next.html", "next.css") + NEXT_JS_FILES:
        shutil.copy(STATIC / f, d / f)
    monkeypatch.setattr(srv, "_STATIC_DIR", d)
    monkeypatch.setattr(srv, "_next_cache", {"mtimes": None, "html": None})
    first = srv._next_html()
    import os
    # every part reloads on its own, the last script included
    for f in ("next.js", "next-voice.js"):
        marker = "ZEBRA_" + f.replace(".", "_").replace("-", "_").upper()
        (d / f).write_text(f"const {marker}=1;", encoding="utf-8")
        st = (d / f).stat()
        os.utime(d / f, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
        assert marker in srv._next_html() and marker not in first


def test_search_meetings_is_read_only_and_origin_gated(running_server):
    _meeting("Orchard Survey 10202026", "We counted the pear trees by the north fence.")
    status, _, body = _get(running_server, "/api/search_meetings?q=orchard")
    assert status == 200
    hits = json.loads(body)["meetings"]
    assert hits and hits[0]["base"] == "Orchard Survey 10202026"
    status, _, _ = _get(running_server, "/api/search_meetings?q=orchard",
                        headers={"Origin": "https://evil.example.com"})
    assert status == 403


def test_get_routes_the_frame_reads_never_write_a_meeting(running_server):
    # plan 4C gate: GET no longer writes. The frame's GETs leave every
    # meeting file byte-identical, and /api/suggest refuses GET outright.
    _meeting("Orchard Survey 10202026", "We counted the pear trees by the north fence.")
    j = config.meeting_file("Orchard Survey 10202026", ".json")
    before = j.read_bytes()
    for path in ("/", "/next", "/api/state", "/api/search?q=pear",
                 "/api/search_meetings?q=pear", "/api/transcript?base=Orchard%20Survey%2010202026",
                 "/api/review?base=Orchard%20Survey%2010202026",
                 "/api/voice_lines?base=Orchard%20Survey%2010202026&speaker=Speaker%201",
                 "/api/history", "/api/archived", "/api/dupes",
                 "/api/suggest?base=Orchard%20Survey%2010202026"):
        status, _, _ = _get(running_server, path)
        assert status in (200, 405), path
    assert j.read_bytes() == before
    assert _get(running_server, "/api/suggest?base=Orchard%20Survey%2010202026")[0] == 405


def test_state_reports_voice_memos_sync(running_server, monkeypatch):
    status, _, body = _get(running_server, "/api/state")
    vm = json.loads(body)["voicememos"]
    assert vm["enabled"] is True and vm["since"]
    monkeypatch.setenv("STT_VOICEMEMOS_SYNC", "0")
    vm = json.loads(_get(running_server, "/api/state")[2])["voicememos"]
    assert vm["enabled"] is False


def test_search_index_backs_the_palette(sandbox):
    # the palette's two calls: line hits and title/summary meetings
    _meeting("Orchard Survey 10202026", "We counted the pear trees by the north fence.")
    assert search.query("pear trees")["hits"][0]["base"] == "Orchard Survey 10202026"
    assert search.meetings("orchard survey")[0]["base"] == "Orchard Survey 10202026"


# ------------------------------------------------------------------ design ---

def test_every_next_text_pair_clears_4_5_in_all_four_blocks():
    from tools.token_contrast import AA, BLOCKS, CSS_PATH, audit
    assert CSS_PATH == STATIC / "next.css"     # the tool audits the panel's sheet
    rows = audit()
    bad = [r.line() for r in rows if r.ratio < AA]
    assert not bad, "\n".join(bad)
    assert {r.block for r in rows} == {label for label, _ in BLOCKS}


def test_next_css_type_floor_is_13px():
    css = (STATIC / "next.css").read_text(encoding="utf-8")
    sizes = [int(n) for n in re.findall(r"(?:font-size|--fs-[a-z]+)\s*:\s*(\d+)px", css)]
    sizes += [int(n) for n in re.findall(r"font:\s*\d+\s+(\d+)px", css)]
    assert sizes and min(sizes) >= 13


def test_next_ui_has_no_em_dashes_and_no_external_assets():
    page = _compose_next()
    assert "—" not in page
    # fonts are the vendored Figtree; no CDN, no remote script
    assert not re.search(r"""(src|href)=["']https?://""", page)
    assert "url(http" not in page


def test_the_audit_measures_the_pairs_the_sheet_actually_paints():
    """A hand-written pair list drifts: it once missed two pairs the sheet
    painted. The audit also scans the sheet for every rule that sets a token
    colour on a token ground, so a new rule cannot add an unmeasured pair."""
    from tools.token_contrast import CSS_PATH, audit, painted_pairs

    assert painted_pairs(".x{color:var(--ink);background:var(--inset)}"
                         ".y{color:var(--sub)}.z{background:var(--card)}") == {("ink", "inset")}
    rows = audit()
    measured = {(r.fg, r.bg) for r in rows}
    painted = painted_pairs(CSS_PATH.read_text(encoding="utf-8"))
    assert painted, "the scan found nothing -- its rule regex broke"
    assert painted <= measured, sorted(painted - measured)
    # every block measures the same set, and nothing is measured twice
    per_block = {}
    for r in rows:
        per_block.setdefault(r.block, []).append((r.fg, r.bg))
    for block, pairs in per_block.items():
        assert len(pairs) == len(set(pairs)), f"{block} measured a pair twice"
    assert len({frozenset(v) for v in per_block.values()}) == 1


def test_the_dark_token_block_stops_at_its_own_closing_brace():
    """The @media pattern opens TWO braces. Counting from the start of the
    match closed the @media wrapper instead of the :root inside it, so every
    later rule in that media query was read as a token declaration."""
    from tools.token_contrast import read_tokens

    css = "@media(prefers-color-scheme:dark){:root{--a:#000;} .x{--b:#fff;}}"
    tokens = read_tokens(css, r"(?m)^@media\(prefers-color-scheme:dark\)\{:root\{")
    assert tokens == {"a": "#000"}, \
        "the slice must end at :root's brace, not the media query's"


def test_every_stage_the_pipeline_reports_has_its_pill_word_in_design_md():
    """The pill names the active file's stage through STAGE_NICE, and a stage
    it does not know reads "Working". Every stage the pipeline reports needs
    its own word, and gui/DESIGN.md's Status pill section lists each one, so a
    new stage cannot fall back to "Working" unnoticed."""
    root = Path(srv.__file__).resolve().parent.parent
    src = "".join((root / p).read_text(encoding="utf-8")
                  for p in ("stt/pipeline.py", "run_batch.py"))
    reported = set(re.findall(r'\b(?:report|set_stage)\(\s*(?:\w+\s*,\s*)?"([a-z]+)"', src))
    # the scan must find the pipeline's stages, or the test proves nothing
    assert reported >= {"downloading", "converting", "transcribing",
                        "diarizing", "verifying", "writing", "summarizing"}
    nice = dict(re.findall(r"(\w+):'([^']+)'",
                           re.search(r"const STAGE_NICE=\{(.*?)\};", _next_js(), re.S).group(1)))
    assert not reported - set(nice), sorted(reported - set(nice))
    assert "STAGE_NICE[e.stage]||'Working'" in _next_js()
    text = DESIGN.read_text(encoding="utf-8")
    sec = text[text.index("## Status pill"):]
    sec = sec[:sec.index("\n## ")]
    missing = [w for w in sorted(set(nice.values()) | {"Working"}) if f"`{w}`" not in sec]
    assert not missing, f"DESIGN.md's Status pill section omits {missing}"


def test_screen_names_for_routes_with_a_slash():
    """QA 4F #8: groups/<key> and ask/<id> shoot to a file in OUT."""
    from tools import next_screens
    assert next_screens._name("groups/gabc123") == "groups-gabc123"
    assert next_screens._name("ask/0123456789ab") == "ask-0123456789ab"
    assert next_screens._name("m/Some Base 10012026") == "meeting"
    assert next_screens._name("m/Some Base 10012026/t/12") == "meeting-transcript"
    assert next_screens._name("home") == "home"


# ------------------------------------------------------------------- fonts ---
# The vendored UI face. Figtree (OFL) is served by the panel itself from
# gui/static/fonts/, never a CDN, behind a strict basename allowlist.
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
    for bad in ("../next.css", "..%2Fnext.css", "%2e%2e/next.css",
                "OFL.txt", "nope.woff2", ""):
        status, _ctype, _body = _get(running_server, "/static/fonts/" + bad)
        assert status == 404, f"font route must refuse {bad!r}"


def test_the_frame_loads_figtree_from_the_font_route():
    page = _compose_next()
    assert "@font-face" in page
    assert "font-family:'Figtree'" in page
    assert "font-weight:300 900" in page          # the variable axis, whole
    assert "font-display:swap" in page
    assert "/static/fonts/figtree-latin.woff2" in page
    # Figtree fronts the UI stack; -apple-system stays the fallback
    assert "--sans:'Figtree',-apple-system" in page


# The panel's surfaces, each with a token from the frame's script or markup
# that carries it (the 2026-11-01 panel audit numbered them, and the old
# surface map in gui/DESIGN.md placed each one before the classic page went).
SURFACE_TOKENS = (
    ("Menu-bar look mark", "/api/seen"),
    ("Sidebar top", 'class="side-top"'),
    ("Status pill", "function pillText"),
    ("Activity run controls", "'run-all'"),
    ("Recordings filter and chips", "function filtered"),
    ("Said in", "function palResults"),
    ("Running now", "function activeRowHTML"),
    ("Needs you", "function needsCard"),
    ("Recordings by day and month", "function recListHTML"),
    ("Folded months", 'data-act="fold"'),
    ("Meeting page", "function meetingView"),
    ("One content column", 'class="page"'),
    ("Row menu", "function rowMenu"),
    ("Selection bar", "function renderBulk"),
    ("Settings page", "function settingsView"),
    ("Voice sheet", "function vsHTML"),
    ("Key sheet", "const KEYS"),
    ("Drop overlay", 'class="drop"'),
    ("Setup check", "function setupCard"),
    ("Tab title", "function tabTitle"),
)


def test_each_surface_token_is_present_in_the_frame():
    assert len(SURFACE_TOKENS) == 20
    page = _compose_next()
    missing = [(name, tok) for name, tok in SURFACE_TOKENS if tok not in page]
    assert not missing, missing


def test_design_md_names_classic_only_as_the_redirect():
    # the old page is deleted, so DESIGN.md describes it nowhere; the one
    # line left says what a /classic bookmark gets
    lines = [ln for ln in DESIGN.read_text(encoding="utf-8").splitlines()
             if "classic" in ln.lower()]
    assert len(lines) == 1 and "`GET /classic` answers 302 to `/`" in lines[0], lines


def test_frame_routes_cover_the_pages_and_no_nav_entry_is_dead():
    js = _next_js()
    for r in ("home", "recordings", "groups", "activity", "speakers", "archive", "settings"):
        assert f"#{r}" in js or f"'{r}'" in js
    # every sidebar entry has a view to render (Groups since Phase 4E)
    nav = re.search(r"const NAV=\[(.*?)\];", js).group(1) + re.search(r"const NAV2=\[(.*?)\];", js).group(1)
    views = re.search(r"const views=\{(.*?)\};", js).group(1)
    for to in re.findall(r"\['(\w+)','", nav):
        assert re.search(rf"\b{to}:\w+View\b", views), to


def test_writes_from_the_frame_are_posts():
    js = _next_js()
    # every api(path,body) with a body is a POST in api(); GET calls carry
    # no body. The write endpoints must never appear in a body-less call.
    writes = ("rename", "set_date", "set_category", "accept_meeting", "bulk", "archive_meeting",
              "restore_meeting", "delete_meeting", "export", "run", "stop", "pause", "resume",
              "queue_hold", "queue_delete", "name", "hide_unknown", "merge_speakers",
              "rename_speaker", "remove_speaker", "remove_sample", "reassign_sample",
              "cloud_keys", "llm_backend", "automation", "schedule", "model", "punctuate",
              "mic_speaker", "seen", "suggest", "review", "ask", "forget", "dismiss_voice",
              "name_preview", "retranscribe")
    for w in writes:
        for m in re.finditer(r"api\('/api/" + w + r"'(\)|,)", js):
            assert m.group(1) == ",", f"/api/{w} called without a body (a GET)"


# ------------------------------------------------------- behavior in Node ---

_STUB = r"""
const _el=()=>new Proxy({innerHTML:'',hidden:false,dataset:{},classList:{toggle(){},add(){},remove(){},contains(){return false}},
 style:{},focus(){},contains(){return false},querySelector(){return null},querySelectorAll(){return []},
 getBoundingClientRect(){return {left:0,right:0,top:0,bottom:0}},setAttribute(){},getAttribute(){return null},removeAttribute(){}},
 {get(t,k){return k in t?t[k]:undefined},set(t,k,v){t[k]=v;return true}});
globalThis.document={addEventListener(){},querySelector:()=>_el(),querySelectorAll:()=>[],getElementById:()=>null,
 documentElement:_el(),body:_el(),hidden:true,activeElement:null,title:''};
globalThis.addEventListener=()=>{};globalThis.setInterval=()=>0;
globalThis.location={hash:'',search:'',href:''};globalThis.innerWidth=1440;globalThis.innerHeight=900;
globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
globalThis.fetch=()=>Promise.reject(new Error('offline'));
globalThis.CSS={escape:s=>s};globalThis.window={scrollTo(){},scrollY:0};globalThis.scrollTo=()=>{};
globalThis.history={replaceState(){}};globalThis.Audio=function(){return {play:()=>Promise.resolve(),pause(){},addEventListener(){}};};
"""


def _run_node(body):
    js = _STUB + _next_js() + "\n;(()=>{" + body + "})();"
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _iso(days_ago):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=days_ago)).isoformat()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_draft_chip_rules_follow_the_title_states():
    out = _run_node("""
const mk=(o)=>Object.assign({base:'b',title:'T',date:'2026-10-01',minutes:5,named:[],ask_clusters:{}},o);
console.log(JSON.stringify({
 need:isDraft(mk({title_state:'needs_title'})),
 prov:isDraft(mk({title_state:'provisional',route:'voice_memo'})),
 pub:isDraft(mk({title_state:'provisional',route:'public_download'})),
 conf:isDraft(mk({title_state:'confirmed'})),
 old:isDraft(mk({})),
 legacy:isDraft(mk({needs_review:true})),
 legacyChip:chipsFor(mk({needs_review:true})).includes('chip'),
 unc:[dateUncertain(mk({date_source:'mtime'})),dateUncertain(mk({date_source:'short_typed'})),dateUncertain(mk({date_source:'typed'}))],
 attn:chipsFor(mk({title_state:'needs_title'})).includes('chip attn'),
 quiet:chipsFor(mk({title_state:'provisional'})).includes('chip draft')}));
""")
    assert out["need"] and out["prov"]
    # a public download wears no chip, and a confirmed or pre-rule meeting none
    assert not out["pub"] and not out["conf"] and not out["old"]
    # a meeting from before the title rule falls back to needs_review
    assert out["legacy"] and out["legacyChip"]
    assert out["unc"] == [True, True, False]
    assert out["attn"] and out["quiet"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_recordings_group_by_day_then_month_and_fold_older_months():
    out = _run_node(f"""
S={{meetings:[
 {{base:'a',title:'Alpha',date:'{_iso(0)}',minutes:3,named:[],ask_clusters:{{}}}},
 {{base:'b',title:'Beta',date:'{_iso(1)}',minutes:3,named:[],ask_clusters:{{}}}},
 {{base:'c',title:'Gamma',date:'{_iso(3)}',minutes:3,named:[],ask_clusters:{{}}}},
 {{base:'d',title:'Delta',date:'2025-03-10',minutes:3,named:[],ask_clusters:{{}}}},
 {{base:'e',title:'Epsilon',date:'2025-01-10',minutes:3,named:[],ask_clusters:{{}}}}]}};
const h=recListHTML();
const heads=[...h.matchAll(/class="eyebrow">(?:<button[^>]*aria-expanded="(true|false)"[^>]*>(?:<svg[\\s\\S]*?<\\/svg>))?([^<]+)/g)].map(m=>[m[2].trim(),m[1]||null]);
console.log(JSON.stringify({{heads,rows:(h.match(/data-row /g)||[]).length}}));
""")
    labels = [h[0] for h in out["heads"]]
    assert labels[:3] == ["Today", "Yesterday", "This week"]
    assert labels[3:] == ["March 2025", "January 2025"]
    # the newest older month stays open, and anything older folds
    assert out["heads"][3][1] == "true" and out["heads"][4][1] == "false"
    assert out["rows"] == 4    # the folded month's row is not drawn


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_filters_match_title_people_and_chips():
    out = _run_node("""
S={meetings:[
 {base:'a',title:'Orchard Walk',date:'2026-10-01',named:['Rowan Pike'],ask_clusters:{},category:'work'},
 {base:'b',title:'Budget',date:'2026-10-02',named:[],ask_clusters:{'Speaker 2':'S2'},category:'personal'},
 {base:'c',title:'Notes',date:'2026-10-03',named:[],ask_clusters:{},title_state:'needs_title'}]};
const run=(q,chip)=>{UI.q=q;UI.chip=chip;return filtered().map(m=>m.base).join('')};
console.log(JSON.stringify({t:run('orch','all'),p:run('rowan','all'),w:run('','work'),
 pe:run('','personal'),d:run('','draft'),v:run('','voices')}));
""")
    assert out == {"t": "a", "p": "a", "w": "a", "pe": "b", "d": "c", "v": "b"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_needs_you_cards_by_kind():
    out = _run_node("""
S={meetings:[{base:'m1',title:'Raw Title',suggested:'Orchard Planning',date:'2026-10-01',minutes:9,named:[],ask_clusters:{}}],
 needs_you:[]};
const t=needsCard({kind:'needs_title',target:'m1',title:'Raw Title',detail:''},0);
const vu=needsCard({kind:'voice',target:'m1',title:'Speaker 4',uid:'U004',detail:'9 min'},1);
const vl=needsCard({kind:'voice',target:'m1',title:'Speaker 4',uid:null,detail:'9 min'},1);
const ns=needsCard({kind:'no_speech',target:'m1',title:'Raw Title',detail:''},2);
console.log(JSON.stringify({
 prefill:t.includes('value="Orchard Planning"'),primary:t.includes('btn primary'),keep:t.includes('data-act="keep"'),
 uid:vu.includes('data-act="name-voice"')&&vu.includes('U004')&&vu.includes('data-base="m1"'),local:vl.includes('href="#m/m1"'),
 silent:ns.includes('No speech found')&&ns.includes('data-act="archive"')}));
""")
    assert all(out.values()), out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_palette_merges_titles_and_said_in_with_deep_links():
    out = _run_node("""
S={meetings:[{base:'Orchard 1',title:'Orchard Survey',date:'2026-10-01',minutes:9,named:[],ask_clusters:{}},
 {base:'Budget 2',title:'Budget',date:'2026-10-02',minutes:9,named:[],ask_clusters:{}}]};
UI.pq='pear';
UI.pres={q:'pear',total:1,meetings:[{base:'Budget 2',title:'Budget'}],
 hits:[{base:'Orchard 1',start:61.4,who:'Speaker 1',snippet:'the pear trees'}]};
const h=palResults();
UI.pq='orch';UI.pres=null;const h2=palResults();
console.log(JSON.stringify({said:h.includes('Said in'),deep:h.includes('href="#m/Orchard%201/t/61.4"'),
 fromIdx:h.includes('href="#m/Budget%202"'),mark:h.includes('<mark>pear</mark>'),
 local:h2.includes('<mark>Orch</mark>ard Survey'),pending:h2.includes('Searching')}));
""")
    assert out == {"said": True, "deep": True, "fromIdx": True, "mark": True,
                   "local": True, "pending": True}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_palette_says_a_stop_word_was_not_searched():
    """'is' showed "No matches.", which reads as "nobody said it". The
    server's note says the word is too common to search on its own."""
    out = _run_node("""
S={meetings:[{base:'b',title:'Budget',date:'2026-10-02',minutes:9,named:[],ask_clusters:{}}]};
const note='\u201cof\u201d is too common to search on its own. Add another word.';
UI.pq='of';UI.pres={q:'of',total:0,meetings:[],hits:[],note};
const h=palResults();
UI.pq='zq';UI.pres={q:'zq',total:0,meetings:[],hits:[],note:''};
const h2=palResults();
console.log(JSON.stringify({note:h.includes('too common'),nm:h.includes('No matches.'),
 h2:h2.includes('No matches.')}));
""")
    assert out == {"note": True, "nm": False, "h2": True}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_tab_title_counts_items_that_joined_while_hidden():
    out = _run_node("""
S={meetings:[],needs_you:[{kind:'needs_title'}]};UI.route='home';
const a=tabTitle();HIDDEN_BASE=1;S.needs_you=[{kind:'needs_title'},{kind:'voice'},{kind:'no_speech'}];
const b=tabTitle();HIDDEN_BASE=null;const c=tabTitle();
console.log(JSON.stringify({a,b,c}));
""")
    assert out == {"a": "Home · Meetings", "b": "(2) Home · Meetings", "c": "Home · Meetings"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_meeting_links_escape_the_base():
    out = _run_node("""
console.log(JSON.stringify({a:meetingHref('A/B #1'),b:meetingHref('x',12.9)}));
""")
    # the meeting page lives in this page now: an in-page hash, no reload
    assert out == {"a": "#m/A%2FB%20%231", "b": "#m/x/t/12.9"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_rows_escape_hostile_titles():
    out = _run_node("""
S={meetings:[]};
const h=rowHTML({base:'b"><img>',title:'<script>x</script>',date:'2026-10-01',minutes:1,named:['<b>n</b>'],ask_clusters:{},summary:'<i>s</i>'});
console.log(JSON.stringify({raw:/<script>|<img>|<b>n|<i>s/.test(h)}));
""")
    assert out["raw"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_highlight_never_lands_inside_an_entity():
    out = _run_node("""
console.log(JSON.stringify({a:hl('R&D plan','amp'),b:hl('a<b','lt'),c:hl('R&D plan','&d'),d:hl('<x>','')}));
""")
    assert out == {"a": "R&amp;D plan", "b": "a&lt;b", "c": "R<mark>&amp;D</mark> plan",
                   "d": "&lt;x&gt;"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_select_from_home_survives_the_route_change_and_name_opens_the_sheet_in_place():
    # rewritten for 4D: Select still carries across to Recordings, and Name on
    # a Home voice card opens the voice sheet over Home with the card's
    # meeting as context, instead of a trip to Speakers
    out = _run_node("""
S={meetings:[{base:'m1',title:'Alpha',date:'2026-10-01',minutes:3,named:[],ask_clusters:{}}],
 needs_you:[],enrolled:[],unknowns:[{uid:'U004',display:'Speaker 4',meetings:['m1']}]};
// a browser hash reads back with its leading '#'
let _h='';Object.defineProperty(location,'hash',{get:()=>_h?'#'+_h:'',set:v=>{_h=String(v).replace(/^#/,'');}});
api=()=>new Promise(()=>{});
UI.route='home';
A.select({dataset:{base:'m1'}});
const hashA=location.hash;route();
const sel={route:UI.route,size:UI.sel.size,has:UI.sel.has('m1')};
location.hash='home';route();
const cleared=UI.sel.size;
A['name-voice']({dataset:{uid:'U004',base:'m1'}});
const nm={hash:location.hash,route:UI.route,vs:VS&&{uid:VS.list[0].uid,base:VS.base,single:VS.single,title:vsHTML().includes('Who is Speaker 4?')}};
location.hash='recordings';route();
console.log(JSON.stringify({hashA,sel,cleared,nm,after:VS}));
""")
    assert out["hashA"] == "#recordings"
    assert out["sel"] == {"route": "recordings", "size": 1, "has": True}
    # an ordinary route change still clears the selection, and closes the sheet
    assert out["cleared"] == 0 and out["after"] is None
    assert out["nm"] == {"hash": "#home", "route": "home",
                         "vs": {"uid": "U004", "base": "m1", "single": True, "title": True}}


# ------------------------------------------------ durations under a minute ---

def test_the_meeting_meta_carries_whole_seconds(sandbox):
    """round(22 / 60) is 0, so a 22 s clip read '0 min' everywhere. The meta
    carries the seconds as well, for the page to show."""
    _meeting("2026-10-20 Short Clip", "hello", duration_sec=22.4)
    srv._meet_cache.clear()
    meta = srv._meeting_meta(config.meeting_file("2026-10-20 Short Clip", ".json"),
                             config.meetings_dir())
    assert (meta["minutes"], meta["seconds"]) == (0, 22)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_clip_under_a_minute_shows_seconds_never_0_min():
    out = _run_node("""
const mk=(o)=>Object.assign({base:'b',title:'T',date:'2026-10-01',named:[],ask_clusters:{}},o);
console.log(JSON.stringify({
 short:durText(mk({minutes:0,seconds:22})),
 edge:durText(mk({minutes:1,seconds:59})),
 min:durText(mk({minutes:1,seconds:60})),
 long:durText(mk({minutes:42,seconds:2520})),
 old:durText(mk({minutes:7})),
 tot:[totalText(22),totalText(300),totalText(5400),totalText(0)],
 row:rowHTML(mk({minutes:0,seconds:22})).includes('22 sec')}));
""")
    assert out["short"] == "22 sec" and out["edge"] == "59 sec"
    assert out["min"] == "1 min" and out["long"] == "42 min" and out["old"] == "7 min"
    assert out["tot"] == ["22 sec", "5 min", "1.5 h", "0 min"]
    assert out["row"]
    js = _next_js()
    # no meta line prints the rounded minutes on its own any more
    assert "minutes||0} min" not in js
    assert "${g.hours} h" not in js


# ------------------------------------------------------ phase 7 polish ---

@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_skip_link_is_the_first_tab_stop_and_focuses_the_page():
    """A keyboard user passed 14 sidebar stops before the page's own first
    control. The skip link is the first focusable element of the body, it
    stays off screen until it has focus, and it focuses main#view without
    touching the hash (a '#view' hash would be read as a route)."""
    page = (STATIC / "next.html").read_text(encoding="utf-8")
    body = page.split("<body>", 1)[1]
    first = re.search(r"<(a|button|input|select|textarea)\b[^>]*>", body).group(0)
    assert 'class="skip"' in first and 'data-act="skip"' in first
    assert body.index('class="skip"') < body.index('id="side"')
    assert 'id="view" tabindex="-1"' in body
    css = (STATIC / "next.css").read_text(encoding="utf-8")
    assert re.search(r"\.skip\{[^}]*top:-\d+px", css)
    assert re.search(r"\.skip:focus\{[^}]*top:var\(--s3\)", css)
    out = _run_node("""
let focused=0;const view={focus(){focused++;}};
document.querySelector=s=>s==='#view'?view:_el();
location.hash='#home';
A.skip();
console.log(JSON.stringify({focused,hash:location.hash}));
""")
    assert out == {"focused": 1, "hash": "#home"}
