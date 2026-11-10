'use strict';
/* ============================================================================
   The panel's frame (plan Phases 4C and 4D). A sidebar with routes for Home,
   Recordings, Activity, Speakers, Archive, and Settings, a search palette on
   the FTS5 index, and a narrow-window fallback below 900px. A meeting opens
   at #m/<base> (next-meeting.js), and voices are named in the voice sheet
   (next-voice.js). The server joins this file, next-meeting.js, and
   next-voice.js in that fixed order into one script: no modules, no build.

   Since the 4D swap, / serves this page. It reads one GET /api/state poll
   and calls only existing endpoints. tests/test_next_shell.py and
   tests/test_next_meeting.py pin it.
   ============================================================================ */

const ICON={
 home:'<path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/>',
 list:'<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
 voice:'<circle cx="10" cy="8" r="4"/><path d="M3 20c.7-3.5 3.3-5.5 7-5.5s6.3 2 7 5.5"/><path d="M18 5.5c1.5 1.8 1.5 4.7 0 6.5"/>',
 act:'<path d="M3 12h4l3-7 4 14 3-7h4"/>',
 arch:'<path d="M3 5h18v4H3z"/><path d="M5 9v10h14V9M10 13h4"/>',
 gear:'<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3 1a7 7 0 0 0-2.1-1.2L14 3h-4l-.5 2.7a7 7 0 0 0-2.1 1.2l-2.3-1-2 3.4 2 1.5A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.5 2 3.4 2.3-1c.6.5 1.3.9 2.1 1.2L10 21h4l.5-2.7c.8-.3 1.5-.7 2.1-1.2l2.3 1 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',
 search:'<circle cx="11" cy="11" r="6.5"/><path d="m16 16 5 5"/>',
 menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
 more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
 play:'<path d="M7 4.5 19 12 7 19.5z" fill="currentColor"/>',
 pause:'<path d="M8 5v14M16 5v14"/>',
 close:'<path d="m6 6 12 12M18 6 6 18"/>',
 next:'<path d="m10 6 6 6-6 6"/>',
 down:'<path d="m6 10 6 6 6-6"/>',
 theme:'<circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/>',
 back:'<path d="m14 6-6 6 6 6"/>',
 edit:'<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13 7 4 4"/>',
 groups:'<path d="M3 7h6l2 2h10v10H3z"/><path d="M3 7V5h6l2 2"/>',
 ask:'<path d="M4 5h16v11H9l-5 4z"/><path d="M9 10h6"/>'
};
const ic=n=>`<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICON[n]||''}</svg>`;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $=sel=>document.querySelector(sel);
const fmtT=t=>{t=Math.max(0,Math.round(+t||0));const h=Math.floor(t/3600),m=Math.floor(t%3600/60),s=t%60;
 return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(s).padStart(2,'0');};
const fmtMin=secs=>{const m=Math.max(1,Math.round((+secs||0)/60));return m>=60?`${Math.floor(m/60)}h ${m%60}m`:`${m}m`;};
const plural=(n,one,many)=>`${n} ${n===1?one:(many||one+'s')}`;
// a recording's length: whole seconds under a minute ('22 sec', never
// '0 min'), else the rounded minutes. A meta from before `seconds` shows minutes
const durText=m=>{const s=m&&m.seconds!=null?Math.round(+m.seconds||0):null;return s!=null&&s<60?`${s} sec`:`${(m&&m.minutes)||0} min`;};
// a total length: seconds under a minute, minutes under an hour, then hours
const totalText=secs=>{const s=Math.round(+secs||0);if(s<60)return s?`${s} sec`:'0 min';if(s<3600)return `${Math.round(s/60)} min`;return `${(s/3600).toFixed(1).replace(/\.0$/,'')} h`;};
// one decimal: a whole-second floor can land before the hit's own line
const meetingHref=(base,t)=>'#m/'+encodeURIComponent(base)+(t!=null?'/t/'+Math.floor(+t*10)/10:'');

/* ---------------------------------------------------------------- data ---- */
async function api(path,body){
 const r=await fetch(path,body!==undefined?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
 let d={};try{d=await r.json();}catch(e){d={};}
 if(!r.ok&&!d.error)d.error='The panel answered '+r.status+'.';
 return d;
}
// Settings are read from the same poll, so a value typed into a field must
// survive the next poll: render() skips a repaint while a field has focus.
let S=null;
const UI={route:'home',q:'',chip:'all',sort:'new',fold:{},sel:new Set(),menu:false,palette:false,keys:false,
 pq:'',pres:null,actTab:'all',hist:null,histQ:'',histF:'all',dupes:null,archived:null,confirm:null,
 naming:null,carry:null,openSamples:null,err:{},keysOpen:false,prefsSub:null,updates:null,mbase:null,mseek:null};
const STAGE_NICE={downloading:'Downloading',converting:'Preparing',transcribing:'Transcribing',
 diarizing:'Speakers',verifying:'Verifying',writing:'Writing',summarizing:'Summary'};
const NEEDS_DAYS=14;

function isoLocal(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
function daysAgo(iso){if(!iso)return 9999;const a=new Date(iso.slice(0,10)+'T12:00:00'),b=new Date(isoLocal(new Date())+'T12:00:00');return Math.round((b-a)/86400000);}
function dayLabel(iso){if(!iso)return '';const n=daysAgo(iso);if(n===0)return 'Today';if(n===1)return 'Yesterday';
 const d=new Date(iso.slice(0,10)+'T12:00:00');return d.toLocaleDateString([],{month:'short',day:'numeric',...(d.getFullYear()!==new Date().getFullYear()?{year:'numeric'}:{})});}
function timeOf(m){if(!m.recorded_at)return '';try{return new Date(m.recorded_at).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});}catch(e){return '';}}
function whenText(m){const t=timeOf(m);return dayLabel(m.date)+(t?', '+t:'');}
function arrivedDays(m){const a=m.arrived_at;if(!a)return 9999;const t=new Date(a);return isNaN(t)?9999:(Date.now()-t.getTime())/86400000;}

const meetings=()=>(S&&S.meetings)||[];
const byBase=b=>meetings().find(m=>m.base===b);
const unnamedN=m=>Object.keys(m.ask_clusters||{}).length;
// a meeting from before the title rule has no title_state. titles.needs_title
// falls back to needs_review for it, and so does the chip.
const isDraft=m=>m.title_state?(m.title_state==='needs_title'||(m.title_state==='provisional'&&m.route!=='public_download')):!!m.needs_review;
const dateUncertain=m=>m.date_source==='mtime'||m.date_source==='short_typed';
const activeRows=()=>((S&&S.timeline)||[]).filter(r=>r.state==='processing'||r.state==='recording');
const waitingRows=()=>((S&&S.timeline)||[]).filter(r=>r.state==='waiting'||r.state==='held');
const failedRows=()=>((S&&S.timeline)||[]).filter(r=>r.state==='failed');
const needsYou=()=>(S&&S.needs_you)||[];
const urgent=()=>((S&&S.tray)||[]).filter(t=>['recorder_stall','recorder_note','failed','dupe_files','dupe_meetings'].includes(t.kind));
// a recorder row whose text names the microphone or audio offers the permission reset
const recFixable=t=>(t.kind==='recorder_stall'||t.kind==='recorder_note')&&/[Mm]icrophone|audio/.test(t.detail||'');

/* --------------------------------------------------------------- frame ---- */
const NAV=[['home','Home','home'],['recordings','Recordings','list'],['groups','Groups','groups'],['ask','Ask','ask'],['speakers','Speakers','voice']];
const NAV2=[['activity','Activity','act'],['archive','Archive','arch'],['settings','Settings','gear']];
const TITLES={home:'Home',recordings:'Recordings',groups:'Groups',ask:'Ask',activity:'Activity',speakers:'Speakers',archive:'Archive',settings:'Settings'};
// the page's name in the tab and the top bar: a meeting shows its title
function pageTitle(){if(UI.route==='meeting'){const m=byBase(UI.mbase);return m?m.title:'Recording';}return TITLES[UI.route]||'Meetings';}
function pillText(){
 if(!S)return '';
 const rec=S.recording;
 if(rec)
  return {cls:'rec',t:(rec.paused?'Recording paused':'Recording ')+(rec.paused?'':fmtT(rec.elapsed_secs||0))};
 const a=Object.values(S.active||{});
 if(S.running&&a.length){const e=a[0];const st=STAGE_NICE[e.stage]||'Working';
  const eta=S.overall_eta_sec?' · about '+fmtMin(S.overall_eta_sec):'';
  return {cls:'',t:`${st}${e.pct!=null?' '+e.pct+'%':''}${eta}`};}
 if(S.relabel_running)return {cls:'',t:'Applying names'};
 const w=waitingRows().length;
 if(S.paused&&w)return {cls:'',t:`Paused · ${w} waiting`};
 if(w)return {cls:'',t:`${w} waiting`};
 return null;
}
function pillHTML(){const p=pillText();if(!p)return '';
 return `<button class="pill ${p.cls}" type="button" data-act="go" data-to="activity" title="Open Activity">${esc(p.t)}</button>`;}
function navHTML(){
 const c=UI.route==='meeting'?'recordings':UI.route;const ny=needsYou().length;const urg=urgent().length;
 const item=([to,l,i])=>{let n='';if(to==='home'&&ny)n=`<span class="count" title="Needs you">${ny}</span>`;
  if(to==='activity'&&urg)n=`<span class="count" title="Needs attention">${urg}</span>`;
  return `<a class="nav-item" href="#${to}" ${c===to?'aria-current="page"':''}>${ic(i)}<span>${l}</span>${n}</a>`;};
 return `<nav class="nav">${NAV.map(item).join('')}</nav><div class="nav-sep"></div><nav class="nav">${NAV2.map(item).join('')}</nav>`;
}
function themeLabel(){return 'Theme: '+(document.documentElement.getAttribute('data-theme')||'auto');}
function renderFrame(){
 $('#side').innerHTML=`<div class="side-top"><span class="wordmark">Meetings</span><button class="icon-btn" type="button" data-act="theme" title="${themeLabel()}" aria-label="${themeLabel()}">${ic('theme')}</button></div>
  ${pillHTML()}
  <button class="btn primary new" type="button" data-act="new">${ic('plus')}New</button>
  <button class="searchbtn" type="button" data-act="palette" title="Search titles and words (/ or Cmd K)">${ic('search')}<span>Search</span></button>
  ${navHTML()}
  <div class="side-foot"><a href="#" data-act="keys">Keys</a></div>`;
 $('#topbar').innerHTML=`<button class="icon-btn" type="button" data-act="menu" aria-label="Open the menu">${ic('menu')}</button>${UI.route==='meeting'?`<a class="icon-btn" href="#recordings" aria-label="Back to Recordings">${ic('back')}</a>`:''}<span class="t">${esc(pageTitle())}</span>${pillHTML()}<button class="icon-btn" type="button" data-act="palette" aria-label="Search">${ic('search')}</button>`;
}

