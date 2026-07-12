/* ============================================================================
   New shell (?ui=new): the poll-and-render loop ONLY. Reads /api/state every
   2s and renders the status pill, the "needs you" tray, and the One Timeline
   (all seven read-only row states). It changes no server state.

   Interactions -- the Process popover, the per-row menu, tray verbs, category
   cycling, naming Accept, bulk selection -- are stubbed in the "Builder B
   seams" block at the bottom for the next builder to wire. Every stub is a safe
   no-op today, so the read-only shell never throws when a button is clicked.
   ============================================================================ */

const $=q=>document.querySelector(q);
async function api(p,body){const r=await fetch(p,body?{method:'POST',body:JSON.stringify(body)}:{});return r.json();}
function esc(s){return (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
// for a value embedded as a JS string literal inside an onclick="..." attribute
function escJs(s){return esc(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'");}

let S=null;

// ---- small formatters (mono data uses tabular-nums via CSS) ----
function clock(secs){secs=Math.max(0,Math.floor(secs||0));
  const h=Math.floor(secs/3600),m=Math.floor(secs%3600/60),s=secs%60;
  return (h?h+':'+String(m).padStart(2,'0'):String(m))+':'+String(s).padStart(2,'0');}
function fmtEta(sec){if(sec==null)return'';if(sec<90)return'1 min';
  if(sec<3600)return Math.round(sec/60)+' min';
  return Math.floor(sec/3600)+'h '+String(Math.round(sec%3600/60)).padStart(2,'0')+'m';}
const STAGE_NICE={downloading:'Downloading',converting:'Preparing',transcribing:'Transcribing',
  diarizing:'Speakers',verifying:'Verifying',writing:'Writing',summarizing:'Summary'};
function shortDate(iso){if(!iso)return'';
  return new Date(iso+'T12:00:00').toLocaleDateString([],{month:'short',day:'numeric'});}

// today / yesterday, computed once in LOCAL time for the date buckets
function _isoLocal(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')
  +'-'+String(d.getDate()).padStart(2,'0');}
const TODAY=_isoLocal(new Date());
const YESTERDAY=(()=>{const d=new Date();d.setDate(d.getDate()-1);return _isoLocal(d);})();
function dateBucket(iso){
  if(!iso)return'Undated';
  if(iso===TODAY)return'Today';
  if(iso===YESTERDAY)return'Yesterday';
  return new Date(iso+'T12:00:00').toLocaleDateString([],{month:'long',year:'numeric'});}
function nameBucket(title){const t=(title||'').trim();
  return /^[a-z]/i.test(t)?t[0].toUpperCase():'#';}

/* ============================ status pill ============================ *
 * The ONLY place pipeline state appears, one line, by priority:
 *   REC ticking  ->  transcribing N  ->  paused N waiting  ->  N waiting  ->  hidden
 */
function drawPill(s){
  const pill=$('#pill');
  const rec=s.recording;
  const waiting=(s.timeline||[]).filter(r=>r.state==='waiting').length;
  let cls='pill',html='',show=true;
  if(rec){
    cls='pill rec';
    const c=`<span id="pillClock">${clock(rec.elapsed_secs)}</span>`;
    html=rec.paused?`&#9208; REC ${c}`:`<span class="recdot"></span>REC ${c}`;
  }else if(s.running){
    cls='pill accent';
    const n=Object.keys(s.active||{}).length||1;
    const eta=s.overall_eta_sec!=null?` &middot; &#8776;${fmtEta(s.overall_eta_sec)}`:'';
    html=`transcribing ${n}${eta}`;
  }else if(s.paused){
    cls='pill amber';
    html=`&#9208; paused${waiting?` &middot; ${waiting} waiting`:''}`;
  }else if(waiting){
    cls='pill';
    html=`${waiting} waiting`;
  }else{show=false;}
  pill.hidden=!show;
  if(show){pill.className=cls;pill.innerHTML=html;}
}

// 1s local tick advances the recording clock between the 2s polls, in place
// (pill + the recording row), so neither jumps in 2s steps. Each poll then
// overwrites elapsed_secs with the server's paused-aware number.
setInterval(()=>{
  const rec=S&&S.recording;
  if(!rec||rec.paused)return;
  rec.elapsed_secs=(rec.elapsed_secs||0)+1;
  const pc=$('#pillClock');if(pc)pc.textContent=clock(rec.elapsed_secs);
  const rc=$('#recRowClock');if(rc)rc.textContent=clock(rec.elapsed_secs);
},1000);

/* ============================ tray ============================ *
 * Amber "needs you" band, ranked by the server. At most 4 rows; a 5th+
 * collapses into "and N more" (Builder B wires the expand).
 */
const TRAY_VERB={recorder_stall:'Fix',failed:'Retry',review:'Review',unknown_voice:'Name'};
function drawTray(s){
  const tray=$('#tray');
  const items=s.tray||[];
  if(!items.length){tray.hidden=true;tray.dataset.sig='';tray.innerHTML='';return;}
  // trayExpanded (set by trayExpand) folds into the signature so toggling it
  // rebuilds, and so a later poll keeps the tray expanded instead of collapsing it
  const sig=JSON.stringify(items.map(t=>[t.kind,t.title,t.detail,t.target,t.count]))+'|'+trayExpanded;
  if(!tray.hidden&&tray.dataset.sig===sig)return;   // unchanged: don't rebuild
  tray.dataset.sig=sig;tray.hidden=false;
  const CAP=4,shown=trayExpanded?items:items.slice(0,CAP),more=trayExpanded?0:items.length-CAP;
  tray.innerHTML=`<div class="trayhdr">Needs you &middot; ${items.length}</div>`
    +shown.map(t=>`<div class="trayrow">
      <span class="tw-title">${esc(t.title)}</span>
      <span class="tw-detail">${esc(t.detail)}</span>
      <button class="btn mini tw-verb" type="button"
        onclick="trayAct('${escJs(t.kind)}','${escJs(t.target)}')">${TRAY_VERB[t.kind]||'Open'}</button>
    </div>`).join('')
    +(more>0?`<button class="traymore" type="button" onclick="trayExpand()">and ${more} more&#8230;</button>`:'');
}

/* ============================ timeline ============================ *
 * One row per meeting/file. The server already sorts it (recording + running
 * pinned to the top, then newest first); by-date grouping preserves that order,
 * by-name re-sorts alphabetically. Read-only: buttons render but are stubs.
 */
function catDot(row){
  const c=row.category||'none';
  const t=c==='work'?'Work':c==='personal'?'Personal':'Untagged';
  return `<button class="cat ${c}" type="button" title="${t} (click to change tag)"
    onclick="cycleCat(event,'${escJs(row.id)}')"></button>`;
}
// waiting/held rows also carry a checkbox so a run of files can be picked for
// "Process selected"; only ready rows carry a category dot (queue files have no
// category yet). Selected state is REAPPLIED after render (applySel), so gutter
// itself stays a pure function of the row.
function gutter(row){
  const selectable=row.state==='ready'||row.state==='waiting'||row.state==='held';
  const chk=selectable
    ? `<input class="chk" type="checkbox" aria-label="Select for bulk actions"
        onclick="toggleSel('${escJs(row.id)}',this.checked)">`
    : `<span class="chk spacer" aria-hidden="true"></span>`;
  const dot=row.state==='ready'?catDot(row)
    :`<span class="cat spacer" style="visibility:hidden"></span>`;
  return chk+dot;
}

// entities: &#9654;=play  &#10073;=heavy bar  &#10005;=x  &#8776;=approx  &#8943;=ellipsis
function slotActions(inner){return `<span class="ractions">${inner}</span>`;}

function bodyAndSlot(row){
  switch(row.state){

    case 'recording':{
      const state=row.paused
        ?`&#9208; paused <span id="recRowClock">${clock(row.elapsed_secs)}</span>`
        :`<span class="capdot"></span>capturing <span id="recRowClock">${clock(row.elapsed_secs)}</span>`;
      return `<div class="rbody"><div class="rtitle">Recording now&#8230;</div></div>
        <div class="rslot"><span class="rstate">${state}</span></div>`;
    }

    case 'waiting':{
      const bits=[];
      if(row.size_mb!=null)bits.push(row.size_mb+' MB');
      if(row.est_minutes)bits.push('&#8776;'+row.est_minutes+' min');
      return `<div class="rbody"><div class="rtitle">${esc(row.title)}</div></div>
        <div class="rslot">
          <span class="rstate yields">${bits.join(' &middot; ')}</span>
          ${slotActions(
            `<button class="iact play" type="button" onclick="rowListen('${escJs(row.id)}')" title="Listen">&#9654;</button>
             <button class="iact" type="button" onclick="rowHold('${escJs(row.id)}')" title="Hold">&#10073;&#10073;</button>
             <button class="iact" type="button" onclick="rowProcess('${escJs(row.id)}')">Process</button>
             <button class="iact" type="button" onclick="rowDelete('${escJs(row.id)}')" title="Delete">&#10005;</button>`)}
        </div>`;
    }

    case 'held':
      return `<div class="rbody"><div class="rtitle">${esc(row.title)}</div>
          <div class="rmeta">automatic runs skip this until you release it</div></div>
        <div class="rslot">
          <span class="rstate yields">&#10073;&#10073; held</span>
          ${slotActions(
            `<button class="iact play" type="button" onclick="rowListen('${escJs(row.id)}')" title="Listen">&#9654;</button>
             <button class="iact" type="button" onclick="rowRelease('${escJs(row.id)}')">Release</button>
             <button class="iact" type="button" onclick="rowProcess('${escJs(row.id)}')">Process</button>
             <button class="iact" type="button" onclick="rowDelete('${escJs(row.id)}')" title="Delete">&#10005;</button>`)}
        </div>`;

    case 'processing':{
      const pct=row.pct!=null?row.pct:null;
      const eta=row.eta!=null?` &middot; &#8776;${fmtEta(row.eta)} left`:'';
      return `<div class="rbody"><div class="rtitle">${esc(row.title)}</div></div>
        <div class="rslot"><span class="rstate">${esc(STAGE_NICE[row.stage]||row.stage||'working')}${pct!=null?' '+pct+'%':''}${eta}</span></div>
        ${pct!=null?`<span class="progress" style="width:${pct}%"></span>`:''}`;
    }

    case 'needs_name':
      // the row IS the form, prefilled. Builder B wires acceptMeeting().
      return `<div class="nameform">
        <input class="ntitle" type="text" value="${esc(row.suggested_title||row.title||'')}"
          placeholder="Name this meeting"
          onkeydown="if(event.key==='Enter')acceptMeeting('${escJs(row.id)}',this)">
        <input type="date" value="${esc(row.suggested_date||row.date||'')}" aria-label="Meeting date">
        ${row.has_audio?`<button class="iact play" type="button" onclick="rowListen('${escJs(row.id)}')" title="Listen">&#9654;</button>`:''}
        <button class="btn primary mini" type="button" onclick="acceptMeeting('${escJs(row.id)}')">Accept</button>
      </div>`;

    case 'ready':{
      const bits=[];
      if(row.date)bits.push(shortDate(row.date));
      if(row.minutes!=null)bits.push(row.minutes+' min');
      if(row.speakers&&row.speakers.length)bits.push(esc(row.speakers.join(', ')));
      let badge='';
      if(row.review_substantial)
        badge=`<span class="badge review yields" onclick="openReviewBadge('${escJs(row.id)}')" title="Step through the flagged segments">${row.review_substantial} to check</span>`;
      else if(row.review_minor)
        badge=`<span class="badge minor yields" onclick="openReviewBadge('${escJs(row.id)}')" title="Minor crumbs to skim">${row.review_minor} minor</span>`;
      return `<div class="rbody">
          <div class="rtitle">${esc(row.title)}</div>
          <div class="rmeta">${bits.join(' &middot; ')}</div>
          ${row.has_summary&&row.summary?`<div class="rsummary">${esc(row.summary)}</div>`:''}
        </div>
        <div class="rslot">
          ${badge}
          ${slotActions(
            `<button class="iact play" type="button" onclick="rowListen('${escJs(row.id)}')" title="Listen without opening">&#9654;</button>
             <button class="iact" type="button" onclick="openMeeting('${escJs(row.id)}')">Open</button>
             <button class="iact" type="button" onclick="rowMenu('${escJs(row.id)}',event)" title="Export, rename, redo">&#8943;</button>`)}
        </div>`;
    }

    case 'failed':
      return `<div class="rbody">
          <div class="rtitle">${esc(row.title)}</div>
          <div class="rmeta err">${esc(row.error||'failed')}</div>
          <div class="rmeta">original stays in the watched folder</div>
        </div>
        <div class="rslot">
          <span class="rstate yields">failed</span>
          ${slotActions(
            `<button class="iact" type="button" onclick="rowRetry('${escJs(row.id)}')">Retry</button>
             <button class="iact" type="button" onclick="rowDelete('${escJs(row.id)}')" title="Remove">&#10005;</button>`)}
        </div>`;

    default:
      return `<div class="rbody"><div class="rtitle">${esc(row.title||'')}</div></div>`;
  }
}
function rowHTML(row){
  return `<div class="row" data-state="${esc(row.state)}" data-id="${esc(row.id)}" tabindex="0">`
    +gutter(row)+bodyAndSlot(row)+`</div>`;
}

// fields that decide whether a rebuild is needed -- elapsed_secs is deliberately
// excluded (the 1s ticker owns it) so a recording never thrashes the list
function sigOf(r){return [r.id,r.state,r.title,r.date,r.pct,r.stage,r.eta,r.category,
  r.review_substantial,r.review_minor,r.has_summary,r.summary,r.size_mb,r.est_minutes,
  r.held,r.error,r.suggested_title,r.suggested_date,r.paused,r.has_audio,r.minutes,
  (r.speakers||[]).join(',')];}

function drawTimeline(s){
  const tl=$('#timeline');
  const cat=$('#filter').value;
  const q=$('#search').value.trim().toLowerCase();
  const sort=$('#sort').value;

  const all=s.timeline||[];
  const rows=all.filter(r=>{
    if(cat&&r.state==='ready'&&r.category!==cat)return false;   // tag filter: ready rows only
    if(q){
      // match the visible name (a needs_name row shows its suggested title) plus speakers
      const hay=((r.title||'')+' '+(r.suggested_title||'')+' '+((r.speakers||[]).join(' '))).toLowerCase();
      if(!hay.includes(q))return false;
    }
    return true;
  });

  // empty states (serif, centered), rendered inside the timeline
  if(all.length===0){
    tl.innerHTML=`<p class="empty">Record a meeting from the menu bar.</p>`;
    $('#rail').hidden=true;tl.dataset.sig='EMPTY';return;
  }
  if(rows.length===0){
    tl.innerHTML=`<p class="empty">No matches.</p>`;
    $('#rail').hidden=true;tl.dataset.sig='NOMATCH:'+cat+':'+q;return;
  }

  const ordered=sort==='name'
    ? rows.slice().sort((a,b)=>(a.title||'').toLowerCase().localeCompare((b.title||'').toLowerCase())
        ||(b.date||'').localeCompare(a.date||''))
    : rows;   // by-date keeps the server's pinned + newest-first order

  const groups=[];
  for(const r of ordered){
    const key=sort==='name'?nameBucket(r.title):dateBucket(r.date);
    if(!groups.length||groups[groups.length-1].key!==key)groups.push({key,rows:[]});
    groups[groups.length-1].rows.push(r);
  }

  const sig=JSON.stringify(ordered.map(sigOf))+'|'+sort+'|'+cat+'|'+q;
  if(tl.dataset.sig===sig){drawRail(groups,q.length>0,sort);return;}  // nothing changed
  // never wipe a half-typed name in a needs_name field; a later unchanged poll
  // (or a blur) lets the rebuild happen. Only INPUT/SELECT focus blocks it, so
  // a merely focused row still refreshes.
  const ae=document.activeElement;
  if(ae&&tl.contains(ae)&&/^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName))return;
  tl.dataset.sig=sig;
  tl.innerHTML=groups.map((g,i)=>
    `<div class="mgroup" id="grp-${i}">${esc(g.key)} &middot; ${g.rows.length}</div>`
    +g.rows.map(rowHTML).join('')).join('');
  drawRail(groups,q.length>0,sort);
}

/* jump rail: year / month, only at 3+ groups with no active search */
function drawRail(groups,hasSearch,sort){
  const rail=$('#rail');
  if(groups.length<3||hasSearch){rail.hidden=true;rail.innerHTML='';return;}
  rail.hidden=false;
  let lastYr='';
  rail.innerHTML=groups.map((g,i)=>{
    let head='',short;
    if(sort==='date'){
      const yr=(g.key.match(/\d{4}/)||[])[0]
        ||(g.rows[0]&&g.rows[0].date?g.rows[0].date.slice(0,4):'');
      if(yr&&yr!==lastYr){head=`<div class="railyr">${yr}</div>`;lastYr=yr;}
      short=g.key==='Today'?'TDY':g.key==='Yesterday'?'YST':g.key==='Undated'?'NA':g.key.slice(0,3);
    }else{short=g.key;}
    return head+`<button class="railbtn" type="button" onclick="railJump(${i})"
      title="Jump to ${esc(g.key)} (${g.rows.length})">${esc(short)}</button>`;
  }).join('');
}
function railJump(i){const el=document.getElementById('grp-'+i);
  if(el)el.scrollIntoView({behavior:'smooth',block:'start'});}

/* ============================ render loop ============================ */
function render(){if(!S)return;drawPill(S);drawTray(S);drawTimeline(S);afterRender();}
async function refresh(){try{S=await api('/api/state');render();}catch(e){}}

/* ============================ theme (reused convention) ============================ *
 * Same stt_theme localStorage key and auto/light/dark cycle as the old panel.
 */
const THEME_META={auto:["&#9680;","Theme: matching macOS. Click for light."],
  light:["&#9728;","Theme: light. Click for dark."],
  dark:["&#9790;","Theme: dark. Click to match macOS."]};
function themeNow(){const q=new URLSearchParams(location.search).get("theme");
  return q==="light"||q==="dark"?q:(localStorage.getItem("stt_theme")||"auto");}
function applyTheme(t){
  if(t==="light"||t==="dark"){localStorage.setItem("stt_theme",t);document.documentElement.dataset.theme=t;}
  else{localStorage.removeItem("stt_theme");delete document.documentElement.dataset.theme;}
  const b=$('#themeDot');if(b){b.innerHTML=THEME_META[t][0];b.title=THEME_META[t][1];}
}
function cycleTheme(){const order=["auto","light","dark"];
  applyTheme(order[(order.indexOf(themeNow())+1)%3]);}

/* ============================ boot / wiring ============================ */
{const ms=localStorage.getItem('stt_msort');if(ms)$('#sort').value=ms;}
{const mc=localStorage.getItem('stt_mcat');if(mc)$('#filter').value=mc;}
applyTheme(themeNow());
$('#sort').onchange=()=>{localStorage.setItem('stt_msort',$('#sort').value);render();};
$('#filter').onchange=()=>{localStorage.setItem('stt_mcat',$('#filter').value);render();};
$('#search').oninput=()=>{render();scheduleSearch();};
$('#themeDot').onclick=cycleTheme;
$('#gear').onclick=()=>{location.href='/?ui=old';};   // bridge until the settings drawer ships
$('#pill').onclick=toggleProcess;
$('#processBtn').onclick=toggleProcess;
refresh();
setInterval(refresh,2000);

/* ============================================================================
   Builder B: interactions. Wires every seam A stubbed, against the EXISTING
   endpoints only (no server change). The old panel accepts the bridge links used
   below until the meeting page ships:
     Open a meeting   ->  /?open=<base>
     Review a meeting ->  /?review=<base>
     Name an unknown  ->  /?who=<uid>
   Re-render safety: selection lives in a Set keyed by row id and is reapplied
   after every rebuild (applySel); the inline clip player is ONE detached element
   re-mounted under its row (mountClip); an open per-row menu is re-anchored or
   closed. A's focus guard is untouched. render() calls afterRender() last.
   ============================================================================ */

// a timeline row by id; meeting rows (id === base) can borrow extra fields the
// old page still gets in the same /api/state (e.g. the audio path a Redo needs)
function rowById(id){return (S&&S.timeline||[]).find(r=>r.id===id);}
function meetingByBase(base){return (S&&S.meetings||[]).find(m=>m.base===base);}
const SELECTABLE=new Set(['ready','waiting','held']);

/* ------------------------------------------------------------- popovers ---- *
 * One floating menu at a time (Process, per-row menu, the small confirms nested
 * in it). Closes on outside mousedown and Escape. */
let _popClose=null;
function closePop(){if(_popClose){_popClose();_popClose=null;}}
function _posPop(el,anchor){
  const r=anchor.getBoundingClientRect(),w=el.offsetWidth||260;
  let left=window.scrollX+r.right-w;
  const minL=window.scrollX+8,maxL=window.scrollX+document.documentElement.clientWidth-w-8;
  if(left<minL)left=minL; if(left>maxL)left=maxL;
  el.style.left=Math.max(8,left)+'px';
  el.style.top=(window.scrollY+r.bottom+6)+'px';
}
function openPop(el,anchor,fill,ignoreSel){
  closePop();
  fill();el.hidden=false;_posPop(el,anchor);
  const onDoc=e=>{
    if(el.contains(e.target))return;
    if(anchor&&anchor.contains&&anchor.contains(e.target))return;
    if(ignoreSel&&e.target.closest&&e.target.closest(ignoreSel))return;
    closePop();
  };
  const onKey=e=>{if(e.key==='Escape'){e.preventDefault();closePop();}};
  setTimeout(()=>document.addEventListener('mousedown',onDoc),0); // skip THIS click
  document.addEventListener('keydown',onKey);
  el.dataset.open='1';
  _popClose=()=>{
    document.removeEventListener('mousedown',onDoc);
    document.removeEventListener('keydown',onKey);
    el.hidden=true;el.dataset.open='';el.innerHTML='';
    if(el.id==='processPop')$('#processBtn').setAttribute('aria-expanded','false');
    if(el.id==='rowmenu')el.dataset.rowid='';
  };
}

/* --------------------------------------------- run options (persisted) ----- *
 * The old page never stored these four (they reset each load); the new shell
 * keeps them under stt_run_* so they survive reloads. Same option meanings. */
const RUNOPTS=[
  {ls:'stt_run_par2',   label:'two at a time',
   note:'Uses about 10 CPU cores for roughly 1.7x throughput.'},
  {ls:'stt_run_strict', label:'strict',
   note:'Never guesses an uncertain speaker; flags it for review instead. For confidential conversations.'},
  {ls:'stt_run_verify', label:'verify',
   note:'A second engine listens too; the spots where they disagree get flagged with both versions.'},
  {ls:'stt_run_onetime',label:'one-time speakers',
   note:'This meeting&#8217;s unnamed voices are not added to the Speakers list. For focus groups.'}];
function optOn(ls){return localStorage.getItem(ls)==='1';}
function optSet(ls,v){localStorage.setItem(ls,v?'1':'0');}
function runOpts(){const g=ls=>localStorage.getItem(ls)==='1';
  return {parallel:g('stt_run_par2')?2:1,strict:g('stt_run_strict'),
    verify:g('stt_run_verify'),onetime:g('stt_run_onetime')};}

/* ------------------------------------------------ Process popover ---------- */
function toggleProcess(){
  const pop=$('#processPop');
  if(pop.dataset.open){closePop();return;}
  openPop(pop,$('#processBtn'),()=>fillProcessPop(pop),'#pill,#processBtn');
  $('#processBtn').setAttribute('aria-expanded','true');
}
function fillProcessPop(pop){
  const s=S||{};
  const waiting=(s.timeline||[]).filter(r=>r.state==='waiting').length;
  const qsel=[...SEL].map(rowById).filter(r=>r&&(r.state==='waiting'||r.state==='held')).length;
  const running=!!s.running;
  let h='';
  h+=`<button class="ppitem" type="button" ${(!waiting||running)?'disabled':''} onclick="ppRunAll()">Process all new${waiting?` <span class="ppc">${waiting}</span>`:''}</button>`;
  h+=`<button class="ppitem" type="button" ${(!qsel||running)?'disabled':''} onclick="ppRunSel()">Process selected${qsel?` <span class="ppc">${qsel}</span>`:''}</button>`;
  h+=`<button class="ppitem" type="button" onclick="ppOther()">Other files&#8230;</button>`;
  if(running)h+=`<button class="ppitem danger" type="button" onclick="ppStop(this)">Stop processing</button>`;
  h+=`<div class="ppsep"></div>`;
  h+=`<button class="ppitem" type="button" onclick="ppPause(this)">${s.paused?'Resume automatic runs':'Pause automatic runs'}</button>`;
  h+=`<div class="ppsep"></div><div class="pphdr">Run options</div>`;
  h+=RUNOPTS.map(o=>`<label class="ppopt"><input type="checkbox" ${optOn(o.ls)?'checked':''} onchange="optSet('${o.ls}',this.checked)"><span><span class="ppopt-l">${o.label}</span><span class="ppopt-n">${o.note}</span></span></label>`).join('');
  pop.innerHTML=h;
}
function ppRunAll(){closePop();api('/api/run',runOpts()).then(refresh);}
function ppRunSel(){
  const files=[...SEL].map(rowById).filter(r=>r&&(r.state==='waiting'||r.state==='held')).map(r=>r.source_file);
  if(!files.length)return;
  closePop();api('/api/run',{files,...runOpts()}).then(()=>{SEL.clear();refresh();});
}
async function ppOther(){
  closePop();
  const r=await api('/api/pick_files',{});
  if(r.cancelled||!(r.paths&&r.paths.length))return;
  await api('/api/run',{paths:r.paths,...runOpts()});
  refresh();
}
async function ppStop(btn){         // blocks up to ~8s server-side, like the old page
  btn.disabled=true;btn.textContent='Stopping…';
  await api('/api/stop',{});
  closePop();refresh();
}
function ppPause(btn){
  btn.disabled=true;
  api(S.paused?'/api/resume':'/api/pause',{}).then(()=>{closePop();refresh();});
}

/* ------------------------------------------------------------- tray -------- */
let trayExpanded=false;
function trayExpand(){trayExpanded=true;drawTray(S);}
async function trayAct(kind,target){
  const ev=window.event;
  if(kind==='recorder_stall'){
    const btn=ev&&ev.target&&ev.target.closest?ev.target.closest('.tw-verb'):null;
    if(btn){btn.disabled=true;btn.textContent='Resetting…';}
    const r=await api('/api/fix_recorder_permissions',{});
    if(!r.ok&&btn){
      btn.disabled=false;btn.textContent='Retry';
      const row=btn.closest('.trayrow'),d=row&&row.querySelector('.tw-detail');
      if(d)d.textContent=r.error||'permission reset failed';
    }
    refresh();return;
  }
  if(kind==='failed'){
    const r=rowById(target);
    if(r&&r.source_file)api('/api/run',{files:[r.source_file],...runOpts()}).then(refresh);
    return;
  }
  if(kind==='review'){location.href='/?review='+encodeURIComponent(target);return;}
  if(kind==='unknown_voice'){location.href='/?who='+encodeURIComponent(target);return;}
  location.href='/?open='+encodeURIComponent(target);
}

/* -------------------------------------------------- inline clip player ----- *
 * Ported from the old page: the player is ONE detached element keyed by row id,
 * remounted under its row after every rebuild so a 2s poll never kills playback
 * mid-clip. The server streams byte ranges, so scrubbing seeks rather than
 * re-downloads. */
let clipAudio=null,clipKey=null,clipBox=null;
function _cfmt(t){t=Math.max(0,Math.floor(t||0));const m=Math.floor(t/60),s=t%60;return m+':'+String(s).padStart(2,'0');}
function _clipBoxEl(){
  if(clipBox)return clipBox;
  clipBox=document.createElement('div');clipBox.className='clipplayer';
  clipBox.innerHTML=`<button id="clipPP" class="iact play" type="button" title="Play / pause" onclick="clipToggle()">&#10073;&#10073;</button>
    <span class="clip-t mono" id="clipCur">0:00</span>
    <input type="range" id="clipSeek" min="0" max="0" step="0.1" value="0" aria-label="Scrub the recording"
      onpointerdown="this.dataset.dragging=1" onpointerup="this.removeAttribute('data-dragging')" oninput="clipSeek(this.value)">
    <span class="clip-t mono" id="clipDur">0:00</span>
    <button class="iact" type="button" title="Stop and close" onclick="stopClip()">&#10005;</button>`;
  return clipBox;
}
function _clipTick(){
  if(!clipAudio)return;
  const bar=$('#clipSeek'),cur=$('#clipCur'),dur=$('#clipDur');
  if(!bar)return;
  const d=isFinite(clipAudio.duration)?clipAudio.duration:0;
  if(!bar.dataset.dragging){bar.max=d||0;bar.value=clipAudio.currentTime||0;}
  if(cur)cur.textContent=_cfmt(clipAudio.currentTime);
  if(dur)dur.textContent=d?_cfmt(d):'0:00';
}
function _clipSync(){const pp=$('#clipPP');if(pp)pp.innerHTML=(clipAudio&&!clipAudio.paused)?'&#10073;&#10073;':'&#9654;';}
function clipToggle(){if(!clipAudio)return;if(clipAudio.paused)clipAudio.play().catch(()=>{});else clipAudio.pause();_clipSync();}
function clipSeek(v){if(clipAudio)clipAudio.currentTime=parseFloat(v)||0;_clipTick();}
function stopClip(){
  if(clipAudio){clipAudio.pause();clipAudio.src='';}
  clipAudio=null;clipKey=null;
  if(clipBox&&clipBox.parentNode)clipBox.parentNode.removeChild(clipBox);
}
function mountClip(){
  if(!clipKey)return;
  const row=document.querySelector('.row[data-id="'+CSS.escape(clipKey)+'"]');
  if(!row)return;                                   // its row is gone / scrolled out
  const box=_clipBoxEl();
  if(row.nextElementSibling!==box)row.insertAdjacentElement('afterend',box);
  _clipTick();_clipSync();
}
function rowListen(id){
  if(clipKey===id){stopClip();return;}              // toggle the same row closed
  stopClip();
  document.querySelectorAll('audio').forEach(a=>a.pause());
  const r=rowById(id);if(!r)return;
  const url=(r.state==='waiting'||r.state==='held')
    ?'/api/queue_audio?name='+encodeURIComponent(r.source_file||'')
    :'/api/audio?base='+encodeURIComponent(id);
  clipKey=id;clipAudio=new Audio(url);
  clipAudio.ontimeupdate=_clipTick;clipAudio.onloadedmetadata=_clipTick;
  clipAudio.onplay=clipAudio.onpause=_clipSync;
  clipAudio.onended=()=>{_clipSync();_clipTick();};
  clipAudio.onerror=()=>{if(clipKey===id)stopClip();};
  mountClip();clipAudio.play().catch(()=>{});
}

/* --------------------------------------------------------- row actions ----- */
function rowHold(id){                    // optimistic: flip to held, then persist
  const r=rowById(id);if(!r||!r.source_file)return;
  r.state='held';r.held=true;render();
  api('/api/queue_hold',{name:r.source_file}).then(refresh);
}
function rowRelease(id){
  const r=rowById(id);if(!r||!r.source_file)return;
  r.state='waiting';r.held=false;render();
  api('/api/queue_hold',{name:r.source_file}).then(refresh);
}
function rowProcess(id){
  const r=rowById(id);if(!r||!r.source_file)return;
  api('/api/run',{files:[r.source_file],...runOpts()}).then(refresh);
}
async function rowDelete(id){
  const r=rowById(id);if(!r||!r.source_file)return;
  if(!confirm(`Delete "${r.source_file}"?\n\nThe audio file is removed and never becomes a meeting. This cannot be undone.`))return;
  if(clipKey===id)stopClip();
  const res=await api('/api/queue_delete',{name:r.source_file,confirm:true});
  if(!res.ok){alert(res.error||'failed');return;}
  refresh();
}
function rowRetry(id){                    // re-run a failed source still in the folder
  const r=rowById(id);if(!r||!r.source_file)return;
  api('/api/run',{files:[r.source_file],...runOpts()}).then(refresh);
}
function openMeeting(id){location.href='/?open='+encodeURIComponent(id);}
function openReviewBadge(id){location.href='/?review='+encodeURIComponent(id);}
function cycleCat(ev,id){                 // untagged -> work -> personal -> untagged
  if(ev)ev.stopPropagation();
  const r=rowById(id);if(!r)return;
  const cur=(r.category&&r.category!=='none')?r.category:'';
  const next={'':'work',work:'personal',personal:''}[cur];
  r.category=next;render();               // optimistic; the dot flips before the poll
  api('/api/set_category',{base:id,category:next}).then(refresh);
}
async function acceptMeeting(id){         // name/date from the row's own form
  const rowEl=document.querySelector('.row[data-id="'+CSS.escape(id)+'"]');
  if(!rowEl)return;
  const form=rowEl.querySelector('.nameform')||rowEl;
  const titleI=form.querySelector('.ntitle'),dateI=form.querySelector('input[type=date]');
  const btn=form.querySelector('.btn.primary');
  const name=(titleI&&titleI.value.trim())||'',date=(dateI&&dateI.value)||'';
  const old=form.querySelector('.nameerr');if(old)old.remove();
  if(btn){btn.disabled=true;btn.textContent='Saving…';}
  const r=await api('/api/accept_meeting',{base:id,name,date});
  if(!r.ok){                               // failure shows inline on the row, no banner
    const live=document.querySelector('.row[data-id="'+CSS.escape(id)+'"] .nameform')||form;
    const b2=live.querySelector('.btn.primary');if(b2){b2.disabled=false;b2.textContent='Accept';}
    let err=live.querySelector('.nameerr');
    if(!err){err=document.createElement('div');err.className='nameerr';live.appendChild(err);}
    err.textContent=r.error||'Could not accept this meeting.';
    return;
  }
  refresh();
}

/* ----------------------------------------------------------- row menu ------ */
function rowMenu(id,ev){
  if(ev)ev.stopPropagation();
  const anchor=(ev&&ev.currentTarget)||document.querySelector('.row[data-id="'+CSS.escape(id)+'"] .rslot .iact:last-child');
  const pop=$('#rowmenu');pop.dataset.rowid=id;
  openPop(pop,anchor,()=>fillRowMenu(pop,id));
}
function fillRowMenu(pop,id){
  const m=meetingByBase(id)||{};
  pop.innerHTML=`
    <button class="ppitem" type="button" onclick="rmExport('${escJs(id)}','docx',this)">Export Word</button>
    <button class="ppitem" type="button" onclick="rmExport('${escJs(id)}','pdf',this)">Export PDF</button>
    <button class="ppitem" type="button" onclick="rmCopy('${escJs(id)}',this)">Copy transcript</button>
    <button class="ppitem" type="button" onclick="rmReveal('${escJs(id)}')">Show files</button>
    <div class="ppsep"></div>
    <button class="ppitem" type="button" onclick="rmRename('${escJs(id)}')">Rename&#8230;</button>
    ${m.audio?`<button class="ppitem" type="button" onclick="rmRedo('${escJs(id)}')">Redo&#8230;</button>`:''}
    <button class="ppitem" type="button" onclick="rmArchive('${escJs(id)}')">Archive</button>
    <div class="ppsep"></div>
    <button class="ppitem danger" type="button" onclick="rmDelete('${escJs(id)}')">Delete&#8230;</button>`;
}
async function rmExport(id,fmt,btn){
  btn.disabled=true;const t=btn.textContent;btn.textContent='Exporting…';
  const r=await api('/api/export',{base:id,fmt});
  btn.disabled=false;btn.textContent=r.ok?'Done ✓':t;
  if(r.error)btn.textContent=t;
  if(r.ok)setTimeout(closePop,650);
}
async function rmCopy(id,btn){
  try{
    const txt=await fetch('/api/txt?base='+encodeURIComponent(id)).then(r=>r.text());
    await navigator.clipboard.writeText(txt);
    btn.textContent='Copied ✓';
  }catch(e){btn.textContent='Copy failed';}
}
function rmReveal(id){api('/api/export',{base:id,fmt:'reveal'});closePop();}
function rmArchive(id){api('/api/archive_meeting',{base:id}).then(()=>{closePop();refresh();});}
function rmRename(id){                    // inline: the row title becomes an input
  closePop();
  const rowEl=document.querySelector('.row[data-id="'+CSS.escape(id)+'"]');
  const titleEl=rowEl&&rowEl.querySelector('.rtitle');
  if(!titleEl)return;
  const cur=(meetingByBase(id)||{}).title||titleEl.textContent||'';
  titleEl.innerHTML=`<input class="renameinput" type="text" value="${esc(cur)}" aria-label="Rename meeting">`;
  const inp=titleEl.querySelector('input');inp.focus();inp.select();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;
    const nm=inp.value.trim();
    if(save&&nm&&nm!==cur){
      const r=await api('/api/rename',{base:id,new:nm});
      if(!r.ok)alert(r.error||'Rename failed');
    }
    refresh();
  };
  inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();finish(true);}else if(e.key==='Escape'){e.preventDefault();finish(false);}};
  inp.onblur=()=>finish(true);
}
async function rmRedo(id){                // mirrors the old redo dialog, as a confirm popover
  const m=meetingByBase(id)||{};
  const pop=$('#rowmenu');
  const ed=await api('/api/edits?base='+encodeURIComponent(id));
  if(pop.hidden)return;                    // menu was dismissed while fetching
  pop.innerHTML=`<div class="ppconfirm">
    <div class="ppctitle">Reprocess &#8220;${esc(m.title||id)}&#8221;?</div>
    <div class="ppcnote">Re-runs transcription and speaker detection from the stored audio. The existing transcript is replaced.</div>
    ${ed.n?`<div class="ppcnote warn">This meeting has ${ed.n} manual edit${ed.n>1?'s':''}. A redo rebuilds from the audio, so they no longer apply; they are archived next to the transcript, not deleted.</div>`:''}
    <label class="ppopt"><input type="checkbox" id="rdStrict"><span><span class="ppopt-l">strict</span><span class="ppopt-n">Never guess an uncertain speaker.</span></span></label>
    <label class="ppopt"><input type="checkbox" id="rdVerify"><span><span class="ppopt-l">verify</span><span class="ppopt-n">A second engine listens too; disagreements get flagged.</span></span></label>
    <label class="ppopt"><input type="checkbox" id="rdOne"><span><span class="ppopt-l">one-time speakers</span><span class="ppopt-n">Do not add this meeting&#8217;s voices to the Speakers list.</span></span></label>
    <div class="ppcrow">
      <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
      <button class="btn primary mini" type="button" onclick="rmRedoGo('${escJs(id)}','${escJs(m.audio||'')}')">Reprocess</button>
    </div></div>`;
}
function rmRedoGo(id,audio){
  if(!audio)return;
  api('/api/run',{paths:[audio],force:true,strict:$('#rdStrict').checked,
    verify:$('#rdVerify').checked,onetime:$('#rdOne').checked}).then(()=>{closePop();refresh();});
}
function rmDelete(id){                    // two-step confirm: Archive instead / Delete forever
  const m=meetingByBase(id)||{};
  const pop=$('#rowmenu');
  pop.innerHTML=`<div class="ppconfirm">
    <div class="ppctitle">Delete &#8220;${esc(m.title||id)}&#8221;?</div>
    <div class="ppcnote">This permanently removes the transcript, the stored audio, and every cache. It cannot be undone.</div>
    <div class="ppcnote">If you might want it back, Archive instead: it moves the meeting out of the main view but keeps everything restorable.</div>
    <div class="ppcrow">
      <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
      <button class="btn mini" type="button" onclick="rmArchive('${escJs(id)}')">Archive instead</button>
      <button class="btn danger mini" type="button" onclick="rmDeleteGo('${escJs(id)}')">Delete forever</button>
    </div></div>`;
}
async function rmDeleteGo(id){
  const r=await api('/api/delete_meeting',{base:id,confirm:true});
  closePop();
  if(!r.ok){alert(r.error||'failed');return;}
  refresh();
}

/* --------------------------------------------------- bulk selection -------- *
 * One Set keyed by row id, reapplied after render. The bulk bar (meeting ops)
 * shows for selected READY rows; "Process selected" (above) consumes the
 * selected waiting/held rows. */
let SEL=new Set(),selAnchor=null;
function toggleSel(id,on){
  const ev=window.event;
  if(ev&&ev.shiftKey&&selAnchor&&selAnchor!==id){   // shift-click range in DOM order
    const ids=[...document.querySelectorAll('#timeline .row .chk')].map(c=>c.closest('.row').dataset.id);
    const a=ids.indexOf(selAnchor),b=ids.indexOf(id);
    if(a>=0&&b>=0)for(let i=Math.min(a,b);i<=Math.max(a,b);i++)SEL.add(ids[i]);
  }else{
    on?SEL.add(id):SEL.delete(id);
  }
  selAnchor=id;applySel();
}
function applySel(){                       // reapply after any rebuild; prune stale ids
  const live=new Set((S&&S.timeline||[]).filter(r=>SELECTABLE.has(r.state)).map(r=>r.id));
  [...SEL].forEach(id=>{if(!live.has(id))SEL.delete(id);});
  document.querySelectorAll('#timeline .row .chk').forEach(c=>{
    c.checked=SEL.has(c.closest('.row').dataset.id);});
  document.body.classList.toggle('has-sel',SEL.size>0);
  drawBulkBar();
}
function _selReady(){return [...SEL].filter(id=>{const r=rowById(id);return r&&r.state==='ready';});}
function drawBulkBar(){
  const bar=$('#bulkbar'),bases=_selReady();
  if(!bases.length){bar.hidden=true;bar.innerHTML='';return;}
  bar.hidden=false;
  bar.innerHTML=`<span class="bcount">${bases.length} selected</span>
    <button class="btn mini" type="button" onclick="bulk('category','work')">Work</button>
    <button class="btn mini" type="button" onclick="bulk('category','personal')">Personal</button>
    <button class="btn mini" type="button" onclick="bulk('category','')">Clear tag</button>
    <button class="btn mini" type="button" onclick="bulkRename()">Rename&#8230;</button>
    <button class="btn mini" type="button" onclick="bulkDate()">Set date&#8230;</button>
    <button class="btn mini" type="button" onclick="bulk('archive')">Archive</button>
    <button class="btn mini" type="button" onclick="bulkDropAudio()">Delete audio&#8230;</button>
    <button class="btn danger mini" type="button" onclick="bulkDelete()">Delete&#8230;</button>
    <span class="grow"></span>
    <button class="btn mini" type="button" onclick="selAllShown()">Select all shown</button>
    <button class="btn mini" type="button" title="Clear selection" onclick="selClear()">&#10005;</button>`;
}
async function bulk(action,value,extra){
  const bases=_selReady();
  if(!bases.length)return;
  const r=await api('/api/bulk',{bases,action,value,...(extra||{})});
  const fails=(r.results||[]).filter(x=>!x.ok);
  if(fails.length)alert(`${fails.length} of ${bases.length} could not be done:\n\n`
    +fails.slice(0,8).map(f=>'- '+f.base+': '+(f.error||'failed')).join('\n'));
  else if(r.freed_mb)alert(`Done. Freed ${r.freed_mb} MB of audio.`);
  SEL.clear();refresh();
}
function bulkRename(){
  const n=prompt(`One name for all ${_selReady().length} selected.\n\nEach keeps its own date in the filename, so recurring meetings stay separate folders.`);
  if(n&&n.trim())bulk('rename',n.trim());
}
function bulkDate(){
  const d=prompt(`Set the date for all ${_selReady().length} selected (YYYY-MM-DD).\n\nThe folder name is re-stamped to match.`);
  if(d&&d.trim())bulk('date',d.trim());
}
function bulkDropAudio(){
  if(!confirm(`Delete the stored AUDIO for ${_selReady().length} meeting(s)? The transcripts are kept.\n\nThis frees most of the space, but it cannot be undone.`))return;
  bulk('drop_audio',null,{confirm:true});
}
function bulkDelete(){
  if(!confirm(`Permanently delete ${_selReady().length} meeting(s), including transcript, audio, and caches?\n\nThis cannot be undone. Archive instead if you might want them back.`))return;
  bulk('delete',null,{confirm:true});
}
function selClear(){SEL.clear();applySel();}
function selAllShown(){
  const ids=[...document.querySelectorAll('#timeline .row[data-state="ready"] .chk')].map(c=>c.closest('.row').dataset.id);
  const all=ids.length&&ids.every(id=>SEL.has(id));
  ids.forEach(id=>all?SEL.delete(id):SEL.add(id));
  applySel();
}

/* --------------------------------------------------------------- search ---- *
 * Debounced full-text (>=3 chars) into #searchhits; a hit bridges to ?open= at
 * click time. The client-side title filter is A's, already wired on #search. */
let searchTimer=null;
function scheduleSearch(){
  clearTimeout(searchTimer);
  const box=$('#searchhits'),q=$('#search').value.trim();
  if(q.length<3){box.hidden=true;box.innerHTML='';return;}
  searchTimer=setTimeout(async()=>{
    const r=await api('/api/search?q='+encodeURIComponent(q));
    if($('#search').value.trim().toLowerCase()!==r.query)return;   // a stale response
    const rx=new RegExp(r.query.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
    const hl=s=>esc(s).replace(rx,m=>'<mark>'+m+'</mark>');
    box.hidden=false;
    if(!r.hits.length){box.innerHTML=`<div class="hitshdr">No transcript matches &#8220;${esc(r.query)}&#8221;.</div>`;return;}
    box.innerHTML=`<div class="hitshdr">Said in transcripts &middot; ${r.total} match${r.total>1?'es':''}</div>`
      +r.hits.map(h=>{const mm=Math.floor(h.start/60),ss=String(Math.floor(h.start%60)).padStart(2,'0');
        return `<div class="hit" onclick="location.href='/?open='+encodeURIComponent('${escJs(h.base)}')" title="Open the transcript">
          <span class="hit-t mono">${mm}:${ss}</span>
          <span class="hit-w">${esc(h.who)}</span>
          <span class="hit-x">${hl(h.snippet)} <span class="hit-b">${esc(h.base)}</span></span></div>`;}).join('');
  },250);
}

/* ---------------------------------------------------- post-render hook ----- *
 * Called last by render(). Keeps the clip player mounted, the selection in sync,
 * and an open per-row menu anchored to its (possibly rebuilt) row. */
function afterRender(){
  mountClip();
  applySel();
  const rm=$('#rowmenu');
  if(rm.dataset.open&&rm.dataset.rowid){
    const anchor=document.querySelector('.row[data-id="'+CSS.escape(rm.dataset.rowid)+'"] .rslot .iact:last-child');
    if(anchor)_posPop(rm,anchor);else closePop();
  }
}
