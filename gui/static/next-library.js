/* ============================================================================
   The library views: Groups at #groups/<key> (plan Phase 4E) and Ask at
   #ask/<thread> (plan Phase 4F).

   Groups form by themselves on the server (stt/groups.py) from title words,
   people, and hour. This page lists them, shows one, and makes one write:
   "Not part of this group" on a row, with an undo. Audio and transcripts
   never move. Opening a group posts one count line to /api/usage, with no
   title and no key, for the plan's one-month use count.

   Ask answers one question across the meetings (stt/ask.py): FTS5
   retrieval into the local model, each claim cited to a meeting and second.
   Saved threads list on the left, the open thread on the right. "Answer
   from" picks All recordings, a group, or the last 30 days, and a follow-up
   keeps its thread's scope. A citation opens #m/<base>/t/<secs>. Nothing
   found reads "I did not find this in your meetings." with the closest
   search hits. A busy assistant disables Ask with its reason and keeps the
   typed question.

   The server joins this file after next.js, next-meeting.js, and
   next-voice.js, so it reuses their helpers (api, esc, rowHTML, render).
   ============================================================================ */

const LIB={groups:null,gload:false,gkey:null,gundo:null,gerr:'',gseen:null,
 threads:null,tid:null,thread:null,adraft:'',abusy:false,apending:null,areason:'',ascope:'all',aconfirm:false};

// #groups, #groups/<key>, #ask, and #ask/<thread>
function parseLibHash(h){
 const m=/^(groups|ask)(?:\/(.+))?$/.exec(h||'');
 if(!m)return null;
 let id=null;try{id=m[2]?decodeURIComponent(m[2]):null;}catch(e){id=null;}
 return {r:m[1],id};
}
function libEnter(lh){
 if(lh.r==='groups'){LIB.gkey=lh.id;if(LIB.groups==null||!lh.id)loadGroups();}
 if(lh.r==='ask'){
  const tid=lh.id&&/^[0-9a-f]{12}$/.test(lh.id)?lh.id:null;
  if(tid!==LIB.tid){LIB.thread=null;LIB.areason='';LIB.aconfirm=false;}
  LIB.tid=tid;loadThreads();if(tid)loadThread(tid);
  if(LIB.groups==null)loadGroups();
 }
}
async function loadGroups(){
 if(LIB.gload)return;LIB.gload=true;
 const r=await api('/api/groups').catch(()=>({error:'Could not reach the panel.'}));
 LIB.gload=false;LIB.groups=r.groups||[];LIB.gerr=r.error||'';
 if(UI.route==='groups'||UI.route==='ask')render(true);
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
function askGroupBtn(g){return `<button class="btn" type="button" data-act="ask-group" data-k="${esc(g.key)}">Ask this group</button>`;}
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
 'ask-group'(el){LIB.ascope='group:'+el.dataset.k;LIB.tid=null;LIB.thread=null;go('ask');},
 async 'g-undo'(){const u=LIB.gundo;if(!u)return;LIB.gundo=null;
  const r=await api('/api/group_override',{base:u.base,group:u.key,undo:true}).catch(()=>({error:'Could not reach the panel.'}));
  if(r.error||r.ok===false){UI.err.group=r.error||'That did not work.';}
  LIB.groups=null;await loadGroups();await refresh();render(true);}
});