/* ---------------------------------------------------------------- rows ---- */
function chipsFor(m){
 let h='';
 if(m.no_speech)h+='<span class="chip draft">No speech</span>';
 if(isDraft(m))h+=`<span class="chip ${m.title_state==='needs_title'?'attn':'draft'}" title="${m.title_state==='needs_title'?'Only the model drafted this title':'An automatic title. Keep it or edit it.'}">Draft title</span>`;
 if(dateUncertain(m))h+='<span class="chip draft" title="This date came from the file time or a short typed date">Date uncertain</span>';
 return h;
}
function rowHTML(m,o={}){
 const named=(m.named||[]).slice(0,3);let ppl=named.join(', ');if((m.named||[]).length>3)ppl+=` +${m.named.length-3}`;
 const bits=[esc(o.day?(timeOf(m)||dayLabel(m.date)):whenText(m)),`${durText(m)}`];if(ppl)bits.push(esc(ppl));
 const u=unnamedN(m);const sel=UI.sel.has(m.base);
 const prev=m.no_speech?'No speech found in this recording.':(m.gist||(m.summary||'').split(/(?<=[.!?])\s+/).slice(0,2).join(' '));
 return `<div class="row ${sel?'selected':''}" data-row data-base="${esc(m.base)}" tabindex="0">
  <label class="ck" title="Select"><input type="checkbox" data-sel="${esc(m.base)}" ${sel?'checked':''} aria-label="Select ${esc(m.title)}"></label>
  <a href="${meetingHref(m.base)}" style="min-width:0;color:inherit;text-decoration:none" data-open><div class="row-title"><span>${esc(m.title)}</span>${chipsFor(m)}</div>
  <div class="meta">${bits.join(' · ')}${u?` · <span class="attn-t nb">${u} unnamed</span>`:''}${m.category?` · ${m.category==='work'?'Work':'Personal'}`:''}${m.group_name&&!o.group?` · <span class="nb" title="Group">${esc(m.group_name)}</span>`:''}</div>
  ${prev?`<div class="preview">${esc(prev)}</div>`:''}</a>
  <div class="rail"><button class="icon-btn" type="button" data-act="row-menu" data-base="${esc(m.base)}" aria-label="More actions for ${esc(m.title)}">${ic('more')}</button></div></div>`;
}
function activeRowHTML(r){
 if(r.state==='recording'){
  return `<div class="row static" data-row tabindex="0"><div style="min-width:0"><div class="row-title"><span>Recording</span><span class="chip rec">${r.paused?'Paused':'Live'}</span>${r.stalled?'<span class="chip rec">No audio is being captured</span>':''}</div>
   <div class="meta mono">${fmtT(r.elapsed_secs||0)} · Stop and save from the menu bar</div></div></div>`;
 }
 const st=STAGE_NICE[r.stage]||'Working';const eta=r.over?`running long · +${fmtMin(r.over)}`:(r.eta?`about ${fmtMin(r.eta)} left`:'');
 const href=r.id&&!String(r.id).startsWith('src:')?meetingHref(r.id):null;
 return `<div class="row ${href?'':'static'}" data-row tabindex="0"><div style="min-width:0"><div class="row-title"><span>${esc(r.title)}</span></div>
  <div class="meta mono">${esc(st)}${r.pct!=null?' '+r.pct+'%':''}${eta?' · '+esc(eta):''}</div></div>
  <div class="rail">${href?`<a class="btn quiet" href="${href}">Open</a>`:''}</div>${r.pct!=null?`<div class="prog"><i style="width:${Math.min(100,+r.pct||0)}%"></i></div>`:''}</div>`;
}
function waitingSummary(){
 const w=waitingRows();if(!w.length)return '';
 const mins=w.reduce((a,r)=>a+(+r.est_minutes||0),0);
 return `<div class="row static" data-row tabindex="0"><div style="min-width:0"><div class="row-title q">${plural(w.length,'file')} waiting</div><div class="meta mono">${mins?'about '+fmtMin(mins*60)+' · ':''}shortest first${S.paused?' · automatic runs paused':''}</div></div><div class="rail"><a class="btn quiet" href="#activity">Details</a></div></div>`;
}

/* ---------------------------------------------------------------- home ---- */
function setupCard(){
 const enrolled=(S.enrolled||[]).length;const you=S.mic_speaker;
 const line=(ok,t,d,btn)=>`<div class="setup-line"><span class="mark ${ok?'ok':'todo'}">${ok?'✓':'!'}</span><div style="min-width:0"><div style="font-weight:600">${t}</div><div class="meta">${d}</div></div>${btn}</div>`;
 return `<div class="setup"><h2>Set up in three checks</h2>
  ${line(!!S.recorder_ready,S.recorder_ready?'Recorder ready':'Recorder not built','The menu-bar recorder saves mic and system audio.','<a class="btn quiet" href="#settings">Recorder settings</a>')}
  ${(()=>{const P=S.paths||{};const gone=!!P.source&&P.source_exists===false;   // set, but not on disk
   return line(!!P.source&&!gone,gone?'Watched folder missing':P.source?'Watched folder set':'Watched folder not set',
    gone?`${esc(P.source)} does not exist yet. Create it, or pick another folder.`:esc(P.source||'Pick the folder new recordings arrive in.'),
    '<a class="btn quiet" href="#settings">Folder settings</a>');})()}
  ${line(!!you,you?'Your voice is set':'Your voice is not set',you?esc(you):(enrolled?'Pick your name in Settings.':'Name yourself in your first meeting to enroll the voice.'),'<a class="btn quiet" href="#settings">Pick your voice</a>')}
  <p class="sub" style="text-align:center">Record from the menu bar, let Voice Memos sync, or drop an audio file here.</p>
  <div style="display:flex;justify-content:center"><button class="btn primary" type="button" data-act="new">${ic('plus')}New</button></div></div>`;
}
function needsCard(x,i){
 const m=byBase(x.target)||{};
 if(x.kind==='needs_title'){
  const v=m.suggested||m.title||x.title;
  return `<div class="ask-card"><div class="ask-line"><input class="title-edit" type="text" id="nt-${esc(x.target)}" data-base="${esc(x.target)}" value="${esc(v)}" aria-label="Title for the recording from ${esc(whenText(m))}">
   <button class="btn ${i===0?'primary':'quiet'}" type="button" data-act="keep" data-base="${esc(x.target)}">Keep title</button><a class="btn plain" href="${meetingHref(x.target)}">Open</a></div>
   <div class="meta">${esc(whenText(m))} · ${durText(m)} · Only the model drafted this title</div>${errLine('keep:'+x.target)}</div>`;
 }
 if(x.kind==='voice'){
  const act=x.uid?`<button class="btn ${i===0?'primary':'quiet'}" type="button" data-act="name-voice" data-uid="${esc(x.uid)}" data-base="${esc(x.target)}">Name</button>`:`<a class="btn ${i===0?'primary':'quiet'}" href="${meetingHref(x.target)}">Open the meeting</a>`;
  return `<div class="ask-card"><div class="ask-line" style="justify-content:space-between"><div style="min-width:0"><div class="row-title">Who is ${esc(x.title)}?</div><div class="meta">${esc(x.detail)}</div></div>${act}</div></div>`;
 }
 const what=x.kind==='no_speech'?'No speech found in this recording.':x.detail;
 return `<div class="ask-card"><div class="ask-line" style="justify-content:space-between"><div style="min-width:0"><div class="row-title">${esc(x.title)}</div><div class="meta attn-t">${esc(what)}</div></div>
  <span class="tools"><a class="btn quiet" href="${meetingHref(x.target)}">Open</a>${x.kind==='no_speech'?`<button class="btn plain" type="button" data-act="archive" data-base="${esc(x.target)}">Archive</button>`:''}</span></div></div>`;
}
function errLine(k){const e=UI.err[k];return e?`<div class="err">${esc(e)}</div>`:'';}
function homeView(){
 if(!meetings().length&&!activeRows().length&&!waitingRows().length)return `<h1>Home</h1>${setupCard()}`;
 const ny=needsYou();
 const run=activeRows().length||waitingRows().length?`<section><div class="eyebrow">Running now</div><div class="stack">${activeRows().map(activeRowHTML).join('')}${waitingSummary()}</div></section>`:'';
 const urg=urgent();
 const att=urg.length?`<section><div class="eyebrow">Needs attention <span class="n">${urg.length}</span><a class="btn quiet act" href="#activity">Open Activity</a></div><div class="stack">${urg.map(t=>`<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(t.title)}</div><div class="meta attn-t">${esc(t.detail||'')}</div></div></div>`).join('')}</div></section>`:'';
 const none=!ny.length?`<div class="none-line"><span class="ok-t" style="font-weight:600">Nothing needs you right now.</span> <span class="sub">New recordings appear here when they are ready.</span></div>`:'';
 const drafts=meetings().filter(m=>m.title_state==='provisional'&&m.route!=='public_download'&&arrivedDays(m)<=NEEDS_DAYS);
 const all=drafts.length>1?`<button class="btn quiet act" type="button" data-act="keep-all">${UI.confirm==='keep-all'?`Confirm: keep ${drafts.length} titles`:'Keep all'}</button>`:'';
 const draftCards=drafts.slice(0,5).map(m=>`<div class="ask-card quiet"><div class="ask-line"><input class="title-edit" type="text" id="nt-${esc(m.base)}" data-base="${esc(m.base)}" value="${esc(m.title)}" aria-label="Title for the recording from ${esc(whenText(m))}"><button class="btn quiet" type="button" data-act="keep" data-base="${esc(m.base)}">Keep title</button><a class="btn plain" href="${meetingHref(m.base)}">Open</a></div><div class="meta">${esc(whenText(m))} · ${durText(m)}${m.title_source?' · From '+esc(SOURCE_NICE[m.title_source]||m.title_source):''}</div>${errLine('keep:'+m.base)}</div>`).join('');
 const recent=[...meetings()].sort((a,b)=>String(b.arrived_at||b.processed_at||'').localeCompare(String(a.arrived_at||a.processed_at||''))).slice(0,5).map(m=>rowHTML(m)).join('');
 return `<div class="head"><h1>Home</h1></div><div class="home"><div class="stack sections">
  ${att}${run}
  <section><div class="eyebrow">Needs you ${ny.length?`<span class="n">${ny.length}</span>`:''}</div><div class="stack">${ny.map(needsCard).join('')}${none}</div></section>
  ${drafts.length?`<section><div class="eyebrow">Draft titles <span class="n">${drafts.length}</span>${all}</div><div class="stack">${draftCards}</div><p class="meta note">Optional. These titles came from a typed label, or the panel made them from the speakers and topic. They block nothing and leave this list ${NEEDS_DAYS} days after they arrive.</p></section>`:''}
  <section><div class="eyebrow">Recent</div><div class="stack">${recent}</div><a class="btn quiet" style="align-self:flex-start;margin-left:calc(var(--s2) * -1)" href="#recordings">All recordings</a></section></div></div>`;
}
const SOURCE_NICE={label:'your label',shortcut:'the Shortcut',public_download:'the public download',calendar:'Calendar',model:'the model',manual:'you',voice_memo:'Voice Memos'};

