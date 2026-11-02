"""The /next frame (plan Phase 4C): the second composed page, served beside
the classic shell until the 4D swap.

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


def _compose_next():
    """Independent re-implementation of the composer for the /next parts."""
    page = (STATIC / "next.html").read_text(encoding="utf-8")
    css = (STATIC / "next.css").read_text(encoding="utf-8")
    js = (STATIC / "next.js").read_text(encoding="utf-8")
    return page.replace("/*@NEXT_CSS@*/", css).replace("//@NEXT_JS@", js)


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

def test_next_serves_the_composed_frame_and_root_keeps_the_classic(running_server):
    status, ctype, body = _get(running_server, "/next")
    assert status == 200 and ctype.startswith("text/html")
    assert body == _compose_next().encode()
    html = body.decode()
    assert "@NEXT_CSS@" not in html and "@NEXT_JS@" not in html
    # the old page still serves / (plan 4C gate), and it is not the frame
    status, _, root = _get(running_server, "/")
    assert status == 200 and b'id="processBtn"' in root and b'id="side"' not in root


def test_next_frame_reloads_from_disk_without_a_restart(sandbox, tmp_path, monkeypatch):
    d = tmp_path / "static"
    d.mkdir()
    for f in ("next.html", "next.css", "next.js"):
        shutil.copy(STATIC / f, d / f)
    monkeypatch.setattr(srv, "_STATIC_DIR", d)
    monkeypatch.setattr(srv, "_next_cache", {"mtimes": None, "html": None})
    first = srv._next_html()
    (d / "next.js").write_text("const ZEBRA_MARKER=1;", encoding="utf-8")
    import os
    st = (d / "next.js").stat()
    os.utime(d / "next.js", ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert "ZEBRA_MARKER" in srv._next_html() and "ZEBRA_MARKER" not in first


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
    for path in ("/next", "/api/state", "/api/search?q=pear", "/api/search_meetings?q=pear",
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
    from tools.token_contrast import AA, BLOCKS, NEXT_CSS_PATH, audit
    rows = audit(NEXT_CSS_PATH)
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


def _surface_rows():
    text = DESIGN.read_text(encoding="utf-8")
    sec = text[text.index("### Where the 20 classic surfaces went"):]
    rows = re.findall(r"^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|\s*(\w+)\s*\|\s*`([^`]+)`\s*\|$", sec, re.M)
    return [(int(n), name.strip(), where.strip(), st, tok) for n, name, where, st, tok in rows]


def test_design_md_places_every_one_of_the_20_surfaces():
    rows = _surface_rows()
    assert [r[0] for r in rows] == list(range(1, 21))
    assert {r[3] for r in rows} <= {"moves", "retires", "stays"}
    # only the two surfaces outside 4C's reach may stay: the menu-bar app
    # (not part of the page) and the reader (Phase 4D rebuilds it)
    assert {r[1] for r in rows if r[3] == "stays"} <= {"Menu-bar app", "Reader"}


def test_each_surface_token_is_present_in_the_frame():
    page = _compose_next()
    missing = [(n, tok) for n, _, _, _, tok in _surface_rows() if tok not in page]
    assert not missing, missing


def test_frame_routes_cover_the_4c_pages_and_nothing_from_later_phases():
    js = (STATIC / "next.js").read_text(encoding="utf-8")
    for r in ("home", "recordings", "activity", "speakers", "archive", "settings"):
        assert f"#{r}" in js or f"'{r}'" in js
    # Groups and Ask are Phases 4E and 4F: no dead nav entry for them yet
    nav = re.search(r"const NAV=\[(.*?)\];", js).group(1) + re.search(r"const NAV2=\[(.*?)\];", js).group(1)
    assert "groups" not in nav and "'ask'" not in nav


def test_writes_from_the_frame_are_posts():
    js = (STATIC / "next.js").read_text(encoding="utf-8")
    # every api(path,body) with a body is a POST in api(); GET calls carry
    # no body. The write endpoints must never appear in a body-less call.
    writes = ("rename", "set_date", "set_category", "accept_meeting", "bulk", "archive_meeting",
              "restore_meeting", "delete_meeting", "export", "run", "stop", "pause", "resume",
              "queue_hold", "queue_delete", "name", "hide_unknown", "merge_speakers",
              "rename_speaker", "remove_speaker", "remove_sample", "reassign_sample",
              "cloud_keys", "llm_backend", "automation", "schedule", "model", "punctuate",
              "mic_speaker", "seen", "suggest")
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
globalThis.CSS={escape:s=>s};globalThis.window={scrollTo(){}};
"""


def _run_node(body):
    js = _STUB + (STATIC / "next.js").read_text(encoding="utf-8") + "\n;(()=>{" + body + "})();"
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
 unc:[dateUncertain(mk({date_source:'mtime'})),dateUncertain(mk({date_source:'short_typed'})),dateUncertain(mk({date_source:'typed'}))],
 attn:chipsFor(mk({title_state:'needs_title'})).includes('chip attn'),
 quiet:chipsFor(mk({title_state:'provisional'})).includes('chip draft')}));
""")
    assert out["need"] and out["prov"]
    # a public download wears no chip, and a confirmed or pre-rule meeting none
    assert not out["pub"] and not out["conf"] and not out["old"]
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
 uid:vu.includes('data-act="name-voice"')&&vu.includes('U004'),local:vl.includes('/#m/m1'),
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
console.log(JSON.stringify({said:h.includes('Said in'),deep:h.includes('/#m/Orchard%201/t/61'),
 fromIdx:h.includes('/#m/Budget%202'),mark:h.includes('<mark>pear</mark>'),
 local:h2.includes('<mark>Orch</mark>ard Survey'),pending:h2.includes('Searching')}));
""")
    assert out == {"said": True, "deep": True, "fromIdx": True, "mark": True,
                   "local": True, "pending": True}


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
    assert out == {"a": "/#m/A%2FB%20%231", "b": "/#m/x/t/12"}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_rows_escape_hostile_titles():
    out = _run_node("""
S={meetings:[]};
const h=rowHTML({base:'b"><img>',title:'<script>x</script>',date:'2026-10-01',minutes:1,named:['<b>n</b>'],ask_clusters:{},summary:'<i>s</i>'});
console.log(JSON.stringify({raw:/<script>|<img>|<b>n|<i>s/.test(h)}));
""")
    assert out["raw"] is False


# ------------------------------------------------- classic seek deep link ---

def test_classic_reader_accepts_the_seek_deep_link():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "#m\\/([^/]+)\\/t\\/" in js
    assert "pendingOpen={base:route.base,seek:route.seek}" in js


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_classic_parse_hash_reads_the_seek_form():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    fn = js[js.index("function parseHash(){"):]
    fn = fn[:fn.index("\n}\n") + 2]
    body = ("let location={hash:''};" + fn + """
location.hash='#m/'+encodeURIComponent('Orchard 1')+'/t/61';const a=parseHash();
location.hash='#m/'+encodeURIComponent('Orchard 1');const b=parseHash();
location.hash='';const c=parseHash();
console.log(JSON.stringify({a,b,c}));""")
    r = subprocess.run([NODE, "-e", body], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["a"] == {"view": "meeting", "base": "Orchard 1", "seek": 61}
    assert out["b"] == {"view": "meeting", "base": "Orchard 1"}
    assert out["c"]["view"] == "timeline"
