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