/* ---------------------------------------------------------- recordings ---- */
function recordingsView(){
 if(!meetings().length)return `<h1>Recordings</h1><div class="empty"><strong>No recordings yet</strong><span>Record from the menu bar, let Voice Memos sync, or drop an audio file here.</span><button class="btn primary" type="button" data-act="new">${ic('plus')}New</button></div>`;
 return `<div class="head"><h1>Recordings</h1><div class="tools" style="flex:1 1 320px;justify-content:flex-end"><input type="search" id="rec-q" placeholder="Filter by title, person, or tag" value="${esc(UI.q)}" aria-label="Filter recordings" style="flex:1 1 200px;max-width:320px">
  <select id="rec-sort" aria-label="Sort"><option value="new" ${UI.sort==='new'?'selected':''}>Newest first</option><option value="old" ${UI.sort==='old'?'selected':''}>Oldest first</option><option value="long" ${UI.sort==='long'?'selected':''}>Longest first</option><option value="az" ${UI.sort==='az'?'selected':''}>Title A to Z</option></select></div></div>
  <div class="chips" id="rec-chips"></div><div id="rec-list" class="stack sections"></div>`;
}
function filtered(){
 const q=UI.q.trim().toLowerCase();
 return meetings().filter(m=>{
  if(q&&!(m.title.toLowerCase().includes(q)||(m.named||[]).some(n=>n.toLowerCase().includes(q))||(m.tags||[]).some(t=>t.toLowerCase()===q)))return false;
  if(UI.chip==='work'&&m.category!=='work')return false;
  if(UI.chip==='personal'&&m.category!=='personal')return false;
  if(UI.chip==='draft'&&!isDraft(m))return false;
  if(UI.chip==='voices'&&!unnamedN(m))return false;
  return true;});
}
function monthKey(iso){return (iso||'').slice(0,7);}
function monthName(key){if(!key)return 'No date';const d=new Date(key+'-15T12:00:00');return d.toLocaleDateString([],{month:'long',year:'numeric'});}
function renderRecList(){
 const box=$('#rec-list');if(!box)return;
 const lib=meetings();
 const chip=(k,l,n)=>`<button class="chip ${UI.chip===k?'on':''}" type="button" data-act="chip" data-k="${k}" aria-pressed="${UI.chip===k}">${l}${n?` <span class="mono">${n}</span>`:''}</button>`;
 const ch=$('#rec-chips');const chh=chip('all','All')+chip('work','Work')+chip('personal','Personal')+chip('draft','Draft title',lib.filter(isDraft).length)+chip('voices','Has voices',lib.filter(m=>unnamedN(m)).length);
 if(ch.dataset.sig!==chh){keepFocus(()=>{ch.innerHTML=chh;ch.dataset.sig=chh;});}
 const html=recListHTML();if(html===REC_SIG)return;
 keepFocus(()=>{box.innerHTML=html;REC_SIG=html;});
}
function recListHTML(){
 let rows=filtered();
 if(!rows.length)return '<div class="empty">No matches.</div>';
 if(UI.sort!=='new'){
  rows=[...rows].sort(UI.sort==='old'?(a,b)=>String(a.date).localeCompare(String(b.date)):UI.sort==='long'?(a,b)=>(b.minutes||0)-(a.minutes||0):(a,b)=>a.title.localeCompare(b.title));
  return `<div class="stack">${rows.map(m=>rowHTML(m)).join('')}</div>`;
 }
 rows=[...rows].sort((a,b)=>String(b.date).localeCompare(String(a.date))||String(b.recorded_at||'').localeCompare(String(a.recorded_at||'')));
 const groups=[];const at=(label,key,fold)=>{let g=groups.find(x=>x.label===label);if(!g){g={label,key,fold,rows:[]};groups.push(g);}return g;};
 const thisMonth=isoLocal(new Date()).slice(0,7);
 // the newest month with recordings stays open, and so does this month.
 // Older months fold, so the list opens on what is recent.
 const newestOld=(rows.find(m=>daysAgo(m.date)>6)||{}).date;const _no=newestOld?monthKey(newestOld):'';
 for(const m of rows){const n=daysAgo(m.date);
  if(n===0)at('Today').rows.push(m);else if(n===1)at('Yesterday').rows.push(m);else if(n>1&&n<=6)at('This week').rows.push(m);
  else{const k=monthKey(m.date);at(monthName(k),k,k<thisMonth&&k!==_no).rows.push(m);}}
 const filtering=UI.q||UI.chip!=='all';
 return groups.map(g=>{const day=g.label==='Today'||g.label==='Yesterday';
  const folded=g.key&&!filtering&&(UI.fold[g.key]!=null?UI.fold[g.key]:g.fold);
  const head=g.key?`<button class="fold" type="button" data-act="fold" data-k="${g.key}" aria-expanded="${!folded}">${folded?ic('next'):ic('down')}${esc(g.label)}</button>`:esc(g.label);
  return `<section><div class="eyebrow">${head}<span class="n">${g.rows.length}</span></div>${folded?'':`<div class="stack">${g.rows.map(m=>rowHTML(m,{day})).join('')}</div>`}</section>`;}).join('');
}

/* ------------------------------------------------------------ activity ---- */
const RUN_OPTS=[['stt_run_par2','Two at a time'],['stt_run_strict','Strict'],['stt_run_verify','Verify'],['stt_run_onetime','One-time speakers']];
function lsGet(k){try{return localStorage.getItem(k);}catch(e){return null;}}
function lsSet(k,v){try{if(v==null)localStorage.removeItem(k);else localStorage.setItem(k,v);}catch(e){}}
function runOpts(){const g=k=>lsGet(k)==='1';return {parallel:g('stt_run_par2')?2:1,strict:g('stt_run_strict'),verify:g('stt_run_verify'),onetime:g('stt_run_onetime')};}
function waitingRowHTML(r){
 const dup=r.dup_of?`<div class="meta attn-t">Already processed as ${esc(r.dup_title||r.dup_of)}${r.dup_reason==='identical'?' (identical file)':''}</div>`:'';
 const conf=UI.confirm==='qdel:'+r.source_file;
 return `<div class="row static" data-row tabindex="0"><div style="min-width:0"><div class="row-title q"><span>${esc(r.title)}</span>${r.held?'<span class="chip draft">Held</span>':''}</div>
  <div class="meta mono">${r.size_mb!=null?r.size_mb+' MB':''}${r.est_minutes?' · about '+fmtMin(r.est_minutes*60):''}${r.est_detail?' · '+esc(r.est_detail):''}</div>${dup}${errLine('q:'+r.source_file)}</div>
  <div class="rail"><button class="btn quiet" type="button" data-act="run-one" data-name="${esc(r.source_file)}">Process</button><button class="btn plain" type="button" data-act="hold" data-name="${esc(r.source_file)}">${r.held?'Release':'Hold'}</button><button class="btn ${conf?'danger':'plain'}" type="button" data-act="qdel" data-name="${esc(r.source_file)}">${conf?'Confirm delete':'Delete'}</button></div></div>`;
}
function failedRowHTML(r){
 return `<div class="row static" data-row tabindex="0"><div style="min-width:0"><div class="row-title q"><span>${esc(r.title)}</span><span class="chip rec">Failed</span></div><div class="meta">${esc(r.error||'')}</div><div class="meta">${esc(r.retry_note||'')}</div>${errLine('f:'+r.source_file)}</div>
  <div class="rail">${r.gone?'':`<button class="btn quiet" type="button" data-act="run-one" data-name="${esc(r.source_file)}">Retry</button>`}<button class="btn plain" type="button" data-act="dismiss-fail" data-name="${esc(r.source_file)}">Dismiss</button></div></div>`;
}
function activityView(){
 const tab=(k,l,n)=>`<button class="chip ${UI.actTab===k?'on':''}" type="button" data-act="acttab" data-k="${k}">${l}${n?` <span class="mono">${n}</span>`:''}</button>`;
 const act=activeRows(),wait=waitingRows(),fail=failedRows(),jobs=S.queued_jobs||[];
 const stopConf=UI.confirm==='stop';
 const tools=`<div class="tools"><button class="btn primary" type="button" data-act="run-all">Process all new</button><button class="btn" type="button" data-act="import">Import files…</button>
  <button class="btn" type="button" data-act="pause">${S.paused?'Resume automatic runs':'Pause automatic runs'}</button>
  ${S.running?`<button class="btn danger" type="button" data-act="stop">${stopConf?'Confirm: stop processing':'Stop processing'}</button>`:''}</div>`;
 const opts=`<div class="tools">${RUN_OPTS.map(([k,l])=>`<label class="toggle"><input type="checkbox" data-opt="${k}" ${lsGet(k)==='1'?'checked':''}>${l}</label>`).join('')}<span class="meta">Options for the next run you start here.</span></div>`;
 const urg=urgent().filter(t=>t.kind!=='failed');   // failures have their own section here
 const attention=urg.length?`<section><div class="eyebrow">Needs attention <span class="n">${urg.length}</span></div><div class="stack">${urg.map(t=>{
   let a='';if(t.kind==='dupe_files'&&t.exact){const c=UI.confirm==='dupes-del';a=`<button class="btn ${c?'danger':'quiet'}" type="button" data-act="dupes-del">${c?`Confirm: delete ${t.exact} copies`:`Delete ${t.exact} exact copies`}</button>`;}
   if(t.kind==='dupe_meetings')a=`<button class="btn quiet" type="button" data-act="acttab" data-k="dupes">Review</button>`;
   if(recFixable(t))a=`<button class="btn quiet" type="button" data-act="rec-fix">Reset permission</button>`;
   const e=t.kind.startsWith('recorder')?errLine('recfix'):errLine('dupes');
   return `<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(t.title)}</div><div class="meta attn-t">${esc(t.detail||'')}</div>${e}</div><div class="rail">${a}</div></div>`;}).join('')}</div></section>`:'';
 const runSec=`<section><div class="eyebrow">Running now</div><div class="stack">${act.map(activeRowHTML).join('')||'<div class="none-line sub">Nothing is running.</div>'}</div></section>`;
 const jobSec=jobs.length?`<section><div class="eyebrow">Queued runs <span class="n">${jobs.length}</span></div><div class="stack">${jobs.map(j=>`<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(j.label)}</div><div class="meta">${j.strict?'Strict · ':''}${j.verify?'Verify · ':''}Starts when the current run ends</div></div><div class="rail"><button class="btn plain" type="button" data-act="unqueue" data-at="${esc(j.at)}">Cancel</button></div></div>`).join('')}</div></section>`:'';
 const waitSec=`<section><div class="eyebrow">Waiting <span class="n">${wait.length||''}</span></div><div class="stack">${wait.map(waitingRowHTML).join('')||'<div class="none-line sub">No files are waiting.</div>'}</div></section>`;
 const failSec=fail.length?`<section><div class="eyebrow">Failed <span class="n">${fail.length}</span></div><div class="stack">${fail.map(failedRowHTML).join('')}</div></section>`:'';
 const recent=(S.recent||[]).slice(0,8);
 const doneSec=recent.length?`<section><div class="eyebrow">Finished recently</div><div class="stack">${recent.map(r=>`<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(r.name)}</div><div class="meta">${esc(fmtAt(r.at))}${r.summary?' · '+esc(r.summary):''}</div></div>${r.ok?'<span class="chip ok">Done</span>':'<span class="chip rec">Failed</span>'}</div>`).join('')}</div></section>`:'';
 let body;
 if(UI.actTab==='history')body=historySec();
 else if(UI.actTab==='dupes')body=dupesSec();
 else if(UI.actTab==='running')body=runSec+jobSec;
 else if(UI.actTab==='waiting')body=waitSec;
 else if(UI.actTab==='failed')body=failSec||'<div class="empty">Nothing has failed.</div>';
 else body=attention+runSec+jobSec+waitSec+failSec+doneSec+vmRemovedSec();
 return `<div class="head"><h1>Activity</h1>${tools}</div>${opts}
  <div class="chips">${tab('all','All')}${tab('running','Running',act.length)}${tab('waiting','Waiting',wait.length)}${tab('failed','Failed',fail.length)}${tab('history','History')}${tab('dupes','Duplicates')}</div>
  ${errLine('activity')}<div class="stack sections">${body}</div>`;
}
function fmtAt(at){if(!at)return '';const d=new Date(at);if(isNaN(d))return at;return dayLabel(isoLocal(d))+', '+d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});}
function historySec(){
 if(UI.hist==null){loadHistory();return '<div class="none-line sub">Loading the history…</div>';}
 const q=UI.histQ.trim().toLowerCase();
 const rows=UI.hist.filter(r=>(!q||String(r.name||'').toLowerCase().includes(q))&&(UI.histF==='all'||(UI.histF==='ok')===!!r.ok));
 const days={};rows.slice(0,300).forEach(r=>{const k=(r.at||'').slice(0,10);(days[k]=days[k]||[]).push(r);});
 return `<div class="tools"><input type="search" id="hist-q" placeholder="Filter by file name" value="${esc(UI.histQ)}" aria-label="Filter history"><select id="hist-f" aria-label="Result"><option value="all" ${UI.histF==='all'?'selected':''}>All results</option><option value="ok" ${UI.histF==='ok'?'selected':''}>Finished</option><option value="failed" ${UI.histF==='failed'?'selected':''}>Failed</option></select></div>
  ${Object.keys(days).length?Object.entries(days).map(([k,rs])=>`<section><div class="eyebrow">${esc(k?dayLabel(k):'No date')} <span class="n">${rs.length}</span></div><div class="stack">${rs.map(r=>`<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(r.name)}</div><div class="meta">${esc(fmtAt(r.at))}${r.summary?' · '+esc(r.summary):''}</div></div>${r.ok?'<span class="chip ok">Done</span>':'<span class="chip rec">Failed</span>'}</div>`).join('')}</div></section>`).join(''):'<div class="empty">No results match.</div>'}`;
}
async function loadHistory(){const r=await api('/api/history').catch(()=>({results:[]}));UI.hist=r.results||[];if(UI.route==='activity')render(true);}
function dupesSec(){
 if(UI.dupes==null){loadDupes();return '<div class="none-line sub">Loading duplicate transcripts…</div>';}
 const d=UI.dupes;const pairs=d.pairs||[];
 const side=s=>`<div style="min-width:0"><a class="row-title q" style="color:inherit;text-decoration:none" href="${meetingHref(s.base)}">${esc(s.title)}</a><div class="meta">${esc(dayLabel(s.date))} · ${durText(s)}${(s.speakers||[]).length?' · '+plural(s.speakers.length,'voice'):''}</div></div>`;
 return `<div class="tools"><button class="btn" type="button" data-act="dupe-scan" ${d.scanning?'disabled':''}>${d.scanning?'Comparing transcripts…':'Compare again'}</button><span class="meta">${d.scanned?plural(pairs.length,'pair')+' found':'No comparison has run yet.'}${d.truncated?' The list is cut short.':''}</span></div>
  ${errLine('dupes')}<div class="stack">${pairs.map(p=>`<div class="row static"><div style="min-width:0;display:grid;gap:var(--s2)">${side(p.a)}${side(p.b)}<div class="meta">Match ${(+p.score).toFixed(2)}</div></div><div class="rail"><button class="btn plain" type="button" data-act="dupe-ignore" data-a="${esc(p.a.base)}" data-b="${esc(p.b.base)}">Not duplicates</button></div></div>`).join('')||(d.scanned?'<div class="empty">No duplicate transcripts.</div>':'')}</div>`;
}
async function loadDupes(){const r=await api('/api/dupes').catch(()=>({pairs:[]}));UI.dupes=r;if(UI.route==='activity')render(true);
 if(r.scanning)setTimeout(()=>{if(UI.route==='activity'&&UI.actTab==='dupes'){UI.dupes=null;render(true);}},2500);}

