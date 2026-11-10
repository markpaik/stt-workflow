"""The new shell's meeting page and voice sheet (plan Phase 4D).

The plan forbids dropping the naming, review, and hostile-name tests of the
old page when its reader was replaced: they encode bugs already found once.
Each test here ports one or more of them to the new files
(gui/static/next-meeting.js and next-voice.js) and names the old test it
ports. The old page and its tests (tests/test_new_shell.py) are deleted, so
those names are a record of what each port covers.

The behavior runs in Node against the joined script (next.js, then
next-meeting.js, then next-voice.js, the server's order) with a stub DOM, a
fake api(), and synthetic data only.
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

NODE = shutil.which("node")
STATIC = Path(srv.__file__).resolve().parent / "static"
DESIGN = Path(srv.__file__).resolve().parent / "DESIGN.md"
JS_FILES = ("next.js", "next-meeting.js", "next-voice.js", "next-library.js")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def _js():
    return "\n".join((STATIC / f).read_text(encoding="utf-8") for f in JS_FILES)


# A stub DOM: elements a test registers in __els come back by id, everything
# else is a throwaway element. The fake api() records every call and answers
# from a handler the test sets (`answer`), or holds the promise open.
_STUB = r"""
const __els={};
const _mk=o=>Object.assign({innerHTML:'',textContent:'',value:'',hidden:false,disabled:false,dataset:{},style:{},id:'',
 classList:{_s:new Set(),add(c){this._s.add(c)},remove(c){this._s.delete(c)},contains(c){return this._s.has(c)},toggle(){}},
 focus(){},blur(){},select(){},contains(){return false},querySelector(){return null},querySelectorAll(){return []},
 getBoundingClientRect(){return {left:0,right:0,top:0,bottom:0}},setAttribute(){},getAttribute(){return null},removeAttribute(){},
 insertAdjacentElement(){},remove(){},scrollIntoView(){},matches(){return false},addEventListener(){},closest(){return null}},o||{});
globalThis.document={addEventListener(){},querySelector:s=>(s[0]==='#'&&__els[s.slice(1)])||_mk(),querySelectorAll:()=>[],
 getElementById:id=>__els[id]||(String(id).startsWith('ln-')?(__els[id]=_mk({id})):null),documentElement:_mk(),body:_mk(),hidden:true,activeElement:null,title:'',createElement:()=>_mk()};
