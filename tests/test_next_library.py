"""The library views of the new shell (gui/static/next-library.js): Groups
(plan Phase 4E). The behavior runs in Node against the joined script, with
the stub DOM and fake api() of tests/test_next_meeting.py. Synthetic data
only."""
from test_next_meeting import _run, needs_node

_VIEW = """
for(const id of ['view','side','topbar','layers','bulk','toast'])__els[id]=__els[id]||_mk({id});
"""

_GROUPS = _VIEW + """
S=Object.assign({meetings:[],needs_you:[],unknowns:[],enrolled:[],llm_available:true,llm_backend:'local'},S||{});
const row=(base,title,o)=>Object.assign({base,title,date:'2026-10-20',minutes:30,named:[],ask_clusters:{},unnamed_clusters:{},summary:'',next_steps:[]},o||{});
S.meetings=[row('GW1','Grant Writing',{group:'gA',group_name:'Grant Writing'}),row('GW2','Grant Writing',{group:'gA',group_name:'Grant Writing'}),
 row('BP1','Board Prep',{group:'gB',group_name:'Board Prep <b>'}),row('BP2','Board Prep',{group:'gB',group_name:'Board Prep <b>'}),row('X1','Snow Day')];
const G=[{key:'gA',kind:'title',name:'Grant Writing',why:'Titles share the words grant, writing',count:2,hours:1.5,bases:['GW1','GW2']},
 {key:'gB',kind:'title',name:'Board Prep <b>',why:'Titles share the words board, prep',count:2,hours:1,bases:['BP1','BP2']}];
answer=(p,b)=>p==='/api/groups'?{groups:G}:(p==='/api/group_override'?{ok:true,not_in:[b.group]}:{ok:true});
"""


@needs_node
def test_groups_route_lists_groups_opens_one_and_counts_the_open():
    out = _run(_GROUPS + """
location.hash='groups';route();await flush();
const first=document.querySelector('#view').innerHTML;
location.hash='groups/gB';route();await flush();
const second=document.querySelector('#view').innerHTML;
console.log(JSON.stringify({route:UI.route,first,second,
 usage:calls.filter(c=>c[0]==='/api/usage').map(c=>c[1].event),
 loads:calls.filter(c=>c[0]==='/api/groups').length}));
""")
    assert out["route"] == "groups"
    assert "Grant Writing" in out["first"] and "2 recordings" in out["first"]
    # a hostile group name is escaped everywhere it shows
    assert "Board Prep &lt;b&gt;" in out["second"] and "<b>" not in out["second"]
    assert 'aria-current="true"' in out["second"]
    # one count line per group opened, never a title or key
    assert out["usage"] == ["group_open", "group_open"]
    assert out["loads"] >= 1


@needs_node
def test_not_part_of_this_group_posts_the_override_and_offers_undo():
    out = _run(_GROUPS + """
location.hash='groups/gA';route();await flush();
const menu=rowMenu('GW2');
await A['g-out']({dataset:{base:'GW2',k:'gA'}});await flush();
const after=document.querySelector('#view').innerHTML;
await A['g-undo']();await flush();
const posts=calls.filter(c=>c[0]==='/api/group_override').map(c=>c[1]);
console.log(JSON.stringify({menu,after,posts,undo:LIB.gundo}));
""")
    assert "Not part of this group" in out["menu"]
    assert out["posts"] == [{"base": "GW2", "group": "gA"},
                            {"base": "GW2", "group": "gA", "undo": True}]
    assert "no longer part of this group" in out["after"] and "Undo" in out["after"]
    assert out["undo"] is None


@needs_node
def test_a_row_shows_its_group_and_an_ungrouped_row_offers_no_correction():
    out = _run(_GROUPS + """
console.log(JSON.stringify({row:rowHTML(byBase('GW1')),menu:rowMenu('X1')}));
""")
    assert 'title="Group">Grant Writing<' in out["row"]
    assert "Not part of this group" not in out["menu"]


@needs_node
def test_no_groups_shows_the_empty_state():
    out = _run(_VIEW + """
answer=(p)=>p==='/api/groups'?{groups:[]}:{};
S=Object.assign({meetings:[],needs_you:[]},S||{});
location.hash='groups';route();await flush();
console.log(JSON.stringify({v:document.querySelector('#view').innerHTML}));
""")
    assert "No groups yet" in out["v"]


# ===================================================================== Ask ===

_ASK = _VIEW + """
S=Object.assign({meetings:[],needs_you:[],unknowns:[],enrolled:[],llm_available:true,llm_backend:'local'},S||{});
S.meetings=[{base:'R1',title:'Roof Budget',date:'2026-10-01',minutes:30,named:[],ask_clusters:{},summary:'',next_steps:[]}];
const T1={id:'aaaaaaaaaaaa',scope:{kind:'all',label:'All recordings'},messages:[
 {q:'How big is the reserve?',a:'It is 412 <b>thousand</b> [1].',nothing_found:false,
  citations:[{n:1,meeting_id:'m1',base:'R1',title:'Roof <i>Budget</i>',date:'2026-10-01',start:100.5,index:5,gone:false},
             {n:2,meeting_id:'m9',base:null,title:'Gone Meeting',date:'2026-09-01',start:3,index:0,gone:true}],hits:[]},
 {q:'Is the heater fixed?',a:'I did not find this in your meetings.',nothing_found:true,citations:[],
  hits:[{base:'R1',title:'Roof Budget',date:'2026-10-01',start:40,snippet:'The roof heater <x> was discussed.'}]}]};
answer=(p,b)=>{
 if(p==='/api/threads')return {threads:[{id:'aaaaaaaaaaaa',q:'How big is the reserve?',scope:'All recordings',updated_at:'2026-10-02T10:00:00',n:2}]};
 if(p.startsWith('/api/thread?id='))return T1;
 if(p==='/api/groups')return {groups:[{key:'gA',name:'Grant Writing',count:2,hours:1,why:'',bases:[]}]};
 return {};
};
"""