/* ------------------------------------------------------------ speakers ---- */
// A named person's row ends with a More button that stays hidden until the
// row is hovered (next.css), so its Samples button sat 40px left of the
// other sections' Name, Hide and Restore. This empty slot holds the same
// place at the end of those rows, so every section's text buttons share
// one right edge. It is shown only where the hidden button is.
const RAIL_SLOT='<span class="rail-slot" aria-hidden="true"></span>';
function speakersView(){
 const unk=(S.unknowns||[]).filter(u=>!u.archived).sort((a,b)=>(b.meetings||[]).length-(a.meetings||[]).length);
 const hidden=(S.unknowns||[]).filter(u=>u.archived);
 const enr=S.enrolled||[];
 if(!unk.length&&!enr.length&&!hidden.length)return `<h1>Speakers</h1><div class="empty"><strong>No voices yet</strong><span>Voices appear after the first meeting. Name one once, and it is recognized in every later meeting.</span></div>`;
 const unkRow=u=>{const n=(u.meetings||[]).length;const look=(u.looks_like||[])[0];const same=(u.suggested||[]).filter(x=>x&&x.uid).slice(0,2);
  return `<div class="row static" data-row tabindex="0" id="v-${esc(u.uid)}"><div style="min-width:0"><div class="row-title">${esc(u.display)}</div><div class="meta">In ${plural(n,'meeting')}${look?` · Looks like <span class="nb">${esc(look.name)} · <span class="mono">${(+look.score).toFixed(2)}</span></span>`:''}</div>
   ${same.length?`<div class="tools">${same.map(x=>`<button class="btn plain" type="button" data-act="link-voice" data-src="${esc(u.uid)}" data-dst="${esc(x.uid)}">Same voice as ${esc(x.display)} · <span class="mono">${(+x.score).toFixed(2)}</span></button>`).join('')}</div>`:''}</div>
   <div class="rail"><button class="icon-btn" type="button" data-act="play" data-speaker="${esc(u.uid)}" aria-label="Play ${esc(u.display)}">${ic(PLAYING===u.uid?'pause':'play')}</button><button class="btn quiet" type="button" data-act="name-voice" data-uid="${esc(u.uid)}" data-base="${esc((u.meetings||[])[0]||'')}">Name</button><button class="btn plain" type="button" data-act="hide-voice" data-uid="${esc(u.uid)}">Hide</button>${RAIL_SLOT}</div></div>`;};
 const others=enr.map(e=>e.name);
 const enrRow=e=>{const conf=UI.confirm==='rm:'+e.name;const open=UI.openSamples===e.name;const mine=S.mic_speaker===e.name;
  return `<div class="row static" data-row tabindex="0"><div style="min-width:0"><div class="row-title">${esc(e.name)}${mine?'<span class="chip draft">Your voice</span>':''}</div><div class="meta">${plural(e.samples||0,'voice sample')}${(e.sources||[]).length?' · from '+plural(e.sources.length,'meeting'):''}</div>
   ${UI.naming&&UI.naming.rename===e.name?renameForm(e):''}${open?samplesHTML(e,others):''}${errLine('spk:'+e.name)}</div>
   <div class="rail"><button class="icon-btn" type="button" data-act="play" data-speaker="${esc(e.name)}" aria-label="Play ${esc(e.name)}">${ic(PLAYING===e.name?'pause':'play')}</button>
   <button class="btn quiet" type="button" data-act="samples" data-name="${esc(e.name)}">${open?'Hide samples':'Samples'}</button>
   <button class="icon-btn" type="button" data-act="spk-menu" data-name="${esc(e.name)}" aria-label="More actions for ${esc(e.name)}">${ic('more')}</button>
   ${conf?`<button class="btn danger" type="button" data-act="rm-spk" data-name="${esc(e.name)}">Confirm remove</button>`:''}${UI.confirm==='mg:'+e.name?`<button class="btn danger" type="button" data-act="merge-force" data-name="${esc(e.name)}">Confirm merge</button>`:''}</div></div>`;};
 return `<div class="head"><div><h1>Speakers</h1><p class="meta">Your voice library stays on this Mac and carries names across meetings. Public meetings never ask.</p></div></div>
  <div class="stack sections"><section><div class="eyebrow">Unknown voices <span class="n">${unk.length}</span></div><div class="stack">${unk.map(unkRow).join('')||'<div class="none-line sub">Every voice has a name.</div>'}</div></section>
  <section><div class="eyebrow">Named people <span class="n">${enr.length}</span></div><div class="stack">${enr.map(enrRow).join('')}</div></section>
  ${hidden.length?`<section><div class="eyebrow">Hidden voices <span class="n">${hidden.length}</span></div><div class="stack">${hidden.map(u=>`<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(u.display)}</div><div class="meta">In ${plural((u.meetings||[]).length,'meeting')}</div></div><div class="rail"><button class="btn quiet" type="button" data-act="unhide-voice" data-uid="${esc(u.uid)}">Restore</button>${RAIL_SLOT}</div></div>`).join('')}</div></section>`:''}</div>`;
}
function renameForm(e){const N=UI.naming;
 return `<div class="name-form"><div class="ask-line"><input type="text" id="rn-in" value="${esc(N.value!=null?N.value:e.name)}" aria-label="New name for ${esc(e.name)}" autocomplete="off"><button class="btn primary" type="button" data-act="rn-save" ${N.busy?'disabled':''}>Rename</button><button class="btn plain" type="button" data-act="vcancel">Cancel</button></div>${N.err?`<div class="err">${esc(N.err)}</div>`:''}</div>`;}
function samplesHTML(e,others){
 const srcs=e.sources||[];
 if(!srcs.length)return `<div class="samples meta">${plural(e.samples||0,'sample')}, with no meeting left to play from.</div>`;
 const opts=others.filter(n=>n!==e.name).map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
 return `<div class="samples">${srcs.map((s,i)=>`<div class="sample"><button class="icon-btn" type="button" data-act="play" data-speaker="${esc(e.name)}" data-meeting="${esc(s)}" aria-label="Play the sample from ${esc(s)}">${ic(PLAYING===e.name+'|'+s?'pause':'play')}</button><span style="min-width:0;overflow-wrap:anywhere">${esc(s)}</span>
  <button class="btn plain" type="button" data-act="rm-sample" data-name="${esc(e.name)}" data-i="${i}">Remove</button>${opts?`<select aria-label="Move this sample to" data-move="${esc(e.name)}" data-i="${i}"><option value="">Move to…</option>${opts}</select>`:''}</div>`).join('')}</div>`;
}

/* ------------------------------------------------------------- archive ---- */
function archiveView(){
 if(UI.archived==null){loadArchived();return '<h1>Archive</h1><div class="none-line sub">Loading…</div>';}
 const a=UI.archived;
 return `<div class="head"><div><h1>Archive</h1><p class="meta">Archiving hides a recording and keeps every file until you remove it. Delete is final, so a backup is the only way back.</p></div></div>
  ${errLine('archive')}<div class="stack">${a.length?a.map(m=>{const conf=UI.confirm==='adel:'+m.base;return `<div class="row static" data-row tabindex="0"><div style="min-width:0"><div class="row-title">${esc(m.title)}</div><div class="meta">${esc(dayLabel(m.date))} · ${durText(m)}${m.category?' · '+(m.category==='work'?'Work':'Personal'):''}</div></div>
   <div class="rail"><button class="btn quiet" type="button" data-act="restore" data-base="${esc(m.base)}">Restore</button><button class="btn ${conf?'danger':'plain'}" type="button" data-act="adel" data-base="${esc(m.base)}">${conf?'Confirm delete':'Delete…'}</button></div></div>`;}).join(''):'<div class="empty">Nothing is archived.</div>'}</div>`;
}
async function loadArchived(){const r=await api('/api/archived').catch(()=>({items:[]}));UI.archived=r.items||[];if(UI.route==='archive')render(true);}

/* ------------------------------------------------------------ settings ---- */
function kv(k,d,v){return `<div class="kv"><div class="k"><span>${k}</span>${d?`<span class="meta">${d}</span>`:''}</div><div class="v">${v}</div></div>`;}
function tog(id,on,dis,label){return `<label class="toggle"><input type="checkbox" id="${id}" ${on?'checked':''} ${dis?'disabled':''} aria-label="${esc(label)}">${on?'On':'Off'}</label>`;}
function vmNote(){const v=S.voicememos||{};if(v.enabled===false)return 'Off in stt.env (STT_VOICEMEMOS_SYNC=0). Share memos to the watched folder instead.';
 return `Finished memos of 3 minutes or more${v.since?', recorded on or after '+esc(new Date(v.since.slice(0,10)+'T12:00:00').toLocaleDateString([],{month:'short',day:'numeric',year:'numeric'})):''}, arrive by themselves. ${v.delete?'Once a memo is stored and checked, it moves to Recently Deleted in Voice Memos (STT_VM_DELETE_AFTER_INGEST=1).':'The library is read only.'}`;}