/* ----------------------------------------------------------------- ask ---- */
async function loadThreads(){
 const r=await api('/api/threads').catch(()=>({threads:[]}));
 LIB.threads=r.threads||[];if(UI.route==='ask')render(true);
}
async function loadThread(tid){
 const r=await api('/api/thread?id='+encodeURIComponent(tid)).catch(()=>({error:'Could not reach the panel.'}));
 if(LIB.tid!==tid)return;
 if(r.error){LIB.thread=null;LIB.areason=r.error==='no such thread'?'That thread is gone.':r.error;}
 else LIB.thread=r;
 if(UI.route==='ask')render(true);
}
function scopeBody(v){
 if(v==='days:30')return {kind:'days',days:30};
 if(String(v).startsWith('group:'))return {kind:'group',key:String(v).slice(6)};
 return {kind:'all'};
}
function citeHTML(c){
 const when=c.date?dayLabel(c.date):'';
 const label=`<span class="mono">${c.n!=null?c.n+'.':''}</span> ${esc(c.title||'Recording')}${when?' · '+esc(when):''} · <span class="mono">${fmtT(c.start)}</span>`;
 if(c.gone||!c.base)return `<span class="cite gone" title="This meeting is no longer in the library">${label}</span>`;
 return `<a class="cite" href="${meetingHref(c.base,c.start)}">${label}</a>`;
}
function msgHTML(m){
 const q=`<div class="msg q">${esc(m.q)}</div>`;
 if(m.nothing_found){
  const hits=(m.hits||[]).filter(h=>!h.gone&&h.base);
  return q+`<div class="msg a"><div>${esc(m.a||'I did not find this in your meetings.')}</div>${hits.length?`<div class="meta">Closest search hits</div><div class="stack">${hits.map(h=>`<a class="hit" href="${meetingHref(h.base,h.start)}"><div class="meta">${esc(h.title||'')}${h.date?' · '+esc(dayLabel(h.date)):''} · <span class="mono">${fmtT(h.start)}</span></div><div>${esc(h.snippet||'')}</div></a>`).join('')}</div>`:''}</div>`;
 }
 const left=m.left_out?`<div class="meta">${plural(m.left_out,'strict meeting')} left out: strict meetings never leave this Mac.</div>`:'';
 return q+`<div class="msg a"><div>${esc(m.a)}</div>${(m.citations||[]).length?`<div class="cites">${m.citations.map(citeHTML).join('')}</div>`:''}${left}</div>`;
}
function askScopeHTML(){
 const t=LIB.thread;
 if(t)return `<span class="meta">Answer from <strong>${esc((t.scope||{}).label||'All recordings')}</strong></span>`;
 const gs=LIB.groups||[];
 const opts=[['all','All recordings'],...gs.map(g=>['group:'+g.key,g.name+' ('+g.count+')']),['days:30','Last 30 days']];
 // a group that is gone falls back to all, once the groups are known
 if(LIB.groups!=null&&!opts.some(o=>o[0]===LIB.ascope))LIB.ascope='all';
 if(!opts.some(o=>o[0]===LIB.ascope))opts.push([LIB.ascope,'This group']);
 return `<label class="meta" for="ask-scope">Answer from</label><select id="ask-scope">${opts.map(([v,l])=>`<option value="${esc(v)}" ${LIB.ascope===v?'selected':''}>${esc(l)}</option>`).join('')}</select>`;
}
function askView(){
 if(!meetings().length)return `<h1>Ask</h1><div class="empty"><strong>Nothing to ask about yet</strong><span>Ask answers from your meetings and links each answer to the moment it came from. It starts working after the first meeting finishes.</span></div>`;
 const local=!S.llm_backend||S.llm_backend==='local';
 const where=local?'Runs on this Mac. Threads save here.':'Runs on the cloud assistant set in Settings. Strict meetings stay on this Mac. Threads save here.';
 const off=!S.llm_available?'Ask needs the assistant installed. Search still works.':'';
 const ts=LIB.threads||[];
 const list=`<div class="eyebrow">Saved <span class="n">${ts.length}</span></div>`+(ts.length?ts.map(x=>`<a class="hit ${x.id===LIB.tid?'sel':''}" href="#ask/${x.id}"><div class="hit-t">${esc(x.q)}</div><div class="meta">${esc(x.scope)}${x.n>1?' · '+plural(x.n,'question'):''}</div></a>`).join(''):'<div class="meta">Threads you ask save here.</div>');
 const t=LIB.thread;
 const msgs=t?(t.messages||[]).map(msgHTML).join(''):(LIB.tid?'<div class="meta">Loading the thread…</div>':'<div class="empty"><span>Ask about decisions, people, or topics across your meetings.</span></div>');
 const pending=LIB.apending?`<div class="msg q">${esc(LIB.apending)}</div><div class="msg a meta" role="status">Reading your meetings…</div>`:'';
 const why=off||LIB.areason||(LIB.abusy?'The assistant is answering. Your question stays here.':'');
 const del=t?`<button class="btn plain ${LIB.aconfirm?'danger':''}" type="button" data-act="ask-del">${LIB.aconfirm?'Confirm: delete thread':'Delete thread'}</button>`:'';
 return `<div class="head"><h1>Ask</h1><span class="tools">${del}<button class="btn" type="button" data-act="ask-new">New question</button></span></div>
  <div class="ask-grid"><nav class="stack thread-list" aria-label="Saved threads">${list}</nav>
  <div class="stack" style="min-width:0;gap:var(--s4)"><div class="tools">${askScopeHTML()}<span class="meta">${esc(where)}</span></div>
  <div class="thread">${msgs}${pending}</div>
  <div class="ask-form"><input type="text" id="ask-q" placeholder="${t?'Ask a follow-up':'Ask across your meetings'}" aria-label="Question" autocomplete="off" value="${esc(LIB.adraft)}" maxlength="2000"><button class="btn primary" type="button" data-act="ask-send" ${off||LIB.abusy?'disabled':''}>Ask</button></div>
  ${why?`<div class="meta ${LIB.areason?'attn-t':''}" id="ask-why">${esc(why)}</div>`:''}</div></div>`;
}
async function askSend(){
 const q=(LIB.adraft||'').trim();if(!q||LIB.abusy||!S.llm_available)return;
 LIB.abusy=true;LIB.areason='';LIB.apending=q;render(true);
 const body={question:q};if(LIB.tid)body.thread=LIB.tid;else body.scope=scopeBody(LIB.ascope);
 let r;try{r=await api('/api/ask_library',body);}catch(e){r={error:'Could not reach the panel.'};}
 LIB.abusy=false;LIB.apending=null;
 // a failure, a busy model included, keeps the typed question in the field
 if(!r||r.error||r.ok===false){LIB.areason=(r&&r.error)||'That did not work.';render(true);return;}
 LIB.adraft='';LIB.tid=r.thread;
 if(location.hash!=='#ask/'+r.thread)history.replaceState(null,'','#ask/'+r.thread);
 await Promise.all([loadThread(r.thread),loadThreads()]);render(true);
 const i=document.getElementById('ask-q');if(i&&i.focus)i.focus();
}
Object.assign(A,{
 'ask-send'(){askSend();},
 'ask-new'(){LIB.tid=null;LIB.thread=null;LIB.areason='';LIB.aconfirm=false;go('ask');render(true);const i=document.getElementById('ask-q');if(i&&i.focus)i.focus();},
 async 'ask-del'(){if(!LIB.aconfirm){LIB.aconfirm=true;render(true);return;}LIB.aconfirm=false;
  const id=LIB.tid;const r=await api('/api/thread_delete',{id}).catch(()=>({ok:false}));
  if(!r.ok){LIB.areason='That thread could not be deleted.';render(true);return;}
  LIB.tid=null;LIB.thread=null;toast('Thread deleted.');go('ask');await loadThreads();}
});
document.addEventListener('input',e=>{if(e.target.id==='ask-q')LIB.adraft=e.target.value;});
document.addEventListener('change',e=>{if(e.target.id==='ask-scope')LIB.ascope=e.target.value;});
document.addEventListener('keydown',e=>{const t=e.target;if(t&&t.id==='ask-q'&&e.key==='Enter'&&!e.isComposing){e.preventDefault();askSend();}});