@needs_node
def test_ask_thread_renders_citations_links_and_nothing_found():
    out = _run(_ASK + """
location.hash='ask/aaaaaaaaaaaa';route();await flush();
console.log(JSON.stringify({route:UI.route,v:document.querySelector('#view').innerHTML,
 loaded:calls.map(c=>c[0])}));
""")
    v = out["v"]
    assert out["route"] == "ask"
    # escaped answer text and titles
    assert "412 &lt;b&gt;thousand&lt;/b&gt; [1]." in v and "<b>thousand" not in v
    assert "Roof &lt;i&gt;Budget&lt;/i&gt;" in v
    # a citation opens the meeting at its second, a gone one is not a link
    assert 'href="#m/R1/t/100.5"' in v
    assert 'class="cite gone"' in v and "Gone Meeting" in v
    # nothing found shows the closest hits, escaped, and no invented text
    assert "I did not find this in your meetings." in v
    assert "Closest search hits" in v and "&lt;x&gt;" in v and 'href="#m/R1/t/40"' in v
    assert "Runs on this Mac. Threads save here." in v
    # a follow-up keeps the thread's scope: no picker on an open thread
    assert 'id="ask-scope"' not in v and "All recordings" in v


@needs_node
def test_ask_sends_with_scope_and_keeps_the_question_when_busy():
    out = _run(_ASK + """
LIB.groups=null;location.hash='ask';route();await flush();
const v0=document.querySelector('#view').innerHTML;
LIB.ascope='group:gA';LIB.adraft='What is the match rate?';
const prev=answer;
answer=(p,b)=>p==='/api/ask_library'?{busy:true,error:'The assistant is busy with a summary or another question. Try again when it finishes. Your question stays here.'}:prev(p,b);
await askSend();await flush();
const busyView=document.querySelector('#view').innerHTML;
const sent=calls.filter(c=>c[0]==='/api/ask_library').map(c=>c[1]);
console.log(JSON.stringify({v0,busyView,sent,draft:LIB.adraft,busy:LIB.abusy,g:LIB.groups}));
""")
    assert 'id="ask-scope"' in out["v0"] and "Grant Writing (2)" in out["v0"]
    assert "Last 30 days" in out["v0"]
    assert out["sent"] == [{"question": "What is the match rate?",
                            "scope": {"kind": "group", "key": "gA"}}]
    # busy: the typed question stays, and the reason shows
    assert out["draft"] == "What is the match rate?" and out["busy"] is False
    assert 'value="What is the match rate?"' in out["busyView"]
    assert "Your question stays here." in out["busyView"]


@needs_node
def test_ask_follow_up_goes_to_the_thread_and_clears_the_field():
    out = _run(_ASK + """
location.hash='ask/aaaaaaaaaaaa';route();await flush();
LIB.adraft='And the roof?';
const prev=answer;
answer=(p,b)=>p==='/api/ask_library'?{ok:true,thread:'aaaaaaaaaaaa',message:{}}:prev(p,b);
await askSend();await flush();
console.log(JSON.stringify({sent:calls.filter(c=>c[0]==='/api/ask_library').map(c=>c[1]),draft:LIB.adraft,tid:LIB.tid}));
""")
    assert out["sent"] == [{"question": "And the roof?", "thread": "aaaaaaaaaaaa"}]
    assert out["draft"] == "" and out["tid"] == "aaaaaaaaaaaa"


@needs_node
def test_ask_states_for_an_empty_library_and_a_missing_model():
    out = _run(_VIEW + """
answer=()=>({threads:[],groups:[]});
S=Object.assign({meetings:[],needs_you:[],llm_available:false},S||{});
location.hash='ask';route();await flush();
const empty=document.querySelector('#view').innerHTML;
S.meetings=[{base:'R1',title:'Roof',date:'2026-10-01',minutes:1,named:[],ask_clusters:{}}];
render(true);
console.log(JSON.stringify({empty,off:document.querySelector('#view').innerHTML}));
""")
    assert "Nothing to ask about yet" in out["empty"]
    assert "Ask needs the assistant installed" in out["off"]
    assert 'data-act="ask-send" disabled' in out["off"]


@needs_node
def test_ask_this_group_opens_ask_with_the_group_scope():
    out = _run(_ASK + """
A['ask-group']({dataset:{k:'gA'}});route();await flush();
console.log(JSON.stringify({route:UI.route,scope:LIB.ascope,hash:location.hash}));
""")
    assert out == {"route": "ask", "scope": "group:gA", "hash": "#ask"}