/* the delete after ingest: what it moved lately, and what waits for the owner */
function vmRemovedSec(){
 const v=S.voicememos||{};const rm=v.removed||[];const rows=[];
 if(v.halted)rows.push(`<div class="row static"><div style="min-width:0"><div class="row-title q">Deletes from Voice Memos are stopped</div><div class="meta attn-t">A delete did not check out. Look in Voice Memos, then run tools/vm_delete.py --clear-halt.</div></div></div>`);
 if(v.manual)rows.push(`<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(plural(v.manual,'memo'))} to delete by hand in Voice Memos</div><div class="meta">tools/vm_delete.py --dry-run says why.</div></div></div>`);
 rm.forEach(r=>rows.push(`<div class="row static"><div style="min-width:0"><div class="row-title q">${esc(r.title||'')}</div><div class="meta">Moved to Recently Deleted in Voice Memos · ${esc(fmtAt(r.at))}</div></div><span class="chip ok">Removed</span></div>`));
 return rows.length?`<section><div class="eyebrow">Removed from Voice Memos</div><div class="stack">${rows.join('')}</div></section>`:'';
}
function settingsView(){
 const sc=S.schedule||{};const master=!S.paused;const inst=!!sc.installed;
 const hh=sc.hour!=null?String(sc.hour).padStart(2,'0')+':'+String(sc.minute||0).padStart(2,'0'):'23:00';
 const pick=(S.asr_choices||[]).filter(c=>!c.cloud||(S.cloud_keys||{})[c.cloud]);
 const model=`<select id="set-model" aria-label="Transcription model">${pick.map(c=>`<option value="${esc(c.id)}" ${c.id===S.model?'selected':''}>${esc(c.label)}</option>`).join('')}</select>`;
 const lb=S.llm_backends||{};const LBN={local:'On this Mac',anthropic:'Anthropic (cloud)',openai:'OpenAI (cloud)'};
 const asst=`<select id="set-llm" aria-label="Assistant">${Object.keys(lb).map(b=>`<option value="${esc(b)}" ${b===S.llm_backend?'selected':''} ${lb[b]?'':'disabled'}>${esc(LBN[b]||b)}${lb[b]?'':' (not set up)'}</option>`).join('')}</select>`;
 const you=`<select id="set-mic" aria-label="Your voice"><option value="">Not set</option>${(S.enrolled||[]).map(e=>`<option value="${esc(e.name)}" ${e.name===S.mic_speaker?'selected':''}>${esc(e.name)}</option>`).join('')}</select>`;
 const ck=S.cloud_keys||{};
 const keyRow=(p,l)=>`<label>${l} ${ck[p]?'<span class="ok-t">set</span>':'<span>not set</span>'}<span class="tools"><input type="password" id="ck-${p}" autocomplete="off" placeholder="${ck[p]?'Paste a new key to replace it':'Paste a key'}" style="flex:1 1 220px">${ck[p]?`<button class="btn plain" type="button" data-act="ck-clear" data-p="${p}">Remove</button>`:''}</span></label>`;
 const th=document.documentElement.getAttribute('data-theme')||'auto';
 const upd=UI.updates?(UI.updates.busy?'Checking…':(UI.updates.models||[]).map(m=>`${esc(m.label)}: ${m.update_available?'update available':(m.update_available===false?'up to date':esc(m.note||'unknown'))}`).join('<br>')):'';
 const fix=UI.err['recfix'];
 return `<h1>Settings</h1><div class="stack sections">
  <section><div class="eyebrow">Automation</div><div>
   ${kv('Automatic runs','The master switch over the folder watch, the nightly run, and login catch-up. Manual runs always work.',tog('set-master',master,false,'Automatic runs'))}
   ${kv('Folder watch',!inst?'Not installed. Run ./setup.sh install-agent.':'New recordings process within moments of landing.',tog('set-watch',!!sc.watch,!inst||!master,'Folder watch'))}
   ${kv('Nightly run',!inst?'Not installed. Run ./setup.sh install-agent.':'Processes everything new at a set time.',`<input type="time" id="set-time" value="${hh}" aria-label="Nightly run time" ${!inst||!master||!sc.nightly?'disabled':''}>${tog('set-nightly',!!sc.nightly,!inst||!master,'Nightly run')}`)}
   ${kv('Voice Memos sync',vmNote(),`<span>${(S.voicememos||{}).enabled===false?'Off':'On'}</span>`)}
  </div>${errLine('auto')}</section>
  <section><div class="eyebrow">Transcription</div><div>
   ${kv('Model',esc(((S.asr_choices||[]).find(c=>c.id===S.model)||{}).note||''),model)}
   ${kv('Punctuation cleanup','A light pass that fixes capitals and stops.',tog('set-punct',!!S.punctuate,false,'Punctuation cleanup'))}
   ${kv('Speed calibration',esc((S.rates||{}).text||'No runs measured yet'),`<span class="mono">${plural((S.rates||{}).runs||0,'run')}</span>`)}
   ${kv('Model updates',upd,`<button class="btn" type="button" data-act="check-updates">Check</button>`)}
  </div>${errLine('model')}</section>
  <section><div class="eyebrow">Summaries and Ask</div><div>${kv('Assistant','A strict meeting always stays on this Mac.',asst)}</div>${errLine('llm')}</section>
  <section><div class="eyebrow">Cloud keys</div><div class="form">${keyRow('scribe','ElevenLabs Scribe')}${keyRow('openai','OpenAI')}${keyRow('voxtral','Mistral Voxtral')}${keyRow('anthropic','Anthropic (assistant)')}
   <div><button class="btn" type="button" data-act="ck-save">Save keys</button></div>${errLine('keys')}</div></section>
  <section><div class="eyebrow">Recorder</div><div>
   ${kv('Your voice','The microphone side of a recording is labeled with this name.',you)}
   ${kv('Microphone permission',fix?esc(fix):'If recordings come out silent, reset the permission and grant both prompts on the next Start.','<button class="btn" type="button" data-act="rec-fix">Reset permission</button>')}
  </div></section>
  <section><div class="eyebrow">Storage</div><div>
   ${kv('Watched folder',esc((S.paths||{}).source||''),'<button class="btn" type="button" data-act="pick-folder" data-which="source">Change…</button>')}
   ${kv('Transcripts folder',esc((S.paths||{}).dest||''),'<button class="btn" type="button" data-act="pick-folder" data-which="dest">Change…</button>')}
   ${UI.folderAsk?`<div class="ask-card"><div class="row-title">Use ${esc(UI.folderAsk.path)}?</div><div class="meta attn-t">${esc(UI.folderAsk.warn)}</div><div class="tools"><button class="btn primary" type="button" data-act="folder-confirm">Use this folder</button><button class="btn quiet" type="button" data-act="folder-cancel">Cancel</button></div></div>`:''}
  </div>${errLine('folder')}</section>
  <section><div class="eyebrow">Appearance</div><div>${kv('Theme','Auto follows macOS.',`<select id="set-theme" aria-label="Theme">${['auto','light','dark'].map(t=>`<option value="${t}" ${t===th?'selected':''}>${t[0].toUpperCase()+t.slice(1)}</option>`).join('')}</select>`)}</div></section>
 </div>`;
}

/* ------------------------------------------------------------ overlays ---- */
function layersHTML(){
 let h='';
 if(UI.menu)h+=`<div class="veil" data-act="close-layer"></div><div class="menu-sheet" role="dialog" aria-label="Menu"><div class="side-top"><span class="wordmark">Meetings</span><button class="icon-btn" type="button" data-act="close-layer" aria-label="Close">${ic('close')}</button></div><button class="btn primary new" type="button" data-act="new">${ic('plus')}New</button>${navHTML()}<button class="nav-item" type="button" data-act="theme">${ic('theme')}<span>${themeLabel()}</span></button></div>`;
 if(UI.palette)h+=`<div class="veil" data-act="close-layer"></div><div class="palette" role="dialog" aria-label="Search"><input type="search" id="pal-in" placeholder="Search titles and words" aria-label="Search" value="${esc(UI.pq)}" autocomplete="off"><div class="res" id="pal-res">${palResults()}</div></div>`;
 if(UI.keys)h+=`<div class="veil" data-act="close-layer"></div><div class="palette keysheet" role="dialog" aria-label="Keyboard keys"><div class="keysheet-head"><strong>Keys</strong><button class="icon-btn" type="button" data-act="close-layer" aria-label="Close">${ic('close')}</button></div><div class="keys">${KEYS.map(([k,d])=>`<span>${k.split(' ').map(x=>/^(or|then)$/.test(x)?`<span class="sub">${x}</span>`:`<kbd>${esc(x)}</kbd>`).join(' ')}</span><span>${esc(d)}</span>`).join('')}</div></div>`;
 $('#layers').innerHTML=h;
}
const KEYS=[['/ or Cmd K','Search titles and words (/ on a Transcript tab finds in it)'],['g then h','Go to Home'],['g then r','Go to Recordings'],['g then g','Go to Groups'],['g then q','Go to Ask'],['g then a','Go to Activity'],['g then s','Go to Speakers'],['g then e','Go to Archive'],['g then ,','Go to Settings'],['j k','Move through rows'],['Enter','Open the row, keep the title, save the voice'],['x','Select the row'],
 ['1 2 3','Summary, Transcript, Ask (on a meeting)'],['[ ]','Previous or next recording'],['Space','Play or pause. In the voice sheet, replay the clip'],['v','Name voices'],['n p','Next or previous uncertain line'],['a u','Accept and go on, or use the second reading'],['Cmd F','Find in the transcript'],['Cmd Enter','Save the line card'],
 ['1 2 3 or s','In the voice sheet: fill from a chip, or skip'],['Esc','Back out one layer, then back to the list'],['?','This sheet']];
// match in the raw text, then escape each slice, so a mark never lands inside an entity
function hl(text,q){const t=String(text==null?'':text);if(!q)return esc(t);const i=t.toLowerCase().indexOf(String(q).toLowerCase());if(i<0)return esc(t);const j=i+String(q).length;return esc(t.slice(0,i))+'<mark>'+esc(t.slice(i,j))+'</mark>'+esc(t.slice(j));}
// inline search filters (plan Phase 4E): the server reads them from the
// query; the palette leaves them out of its own title match and highlight
const PAL_FILTER=/(?:^|\s)(?:after|before|route|dated|from|in):(?:"[^"]*"|\S+)/gi;
function palText(){return UI.pq.replace(PAL_FILTER,' ').replace(/\s+/g,' ').trim();}
function palResults(){
 const q=palText();const lq=q.toLowerCase();const filtered=q!==UI.pq.trim();
 if(q.length<2)return `<div class="meta pop-note">Type a word someone said, a title, or a name.</div>`+[...meetings()].slice(0,5).map(m=>`<a class="hit" href="${meetingHref(m.base)}"><div class="hit-t">${esc(m.title)}</div><div class="meta">${esc(whenText(m))}</div></a>`).join('');
 const r=UI.pres&&UI.pres.q===UI.pq.trim()?UI.pres:null;
 if(r&&r.error)return `<div class="err pop-note">${esc(r.error)}</div>`;
 // with a filter, only the server knows which meetings pass it
 const local=filtered?[]:meetings().filter(m=>m.title.toLowerCase().includes(lq)||(m.named||[]).some(n=>n.toLowerCase().includes(lq)));
 const fromIdx=r?(r.meetings||[]).map(x=>byBase(x.base)).filter(Boolean):[];
 const titles=[...local,...fromIdx].filter((m,i,a)=>a.indexOf(m)===i).slice(0,6);
 const said=r?(r.hits||[]).slice(0,10):[];
 const pending=q.length>=2&&!r;
 // a stop word alone ('is') is not searched: say so, never 'No matches.'
 const note=r&&r.note?`<div class="meta pop-note">${esc(r.note)}</div>`:'';
 if(!titles.length&&!said.length)return pending?'<div class="empty">Searching…</div>':(note||'<div class="empty">No matches.</div>');
 const tby=b=>{const m=byBase(b);return m?m.title:b;};
 return (titles.length?`<div class="eyebrow">Titles <span class="n">${titles.length}</span></div>`:'')+titles.map(m=>`<a class="hit" href="${meetingHref(m.base)}"><div class="hit-t">${hl(m.title,q)}</div><div class="meta">${esc(whenText(m))} · ${durText(m)}</div></a>`).join('')
  +(said.length?`<div class="eyebrow">Said in <span class="n">${r.total>said.length?said.length+' of '+r.total:said.length}</span></div>`:'')
  +said.map(h=>`<a class="hit" href="${meetingHref(h.base,h.start)}"><div class="meta">${esc(tby(h.base))}</div><div>${hl(h.snippet||h.text||'',q)}</div><div class="meta"><span class="mono">${fmtT(h.start)}</span>${h.who?' · '+esc(h.who):''}</div></a>`).join('')
  +(pending?'<div class="meta pop-note">Searching transcripts…</div>':'')+note;
}
let palTimer=null,palSeq=0;
// the query goes as typed: two letters in capitals ('AI', 'IT', 'HR') search
// as an acronym, so the server needs the case (stt/search.py _short)
function palSearch(){
 clearTimeout(palTimer);const q=UI.pq.trim();
 if(q.length<2){UI.pres=null;return;}
 palTimer=setTimeout(async()=>{const seq=++palSeq;
  const [a,b]=await Promise.all([api('/api/search?q='+encodeURIComponent(q)).catch(()=>({hits:[]})),api('/api/search_meetings?q='+encodeURIComponent(q)).catch(()=>({meetings:[]}))]);
  if(seq!==palSeq||UI.pq.trim()!==q)return;   // a stale answer
  UI.pres={q,hits:a.hits||[],total:a.total||0,meetings:b.meetings||[],error:a.error||'',note:a.note||''};
  const box=$('#pal-res');if(box)box.innerHTML=palResults();},180);
}
function showPop(anchor,html){const p=$('#pop');p.innerHTML=html;p.hidden=false;const r=anchor.getBoundingClientRect();p.style.left='0';p.style.top='0';
 const w=p.offsetWidth,h=p.offsetHeight;let x=Math.min(r.left,innerWidth-w-12),y=r.bottom+6;if(y+h>innerHeight-8)y=Math.max(8,r.top-h-6);
 p.style.left=Math.max(12,x)+'px';p.style.top=y+'px';const f=p.querySelector('input,button');if(f)f.focus();POP_ANCHOR=anchor;}