globalThis.addEventListener=()=>{};globalThis.setInterval=()=>0;globalThis.clearInterval=()=>{};
let _h='';globalThis.location={search:'',href:''};
Object.defineProperty(globalThis.location,'hash',{get:()=>_h?'#'+_h:'',set:v=>{_h=String(v).replace(/^#/,'');}});
globalThis.innerWidth=1440;globalThis.innerHeight=900;
globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};
globalThis.fetch=()=>Promise.reject(new Error('offline'));
globalThis.CSS={escape:s=>s};globalThis.window={scrollTo(){},scrollY:0};globalThis.scrollTo=()=>{};
globalThis.history={replaceState(a,b,u){_h=String(u).replace(/^#/,'');}};
globalThis.Audio=function(src){return {src,paused:true,readyState:0,currentTime:0,play(){this.paused=false;return Promise.resolve();},pause(){this.paused=true;},addEventListener(){}};};
"""

_PRE = r"""
// let the page's own startup (refresh, then route) finish first
for(let k=0;k<8;k++)await new Promise(r=>setTimeout(r,0));
const calls=[];let answer=null;
api=(p,b)=>{calls.push(b===undefined?[p]:[p,b]);return answer?Promise.resolve(answer(p,b)):new Promise(()=>{});};
const toasts=[];toast=m=>toasts.push(m);
let refreshes=0;refresh=async()=>{refreshes++;};
const tick=()=>new Promise(r=>setTimeout(r,0));
const flush=async()=>{for(let k=0;k<8;k++)await tick();};
const seg=(index,start,end,speaker,who,text,flags)=>({index,start,end,speaker,who,text,flags:flags||[],edited:false});
const opt=(id,display,o)=>Object.assign({id,display,named:false,local:false,dismissed:false},o||{});
// open the meeting page on `base` with a transcript answer, the real way
async function openPage(base,tr,row){
 S=Object.assign({meetings:[],needs_you:[],unknowns:[],enrolled:[],llm_available:true,llm_backend:'local'},S||{});
 if(!S.meetings.some(m=>m.base===base))S.meetings.push(Object.assign({base,title:'Synthetic '+base,date:'2026-10-20',minutes:30,named:[],
  ask_clusters:{},unnamed_clusters:{},short_voices:0,generated_at:'S1',summary:'',next_steps:[]},row||{}));
 const prev=answer;
 answer=(p,b)=>p.startsWith('/api/transcript')?Object.assign({generated_at:'S1',segments:[],speaker_options:[],people:[]},tr):(prev?prev(p,b):{});
 location.hash='m/'+encodeURIComponent(base);route();await flush();answer=prev;
}
"""


def _run(body, timeout=30, pre=""):
    # `pre` runs after the stub and before the page's scripts
    js = _STUB + pre + _js() + "\n;(async()=>{" + _PRE + body + "\n})().catch(e=>{console.log(JSON.stringify({threw:String(e&&e.stack||e)}));});"
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert not (isinstance(out, dict) and "threw" in out), out.get("threw")
    return out


# ================================================================ routing ===

@needs_node
def test_meeting_hash_reads_both_deep_link_forms():
    # ports test_classic_parse_hash_reads_the_seek_form: #m/<base> and
    # #m/<base>/t/<secs>, which /classic bookmarks carry across the redirect
    out = _run("""
console.log(JSON.stringify({a:parseMeetingHash('m/'+encodeURIComponent('Orchard 1')+'/t/61'),
 b:parseMeetingHash('m/'+encodeURIComponent('Orchard 1')),c:parseMeetingHash('home'),
 d:parseMeetingHash('m/%E0%A4%A'),e:parseMeetingHash('m/A%2FB%20%231')}));
""")
    assert out["a"] == {"base": "Orchard 1", "seek": 61}
    assert out["b"] == {"base": "Orchard 1", "seek": None}
    assert out["c"] is None and out["d"] is None
    assert out["e"] == {"base": "A/B #1", "seek": None}


@needs_node
def test_route_opens_the_page_seeks_a_deep_link_and_leaving_tears_it_down():
    out = _run("""
await openPage('M1',{segments:[seg(0,0,5,'S0','Speaker 1','hello'),seg(1,60,70,'S0','Speaker 1','the pear trees')]});
const opened={route:UI.route,base:MP&&MP.base,tab:MP.tab,loaded:MP.loaded,n:MP.segs.length,
 asked:calls.some(c=>c[0]==='/api/transcript?base=M1')};
location.hash='m/M1/t/61';route();
const seeked={tab:MP.tab,now:MP.nowIdx};
location.hash='recordings';route();
console.log(JSON.stringify({opened,seeked,after:{mp:MP,route:UI.route,mbase:UI.mbase}}));
""")
    assert out["opened"] == {"route": "meeting", "base": "M1", "tab": "summary",
                             "loaded": True, "n": 2, "asked": True}
    # a Said in hit lands on the Transcript tab at its line
    assert out["seeked"] == {"tab": "transcript", "now": 1}
    assert out["after"] == {"mp": None, "route": "recordings", "mbase": None}


# ================================================================= header ===

@needs_node
def test_header_names_three_people_and_counts_the_unnamed():
    # ports test_the_reader_header_names_three_people_and_counts_the_unnamed
    out = _run("""
await openPage('M1',{},{named:['Ada Quill','Bo Reyes','Cy Tran','Di Ober'],
 unnamed_clusters:{'Speaker 7':'S7','Speaker 9':'S9'},minutes:42});
const h=mHeadHTML(mRow());
console.log(JSON.stringify({h}));
""")
    h = out["h"]
    assert "Ada Quill, Bo Reyes, Cy Tran +1" in h and "Di Ober" not in h
    assert "42 min" in h and '<span class="attn-t nb">2 unnamed</span>' in h
    assert "Speaker 7" not in h        # never a raw roster label in the meta line


@needs_node
def test_header_shows_the_date_source_the_location_note_and_the_audio_note():
    # ports test_a_silent_meeting_body_and_the_audio_note and
    # test_the_meeting_header_holds_the_audio_note_and_both_loads_fill_it,
    # with the 4D additions: the date source and the location note
    out = _run("""
await openPage('M1',{audio_short_by:3},{date_source:'mtime',location_note:'Harbor Street Library',strict:true});
const a=mHeadHTML(mRow());
await openPage('M2',{},{date_source:'typed'});
const b=mHeadHTML(mRow());
await openPage('M3',{});
const c=mHeadHTML(mRow());
console.log(JSON.stringify({a,b,c}));
""")
    assert "Date uncertain" in out["a"] and "Date from the file time" in out["a"]
    assert "Recorded at Harbor Street Library" in out["a"]
    assert "The audio is 3 minutes shorter than the transcript: audio may be lost." in out["a"]
    assert "Strict" in out["a"]
    # the whole label per source: 'Date from typed in the name' read badly
    assert "Date uncertain" not in out["b"] and "<span>Date from the name</span>" in out["b"]
    assert "typed in the name" not in out["b"]
    # a meeting from before the keys shows neither
    assert "Date from" not in out["c"] and "Recorded at" not in out["c"]


@needs_node
def test_draft_chip_keep_and_an_edited_title_confirm_one_meeting_and_follow_a_restamp():
    # ports the finish strip's title step (fsAccept): a Keep on one opened
    # meeting posts accept_meeting, and a re-stamped folder takes the page along
    out = _run("""
await openPage('Old Name 10202026',{},{title_state:'provisional',route:'voice_memo'});
const chip=mHeadHTML(mRow());
answer=(p,b)=>p==='/api/accept_meeting'?{ok:true,base:'New Name 10202026'}:{};
A['m-title']();MP.titleDraft='New Name';
__els['m-title-in']={value:'New Name'};
A['m-title-save']();await flush();
const post=calls.filter(c=>c[0]==='/api/accept_meeting');
console.log(JSON.stringify({chip:chip.includes('Draft title')&&chip.includes('data-act="m-keep"'),post,
 base:MP&&MP.base,hash:location.hash,edit:MP&&MP.editTitle}));
""")
    assert out["chip"] is True
    assert out["post"] == [["/api/accept_meeting", {"base": "Old Name 10202026", "name": "New Name"}]]
    assert out["base"] == "New Name 10202026" and out["hash"] == "#m/New%20Name%2010202026"
    assert out["edit"] is False


@needs_node
def test_a_public_download_and_a_confirmed_title_wear_no_chip():
    out = _run("""
await openPage('P1',{},{title_state:'provisional',route:'public_download'});
const p=mHeadHTML(mRow());
await openPage('C1',{},{title_state:'confirmed'});
const c=mHeadHTML(mRow());
await openPage('L1',{},{needs_review:true});
const l=mHeadHTML(mRow());
console.log(JSON.stringify({p:p.includes('Draft title'),c:c.includes('Draft title'),l:l.includes('Draft title')}));
""")
    # a meeting from before the title rule falls back to needs_review
    assert out == {"p": False, "c": False, "l": True}


@needs_node
def test_a_silent_meeting_offers_no_summary_no_generate_and_no_ask():
    # ports test_a_silent_meeting_body_and_the_audio_note: a model given
    # nothing writes a plausible summary of a meeting that never happened
    out = _run("""
await openPage('Q1',{no_speech:true,segments:[]},{no_speech:true});
const m=mRow();
console.log(JSON.stringify({s:mSummaryHTML(m),tool:mTabToolHTML(m),ask:mAskHTML(m),lines:mLinesHTML(),notice:mNoticeHTML(m)}));
""")
    assert "No speech found" in out["s"] and "m-gen" not in out["s"]
    assert out["tool"] == ""
    assert "Nothing to ask about" in out["ask"] and "m-askq" not in out["ask"]
    assert "No speech found" in out["lines"] and out["notice"] == ""


@needs_node
def test_generate_is_a_post_and_the_old_summary_stays_while_writing():
    # ports test_meeting_page_uses_the_read_endpoints_verbatim for the
    # summary: /api/suggest is a POST since 4C, and a regenerate keeps the
    # old text on screen until the new one lands
    out = _run("""
await openPage('G1',{},{summary:'The old summary.'});
mGenerate();await tick();
const busy=mSummaryHTML(mRow());
const post=calls.filter(c=>c[0]==='/api/suggest');
await openPage('G2',{},{summary:''});
answer=p=>p==='/api/suggest'?{error:'local LLM not installed'}:{};
await mGenerate();await flush();
console.log(JSON.stringify({busy,post,err:mSummaryHTML(mRow())}));
""")
    assert out["post"] == [["/api/suggest", {"base": "G1"}]]
    assert "The old summary." in out["busy"] and "Writing a new summary" in out["busy"]
    assert "local LLM not installed" in out["err"] and 'data-act="m-gen"' in out["err"]


@needs_node
def test_the_notice_counts_the_voices_that_ask_and_hides_the_brief_ones():
    out = _run("""
await openPage('N1',{},{ask_clusters:{'Speaker 3':'S3','Speaker 4':'S4'},short_voices:2});
const a=mNoticeHTML(mRow());
await openPage('N2',{},{ask_clusters:{},short_voices:3});
const b=mNoticeHTML(mRow());
console.log(JSON.stringify({a,b}));
""")
    assert "2 voices unnamed." in out["a"] and "2 brief voices under a minute are hidden." in out["a"]
    assert 'data-act="voices"' in out["a"]
    # nothing asks: no notice at all
    assert out["b"] == ""


# ======================================================= legend and lines ===

_SPLIT = """
S={unknowns:[{uid:'U068',display:'Speaker 68',meetings:['SV']}],enrolled:[]};
await openPage('SV',{segments:[seg(0,0,30,'SA','Speaker 68','first half of a long turn'),
 seg(1,30,40,'SB','Speaker 68','the other cluster'),seg(2,40,50,'SC','Ana Ortiz','a named line'),
 seg(3,50,55,'SD','Voice 2','a local voice'),seg(4,55,58,'SE','Speaker 70','dismissed noise'),
 seg(5,58,60,'SF','Speaker 71','one-off')],
 speaker_options:[opt('SA','Speaker 68'),opt('SB','Speaker 68'),opt('SC','Ana Ortiz',{named:true}),
 opt('SD','Rae Lind',{named:true,local:true}),opt('SE','Speaker 70',{dismissed:true}),opt('SF','Speaker 71')]});
"""


@needs_node
def test_the_legend_shows_one_chip_per_voice_and_a_split_voice_once():
    # ports test_the_legend_shows_one_chip_per_voice and
    # test_a_split_voice_collapses_by_uid_else_by_display
    out = _run(_SPLIT + """
console.log(JSON.stringify({voices:MP.voices.map(v=>[v.kind,v.display,v.ids,v.uid]),legend:mLegendHTML()}));
""")
    assert out["voices"] == [["uid", "Speaker 68", ["SA", "SB"], "U068"],
                             ["plain", "Ana Ortiz", ["SC"], None],
                             ["local", "Rae Lind", ["SD"], None],
                             ["dismissed", "Speaker 70", ["SE"], None],
                             ["cluster", "Speaker 71", ["SF"], None]]
    assert out["legend"].count("Speaker 68") == 1


@needs_node
def test_a_dismissed_voice_stops_asking_and_offers_the_way_back_in_one_request():
    # ports test_legend_drops_the_question_mark_for_a_dismissed_voice and
    # test_a_dismissed_voice_offers_the_way_back
    out = _run(_SPLIT + """
const lg=mLegendHTML();
const chip=lg.slice(lg.indexOf('Speaker 70')-200,lg.indexOf('Speaker 70')+60);
const line=mLineHTML(MP.segs[4],4);
answer=()=>({ok:true});
A['m-restore']({dataset:{id:'SE'}});await flush();
console.log(JSON.stringify({chip,line,post:calls.filter(c=>c[0]==='/api/dismiss_voice')}));
""")
    assert 'data-act="m-restore"' in out["chip"] and "chip attn" not in out["chip"]
    # the line keeps its words, and its speaker no longer asks for a name
    assert 'data-act="m-voice"' not in out["line"] and "dismissed noise" in out["line"]
    assert out["post"] == [["/api/dismiss_voice", {"base": "SV", "speaker": "SE", "restore": True}]]


@needs_node
def test_a_locally_named_voice_stays_editable_and_gets_clear_name_not_not_a_real_speaker():
    # ports test_a_locally_named_chip_stays_editable,
    # test_a_locally_named_voice_gets_clear_name_not_not_a_real_speaker and
    # test_clear_name_posts_an_empty_local_name
    out = _run(_SPLIT + """
const lg=mLegendHTML();
vsOpenVoice('SD');
const h=vsHTML();
answer=p=>p==='/api/name'?{ok:true,local:true,applied:true}:{};
await vsClear();await flush();
console.log(JSON.stringify({chip:/data-act="m-voice" data-id="SD"/.test(lg),clear:h.includes('data-act="vs-clear"'),
 drop:h.includes('data-act="vs-drop"'),post:calls.filter(c=>c[0]==='/api/name'),closed:VS===null}));
""")
    assert out["chip"] and out["clear"] and not out["drop"]
    assert out["post"] == [["/api/name", {"meeting": "SV", "speaker": "SD", "name": "", "local": True}]]
    assert out["closed"] is True


@needs_node
def test_no_line_renders_a_raw_diarizer_id():
    # ports test_no_transcript_line_renders_a_raw_diarizer_id: the line shows
    # the server's display label, never the cluster id it is keyed by
    out = _run(_SPLIT + "console.log(JSON.stringify({h:mLinesHTML()}));")
    text = re.sub(r'data-id="[^"]*"', "", out["h"])
    for raw in ("SA", "SB", "SC", "SD", "SE", "SF"):
        assert not re.search(r">\s*" + raw + r"\s*<", text), raw
    assert "Speaker 68" in out["h"] and "Ana Ortiz" in out["h"]


_HOSTILE = "<img src=x onerror=alert(1)>"


@needs_node
def test_hostile_titles_names_lines_flags_and_answers_never_become_markup():
    # ports test_needs_name_row_escapes_hostile_filenames_and_speaker_names,
    # test_the_duplicate_line_survives_a_hostile_meeting_name, and
    # test_rows_escape_hostile_titles, across every 4D surface
    out = _run(f"""
const H={json.dumps(_HOSTILE)};
S={{unknowns:[{{uid:'U1',display:H,meetings:['B"><b>']}}],enrolled:[{{name:H}}],llm_available:true,llm_backend:'local'}};
await openPage('B"><b>',{{segments:[seg(0,0,9,'S1',H,H,[H])],speaker_options:[opt('S1',H)]}},
 {{title:H,named:[H],summary:H,next_steps:[H],location_note:H,date_source:H,unnamed_clusters:{{[H]:'S1'}},ask_clusters:{{[H]:'S1'}}}});
const parts=[mHeadHTML(mRow()),mNoticeHTML(mRow()),mSideHTML(mRow()),mSummaryHTML(mRow()),mLinesHTML(),mLegendHTML()];
mAskThread(MP.base).hist.push({{q:H,a:H}});parts.push(mAskHTML(mRow()));
MP.findState=null;mFindRun('img',true);parts.push(mLineHTML(MP.segs[0],0));
vsOpenVoice('S1');VS.looks=[{{name:H,score:.8}}];VS.lines=[{{start:1,dur:2,text:H}}];VS.preview=H;VS.err=H;
VS.warn={{kind:'warn',text:H}};parts.push(vsHTML());
answer=(p)=>p.startsWith('/api/review?')?{{items:[{{index:0,start:0,end:9,text:H,flags:[H],speaker:'S1',alt:[{{ours:H,theirs:H}}]}}]}}:{{}};
await mRevStart(null);parts.push(mRevBarHTML());parts.push(spkOptions(MP.spkOpts,[H],'S1'));
console.log(JSON.stringify({{parts}}));
""")
    for i, p in enumerate(out["parts"]):
        assert "<img" not in p and "<b>" not in p, (i, p[:300])
        assert 'onerror=alert(1)>' not in p.replace("&gt;", ""), i


# ============================================================ voice sheet ===

@needs_node
def test_the_sheet_puts_the_form_above_the_evidence():
    # ports test_naming_panel_puts_the_form_above_the_evidence
    out = _run(_SPLIT + """
vsOpenVoice('SA');VS.preview='Names this voice here only';VS.looks=[{name:'Mina Okafor',score:.84}];
VS.clip={start:4,end:9};VS.lines=[{start:4,dur:5,text:'what they said'}];VS.named=['Ana Ortiz'];
console.log(JSON.stringify({h:vsHTML()}));
""")
    h = out["h"]
    order = [h.index(k) for k in ('id="vs-in"', 'id="vs-preview"', "Looks like Mina Okafor",
                                  "Longest turn", "What this voice said", "Already recognized: Ana Ortiz",
                                  'data-act="vs-save"')]
    assert order == sorted(order)
    assert "Who is Speaker 68?" in h
    # the clip's start reads as where the turn is, beside the talk time:
    # 'Longest turn · 0:01' read as a length next to '1m 42s of talk'
    assert 'Longest turn at <span class="mono">0:04</span>' in h


@needs_node
def test_a_uid_that_is_no_longer_unknown_never_opens_a_dead_sheet():
    # ports test_stale_uid_chip_never_opens_a_dead_panel and
    # test_a_uid_that_is_no_longer_unknown_refreshes_instead_of_opening
    out = _run("""
S={unknowns:[{uid:'U050',display:'Speaker 50',meetings:['B']}],relabel_running:true,meetings:[],enrolled:[]};
let reloads=0;mReload=()=>{reloads++;};
vsOpenUid('U049','B');
const stale={vs:VS,toasts:toasts.slice(),refreshes};
S.relabel_running=false;vsOpenUid('U049','B');
const after=toasts.slice(-1);
vsOpenUid('U050','B');
console.log(JSON.stringify({stale,after,open:VS&&{uid:VS.list[0].uid,display:VS.list[0].display,base:VS.base}}));
""")
    assert out["stale"] == {"vs": None, "toasts": ["Already named. Names are being applied now."], "refreshes": 1}
    assert out["after"] == ["This voice was already named."]
    assert out["open"] == {"uid": "U050", "display": "Speaker 50", "base": "B"}


@needs_node
def test_save_captures_its_identity_before_the_await():
    # ports test_save_captures_its_identity_before_the_await: the save posts
    # the voice it was opened for, the open transcript rebuilds on success,
    # and a late answer never closes or advances a sheet that moved on
    out = _run("""
S={unknowns:[{uid:'U049',display:'Speaker 49',meetings:['LT']},{uid:'U050',display:'Speaker 50',meetings:['X']}],enrolled:[]};
await openPage('LT',{segments:[seg(0,0,9,'S49','Speaker 49','hello')],speaker_options:[opt('S49','Speaker 49')]});
let reloads=0;mReload=()=>{reloads++;};
let release;api=(p,b)=>{calls.push([p,b]);return p==='/api/name'?new Promise(r=>{release=r;}):new Promise(()=>{});};
vsOpenUid('U049','LT');VS.value='  Rowan Pike  ';
const p=vsSave();
vsOpenUid('U050','X');VS.value='half typed';
release({ok:true,targets:['LT'],applied:'LT'});await p;await flush();
console.log(JSON.stringify({post:calls.filter(c=>c[0]==='/api/name'),reloads,refreshes,
 still:VS&&{uid:VS.list[0].uid,value:VS.value,i:VS.i},busy:VBUSY,run:VRUN.resolved}));
""")
    assert out["post"] == [["/api/name", {"uid": "U049", "name": "Rowan Pike", "confirm": False,
                                          "meeting": "LT"}]]
    assert out["reloads"] == 1 and out["refreshes"] >= 1
    assert out["still"] == {"uid": "U050", "value": "half typed", "i": 0}
    assert out["busy"] is False and out["run"] == ["U049"]


@needs_node
def test_save_survives_a_fetch_that_never_answers():
    # ports test_save_survives_a_fetch_that_never_answers
    out = _run("""
S={unknowns:[{uid:'U049',display:'Speaker 49',meetings:['B']}],enrolled:[]};
api=()=>Promise.reject(new Error('network down'));
let reloads=0;mReload=()=>{reloads++;};
vsOpenUid('U049','B');VS.value='Rowan Pike';
await vsSave();
console.log(JSON.stringify({err:VS.err,busy:VBUSY,open:!!VS,reloads,h:vsHTML().includes('>Save<')}));
""")
    assert out == {"err": "Could not reach the panel.", "busy": False, "open": True,
                   "reloads": 0, "h": True}


@needs_node
def test_the_floor_refusal_offers_the_meeting_only_save():
    # ports test_the_floor_refusal_offers_the_meeting_only_save, and the
    # split-voice rule of test_a_meeting_only_name_reaches_every_cluster_of_a_split_voice
    out = _run("""
S={unknowns:[],enrolled:[]};
await openPage('F1',{segments:[seg(0,0,3,'SA','Speaker 5','one'),seg(1,3,6,'SB','Speaker 5','two')],
 speaker_options:[opt('SA','Speaker 5'),opt('SB','Speaker 5')]});
answer=(p,b)=>p==='/api/name'?(b.local?{ok:true,local:true,applied:true,targets:[]}:{ok:false,floor:true,error:'Too little speech to learn this voice.'}):{};
vsOpenVoice('SA');VS.value='Rowan Pike';
await vsSave();
const floor={warn:VS.warn,h:vsHTML()};
await vsSave(false,true);await flush();
console.log(JSON.stringify({floor,posts:calls.filter(c=>c[0]==='/api/name').map(c=>c[1])}));
""")
    assert out["floor"]["warn"]["kind"] == "floor"
    assert "Name for this meeting" in out["floor"]["h"] and 'data-act="vs-local"' in out["floor"]["h"]
    assert out["posts"] == [
        {"meeting": "F1", "speaker": "SA", "name": "Rowan Pike", "confirm": False, "local": False},
        {"meeting": "F1", "speaker": "SA", "name": "Rowan Pike", "confirm": False, "local": True,
         "speakers": ["SA", "SB"]}]


@needs_node
def test_the_floor_and_warn_confirms_cannot_double_save():
    # ports test_the_floor_and_warn_confirms_cannot_double_save
    out = _run("""
S={unknowns:[{uid:'U9',display:'Speaker 9',meetings:['B']}],enrolled:[]};
let release;api=(p,b)=>{calls.push([p,b]);return p==='/api/name'?new Promise(r=>{release=r;}):new Promise(()=>{});};
vsOpenUid('U9','B');VS.value='Rowan Pike';VS.warn={kind:'warn',text:'may be the wrong person'};
const a=vsSave(true);const b=vsSave(true);A['vs-force']();
const during={n:calls.filter(c=>c[0]==='/api/name').length,h:vsHTML()};
release({ok:true,targets:[]});await a;await b;
console.log(JSON.stringify({n:during.n,disabled:/data-act="vs-save" disabled/.test(during.h)}));
""")
    assert out == {"n": 1, "disabled": True}


@needs_node
def test_the_warn_says_both_scores_and_enter_never_confirms_it():
    out = _run("""
S={unknowns:[{uid:'U9',display:'Speaker 9',meetings:['B']}],enrolled:[]};
answer=p=>p==='/api/name'?{ok:false,warn:'suspect_sample',own:.41,cross:.77,cross_name:'Bo Reyes'}:{};
vsOpenUid('U9','B');VS.value='Ada Quill';
await vsSave();
const warn=VS.warn.text;
const n0=calls.filter(c=>c[0]==='/api/name').length;
vsKey({key:'Enter',target:{id:'vs-in',value:'Ada Quill',matches:()=>true},preventDefault(){}});await flush();
console.log(JSON.stringify({warn,more:calls.filter(c=>c[0]==='/api/name').length-n0}));
""")
    assert "0.41" in out["warn"] and "Bo Reyes better (0.77)" in out["warn"]
    assert out["more"] == 0


@needs_node
def test_save_says_how_many_other_meetings_are_updating_and_here_only_when_applied():
    # ports test_save_says_how_many_other_meetings_are_updating,
    # test_a_uid_save_sends_the_open_meeting_and_says_here_only_when_applied,
    # and test_a_uid_save_whose_open_meeting_was_only_queued_does_not_say_here
    out = _run("""
namedToast({targets:['A','B','C']},'H');
namedToast({targets:['A','B'],applied:'H'},'H');
namedToast({targets:['A'],applied:'H'},'H');
namedToast({targets:[],applied:'H'},'H');
namedToast({targets:['--all']},'H');
namedToast({targets:['A','B'],applied:null},'H');
console.log(JSON.stringify(toasts));
""")
    assert out == ["Named. Updating 3 meetings.", "Named here. Updating 2 other meetings.",
                   "Named here. Updating 1 other meeting.", "Named here.",
                   "Named. Updating all meetings.", "Named. Updating 2 meetings."]


@needs_node
def test_the_preview_is_asked_for_without_waiting_and_counts_the_other_meetings():
    # ports test_the_naming_panel_asks_for_a_preview_without_waiting_for_it and
    # test_the_preview_line_counts_the_other_meetings_not_this_one
    out = _run("""
S={unknowns:[{uid:'U7',display:'Speaker 7',meetings:['B']}],enrolled:[]};
vsOpenUid('U7','B');
const first={h:vsHTML().includes('id="vs-in"'),preview:VS.preview,asked:calls.filter(c=>c[0]==='/api/name_preview')};
console.log(JSON.stringify({first,lines:[vsPreviewLine({targets:['B','C','D'],here:'B'}),vsPreviewLine({targets:['B'],here:'B'}),
 vsPreviewLine({targets:['C'],here:null}),vsPreviewLine({targets:[]})]}));
""")
    assert out["first"] == {"h": True, "preview": None,
                            "asked": [["/api/name_preview", {"uid": "U7"}]]}
    assert out["lines"] == ["Names this voice here and in 2 other meetings", "Names this voice here only",
                            "Names this voice here and in 1 other meeting", "Names this voice here only"]


@needs_node
def test_a_preview_or_lines_for_a_voice_no_longer_shown_paint_nothing():
    out = _run("""
S={unknowns:[{uid:'U7',display:'Speaker 7',meetings:['B']},{uid:'U8',display:'Speaker 8',meetings:['B']}],enrolled:[]};
const rel=[];api=(p,b)=>{calls.push([p,b]);return new Promise(r=>rel.push([p,r]));};
vsOpenUid('U7','B');vsOpenUid('U8','B');
for(const [p,r] of rel.slice(0,3))r(p==='/api/name_preview'?{targets:['B','Z'],here:null,looks_like:[{name:'Old',score:.9}]}:
 p.startsWith('/api/voice_lines')?{lines:[{start:1,dur:9,text:'old voice'}],talk_secs:9}:{clips:[]});
await flush();
console.log(JSON.stringify({preview:VS.preview,looks:VS.looks,lines:VS.lines}));
""")
    assert out == {"preview": None, "looks": [], "lines": None}


@needs_node
def test_a_looks_like_chip_fills_the_name_box_and_saves_nothing():
    # ports test_a_looks_like_chip_fills_the_name_box_and_saves_nothing
    out = _run("""
S={unknowns:[{uid:'U7',display:'Speaker 7',meetings:['B']}],enrolled:[]};
vsOpenUid('U7','B');VS.looks=[{name:'Mina Okafor',score:.84},{name:'Luis Romero',score:.71}];
A['vs-fill']({dataset:{k:'1'}});
const k=vsKey({key:'1',target:{tagName:'BODY',matches:()=>false},preventDefault(){}});
console.log(JSON.stringify({value:VS.value,k,names:calls.filter(c=>c[0]==='/api/name').length,h:vsHTML().includes('value="Mina Okafor"')}));
""")
    assert out == {"value": "Mina Okafor", "k": True, "names": 0, "h": True}


@needs_node
def test_a_voice_lines_error_is_not_read_as_silence_and_a_split_voice_merges_its_lines():
    # ports test_voice_lines_error_is_not_read_as_silence and
    # test_a_split_voice_plays_and_quotes_from_all_its_clusters
    out = _run(_SPLIT + """
answer=(p)=>p.includes('speaker=SA')?{lines:[{start:3,dur:4,text:'short one'}],talk_secs:4}
 :p.includes('speaker=SB')?{lines:[{start:30,dur:10,text:'the long one'}],talk_secs:10}:{targets:[]};
vsOpenVoice('SA');await flush();
const merged={lines:VS.lines.map(l=>l.text),talk:VS.talk,asked:calls.filter(c=>c[0].startsWith('/api/voice_lines')).map(c=>c[0])};
answer=(p)=>p.startsWith('/api/voice_lines')?{error:'no transcript'}:{targets:[]};
vsOpenVoice('SF');await flush();
const err=VS.lines;
answer=(p)=>p.startsWith('/api/voice_lines')?{lines:[],talk_secs:0}:{targets:[]};
vsOpenVoice('SF');await flush();
console.log(JSON.stringify({merged,err,empty:vsHTML().includes('No transcript text was attributed to this voice.')}));
""")
    assert out["merged"]["lines"] == ["the long one", "short one"]     # longest first
    assert out["merged"]["talk"] == 14
    assert out["merged"]["asked"] == ["/api/voice_lines?base=SV&speaker=SA",
                                      "/api/voice_lines?base=SV&speaker=SB"]
    assert out["err"] is None         # an error paints nothing, never "no text"
    assert out["empty"] is True


@needs_node
def test_not_a_real_speaker_forgets_a_registry_voice_then_dismisses_its_clusters_in_one_request():
    # ports test_dismiss_posts_the_meeting_scoped_call_not_the_registry_tombstone
    # and fvForget's split-voice rule
    out = _run(_SPLIT + """
let reloads=0;mReload=()=>{reloads++;};
answer=()=>({ok:true});
vsOpenVoice('SA');await vsDrop();await flush();
const uid=calls.filter(c=>c[0]==='/api/forget'||c[0]==='/api/dismiss_voice');
calls.length=0;
vsOpenVoice('SF');await vsDrop();await flush();
const cl=calls.filter(c=>c[0]==='/api/forget'||c[0]==='/api/dismiss_voice');
console.log(JSON.stringify({uid,cl,reloads,resolved:VRUN.resolved}));
""")
    assert out["uid"] == [["/api/forget", {"uid": "U068"}],
                          ["/api/dismiss_voice", {"base": "SV", "speaker": "SA", "speakers": ["SA", "SB"]}]]
    # a cluster the registry never tracked: the meeting-scoped call only
    assert out["cl"] == [["/api/dismiss_voice", {"base": "SV", "speaker": "SF"}]]
    assert out["reloads"] == 2 and out["resolved"] == ["U068"]


@needs_node
def test_forget_dismiss_and_save_close_only_the_sheet_they_acted_on():
    # ports test_forget_and_dismiss_close_only_the_panel_they_acted_on and
    # test_a_save_for_the_voice_still_shown_does_close_the_panel
    out = _run(_SPLIT + """
mReload=()=>{};
let rel=[];api=(p,b)=>{calls.push([p,b]);return p==='/api/forget'||p==='/api/dismiss_voice'||p==='/api/name'?new Promise(r=>rel.push(r)):new Promise(()=>{});};
const drain=async v=>{for(let k=0;k<4;k++){const r=rel;rel=[];r.forEach(f=>f(v));await flush();}};
vsOpenVoice('SA');const d=vsDrop();
vsOpenVoice('SF');VS.value='typing';
await drain({ok:true});await d;
const other={open:!!VS,display:VS&&VS.list[0].display,value:VS&&VS.value};
VS.value='Rowan Pike';const s=vsSave(false,true);await drain({ok:true,targets:[]});await s;
console.log(JSON.stringify({other,closed:VS===null}));
""")
    assert out["other"] == {"open": True, "display": "Speaker 71", "value": "typing"}
    # the save was for the voice still shown, a single voice: the sheet closes
    assert out["closed"] is True


@needs_node
def test_the_sheet_token_includes_the_meeting_in_uid_mode():
    # ports test_the_panel_token_includes_the_meeting_in_uid_mode
    out = _run("""
S={unknowns:[{uid:'U7',display:'Speaker 7',meetings:['A','B']}],enrolled:[]};
const rel=[];api=(p,b)=>{calls.push([p,b]);return new Promise(r=>rel.push([p,r]));};
vsOpenUid('U7','A');const t1=vsToken();
vsOpenUid('U7','B');const t2=vsToken();
// the first meeting's lines land late: they must not paint the second
rel.filter(([p])=>p.includes('base=A')).forEach(([p,r])=>r({lines:[{start:1,dur:9,text:'from A'}],talk_secs:9}));
await flush();
console.log(JSON.stringify({differ:t1!==t2,lines:VS.lines}));
""")
    assert out == {"differ": True, "lines": None}


@needs_node
def test_an_archived_only_voice_points_at_restore_instead_of_a_dead_player():
    # ports test_an_archived_only_voice_points_at_restore_instead_of_a_dead_player
    out = _run("""
S={unknowns:[{uid:'U7',display:'Speaker 7',meetings:['Z']}],enrolled:[]};
answer=p=>p.startsWith('/api/voice_clips')?{clips:[],reason:'sources_archived'}:{targets:[]};
vsOpenUid('U7',null);await flush();
const a=vsHTML();
answer=p=>p.startsWith('/api/voice_clips')?{clips:[],reason:'sources_deleted'}:{targets:[]};
vsOpenUid('U7',null);await flush();
const d=vsHTML();
answer=p=>p.startsWith('/api/voice_clips')?{clips:[{meeting:'Z',start:12,dur:6}]}:{targets:[]};
vsOpenUid('U7',null);await flush();
console.log(JSON.stringify({a,d,clip:VS.clip}));
""")
    assert "archived. Restore one to hear it." in out["a"] and 'data-act="vs-play"' not in out["a"]
    assert "The source recordings were deleted." in out["d"]
    assert out["clip"]["src"] == "/api/snippet?meeting=Z&speaker=U7&secs=45"


@needs_node
def test_the_walk_starts_with_the_most_talk_and_never_revisits():
    # ports test_the_voice_queue_starts_with_the_most_talk_time_and_never_revisits,
    # test_a_named_voice_is_remembered_by_the_queue,
    # test_a_skipped_registry_voice_is_remembered_by_the_queue
    out = _run("""
const vs=[{id:'A',ids:['A'],display:'Speaker 1',uid:'U1'},{id:'B',ids:['B','B2'],display:'Speaker 2',uid:'U2'},
 {id:'C',ids:['C'],display:'Speaker 3',uid:null},{id:'D',ids:['D'],display:'Speaker 4',uid:'U4'}];
const segs=[seg(0,0,10,'A'),seg(1,10,30,'B'),seg(2,30,35,'B2'),seg(3,35,80,'C'),seg(4,80,82,'D')];
const order=voiceOrder(vs,segs,[],[]).map(v=>[v.display,v.talk,v.st]);
const after=voiceOrder(vs,segs,['U2'],['U1']).map(v=>[v.display,v.st]);
console.log(JSON.stringify({order,after}));
""")
    # a split voice talks for all of its clusters
    assert out["order"] == [["Speaker 3", 45, ""], ["Speaker 2", 25, ""],
                            ["Speaker 1", 10, ""], ["Speaker 4", 2, ""]]
    # named is gone from the walk; skipped arrives already skipped
    assert out["after"] == [["Speaker 3", ""], ["Speaker 1", "skip"], ["Speaker 4", ""]]


_QUEUE = """
S={unknowns:[{uid:'U1',display:'Speaker 1',meetings:['M1']},{uid:'U2',display:'Speaker 2',meetings:['M1','M2']}],enrolled:[],
 needs_you:[{kind:'voice',target:'M1'},{kind:'voice',target:'M2'}],
 meetings:[{base:'M2',title:'Second Meeting',date:'2026-10-21',minutes:9,named:[],ask_clusters:{'Speaker 2':'X2'},arrived_at:new Date().toISOString()}]};
await openPage('M1',{segments:[seg(0,0,100,'X1','Speaker 1','long talk'),seg(1,100,180,'X2','Speaker 2','more talk')],
 speaker_options:[opt('X1','Speaker 1'),opt('X2','Speaker 2')]},{ask_clusters:{'Speaker 1':'X1','Speaker 2':'X2'},short_voices:0,
 arrived_at:new Date().toISOString()});
"""


@needs_node
def test_name_voices_walks_this_meeting_then_offers_the_next_meeting():
    # ports test_name_n_voices_walks_the_voices_step_across_meetings and
    # test_name_n_voices_asks_about_exactly_n_voices
    out = _run(_QUEUE + """
mReload=()=>{};
answer=p=>p==='/api/name'?{ok:true,targets:[]}:{targets:[]};
A.voices();
const first={n:VS.list.length,who:VS.list.map(v=>v.display),i:VS.i,h:vsHTML().includes('voice 1 of 2')};
VS.value='Rowan Pike';await vsSave();await flush();
const second={i:VS.i,who:VS.list[VS.i].display};
vsSkip();
const end={done:VS.done,saved:VS.saved,skipped:VS.skipped,h:vsHTML()};
A['vs-nextm']({dataset:{base:'M2'}});
console.log(JSON.stringify({first,second,end:{done:end.done,saved:end.saved,skipped:end.skipped,
 counts:end.h.includes('1 named, 1 skipped'),next:end.h.includes('Next meeting: Second Meeting')},
 hash:location.hash,carry:VCARRY,seen:VRUN.seen,passed:VRUN.passed}));
""")
    # the most talk first: Speaker 1 talks 100s, Speaker 2 80s
    assert out["first"] == {"n": 2, "who": ["Speaker 1", "Speaker 2"], "i": 0, "h": True}
    assert out["second"] == {"i": 1, "who": "Speaker 2"}
    assert out["end"] == {"done": True, "saved": 1, "skipped": 1, "counts": True, "next": True}
    assert out["hash"] == "#m/M2" and out["carry"] == {"base": "M2"}
    assert out["seen"] == ["M1", "M2"] and out["passed"] == ["U2"]


@needs_node
def test_a_meeting_whose_every_voice_was_skipped_is_not_asked_again():
    # ports test_a_meeting_whose_every_voice_was_skipped_is_not_asked_again and
    # test_the_voice_queue_passes_by_voices_it_already_asked_about
    out = _run(_QUEUE + """
VRUN.passed.push('U1','U2');
const h=vsOpenMeeting({queue:true});
const handed={hash:location.hash,carry:VCARRY,vs:VS};
location.hash='m/M1';route();await flush();
// the reader asks again herself: the skips do not hold her back
vsOpenMeeting({});
console.log(JSON.stringify({h,handed,again:VS&&VS.list.map(v=>v.st)}));
""")
    assert out["h"] is True
    assert out["handed"] == {"hash": "#m/M2", "carry": {"base": "M2"}, "vs": None}
    assert out["again"] == ["", ""]


@needs_node
def test_the_sheet_keys_pick_save_skip_and_never_fire_while_typing():
    # ports test_the_voices_step_keys_pick_save_and_skip and
    # test_the_voices_step_keys_stand_back_while_the_drawer_is_open
    out = _run(_QUEUE + """
mReload=()=>{};
A.voices();VS.looks=[{name:'Mina Okafor',score:.8}];
const body={tagName:'BODY',matches:()=>false};const box={id:'vs-in',tagName:'INPUT',value:'',matches:()=>true};
const ev=(key,target)=>({key,target,preventDefault(){}});
const typed=vsKey(ev('s',box));const stillI=VS.i;
const one=vsKey(ev('1',body));const filled=VS.value;
vsKey(ev('s',body));const skipped=VS.i;
const other=vsKey(ev('g',body));
vsKey(ev('Escape',body));
console.log(JSON.stringify({typed,stillI,one,filled,skipped,other,closed:VS===null}));
""")
    # typing in the box: the key is the box's, nothing moves
    assert out["typed"] is False and out["stillI"] == 0
    assert out["one"] is True and out["filled"] == "Mina Okafor"
    assert out["skipped"] == 1
    # an open layer owns every plain key
    assert out["other"] is True and out["closed"] is True


@needs_node
def test_the_needs_you_name_button_opens_its_own_voice():
    # ports test_the_needs_you_name_button_opens_its_own_voice
    out = _run("""
S={unknowns:[{uid:'U4',display:'Speaker 4',meetings:['m1']},{uid:'U5',display:'Speaker 5',meetings:['m1']}],enrolled:[],
 meetings:[{base:'m1',title:'Alpha',date:'2026-10-01',minutes:9,named:[],ask_clusters:{}}],needs_you:[]};
const card=needsCard({kind:'voice',target:'m1',title:'Speaker 5',uid:'U5',detail:'9 min'},0);
const m=card.match(/data-uid="([^"]*)" data-base="([^"]*)"/);
A['name-voice']({dataset:{uid:m[1],base:m[2]}});
console.log(JSON.stringify({uid:VS.list[0].uid,base:VS.base}));
""")
    assert out == {"uid": "U5", "base": "m1"}


# ================================================================= review ===

_FLAGS = """
await openPage('R1',{segments:[seg(0,0,4,'S1','Speaker 1','clean line'),
 seg(3,4,9,'S1','Speaker 1','second flagged',['overlap']),seg(1,9,9.5,'S2','Speaker 2','so yeah',['overlap']),
 seg(7,10,14,'S2','Speaker 2','first flagged',['id_mismatch'])],
 speaker_options:[opt('S1','Ana Ortiz',{named:true}),opt('S2','Bo Reyes',{named:true})]},{flagged:2,flagged_minor:1});
// the server fronts substantial flags: the list arrives out of document order
const items=[{index:7,start:10,end:14,text:'first flagged',flags:['id_mismatch'],speaker:'S2',alt:[{ours:'first',theirs:'worst'}]},
 {index:3,start:4,end:9,text:'second flagged',flags:['overlap'],speaker:'S1'},
 {index:1,start:9,end:9.5,text:'so yeah',flags:['overlap'],speaker:'S2',minor:true}];
answer=(p,b)=>p.startsWith('/api/review?')?{items:items.map(x=>Object.assign({},x))}:p==='/api/review'?{ok:true}:{};
"""


@needs_node
def test_the_walk_reads_flags_in_document_order_from_a_check():
    # ports test_stepper_walks_flags_in_document_order and
    # test_stepper_orders_fabricated_out_of_order_flags_by_start_time
    out = _run(_FLAGS + """
await mRevStart(null);
const a=MP.rev.items.map(x=>x.index);
A['m-check']({dataset:{i:'3'}});await flush();
console.log(JSON.stringify({a,at:MP.rev.at,i:MP.rev.i,tab:MP.tab}));
""")
    assert out["a"] == [3, 1, 7]
    # Check opens the walk at its own line
    assert out["at"] == {"index": 7, "start": 10, "end": 14} and out["i"] == 2 and out["tab"] == "transcript"


@needs_node
def test_review_posts_carry_the_original_segment_index_and_its_start():
    # ports test_review_posts_carry_the_original_segment_index
    out = _run(_FLAGS + """
await mRevStart(null);
await mRevApply('accept');
__els['mc-text']={value:'edited words'};__els['mc-spk']={value:'S2'};
await mEditSave(3);
console.log(JSON.stringify(calls.filter(c=>c[0]==='/api/review').map(c=>c[1])));
""")
    assert out[0] == {"base": "R1", "index": 3, "start": 4, "end": 9, "action": "accept"}
    # array position 3 is the json index 7
    assert out[1] == {"base": "R1", "index": 7, "start": 10, "end": 14, "action": "edit",
                      "text": "edited words", "speaker": "S2"}


@needs_node
def test_bar_verdicts_advance_and_card_work_holds_the_position():
    # ports test_bar_verdicts_advance_and_card_work_holds_position
    out = _run(_FLAGS + """
await mRevStart(null);
await mRevApply('accept');
const afterAccept={i:MP.rev.i,at:MP.rev.at,n:MP.rev.items.length,hold:MP.rev.hold};
__els['mc-text']={value:'so yeah fixed'};__els['mc-spk']={value:'S2'};
await mRevSave();
const afterSave={i:MP.rev.i,at:MP.rev.at,n:MP.rev.items.length,hold:MP.rev.hold};
mRevGo(1);
const afterGo={i:MP.rev.i,at:MP.rev.at,hold:MP.rev.hold};
console.log(JSON.stringify({afterAccept,afterSave,afterGo,bar:mRevBarHTML()}));
""")
    assert out["afterAccept"] == {"i": 0, "at": {"index": 1, "start": 9, "end": 9.5}, "n": 2, "hold": False}
    # Save resolves the flag but holds: the bar says Saved, the spot stays
    assert out["afterSave"] == {"i": 0, "at": {"index": 1, "start": 9, "end": 9.5}, "n": 1, "hold": True}
    assert out["afterGo"] == {"i": 0, "at": {"index": 7, "start": 10, "end": 14}, "hold": False}


@needs_node
def test_the_walk_keys_guard_typing_layers_and_a_focused_button():
    # ports test_np_keys_step_flags_and_guard_typing and
    # test_stepping_fast_path_keys_guard_typing_and_popovers
    out = _run(_FLAGS + """
const body={tagName:'BODY',matches:()=>false};
const input={tagName:'INPUT',id:'x',matches:()=>true};
const btn={tagName:'BUTTON',matches:()=>false};
const ev=(key,target,o)=>Object.assign({key,target,preventDefault(){}},o||{});
const n0=mKey(ev('n',body));await flush();
const started=!!(MP.rev&&MP.rev.active);
const typingN=mKey(ev('n',input));
const i0=MP.rev.i;
mKey(ev('n',body));const i1=MP.rev.i;
mKey(ev('p',body));const i2=MP.rev.i;
const onBtn=mKey(ev('Enter',btn));
const posts0=calls.filter(c=>c[0]==='/api/review').length;
mKey(ev('a',body));await flush();
const posts1=calls.filter(c=>c[0]==='/api/review').length;
const cmdA=mKey(ev('a',body,{metaKey:true}));
mKey(ev('Escape',body));
console.log(JSON.stringify({n0,started,typingN,i0,i1,i2,onBtn,accepted:posts1-posts0,cmdA,exited:MP.rev===null}));
""")
    assert out["n0"] is True and out["started"] is True
    assert out["typingN"] is False
    assert (out["i0"], out["i1"], out["i2"]) == (0, 1, 0)
    assert out["onBtn"] is False and out["accepted"] == 1 and out["cmdA"] is False
    assert out["exited"] is True


@needs_node
def test_accept_all_remaining_is_an_honest_two_step():
    # ports test_accept_all_remaining_is_an_honest_house_two_step
    out = _run(_FLAGS + """
await mRevStart(null);
const before=mRevBarHTML();
A['rev-all']();const ask=mRevBarHTML();
const posted0=calls.filter(c=>c[0]==='/api/review').length;
A['rev-all-cancel']();const back=mRevBarHTML();
A['rev-all']();await mRevAllGo();await flush();
console.log(JSON.stringify({before:before.includes('Accept all 3 remaining'),ask:ask.includes('Keep all 3 as transcribed?')&&ask.includes('data-act="rev-all-go"'),
 posted0,back:back.includes('data-act="rev-all"'),posts:calls.filter(c=>c[0]==='/api/review').map(c=>c[1].index),rev:MP.rev,
 flags:MP.segs.filter(g=>g.flags.length).length}));
""")
    assert out["before"] and out["ask"] and out["back"]
    assert out["posted0"] == 0
    assert out["posts"] == [3, 1, 7] and out["rev"] is None and out["flags"] == 0


@needs_node
def test_the_uncertain_count_drops_as_flags_resolve_and_the_chip_leaves_at_zero():
    # ports test_flag_strip_counts_update_optimistically_and_clear_at_zero
    out = _run(_FLAGS + """
const c0=mFlagChipHTML();
await mRevStart(null);await mRevApply('accept');
const c1=mFlagChipHTML();
await mRevApply('accept');await mRevApply('accept');
console.log(JSON.stringify({c0,c1,c2:mFlagChipHTML(),rev:MP.rev}));
""")
    assert "Uncertain lines" in out["c0"] and '<span class="mono">3</span>' in out["c0"]
    assert '<span class="mono">2</span>' in out["c1"]
    assert out["c2"] == "" and out["rev"] is None


@needs_node
def test_the_uncertain_filter_shows_only_flagged_lines():
    out = _run(_FLAGS + """
const all=(mLinesHTML().match(/class="line/g)||[]).length;
A['m-onlyflag']();
const only=(mLinesHTML().match(/class="line/g)||[]).length;
console.log(JSON.stringify({all,only,chip:mFlagChipHTML().includes('aria-pressed="true"')}));
""")
    assert out == {"all": 4, "only": 3, "chip": True}


@needs_node
def test_a_structural_reload_keeps_the_stepping_position():
    # ports test_structural_reload_restores_scroll_and_stepping_position
    out = _run(_FLAGS + """
await mRevStart(null);await mRevApply('accept');   // now at index 1, start 9
// a split re-issues the segments: new indexes, the same times
answer=(p,b)=>p.startsWith('/api/transcript')?{generated_at:'S2',segments:[seg(0,0,4,'S1','Ana Ortiz','clean line'),
 seg(1,4,9,'S1','Ana Ortiz','second flagged'),seg(2,9,9.5,'S2','Bo Reyes','so yeah',['overlap']),seg(3,9.5,9.9,'S2','Bo Reyes','x'),
 seg(4,10,14,'S2','Bo Reyes','first flagged',['id_mismatch'])],speaker_options:[]}
 :p.startsWith('/api/review?')?{items:[{index:2,start:9,end:9.5,flags:['overlap'],text:'so yeah'},{index:4,start:10,end:14,flags:['id_mismatch'],text:'first flagged'}]}:{};
await mReload();
console.log(JSON.stringify({i:MP.rev.i,hold:MP.rev.hold,items:MP.rev.items.map(x=>x.index),stamp:MP.stamp}));
""")
    assert out == {"i": 0, "hold": True, "items": [2, 4], "stamp": "S2"}


@needs_node
def test_editing_everywhere_with_two_step_remove_split_and_insert_payloads():
    # ports test_segment_editing_everywhere_with_house_confirms
    out = _run(_FLAGS + """
answer=(p)=>p==='/api/review'?{ok:true,index:9}:{};
mReload=async()=>{};
__els['mc-err']={hidden:true,innerHTML:'',textContent:''};
mDeleteAsk(0);
const ask={html:__els['mc-err'].innerHTML,posted:calls.filter(c=>c[0]==='/api/review').length};
await mDeleteGo(0);
__els['mc-sa']={value:'S1'};__els['mc-sb']={value:'name:Cy Tran'};__els['mc-ta1']={value:'clean'};__els['mc-ta2']={value:'line'};
await mSplitSave(0);
__els['mc-nspk']={value:'S2'};__els['mc-ntext']={value:'a missed line'};__els['mc-nat']={value:'1:05'};
await mInsertSave();
console.log(JSON.stringify({ask,posts:calls.filter(c=>c[0]==='/api/review').map(c=>c[1])}));
""")
    assert "Remove this line?" in out["ask"]["html"] and 'data-act="mc-delgo"' in out["ask"]["html"]
    assert out["ask"]["posted"] == 0
    assert out["posts"] == [
        {"base": "R1", "action": "delete", "index": 0, "start": 0, "end": 4},
        {"base": "R1", "action": "split", "index": 0, "start": 0, "end": 4, "text_a": "clean", "text_b": "line",
         "speaker_a": "S1", "speaker_b": "name:Cy Tran"},
        {"base": "R1", "action": "insert", "start": 65, "end": 68, "speaker": "S2", "text": "a missed line"}]


@needs_node
def test_an_empty_new_person_box_refuses_instead_of_falling_back_to_the_first_speaker():
    # ports tests/test_server_security.py::test_speaker_select_restores_prior_selection_on_cancel:
    # a line is never silently given to option 0
    out = _run(_FLAGS + """
answer=(p)=>p==='/api/review'?{ok:true}:{};
__els['mc-err']={hidden:true,textContent:''};
__els['mc-spk']={value:'__new__'};__els['mc-spk-new']={value:'   '};__els['mc-text']={value:'x'};
await mEditSave(1);
const refused={err:__els['mc-err'].textContent,posts:calls.filter(c=>c[0]==='/api/review').length};
__els['mc-spk-new']={value:'Cy Tran'};
console.log(JSON.stringify({refused,named:mSpkValue('mc-spk'),opts:spkOptions([{id:'S1',display:'Ana'}],['Ana','Cy Tran'],'S1')}));
""")
    assert out["refused"] == {"err": "Pick or name the speaker first.", "posts": 0}
    assert out["named"] == "name:Cy Tran"
    assert 'value="S1" selected' in out["opts"] and 'value="name:Cy Tran"' in out["opts"]
    assert 'value="name:Ana"' not in out["opts"]       # a speaker already here is not offered twice


@needs_node
def test_accept_minor_never_rebuilds_a_card_mid_edit():
    out = _run(_FLAGS + """
await mRevStart(null);              // on index 3, a substantial flag
let cards=0;const real=mRevCard;mRevCard=()=>{cards++;real();};
await mRevMinor();
console.log(JSON.stringify({cards,i:MP.rev.i,items:MP.rev.items.map(x=>x.index),post:calls.filter(c=>c[0]==='/api/review').map(c=>c[1])}));
""")
    assert out["cards"] == 0 and out["items"] == [3, 7] and out["i"] == 0
    assert out["post"] == [{"base": "R1", "action": "accept_minor"}]


# ======================================================= relabel watcher ===

@needs_node
def test_a_finished_relabel_reloads_the_open_transcript_but_holds_while_mid_edit():
    # ports test_a_finished_relabel_rebuilds_the_open_transcript and
    # test_the_stamp_watcher_ignores_a_page_that_is_not_open
    out = _run("""
await openPage('W1',{segments:[seg(0,0,5,'S1','Speaker 1','old words')],speaker_options:[opt('S1','Speaker 1')]});
let reloads=0;mReload=()=>{reloads++;};
const m=mRow();
mRelabelWatch(m);const same=reloads;
m.generated_at='S2';
MP.findState={q:'half'};mRelabelWatch(m);const held=reloads;
MP.findState=null;VS={};mRelabelWatch(m);const sheet=reloads;VS=null;
mRelabelWatch(m);const done=reloads;
mRelabelWatch(m);const once=reloads;
// another meeting's stamp moving never reloads this page
mRelabelWatch({base:'Other',generated_at:'S9'});
console.log(JSON.stringify({same,held,sheet,done,once,other:reloads,stamp:MP.stamp}));
""")
    assert out == {"same": 0, "held": 0, "sheet": 0, "done": 1, "once": 1, "other": 1, "stamp": "S2"}


@needs_node
def test_a_reload_never_starts_another_reload():
    # a regression found while porting: the transcript's stamp and the row's
    # can differ until the next poll, and a reload that ran the watcher
    # reloaded itself without end
    out = _run("""
await openPage('W2',{generated_at:'S2',segments:[seg(0,0,5,'S1','Speaker 1','words')]});
const before=calls.filter(c=>c[0].startsWith('/api/transcript')).length;
answer=p=>p.startsWith('/api/transcript')?{generated_at:'S3',segments:[seg(0,0,5,'S1','Speaker 1','new words')],speaker_options:[]}:{};
await mReload();await flush();await flush();
console.log(JSON.stringify({more:calls.filter(c=>c[0].startsWith('/api/transcript')).length-before,stamp:MP.stamp}));
""")
    assert out == {"more": 1, "stamp": "S3"}


@needs_node
def test_a_rename_that_restamps_the_folder_drops_the_old_audio_and_reloads():
    out = _run("""
await openPage('Old 10202026',{segments:[seg(0,0,5,'S1','Speaker 1','words')]},{audio:'/x/old.m4a'});
mAudio();const old=MP.audio;
S.meetings.push({base:'New 10202026',title:'New',date:'2026-10-20',minutes:1,named:[],ask_clusters:{},audio:'/x/new.m4a'});
answer=p=>p.startsWith('/api/transcript')?{generated_at:'S1',segments:[],speaker_options:[]}:{};
mFollow('Old 10202026','New 10202026');await flush();
console.log(JSON.stringify({paused:old.paused,audio:MP.audio,base:MP.base,hash:location.hash,
 fetched:calls.filter(c=>c[0]==='/api/transcript?base=New%2010202026').length}));
""")
    assert out == {"paused": True, "audio": None, "base": "New 10202026",
                   "hash": "#m/New%2010202026", "fetched": 1}


@needs_node
def test_a_typed_question_survives_a_forced_repaint_and_the_sheet_swallows_cmd_k():
    out = _run("""
await openPage('K2',{segments:[seg(0,0,5,'S1','Speaker 1','words')],speaker_options:[opt('S1','Speaker 1')]});
const box={id:'m-askq',matches:()=>true};
__els['tp-ask']=Object.assign({},{innerHTML:'typed panel',contains:x=>x===box});
document.activeElement=box;
mSync(mRow(),true);
const kept=__els['tp-ask'].innerHTML;
document.activeElement=null;
vsOpenVoice('S1');
let pal=0;const real=A.palette;A.palette=()=>{pal++;};
const took=vsKey({key:'k',metaKey:true,target:{tagName:'BODY',matches:()=>false},preventDefault(){}});
console.log(JSON.stringify({kept,took,pal}));
""")
    assert out == {"kept": "typed panel", "took": True, "pal": 0}


# ==================================================================== ask ===

@needs_node
def test_ask_is_a_post_with_the_last_three_good_turns():
    # ports test_meeting_page_ask_stays_a_post_with_the_server_contract
    out = _run("""
await openPage('K1',{segments:[seg(0,0,5,'S1','Speaker 1','words')]});
const t=mAskThread('K1');
t.hist=[{q:'q1',a:'a1'},{q:'q2',a:'x',err:true},{q:'q3',a:'a3'},{q:'q4',a:'a4'},{q:'q5',a:'a5'}];
answer=p=>p==='/api/ask'?{ok:true,answer:'an answer',truncated:true}:{};
await mAskSend('What was decided?');
console.log(JSON.stringify({post:calls.filter(c=>c[0]==='/api/ask'),last:t.hist[t.hist.length-1],note:t.note}));
""")
    assert out["post"] == [["/api/ask", {"base": "K1", "question": "What was decided?",
                                         "history": [{"q": "q3", "a": "a3"}, {"q": "q4", "a": "a4"},
                                                     {"q": "q5", "a": "a5"}]}]]
    assert out["last"] == {"q": "What was decided?", "a": "an answer"}
    assert "middle was sampled" in out["note"]


@needs_node
def test_a_busy_assistant_keeps_the_typed_question_and_says_why():
    out = _run("""
await openPage('K1',{segments:[seg(0,0,5,'S1','Speaker 1','words')]});
answer=p=>p==='/api/ask'?{error:'The local model is busy with another summary or question. Try again in a minute.'}:{};
await mAskSend('Who owes what?');
const t=mAskThread('K1');const h=mAskHTML(mRow());
S.llm_available=false;const off=mAskHTML(mRow());
console.log(JSON.stringify({hist:t.hist.length,draft:t.draft,h:h.includes('value="Who owes what?"')&&h.includes('busy with another'),
 off:off.includes('Ask needs the local model installed.')&&/data-act="m-ask" disabled/.test(off)}));
""")
    assert out == {"hist": 0, "draft": "Who owes what?", "h": True, "off": True}


# =================================================================== keys ===

@needs_node
def test_meeting_keys_switch_tabs_step_recordings_and_escape_back_to_the_list():
    out = _run("""
S={meetings:[{base:'A',title:'Alpha',date:'2026-10-03',minutes:1,named:[],ask_clusters:{}},
 {base:'B',title:'Beta',date:'2026-10-02',minutes:1,named:[],ask_clusters:{}},
 {base:'C',title:'Gamma',date:'2026-10-01',minutes:1,named:[],ask_clusters:{}}]};
await openPage('B',{segments:[seg(0,0,5,'S1','Speaker 1','words')]});
const body={tagName:'BODY',matches:()=>false};const ev=(key,o)=>Object.assign({key,target:body,preventDefault(){}},o||{});
mKey(ev('2'));const t2=MP.tab;mKey(ev('3'));const t3=MP.tab;
const typing=mKey({key:'1',target:{tagName:'INPUT',matches:()=>true},preventDefault(){}});
mKey(ev(']'));const next=location.hash;
location.hash='m/B';route();await flush();
mKey(ev('['));const prev=location.hash;
location.hash='m/B';route();await flush();
mKey(ev('Escape'));
console.log(JSON.stringify({t2,t3,typing,next,prev,back:location.hash}));
""")
    assert out["t2"] == "transcript" and out["t3"] == "ask" and out["typing"] is False
    # the Recordings list's order: newest first
    assert out["next"] == "#m/C" and out["prev"] == "#m/A"
    assert out["back"] == "#recordings"


def test_the_key_sheet_lists_the_meeting_and_sheet_keys():
    js = (STATIC / "next.js").read_text(encoding="utf-8")
    keys = re.search(r"const KEYS=\[(.*?)\];", js, re.S).group(1)
    for k in ("'1 2 3'", "'[ ]'", "'Space'", "'v'", "'n p'", "'a u'", "'Cmd F'", "'Cmd Enter'",
              "'1 2 3 or s'"):
        assert k in keys, k


@needs_node
def test_find_marks_never_land_inside_an_entity_and_count_their_hits():
    out = _run("""
await openPage('F1',{segments:[seg(0,0,5,'S1','Speaker 1','R&D plan and R&D budget'),seg(1,5,9,'S1','Speaker 1','a<b')]});
mFindRun('r&d',true);const a=mLineHTML(MP.segs[0],0);const n=MP.findState.hits.length;
mFindRun('amp',true);const amp=MP.findState.hits.length;
mFindRun('lt',true);const lt=MP.findState.hits.length;
console.log(JSON.stringify({a,n,amp,lt}));
""")
    assert '<mark class="cur">R&amp;D</mark> plan and <mark>R&amp;D</mark> budget' in out["a"]
    assert out["n"] == 2 and out["amp"] == 0 and out["lt"] == 0


# ================================================================= server ===

@pytest.fixture
def running_server(sandbox):
    httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    t.join(timeout=2)


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    out = (r.status, r.read())
    conn.close()
    return out


def test_meeting_rows_carry_the_location_note(running_server):
    from conftest import mfile
    d = {"source_file": "x.m4a", "duration_sec": 300.0, "date": "2026-11-02",
         "speakers": [], "segments": [], "location_note": "Harbor Street Library"}
    mfile("Library Walk 11022026", ".json").write_text(json.dumps(d))
    status, body = _get(running_server, "/api/state")
    rows = {m["base"]: m for m in json.loads(body)["meetings"]}
    assert rows["Library Walk 11022026"]["location_note"] == "Harbor Street Library"


def _below_floor_meeting(base):
    """A meeting shaped like the real one that showed "SPEAKER_08": every
    segment's speaker is on the roster, but the voices below the minting
    floor (no uid) carry no `display` on their segments, because the
    pipeline stamps a segment's display only from a name or a uid. The
    roster reads "Voice 9" (build_speakers' fallback). Beside it: a named
    person, a registry voice, a dismissed below-floor voice, and a
    meeting-local name."""
    from conftest import mfile
    roster = [
        {"id": "SPEAKER_00", "name": "Grace Lindqvist", "global_id": None,
         "display": "Grace Lindqvist"},
        {"id": "SPEAKER_01", "name": None, "global_id": "U070", "display": "Speaker 70"},
        {"id": "SPEAKER_03", "name": None, "global_id": None, "display": "Voice 4"},
        {"id": "SPEAKER_05", "name": "Elena Ruiz", "global_id": None,
         "display": "Elena Ruiz", "local": True},
        {"id": "SPEAKER_08", "name": None, "global_id": None, "display": "Voice 9"},
    ]
    stamped = {"SPEAKER_00", "SPEAKER_01", "SPEAKER_05"}
    segs = []
    for i, s in enumerate(roster):
        seg = {"start": i * 10.0, "end": i * 10.0 + 4.0, "speaker": s["id"],
               "name": s["name"], "text": f"budget line number {i} spoken"}
        if s["id"] in stamped:
            seg["display"] = s["display"]
        segs.append(seg)
    segs.append({"start": 60.0, "end": 62.0, "speaker": "SPEAKER_08", "name": None,
                 "text": "one more from nine"})
    d = {"source_file": f"{base}.m4a", "duration_sec": 70.0, "date": "2026-09-23",
         "speakers": roster, "segments": segs, "words": [],
         "dismissed_voices": ["SPEAKER_03"], "local_names": {"SPEAKER_05": "Elena Ruiz"}}
    mfile(base, ".json").write_text(json.dumps(d))
    mfile(base, ".txt").write_text("stub")


def test_transcript_and_search_label_a_below_floor_voice_by_its_roster_display(running_server):
    """The server half of the raw-id fix (the page half is
    test_no_line_renders_a_raw_diarizer_id). On a real meeting one line read
    "SPEAKER_08" while the legend showed "Voice 9": /api/transcript labelled a
    line by the segment's own display, else its name, else the RAW id, and a
    voice below the minting floor never gets a display on its segments. A
    line now carries its roster display in `who`, and a search hit too."""
    from urllib.parse import quote
    base = "Below Floor Mtg 09232026"
    _below_floor_meeting(base)
    status, body = _get(running_server, "/api/transcript?base=" + quote(base))
    assert status == 200
    tr = json.loads(body)
    assert [g["who"] for g in tr["segments"]] == [
        "Grace Lindqvist", "Speaker 70", "Voice 4", "Elena Ruiz", "Voice 9", "Voice 9"]
    assert not any(re.fullmatch(r"SPEAKER_\d+", g["who"]) for g in tr["segments"])
    assert "Voice 9" in tr["speakers"]
    status, body = _get(running_server, "/api/search?q=" + quote("from nine"))
    assert status == 200
    assert [h["who"] for h in json.loads(body)["hits"]] == ["Voice 9"]


def test_every_page_route_is_origin_gated(running_server):
    for path, ok in (("/", 200), ("/next", 200), ("/classic", 302)):
        assert _get(running_server, path)[0] == ok, path
        assert _get(running_server, path, {"Origin": "https://evil.example.com"})[0] == 403, path


def test_the_joined_script_is_three_plain_files_with_no_modules():
    for f in JS_FILES:
        src = (STATIC / f).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|export)\b", src, re.M), f
        assert "—" not in src, f
    assert srv._NEXT_PARTS == ("next.html", "next.css") + JS_FILES


# ======================================================= Phase 4D QA fixes ===

# the page's document listeners, kept so a test can fire a key at them
_LISTEN = r"""
const __L={};document.addEventListener=(t,f)=>{(__L[t]=__L[t]||[]).push(f);};
const fire=(t,e)=>(__L[t]||[]).forEach(f=>f(e));
"""


@needs_node
def test_enter_on_a_palette_hit_in_the_open_meeting_closes_the_palette():
    out = _run("""
await openPage('P1',{segments:[seg(0,0,5,'S1','Speaker 1','hello'),seg(1,60,70,'S1','Speaker 1','pear trees')]});
__els['pop']=_mk({hidden:true});__els['layers']=_mk();
let routed=0;const realRoute=route;route=()=>{routed++;realRoute();};
const enter=href=>{__els['pal-res .hit.sel']=_mk({getAttribute:()=>href});
 UI.palette=true;layersHTML();
 fire('keydown',{key:'Enter',target:_mk({id:'pal-in',matches:()=>true}),preventDefault(){}});
 return {palette:UI.palette,layer:__els['layers'].innerHTML.includes('palette'),routed};};
// the title hit of the open meeting: the hash does not change
const title=enter('#m/P1');
// a Said in hit: the hash changes, and hashchange takes the early return
const said=enter('#m/P1/t/60');location.hash='m/P1/t/60';UI.palette=true;layersHTML();route();
console.log(JSON.stringify({title,said,afterRoute:{palette:UI.palette,layer:__els['layers'].innerHTML.includes('palette'),now:MP.nowIdx}}));
""", pre=_LISTEN)
    assert out["title"] == {"palette": False, "layer": False, "routed": 1}
    assert out["said"]["palette"] is False and out["said"]["layer"] is False
    assert out["afterRoute"] == {"palette": False, "layer": False, "now": 1}


@needs_node
def test_a_refresh_during_a_poll_waits_for_a_fresh_poll():
    # a save that awaits refresh() while the 2-second poll is in flight must
    # read state from a request that started after the save
    js = _STUB + _js() + r"""
;(async()=>{
for(let k=0;k<8;k++)await new Promise(r=>setTimeout(r,0));
let n=0;const held=[];
fetch=()=>{n++;const k=n;return new Promise(r=>held.push(()=>r({ok:true,json:async()=>({meetings:[],gen:k})})));};
const tick=()=>new Promise(r=>setTimeout(r,0));
const p1=refresh();await tick();
const p2=refresh(),p3=refresh();await tick();
const during=n;
held[0]();await p1;const afterFirst=S.gen;await tick();
const started=n;held[1]();await p2;await p3;
console.log(JSON.stringify({during,afterFirst,started,final:S.gen,same:p2===p3}));
})().catch(e=>console.log(JSON.stringify({threw:String(e&&e.stack||e)})));
"""
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    # one request at a time, the two late callers share ONE follow-up poll,
    # and they resolve only on its answer
    assert out == {"during": 1, "afterFirst": 1, "started": 2, "final": 2, "same": True}


@needs_node
def test_a_said_in_link_keeps_a_tenth_of_a_second():
    out = _run("""
console.log(JSON.stringify({a:meetingHref('L1',11.64),b:meetingHref('L1',0),c:parseMeetingHash(meetingHref('L1',11.64).slice(1))}));
""")
    assert out == {"a": "#m/L1/t/11.6", "b": "#m/L1/t/0", "c": {"base": "L1", "seek": 11.6}}


@needs_node
def test_the_dock_shows_the_length_before_the_first_play():
    out = _run("""
__els['dock']=_mk();__els['a-dur']=_mk({textContent:'0:00'});__els['a-seek']=_mk({max:0});
await openPage('D1',{segments:[seg(0,0,5,'S1','Speaker 1','words')]},{audio:'/x/d1.m4a',duration_sec:754});
mDock();
console.log(JSON.stringify({dur:__els['a-dur'].textContent,max:__els['a-seek'].max,player:MP.audio}));
""")
    assert out == {"dur": "12:34", "max": 754, "player": None}


@needs_node
def test_the_speakers_name_button_opens_the_voice_in_a_meeting_and_the_clip_says_its_title():
    out = _run("""
S={unknowns:[{uid:'U7',display:'Speaker 7',meetings:['f1 01152026','f2 01162026']}],enrolled:[],
 meetings:[{base:'f1 01152026',title:'Synthetic Demo',date:'2026-01-15',minutes:9,named:[],ask_clusters:{}},
  {base:'f2 01162026',title:'Synthetic Other',date:'2026-01-16',minutes:9,named:[],ask_clusters:{}}],needs_you:[]};
UI.route='speakers';
const html=speakersView();
const m=html.match(/data-act="name-voice" data-uid="([^"]*)" data-base="([^"]*)"/);
answer=p=>p.startsWith('/api/voice_lines')?{lines:[{start:8,dur:4,text:'synthetic words'}],talk_secs:12}
 :p.startsWith('/api/voice_clips')?{clips:[{meeting:'f2 01162026',start:3,dur:5}]}:{targets:[]};
A['name-voice']({dataset:{uid:m[1],base:m[2]}});await flush();
const where=vsClipWhere(),lines=(VS.lines||[]).length;
VS.clip.meeting='f1 01152026';const self=vsClipWhere();
VS.clip.meeting='gone 01012026';const gone=vsClipWhere();
console.log(JSON.stringify({base:VS.base,lines,where,self,gone}));
""")
    assert out == {"base": "f1 01152026", "lines": 1, "where": "Synthetic Other", "self": "", "gone": ""}


@needs_node
def test_a_hidden_unknown_keeps_its_uid_on_the_page():
    # ports test_row_unknown_uid_matches_archived_voices_not_just_live_ones:
    # hiding an unknown stops the nagging, never the naming
    out = _run("""
S={unknowns:[{uid:'U1',display:'Voice 9',meetings:['base1'],archived:true},
 {uid:'U2',display:'Voice 10',meetings:['base1'],archived:false}]};
console.log(JSON.stringify([mUnknownUid('Voice 9','base1'),mUnknownUid('Voice 10','base1'),
 mUnknownUid('Voice 99','base1'),mUnknownUid('Voice 9','otherbase')]));
""")
    assert out == ["U1", "U2", None, None]


def test_long_unbroken_text_wraps_inside_the_meeting_page():
    """A decision holding a 200-character URL, or an open question of 300
    letters with no space, widened the grid to 3,500 px: the page scrolled
    sideways at 1280 px and 420 px and the gist went off screen. The reading
    text breaks anywhere when it must, and a list item may shrink below its
    longest word. Checked in headless Chrome against a demo_seed panel."""
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "gui/static/next.css").read_text()
    rule = re.search(r"^\.reading\{([^}]*)\}", css, re.M).group(1)
    assert "overflow-wrap:anywhere" in rule and "min-width:0" in rule
    assert re.search(r"^\.steps>li\{[^}]*min-width:0", css, re.M)


@needs_node
def test_the_second_engine_hint_ends_its_sentence_once():
    """The card wrote a period after the closing quote, so a line that
    already ended in one read 'two.”.'."""
    out = _run("""
await openPage('R2',{segments:[seg(0,0,4,'S1','Speaker 1','The target was three percent, not two.',['possible:engine_disagreement']),
 seg(1,4,8,'S1','Speaker 1','we agreed on it',['possible:engine_disagreement'])],
 speaker_options:[opt('S1','Ana Ortiz',{named:true})]},{flagged:2});
const items=[{index:0,start:0,end:4,text:'The target was three percent, not two.',flags:['possible:engine_disagreement'],speaker:'S1',
  alt:[{ours:'The target was three percent, not two.',theirs:'The target was two percent.'}]},
 {index:1,start:4,end:8,text:'we agreed on it',flags:['possible:engine_disagreement'],speaker:'S1',
  alt:[{ours:'we agreed on it',theirs:'we agreed on that'}]}];
answer=(p,b)=>p.startsWith('/api/review?')?{items:items.map(x=>Object.assign({},x))}:{};
const made=[];const oc=document.createElement;document.createElement=()=>{const e=oc();made.push(e);return e;};
await mRevStart(null);
const first=made.map(e=>e.innerHTML).filter(h=>h.includes('second engine')).pop();
mRevGo(1);await flush();
const second=made.map(e=>e.innerHTML).filter(h=>h.includes('second engine')).pop();
console.log(JSON.stringify({first,second}));
""")
    assert "not two.”." not in out["first"]
    assert "where this says “The target was three percent, not two.” <button" in out["first"]
    assert "where this says “we agreed on it”. <button" in out["second"]


@needs_node
def test_a_title_save_toasts_the_note_the_server_sends():
    """A future date typed in a title stays in the title, and the reply
    says so in `note`. The toast said only 'Title saved.' or 'Saved.'."""
    out = _run("""
await openPage('2026-08-20 Planning',{},{title_state:'confirmed'});
answer=(p,b)=>p==='/api/accept_meeting'?{ok:true,base:'2026-08-20 Planning',note:'NOTE-A'}:{};
A['m-title']();__els['m-title-in']={value:'2027-03-14 Retreat'};
A['m-title-save']();await flush();
const a=toasts.slice(-1)[0];
answer=(p,b)=>p==='/api/rename'?{ok:true,base:'2026-08-20 Planning',note:'NOTE-B'}:{};
__els['pf-in']={value:'2027-03-14 Retreat',dataset:{kind:'rename',base:'2026-08-20 Planning'}};
__els['pf-err']={hidden:true,textContent:''};
await A['pf-save']();await flush();
const b=toasts.slice(-1)[0];
answer=(p,b)=>p==='/api/rename'?{ok:true,base:'2026-08-20 Planning'}:{};
await A['pf-save']();await flush();
console.log(JSON.stringify({a,b,c:toasts.slice(-1)[0]}));
""")
    assert out == {"a": "NOTE-A", "b": "NOTE-B", "c": "Saved."}


@needs_node
def test_the_meeting_page_has_one_h1_and_it_stays_on_a_phone():
    """Every route had an h1 but the meeting page, whose title was a bare
    button. The title button now sits in an h1, and the phone rule that
    hides a route's h1 (the top bar names the route there) keeps this one."""
    out = _run("""
await openPage('2026-10-20 Board Prep',{},{title:'Board Prep'});
const h=mHeadHTML(mRow());
MP.editTitle=true;const e=mHeadHTML(mRow());MP.editTitle=false;
console.log(JSON.stringify({h,e}));
""")
    assert out["h"].count("<h1") == 1
    assert re.search(r'<h1 class="m-h1"><button class="m-title"[^>]*>Board Prep</button></h1>', out["h"])
    assert 'id="m-title-in"' in out["e"]
    css = (STATIC / "next.css").read_text(encoding="utf-8")
    phone = css[css.index("@media (max-width:899px)"):]
    phone = phone[:phone.index("\n}\n")]
    assert "h1{display:none}" in phone and "h1.m-h1{display:flex}" in phone
