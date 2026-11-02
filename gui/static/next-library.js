/* ============================================================================
   The library views (plan Phase 4E): Groups at #groups/<key>.

   Groups form by themselves on the server (stt/groups.py) from title words,
   people, and hour. This page lists them, shows one, and makes one write:
   "Not part of this group" on a row, with an undo. Audio and transcripts
   never move. Opening a group posts one count line to /api/usage, with no
   title and no key, for the plan's one-month use count.

   The server joins this file after next.js, next-meeting.js, and
   next-voice.js, so it reuses their helpers (api, esc, rowHTML, render).
   ============================================================================ */

const LIB={groups:null,gload:false,gkey:null,gundo:null,gerr:'',gseen:null};

// #groups and #groups/<key>
function parseLibHash(h){
 const m=/^(groups)(?:\/(.+))?$/.exec(h||'');
 if(!m)return null;
 let id=null;try{id=m[2]?decodeURIComponent(m[2]):null;}catch(e){id=null;}
 return {r:m[1],id};
}
function libEnter(lh){
 if(lh.r==='groups'){LIB.gkey=lh.id;if(LIB.groups==null||!lh.id)loadGroups();}
}
async function loadGroups(){
 if(LIB.gload)return;LIB.gload=true;
 const r=await api('/api/groups').catch(()=>({error:'Could not reach the panel.'}));
 LIB.gload=false;LIB.groups=r.groups||[];LIB.gerr=r.error||'';
 if(UI.route==='groups')render(true);
}
const groupByKey=k=>(LIB.groups||[]).find(g=>g.key===k);
function groupsEmpty(){
 return `<div class="empty"><strong>No groups yet</strong><span>Groups form by themselves when two or more meetings share title words, or the same people at the same hour. There is nothing to file.</span></div>`;
}
function groupsView(){
 if(LIB.groups==null){if(!LIB.gload)loadGroups();return `<h1>Groups</h1><div class="meta">Loading groups…</div>`;}
 if(LIB.gerr)return `<h1>Groups</h1><div class="err">${esc(LIB.gerr)}</div>`;
 const live=LIB.groups;
 if(!live.length)return `<h1>Groups</h1>${groupsEmpty()}`;
 let g=groupByKey(LIB.gkey);if(!g)g=live[0];
 if(LIB.gseen!==g.key){LIB.gseen=g.key;api('/api/usage',{event:'group_open'}).catch(()=>{});}
 const tree=live.map(x=>`<a class="tree-item" href="#groups/${encodeURIComponent(x.key)}" ${x.key===g.key?'aria-current="true"':''}><span>${esc(x.name)}</span><span class="n mono">${x.count}</span></a>`).join('');
 const rows=g.bases.map(byBase).filter(Boolean);
 const undo=LIB.gundo?`<div class="notice"><span>${esc(LIB.gundo.title)} is no longer part of this group.</span><button class="btn quiet" type="button" data-act="g-undo">Undo</button></div>`:'';
 return `<div class="head"><div><h1>Groups</h1><p class="meta">These form by themselves from title words, people, and hour. No file moves, and there is nothing to file.</p></div></div>
  <div class="f-grid"><nav class="tree" aria-label="Groups">${tree}</nav>
  <div class="stack sections" style="min-width:0"><div class="head"><div style="min-width:0"><h2>${esc(g.name)}</h2><div class="meta">${plural(g.count,'recording')} · <span class="mono">${g.hours} h</span> · ${esc(g.why)}</div></div>
   ${typeof askGroupBtn==='function'?askGroupBtn(g):''}</div>${undo}${errLine('group')}
   <div class="stack">${rows.map(m=>rowHTML(m,{group:g.key})).join('')}</div></div></div>`;
}
// the one correction this screen makes, from a grouped row's menu
function groupMenuItem(base,key){
 const g=groupByKey(key);if(!g)return '';
 return `<hr><button class="mi" type="button" data-act="g-out" data-base="${esc(base)}" data-k="${esc(key)}">Not part of this group<small>${esc(g.name)}</small></button>`;
}
Object.assign(A,{
 async 'g-out'(el){hidePop();const b=el.dataset.base,k=el.dataset.k;const m=byBase(b)||{};
  const r=await api('/api/group_override',{base:b,group:k}).catch(()=>({error:'Could not reach the panel.'}));
  if(r.error||r.ok===false){UI.err.group=r.error||'That did not work.';render(true);return;}
  delete UI.err.group;LIB.gundo={base:b,key:k,title:m.title||b};LIB.groups=null;await loadGroups();await refresh();render(true);},
 async 'g-undo'(){const u=LIB.gundo;if(!u)return;LIB.gundo=null;
  const r=await api('/api/group_override',{base:u.base,group:u.key,undo:true}).catch(()=>({error:'Could not reach the panel.'}));
  if(r.error||r.ok===false){UI.err.group=r.error||'That did not work.';}
  LIB.groups=null;await loadGroups();await refresh();render(true);}
});