let POP_ANCHOR=null;
function hidePop(){const p=$('#pop');p.hidden=true;p.innerHTML='';POP_ANCHOR=null;}
function rowMenu(base){const m=byBase(base)||{};
 return `<button class="mi" type="button" data-act="open" data-base="${esc(base)}">Open</button><button class="mi" type="button" data-act="select" data-base="${esc(base)}">Select</button><hr>
  <button class="mi" type="button" data-act="pop-rename" data-base="${esc(base)}">Rename…</button><button class="mi" type="button" data-act="pop-date" data-base="${esc(base)}">Set date…</button>
  <button class="mi" type="button" data-act="cat" data-base="${esc(base)}" data-v="${m.category==='work'?'':'work'}">${m.category==='work'?'Clear Work tag':'Tag Work'}</button><button class="mi" type="button" data-act="cat" data-base="${esc(base)}" data-v="${m.category==='personal'?'':'personal'}">${m.category==='personal'?'Clear Personal tag':'Tag Personal'}</button><hr>
  <button class="mi" type="button" data-act="export" data-base="${esc(base)}" data-fmt="docx">Export Word</button><button class="mi" type="button" data-act="export" data-base="${esc(base)}" data-fmt="pdf">Export PDF</button>
  <button class="mi" type="button" data-act="copy" data-base="${esc(base)}">Copy transcript</button><button class="mi" type="button" data-act="export" data-base="${esc(base)}" data-fmt="reveal">Show files</button><hr>
  <button class="mi" type="button" data-act="pop-redo" data-base="${esc(base)}">Redo…<small>Rebuilds from the stored audio</small></button>
  <button class="mi" type="button" data-act="archive" data-base="${esc(base)}">Archive</button><button class="mi danger" type="button" data-act="pop-del" data-base="${esc(base)}">Delete…</button>${m.group?groupMenuItem(base,m.group):''}`;}
function popForm(kind,base,label,value,type){
 return `<div class="pop-form"><label class="meta" for="pf-in">${esc(label)}</label><input type="${type||'text'}" id="pf-in" value="${esc(value)}" data-kind="${kind}" data-base="${esc(base)}"><div class="err" id="pf-err" hidden></div>
  <div class="row-btns"><button class="btn plain" type="button" data-act="cancel-pop">Cancel</button><button class="btn primary" type="button" data-act="pf-save">Save</button></div></div>`;}

/* -------------------------------------------------------------- render ---- */
function focusGuard(){const a=document.activeElement;return a&&$('#view').contains(a)&&a.matches('input:not([type=checkbox]),select,textarea');}
// Repaint only on a real change, and put keyboard focus back where it was: a
// focused row by its meeting, any other control by its id.
function keepFocus(fn){
 const a=document.activeElement;const row=a&&a.matches&&a.matches('[data-row]')?a.dataset.base:null;const id=a&&a.id;
 const pos=id&&a.selectionStart;fn();
 if(row){const el=document.querySelector(`#view [data-row][data-base="${CSS.escape(row)}"]`);if(el)el.focus();}
 else if(id){const el=document.getElementById(id);if(el&&el!==a&&$('#view').contains(el)){el.focus();try{if(pos!=null)el.setSelectionRange(pos,pos);}catch(e){}}}
}
let LAST_SIG='',REC_SIG='';
function render(force){
 if(!S)return;
 renderFrame();
 if(UI.route==='meeting'){LAST_SIG='';mRender(force);renderBulk();return;}
 const views={home:homeView,recordings:recordingsView,groups:groupsView,ask:askView,activity:activityView,speakers:speakersView,archive:archiveView,settings:settingsView};
 // a 2s poll must never wipe a field being typed into. The frame repaints
 // anyway, the Recordings list below its filter keeps up, and the rest of
 // the page catches up on the next poll after the field loses focus.
 if(!force&&focusGuard()){if(UI.route==='recordings')renderRecList();renderBulk();return;}
 const html=(views[UI.route]||homeView)();
 const sig=UI.route+'\u0001'+html;
 if(force||sig!==LAST_SIG){keepFocus(()=>{$('#view').innerHTML=html;LAST_SIG=sig;REC_SIG='';if(UI.route==='recordings')renderRecList();});}
 else if(UI.route==='recordings')renderRecList();
 $('#view').classList.toggle('has-sel',UI.sel.size>0);
 renderBulk();
}
function renderBulk(){
 const b=$('#bulk');
 if(!UI.sel.size||UI.route!=='recordings'){b.hidden=true;b.innerHTML='';return;}
 const n=UI.sel.size;const cd=UI.confirm==='bulk-del',ca=UI.confirm==='bulk-audio';
 b.hidden=false;
 b.innerHTML=`<strong class="mono">${n}</strong><button class="btn" type="button" data-act="bulk" data-a="accept">Keep titles</button><button class="btn" type="button" data-act="bulk" data-a="category" data-v="work">Work</button><button class="btn" type="button" data-act="bulk" data-a="category" data-v="personal">Personal</button><button class="btn" type="button" data-act="bulk" data-a="category" data-v="">Clear tag</button>
  <button class="btn" type="button" data-act="bulk-form" data-a="rename">Rename…</button><button class="btn" type="button" data-act="bulk-form" data-a="date">Set date…</button><button class="btn" type="button" data-act="bulk" data-a="archive">Archive</button>
  <button class="btn ${ca?'danger':''}" type="button" data-act="bulk" data-a="drop_audio">${ca?'Confirm: delete audio':'Delete audio…'}</button><button class="btn ${cd?'danger':''}" type="button" data-act="bulk" data-a="delete">${cd?`Confirm: delete ${n}`:'Delete…'}</button>
  <button class="btn plain" type="button" data-act="sel-all">Select all shown</button><button class="icon-btn" type="button" data-act="sel-clear" aria-label="Clear selection">${ic('close')}</button>`;
}
let toastTimer;
function toast(msg){const t=$('#toast');t.hidden=false;t.textContent=msg;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{t.hidden=true;},3600);}

/* -------------------------------------------------------------- routes ---- */
function route(){
 const h=(location.hash||'').slice(1)||'home';
 hidePop();UI.menu=false;UI.palette=false;UI.keys=false;UI.confirm=null;
 // #m/<base> and #m/<base>/t/<secs>: the meeting page (next-meeting.js)
 const mh=parseMeetingHash(h);
 // #groups/<key> (next-library.js)
 const lh=mh?null:parseLibHash(h);
 const r=mh?'meeting':lh?lh.r:TITLES[h]?h:'home';
 const sameMeeting=mh&&UI.route==='meeting'&&UI.mbase===mh.base;
 if(r!==UI.route||(mh&&!sameMeeting)){UI.sel.clear();UI.naming=null;if(r==='archive')UI.archived=null;if(r==='activity'){UI.hist=null;UI.dupes=null;}if(r==='groups'){LIB.groups=null;LIB.gundo=null;LIB.gseen=null;}}
 if(lh)libEnter(lh);
 if(r!=='meeting'){mTeardown();UI.mbase=null;}
 // the voice sheet stays only for a hand-off into the meeting it walks
 if(VS&&!(mh&&VCARRY&&VCARRY.base===mh.base))vsClose();
 if(mh){UI.mbase=mh.base;UI.mseek=mh.seek;if(sameMeeting&&mh.seek!=null&&MP&&MP.loaded){UI.route=r;layersHTML();mSeekLine(mh.seek);return;}}
 // Select from a Home row menu carries its selection across the clear above
 const c=UI.carry;UI.carry=null;const carried=c&&c.to===r;
 if(carried){(c.sel||[]).forEach(b=>UI.sel.add(b));}
 UI.route=r;document.title=tabTitle();
 layersHTML();render(true);if(!sameMeeting)window.scrollTo(0,0);
 if(carried&&c.after)c.after();
}
function goCarry(to,c){UI.carry=Object.assign({to},c);go(to);}
function go(to){if(location.hash==='#'+to)route();else location.hash=to;}
// the Recordings list's order as it stands: the filter, the chip, the sort.
// A meeting's previous and next walk it. Outside the filter, all of it.
function recOrder(){
 let rows=filtered();if(UI.mbase&&!rows.some(m=>m.base===UI.mbase))rows=meetings().slice();
 const by={old:(a,b)=>String(a.date).localeCompare(String(b.date)),long:(a,b)=>(b.minutes||0)-(a.minutes||0),az:(a,b)=>a.title.localeCompare(b.title)}[UI.sort]
  ||((a,b)=>String(b.date).localeCompare(String(a.date))||String(b.recorded_at||'').localeCompare(String(a.recorded_at||'')));
 return [...rows].sort(by).map(m=>m.base);
}

/* ------------------------------------------------------------- actions ---- */
async function act(path,body,errKey,after){
 const r=await api(path,body).catch(()=>({error:'Could not reach the panel.'}));
 if(r.error||r.ok===false){if(errKey){UI.err[errKey]=r.error||'That did not work.';}else toast(r.error||'That did not work.');}
 else if(errKey)delete UI.err[errKey];
 if(after)after(r);
 await refresh();render(true);
 return r;
}
let PLAYER=null,PLAYING=null;
function play(el){
 const spk=el.dataset.speaker,mtg=el.dataset.meeting||'';const key=mtg?spk+'|'+mtg:spk;
 if(PLAYER){PLAYER.pause();PLAYER=null;}
 if(PLAYING===key){PLAYING=null;render(true);return;}
 PLAYING=key;render(true);
 PLAYER=new Audio('/api/snippet?speaker='+encodeURIComponent(spk)+(mtg?'&meeting='+encodeURIComponent(mtg):'')+'&secs=30');
 const stop=()=>{if(PLAYING===key){PLAYING=null;PLAYER=null;render(true);}};
 PLAYER.onended=stop;PLAYER.onerror=()=>{stop();toast('No clip could be cut for this voice.');};
 PLAYER.play().catch(stop);
}
const A={
 // the skip link: focus the page column. A hash link would be read as a route.
 skip(){const v=$('#view');if(v&&v.focus)v.focus();},
 go(el){go(el.dataset.to);},
 theme(){const r=document.documentElement;const cur=r.getAttribute('data-theme')||'auto';const nx={auto:'light',light:'dark',dark:'auto'}[cur];setTheme(nx);},
 menu(){UI.menu=true;layersHTML();},
 'close-layer'(){closeLayer();},
 keys(){UI.keys=true;layersHTML();},
 palette(){UI.palette=true;UI.pq='';UI.pres=null;layersHTML();const i=$('#pal-in');if(i)i.focus();},
 new(el){showPop(el,`<button class="mi" type="button" data-act="import">Import files<small>Audio or video. You can also drop files anywhere on this page.</small></button>
  <div class="pop-note"><strong>Record on this Mac</strong><span class="meta">Use Start recording in the menu bar. It appears here when you stop.</span></div>
  <div class="pop-note"><strong>From the iPhone</strong><span class="meta">Voice Memos sync is on: a finished memo of 3 minutes or more arrives by itself.</span></div>`);},
 async import(){hidePop();const r=await api('/api/pick_files',{}).catch(()=>({}));if(r.cancelled||!r.paths)return;
  await act('/api/run',{paths:r.paths,...runOpts()},'activity');toast(plural(r.paths.length,'file')+' added to the queue.');go('activity');},
 'row-menu'(el){if(!$('#pop').hidden&&POP_ANCHOR===el){hidePop();return;}showPop(el,rowMenu(el.dataset.base));},
 'spk-menu'(el){const n=el.dataset.name;const others=(S.enrolled||[]).filter(e=>e.name!==n);
  showPop(el,`<button class="mi" type="button" data-act="rn-open" data-name="${esc(n)}">Rename…</button>${others.length?`<div class="pop-form"><label class="meta" for="mg-sel">Merge into</label><select id="mg-sel">${others.map(e=>`<option value="${esc(e.name)}">${esc(e.name)}</option>`).join('')}</select><div class="row-btns"><button class="btn" type="button" data-act="merge" data-name="${esc(n)}">Merge</button></div></div>`:''}<hr><button class="mi danger" type="button" data-act="rm-ask" data-name="${esc(n)}">Remove…<small>Forgets the voice. Meetings keep the name.</small></button>`);},
 open(el){hidePop();location.hash=meetingHref(el.dataset.base).slice(1);},
 select(el){hidePop();const b=el.dataset.base;if(UI.route!=='recordings')goCarry('recordings',{sel:[b]});else{UI.sel.add(b);render(true);}},
 'cancel-pop'(){hidePop();},
 'pop-rename'(el){const m=byBase(el.dataset.base)||{};showPop(POP_ANCHOR||el,popForm('rename',el.dataset.base,'New title',m.title||''));},
 'pop-date'(el){const m=byBase(el.dataset.base)||{};showPop(POP_ANCHOR||el,popForm('date',el.dataset.base,'Date (YYYY-MM-DD)',m.date||'','text'));},
 'pop-redo'(el){const m=byBase(el.dataset.base)||{};showPop(POP_ANCHOR||el,`<div class="pop-note"><strong>Redo ${esc(m.title||'')}?</strong><span class="meta">Re-runs transcription and speaker detection from the stored audio. The transcript is replaced, and any manual edits are archived next to it.</span></div>${m.audio?`<button class="mi" type="button" data-act="redo" data-base="${esc(m.base)}">Redo</button>`:'<div class="pop-note err">The stored audio is missing, so a Redo cannot run.</div>'}<button class="mi" type="button" data-act="cancel-pop">Cancel</button>`);},
 async redo(el){const m=byBase(el.dataset.base)||{};hidePop();if(!m.audio)return;await act('/api/run',{paths:[m.audio],force:true,...runOpts(),parallel:1});toast('Redo queued.');},
 'pop-del'(el){const m=byBase(el.dataset.base)||{};showPop(POP_ANCHOR||el,`<div class="pop-note"><strong>Delete ${esc(m.title||'')}?</strong><span class="meta">This is final. The audio and transcript are removed, and only a backup can bring them back.</span></div><button class="mi danger" type="button" data-act="del" data-base="${esc(el.dataset.base)}">Delete</button><button class="mi" type="button" data-act="archive" data-base="${esc(el.dataset.base)}">Archive instead</button><button class="mi" type="button" data-act="cancel-pop">Cancel</button>`);},
 async del(el){hidePop();const b=el.dataset.base;UI.sel.delete(b);const r=await act('/api/delete_meeting',{base:b,confirm:true});if(r.ok!==false&&!r.error){toast('Deleted.');if(UI.route==='meeting'&&UI.mbase===b)go('recordings');}},
 async archive(el){hidePop();const b=el.dataset.base;UI.sel.delete(b);const r=await act('/api/archive_meeting',{base:b});if(r.ok!==false&&!r.error){toast('Archived. Restore it from Archive.');if(UI.route==='meeting'&&UI.mbase===b)go('recordings');}},
 async cat(el){hidePop();await act('/api/set_category',{base:el.dataset.base,category:el.dataset.v});},
 async export(el){hidePop();const f=el.dataset.fmt;const r=await act('/api/export',{base:el.dataset.base,fmt:f});if(r.ok!==false&&!r.error)toast(f==='reveal'?'Shown in Finder.':'Exported and shown in Finder.');},
 async copy(el){hidePop();try{const r=await fetch('/api/txt?base='+encodeURIComponent(el.dataset.base));const t=await r.text();if(!r.ok)throw 0;await navigator.clipboard.writeText(t);toast('Copied the transcript.');}catch(e){toast('Could not copy the transcript.');}},
 async 'pf-save'(){const i=$('#pf-in');if(!i)return;const k=i.dataset.kind,b=i.dataset.base,v=i.value.trim();const err=$('#pf-err');
  let r;if(k==='rename')r=await api('/api/rename',{base:b,new:v});else if(k==='date')r=await api('/api/set_date',{base:b,date:v});
  else if(k==='bulk-rename'||k==='bulk-date'){r=await api('/api/bulk',{action:k==='bulk-rename'?'rename':'date',bases:[...UI.sel],value:v});}
  if(!r||r.error||r.ok===false){err.hidden=false;err.textContent=(r&&r.error)||((r&&r.results)||[]).filter(x=>!x.ok).map(x=>x.error).filter(Boolean)[0]||'That did not work.';return;}
  hidePop();if(k.startsWith('bulk'))UI.sel.clear();await refresh();
  // a note says what the save did not do as typed (a future date kept in the title)
  if(r.base&&r.base!==b&&UI.route==='meeting'&&UI.mbase===b){mFollow(b,r.base);toast(r.note||'Saved.');return;}
  render(true);toast(r.note||'Saved.');},
 async keep(el){const b=el.dataset.base;const i=document.getElementById('nt-'+b);const name=i?i.value.trim():'';
  await act('/api/accept_meeting',{base:b,name:name||undefined},'keep:'+b);toast('Title kept.');},
 async 'keep-all'(){if(UI.confirm!=='keep-all'){UI.confirm='keep-all';render(true);return;}UI.confirm=null;
  const bs=meetings().filter(m=>m.title_state==='provisional'&&m.route!=='public_download'&&arrivedDays(m)<=NEEDS_DAYS).map(m=>m.base);
  const r=await act('/api/bulk',{action:'accept',bases:bs});toast(`Kept ${r.n_ok||0} titles.`);},
 chip(el){UI.chip=UI.chip===el.dataset.k?'all':el.dataset.k;renderRecList();},
 fold(el){const k=el.dataset.k;const cur=el.getAttribute('aria-expanded')!=='true';UI.fold[k]=!cur;renderRecList();},
 async bulk(el){const a=el.dataset.a;const two=a==='delete'||a==='drop_audio';const ck=a==='delete'?'bulk-del':'bulk-audio';
  if(two&&UI.confirm!==ck){UI.confirm=ck;renderBulk();return;}UI.confirm=null;
  const bs=[...UI.sel];const r=await api('/api/bulk',{action:a,bases:bs,value:el.dataset.v,confirm:two}).catch(()=>null);
  if(!r){toast('Could not reach the panel.');return;}
  const bad=(r.results||[]).filter(x=>!x.ok);
  if(['archive','delete'].includes(a))UI.sel.clear();
  toast(bad.length?`${r.n_ok} done. ${bad.length} could not be changed: ${bad[0].error||'error'}`:`${r.n_ok} done.${r.freed_mb?' Freed '+r.freed_mb+' MB.':''}`);
  await refresh();render(true);},
 'bulk-form'(el){const a=el.dataset.a;showPop(el,popForm('bulk-'+a,'',a==='rename'?`New title for ${UI.sel.size} recordings`:'Date (YYYY-MM-DD)',''));},
 'sel-all'(){filtered().forEach(m=>UI.sel.add(m.base));render(true);},
 'sel-clear'(){UI.sel.clear();UI.confirm=null;render(true);},
 acttab(el){UI.actTab=el.dataset.k;if(el.dataset.k==='history')UI.hist=null;if(el.dataset.k==='dupes')UI.dupes=null;render(true);},
 async 'run-all'(){await act('/api/run',{...runOpts()},'activity');toast('Processing all new recordings.');},
 async 'run-one'(el){await act('/api/run',{files:[el.dataset.name],...runOpts()},'q:'+el.dataset.name);toast('Queued.');},
 async hold(el){await act('/api/queue_hold',{name:el.dataset.name},'q:'+el.dataset.name);},
 async qdel(el){const n=el.dataset.name;if(UI.confirm!=='qdel:'+n){UI.confirm='qdel:'+n;render(true);return;}UI.confirm=null;await act('/api/queue_delete',{name:n,confirm:true},'q:'+n);},
 async 'dismiss-fail'(el){await act('/api/dismiss_failure',{name:el.dataset.name},'f:'+el.dataset.name);},
 async pause(){await act(S.paused?'/api/resume':'/api/pause',{},'activity');},
 async stop(){if(UI.confirm!=='stop'){UI.confirm='stop';render(true);return;}UI.confirm=null;await act('/api/stop',{},'activity');toast('Stopped.');},
 async unqueue(el){await act('/api/unqueue',{at:+el.dataset.at},'activity');},
 async 'dupes-del'(){if(UI.confirm!=='dupes-del'){UI.confirm='dupes-del';render(true);return;}UI.confirm=null;await act('/api/queue_delete_dupes',{confirm:true},'dupes');},
 async 'dupe-scan'(){await act('/api/dupe_scan',{go:true},'dupes');UI.dupes=null;render(true);},
 async 'dupe-ignore'(el){await act('/api/dupe_ignore',{a:el.dataset.a,b:el.dataset.b},'dupes');UI.dupes=null;render(true);},
 play(el){play(el);},
 // the voice sheet, over whatever page is open. A Home card knows the
 // meeting the voice was heard in, so the sheet shows what it said there.
 'name-voice'(el){vsOpenUid(el.dataset.uid,el.dataset.base||null);},
 vcancel(){UI.naming=null;render(true);},
 async 'hide-voice'(el){await act('/api/hide_unknown',{uid:el.dataset.uid,hide:true});toast('Hidden. Restore it under Hidden voices.');},
 async 'link-voice'(el){const r=await act('/api/merge_speakers',{src:'uid:'+el.dataset.src,dst:'uid:'+el.dataset.dst});if(r.ok!==false&&!r.error)toast('Linked. The two voices are one now.');},
 async 'unhide-voice'(el){await act('/api/hide_unknown',{uid:el.dataset.uid,hide:false});},
 samples(el){UI.openSamples=UI.openSamples===el.dataset.name?null:el.dataset.name;render(true);},
 'rn-open'(el){hidePop();UI.naming={rename:el.dataset.name,value:null};render(true);const i=$('#rn-in');if(i){i.focus();i.select();}},
 async 'rn-save'(){const N=UI.naming;const i=$('#rn-in');const v=(i?i.value:'').trim();if(!v||!N){return;}N.busy=true;
  const r=await api('/api/rename_speaker',{name:N.rename,new:v}).catch(()=>null);N.busy=false;
  // onto an enrolled person whose voice does not match: the merge confirm
  if(r&&r.need_confirm){UI.naming=null;UI.confirm='mg:'+N.rename;UI.mergeTo=r.dst;UI.err['spk:'+N.rename]=r.error;render(true);return;}
  if(!r||!r.ok){N.err=(r&&r.error)||'Could not rename. The new name may already exist; merge instead.';N.value=v;render(true);return;}
  UI.naming=null;toast('Renamed. The meetings update in the background.');await refresh();render(true);},
 async merge(el){const dst=($('#mg-sel')||{}).value;hidePop();if(!dst)return;const n=el.dataset.name,r=await act('/api/merge_speakers',{src:'name:'+n,dst:'name:'+dst},'spk:'+n);if(r.need_confirm){UI.confirm='mg:'+n;UI.mergeTo=dst;render(true);return;}if(r.ok!==false&&!r.error)toast(`Merged into ${dst}.`);},
 async 'merge-force'(el){const n=el.dataset.name,dst=UI.mergeTo;UI.confirm=null;UI.mergeTo=null;if(!dst)return;const r=await act('/api/merge_speakers',{src:'name:'+n,dst:'name:'+dst,confirm:true},'spk:'+n);if(r.ok!==false&&!r.error)toast(`Merged into ${dst}.`);},
 'rm-ask'(el){hidePop();UI.confirm='rm:'+el.dataset.name;render(true);},
 async 'rm-spk'(el){UI.confirm=null;await act('/api/remove_speaker',{name:el.dataset.name},'spk:'+el.dataset.name);toast('Removed.');},
 async 'rm-sample'(el){await act('/api/remove_sample',{name:el.dataset.name,index:+el.dataset.i},'spk:'+el.dataset.name);},
 async restore(el){await act('/api/restore_meeting',{base:el.dataset.base},'archive');UI.archived=null;render(true);toast('Restored.');},
 async adel(el){const b=el.dataset.base;if(UI.confirm!=='adel:'+b){UI.confirm='adel:'+b;render(true);return;}UI.confirm=null;await act('/api/delete_meeting',{base:b,confirm:true},'archive');UI.archived=null;render(true);toast('Deleted.');},
 async 'check-updates'(){UI.updates={busy:true};render(true);const r=await api('/api/check_updates').catch(()=>({models:[]}));UI.updates=r;render(true);},
 async 'rec-fix'(){const r=await api('/api/fix_recorder_permissions',{}).catch(()=>({error:'Could not reach the panel.'}));UI.err['recfix']=r.message||r.error||'';render(true);},
 async 'pick-folder'(el){const w=el.dataset.which;UI.folderAsk=null;const r=await act('/api/pick_folder',{which:w,prompt:w==='source'?'Choose the watched folder':'Choose the transcripts folder'},'folder');if(r&&r.need_confirm){UI.folderAsk={which:r.which,path:r.path,warn:r.warn};render(true);}},
 async 'folder-confirm'(){const a=UI.folderAsk;UI.folderAsk=null;if(a)await act('/api/set_folder',{which:a.which,path:a.path,confirm:true},'folder');},
 'folder-cancel'(){UI.folderAsk=null;render(true);},
 async 'ck-save'(){const body={};['scribe','openai','voxtral','anthropic'].forEach(p=>{const i=$('#ck-'+p);if(i&&i.value.trim())body[p]=i.value.trim();});
  if(!Object.keys(body).length){UI.err.keys='Paste a key first.';render(true);return;}
  await act('/api/cloud_keys',body,'keys');toast('Keys saved.');},
 async 'ck-clear'(el){await act('/api/cloud_keys',{clear:[el.dataset.p]},'keys');}
};
function setTheme(t){const r=document.documentElement;if(t==='auto')r.removeAttribute('data-theme');else r.setAttribute('data-theme',t);lsSet('stt_theme',t==='auto'?null:t);renderFrame();layersHTML();if(UI.route==='settings')render(true);}
function closeLayer(){if(!$('#pop').hidden){hidePop();return;}if(UI.keys)UI.keys=false;else if(UI.palette)UI.palette=false;else if(UI.menu)UI.menu=false;layersHTML();}

/* -------------------------------------------------------------- events ---- */
document.addEventListener('click',e=>{
 const el=e.target.closest('[data-act]');
 const pop=$('#pop');
 if(!pop.hidden&&!e.target.closest('#pop')&&!(el&&['row-menu','new','spk-menu','bulk-form'].includes(el.dataset.act)))hidePop();
 if(e.target.matches('input[type=checkbox]'))return;
 if(el&&el.disabled)return;
 if(e.target.closest('a[href^="#"]')&&(UI.menu||UI.palette)){UI.menu=false;UI.palette=false;layersHTML();}
 if(!el)return;
 if(el.tagName==='A')e.preventDefault();
 const fn=A[el.dataset.act];if(fn)fn(el,e);
});
document.addEventListener('change',e=>{
 const t=e.target;
 if(t.dataset.sel){if(t.checked)UI.sel.add(t.dataset.sel);else UI.sel.delete(t.dataset.sel);const row=t.closest('.row');if(row)row.classList.toggle('selected',t.checked);$('#view').classList.toggle('has-sel',UI.sel.size>0);UI.confirm=null;renderBulk();return;}
 if(t.dataset.opt){lsSet(t.dataset.opt,t.checked?'1':null);return;}
 if(t.dataset.move){const to=t.value;if(to)act('/api/reassign_sample',{name:t.dataset.move,index:+t.dataset.i,to},'spk:'+t.dataset.move);return;}
 if(t.id==='rec-sort'){UI.sort=t.value;renderRecList();return;}
 if(t.id==='hist-f'){UI.histF=t.value;render(true);return;}
 if(t.id==='set-master')act(t.checked?'/api/resume':'/api/pause',{},'auto');
 else if(t.id==='set-watch')act('/api/automation',{watch:t.checked},'auto');
 else if(t.id==='set-nightly')act('/api/automation',{nightly:t.checked},'auto');
 else if(t.id==='set-time'){const [h,m]=(t.value||'').split(':').map(Number);if(!isNaN(h)&&!isNaN(m))act('/api/schedule',{hour:h,minute:m},'auto');}
 else if(t.id==='set-punct')act('/api/punctuate',{on:t.checked});
 else if(t.id==='set-model')act('/api/model',{model:t.value},'model');
 else if(t.id==='set-llm')act('/api/llm_backend',{backend:t.value},'llm');
 else if(t.id==='set-mic')act('/api/mic_speaker',{name:t.value});
 else if(t.id==='set-theme')setTheme(t.value);
});
document.addEventListener('input',e=>{
 const t=e.target;
 if(t.id==='rec-q'){UI.q=t.value;renderRecList();}
 else if(t.id==='pal-in'){UI.pq=t.value;$('#pal-res').innerHTML=palResults();palSearch();}
 else if(t.id==='hist-q'){UI.histQ=t.value;}
});
document.addEventListener('focusout',e=>{if(e.target.id==='hist-q')setTimeout(()=>render(true),0);});
let gPending=0;
document.addEventListener('keydown',e=>{
 const t=e.target,typing=t.matches&&t.matches('input,textarea,select');
 if(e.key==='Escape'&&!$('#pop').hidden){hidePop();return;}
 if(VS&&vsKey(e))return;                 // the voice sheet owns every key while open
 if(e.key==='Escape'){
  if(UI.keys||UI.palette||UI.menu){closeLayer();return;}
  if(UI.naming){UI.naming=null;render(true);return;}
  if(UI.route==='meeting'&&mKey(e))return;
  if(typing){t.blur();return;}
  if(UI.sel.size){UI.sel.clear();render(true);}
  return;
 }
 if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();A.palette();return;}
 // an open popover owns the plain keys, except inside its own form
 if(!$('#pop').hidden&&!e.metaKey&&!e.ctrlKey&&!(t.closest&&t.closest('#pop')))return;
 if(!UI.palette&&!UI.menu&&!UI.keys&&UI.route==='meeting'&&mKey(e))return;
 if(t.id==='pal-in'&&(e.key==='ArrowDown'||e.key==='ArrowUp')){const hs=[...document.querySelectorAll('#pal-res .hit')];if(!hs.length)return;e.preventDefault();
  let i=hs.findIndex(h=>h.classList.contains('sel'));hs.forEach(h=>h.classList.remove('sel'));i=e.key==='ArrowDown'?Math.min(hs.length-1,i+1):Math.max(0,i-1);hs[i].classList.add('sel');hs[i].scrollIntoView({block:'nearest'});return;}
 // close the palette first: a hit in the open meeting may not change the
 // hash (no hashchange), or may take route()'s same-meeting early return
 if(t.id==='pal-in'&&e.key==='Enter'){const h=document.querySelector('#pal-res .hit.sel')||document.querySelector('#pal-res .hit');if(!h)return;
  const href=h.getAttribute('href');UI.palette=false;layersHTML();if(href===location.hash)route();else location.href=href;return;}
 if(t.id==='rn-in'&&e.key==='Enter'){e.preventDefault();A['rn-save']();return;}
 if(t.id==='pf-in'&&e.key==='Enter'){e.preventDefault();A['pf-save']();return;}
 if(t.id&&t.id.startsWith('nt-')&&e.key==='Enter'){e.preventDefault();A.keep({dataset:{base:t.dataset.base}});return;}
 if(typing||e.metaKey||e.ctrlKey||e.altKey)return;
 if(UI.palette||UI.menu)return;
 if(UI.keys){if(e.key==='?')closeLayer();return;}
 if(e.key==='?'){UI.keys=true;layersHTML();return;}
 if(e.key==='/'){e.preventDefault();A.palette();return;}
 if(gPending&&Date.now()-gPending<1200){gPending=0;const to={h:'home',r:'recordings',g:'groups',q:'ask',a:'activity',s:'speakers',e:'archive',',':'settings'}[e.key];if(to){go(to);return;}}
 if(e.key==='g'){gPending=Date.now();return;}
 if(e.key==='j'||e.key==='k'){const rows=[...document.querySelectorAll('#view [data-row]')];if(!rows.length)return;e.preventDefault();
  const i=rows.indexOf(t.closest?t.closest('[data-row]'):null);const n=e.key==='j'?Math.min(rows.length-1,i+1):Math.max(0,i-1);rows[n<0?0:n].focus();return;}
 const row=t.closest&&t.closest('[data-row]');
 if(row&&e.key==='Enter'&&row.dataset.base&&t===row){location.hash=meetingHref(row.dataset.base).slice(1);return;}
 if(row&&e.key==='x'&&row.dataset.base){const c=row.querySelector('input[type=checkbox]');if(c){c.checked=!c.checked;c.dispatchEvent(new Event('change',{bubbles:true}));}return;}
});

/* --------------------------------------------------------- drop upload ---- */
let dragN=0;
const hasFiles=e=>e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files');
addEventListener('dragenter',e=>{if(!hasFiles(e))return;e.preventDefault();dragN++;$('#drop').hidden=false;});
addEventListener('dragover',e=>{if(hasFiles(e))e.preventDefault();});
addEventListener('dragleave',()=>{if(--dragN<=0){dragN=0;$('#drop').hidden=true;}});
addEventListener('drop',async e=>{if(!hasFiles(e))return;e.preventDefault();dragN=0;$('#drop').hidden=true;
 const files=[...e.dataTransfer.files];let ok=0;const bad=[];
 for(const f of files){toast(`Uploading ${f.name}…`);
  try{const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',body:f});const d=await r.json().catch(()=>({}));if(r.ok&&d.ok)ok++;else bad.push(d.error||f.name);}
  catch(err){bad.push(f.name);}}
 toast(bad.length?`${ok} added. Not added: ${bad[0]}`:`${plural(ok,'file')} added to the queue.`);await refresh();render(true);});

/* ----------------------------------------------- poll, tab title, seen ---- */
// The tab title counts Needs you items that joined while the tab was hidden,
// as "(N) Meetings". A visible tab marks the look for the menu bar's "new
// since last look" count.
let HIDDEN_BASE=null;
function tabTitle(){const n=HIDDEN_BASE==null?0:Math.max(0,needsYou().length-HIDDEN_BASE);return (n?`(${n}) `:'')+pageTitle()+' · Meetings';}
document.addEventListener('visibilitychange',()=>{
 if(document.hidden){HIDDEN_BASE=needsYou().length;}
 else{HIDDEN_BASE=null;document.title=tabTitle();api('/api/seen',{}).catch(()=>{});refresh().then(()=>render());}
});
// One poll at a time. A caller that arrives while a poll is in flight gets a
// fresh poll that starts after it: a save that awaits refresh() must read
// state from after the save, never the stale answer of an older request.
let POLL=null,POLL_NEXT=null;
function refresh(){
 if(POLL){if(!POLL_NEXT)POLL_NEXT=POLL.then(()=>{POLL_NEXT=null;return refresh();});return POLL_NEXT;}
 POLL=(async()=>{try{const r=await fetch('/api/state');if(r.ok)S=await r.json();}catch(e){}
  finally{POLL=null;}document.title=tabTitle();})();
 return POLL;
}
async function tick(){if(document.hidden&&S){await refresh();return;}await refresh();render();}
addEventListener('hashchange',route);
(async()=>{await refresh();if(!S){$('#view').innerHTML='<div class="empty"><strong>The panel is not answering.</strong><span>Reload when the menu-bar app is running.</span></div>';}
 if(!document.hidden)api('/api/seen',{}).catch(()=>{});
 route();setInterval(tick,2000);
 // dev-only: tools/next_screens.py reads the page width back with ?probe=1
 if(new URLSearchParams(location.search).get('probe')==='1')setTimeout(()=>{document.body.dataset.probe=JSON.stringify({sw:document.documentElement.scrollWidth,iw:innerWidth,route:UI.route});},1500);})();
