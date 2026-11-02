/* ============================================================================
   The panel shell: the poll-and-render loop ONLY. Reads /api/state every
   2s and renders the status pill and the One Timeline: its Now and To finish
   sections (which carry the server's ranked tray items), then the library.
   It changes no server state.

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

// A transient one-line notice for something that already happened elsewhere
// (a stale chip clicked after the voice was named). Never for an error the user
// must act on -- those stay inline next to their control. Text only, set with
// textContent: nothing here is ever parsed as HTML.
let TOAST_T=null;
function toast(msg){
  let el=document.getElementById('toast');
  if(!el){
    el=document.createElement('div');
    el.id='toast';el.className='toast';el.setAttribute('role','status');
    document.body.appendChild(el);
  }
  el.textContent=msg;el.hidden=false;
  if(TOAST_T)clearTimeout(TOAST_T);
  TOAST_T=setTimeout(()=>{const t=document.getElementById('toast');if(t)t.hidden=true;},4000);
}

// ---- small formatters (digits align via the body's tabular-nums) ----
function clock(secs){secs=Math.max(0,Math.floor(secs||0));
  const h=Math.floor(secs/3600),m=Math.floor(secs%3600/60),s=secs%60;
  return (h?h+':'+String(m).padStart(2,'0'):String(m))+':'+String(s).padStart(2,'0');}
function fmtEta(sec){if(sec==null)return'';if(sec<90)return'1 min';
  if(sec<3600)return Math.round(sec/60)+' min';
  return Math.floor(sec/3600)+'h '+String(Math.round(sec%3600/60)).padStart(2,'0')+'m';}
// a stage past its estimate: "running long · +12m" in place of a countdown,
// the minutes past the estimate, whole and growing (never "+0m"); past an
// hour "+1h 05m", the countdown's own form
function fmtOver(sec){
  const m=Math.max(1,Math.floor((sec||0)/60));
  return 'running long &middot; +'+(m<60?m+'m':Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m');
}
const STAGE_NICE={downloading:'Downloading',converting:'Preparing',transcribing:'Transcribing',
  diarizing:'Speakers',verifying:'Verifying',writing:'Writing',summarizing:'Summary'};
function shortDate(iso){if(!iso)return'';
  return new Date(iso+'T12:00:00').toLocaleDateString([],{month:'short',day:'numeric'});}

// today / yesterday, computed once in LOCAL time for the date buckets
function _isoLocal(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')
  +'-'+String(d.getDate()).padStart(2,'0');}
const TODAY=_isoLocal(new Date());
const YESTERDAY=(()=>{const d=new Date();d.setDate(d.getDate()-1);return _isoLocal(d);})();
// the three special date buckets, named once: dateBucket produces them and
// the accordion (grpMonthId / grpYear) and the rail match on them
const BUCKET={today:'Today',yesterday:'Yesterday',undated:'Undated'};
function dateBucket(iso){
  if(!iso)return BUCKET.undated;
  if(iso===TODAY)return BUCKET.today;
  if(iso===YESTERDAY)return BUCKET.yesterday;
  return new Date(iso+'T12:00:00').toLocaleDateString([],{month:'long',year:'numeric'});}
function nameBucket(title){const t=(title||'').trim();
  return /^[a-z]/i.test(t)?t[0].toUpperCase():'#';}

// three-zone ordering (by-date view, docs/ui-refresh phase 2). The pipeline's
// live states form the "Now" section, in this fixed order, so a failed April
// file sits at the top rather than buried in April. A meeting with an open
// step forms "To finish" (timelineSections). Everything else groups by its own
// meeting date. Sorting by one key while grouping by another is what
// fragmented the months; the library therefore sorts AND groups by dateKey.
const PIN_ORDER={recording:0,processing:1,failed:2,held:3,waiting:4};
const NOW_STATES=new Set(Object.keys(PIN_ORDER));
function dateKey(r){return r.date||((r.when||'').slice(0,10));}   // fall back to last-activity

/* ==================== the year-to-month accordion ====================
 * (DESIGN.md, Indigo Blueprint.) Month sections nest under YEAR sections
 * only when the library spans two or more years -- a single-year library
 * gets no year header, because there is nothing to fold it against. The
 * pinned actionable cluster is never part of this: it sits above every
 * section and never folds.
 *
 * Section ids are DATE-derived, not index-derived, so a persisted state
 * survives a month arriving, a rename, or a re-sort:
 *   y-2026        a year section
 *   m-2026-07     a month section
 *   m-today / m-yesterday / m-undated   the three special buckets
 * Today and Yesterday leave their own month's bucket (dateBucket peels them
 * off), so m-today can never collide with m-2026-09.
 */
function grpMonthId(g){
  if(g.key===BUCKET.today)return 'm-today';
  if(g.key===BUCKET.yesterday)return 'm-yesterday';
  if(g.key===BUCKET.undated)return 'm-undated';
  const k=dateKey(g.rows[0]||{})||'';
  return 'm-'+(k.slice(0,7)||'x');
}
function grpYear(g){
  if(g.key===BUCKET.undated)return '';       // undated belongs to no year
  const k=dateKey(g.rows[0]||{})||'';
  return k.slice(0,4)||((String(g.key).match(/\d{4}/)||[])[0]||'');
}
// stamp the accordion identity on every date group (the pinned cluster has none)
function grpStamp(groups){
  for(const g of groups){if(g.pinned)continue;g.id=grpMonthId(g);g.year=grpYear(g);}
  return groups;
}
// year sections appear ONLY when the library spans two or more years: a single
// year has nothing to fold against, so it gets month sections and no year line
function grpNest(groups){
  return new Set(groups.filter(g=>!g.pinned&&g.year).map(g=>g.year)).size>=2;
}
// the current year / month / previous month, read at CALL time: the panel
// polls forever, so a load-time constant would still name last month after
// the page sat open across midnight on the first.
function curMonths(){
  const t=_isoLocal(new Date());
  const d=new Date(t+'T12:00:00');d.setDate(1);d.setMonth(d.getMonth()-1);
  return {year:t.slice(0,4),month:t.slice(0,7),
    prev:d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')};
}
// defaults: the current and previous month open, the current year open,
// everything older folded. A month that has never been seen therefore opens
// exactly when it is current, which is what "a new month opens by default"
// means once the state is derived rather than stored. A year also opens when
// it HOLDS the previous month -- otherwise, every January, a folded prior
// year would swallow December and Yesterday with it.
function grpDefaultOpen(id){
  const cm=curMonths();
  if(id.slice(0,2)==='y-'){const y=id.slice(2);
    return y===cm.year||y===cm.prev.slice(0,4);}
  if(id==='m-today'||id==='m-yesterday')return true;
  if(id==='m-undated'||id==='q-more')return false;   // q-more: Now's queue fold
  const ym=id.slice(2);
  return ym===cm.month||ym===cm.prev;
}
const GRP_KEY='stt_groups';
let GRP=(()=>{try{const v=JSON.parse(localStorage.getItem(GRP_KEY)||'{}');
  return (v&&typeof v==='object')?v:{};}catch(e){return {};}})();
function grpOpen(id){const v=GRP[id];
  return v==='open'?true:v==='closed'?false:grpDefaultOpen(id);}
function grpSet(id,open){GRP[id]=open?'open':'closed';
  try{localStorage.setItem(GRP_KEY,JSON.stringify(GRP));}catch(e){}}
// A search or the flagged filter FORCES every section open so a match is never
// hidden behind a fold. The stored state is not touched -- neither by the
// filter nor by a click on a header WHILE it is on -- so clearing the filter
// restores exactly the folds the user left.
function grpForced(){return $('#search').value.trim().length>0||LF.flags||LF.voices;}
function grpToggle(id){
  if(grpForced())return;      // already open by the filter: store nothing, redraw nothing
  grpSet(id,!grpOpen(id));
  if(S){drawTimeline(S);afterRender();}
}
// the open state of every date section, computed ONCE per render: the render
// signature and the markup then read the same two flags instead of each
// re-deriving the rule (they disagreed once, and the fold flickered).
function grpStates(groups,nest,forced){
  for(const g of groups){
    if(g.pinned)continue;
    g.open=forced||grpOpen(g.id);
    g.yearOpen=(!nest||!g.year)?true:(forced||grpOpen('y-'+g.year));
  }
  return groups;
}

/* ============================ status pill ============================ *
 * The ONLY place pipeline state appears. Two segments, each shown only when it
 * has something to say (docs/ui-refresh phase 2): the pipeline, by priority,
 *   REC ticking  ->  <stage> N  ->  paused N waiting  ->  N waiting
 * and beside it the naming, "naming 3 of 7" -- its own segment, so a naming
 * never hides behind a transcription. Nothing to say hides the pill.
 */
// the running segment's word: the active files' own stage ("speakers" while
// diarizing), "transcribing" only while transcribing. Files in different
// stages, or a stage not reported yet, read "processing".
function pillStage(active){
  const st=new Set(Object.values(active||{}).map(e=>e&&e.stage));
  const one=st.size===1?[...st][0]:null;
  return one&&STAGE_NICE[one]?STAGE_NICE[one].toLowerCase():'processing';
}
function pillParts(s){
  const tl=s.timeline||[];
  const rec=s.recording;
  const waiting=tl.filter(r=>r.state==='waiting').length;
  // paused shows the whole queued backlog (waiting + held), not just waiting
  const queued=tl.filter(r=>r.state==='waiting'||r.state==='held').length;
  let cls='pill',pipe='';
  if(rec){
    cls='pill rec';
    const c=`<span id="pillClock">${clock(rec.elapsed_secs)}</span>`;
    pipe=rec.paused?`&#9208; REC ${c}`:`<span class="recdot"></span>REC ${c}`;
  }else if(s.running){
    cls='pill accent';
    const n=Object.keys(s.active||{}).length||1;
    // a file past its stage's estimate says so in place of the countdown
    // (the longest overrun when two run); the queue total, which carries
    // that file's tail, follows only when other files wait behind it
    const over=Object.values(s.active||{}).reduce((a,e)=>
      e&&e.over_sec!=null&&(a==null||e.over_sec>a)?e.over_sec:a,null);
    const eta=over!=null
      ?` &middot; ${fmtOver(over)}`+((s.pending||[]).length&&s.overall_eta_sec!=null
        ?` &middot; &#8776;${fmtEta(s.overall_eta_sec)} in all`:'')
      :s.overall_eta_sec!=null?` &middot; &#8776;${fmtEta(s.overall_eta_sec)}`:'';
    // runs requested while this one holds the lock wait in the job queue; the
    // pill carries their count so a queued Redo is never invisible
    const qj=(s.queued_jobs||[]).length;
    pipe=`${pillStage(s.active)} ${n}${eta}${qj?` &middot; ${qj} queued`:''}`;
  }else if(s.paused){
    cls='pill amber';
    pipe=`&#9208; paused${queued?` &middot; ${queued} waiting`:''}`;
  }else if(waiting){
    pipe=`${waiting} waiting`;
  }
  // the naming segment: BOTH signals (relabel_pending alone only means a
  // second pass is QUEUED behind a first). The queue knows how many meetings
  // this naming touches, so say so; a whole-library pass re-lists as it goes
  // and has no total, so it keeps the old wording rather than inventing one.
  let naming='';
  if(s.relabel_running||s.relabel_pending){
    const j=(s.jobs||{}).running;
    naming=(j&&j.total)?`naming ${Math.min(j.done+1,j.total)} of ${j.total}`
      :'applying names&#8230;';
  }
  const segs=[pipe,naming].filter(Boolean);
  if(!segs.length)return null;
  return {cls,html:segs.length>1
    ?segs.map(x=>`<span class="pseg">${x}</span>`).join(''):segs[0]};
}
function drawPill(s){
  const pill=$('#pill'),p=pillParts(s);
  pill.hidden=!p;
  if(p){pill.className=p.cls;pill.innerHTML=p.html;}
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

/* ==================== recorder outcome (success) ==================== *
 * The last recording SAVED ("Saved X - processing"): good news, so a quiet
 * sub-styled line right under the header, never an amber tray ask. The server
 * expires a success note on its own (~20s TTL in stt/status.py), so the line
 * needs no dismiss and disappears by itself when the poll stops carrying it.
 * A FAILED note is a decision and renders as a Now row instead (nowTrayHTML).
 */
function drawRecOk(s){
  const el=$('#recok');if(!el)return;
  const rn=s.recorder_note;
  const on=!!(rn&&rn.ok);
  el.hidden=!on;
  if(!on){el.dataset.at='';el.textContent='';return;}
  if(el.dataset.at===rn.at)return;        // unchanged note: nothing to redo
  el.dataset.at=rn.at;
  el.textContent='✓ '+rn.text;       // the old strip's copy, verbatim
}

/* ====================== the tray's items, in Now ====================== *
 * /api/state still carries a ranked `tray` of what needs the user, but the
 * band that showed it is gone (docs/ui-refresh phase 2). Its urgent kinds get
 * one compact row each at the top of the Now section -- the recorder's FAILED
 * note, a recorder stall, the duplicate-file sweep, and duplicate transcripts
 * -- and a failed file is its own failed row in Now, dismiss and Retry
 * included. The chronic kinds became the To finish eyebrow's two actions:
 * "Name N voices" (N from the timeline's finish records, see voiceQueueCount;
 * the unknown_voice count only when no live meeting holds a voice) and
 * "Review flags" (review).
 */
function _trayVerb(label,onclick,busy){
  return `<button class="btn mini tw-verb" type="button" onclick="${onclick}"${busy?' disabled':''}>${label}</button>`;
}
function _trayRow(title,detail,verb){
  return `<div class="trayrow">
    <span class="tw-title">${esc(title)}</span>
    <span class="tw-detail">${esc(detail)}</span>
    ${verb}</div>`;
}
// the armed state of the duplicate-file sweep: deleting files has no undo, so
// it is the house two-step, and the flag rides the timeline signature (via
// traySig) so a 2s poll cannot disarm it under the user's cursor. A FAILURE is
// state too (DUPE_ERR): writing it straight into the DOM died within 2s,
// because the signature was computed before the flag flipped and the next
// poll rebuilt the list over it.
let DUPE_ARMED=false,DUPE_ERR='';
function nowRedraw(){if(S&&(route.view==='timeline'||PANES)){drawTimeline(S);afterRender();}}
function dupeArm(on){DUPE_ARMED=!!on;if(on)DUPE_ERR='';nowRedraw();}
// the armed state belongs to the dupe_files row that armed it. Arm the sweep,
// walk away without confirming, and the flagged file leaves the queue by
// another path: the flag used to survive, so the NEXT duplicate row (days
// later, different files) rendered pre-armed and its first click fired the
// no-undo bulk delete with confirm:true. Cleared whenever that row is gone,
// and on every route change.
function dupeDisarm(){DUPE_ARMED=false;DUPE_ERR='';}
async function dupeDeleteGo(btn){
  if(btn){btn.disabled=true;btn.innerHTML='Deleting&#8230;';}
  let r;
  try{r=await api('/api/queue_delete_dupes',{confirm:true});}
  catch(e){r=null;}
  DUPE_ARMED=false;
  if(!r||!r.ok){
    DUPE_ERR=(r&&r.error)||'Could not delete those files.';
    nowRedraw();
    return;
  }
  DUPE_ERR='';
  // ok:true with an empty deleted list is NOT success: every server-side skip
  // (held, mid-run, the matched meeting gone, the last copy, a changed file,
  // an unlink that failed) answered that way, so the row re-rendered its
  // Delete button and silently no-opped on every further click. The server
  // names each skip; say so in the row's own detail line.
  if(!(r.deleted&&r.deleted.length)){
    const why=[...new Set((r.skipped||[]).map(x=>x.reason).filter(Boolean))];
    DUPE_ERR=why.length
      ?'Nothing was deleted: '+why.join('; ')+'.'
      :'Nothing was deleted: no waiting file is still an exact copy.';
    nowRedraw();
    refresh();
    return;
  }
  refresh();
}
// what the Now rows depend on, folded into the timeline's change signature so
// a poll neither drops an armed sweep nor thrashes the rows. It runs BEFORE
// the signature is compared: no dupe_files item means there is nothing armed
// to confirm (see dupeDisarm).
const NOW_KINDS=new Set(['recorder_stall','dupe_files','dupe_meetings']);
// the outcome of a Now row's own Fix verb, keyed by kind: {at,label,busy,
// detail}. `at` ties it to the note (or stall) that asked, so a new one starts
// clean. Rendered by nowTrayHTML and folded into traySig.
let NOW_MSG={};
function nowMsg(kind,at){const m=NOW_MSG[kind];return m&&m.at===at?m:null;}
function traySig(s){
  const items=s.tray||[];
  if(!items.some(t=>t.kind==='dupe_files'))dupeDisarm();
  const note=(s.recorder_note&&!s.recorder_note.ok)?s.recorder_note:null;
  return JSON.stringify(items.filter(t=>NOW_KINDS.has(t.kind))
      .map(t=>[t.kind,t.title,t.detail,t.target,t.count,t.exact]))
    +'|'+(note?note.at+'\x1f'+note.text:'')
    +'|'+voiceQueueCount(s.timeline)+'|'+trayCount(s,'unknown_voice')+'|'+trayCount(s,'review')
    +'|'+DUPE_ARMED+'|'+DUPE_ERR+'|'+JSON.stringify(NOW_MSG);
}
function trayCount(s,kind){return (s.tray||[]).filter(t=>t.kind===kind).length;}
// the urgent kinds, one compact row each, in the tray's own rank order
function nowTrayHTML(s){
  const items=s.tray||[];
  // the recorder's last outcome when it FAILED ("captured NO audio..."): a
  // decision, so it outranks everything, even a live stall. A SUCCESS note is
  // the quiet #recok line under the header instead (drawRecOk), never in here.
  const note=(s.recorder_note&&!s.recorder_note.ok)?s.recorder_note:null;
  let h='';
  // the note text verbatim, Fix permissions when it reads like a permission
  // problem (the old strip's gate), and an x that dismisses it server-side
  // (it persists until then)
  if(note){
    const m=nowMsg('recorder_note',note.at);
    const fix=/[Mm]icrophone|audio/.test(note.text)
      ?_trayVerb(m?m.label:'Fix permissions',`trayAct('recorder_note','')`,m&&m.busy):'';
    const x=`<button class="btn mini tw-verb" type="button" title="Dismiss"
      onclick="api('/api/recorder_note',{clear:true}).then(refresh)">&#10005;</button>`;
    h+=`<div class="trayrow">
      <span class="tw-title">Recorder</span>
      <span class="tw-detail">${esc(m&&m.detail||note.text)}</span>
      <span class="tw-acts">${fix}${x}</span></div>`;
  }
  // rare + urgent: one line each, acted on in place
  for(const t of items.filter(t=>t.kind==='recorder_stall')){
    const m=nowMsg('recorder_stall',t.target);
    h+=_trayRow(t.title,m&&m.detail||t.detail,
      _trayVerb(m?m.label:'Fix',`trayAct('recorder_stall','${escJs(t.target)}')`,m&&m.busy));
  }
  // waiting files that were already processed: the next automatic run spends
  // real hours transcribing them again. The bulk delete only ever covers the
  // byte-identical ones (t.exact) -- a name-only match is deleted one file at
  // a time, from its own row, on purpose.
  for(const t of items.filter(t=>t.kind==='dupe_files')){
    const verbs=DUPE_ARMED
      ?`<button class="btn mini tw-verb" type="button" onclick="dupeArm(false)">Cancel</button>
        <button class="btn danger mini tw-verb" type="button"
          onclick="dupeDeleteGo(this)">Delete ${t.exact} file${t.exact!==1?'s':''}</button>`
      :(t.exact?_trayVerb('Delete copies',`dupeArm(true)`):'');
    // a failed sweep replaces the detail until the next arm or a success:
    // the renderer owns this text, so a poll's rebuild keeps it visible
    h+=_trayRow(t.title,DUPE_ERR&&!DUPE_ARMED?DUPE_ERR:t.detail,verbs);
  }
  for(const t of items.filter(t=>t.kind==='dupe_meetings'))
    h+=_trayRow(t.title,t.detail,_trayVerb('Review &#8594;',`trayAct('dupe_meetings','')`));
  return h;
}

/* ============================ timeline ============================ *
 * One row per meeting/file. The server already sorts it (recording + running
 * pinned to the top, then newest first); by-date grouping preserves that order,
 * by-name re-sorts alphabetically. Read-only: buttons render but are stubs.
 */
// Boardroom category mark, in the card's right rail: a tagged row wears the
// colored chip; an untagged row shows the hollow dot. Both cycle
// untagged -> work -> personal on click (the chip IS the affordance).
function catDot(row){
  const c=row.category||'none';
  if(c==='work'||c==='personal'){
    const t=c==='work'?'Work':'Personal';
    return `<button class="catchip ${c}" type="button" title="${t} (click to change tag)"
      onclick="cycleCat(event,'${escJs(row.id)}')">${t}</button>`;
  }
  return `<button class="cat none" type="button" title="Untagged (click to tag Work / Personal)"
    onclick="cycleCat(event,'${escJs(row.id)}')"></button>`;
}
// waiting/held rows also carry a checkbox so a run of files can be picked for
// "Process selected". The category mark lives in the right rail (catDot above),
// not the gutter. Selected state is REAPPLIED after render (applySel), so
// gutter itself stays a pure function of the row.
function gutter(row){
  const selectable=row.state==='ready'||row.state==='waiting'||row.state==='held';
  return selectable
    ? `<input class="chk" type="checkbox" aria-label="Select for bulk actions"
        onclick="toggleSel('${escJs(row.id)}',this.checked)">`
    : `<span class="chk spacer" aria-hidden="true"></span>`;
}

// entities: &#9654;=play  &#10073;=heavy bar  &#10005;=x  &#8776;=approx  &#8943;=ellipsis
function slotActions(inner){return `<span class="ractions">${inner}</span>`;}

// This waiting file was already processed. Said ON THE ROW, before the batch
// ever picks it up: transcribing it again costs real hours and leaves a twin in
// the library. The two signals are not equally strong and do not read alike --
// identical bytes is proof, a shared filename is a suggestion (a recurring
// export honestly reuses one name for different meetings), so only the first
// one claims the file is a copy. The meeting it matches is one click away.
function dupNote(row){
  if(!row.dup_of)return '';
  const title=esc(row.dup_title||row.dup_of);
  const lead=row.dup_reason==='identical'
    ?'identical to'
    :'same source name as';
  // an ARCHIVED match has no page in the meeting view (it renders live
  // meetings only), so the chip linked to a permanent "Loading meeting"
  // spinner. Name it plainly and say where it lives instead.
  const what=row.dup_archived
    ?`<span class="dupname">${title}</span> <span class="dupwhere">(archived)</span>`
    :`<a class="dlink" href="#m/${encodeURIComponent(row.dup_of)}">${title}</a>`;
  return `<div class="rmeta dupnote"><span class="dupflag">already processed</span>
    ${lead} ${what}</div>`;
}

// the filename minus its final extension, and nothing else: the date and topic
// the user typed into the name are why the Title step offers it (fsUseFile).
function srcStem(name){
  if(!name)return '';
  const i=name.lastIndexOf('.');
  return i>0?name.slice(0,i):name;
}

// A queued file's name in two parts: when its last word is short (the date
// and extension, "... Meeting 09102026.mp4") that word stays whole and the
// head ends in the ellipsis, so recordings that share a long prefix still
// read apart in the narrow list pane. A name with no such word is one part.
function queueName(name){
  const t=String(name||''),i=t.lastIndexOf(' ');
  if(i<=0||t.length-i>18)return `<span class="qhead">${esc(t)}</span>`;
  return `<span class="qhead">${esc(t.slice(0,i))}</span><span class="qtail">&nbsp;${esc(t.slice(i+1))}</span>`;
}
// A waiting or held file is ONE compact line: the file name, then its size
// and estimate in mono at the right, and the same hover actions it always had.
// Five multi-hour board recordings as full rows pushed To finish below the
// fold. The name's tooltip carries what the line leaves out: the whole name,
// its size, the per-stage split of the estimate, and what held means. A
// duplicate still says so on the line (dupNote), since it can save hours of
// transcription.
function queueBodyAndSlot(row){
  const held=row.state==='held',id=escJs(row.id);
  const size=row.size_mb!=null?row.size_mb+' MB':'';
  const est=row.est_minutes?'&#8776;'+fmtEta(row.est_minutes*60):'';
  const tip=[row.title,size,row.est_detail,held?'Held: automatic runs skip this until you release it':'']
    .filter(Boolean).map(esc).join('&#10;');
  // the size is its own span: the narrow list pane shows the estimate alone
  const info=(size||est)?`<span class="mono">${size?`<span class="qsize">${size}${est?' &middot; ':''}</span>`:''}${est}</span>`:'';
  const state=held?`<span class="qheld">&#10073;&#10073; held</span>${info?' '+info:''}`
    :(info||'waiting');
  return `<div class="rbody"><div class="rtitle" title="${tip}">${queueName(row.title)}</div>${dupNote(row)}</div>
        <div class="rslot">
          <span class="rstate qinfo yields">${state}</span>
          ${slotActions(
            `<button class="iact play" type="button" onclick="rowListen('${id}')" title="Listen">&#9654;</button>
             ${held?`<button class="iact" type="button" onclick="rowRelease('${id}')">Release</button>`
               :`<button class="iact" type="button" onclick="rowHold('${id}')" title="Hold">&#10073;&#10073;</button>`}
             <button class="iact" type="button" onclick="rowProcess('${id}')">Process</button>
             <button class="iact" type="button" onclick="rowDelete('${id}',event)" title="Delete">&#10005;</button>`)}
        </div>`;
}

function bodyAndSlot(row){
  switch(row.state){

    case 'recording':{
      // the elapsed clock is the one piece of true mono data in a row slot
      const state=row.paused
        ?`&#9208; paused <span id="recRowClock" class="mono">${clock(row.elapsed_secs)}</span>`
        :`<span class="capdot"></span>capturing <span id="recRowClock" class="mono">${clock(row.elapsed_secs)}</span>`;
      return `<div class="rbody"><div class="rtitle">Recording now&#8230;</div></div>
        <div class="rslot"><span class="rstate spill rec">${state}</span></div>`;
    }

    case 'waiting':
    case 'held':
      return queueBodyAndSlot(row);

    case 'processing':{
      const pct=row.pct!=null?row.pct:null;
      // past the stage's estimate: how far past, never a frozen remainder
      const eta=row.over!=null?` &middot; ${fmtOver(row.over)}`
        :row.eta!=null?` &middot; &#8776;${fmtEta(row.eta)} left`:'';
      return `<div class="rbody"><div class="rtitle">${esc(row.title)}</div></div>
        <div class="rslot"><span class="rstate spill busy">${esc(STAGE_NICE[row.stage]||row.stage||'working')}${pct!=null?' '+pct+'%':''}${eta}</span></div>
        ${pct!=null?`<span class="progress" style="width:${pct}%"></span>`:''}`;
    }

    case 'ready':{
      const bits=[];
      // the date is click-to-edit in place (old page parity); rowDateEdit
      // swaps it for a date input, /api/set_date re-stamps the folder
      if(row.date)bits.push(`<span class="rdate" title="Click to change the meeting date" onclick="rowDateEdit(event,'${escJs(row.id)}')">${shortDate(row.date)}</span>`);
      if(row.minutes!=null)bits.push(row.minutes+' min');
      if(row.speakers&&row.speakers.length)bits.push(esc(row.speakers.join(', ')));
      // Boardroom right rail, beneath the state pill: the category chip (or
      // the hollow untagged dot) and the review count as plain amber TEXT
      // (no chip; forty chip-wearing rows read as a wall of warnings). Still
      // click-to-review via the same bridge.
      const railsub=[catDot(row)];
      if(row.review_substantial)
        railsub.push(`<span class="rev" onclick="openReviewBadge('${escJs(row.id)}')" title="Step through the flagged segments">${row.review_substantial} to check</span>`);
      else if(row.review_minor)
        railsub.push(`<span class="rev minor" onclick="openReviewBadge('${escJs(row.id)}')" title="Minor crumbs to skim">${row.review_minor} minor</span>`);
      // click-to-expand peek (replaces the old hover tooltip): the FULL summary
      // and committed next steps come from the already-polled meetings entry
      // (the timeline row only carries a preview); no extra fetch. Rendered
      // collapsed; .row.open (from OPEN) reveals it, height animated in CSS.
      const m=meetingByBase(row.id)||{};
      const sum=m.summary||row.summary||'';
      const steps=(m.next_steps&&m.next_steps.length)
        ?`<div class="rexsteps-h">Committed next steps</div><ul class="rexsteps">${
            m.next_steps.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>`:'';
      const exp=`<div class="rexp"><div class="rexpin">
          <div class="rexsum${sum?'':' muted'}">${esc(sum||'No summary yet.')}</div>${steps}
          <a class="rexopen" href="#m/${encodeURIComponent(row.id)}">Open transcript &#8594;</a>
        </div></div>`;
      return `<div class="rbody">
          <div class="rtitle"><span class="rname" title="Click to rename" onclick="rowTitleEdit(event,'${escJs(row.id)}')">${esc(row.title)}</span></div>
          <div class="rmeta">${bits.join(' &middot; ')}</div>
          ${row.has_summary&&row.summary?`<div class="rsummary">${esc(row.summary)}</div>`:''}
          ${exp}
        </div>
        <div class="rslot">
          <span class="rstate spill ok yields">ready</span>
          ${slotActions(
            `<button class="iact play" type="button" onclick="rowListen('${escJs(row.id)}')" title="Listen without opening">&#9654;</button>
             <button class="iact" type="button" onclick="openMeeting('${escJs(row.id)}')">Open</button>
             <button class="iact" type="button" onclick="rowMenu('${escJs(row.id)}',event)" title="Export, rename, redo">&#8943;</button>`)}
          <div class="rsub">${railsub.join('')}</div>
        </div>`;
    }

    case 'failed':
      // the note comes from the row: "still in the folder, re-runs" and "the
      // original is gone" are OPPOSITE situations, and the old hardcoded line
      // claimed the first even when the server said the second. A gone source
      // gets no Retry (there is nothing to run) and its X dismisses the record
      // instead of calling the file-delete endpoint, which would refuse.
      return `<div class="rbody">
          <div class="rtitle">${esc(row.title)}</div>
          <div class="rmeta err">${esc(row.error||'failed')}</div>
          <div class="rmeta">${esc(row.retry_note||'original stays in the watched folder')}</div>
        </div>
        <div class="rslot">
          <span class="rstate spill rec yields">failed</span>
          ${slotActions(row.gone
            ?`<button class="iact" type="button" onclick="failDismiss('${escJs(row.id)}')" title="Dismiss this failure">&#10005;</button>`
            :`<button class="iact" type="button" onclick="rowRetry('${escJs(row.id)}')">Retry</button>
             <button class="iact" type="button" onclick="rowDelete('${escJs(row.id)}',event)" title="Remove">&#10005;</button>`)}
        </div>`;

    default:
      return `<div class="rbody"><div class="rtitle">${esc(row.title||'')}</div></div>`;
  }
}
// expanded ready rows (click-to-expand peek), keyed by row id. Multiple rows may
// stay open; the flag folds into sigOf so expansions survive the 2s polls.
const OPEN=new Set();
function toggleExpand(id){
  OPEN.has(id)?OPEN.delete(id):OPEN.add(id);
  // flip the live row now (the CSS grid-rows transition animates the height,
  // ~180ms; none under reduced motion); the next poll re-renders it already
  // in this state, so nothing snaps back
  const el=document.querySelector('.row[data-id="'+CSS.escape(id)+'"]');
  if(el)el.classList.toggle('open',OPEN.has(id));
}

function rowHTML(row){
  // a meeting waiting for its title is a compact To finish row in either
  // sort: the finish strip's Title step names it, so the list never holds a form
  if(row.state==='needs_name')return finishRowHTML(row);
  const open=row.state==='ready'&&OPEN.has(row.id);
  const q=row.state==='waiting'||row.state==='held';
  return `<div class="row${open?' open':''}${q?' qrow':''}" data-state="${esc(row.state)}" data-id="${esc(row.id)}" tabindex="0">`
    +gutter(row)+bodyAndSlot(row)+`</div>`;
}

/* ====================== the Now and To finish sections ====================== *
 * (docs/ui-refresh phase 2.) The by-date list opens with two sections above
 * the accordion, each under the house eyebrow: "Now" (the pipeline's live
 * rows and the tray's urgent kinds) and "To finish" (every meeting with an
 * open step: its title, or any step on a meeting processed in the last 14
 * days). A section with nothing in it renders nothing; a finished meeting
 * lives only in the accordion.
 */
function timelineSections(rows,now){
  const out={now:[],fin:[],rest:[]};
  for(const r of rows||[]){
    if(NOW_STATES.has(r.state))out.now.push(r);
    else if(toFinish(r,now))out.fin.push(r);
    else out.rest.push(r);
  }
  // Needs you keeps the server's own rank
  out.fin.sort((a,b)=>(a.needs_you_rank||0)-(b.needs_you_rank||0));
  // waiting files in the order they will run (the server's run_order:
  // shortest first); everything else newest first
  out.now.sort((a,b)=>(PIN_ORDER[a.state]-PIN_ORDER[b.state])
    ||((a.run_order!=null?a.run_order:1e9)-(b.run_order!=null?b.run_order:1e9))
    ||(b.when||'').localeCompare(a.when||''));
  // To finish keeps the timeline's own order: it is the order ] walks
  out.rest.sort((a,b)=>dateKey(b).localeCompare(dateKey(a))
    ||(b.when||'').localeCompare(a.when||''));
  return out;
}
// Now's queue: past three waiting or held files, the rest fold under ONE
// line, "N more waiting · ≈Xh" (their count and summed estimate), which a
// click opens. The fold persists as the accordion's do (stt_groups, id
// q-more; folded by default), and a search or a filter forces it open, as it
// forces every section, so a match is never hidden. The queue rows sit last
// in Now (PIN_ORDER), so the fold is always the tail of the section.
// Answers the markup and the ids the fold hides (the selection drops them).
const QUEUE_SHOW=3,QUEUE_FOLD='q-more';
function nowRowsHTML(rows,open){
  const q=(rows||[]).filter(r=>r.state==='waiting'||r.state==='held');
  if(q.length<=QUEUE_SHOW)return {html:(rows||[]).map(rowHTML).join(''),hidden:[]};
  const rest=q.slice(QUEUE_SHOW),keep=rows.slice(0,rows.length-rest.length);
  const mins=rest.reduce((a,r)=>a+(r.est_minutes||0),0);
  const line=`<button class="ghead qmore" type="button" id="qmore" aria-expanded="${open?'true':'false'}"`
    +` onclick="grpToggle('${QUEUE_FOLD}')" title="${open?'Fold the rest of the queue':'Show the rest of the queue'}"`
    +`><span class="gchev" aria-hidden="true">${open?'&#9662;':'&#9656;'}</span`
    +`><span class="glabel"><span class="mono">${rest.length}</span> more waiting`
    +(mins?` &middot; <span class="mono">&#8776;${fmtEta(mins*60)}</span>`:'')+`</span></button>`;
  return {html:keep.map(rowHTML).join('')+line+(open?rest.map(rowHTML).join(''):''),
    hidden:open?[]:rest.map(r=>r.id)};
}
// the three steps as dots: done green with a check, open teal with its count
function finishDotsHTML(row){
  return `<span class="steps">`+finishSteps(row).slice(0,3).map(st=>{
    const done=st.state==='done';
    const c=!done&&st.count?` <span class="mono">${st.count}</span>`:'';
    return `<span class="sdot${done?' done':''}">${done?'&#10003; ':''}${st.label}${c}</span>`;
  }).join('')+`</span>`;
}
// the meta line of a compact row: the duration, up to three named people
// (the server's `named`, the most talk first) with "+N" for the rest, then
// the unnamed voices as ONE teal count, the same finish.voices the Voices dot
// and the strip show. A raw "Speaker N" or "Voice N" label never appears: a
// board meeting's row listed 22 of them.
function peopleMetaBits(row){
  const bits=[],named=row.named||[],n=(row.finish&&row.finish.voices)||0;
  if(row.minutes!=null)bits.push(row.minutes+' min');
  if(named.length)bits.push(esc(named.slice(0,3).join(', '))
    +(named.length>3?` +${named.length-3}`:''));
  if(n>0)bits.push(`<span class="unn">${n} unnamed</span>`);
  return bits;
}
// one compact row: the title and date, one meta line, the step dots. The row
// is a link to the meeting, where the finish strip takes over.
function finishRowHTML(row){
  const title=(row.state==='needs_name'&&row.suggested_title)||row.title||'';
  const bits=peopleMetaBits(row);
  // a Needs you row says why it is there (no speech, audio short, no title)
  if(row.needs_you_detail&&row.needs_you!=='needs_title')bits.unshift(esc(row.needs_you_detail));
  return `<a class="lrow" href="#m/${encodeURIComponent(row.id)}" data-id="${esc(row.id)}"`
    +` data-state="${esc(row.state)}"><span class="lt"><span class="ltitle">${esc(title)}</span>`
    +(row.date?`<span class="ldate">${esc(shortDate(row.date))}</span>`:'')+`</span>`
    +(bits.length?`<span class="lmeta">${bits.join(' &middot; ')}</span>`:'')
    +finishDotsHTML(row)+`</a>`;
}
// a section's eyebrow: the label, a hairline rule, then any actions
function secHeadHTML(label,id,acts){
  return `<div class="mgroup sechead" id="${id}"><span class="glabel">${label}</span>`
    +`<span class="grule" aria-hidden="true"></span>${acts||''}</div>`;
}
// the To finish eyebrow's two actions, each only when it applies: "Name N
// voices" walks the unknown voices meeting by meeting (fsVoicesStart), and
// "Review flags" presses or clears the "Has flags" chip (pressed while it is
// on).
// `panel`: no live meeting asks about these voices (each was heard only in
// archived meetings), so the button opens the naming panel on one at a time
// and its title says so.
function finishActsHTML(nVoices,nFlagged,on,panel){
  let h='';
  if(nVoices>0)h+=`<button class="secact" type="button" onclick="fsVoicesStart()"`
    +` title="${panel?'Heard only in archived meetings: name them one at a time'
      :'Name each unknown voice in turn, the most talk time first'}">`
    +`Name ${nVoices} voice${nVoices!==1?'s':''}</button>`;
  if(nFlagged>0||on)h+=`<button class="secact" type="button" onclick="flaggedToggle()"`
    +` aria-pressed="${on?'true':'false'}"`
    +` title="${on?'Show every meeting again':'Show only meetings with flagged lines'}">`
    +`${on?'Review flags &#10005;':'Review flags'}</button>`;
  return h;
}
// the markup above the accordion: Now (urgent items, uploads, live rows), then
// To finish (its eyebrow when it has rows or an action applies, then its rows)
function headSectionsHTML(nowHTML,fin,acts,extra){
  return (nowHTML?secHeadHTML('Now','sec-now')+nowHTML:'')
    +((fin.length||acts||extra)?secHeadHTML('Needs you','sec-fin',acts)
      +fin.map(finishRowHTML).join('')+(extra||''):'');
}
// the one voice row of Needs you: the unnamed voice with the most talk in
// non-public meetings. Its verb opens THAT voice (needsYouVoiceOpen), never
// whichever meeting holds the most talk library-wide. Optional: ignoring it
// costs nothing.
function needsYouVoiceHTML(s){
  const v=((s&&s.needs_you)||[]).find(x=>x.kind==='voice');
  if(!v)return '';
  const call=`needsYouVoiceOpen('${escJs(v.target)}','${escJs(v.uid||'')}')`;
  return _trayRow(v.title,v.detail,_trayVerb('Name &#8594;',call));
}
// a registry voice opens the naming panel on itself, with the row's meeting
// for its lines. A transcript-local voice opens its own meeting's Voices step.
function needsYouVoiceOpen(target,uid){
  if(uid){openNamePanelByUid(uid,target);return;}
  fsVoiceCarry(target,[],[],[]);
}

/* ================ search chips, Said in, and the setup check ================ *
 * (docs/ui-refresh phase 4.) The search field sits at the top of the list,
 * and one row of chips under it replaces the header's two selects and the
 * "flagged only" chip: All, one chip per category (Work, Personal), "Has
 * flags N", "Has voices N", and "A-Z", which switches to the by-name
 * grouping. A pressed chip clears with one click. The category and the
 * grouping persist in the keys the selects used (stt_mcat, stt_msort), so
 * nothing resets; Has flags and Has voices last for the session, as the
 * flagged-only filter did. Full-text hits head the list as "Said in", and an
 * empty library shows the setup check where the list would be.
 */
const CHIP_CATS=[['work','Work'],['personal','Personal']];
// the saved choices, read the way the selects stored them
function chipLoad(get){
  const c=get('stt_mcat'),s=get('stt_msort');
  return {cat:CHIP_CATS.some(x=>x[0]===c)?c:'',sort:s==='name'?'name':'date',
    flags:false,voices:false};
}
let LF=chipLoad(k=>{try{return localStorage.getItem(k);}catch(e){return null;}});
// the state after a click on one chip. All clears the three filters (the
// grouping is not a filter); a pressed chip clears itself.
function chipNext(st,key){
  const n={...st};
  if(key==='all'){n.cat='';n.flags=false;n.voices=false;}
  else if(CHIP_CATS.some(x=>x[0]===key))n.cat=st.cat===key?'':key;
  else if(key==='flags')n.flags=!st.flags;
  else if(key==='voices')n.voices=!st.voices;
  else if(key==='az')n.sort=st.sort==='name'?'date':'name';
  return n;
}
// the chips filter ready meetings only: Now and a meeting waiting for its
// title always stay, as they did under the flagged-only filter
function chipKeeps(r,st){
  if(!r||r.state!=='ready')return true;
  if(st.cat&&r.category!==st.cat)return false;
  if(st.flags&&!(r.review_substantial>0))return false;
  if(st.voices&&!(r.finish&&r.finish.voices>0))return false;
  return true;
}
// the counts on "Has flags" and "Has voices": the ready meetings each keeps
function chipCounts(timeline){
  let flags=0,voices=0;
  for(const r of timeline||[]){
    if(!r||r.state!=='ready')continue;
    if(r.review_substantial>0)flags++;
    if(r.finish&&r.finish.voices>0)voices++;
  }
  return {flags,voices};
}
// the chips in their order. A count chip shows while it has something to
// keep, or while it is pressed (so it can always be cleared).
function chipList(st,counts){
  const all=!st.cat&&!st.flags&&!st.voices;
  const out=[{key:'all',label:'All',on:all,
    title:all?'Showing every meeting':'Show every meeting: clear the filters'}];
  for(const [k,l] of CHIP_CATS)out.push({key:k,label:l,on:st.cat===k,
    title:st.cat===k?`Showing only ${l} meetings. Click to show all.`:`Show only ${l} meetings`});
  if(counts.flags||st.flags)out.push({key:'flags',label:'Has flags',n:counts.flags,on:st.flags,
    title:st.flags?'Showing only meetings with flagged lines. Click to clear.'
      :'Show only meetings with flagged lines to check'});
  if(counts.voices||st.voices)out.push({key:'voices',label:'Has voices',n:counts.voices,on:st.voices,
    title:st.voices?'Showing only meetings with a voice to name. Click to clear.'
      :'Show only meetings with a voice still to name'});
  out.push({key:'az',label:'A&#8211;Z',on:st.sort==='name',
    title:st.sort==='name'?'Grouped by name. Click to group by month.':'Group the list by name, A to Z'});
  return out;
}
function chipsHTML(list){
  return list.map(c=>`<button class="fchip${c.key==='az'?' az':''}" type="button"`
    +` data-k="${c.key}" aria-pressed="${c.on?'true':'false'}" title="${esc(c.title)}"`
    +` onclick="chipGo('${c.key}')">${c.label}`
    +(c.n!=null?` <span class="mono">${c.n}</span>`:'')
    +(c.on&&c.key!=='all'?' <span class="fx" aria-hidden="true">&#10005;</span>':'')
    +`</button>`).join('');
}
function chipGo(key){
  LF=chipNext(LF,key);
  try{localStorage.setItem('stt_mcat',LF.cat);localStorage.setItem('stt_msort',LF.sort);}catch(e){}
  render();
}
// the chip row, redrawn only when a chip or a count changes; a focused chip
// keeps its focus through the redraw
function drawChips(s){
  const box=$('#fchips');if(!box)return;
  const html=chipsHTML(chipList(LF,chipCounts(s&&s.timeline)));
  if(box.dataset.sig===html)return;
  const ae=document.activeElement,k=ae&&box.contains(ae)?ae.dataset.k:null;
  box.dataset.sig=html;box.innerHTML=html;
  const b=k&&box.querySelector('[data-k="'+k+'"]');
  if(b)b.focus({preventScroll:true});
}

let HITS=null;       // the last /api/search answer: {query, hits, total}
let SAID_TOP=null;   // the first line the Said in group shows: Enter opens it
// "Said in": the full-text hits, grouped by meeting in the server's order
// (newest meeting first). titleOf(base) names the meeting, or answers null
// for one the chips filter out, whose lines then drop too.
function saidGroups(hits,titleOf){
  const out=[],at=new Map();
  for(const h of hits||[]){
    const title=titleOf(h.base);
    if(title==null)continue;
    if(!at.has(h.base)){at.set(h.base,out.length);out.push({base:h.base,title,hits:[]});}
    out[at.get(h.base)].hits.push(h);
  }
  return out;
}
function hitClock(t){
  t=Math.max(0,Math.floor(t||0));
  return Math.floor(t/60)+':'+String(t%60).padStart(2,'0');
}
// the group's markup: the section eyebrow with the line count, then each
// meeting's title over its lines. A line is a button: a click or Enter opens
// the meeting at that moment (openHit, then mSeek).
function saidInHTML(groups,q,capped){
  if(!groups.length)return '';
  const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
  const hl=t=>esc(t).replace(rx,m=>'<mark>'+m+'</mark>');
  const n=groups.reduce((a,g)=>a+g.hits.length,0);
  return secHeadHTML('Said in','sec-said',`<span class="gcount mono">${n}</span>`)
    +groups.map(g=>`<div class="said"><div class="said-t">${esc(g.title)}</div>`
      +g.hits.map(h=>`<button class="hit" type="button" title="Open the meeting at this moment"`
        +` onclick="openHit('${escJs(h.base)}',${Number(h.start)||0})">`
        +`<span class="hit-t mono">${hitClock(h.start)}</span>`
        +`<span class="hit-x">${h.who?`<span class="hit-w">${esc(h.who)}</span> `:''}${hl(h.snippet)}</span>`
        +`</button>`).join('')+`</div>`).join('')
    +(capped?`<p class="said-more">The newest ${capped[0]} of ${capped[1]} lines. Add a word to narrow them.</p>`:'');
}

// the library holds no meeting yet (a file in Now does not count)
function libraryEmpty(timeline){
  return !(timeline||[]).some(r=>r&&(r.state==='ready'||r.state==='needs_name'));
}
// the first-run check: three lines from /api/state, each with the drawer
// section that fixes it ([section, anchor id])
function setupCheck(s){
  s=s||{};
  const src=(s.paths&&s.paths.source)||'';
  const me=s.mic_speaker||'';
  const enrolled=!!me&&(s.enrolled||[]).some(e=>e&&e.name===me);
  return [
    {key:'recorder',ok:!!s.recorder_ready,
      label:s.recorder_ready?'Recorder ready':'Recorder not installed',
      detail:s.recorder_ready?'The menu-bar recorder is installed.'
        :'Build it with ./setup.sh, then record from the menu bar.',
      btn:'Recorder settings',go:['settings','dg-recorder']},
    {key:'folder',ok:!!src,
      label:src?'Watched folder set':'No watched folder',
      detail:src?src.replace(/^\/Users\/[^/]+/,'~'):'New recordings are picked up from a watched folder.',
      btn:'Folder settings',go:['settings','drow-source']},
    {key:'voice',ok:enrolled,
      label:!me?'Your voice is not set':enrolled?'Your voice is enrolled':'Your voice is not enrolled yet',
      detail:!me?'Pick your name, so recorded calls separate you from the others.'
        :enrolled?me:`Name ${me} in your first meeting to enroll the voice.`,
      btn:!me?'Pick your voice':'Speakers',go:!me?['settings','dg-recorder']:['speakers','']},
  ];
}
function setupHTML(items){
  return `<section class="setup" aria-label="Setup check">`
    +items.map(it=>`<div class="setrow${it.ok?' ok':''}">`
      +`<span class="setmark" aria-hidden="true">${it.ok?'&#10003;':'!'}</span>`
      +`<div class="setbody"><div class="setname">${esc(it.label)}</div>`
      +`<div class="setsub">${esc(it.detail)}</div></div>`
      +`<button class="btn mini" type="button" onclick="openDrawerAt('${it.go[0]}','${it.go[1]}')">${esc(it.btn)}</button>`
      +`</div>`).join('')
    +`<p class="setdrop">Record from the menu bar, or drop an audio file here.</p></section>`;
}

// fields that decide whether a rebuild is needed -- elapsed_secs is deliberately
// excluded (the 1s ticker owns it) so a recording never thrashes the list.
// An expanded row folds its open flag (and the full summary/steps it shows)
// into the signature, so the 2s poll re-renders it still open.
function sigOf(r){
  const open=r.state==='ready'&&OPEN.has(r.id);
  const m=open?(meetingByBase(r.id)||{}):null;
  return [r.id,r.state,r.title,r.date,r.pct,r.stage,r.eta,r.over,r.category,
  // the duplicate flag lands a poll or two AFTER the row first painted (the
  // check hashes on a size collision), so it has to be part of the signature or
  // the chip never appears on a row that is already on screen
  r.dup_of,r.dup_reason,r.dup_archived,
  r.review_substantial,r.review_minor,r.has_summary,r.summary,r.size_mb,r.est_minutes,
  r.est_detail,
  r.held,r.error,r.suggested_title,r.suggested_date,r.paused,r.has_audio,r.minutes,
  (r.speakers||[]).join(','),(r.named||[]).join(','),
  // the finish record paints a To finish row's dots, and moves as voices are
  // named and flags checked while nothing else on the row changes
  r.finish?[r.finish.title,r.finish.voices,r.finish.flags].join(','):'',
  // Needs you membership, place, and reason move without the row changing
  r.needs_you,r.needs_you_rank,r.needs_you_detail,
  open?1:0,m?(m.summary||''):'',m?(m.next_steps||[]).join(''):''];}

function drawTimeline(s){
  const tl=$('#timeline');
  const q=$('#search').value.trim().toLowerCase();
  const sort=LF.sort;

  const all=s.timeline||[];
  // the chips (a category, Has flags, Has voices) filter ready rows only; Now
  // and the meetings waiting for a title stay visible
  const rows=all.filter(r=>{
    if(!chipKeeps(r,LF))return false;
    if(q){
      // match the visible name (a needs_name row shows its suggested title) plus speakers
      const hay=((r.title||'')+' '+(r.suggested_title||'')+' '+((r.speakers||[]).join(' '))).toLowerCase();
      if(!hay.includes(q))return false;
    }
    return true;
  });

  // synthetic upload rows: prune the ones whose REAL waiting row has landed
  // (the same rebuild adds one and drops the other, so the swap never
  // flickers), then pin the survivors above every row
  uploadsPrune();
  const upHTML=UPLOADS.map(uploadRowHTML).join('');

  // the tray's urgent kinds and the To finish actions never depend on the
  // sort, a search, or an empty library: the band that held them is gone, so
  // they must stay reachable from every state of the list. traySig runs first
  // (it disarms a dupe sweep whose row is gone).
  const tsig=traySig(s);
  const urgent=nowTrayHTML(s);
  // "Name N voices": the voices the queue will ask about, each once. With
  // none in a live meeting, the tray's unknown voices, which the button opens
  // one at a time in the naming panel (fsVoicesStart's fallback).
  // Needs you asks for no flags and holds one voice row (needsYouVoiceHTML),
  // so the eyebrow keeps only the way out of a pressed Has flags filter
  const acts=finishActsHTML(0,0,LF.flags,false);
  const nyv=needsYouVoiceHTML(s);

  // "Said in": the full-text hits for the query in the field, at the top of
  // the list. A meeting the chips filter out drops its lines too. SAID_TOP is
  // the line Enter in the search field opens.
  const byId=new Map(all.map(r=>[r.id,r]));
  const live=HITS&&q.length>=3&&HITS.query===q?HITS:null;
  const groups0=live?saidGroups(live.hits,base=>{
    const r=byId.get(base);
    if(!r)return base;
    if(!chipKeeps(r,LF))return null;
    return (r.state==='needs_name'&&r.suggested_title)||r.title||base;
  }):[];
  SAID_TOP=groups0.length?groups0[0].hits[0]:null;
  const capped=live&&live.total>live.hits.length?[live.hits.length,live.total]:null;
  const said=saidInHTML(groups0,q,capped);
  const ssig=groups0.map(g=>g.base+':'+g.hits.length).join(',')+(capped?':'+capped:'');
  // an empty library: the setup check stands where the list would be
  const first=libraryEmpty(all);
  const setup=first?setupHTML(setupCheck(s)):'';
  const fsig=ssig+'|'+setup+'|'+nyv;
  const lfsig=LF.cat+':'+LF.flags+':'+LF.voices;

  // empty states (quiet, centered), rendered inside the timeline under Now.
  // "No matches." only when the Said in group found nothing either.
  if((all.length===0||rows.length===0)&&!UPLOADS.length){
    const empty=first?setup:said?'':'<p class="empty">No matches.</p>';
    const sig=(first?'EMPTY':'NOMATCH:'+lfsig+':'+q)+'|'+tsig+'|'+fsig;
    $('#rail').hidden=true;
    if(tl.dataset.sig===sig)return;
    tl.dataset.sig=sig;
    tl.innerHTML=said+headSectionsHTML(urgent,[],acts,nyv)+empty;
    return;
  }

  // ---- ordering + grouping ----
  // by-name: one flat alphabetical list, letter groups, under Now's urgent
  // items and the To finish actions.
  // by-date: Now and To finish first (one unfoldable group above every
  // section), then the library sorted AND grouped by the SAME key (meeting
  // date, newest first).
  const groups=[];
  let nest=false;                 // year sections only once the library spans 2+ years
  let head='',split='';
  if(sort==='name'){
    head=headSectionsHTML(urgent,[],acts,nyv);
    const ordered=rows.slice().sort((a,b)=>
      (a.title||'').toLowerCase().localeCompare((b.title||'').toLowerCase())
      ||(b.date||'').localeCompare(a.date||''));
    for(const r of ordered){
      const key=nameBucket(r.title);
      if(!groups.length||groups[groups.length-1].key!==key)groups.push({key,rows:[]});
      groups[groups.length-1].rows.push(r);
    }
  }else{
    const sec=timelineSections(rows);
    const qopen=grpForced()||grpOpen(QUEUE_FOLD);
    const nowRows=nowRowsHTML(sec.now,qopen);
    const top=headSectionsHTML(urgent+upHTML+nowRows.html,sec.fin,acts,nyv);
    // the pinned group carries its own markup: the two sections, whole. The
    // queue fold's hidden rows ride it, so the selection can drop them.
    if(top)groups.push({key:'',pinned:true,rows:sec.now.concat(sec.fin),html:top,
      folded:nowRows.hidden});
    split=sec.now.length+':'+sec.fin.length+':'+(qopen?1:0);
    for(const r of sec.rest){
      const key=dateBucket(dateKey(r));
      const last=groups[groups.length-1];
      if(!last||last.pinned||last.key!==key)groups.push({key,rows:[]});
      groups[groups.length-1].rows.push(r);
    }
    // stamp each date group with its accordion identity, then decide whether
    // the library spans two or more years -- the ONLY condition under which
    // year sections appear at all
    nest=grpNest(grpStamp(groups));
  }
  LAST_GROUPS=groups;                 // the jump rail resolves its index here
  LAST_NEST=nest;                     // ...and plants a year key only when years exist

  const forced=grpForced();
  if(sort==='date')grpStates(groups,nest,forced);      // g.open / g.yearOpen, once
  const ordered=[].concat(...groups.map(g=>g.rows));   // flat, for the change signature
  // the fold state is part of the signature: a toggle changes what is in the
  // DOM without changing a single row, and the poll must not undo it. So is
  // the Now / To finish split: a meeting aging out of To finish moves zones.
  const gsig=sort!=='date'?'':groups.map(g=>g.pinned?'P'+split
    :g.id+':'+(g.open?1:0)+':'+(g.yearOpen?1:0)).join(',');
  const sig=JSON.stringify(ordered.map(sigOf))+'|'+sort+'|'+lfsig+'|'+q
    +'|'+uploadsSig()+'|'+(nest?1:0)+'|'+gsig+'|'+tsig+'|'+fsig;
  if(tl.dataset.sig===sig){drawRail(groups,q.length>0,sort);return;}  // nothing changed
  // never wipe a half-typed title or date (an inline rename in a row); a later
  // unchanged poll (or a blur) lets the rebuild happen. Only INPUT/SELECT
  // focus blocks it, so a merely focused row still refreshes.
  const ae=document.activeElement;
  if(ae&&tl.contains(ae)&&/^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName))return;
  tl.dataset.sig=sig;
  tl.innerHTML=said+(sort==='date'
    ?accordionHTML(groups,nest,forced)
    :head+upHTML+groups.map((g,i)=>
      `<div class="mgroup" id="grp-${i}">${esc(g.key)} &middot; ${g.rows.length}</div>`
      +g.rows.map(rowHTML).join('')).join(''))+setup;
  drawRail(groups,q.length>0,sort);
}

/* One header LINE per section: chevron, eyebrow, hairline rule, count in mono.
 * A collapsed section contributes its header and nothing else -- no rows in
 * the DOM -- and a collapsed YEAR swallows its month headers too, so a
 * five-year library reads as five lines. */
function ghead(id,label,count,cls,domId,open){
  return `<button class="ghead ${cls}" type="button" id="${esc(domId)}"
    aria-expanded="${open?'true':'false'}" onclick="grpToggle('${escJs(id)}')"
    ><span class="gchev" aria-hidden="true">${open?'&#9662;':'&#9656;'}</span
    ><span class="glabel">${esc(label)}</span
    ><span class="grule" aria-hidden="true"></span
    ><span class="gcount mono">${count}</span></button>`;
}
function accordionHTML(groups,nest,forced){
  let html='',lastYr=null;
  // drawTimeline stamps the open state; a direct caller gets it stamped here
  if(groups.some(g=>!g.pinned&&g.open===undefined))grpStates(groups,nest,forced);
  for(let i=0;i<groups.length;i++){
    const g=groups[i];
    // the pinned group: above every section, unfoldable. drawTimeline hands
    // it the Now and To finish sections whole (g.html); bare rows otherwise
    if(g.pinned){html+=g.html!=null?g.html:g.rows.map(rowHTML).join('');continue;}
    if(nest&&g.year&&g.year!==lastYr){
      lastYr=g.year;
      const yCount=groups.reduce((n,x)=>
        n+((!x.pinned&&x.year===g.year)?x.rows.length:0),0);
      html+=ghead('y-'+g.year,g.year,yCount,'yhead','ysec-'+g.year,g.yearOpen);
    }
    if(!g.yearOpen)continue;               // a folded year hides its months too
    html+=ghead(g.id,g.key,g.rows.length,
      'mgroup'+(nest&&g.year?' inyear':''),'grp-'+i,g.open);
    if(g.open)html+=g.rows.map(rowHTML).join('');
  }
  return html;
}

/* jump rail: regenerated from the ORDERED date groups (the pinned cluster has no
 * rail entry). By date: TODAY/YST first, then a year anchor before each year's
 * first month, months beneath. By name: the letter groups. 3+ groups, no search.
 * The rail lists EVERY month, folded or not: railJump unfolds what it lands on. */
let LAST_GROUPS=[],LAST_NEST=false;
function _railBtn(i,g,short){
  return `<button class="railbtn" type="button" onclick="railJump(${i})"
    title="Jump to ${esc(g.key)} (${g.rows.length})">${esc(short)}</button>`;
}
function drawRail(groups,hasSearch,sort){
  const rail=$('#rail');
  const railGroups=groups.filter(g=>!g.pinned);        // the pinned cluster is never in the rail
  if(railGroups.length<3||hasSearch){rail.hidden=true;rail.innerHTML='';return;}
  rail.hidden=false;
  let lastYr='';
  rail.innerHTML=groups.map((g,i)=>{
    if(g.pinned)return '';
    if(sort!=='date')return _railBtn(i,g,g.key);
    if(g.key===BUCKET.today)return _railBtn(i,g,'TDY');
    if(g.key===BUCKET.yesterday)return _railBtn(i,g,'YST');
    if(g.key===BUCKET.undated)return _railBtn(i,g,'NA');
    // a month group: anchor its year the first time that year appears. The
    // year is the one grpStamp already resolved, never re-parsed from the label
    const yr=g.year||'';
    let head='';
    if(yr&&yr!==lastYr){head=`<div class="railyr">${yr}</div>`;lastYr=yr;}
    return head+_railBtn(i,g,g.key.slice(0,3));
  }).join('');
}
/* A rail click must land on rows, not on a folded header: it opens the target
 * month AND the year holding it (persisting both, so the fold the user just
 * asked for survives the next poll), rebuilds, and only then scrolls. Under a
 * filter every section is open already, so nothing is stored; and a year key
 * is stored only when the library actually HAS year sections. */
function railJump(i){
  const g=LAST_GROUPS[i];
  if(g&&!g.pinned&&g.id){
    if(!grpForced()){
      if(LAST_NEST&&g.year)grpSet('y-'+g.year,true);
      grpSet(g.id,true);
    }
    if(S){drawTimeline(S);afterRender();}
  }
  const el=document.getElementById('grp-'+i);
  if(el&&paned())paneReveal(el,true);   // two panes: the list scrolls, the reader stays
  else if(el)el.scrollIntoView({behavior:'smooth',block:'start'});
}

/* ============================ render loop ============================ */
// A relabel rewrites the transcript the user is STARING AT, so that page is the
// last place a new name shows up. This used to watch the falling edge of one
// library-wide "a relabel is running" boolean: back-to-back passes kept it high
// so the edge never came, and even when it did the flag said nothing about
// whether THIS meeting had changed -- every finished pass rebuilt the page.
// Each timeline row now carries its own save stamp (generated_at, re-written by
// every transcript write), so the open page watches only its own row and
// reloads exactly when its own bytes moved.
function relabelWatch(s){
  if(!MP||!MP.built||!route||route.view!=='meeting'||route.base!==MP.base)return;
  const r=(s.timeline||[]).find(x=>x.id===MP.base);
  const st=r&&r.generated_at;
  if(!st||st===MP.stamp)return;
  // mid-edit: a rebuild here would throw away an open naming panel, an
  // inline edit card, a half-typed rename, or an unsaved transcript search
  // query (mReloadSegs clears the find box). The stamp is deliberately NOT
  // recorded, so the change is held, not lost: a later tick does the reload
  // (this runs every 2s, so "later" is 2 seconds).
  const fb=document.getElementById('mfind');
  if(NP||document.getElementById('mcard')
    ||(fb&&fb.value&&fb.value.trim())
    ||document.querySelector('.renameinput,.dateinput'))return;
  MP.stamp=st;
  mReloadSegs();
}
// While a meeting page is open the 2s poll keeps the pill and bulk regions
// live (drawPill/applySel) but must NOT rebuild the meeting document
// (guard on route); a pending deep-link build completes once S has arrived.
// relabelWatch runs FIRST, before that early return: the meeting view is the
// one place a finished relabel has to be picked up. With two panes the list
// beside the reader keeps drawing, and a list with no meeting in the reader
// opens the default one (readerSync).
function render(){if(!S)return;relabelWatch(S);mHeadSync(S);drawPill(S);drawRecOk(S);drawDrawer(S);
  drawProcessPop(S);   // an open Process popover refreshes in place with the poll
  drawChips(S);        // the chip row under the search field, and its counts
  if(route&&route.view==='meeting'){maybeBuildPending();if(!PANES){applySel();return;}}
  else if(PANES)readerSync();
  drawTimeline(S);afterRender();}
/* The row flash. A relabel rewrites transcripts the user is not looking at, and
 * in a list of a hundred rows a name quietly changing three cards down is
 * invisible. Every row carries its own save stamp (generated_at), so a poll can
 * name exactly which meetings were rewritten since the last one and tint those
 * cards for two seconds.
 * It reads the PREVIOUS state object and applies a class AFTER the rebuild, so
 * it never enters drawTimeline's change signature: a flash must not be able to
 * force a rebuild, and a row that merely appeared (no previous stamp to move
 * from) is not an update. */
const FLASH_MS=2000;
function stampsMoved(prev,next){
  if(!prev)return [];
  const was=new Map((prev.timeline||[]).map(r=>[r.id,r.generated_at]));
  const out=[];
  for(const r of (next&&next.timeline)||[]){
    const before=was.get(r.id);
    if(before&&r.generated_at&&r.generated_at!==before)out.push(r.id);
  }
  return out;
}
function flashRows(ids){
  if(!ids||!ids.length)return;
  const want=new Set(ids);
  document.querySelectorAll('.row[data-id], .lrow[data-id]').forEach(el=>{
    if(!want.has(el.dataset.id))return;
    el.classList.add('updated');
    setTimeout(()=>{el.classList.remove('updated');},FLASH_MS);
  });
}
/* The tab title. A meeting that joins To finish while the panel sits in a
 * background tab says so there: "(1) Meetings", counting every meeting that
 * joined since the tab was hidden. The count resets the moment the tab is
 * visible again. The first poll only sets the baseline, so opening the panel
 * in a background tab never counts what was already waiting. */
let TAB_SEEN=null;
const TAB_JOINED=new Set();
function finishIds(timeline,now){
  return (timeline||[]).filter(r=>toFinish(r,now)).map(r=>r.id);
}
function tabJoins(prev,next){
  if(!prev)return [];
  const was=new Set(prev);
  return (next||[]).filter(id=>!was.has(id));
}
function tabTitle(n){return n>0?'('+n+') Meetings':'Meetings';}
function tabWatch(s){
  const ids=finishIds(s&&s.timeline);
  if(document.hidden)for(const id of tabJoins(TAB_SEEN,ids))TAB_JOINED.add(id);
  TAB_SEEN=ids;
  document.title=tabTitle(TAB_JOINED.size);
}
document.addEventListener('visibilitychange',()=>{
  if(document.hidden)return;
  TAB_JOINED.clear();document.title=tabTitle(0);
});
// the last look, for the menu bar's "new since last look" count: a POST
// while the panel is in front of the owner, at most once a minute (or at
// once when the window comes back). A hidden tab never counts as a look.
let SEEN_AT=0;
function markSeen(now){
  if(document.hidden)return;
  const t=Date.now();
  if(!now&&t-SEEN_AT<60000)return;
  SEEN_AT=t;
  api('/api/seen',{}).catch(()=>{});
}
document.addEventListener('visibilitychange',()=>markSeen(true));
async function refresh(){try{
  const prev=S;
  S=await api('/api/state');
  tabWatch(S);
  markSeen(false);
  render();
  // the list view only: the meeting page answers a moved stamp by reloading
  // itself (relabelWatch), which is a louder signal than a tint. Two panes
  // show the list beside it, so its rows flash there too.
  if(!route||route.view!=='meeting'||PANES)flashRows(stampsMoved(prev,S));
  readerKeep();
}catch(e){}}

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
applyTheme(themeNow());
$('#search').oninput=()=>{render();scheduleSearch();};
// ready-row click-to-expand: clicking the row body toggles the summary peek;
// its controls (buttons, links, checkbox, category dot, inputs, the review
// count, the click-to-edit title and date) never do, and neither does a
// click that is selecting text
// Two panes: a click on a meeting row (a To finish link included) selects it
// into the reader instead, and on the reader's own row wakes its finish flow.
// A modified click on a To finish link keeps the browser's own (a new tab).
$('#timeline').addEventListener('click',e=>{
  if(PANES){
    const el=e.target.closest('.row, .lrow');
    const own=el&&el.classList.contains('lrow');
    if(el&&rowOpens(el.dataset.state)&&!(own&&(e.metaKey||e.ctrlKey||e.shiftKey||e.button))
       &&!e.target.closest(own?'button,input':'button,a,input,select,textarea,label,.rev,.rname,.rdate')
       &&!(window.getSelection&&String(window.getSelection()))){
      e.preventDefault();readerOpen(el.dataset.id);
    }
    return;
  }
  const row=e.target.closest('.row');
  if(!row||row.dataset.state!=='ready')return;
  if(e.target.closest('button,a,input,select,textarea,label,.rev,.rname,.rdate'))return;
  if(window.getSelection&&String(window.getSelection()))return;
  toggleExpand(row.dataset.id);
});
$('#themeDot').onclick=cycleTheme;
$('#gear').onclick=()=>openDrawer();   // the settings drawer (Builder D): no old-page bridge remains
$('#pill').onclick=toggleProcess;
$('#processBtn').onclick=toggleProcess;
refresh();
setInterval(refresh,2000);

/* ============================================================================
   Builder B: interactions. Wires every seam A stubbed, against the EXISTING
   endpoints only (no server change). The edit layer (Builder C, below) removed
   the last per-meeting bridges to the old page: opening, reviewing, and voice
   naming all happen in the shell now (#m route, the inline flag stepper, the
   naming slide-over). The old page keeps its deep-link params for its own use.
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
  const r=anchor.getBoundingClientRect();
  const vw=document.documentElement.clientWidth,vh=document.documentElement.clientHeight;
  const w=el.offsetWidth||260,h=el.offsetHeight||0;
  // horizontal: right-align to the anchor, clamped inside the viewport
  let left=r.right-w;
  const maxL=vw-w-8;
  if(left>maxL)left=maxL; if(left<8)left=8;
  // vertical: below the anchor by default; if its bottom would fall past the
  // viewport, flip up and anchor above the row instead (clamped to the top edge)
  let top=r.bottom+6;
  if(top+h>vh)top=Math.max(8,r.top-6-h);
  el.style.left=(window.scrollX+left)+'px';
  el.style.top=(window.scrollY+top)+'px';
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
    _bulkPop=null;   // a selection-scoped confirm dies with its popover
    if(el.id==='processPop')$('#processBtn').setAttribute('aria-expanded','false');
    if(el.id==='rowmenu'){el.dataset.rowid='';el.dataset.page='';rowActing(null);}
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
  openPop(pop,$('#processBtn'),()=>{
    pop.dataset.sig=ppSig(S||{});   // baseline for the poll's drawProcessPop
    fillProcessPop(pop);
  },'#pill,#processBtn');
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
  // runs requested while another held the lock (redos, hand-picked files):
  // they wait in the job queue and start on their own; the x cancels one
  const qjobs=s.queued_jobs||[];
  if(qjobs.length){
    h+=`<div class="ppsep"></div><div class="pphdr">Queued runs</div>`;
    h+=qjobs.map(j=>`<div class="ppqrow">
      <span class="ppql">&#8635; ${esc(j.label)}</span>
      <span class="ppqs">&middot; ${running?'starts after the current run':'starting&#8230;'}</span>
      <button class="ppqx" type="button" title="Cancel this queued run"
        onclick="ppUnqueue(${Number(j.at)||0})">&#10005;</button>
    </div>`).join('');
  }
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
// cancel a queued run: optimistic removal (the row and the pill count drop
// NOW), then the POST; if it could not be cancelled (it already started) the
// next poll simply brings the truth back
function ppUnqueue(at){
  if(S)S.queued_jobs=(S.queued_jobs||[]).filter(j=>j.at!==at);
  const pop=$('#processPop');
  if(pop.dataset.open){pop.dataset.sig=ppSig(S||{});fillProcessPop(pop);}
  if(S)drawPill(S);
  api('/api/unqueue',{at}).then(refresh);
}
// the popover refreshes with the 2s poll while open (the drawer's signature
// pattern): rebuild only when the state it renders changed, never under a
// focused control, and never by closing it
function ppSig(s){
  const waiting=(s.timeline||[]).filter(r=>r.state==='waiting').length;
  const qsel=[...SEL].map(rowById).filter(r=>r&&(r.state==='waiting'||r.state==='held')).length;
  return JSON.stringify([waiting,qsel,!!s.running,!!s.paused,
    (s.queued_jobs||[]).map(j=>[j.at,j.label])]);
}
function drawProcessPop(s){
  const pop=$('#processPop');
  if(!pop||!pop.dataset.open)return;
  const sig=ppSig(s||{});
  if(pop.dataset.sig===sig)return;
  if(dFocusGuard(pop))return;   // a focused run-option checkbox blocks the swap
  pop.dataset.sig=sig;
  fillProcessPop(pop);
}

/* ---------------------------------------------- tray items + filters ------ */
// "Review flags" (the To finish eyebrow) presses or clears the Has flags chip:
// only ready rows with lines to check show (Now and To finish stay)
function flaggedToggle(){chipGo('flags');}
async function trayAct(kind,target){
  if(kind==='recorder_note'||kind==='recorder_stall'){
    // Fix permissions, on the failed recorder note or a live stall: an
    // in-flight label, then the outcome in the row's own detail text. Both
    // are STATE (NOW_MSG), because the row lives in the list now and the
    // list's 2s rebuild would wipe anything written into the DOM
    const at=kind==='recorder_note'?((S&&S.recorder_note)||{}).at:target;
    NOW_MSG[kind]={at,label:'Resetting…',busy:true,detail:''};nowRedraw();
    let r;
    try{r=await api('/api/fix_recorder_permissions',{});}
    catch(e){r={ok:false,error:'Could not reach the server.'};}
    if(r&&r.ok&&kind==='recorder_stall')delete NOW_MSG[kind];   // the poll says if it cleared
    else NOW_MSG[kind]=r&&r.ok
      ?{at,label:'Done ✓',busy:true,detail:r.message||'Permissions reset.'}
      :{at,label:'Retry',busy:false,detail:(r&&r.error)||'permission reset failed'};
    nowRedraw();
    if(kind==='recorder_stall')refresh();
    return;
  }
  if(kind==='dupe_meetings'){openDrawer('dupes');return;}
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
  if(!row){stopClip();return;}                      // its row left the list, or folded
                                                    // away: stop, never play on blind
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
function rowDelete(id,ev){                // two-step confirm in a popover, no browser dialog
  if(ev)ev.stopPropagation();
  const r=rowById(id);if(!r||!r.source_file)return;
  const anchor=(ev&&ev.currentTarget)
    ||document.querySelector('.row[data-id="'+CSS.escape(id)+'"] .rslot .iact:last-child');
  const pop=$('#rowmenu');pop.dataset.rowid=id;
  openPop(pop,anchor,()=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Delete this recording?</div>
      <div class="ppcnote">The audio file is removed and never becomes a meeting. It cannot be undone.</div>
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn danger mini" type="button" onclick="rowDeleteGo('${escJs(id)}')">Delete</button>
      </div></div>`;
  });
  rowActing(id);
}
async function rowDeleteGo(id){
  const r=rowById(id);if(!r||!r.source_file){closePop();return;}
  if(clipKey===id)stopClip();
  const res=await api('/api/queue_delete',{name:r.source_file,confirm:true});
  const pop=$('#rowmenu');
  if(!res.ok){                            // failure stays in the popover, never a browser alert
    if(!pop.hidden)pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Could not delete</div>
      <div class="ppcnote err">${esc(res.error||'The recording could not be deleted.')}</div>
      <div class="ppcrow"><button class="btn mini" type="button" onclick="closePop()">Close</button></div>
    </div>`;
    return;
  }
  closePop();refresh();
}
async function failDismiss(id){           // a failure whose source file is gone
  const r=rowById(id);if(!r||!r.source_file)return;
  const res=await api('/api/dismiss_failure',{name:r.source_file});
  if(res&&res.ok)refresh();
}
function rowRetry(id){                    // re-run a failed source still in the folder
  const r=rowById(id);if(!r||!r.source_file)return;
  api('/api/run',{files:[r.source_file],...runOpts()}).then(refresh);
}
// in-shell route: set the hash and let the hashchange handler open the page
// (Builder A, at the bottom). No navigation away; back/forward and deep links work.
function openMeeting(id){location.hash='#m/'+encodeURIComponent(id);}
// a row's review count: open the meeting page with the
// flag stepper active on the first flag (the same pendingOpen pattern as the
// ask focus and the search-hit seek: a flag the build consumes, never a timeout)
function openReviewBadge(id){
  if(route.view==='meeting'&&route.base===id){reviewStart(0);return;}
  pendingOpen={base:id,review:true};
  location.hash='#m/'+encodeURIComponent(id);
}
function cycleCat(ev,id){                 // untagged -> work -> personal -> untagged
  if(ev)ev.stopPropagation();
  const r=rowById(id);if(!r)return;
  const cur=(r.category&&r.category!=='none')?r.category:'';
  const next={'':'work',work:'personal',personal:''}[cur];
  r.category=next;render();               // optimistic; the dot flips before the poll
  api('/api/set_category',{base:id,category:next}).then(refresh);
}
/* ----------------------------------------------------------- row menu ------ */
// the row whose menu / delete confirm is open keeps its hover actions in the
// flow (.acting): the pointer is inside the popover, so :hover alone would
// drop them, reflowing the row and zeroing the open menu's anchor rect
function rowActing(id){
  document.querySelectorAll('#timeline .row.acting').forEach(r=>r.classList.remove('acting'));
  if(!id)return;
  const el=document.querySelector('#timeline .row[data-id="'+CSS.escape(id)+'"]');
  if(el)el.classList.add('acting');
}
function rowMenu(id,ev){
  if(ev)ev.stopPropagation();
  const anchor=(ev&&ev.currentTarget)||document.querySelector('.row[data-id="'+CSS.escape(id)+'"] .rslot .iact:last-child');
  const pop=$('#rowmenu');pop.dataset.rowid=id;
  openPop(pop,anchor,()=>fillRowMenu(pop,id));
  rowActing(id);
}
function fillRowMenu(pop,id){
  const m=meetingByBase(id)||{};
  pop.innerHTML=`
    <button class="ppitem" type="button" onclick="rmAsk('${escJs(id)}')">Ask a question</button>
    <div class="ppsep"></div>
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
  let r;
  try{r=await api('/api/export',{base:id,fmt});}
  catch(e){r={error:'Export failed.'};}
  btn.disabled=false;
  if(r.ok){btn.textContent='Done ✓';setTimeout(closePop,650);return;}
  btn.textContent=t;
  // failure stays in the menu (e.g. pandoc missing for Word), never an alert
  const pop=$('#rowmenu');
  if(!pop.hidden)pop.innerHTML=`<div class="ppconfirm">
    <div class="ppctitle">Could not export</div>
    <div class="ppcnote err">${esc(r.error||'The export failed.')}</div>
    <div class="ppcrow"><button class="btn mini" type="button" onclick="closePop()">Close</button></div>
  </div>`;
}
async function rmCopy(id,btn){
  try{
    const txt=await fetch('/api/txt?base='+encodeURIComponent(id)).then(r=>r.text());
    await navigator.clipboard.writeText(txt);
    btn.textContent='Copied ✓';
  }catch(e){btn.textContent='Copy failed';}
}
// Ask a question: open the meeting page and land focused in its ask input.
// pendingOpen is the focus-after-build flag buildMeeting consumes (no timeouts).
function rmAsk(id){
  closePop();
  if(route.view==='meeting'&&route.base===id){const q=$('#maskq');if(q)q.focus();return;}
  pendingOpen={base:id,ask:true};
  location.hash='#m/'+encodeURIComponent(id);
}
function rmReveal(id){api('/api/export',{base:id,fmt:'reveal'});closePop();}
function rmArchive(id){api('/api/archive_meeting',{base:id}).then(()=>{closePop();
  // archived from its own page: the page is gone, return to the library
  // (two panes: the reader opens the default meeting, never an empty pane)
  if(route.view==='meeting'&&route.base===id)readerLeave(id);
  archHint();   // the quiet "archived · view" note pointing at the drawer's Archive
  refresh();});}
function rmRename(id,inRow){              // inline: the title becomes an input,
  closePop();                             // on the row OR on the meeting page
  // two panes show both at once: the list row's own title renames the row
  const onPage=!inRow&&route.view==='meeting'&&route.base===id;
  const titleEl=onPage
    ?document.querySelector('#meetingpage .mtitle')
    :(r=>r&&r.querySelector('.rtitle'))(document.querySelector('.row[data-id="'+CSS.escape(id)+'"]'));
  if(!titleEl||titleEl.querySelector('.renameinput'))return;   // already editing
  const cur=(meetingByBase(id)||{}).title||titleEl.textContent||'';
  titleEl.innerHTML=`<input class="renameinput" type="text" value="${esc(cur)}" aria-label="Rename meeting">`;
  const inp=titleEl.querySelector('input');inp.focus();inp.select();
  let done=false,moved=null;
  const finish=async save=>{
    if(done)return;done=true;
    const nm=inp.value.trim();
    let saved=false;
    if(save&&nm&&nm!==cur){
      const r=await api('/api/rename',{base:id,new:nm});
      if(!r.ok){                          // keep the field open, surface the error inline (no alert)
        done=false;
        let err=titleEl.querySelector('.nameerr');
        if(!err){err=document.createElement('div');err.className='nameerr';titleEl.appendChild(err);}
        err.textContent=r.error||'Rename failed';
        inp.focus();
        return;
      }
      saved=true;
      // a rename re-stamps the folder, so the base CHANGES: re-route the open
      // page to the new base (the hashchange handler rebuilds it there)
      if(onPage&&r.base&&r.base!==id){location.hash='#m/'+encodeURIComponent(r.base);return;}
      moved=r.base;
      const r0=rowById(id);if(r0)r0.title=nm;          // optimistic, pre-poll
      const m0=meetingByBase(id);if(m0)m0.title=nm;
    }
    if(onPage)titleEl.textContent=saved?nm:cur;   // cancelled / unchanged: restore
    else{inp.blur();redrawRows();readerFollow(id,moved);}   // the .rname span returns
    refresh();
  };
  inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();finish(true);}else if(e.key==='Escape'){e.preventDefault();finish(false);}};
  inp.onblur=()=>finish(true);
}
// click-to-edit on ready rows (the old page's quick path, restored): clicking
// the TITLE reuses the inline rename above; clicking the DATE in the meta line
// swaps it for a date input. Both stop the click so the row never expands
// (the timeline guard also excludes .rname/.rdate), and both survive the 2s
// poll through drawTimeline's focused-input guard.
// Two panes: the first click on a row selects it into the reader, and its
// title and date edit in place once the reader shows it (listPick). The
// reader's own header title renames through here too, on the page.
function rowTitleEdit(ev,id){if(ev)ev.stopPropagation();
  const inList=!!(ev&&ev.target&&ev.target.closest&&ev.target.closest('#timeline'));
  if(inList&&listPick(id))return;
  rmRename(id,inList);}
function rowDateEdit(ev,id){
  if(ev)ev.stopPropagation();
  if(listPick(id))return;
  const rowEl=document.querySelector('.row[data-id="'+CSS.escape(id)+'"]');
  const span=rowEl&&rowEl.querySelector('.rdate');
  if(!span||rowEl.querySelector('.dateinput'))return;    // already editing
  const cur=(rowById(id)||{}).date||'';
  span.outerHTML=`<input type="date" class="dateinput" value="${esc(cur)}" aria-label="Meeting date">`;
  const inp=rowEl.querySelector('.dateinput');
  if(!inp)return;
  inp.focus();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;
    const d=inp.value;
    if(save&&d&&d!==cur){
      const r=await api('/api/set_date',{base:id,date:d});
      if(!r.ok){                          // inline on the row, never an alert
        done=false;
        const live=document.querySelector('.row[data-id="'+CSS.escape(id)+'"] .rbody')||rowEl;
        let err=live.querySelector('.nameerr');
        if(!err){err=document.createElement('div');err.className='nameerr';live.appendChild(err);}
        err.textContent=r.error||'Date not saved';
        inp.focus();
        return;
      }
      // the folder re-stamps server-side (the response carries the new base);
      // optimistically move the date so the row regroups NOW, and let the
      // refresh right below bring the re-stamped id
      const r0=rowById(id);if(r0)r0.date=d;
      readerFollow(id,r.base);        // two panes: the reader moves with it
    }
    inp.blur();redrawRows();refresh();
  };
  inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();finish(true);}else if(e.key==='Escape'){e.preventDefault();finish(false);}};
  inp.onblur=()=>finish(true);
}
// force the next paint to rebuild the rows: an inline edit swapped a span for
// an input, and the change signature alone would never notice putting it back
function redrawRows(){
  const tl=$('#timeline');if(!tl)return;
  tl.dataset.sig='';
  if(S&&(route.view!=='meeting'||PANES)){drawTimeline(S);afterRender();}
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
  const pop=$('#rowmenu');
  if(!r.ok){                              // failure stays in the popover, never a browser alert
    if(!pop.hidden)pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Could not delete</div>
      <div class="ppcnote err">${esc(r.error||'The meeting could not be deleted.')}</div>
      <div class="ppcrow"><button class="btn mini" type="button" onclick="closePop()">Close</button></div>
    </div>`;
    return;
  }
  closePop();
  // deleted from its own page: the page is gone, return to the library
  // (two panes: the reader opens the default meeting, never an empty pane)
  if(route.view==='meeting'&&route.base===id)readerLeave(id);
  refresh();
}

/* --------------------------------------------------- bulk selection -------- *
 * One Set keyed by row id, reapplied after render. The bulk bar (meeting ops)
 * shows for selected READY rows; "Process selected" (above) consumes the
 * selected waiting/held rows. */
let SEL=new Set(),selAnchor=null;
function toggleSel(id,on){
  const ev=window.event;
  // shift-click range in DOM order. Rows inside a folded section are not in
  // the DOM, so the range excludes them BY DESIGN: they are not shown.
  if(ev&&ev.shiftKey&&selAnchor&&selAnchor!==id){
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
  // ...and prune what a FOLD hides. A row inside a collapsed month or year is
  // not in the DOM at all, so keeping it selected would make "N selected"
  // disagree with the checked boxes and let a bulk delete reach a row nobody
  // can see. A search or category filter is different: it narrows the view on
  // purpose and the selection survives it, as it always has.
  const folded=new Set();
  for(const g of LAST_GROUPS){if(g.pinned){(g.folded||[]).forEach(id=>folded.add(id));continue;}
    if(g.open===false||g.yearOpen===false)g.rows.forEach(r=>folded.add(r.id));}
  [...SEL].forEach(id=>{if(!live.has(id)||folded.has(id))SEL.delete(id);});
  document.querySelectorAll('#timeline .row .chk').forEach(c=>{
    c.checked=SEL.has(c.closest('.row').dataset.id);});
  document.body.classList.toggle('has-sel',SEL.size>0);
  drawBulkBar();
}
function _selReady(){return [...SEL].filter(id=>{const r=rowById(id);return r&&r.state==='ready';});}
function drawBulkBar(){
  const bar=$('#bulkbar'),bases=_selReady();
  if(!bases.length){bar.hidden=true;bar.dataset.sig='';bar.innerHTML='';return;}
  bar.hidden=false;
  // only rebuild when the count actually changed: an unconditional rebuild
  // detached the very button an open confirm popover is anchored to, on every
  // 2s poll, and the popover then had no geometry to re-anchor against
  const sig=String(bases.length);
  if(bar.dataset.sig===sig)return;
  bar.dataset.sig=sig;
  bar.innerHTML=`<span class="bcount">${bases.length} selected</span>
    <button class="btn mini" type="button" onclick="bulk('category','work')">Work</button>
    <button class="btn mini" type="button" onclick="bulk('category','personal')">Personal</button>
    <button class="btn mini" type="button" onclick="bulk('category','')">Clear tag</button>
    <button class="btn mini" type="button" onclick="bulkRename(this)">Rename&#8230;</button>
    <button class="btn mini" type="button" onclick="bulkDate(this)">Set date&#8230;</button>
    <button class="btn mini" type="button" onclick="bulk('archive')">Archive</button>
    <button class="btn mini" type="button" onclick="bulkDropAudio(this)">Delete audio&#8230;</button>
    <button class="btn danger mini" type="button" onclick="bulkDelete(this)">Delete&#8230;</button>
    <span class="grow"></span>
    <button class="btn mini" type="button" onclick="selAllShown()">Select all shown</button>
    <button class="btn mini" type="button" title="Clear selection" onclick="selClear()">&#10005;</button>`;
}
async function bulk(action,value,extra){
  const bases=_selReady();
  if(!bases.length)return;
  let r=null;
  try{r=await api('/api/bulk',{bases,action,value,...(extra||{})});}
  catch(e){r=null;}
  if(!r){
    // the call itself failed (server gone, connection dropped). It used to
    // surface only as an unhandled rejection in the console: the popover
    // closed, nothing else appeared, and the selection never cleared. Say so,
    // and KEEP the selection so the action can simply be tried again.
    bulkResult('Could not reach the server',
      'Nothing was reported as done. The selection is still here, so you can try again.',true);
    return;
  }
  const fails=(r.results||[]).filter(x=>!x.ok);
  const notes=fails.slice(0,8).map(f=>esc(f.base)+': '+esc(f.error||'failed'));
  // the server caps a bulk action at 500 items and now SAYS how many it
  // dropped; a silently lost tail read exactly like an item never selected
  if(r.dropped)notes.push(esc(r.dropped+' were over the 500-item limit and were not attempted.'));
  if(!r.results&&r.error)notes.push(esc(r.error));
  if(notes.length)
    bulkResult(`${fails.length+(r.dropped||0)} of ${bases.length} could not be done`,
      notes.join('<br>'),true);
  else if(r.freed_mb)
    bulkResult('Done',`Freed ${r.freed_mb} MB of audio.`,false);
  if(action==='archive'&&!fails.length)archHint();   // quiet pointer to the drawer's Archive
  SEL.clear();refresh();
}
/* a bulk confirm states a COUNT ("Delete 2 meetings?"). The 2s poll can change
 * the selection underneath it (a row left the library, a meeting was archived
 * elsewhere), and afterRender's reposition-or-close check only ever fired on a
 * truthy rowid, which a bulk popover never sets. So the popover could name one
 * count while bulkDeleteGo sent another. It now re-reads the live selection on
 * every render: repaint at a new count, close outright at zero. Popovers
 * carrying a typed value (rename, set date) pass repaint:false so a poll never
 * wipes the half-typed field; they still close when the selection empties. */
let _bulkPop=null;      // {fill, anchor, n, repaint} while such a popover is open
function openBulkPop(anchor,fill,repaint){
  const pop=$('#rowmenu');pop.dataset.rowid='';
  _bulkPop={fill,anchor,n:_selReady().length,repaint:repaint!==false};
  openPop(pop,anchor,()=>fill(pop,_bulkPop.n));
  return pop;
}
function bulkPopSync(){
  const pop=$('#rowmenu');
  if(!_bulkPop||!pop||!pop.dataset.open)return;
  const n=_selReady().length;
  if(!n){closePop();return;}                     // nothing left to act on
  if(n!==_bulkPop.n&&_bulkPop.repaint){
    _bulkPop.n=n;
    _bulkPop.fill(pop,n);
  }else if(n!==_bulkPop.n){_bulkPop.n=n;}
  // the bar rebuilds when the count changes, so the anchor button can be
  // detached by now; the bar itself always survives
  const a=(_bulkPop.anchor&&_bulkPop.anchor.isConnected)?_bulkPop.anchor:$('#bulkbar');
  if(a)_posPop(pop,a);
}
// bulk outcome as a popover off the bulk bar (never a browser alert or banner)
function bulkResult(title,note,isErr){
  const pop=$('#rowmenu');pop.dataset.rowid='';
  openPop(pop,$('#bulkbar'),()=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">${esc(title)}</div>
      <div class="ppcnote${isErr?' err':''}">${note}</div>
      <div class="ppcrow"><button class="btn mini" type="button" onclick="closePop()">OK</button></div>
    </div>`;
  });
}
function bulkRename(btn){                  // name input in a popover, never a native prompt
  if(!_selReady().length)return;
  const pop=openBulkPop(btn,(pop,n)=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Rename ${n} selected</div>
      <div class="ppcnote">One name for all of them. Each keeps its own date in the filename, so recurring meetings stay separate folders.</div>
      <input class="ppinput" type="text" placeholder="New name" aria-label="New name for all selected">
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn primary mini" type="button" onclick="bulkRenameGo()">Rename</button>
      </div></div>`;
  },false);
  const inp=pop.querySelector('.ppinput');
  if(inp){inp.focus();inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();bulkRenameGo();}};}
}
function bulkRenameGo(){
  const inp=$('#rowmenu .ppinput'),nm=inp?inp.value.trim():'';
  if(!nm)return;
  closePop();bulk('rename',nm);
}
function bulkDate(btn){                     // date input in a popover, never a native prompt
  if(!_selReady().length)return;
  const pop=openBulkPop(btn,(pop,n)=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Set the date for ${n} selected</div>
      <div class="ppcnote">The folder name is re-stamped to match. Each keeps its own name.</div>
      <input class="ppinput" type="date" aria-label="New date for all selected">
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn primary mini" type="button" onclick="bulkDateGo()">Set date</button>
      </div></div>`;
  },false);
  const inp=pop.querySelector('.ppinput');
  if(inp){inp.focus();inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();bulkDateGo();}};}
}
function bulkDateGo(){
  const inp=$('#rowmenu .ppinput'),d=inp?inp.value.trim():'';
  if(!d)return;
  closePop();bulk('date',d);
}
function bulkDropAudio(btn){                // confirm popover, never a native dialog
  if(!_selReady().length)return;
  openBulkPop(btn,(pop,n)=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Delete stored audio for ${n} meeting${n>1?'s':''}?</div>
      <div class="ppcnote">The transcripts are kept. This frees most of the space, but it cannot be undone.</div>
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn danger mini" type="button" onclick="bulkDropAudioGo()">Delete audio</button>
      </div></div>`;
  });
}
function bulkDropAudioGo(){closePop();bulk('drop_audio',null,{confirm:true});}
function bulkDelete(btn){                   // two-step confirm popover, never a native dialog
  if(!_selReady().length)return;
  openBulkPop(btn,(pop,n)=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Delete ${n} meeting${n>1?'s':''}?</div>
      <div class="ppcnote">This removes the transcript, audio, and every cache. It cannot be undone.</div>
      <div class="ppcnote">If you might want ${n>1?'them':'it'} back, Archive instead keeps everything restorable.</div>
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn mini" type="button" onclick="bulkArchiveInstead()">Archive instead</button>
        <button class="btn danger mini" type="button" onclick="bulkDeleteGo()">Delete forever</button>
      </div></div>`;
  });
}
function bulkArchiveInstead(){closePop();bulk('archive');}
function bulkDeleteGo(){closePop();bulk('delete',null,{confirm:true});}
function selClear(){SEL.clear();applySel();}
function selAllShown(){
  const ids=[...document.querySelectorAll('#timeline .row[data-state="ready"] .chk')].map(c=>c.closest('.row').dataset.id);
  const all=ids.length&&ids.every(id=>SEL.has(id));
  ids.forEach(id=>all?SEL.delete(id):SEL.add(id));
  applySel();
}

/* --------------------------------------------------------------- search ---- *
 * Debounced full-text (>=3 chars) into HITS; drawTimeline renders them as the
 * "Said in" group at the top of the list (docs/ui-refresh phase 4). A hit
 * opens the in-shell meeting page (#m/<base>) and seeks the audio to the
 * hit's moment via mSeek once the page is built; with two panes it opens in
 * the reader beside the list. The client-side title filter is A's, on #search. */
// a hit carries its start time in seconds (the old page passed the segment
// index to openTranscript; the new page's mSeek takes the time directly)
function openHit(base,t){
  if(route.view==='meeting'&&route.base===base){mSeek(t);return;}
  pendingOpen={base,seek:t};
  location.hash='#m/'+encodeURIComponent(base);
}
let searchTimer=null;
function scheduleSearch(){
  clearTimeout(searchTimer);
  const q=$('#search').value.trim();
  if(q.length<3){if(HITS){HITS=null;render();}return;}
  searchTimer=setTimeout(async()=>{
    let r;
    try{r=await api('/api/search?q='+encodeURIComponent(q));}catch(e){return;}
    if(!r||$('#search').value.trim().toLowerCase()!==r.query)return;   // a stale response
    HITS=r;render();
  },250);
}

/* ---------------------------------------------------- post-render hook ----- *
 * Called last by render(). Keeps the clip player mounted, the selection in sync,
 * and an open per-row menu anchored to its (possibly rebuilt) row. */
function afterRender(){
  mountClip();
  applySel();
  bulkPopSync();     // an open bulk confirm re-reads the live selection
  kbRestore();
  selAfter();        // two panes: the reader's row wears --sel again
  const rm=$('#rowmenu');
  // a menu opened from the reader's header stays there (two panes rebuild the
  // list beside it on every poll, and its row may sit in a folded month)
  if(rm.dataset.open&&rm.dataset.rowid&&!rm.dataset.page){
    rowActing(rm.dataset.rowid);   // the rebuilt row regains .acting first, so
                                   // its action cluster has geometry to anchor to
    const anchor=document.querySelector('.row[data-id="'+CSS.escape(rm.dataset.rowid)+'"] .rslot .iact:last-child');
    if(anchor)_posPop(rm,anchor);else closePop();
  }
}

/* ============================================================================
   Builder A: THE MEETING PAGE. A hash route #m/<base> opens ONE scrollable
   document in place of the timeline -- header, docked audio, summary, flagged
   strip, transcript, pinned ask bar -- replacing the old page's Read modal,
   Summary modal, and Ask flyout. The reader (segment render, audio seek +
   playing-line highlight, in-page find) and the ask thread are ported from the
   old app.js with the same endpoint semantics; naming/review/editing land in
   the next pass (seam: reviewStep, on the flagged strip).
   ============================================================================ */

// bright per-speaker palette, reused verbatim from the old reader's sdot colors
const HUES=['#0071e3','#34c759','#ff9f0a','#ff375f','#bf5af2','#64d2ff','#ffd60a','#ac8e68'];

let route={view:'timeline',base:null};   // desired view, derived from the hash
let savedScroll=0;                        // timeline scroll, restored on return
let MP=null;                              // meeting page + audio player state
let MF=null;                              // find-in-transcript state {q,rx,hits,cur}
let mFindT=null;
const MRATES=[1,1.25,1.5,2];
const askThreads={};                      // base -> {hist:[{q,a,err}], busy}; dies with the page
let pendingOpen=null;                     // {base, seek?:secs, ask?:true} -- one deep action
                                          // (search-hit seek / row-menu ask) consumed by the
                                          // next buildMeeting; a flag, never a timeout

function parseHash(){
  const h=location.hash||'';
  const m=h.match(/^#m\/(.+)$/);
  return m?{view:'meeting',base:decodeURIComponent(m[1])}:{view:'timeline',base:null};
}
function applyRoute(){
  if(NP)closeNamePanel();          // a route change closes the naming slide-over
  dupeDisarm();                    // and disarms the no-undo duplicate sweep
  if(route.view==='meeting')enterMeeting(route.base);
  else if(!readerSync())exitMeeting();   // two panes: no hash opens the default meeting
  selFollow();                     // two panes: the list's selection follows the hash
}
function _instantScroll(y){
  const el=document.documentElement,prev=el.style.scrollBehavior;
  el.style.scrollBehavior='auto';window.scrollTo(0,y||0);el.style.scrollBehavior=prev;
}
function enterMeeting(base){
  const already=document.body.classList.contains('route-meeting');
  if(!already)savedScroll=window.scrollY;   // capture the list position once, on entry
  document.body.classList.add('route-meeting');
  $('#meetingpage').hidden=false;
  closePop();stopClip();
  document.querySelectorAll('#timeline audio').forEach(a=>{try{a.pause();}catch(e){}});
  buildMeeting(base);
  _instantScroll(0);
}
function exitMeeting(){
  if(!document.body.classList.contains('route-meeting'))return;
  teardownMeeting();
  document.body.classList.remove('route-meeting');
  const p=$('#meetingpage');p.hidden=true;p.innerHTML='';
  if(S)drawTimeline(S);                      // repaint the (possibly changed) list
  _instantScroll(savedScroll);
}
function teardownMeeting(){
  if(MP){
    if(MP.audio){try{MP.audio.pause();}catch(e){}MP.audio.src='';MP.audio=null;}
    if(MP.tick){clearInterval(MP.tick);MP.tick=null;}
  }
  window.removeEventListener('scroll',mOnScroll);
  document.removeEventListener('keydown',mKey);
  MF=null;MP=null;MR=null;FS=null;
}
// called by the poll's render() while a meeting is open: finish a deep-link build
// that was waiting for S, without ever rebuilding an already-built page
function maybeBuildPending(){
  if(route.view!=='meeting')return;
  if(!MP||MP.base!==route.base||MP.pending)buildMeeting(route.base);
}
function mBack(ev){if(ev)ev.preventDefault();location.hash='';}   // fires hashchange -> timeline

/* ============================================================================
   TWO PANES (docs/ui-refresh/proposal.md, phase 3). At 900px and wider the
   list keeps the left pane and the meeting page above mounts beside it, in
   the reader, instead of replacing it. The #m/<base> route is unchanged: a
   selected row sets the hash, and a hash change selects the row, so deep
   links, reload, and the back button work as before. Below 900px nothing
   here changes the page: the list, or the meeting page with its back link.
   app.js decides the width (paneSync sets body.panes); the stylesheet lays
   the panes out from that class while a meeting is open (body.route-meeting).
   ============================================================================ */
const PANES_MIN=900;       // the narrow fallback's edge (px of viewport width)
const WALK_MS=150;         // j/k: the reader mounts once the walk pauses this long
let PANES=false;           // two panes at this width (paneSync keeps it current)
let RSEL=null;             // the row that wears --sel: the reader's meeting, or the
                           // row a j/k walk is on before the reader catches up
let RQUIET=null;           // the base the next mount opens quietly: a j/k walk or
                           // the default open never takes focus or plays a clip
let WALK_T=null,WALK_GO=null;   // the walk's pending mount, and the base it set
let SEL_REVEAL=false;      // a route change still owes the list a scroll to its row

// the narrow fallback decision: two panes from 900px up, one column below
function twoPanes(width){return width>=PANES_MIN;}
// the list is a pane right now: two panes and a meeting in the reader. With
// no meeting to read (an empty library) the list keeps today's one column.
function paned(){return PANES&&route.view==='meeting';}
// a row that opens in the reader: a meeting, ready or waiting for its title
function rowOpens(state){return state==='ready'||state==='needs_name';}
// the reader with no hash: the first To finish row, else the newest meeting.
// `skip` is a meeting that just left (archived or deleted from the reader).
function readerDefault(timeline,now,skip){
  const sec=timelineSections((timeline||[]).filter(r=>r&&r.id!==skip),now);
  const r=sec.fin[0]||sec.rest.find(x=>rowOpens(x.state));
  return r?r.id:null;
}
// the reader's meeting is no longer in the library at all
function readerLost(timeline,base){return !(timeline||[]).some(r=>r&&r.id===base);}

function paneSync(){
  const two=twoPanes(window.innerWidth);
  if(two===PANES&&document.body.classList.contains('panes')===two)return;
  PANES=two;
  document.body.classList.toggle('panes',two);
  if(!two){clearTimeout(WALK_T);WALK_T=null;}
  mStickyTop();
  selPaint();
  if(S)render();       // the list returns beside the reader, or steps aside
}
// the row the reader's meeting holds in the list (none while its month is folded)
function selRowEl(){
  if(!RSEL)return null;
  const k=CSS.escape(RSEL);
  return document.querySelector('#timeline .row[data-id="'+k+'"], #timeline .lrow[data-id="'+k+'"]');
}
// --sel on exactly one row: accent-soft ground and a 3px inset accent edge
function selPaint(){
  document.querySelectorAll('#timeline .sel').forEach(el=>{
    el.classList.remove('sel');el.removeAttribute('aria-current');});
  const el=PANES?selRowEl():null;
  if(el){el.classList.add('sel');el.setAttribute('aria-current','true');}
}
// after a rebuild: paint again, and pay a route change's scroll once its row
// exists. Before the first poll there is no list yet, so the debt waits; a
// row in a folded month never appears, so a drawn list settles it.
function selAfter(){
  selPaint();
  if(!SEL_REVEAL||!paned())return;
  const el=selRowEl();
  if(el)paneReveal(el);
  if(el||S)SEL_REVEAL=false;
}
// the list pane scrolls on its own: bring a row into it without touching the
// window, whose scroll is the reader's. `start` puts it at the top (a rail jump).
function paneReveal(el,start){
  const pane=el&&el.closest?el.closest('.wrap'):null;
  if(!pane)return;
  const r=el.getBoundingClientRect(),p=pane.getBoundingClientRect(),m=8;
  if(start||r.top<p.top+m)pane.scrollTop+=r.top-p.top-m;
  else if(r.bottom>p.bottom-m)pane.scrollTop+=r.bottom-p.bottom+m;
}
// select a list row: it wears --sel at once, and the reader follows by the #m
// route, at once (a click, Enter) or when a j/k walk pauses for WALK_MS, so a
// held j never rebuilds the reader on every row it passes
function selectRow(id,walk){
  RSEL=id;selPaint();
  clearTimeout(WALK_T);WALK_T=null;
  if(route.view==='meeting'&&route.base===id)return;   // the reader shows it already
  const go=()=>{
    WALK_T=null;
    if(route.view==='meeting'&&route.base===id)return;
    WALK_GO=id;if(walk)RQUIET=id;
    location.hash='#m/'+encodeURIComponent(id);
  };
  if(walk)WALK_T=setTimeout(go,WALK_MS);else go();
}
// the hash moved (a selection, ] or [, the back button, a deep link): the
// list's selection follows it. A walk still pending past the step that set
// this hash keeps its own row; any other route change cancels the walk.
function selFollow(){
  const own=WALK_GO!=null&&route.view==='meeting'&&route.base===WALK_GO;
  WALK_GO=null;
  if(!(own&&WALK_T)){
    clearTimeout(WALK_T);WALK_T=null;
    RSEL=route.view==='meeting'?route.base:null;
  }
  SEL_REVEAL=true;
  selAfter();
}
// two panes and no meeting in the reader: open the default one, replacing the
// empty history entry so the back button never lands on an empty reader
function readerSync(){
  if(!PANES||!S||route.view==='meeting')return false;
  const id=readerDefault(S.timeline);
  if(!id)return false;
  RQUIET=id;
  location.replace('#m/'+encodeURIComponent(id));
  return true;
}
// the reader's meeting is gone (archived, deleted): one column goes back to
// the list, as before; two panes open the default meeting instead
function readerLeave(id){
  if(!PANES){location.hash='';return;}
  const next=readerDefault(S&&S.timeline,undefined,id);
  if(next){RQUIET=next;location.replace('#m/'+encodeURIComponent(next));}
  else location.hash='';
}
// a poll no longer lists the reader's meeting (a bulk archive, a bulk rename
// that re-stamped it): open the default rather than keep a stale page. A
// hand-off in flight (FS_CARRY) re-routes on its own; an open layer waits.
function readerKeep(){
  if(!PANES||!S||route.view!=='meeting'||!MP||!MP.built||MP.base!==route.base)return;
  if(FS_CARRY||kbLayerOpen())return;
  if(readerLost(S.timeline,route.base))readerLeave(route.base);
}
// a list edit re-stamped the reader's meeting (a row rename or date): follow it
function readerFollow(from,to){
  if(PANES&&to&&to!==from&&route.view==='meeting'&&route.base===from)
    location.replace('#m/'+encodeURIComponent(to));
}
// two panes: a click on a row whose meeting is not in the reader selects it
// (true); the reader's own row answers false, so the click does its own work
function listPick(id){
  if(!PANES||(route.view==='meeting'&&route.base===id))return false;
  selectRow(id);return true;
}
// a click or Enter on a meeting row: the reader shows it now; on the reader's
// own row the keyboard moves into the reader (readerTake)
function readerOpen(id){
  const same=route.view==='meeting'&&route.base===id;
  selectRow(id);
  if(same)readerTake();
}
// the keyboard leaves the list for the reader: the ring lets go (and the next
// rebuild does not restore it), so Enter, n, and a are the reader's, and the
// finish flow a j/k walk mounted quietly wakes. Escape brings the ring back.
function readerTake(){
  kbLast=null;
  const ae=document.activeElement;
  if(ae&&ae.closest&&ae.closest('#timeline'))ae.blur();
  fsWake();
}

/* ------------------------------------------------------ build the page ----- */
async function buildMeeting(base){
  const m=meetingByBase(base);
  const page=$('#meetingpage');
  if(!S||!m){                                // deep link before S loaded, or unknown base
    page.innerHTML='<div class="mloading"><span class="spin"></span> Loading meeting&#8230;</div>';
    MP={base,pending:true};
    return;
  }
  if(MP&&MP.base===base&&MP.built)return;    // poll re-entry: already built
  teardownMeeting();
  MP={base,built:true,audio:null,segs:[],color:{},follow:true,rate:1,
      hasAudio:!!m.audio,tick:null,autoScroll:false,nowIdx:null,
      spkOpts:[],people:[],spanStop:null,pendingReview:false,
      // the save stamp of the bytes on screen; relabelWatch compares the
      // polled row against it. Filled in from the transcript response below.
      stamp:null};

  page.innerHTML=
    _mHeader(m,base)
    +(m.audio?_mAudioBar():'')
    +'<div id="mfinish" class="mfinish" hidden></div>'
    +_mSummary(m)
    +_mFlagged(m)
    +_mTranscriptShell()
    +_mAsk(base);

  if(m.audio)mAudioInit(base);
  mStickyTop();
  mAskInit(base);
  fsInit(base);          // the finish strip, when this meeting has an open step
  // a pending deep action lands once, as soon as the page skeleton exists:
  // seek the audio to a search hit's moment (the transcript scroll follows
  // after the segments load, below), focus the ask input, or arm the flag
  // stepper (consumed after the segments arrive, below)
  if(pendingOpen){
    const p=pendingOpen;pendingOpen=null;    // stale-for-another-base also drops here
    if(p.base===base){
      if(p.seek!=null){MP.seekTo=p.seek;if(MP.hasAudio)mSeek(p.seek);}
      if(p.ask){const q=$('#maskq');if(q)q.focus();}
      if(p.review)MP.pendingReview=true;
    }
  }
  document.removeEventListener('keydown',mKey);
  document.addEventListener('keydown',mKey);   // slash / Cmd-F focus the find field

  let d;
  try{d=await api('/api/transcript?base='+encodeURIComponent(base));}
  catch(e){d={error:'The transcript could not be loaded.'};}
  if(route.view!=='meeting'||route.base!==base)return;   // navigated away mid-fetch
  const body=$('#mtbody');if(!body)return;
  if(d.error){body.innerHTML='<div class="mterr">'+esc(d.error)+'</div>';return;}
  MP.segs=d.segments||[];
  MP.noSpeech=!!d.no_speech;
  MP.spkOpts=d.speaker_options||[];
  MP.people=d.people||[];
  MP.stamp=d.generated_at||null;
  mAudioNote(d.audio_short_by);
  (d.speakers||[]).forEach((w,i)=>MP.color[w]=HUES[i%HUES.length]);
  body.innerHTML=mBodyHTML();
  mLegend(d);
  fsSegsLoaded();        // the strip's voices and flags, from these segments
  // a search hit's moment: scroll its line into view now that the segments
  // exist (with audio, mSeek above already cued playback; the follow highlight
  // owns the .mnow mark from here, so only mark it ourselves when audioless)
  if(MP.seekTo!=null){
    const t=MP.seekTo;MP.seekTo=null;
    const i=MP.segs.findIndex(g=>t>=g.start&&t<g.end);
    const el=i>=0?document.getElementById('mseg'+i):null;
    if(el){
      if(!MP.hasAudio){el.classList.add('mnow');MP.nowIdx=i;}
      el.scrollIntoView({block:'center'});
    }
  }
  // a review chip opened this page: start stepping on the first flag
  if(MP.pendingReview){MP.pendingReview=false;if(MP.segs.length)reviewStart(0);}
}

/* --------------------------------------------------------- header block ---- */
// the header's meta line: the date, then the To finish row's own rule
// (peopleMetaBits): the duration, up to three named people with "+N" for the
// rest, then "N unnamed" in the attention teal. The unnamed count is the
// meeting's unnamed_clusters, the set the row's finish.voices counts, so a
// split voice counts once. It listed every raw roster label before: "Grace
// Lindqvist, Elena Ruiz, Voice 2, Speaker 72, Speaker 86, ..."
function mMetaBits(m){
  const bits=m.date?[esc(shortDate(m.date))]:[];
  return bits.concat(peopleMetaBits({minutes:m.minutes,named:m.named,
    finish:{voices:Object.keys(m.unnamed_clusters||{}).length}}));
}
// the poll keeps that line current: a name saved on the page drops the
// "N unnamed" count without a rebuild. The last markup rides the element,
// because innerHTML reads back serialized and never equals the source string.
function mHeadSync(s){
  if(!MP||!MP.built||!route||route.view!=='meeting'||route.base!==MP.base)return;
  const el=document.getElementById('mmbits');
  const m=((s&&s.meetings)||[]).find(x=>x.base===MP.base);
  if(!el||!m)return;
  const h=mMetaBits(m).join(' &middot; ');
  if(el.dataset.h!==h){el.innerHTML=h;el.dataset.h=h;}
}
// the Draft title chip: a provisional title (a typed label, or the
// amnesty's relabel) that no person has kept or edited. No count anywhere:
// a count would turn the chip into a second backlog. A public download keeps
// its published name and wears no chip.
function draftChipHTML(m){
  return (m&&m.title_state==='provisional'&&m.route!=='public_download')
    ?'<span class="mchip draft" title="A draft title: edit it, or leave it">Draft title</span>':'';
}
// the date's source, shown for every date. A file-time date or a short
// typed one is a guess, so it also wears the Date uncertain chip. A meeting
// from before the key (no date_source) shows neither. No count anywhere.
function dateSourceHTML(m){
  const src=m&&m.date_source;
  if(!src)return '';
  const said={typed:'typed in the name',recorder:'the recorder',
    short_typed:'a short date in the name',mtime:'the file time',manual:'set by hand'};
  const unsure=src==='mtime'||src==='short_typed';
  return (unsure?'<span class="mchip dateunsure" title="This date may be the transfer day: change it if it is wrong">Date uncertain</span>':'')
    +`<span class="mdatesrc">Date from ${esc(said[src]||src)}</span>`;
}
function _mHeader(m,base){
  const bits=mMetaBits(m);
  const dot=(m.category==='work'||m.category==='personal')?m.category:'none';
  const dotTitle=dot==='work'?'Work':dot==='personal'?'Personal':'Untagged';
  const strict=m.strict?'<span class="mchip">strict</span>':'';
  const draft=draftChipHTML(m);
  const nfl=(m.flagged||0)+(m.flagged_minor||0);
  const flagged=nfl>0?`<span class="mflagnote">${nfl} flagged</span>`:'';
  // everything a row can do, the page can do: the same ⋯ menu, and the same
  // category cycle (untagged -> work -> personal), optimistic. Tagged, the
  // control wears the row chip's label; untagged, it is the hollow dot.
  return `<div class="mhead">
    <a class="mback" href="#" onclick="mBack(event)">&#8592; Meetings</a>
    <button class="iact mhmenu" id="mmenu" type="button" title="Export, copy, rename, redo&#8230;"
      aria-label="Meeting actions" onclick="mMenu(event)">&#8943;</button>
    <h1 class="mtitle" title="Click to rename" onclick="rowTitleEdit(event,'${escJs(base)}')">${esc(m.title||base)}</h1>
    <div class="mmeta"><span id="mmbits">${bits.join(' &middot; ')}</span>
      <button class="mcat ${dot}" id="mcatdot" type="button"
        title="${dotTitle} (click to change tag)" onclick="mCycleCat(event)">${dot==='none'?'':dotTitle}</button>${draft}${dateSourceHTML(m)}${strict}${flagged}<span class="mflagnote" id="maudionote" hidden></span></div>
  </div>`;
}
// the audio-may-be-lost note: the Needs you row's words, kept on the
// meeting's own header after the row leaves the list (plan row 10). Filled
// from the transcript response, so only the open meeting pays for the probe.
function audioNoteText(mins){
  return mins?`The audio is ${mins} minute${mins!==1?'s':''} shorter than the transcript: audio may be lost`:'';
}
function mAudioNote(mins){
  const el=document.getElementById('maudionote');
  if(!el)return;
  const t=audioNoteText(mins);
  el.textContent=t;el.hidden=!t;
}
// header dot: same optimistic /api/set_category cycle as the library rows
function mCycleCat(ev){
  if(ev)ev.stopPropagation();
  if(!MP)return;
  const m=meetingByBase(MP.base);if(!m)return;
  const cur=(m.category&&m.category!=='none')?m.category:'';
  const next={'':'work',work:'personal',personal:''}[cur];
  m.category=next;
  const r=rowById(MP.base);if(r)r.category=next;   // the library row agrees on return
  const d=$('#mcatdot');
  if(d){d.className='mcat '+(next||'none');
    d.textContent=next==='work'?'Work':next==='personal'?'Personal':'';
    d.title=(next==='work'?'Work':next==='personal'?'Personal':'Untagged')+' (click to change tag)';}
  api('/api/set_category',{base:MP.base,category:next}).then(refresh);
}
// the row ⋯ menu, anchored to the page header: same items, same handlers
function mMenu(ev){
  if(ev)ev.stopPropagation();
  if(!MP)return;
  const anchor=(ev&&ev.currentTarget)||$('#mmenu');
  const pop=$('#rowmenu');pop.dataset.rowid=MP.base;
  openPop(pop,anchor,()=>fillRowMenu(pop,MP.base));
  pop.dataset.page='1';   // anchored in the reader: a list rebuild never moves it
}

/* ---------------------------------------------------- docked audio bar ----- *
 * A custom player (play/pause, buffered scrubber, mono remaining-time, rate
 * cycle, follow toggle). Sticky just under the shell header. Ported behaviors:
 * a line click seeks (readyState guard from tvSeek), the playing line highlights,
 * and the page auto-follows until the reader scrolls against it. */
function _mAudioBar(){
  return `<div class="maudio" id="maudio">
    <button class="mpp iact play" id="mpp" type="button" onclick="mPlayPause()" title="Play / pause">&#9654;</button>
    <span class="mt mono" id="mcur">0:00</span>
    <div class="mtrack" id="mtrack">
      <div class="mbuf" id="mbuf"></div>
      <input id="mseek" class="mseek" type="range" min="0" max="0" step="0.1" value="0"
        aria-label="Scrub the recording"
        onpointerdown="this.dataset.drag=1" onpointerup="this.removeAttribute('data-drag')"
        oninput="mScrub(this.value)">
    </div>
    <span class="mt mono" id="mdur">0:00</span>
    <span class="meta mono" id="meta" title="Time remaining">&#8722;0:00</span>
    <button class="mrate" id="mrate" type="button" onclick="mCycleRate()" title="Playback speed">1&#215;</button>
    <button class="mfollow" id="mfollow" type="button" onclick="mToggleFollow()"
      aria-pressed="true" title="Auto-scroll to the line that is playing">follow</button>
    <button class="mfollow" id="maddline" type="button" onclick="mAddAtPlayhead()"
      title="Add a line the pipeline missed, at the audio&#8217;s current position: pause where you heard it, then click">&#65291; line at playhead</button>
  </div>`;
}
function mAudioInit(base){
  const a=new Audio('/api/audio?base='+encodeURIComponent(base));
  a.preload='metadata';
  MP.audio=a;
  a.onloadedmetadata=mAudioTick;
  a.ontimeupdate=mAudioTick;
  a.onprogress=mBufTick;
  a.onplay=a.onpause=mSyncPP;
  a.onended=()=>{mSyncPP();mAudioTick();};
  MP.tick=setInterval(mHighlight,300);        // keep the highlight live between timeupdates
  window.addEventListener('scroll',mOnScroll,{passive:true});
  mFollowUI();mSyncPP();
}
function mAudioTick(){
  const a=MP&&MP.audio;if(!a)return;
  // span playback (Play clip / Play span): stop just past the segment's end
  if(MP.spanStop!=null&&a.currentTime>=MP.spanStop){a.pause();MP.spanStop=null;mSyncPP();}
  const seek=$('#mseek'),cur=$('#mcur'),dur=$('#mdur'),eta=$('#meta');
  const d=isFinite(a.duration)?a.duration:0;
  if(seek&&!seek.dataset.drag){seek.max=d||0;seek.value=a.currentTime||0;}
  if(cur)cur.textContent=clock(a.currentTime);
  if(dur)dur.textContent=clock(d);
  if(eta)eta.innerHTML='&#8722;'+clock(Math.max(0,d-(a.currentTime||0)));
  mBufTick();mHighlight();
}
function mBufTick(){
  const a=MP&&MP.audio,buf=$('#mbuf');if(!a||!buf)return;
  const d=isFinite(a.duration)?a.duration:0;
  let end=0;try{if(a.buffered.length)end=a.buffered.end(a.buffered.length-1);}catch(e){}
  buf.style.width=(d?Math.min(100,end/d*100):0)+'%';
}
function mSyncPP(){const b=$('#mpp');if(b)b.innerHTML=(MP&&MP.audio&&!MP.audio.paused)?'&#10073;&#10073;':'&#9654;';}
function mPlayPause(){const a=MP&&MP.audio;if(!a)return;
  MP.spanStop=null;                              // manual control ends span playback
  if(a.paused)a.play().catch(()=>{});else a.pause();mSyncPP();}
function mScrub(v){const a=MP&&MP.audio;if(!a)return;
  MP.spanStop=null;a.currentTime=parseFloat(v)||0;mAudioTick();}
function mCycleRate(){
  if(!MP||!MP.audio)return;
  MP.rate=MRATES[(MRATES.indexOf(MP.rate)+1)%MRATES.length];
  MP.audio.playbackRate=MP.rate;
  const b=$('#mrate');if(b)b.innerHTML=MP.rate+'&#215;';
}
// clicking any transcript line seeks there and (re)enables follow -- the reader
// is navigating by transcript, so following the playhead is what they want
function mSeek(t){
  const a=MP&&MP.audio;if(!a)return;
  MP.spanStop=null;                              // a line click ends span playback
  MP.follow=true;mFollowUI();
  const go=()=>{a.currentTime=t;a.play().catch(()=>{});mSyncPP();};
  a.readyState>=1?go():a.addEventListener('loadedmetadata',go,{once:true});
}
function mHighlight(){
  const a=MP&&MP.audio;if(!a||!MP.segs.length)return;
  const t=a.currentTime;
  const i=MP.segs.findIndex(g=>t>=g.start&&t<g.end);
  if(i===MP.nowIdx)return;
  if(MP.nowIdx!=null){const pe=document.getElementById('mseg'+MP.nowIdx);if(pe)pe.classList.remove('mnow');}
  MP.nowIdx=i;
  if(i<0)return;
  const el=document.getElementById('mseg'+i);
  if(!el)return;
  el.classList.add('mnow');
  if(MP.follow&&!a.paused){
    MP.autoScroll=true;                         // suppress the follow-off from our own scroll
    el.scrollIntoView({block:'center'});
    clearTimeout(MP._asT);MP._asT=setTimeout(()=>{MP.autoScroll=false;},250);
  }
}
function mOnScroll(){
  if(!MP||MP.autoScroll)return;                 // ignore our own scrollIntoView
  if(MP.follow){MP.follow=false;mFollowUI();}    // the reader scrolled against the follow
}
function mToggleFollow(){
  if(!MP)return;
  MP.follow=!MP.follow;mFollowUI();
  if(MP.follow){MP.nowIdx=null;mHighlight();}     // snap back to the playing line
}
function mFollowUI(){
  const b=$('#mfollow');if(!b)return;
  b.classList.toggle('on',!!(MP&&MP.follow));
  b.setAttribute('aria-pressed',(MP&&MP.follow)?'true':'false');
}

/* ------------------------------------------------------- summary section --- */
function _mSummary(m){return `<section class="msection msummary" id="msummary">${_mSummaryInner(m)}</section>`;}
function _mSummaryInner(m){
  // a transcript with 0 segments: say so, and offer no Generate. A model
  // given nothing writes a plausible summary of a meeting that never happened.
  if(m.no_speech)return `<div class="mseclabel">Summary</div>
    <div class="msumbody muted" id="msumbody">No speech found in this recording.</div>`;
  const has=m.summary&&m.summary.trim();
  const steps=(m.next_steps&&m.next_steps.length)
    ?`<div class="mstepshdr">Committed next steps</div><ul class="msteps">${m.next_steps.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>`:'';
  if(has){
    return `<div class="mseclabel">Summary</div>
      <div class="msumbody" id="msumbody">${esc(m.summary)}</div>${steps}
      <div class="msumfoot"><button class="btn mini" id="mgenbtn" type="button" onclick="mGenSummary()">Regenerate summary</button></div>`;
  }
  // no summary yet: the old page's exact phrasing, with its em dash dropped for
  // the new shell's no-em-dash house rule
  const hint='No summary yet. Generate one below. '
    +(S.llm_backend==='local'?'Runs locally; nothing leaves this Mac.':'Uses your cloud assistant.');
  return `<div class="mseclabel">Summary</div>
    <div class="msumbody muted" id="msumbody">${esc(hint)}</div>
    <div class="msumfoot"><button class="btn primary mini" id="mgenbtn" type="button" onclick="mGenSummary()">Generate summary</button></div>`;
}
// wired: GET /api/suggest (same call + persistence as the old genSummary), then
// refresh S and repaint the summary section. Failure (e.g. no local backend)
// is surfaced inline, never a crash or a banner.
async function mGenSummary(){
  if(!MP)return;const base=MP.base;
  const btn=$('#mgenbtn'),body=$('#msumbody');
  if(btn){btn.disabled=true;btn.textContent='Generating…';}
  const wait='Reading the transcript… '+(S.llm_backend==='local'?'(~10-20s)':'(a few seconds)');
  if(body)body.innerHTML='<span class="spin"></span> '+esc(wait);
  let r;
  try{r=await api('/api/suggest?base='+encodeURIComponent(base));}
  catch(e){r={error:'The summary service is unavailable.'};}
  if(route.view!=='meeting'||route.base!==base)return;
  if(r&&r.summary){
    await refresh();
    if(route.view!=='meeting'||route.base!==base)return;
    const sec=$('#msummary');if(sec)sec.innerHTML=_mSummaryInner(meetingByBase(base)||{});
  }else{
    const sec=$('#msummary');if(sec)sec.innerHTML=_mSummaryInner(meetingByBase(base)||{});
    const b2=$('#msumbody');
    if(b2){b2.classList.add('mterr');b2.textContent=(r&&r.error)||'No summary produced.';}
  }
}

/* -------------------------------------------------------- flagged strip ---- *
 * The strip is the stepping CONTROLLER: clicking it (or pressing n) walks the
 * flagged segments in place; a one-click Accept sits beside it when minor
 * crumbs exist. The stepper itself lives in the edit layer below. */
function _mFlagged(m){
  const n=(m.flagged||0)+(m.flagged_minor||0);
  if(!n)return '';
  return `<div class="mflagbar" id="mflagbar">
    <button class="mflagged" id="mflagstrip" type="button" onclick="reviewStep()"
      title="Step through the flagged lines in place (n next, p previous)">&#9888; ${n} flagged line${n>1?'s':''}</button>
    ${m.flagged_minor?`<button class="btn mini" id="mflagminor" type="button" onclick="reviewAcceptMinor(this)"
      title="Sub-second crosstalk crumbs (&#8220;like&#8221;, &#8220;so&#8221;&#8230;): accept them all in one click; substantial lines stay">&#10003; Accept ${m.flagged_minor} minor</button>`:''}
  </div>`;
}

/* ----------------------------------------------------- transcript + find --- *
 * Time (mono, click to seek), speaker (weight 600 + per-speaker sdot color),
 * text; flagged lines get the amber wash. Find is ported from the old reader:
 * occurrence count, prev/next wrap, highlight + scroll, Escape clears the find
 * (not the page), slash or Cmd-F focuses the field. */
function _mTranscriptShell(){
  return `<section class="msection mtrans">
    <div class="msechead"><span class="mseclabel">Transcript</span>
      <span id="mlegend" class="mlegend"></span></div>
    <div class="mfindbar">
      <input id="mfind" class="mfind" type="search" autocomplete="off" spellcheck="false"
        placeholder="Find in transcript ( / or &#8984;F )" aria-label="Find in transcript"
        oninput="mFindInput()" onkeydown="mFindKey(event)">
      <span id="mfindn" class="mfindn"></span>
      <button class="iact" type="button" onclick="mFindNav(-1)" title="Previous match (Shift Enter)">&#8249;</button>
      <button class="iact" type="button" onclick="mFindNav(1)" title="Next match (Enter)">&#8250;</button>
    </div>
    <div id="mtbody" class="mtbody"><div class="mloading"><span class="spin"></span> Loading transcript&#8230;</div></div>
  </section>`;
}
// the legend's voices: one per VOICE, not one per cluster. The diarizer can
// split one person into two clusters that share a uid (both "Speaker 68"), and
// the legend showed that voice twice. Clusters collapse exactly as
// fsVoicesFrom collapses them (the uid, or with no uid the display), and only
// inside one chip kind: a meeting-local "Ana" never merges with an enrolled
// "Ana", and a dismissed cluster keeps its own undo chip. Each voice carries
// `ids`, every cluster it holds, first one first; `id` stays the first.
function legendVoices(opts){
  const out=[],by=new Map();
  for(const o of (opts||[])){
    const uid=o.named?null:mUnknownUid(o.display);
    const kind=uid?'uid':(!o.named&&!o.dismissed&&o.id!==o.display)?'cluster'
      :(o.named&&o.local)?'local':(!o.named&&o.dismissed)?'dismissed':'plain';
    const k=kind+'\x00'+(uid?'u\x00'+uid:'d\x00'+o.display);
    if(by.has(k)){by.get(k).ids.push(o.id);continue;}
    const v=Object.assign({},o,{uid,ids:[o.id]});
    by.set(k,v);out.push(v);
  }
  return out;
}
// every cluster of the voice `speaker` belongs to, as the legend on the page
// groups them: a name, a dismissal, or its undo acts on the whole voice.
// [speaker] alone when the page shows another meeting.
function mVoiceIds(meeting,speaker){
  const v=MP&&MP.base===meeting&&(MP.voices||[]).find(x=>x.ids.includes(speaker));
  return v?v.ids.slice():[speaker];
}
function mLegend(d){
  const box=$('#mlegend');if(!box)return;
  // one chip per VOICE (legendVoices). Never a bare display-keyed lookup: that
  // collapsed two speakers sharing one name (a local name matching an
  // enrolled person's) into a single entry, and both chips then opened the
  // naming panel for the same, possibly wrong, cluster
  MP.voices=legendVoices(d.speaker_options);
  box.innerHTML=MP.voices.map(o=>{
    const w=o.display;
    const dot=`<span class="msdot" style="background:${MP.color[w]}"></span>`;
    const uid=o.uid;
    // an unnamed voice ("Speaker N" with an unknown-registry entry that lists
    // this meeting): its legend chip opens the naming slide-over
    if(uid)return `<button class="mlg unk" type="button" title="Who is this? Listen and name this voice"
          onclick="openNamePanelByUid('${escJs(uid)}','${escJs(MP.base)}')">${dot}${esc(w)}<span class="mlgq">?</span></button>`;
    // an unnamed voice with NO registry entry: a 'Voice N' the minting floor
    // kept transcript-local, or a voice suppressed by a 'not a real speaker'
    // tombstone. Every unnamed voice on the page stays nameable: the chip
    // names it straight from THIS meeting's audio (/api/name meeting mode),
    // which the tombstone does not gate. Without this, three different
    // suppression layers all rendered as identical dead text and the only
    // sign of a real person was an unnameable label.
    // ...unless it was dismissed as "not a real speaker" in THIS meeting: the
    // lines stay where they are, the chip just stops asking to be named.
    if(!o.named&&!o.dismissed&&o.id!==w)
      return `<button class="mlg unk" type="button" title="Who is this? Listen and name this voice"
          onclick="openNamePanelByCluster('${escJs(MP.base)}','${escJs(o.id)}','${escJs(w)}')">${dot}${esc(w)}<span class="mlgq">?</span></button>`;
    // named for THIS meeting only (no voiceprint): the chip stays editable,
    // because re-saving through the same floor-then-confirm flow is the only
    // way to fix a typo in a name the registry never learned
    if(o.named&&o.local)
      return `<button class="mlg unk" type="button" title="Named for this meeting only. Click to change or clear"
          onclick="openNamePanelByCluster('${escJs(MP.base)}','${escJs(o.id)}','${escJs(w)}',true)">${dot}${esc(w)}</button>`;
    // dismissed as "not a real speaker": the chip stopped asking, but a human
    // who changes their mind needs a way BACK -- a dismissal with no undo on
    // the page left the voice permanently unnameable from the transcript
    if(!o.named&&o.dismissed)
      return `<button class="mlg unk" type="button" title="Dismissed as not a real speaker. Click to make it nameable again"
          onclick="mRestoreVoice('${escJs(MP.base)}','${escJs(o.id)}')">${dot}${esc(w)}<span class="mlgr">&#8634;</span></button>`;
    return `<span class="mlg">${dot}${esc(w)}</span>`;
  }).join('');
}
// the undo restores every cluster of the voice, or the one left dismissed
// splits the chip back into two. ONE request (`speakers`): the server checks
// every id and restores all or none, where one call per cluster could stop
// halfway and leave part of the voice restored
async function mRestoreVoice(base,id){
  const ids=mVoiceIds(base,id);
  const body={base:base,speaker:ids[0],restore:true};
  if(ids.length>1)body.speakers=ids;
  const r=await api('/api/dismiss_voice',body);
  if(r&&r.ok){refresh();if(MP&&MP.base===base)mReloadSegs();}
}
function mUnknownUid(display){
  // hidden (archived) unknowns keep their "Who is this?" chip: hiding was meant
  // to stop the drawer/tray NAGGING about a one-time voice, not to strip the
  // naming affordance out of every transcript the voice appears in. Filtering
  // them here left unnamed speakers on the page with no way to review them.
  const u=(S&&S.unknowns||[]).find(u=>u.display===display
    &&(u.meetings||[]).includes(MP.base));
  return u?u.uid:null;
}
function mSegHTML(g,i){
  const mm=Math.floor(g.start/60),ss=String(Math.floor(g.start%60)).padStart(2,'0');
  const hue=MP.color[g.who]||'var(--sub)';
  const flag=g.flags&&g.flags.length;
  const seek=MP.hasAudio?` onclick="mSeek(${g.start})"`:'';
  return `<div class="mseg${flag?' flagged':''}" id="mseg${i}"${seek}>
    <span class="mtime mono">${mm}:${ss}</span>
    <span class="mspk"><span class="msdot" style="background:${hue}"></span>${esc(g.who)}${flag?` <span class="mstar" title="Uncertain: ${esc((g.flags||[]).join(', '))}">*</span>`:''}${g.edited?' <span class="medited" title="Edited by you">&#9998;</span>':''}</span>
    <span class="mtext">${esc(g.text)}</span>
    <button class="msegedit" type="button" title="Fix this line (speaker or text)"
      onclick="mEdit(${i},event)">&#9998;</button>
  </div>`;
}
// the gap between two lines: a quiet hover affordance for a voice the pipeline
// missed entirely (i = the segment ABOVE the gap; -1 = before the first line)
function mGapHTML(i){
  return `<div class="mgap" id="mgap${i}" tabindex="0" role="button"
    aria-label="Add a line here: a voice the pipeline missed"
    onclick="mInsertAt(${i})"
    onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();mInsertAt(${i})}"
    title="Add a line here: a voice the pipeline missed">&#65291; add line</div>`;
}
function mBodyHTML(){
  // a silent recording is a fact about the audio, not an error: the same
  // muted words the summary uses
  if(!MP.segs.length)return MP.noSpeech
    ?'<div class="msumbody muted">No speech found in this recording.</div>'
    :'<div class="mterr">This transcript has no lines yet.</div>';
  return mGapHTML(-1)+MP.segs.map((g,i)=>mSegHTML(g,i)+mGapHTML(i)).join('');
}
function mFindInput(){clearTimeout(mFindT);mFindT=setTimeout(()=>mFindRun(true),200);}
function mFindKey(e){
  if(e.key==='Enter'){
    e.preventDefault();clearTimeout(mFindT);
    const q=$('#mfind').value.trim();
    if(!MF||MF.q!==q)mFindRun(true);else mFindNav(e.shiftKey?-1:1);
  }else if(e.key==='Escape'){
    e.preventDefault();e.stopPropagation();
    mFindClear();$('#mfind').blur();
  }
}
function mFindRun(reset){
  const f=$('#mfind'),q=f?(f.value||'').trim():'';
  const prev=MF;mFindUnmark();MF=null;
  if(q.length<2){mFindCount();return;}
  const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
  const hits=[];
  MP.segs.forEach((g,i)=>{const mm=g.text.match(rx);if(mm)for(let k=0;k<mm.length;k++)hits.push({i,k});});
  const cur=hits.length?(reset?0:Math.min(prev?Math.max(prev.cur,0):0,hits.length-1)):-1;
  MF={q,rx,hits,cur};
  mFindMark();mFindCount();
  if(reset&&cur>=0)mFindShow();
}
function mFindMark(){
  if(!MF||!MF.hits.length)return;
  const rows={};MF.hits.forEach((h,n)=>{(rows[h.i]=rows[h.i]||[]).push(n);});
  for(const i in rows){
    const el=document.getElementById('mseg'+i),g=MP.segs[i];
    if(!el)continue;
    const x=el.querySelector('.mtext');if(!x)continue;
    const t=g.text;let out='',last=0,k=0;
    t.replace(MF.rx,(mm,off)=>{                     // escape AROUND matches so & < > still highlight
      const n=rows[i][k++];
      out+=esc(t.slice(last,off))+`<mark${n===MF.cur?' class="cur"':''}>${esc(mm)}</mark>`;
      last=off+mm.length;return mm;
    });
    x.innerHTML=out+esc(t.slice(last));
  }
}
function mFindUnmark(){
  if(!MF)return;
  new Set(MF.hits.map(h=>h.i)).forEach(i=>{
    const el=document.getElementById('mseg'+i),g=MP.segs[i];
    if(!el||!g)return;
    const x=el.querySelector('.mtext');if(x)x.innerHTML=esc(g.text);
  });
}
function mFindNav(d){
  if(!MF||!MF.hits.length)return;
  MF.cur=(MF.cur+d+MF.hits.length)%MF.hits.length;
  mFindMark();mFindCount();mFindShow();
}
function mFindShow(){
  const h=MF.hits[MF.cur];if(!h)return;
  const el=document.getElementById('mseg'+h.i);if(!el)return;
  (el.querySelector('mark.cur')||el).scrollIntoView({block:'center'});
}
function mFindCount(){
  const n=$('#mfindn');if(!n)return;
  n.textContent=MF?(MF.hits.length?`${MF.cur+1} of ${MF.hits.length}`:'0 of 0'):'';
}
function mFindClear(){
  clearTimeout(mFindT);mFindUnmark();MF=null;
  const f=$('#mfind');if(f)f.value='';mFindCount();
}
function mKey(e){
  if(route.view!=='meeting')return;
  // an edit layer over the page (the naming panel, the drawer) owns every key
  // while it is open: n/p, /, the stepper's keys and the finish flow's stay put
  if(kbLayerOpen())return;
  // an open popover (Process, a ⋯ menu, a bulk confirm) owns the plain keys
  // the same way, so a list key it held back never lands in the reader
  // instead (/ from the list is not the reader's find). Its Escape closes it.
  if(_popClose&&!e.metaKey&&!e.ctrlKey)return;
  if(fsKey(e))return;    // the finish flow's keys: ] [ and the Voices step's
  // two panes: the list took this key (its Enter, e, x, /, j, k) before us
  if(e.defaultPrevented)return;
  const ae=document.activeElement,typing=ae&&/^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName);
  if((e.metaKey||e.ctrlKey)&&e.key==='Enter'&&document.getElementById('mcard')){
    // Cmd/Ctrl+Enter saves the open card from anywhere inside it, textarea
    // included (plain Enter there stays a newline); the card's one primary
    // button IS its save/commit, whichever card is up
    e.preventDefault();
    const b=document.querySelector('#mcard .btn.primary');if(b)b.click();
  }else if((e.metaKey||e.ctrlKey)&&(e.key==='f'||e.key==='F')){
    e.preventDefault();const f=$('#mfind');if(f){f.focus();f.select();}
  }else if(e.key==='/'&&!typing){
    e.preventDefault();const f=$('#mfind');if(f)f.focus();
  }else if(!typing&&(e.key==='n'||e.key==='p')){
    // n/p step the flag review in place; both skip while typing in any input
    if(MR&&MR.active){e.preventDefault();reviewGo(e.key==='n'?1:-1);}
    else if(e.key==='n'&&document.getElementById('mflagbar')){e.preventDefault();reviewStep();}
  }else if(!typing&&!e.metaKey&&!e.ctrlKey&&!e.altKey&&MR&&MR.active&&!_popClose&&!NP
           &&(e.key==='Enter'||e.key==='a'||e.key==='u')){
    // the stepping fast path: Enter or a = accept-and-advance, u = swap in the
    // second engine's version when this flag carries one. A focused button
    // keeps its own Enter (the two-step confirms stay keyboard-reachable),
    // and an open popover owns all three keys.
    if(e.key==='Enter'&&ae&&ae.tagName==='BUTTON')return;
    if(e.key==='u'){
      const it=MR.hold?null:MR.items[MR.i];
      if(!(it&&it.alt&&it.alt.length))return;
      e.preventDefault();reviewUseAltGo();
    }else{e.preventDefault();reviewApply('accept');}
  }else if(e.key==='Escape'&&!typing&&!NP&&!_popClose&&!e.defaultPrevented){
    // leave the stepper / close an open card (the naming panel and any open
    // popover own their Escape first). mKey registers after the panel's and
    // the drawer's own Escape handlers, so by now a layer that took this
    // press is already closed: its preventDefault is the only trace, and one
    // Escape must never also exit the stepper under it
    if(MR&&MR.active){e.preventDefault();reviewExit();}
    else if(document.getElementById('mcard')){e.preventDefault();mCloseCard();}
  }
}

/* -------------------------------------------------- pinned ask bar + thread - *
 * The thread renders in the document flow (question right / answer left); the
 * input is pinned to the viewport bottom. Ported from the old openAsk/askSend:
 * the privacy note (local vs cloud), last-3 successful turns ride along as
 * history, POST /api/ask, the truncated note, disabled when no LLM. In-memory
 * per meeting until reload; never persisted. */
function _mAsk(base){
  const priv='Answers come from this transcript only. '
    +(S.llm_backend==='local'?'They are generated on this Mac; nothing leaves the machine.'
      :'They are generated by your cloud assistant (the transcript text is sent to it for this).')
    +' Follow-up questions understand the earlier ones. The thread is never saved.';
  return `<section class="msection mask">
    <div class="mseclabel">Ask</div>
    <p class="maskpriv">${esc(priv)}</p>
    <div class="maskthread" id="maskthread"></div>
    <div class="masknote" id="masknote"></div>
    <div class="maskbar" id="maskbar">
      <input class="maskq" id="maskq" type="text" autocomplete="off" spellcheck="false"
        placeholder="Ask about this meeting" aria-label="Ask about this meeting"
        onkeydown="if(event.key==='Enter')mAskSend()">
      <button class="btn primary maskbtn" id="maskbtn" type="button" onclick="mAskSend()">Ask</button>
    </div>
  </section>`;
}
function mAskInit(base){
  if(!askThreads[base])askThreads[base]={hist:[],busy:false};
  const t=askThreads[base],dis=!S.llm_available;
  const q=$('#maskq'),b=$('#maskbtn');
  if(q){q.disabled=dis||t.busy;if(dis)q.placeholder='Ask needs the local model installed';}
  if(b)b.disabled=dis||t.busy;
  mAskRender(base);
}
function mAskRender(base){
  const t=askThreads[base];if(!t)return;
  const box=$('#maskthread');if(!box)return;
  box.innerHTML=t.hist.length?t.hist.map(h=>`
    <div class="mbub q">${esc(h.q)}</div>
    <div class="mbub a${h.err?' err':''}">${
      h.a?esc(h.a):'<span class="spin"></span> Reading the transcript and thinking… '
        +(S.llm_backend==='local'?'usually 20-60s (the model loads fresh for each question)':'usually a few seconds')}</div>`).join('')
    :'<div class="maskempty">Ask anything about this meeting: decisions, who said what, commitments…</div>';
}
async function mAskSend(){
  if(!MP)return;const base=MP.base,t=askThreads[base];
  if(!t||t.busy||!S.llm_available)return;
  const inp=$('#maskq'),q=inp?inp.value.trim():'';
  if(!q)return;
  const hist=t.hist.filter(h=>h.a&&!h.err).slice(-3).map(h=>({q:h.q,a:h.a}));
  t.hist.push({q,a:''});t.busy=true;
  if(inp){inp.value='';inp.disabled=true;}
  const btn=$('#maskbtn');if(btn)btn.disabled=true;
  const note=$('#masknote');if(note)note.textContent='';
  mAskRender(base);
  let r;
  try{r=await api('/api/ask',{base,question:q,history:hist});}
  catch(e){r={error:'The assistant is unavailable.'};}
  const cur=t.hist[t.hist.length-1];
  if(r&&r.answer)cur.a=r.answer;
  else{cur.a='⚠ '+((r&&r.error)||'No answer produced.');cur.err=true;}
  t.busy=false;
  if(route.view!=='meeting'||route.base!==base)return;   // a different page is open now
  if(note)note.textContent=r&&r.truncated
    ?'Long meeting: middle portions were sampled, so details from the middle may be missing.':'';
  if(inp){inp.disabled=false;inp.focus();}
  if(btn)btn.disabled=false;
  mAskRender(base);
}
function mStickyTop(){
  // the audio bar docks under the shell header; the review bar (while flag
  // stepping) docks under the audio bar, or under the header when audioless
  const hdr=$('.hdr'),bar=$('#maudio'),rb=document.getElementById('mrevbar');
  const top=hdr?hdr.offsetHeight:56;
  // the list pane sticks under the header and fills the rest of the window
  document.documentElement.style.setProperty('--hdrh',top+'px');
  if(bar)bar.style.top=top+'px';
  if(rb)rb.style.top=(top+(bar?bar.offsetHeight+6:6))+'px';
}

/* ============================================================================
   Builder C: THE EDIT LAYER. One inline card serves both repair flows: the
   flag STEPPER (the amber strip walks the flagged segments in place: reason,
   play clip, second-engine alternative, editable text + speaker, Accept as-is /
   Save / Skip) and the everywhere EDITOR (the quiet pencil on any segment:
   speaker/text, remove, split, re-transcribe with an engine choice, play span).
   Gaps and the audio bar insert missed lines. All of it POSTs the EXISTING
   /api/review shapes with the segment's ORIGINAL json index (g.index /
   it.index) plus its start as the server's cross-check; the array position i
   appears only in DOM ids. Plus the voice-naming slide-over that retired the
   old page's who-is-this bridge. Confirmations are the house two-step, never
   a native dialog.
   ============================================================================ */

const ENGINES=[['parakeet','Parakeet &middot; fast'],
  ['mlxwhisper:large-v3','Whisper v3 &middot; thorough'],
  ['mlxwhisper:turbo','Whisper turbo']];
let MR=null;    // flag stepper state: {items,i,active,busy,hold,at} (speaker
                // options ride on MP). busy: one review POST in flight.
                // hold: the position is HELD on a spot the card just resolved,
                // with MR.i already naming the NEXT unresolved item.
                // at: {index,start} identity of the spot the card sits on.

/* ------------------------------------------------------- the inline card --- *
 * ONE card at a time, mounted as a SIBLING after its segment (the row stays in
 * the document, so find/highlight code keeps working); the segment is marked
 * .editing (editor) or .mrev (stepper emphasis). The poll never rebuilds the
 * open meeting page, so the card survives every 2s refresh by construction. */
function mCardEl(html,afterEl,cls){
  mCloseCard();
  const d=document.createElement('div');
  d.id='mcard';d.className='mcard'+(cls?' '+cls:'');
  d.innerHTML=html;
  afterEl.insertAdjacentElement('afterend',d);
  return d;
}
function mCloseCard(){
  const d=document.getElementById('mcard');
  if(d)d.remove();
  document.querySelectorAll('.mseg.editing').forEach(e=>e.classList.remove('editing'));
  document.querySelectorAll('.mseg.mrev').forEach(e=>e.classList.remove('mrev'));
}
function mCardErr(msg){
  const e=document.getElementById('mcerr');
  if(!e)return;
  e.hidden=!msg;e.textContent=msg||'';
}

/* speaker picker: this meeting's speakers, every enrolled person, then
 * "New person" (ported from the old page's spkOptions) */
function spkOptions(speakers,people,sel){
  const seen=new Set(speakers.map(s=>s.display));
  let h=speakers.map(s=>`<option value="${esc(s.id)}" ${s.id===sel?'selected':''}>${esc(s.display)}</option>`).join('');
  const others=(people||[]).filter(p=>!seen.has(p));
  if(others.length)h+=`<optgroup label="Someone else">${others.map(p=>`<option value="name:${esc(p)}">${esc(p)}</option>`).join('')}</optgroup>`;
  return h+`<option value="__new__">&#65291; New person&#8230;</option>`;
}
function mSpkSelect(id,sel){
  return `<select id="${id}" class="mcsel" title="Who actually said this?">${spkOptions(MP.spkOpts,MP.people,sel)}</select>`;
}
// "+ New person": an inline input in place of the old page's native prompt.
// Enter (or blur with text) commits; Escape or an empty blur restores the
// previous selection, exactly like cancelling the old prompt did.
function mWireNew(sel){
  if(!sel)return;
  sel.addEventListener('focus',()=>{if(sel.value!=='__new__')sel._prev=sel.value;});
  sel.addEventListener('change',()=>{
    if(sel.value!=='__new__'){sel._prev=sel.value;return;}
    let inp=sel.parentElement.querySelector('.mnewspk');
    if(inp){inp.focus();return;}
    inp=document.createElement('input');
    inp.className='mnewspk';inp.type='text';
    inp.placeholder='Who said this?';
    inp.setAttribute('aria-label','New person name');
    sel.insertAdjacentElement('afterend',inp);
    const cancel=()=>{inp.remove();if(sel._prev!=null)sel.value=sel._prev;else sel.selectedIndex=0;};
    const commit=()=>{
      const nm=inp.value.trim();
      if(!nm){cancel();return;}
      const o=document.createElement('option');o.value='name:'+nm;o.textContent=nm;
      sel.insertBefore(o,sel.lastElementChild);sel.value='name:'+nm;sel._prev=sel.value;
      inp.remove();
    };
    inp.onkeydown=e=>{
      if(e.key==='Enter'){e.preventDefault();commit();}
      else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();cancel();}
    };
    inp.onblur=commit;
    inp.focus();
  });
}

/* span playback: seek a little before the span, stop a little after (the
 * mAudioTick guard owns the stop). Manual control anywhere clears it. */
function mPlaySpan(start,end){
  const a=MP&&MP.audio;if(!a)return;
  MP.spanStop=end+0.7;
  const go=()=>{a.currentTime=Math.max(0,start-0.7);a.play().catch(()=>{});mSyncPP();};
  a.readyState>=1?go():a.addEventListener('loadedmetadata',go,{once:true});
}
function mPlaySpanBtn(i,ev){if(ev)ev.stopPropagation();const g=MP.segs[i];if(g)mPlaySpan(g.start,g.end);}

/* keep the strip, its Accept-minor button, and the header note honest after
 * every flag-resolving action, from the client's own segment state. The strip
 * disappears at zero. mIsMinor mirrors the server's review.is_minor. */
function mIsMinor(g){return (g.end-g.start)<1.0&&g.text.split(/\s+/).filter(Boolean).length<=3;}
function mSyncFlagStrip(){
  if(!MP)return;
  const flagged=MP.segs.filter(g=>g.flags&&g.flags.length);
  const minors=flagged.filter(mIsMinor).length,n=flagged.length;
  const bar=document.getElementById('mflagbar');
  if(bar){
    if(!n)bar.remove();
    else{
      const s=document.getElementById('mflagstrip');
      if(s)s.innerHTML=`&#9888; ${n} flagged line${n>1?'s':''}`;
      const mb=document.getElementById('mflagminor');
      if(mb){if(!minors)mb.remove();else mb.innerHTML=`&#10003; Accept ${minors} minor`;}
    }
  }
  const note=document.querySelector('#meetingpage .mflagnote');
  if(note){if(!n)note.remove();else note.textContent=n+' flagged';}
  fsFlagsSync();   // the finish strip's Flags count, and Done when none are left
}

// refresh ONE rebuilt row in place (text/speaker/flag changed locally)
function mRefreshSeg(i){
  const el=document.getElementById('mseg'+i);
  if(!el)return;
  el.outerHTML=mSegHTML(MP.segs[i],i);
  if(MF)mFindRun(false);          // re-mark find hits on the rebuilt row
}

/* re-fetch the transcript after a STRUCTURAL change (insert / split / delete /
 * a merge folding neighbors into one turn): the server re-issues segments with
 * fresh ORIGINAL indexes, the legend and colors rebuild, and the optional
 * target (an original index from the response) scrolls into view. While flag
 * stepping is active the reload must not lose the reviewer's place: the scroll
 * position is restored, the review list is refetched (its snapshot indexes
 * shifted with the structure), and the stepping position re-anchors to the
 * nearest surviving spot by START TIME, held (never auto-advanced). */
async function mReloadSegs(target){
  if(!MP)return;
  const base=MP.base;
  const stepping=!!(MR&&MR.active);
  const anchor=stepping&&MR.at?MR.at.start:null;
  const y=window.scrollY;
  let d;
  try{d=await api('/api/transcript?base='+encodeURIComponent(base));}
  catch(e){d={error:'The transcript could not be reloaded.'};}
  if(route.view!=='meeting'||route.base!==base)return;
  const body=$('#mtbody');if(!body)return;
  if(d.error){body.innerHTML='<div class="mterr">'+esc(d.error)+'</div>';return;}
  mCloseCard();
  MF=null;{const f=$('#mfind');if(f)f.value='';}mFindCount();
  MP.segs=d.segments||[];
  MP.noSpeech=!!d.no_speech;
  MP.spkOpts=d.speaker_options||[];
  MP.people=d.people||[];
  if(d.generated_at)MP.stamp=d.generated_at;
  mAudioNote(d.audio_short_by);
  MP.color={};(d.speakers||[]).forEach((w,i)=>MP.color[w]=HUES[i%HUES.length]);
  MP.nowIdx=null;
  body.innerHTML=mBodyHTML();
  mLegend(d);
  mSyncFlagStrip();
  fsSegsLoaded();
  if(stepping){
    let rv;
    try{rv=await api('/api/review?base='+encodeURIComponent(base));}
    catch(e){rv=null;}
    if(route.view!=='meeting'||route.base!==base)return;
    if(!MR||!MR.active){refresh();return;}         // exited while refetching
    if(!rv||rv.error||!rv.items||!rv.items.length)reviewExit();
    else{
      if(rv.speakers&&rv.speakers.length)MP.spkOpts=rv.speakers;
      if(rv.people)MP.people=rv.people;
      const items=_revOrder(rv.items);
      // held position: MR.i re-aims at the next unresolved flag AT or BELOW
      // the anchor in document order; past the last it wraps to the earliest
      const a=anchor!=null?anchor:items[0].start;
      let bi=items.findIndex(x=>x.start>=a-0.25);
      if(bi<0)bi=0;
      MR.items=items;MR.i=bi;MR.hold=true;
      // keep the held spot readable: re-mark the nearest surviving segment
      if(anchor!=null){
        const p=MP.segs.findIndex(g=>Math.abs(g.start-anchor)<0.25);
        const el=p>=0?document.getElementById('mseg'+p):null;
        if(el)el.classList.add('mrev');
      }
      reviewBarSync();
    }
    _instantScroll(y);                             // the reader stays put
    refresh();
    return;
  }
  if(target!=null){
    const p=MP.segs.findIndex(g=>g.index===target);
    const el=p>=0?document.getElementById('mseg'+p):null;
    if(el){
      el.scrollIntoView({block:'center'});
      el.classList.add('mrev');
      setTimeout(()=>el.classList.remove('mrev'),1200);
    }
  }
  refresh();
}

/* ------------------------------------------------------- the flag stepper -- *
 * Stepping is a CONVEYOR, the card is a WORKSPACE. While stepping, a compact
 * verdict bar docks under the sticky audio bar (under the shell header when
 * the meeting has no audio) so its controls NEVER move while the transcript
 * scrolls beneath. Bar actions are verdicts and ADVANCE (Accept, Use alt,
 * Skip, the arrows, Enter/a/u); card actions are work at a spot the reviewer
 * chose and HOLD POSITION (Save, split, remove, insert). */
// the strip is the controller: the first click starts on the first flag,
// later clicks (and n/p) step through the rest
function reviewStep(){if(MR&&MR.active)reviewGo(1);else reviewStart(0);}
/* the server fronts substantial flags (its list groups by weight); the walk
 * must read the TRANSCRIPT top to bottom, so the list re-sorts to document
 * order -- start time, original index as the tiebreak -- at fetch and reload */
function _revOrder(items){
  return (items||[]).slice().sort((a,b)=>(a.start-b.start)||(a.index-b.index));
}
async function reviewStart(at){
  if(!MP||!MP.built)return;
  const base=MP.base;
  let d;
  try{d=await api('/api/review?base='+encodeURIComponent(base));}
  catch(e){d={error:'The review list could not be loaded.'};}
  if(route.view!=='meeting'||route.base!==base)return;
  if(d.error)return;
  if(!d.items||!d.items.length){MR=null;mSyncFlagStrip();refresh();return;}
  MR={items:_revOrder(d.items),i:Math.min(at||0,d.items.length-1),active:true,
      busy:false,hold:false,at:null};       // i=0 is the FIRST flag on the page
  // the review list's speaker options are fresher than the page load's copy
  if(d.speakers&&d.speakers.length)MP.spkOpts=d.speakers;
  if(d.people)MP.people=d.people;
  reviewBarMount();
  renderReviewCard();
}
function reviewGo(d){
  if(!MR||!MR.active||MR.busy)return;   // a verdict in flight owns MR.i
  if(!MR.items.length){reviewExit();return;}     // everything resolved: done
  if(MR.hold){
    // the position was held on a resolved spot: MR.i already names the next
    // unresolved item, so forward just lands there; back takes the one before
    MR.hold=false;
    MR.i=d<0?(MR.i-1+MR.items.length)%MR.items.length:MR.i%MR.items.length;
  }else MR.i=(MR.i+d+MR.items.length)%MR.items.length;   // wraps at both ends
  renderReviewCard();
}
function reviewExit(){MR=null;reviewBarUnmount();mCloseCard();mSyncFlagStrip();}

/* ------------------------------------------------- the sticky verdict bar -- */
function _mRevBar(){
  return `<div class="mrevbar" id="mrevbar" role="toolbar" aria-label="Step through the flagged lines">
    <span class="mrbcount" id="mrbcount"></span>
    <button class="iact" type="button" onclick="reviewGo(-1)" title="Previous flag (p)">&#8249;</button>
    <button class="iact" type="button" onclick="reviewGo(1)" title="Next flag (n)">&#8250;</button>
    ${MP.hasAudio?`<button class="iact" type="button" onclick="reviewPlay()"
      title="Play this flag&#8217;s stretch of the recording">&#9654; Play</button>`:''}
    <button class="btn primary mini" id="mrbaccept" type="button" onclick="reviewApply('accept')"
      title="This line is accurate: clear the flag and go to the next (Enter or a)">&#10003; Accept</button>
    <button class="btn mini" id="mrbalt" type="button" onclick="reviewUseAltGo()"
      title="Swap in the second engine&#8217;s version and go to the next (u)">Use alt</button>
    <button class="btn mini" type="button" onclick="reviewGo(1)"
      title="Leave it flagged and look at the next one (n)">Skip</button>
    <button class="iact mrbdone" type="button" onclick="reviewExit()"
      title="Stop stepping; unresolved lines stay flagged (Escape)">&#10005; done</button>
    <span class="grow"></span>
    <button class="btn mini" id="mrbminor" type="button" onclick="reviewAcceptMinor(this)"
      title="Sub-second crosstalk crumbs (&#8220;like&#8221;, &#8220;so&#8221;&#8230;): accept them all in one click; substantial lines stay"></button>
    <button class="btn mini" id="mrball" type="button" onclick="reviewAcceptAllAsk(this)"
      title="Accept every remaining flagged line exactly as transcribed"></button>
  </div>`;
}
function reviewBarMount(){
  if(document.getElementById('mrevbar'))return;
  const anchor=document.getElementById('maudio')
    ||document.querySelector('#meetingpage .mhead');
  if(!anchor)return;
  anchor.insertAdjacentHTML('afterend',_mRevBar());
  const strip=document.getElementById('mflagbar');
  if(strip)strip.hidden=true;                    // the bar replaces the strip
  mStickyTop();
  reviewBarSync();
}
function reviewBarUnmount(){
  const b=document.getElementById('mrevbar');if(b)b.remove();
  const strip=document.getElementById('mflagbar');
  if(strip)strip.hidden=false;                   // the strip returns on exit
}
/* the bar re-derives everything visible from MR after every step/resolution:
 * position counter, alt availability, the minor and accept-all counts */
function reviewBarSync(){
  if(!MR||!document.getElementById('mrevbar'))return;
  const n=MR.items.length,it=MR.hold?null:MR.items[MR.i];
  const c=document.getElementById('mrbcount');
  if(c)c.innerHTML=MR.hold
    ?`&#10003; saved &middot; ${n} left`
    :`&#9888; ${MR.i+1} of ${n}`;
  const alt=document.getElementById('mrbalt');
  if(alt)alt.disabled=!(it&&it.alt&&it.alt.length);
  const minors=MR.items.filter(x=>x.minor).length;
  const mb=document.getElementById('mrbminor');
  if(mb){mb.hidden=!minors;if(minors)mb.innerHTML=`&#10003; Accept ${minors} minor`;}
  const all=document.getElementById('mrball');
  if(all){all.hidden=!n;if(n)all.innerHTML=`Accept all ${n} remaining&#8230;`;}
}
// the array position of a review item's segment: matched by ORIGINAL index,
// with start-time proximity as the fallback (same spirit as the server's own
// _locate_segment cross-check)
function _revPos(it){
  let p=MP.segs.findIndex(g=>g.index===it.index);
  if(p<0)p=MP.segs.findIndex(g=>Math.abs(g.start-it.start)<0.25);
  return p;
}
/* the inline card: the WORKSPACE for real edits at the flagged segment. The
 * verdict controls (prev/next, accept, skip, play) live in the bar above; the
 * card keeps the flag reason, the alt preview, speaker+text editing, and Save.
 * Save holds position (reviewSave); only the bar or its keys move on. */
function renderReviewCard(){
  const it=MR.items[MR.i];
  const p=_revPos(it);
  const el=p>=0?document.getElementById('mseg'+p):null;
  if(!el){reviewExit();return;}    // transcript changed underneath: bow out quietly
  MR.hold=false;
  MR.at={index:it.index,start:it.start};
  const alts=(it.alt||[]).map((a,k)=>`<div class="mcalt">Second engine heard
      &#8220;<b>${esc(a.theirs||'(nothing)')}</b>&#8221; where this says
      &#8220;${esc(a.ours||'(nothing)')}&#8221;
      <button class="iact" type="button" onclick="reviewUseAlt(${k})"
        title="Swap the second engine&#8217;s version into the text below">Use it</button></div>`).join('');
  const card=mCardEl(`
    <div class="mcrow mchead">
      <span class="mcflag">&#9888; ${esc(it.flags.join(', '))}${it.minor?' &middot; minor':''}</span>
    </div>
    ${alts}
    <div class="mcrow">
      ${mSpkSelect('mcspk',it.speaker)}
      <textarea id="mctext" class="mctext" aria-label="Corrected text">${esc(it.text)}</textarea>
    </div>
    <div class="mcrow mcbtns">
      <span class="grow"></span>
      <button class="btn primary mini" type="button" onclick="reviewSave()"
        title="Save the corrected speaker/text and stay here (&#8984;Enter); step on with the bar">Save changes</button>
    </div>
    <div id="mcerr" class="mcerr" hidden></div>`,el,'mrevcard');
  el.classList.add('mrev');   // AFTER mCardEl: its mCloseCard clears the marks
  mWireNew(card.querySelector('#mcspk'));
  el.scrollIntoView({block:'center'});
  reviewPlay();
  mSyncFlagStrip();
  reviewBarSync();
}
function reviewUseAlt(k){
  const a=MR.items[MR.i].alt[k],ta=$('#mctext');
  if(!a||!ta)return;
  // "ours" is normalized tokens: match them loosely against the display text
  const pat=a.ours.trim().split(/\s+/).map(t=>t.replace(/[.*+?^$()|[\]\\{}]/g,'\\$&')).join("[^A-Za-z0-9']+");
  const re=pat?new RegExp(pat,'i'):null;
  if(re&&re.test(ta.value))ta.value=ta.value.replace(re,a.theirs);
  else ta.value=(ta.value+' '+a.theirs).trim();
}
function reviewPlay(){
  const it=MR&&MR.items[MR.i];
  if(it&&MP&&MP.hasAudio)mPlaySpan(it.start,it.end);
}
/* a BAR verdict on the current flag: resolves it and ADVANCES to the next
 * unresolved one (wrapping past the end). Card work goes through reviewSave
 * below, which holds position instead. */
async function reviewApply(action){
  if(!MR||!MR.active||MR.busy)return;
  if(MR.hold){reviewGo(1);return;}   // the held spot is already resolved: move on
  const it=MR.items[MR.i];
  if(!it)return;
  // the item carries the segment's ORIGINAL json index; start is the server's
  // cross-check in case the file changed since the list was fetched
  const body={base:MP.base,index:it.index,start:it.start,action};
  if(action==='edit'){
    const v=$('#mcspk').value;
    if(v==='__new__'){mCardErr('Pick or name the speaker first.');return;}
    body.text=$('#mctext').value;body.speaker=v;
  }
  MR.busy=true;
  let r;
  try{r=await api('/api/review',body);}
  catch(e){r={error:'Save failed'};}
  if(MR)MR.busy=false;
  if(!MP||route.view!=='meeting')return;   // navigated away while saving
  if(!r.ok){mCardErr(r.error||'Save failed');return;}
  if(r.merged){
    // the reassignment folded neighbors into one turn: every index in this
    // pre-fetched list may be stale. mReloadSegs refetches the list and
    // re-anchors the stepping position in place.
    await mReloadSegs(r.index);
    return;
  }
  if(action==='edit'&&(body.speaker||'').startsWith('name:')){
    await mReloadSegs();             // a new person joined: fresh legend/colors
    return;                          // (stepping re-anchored inside)
  }
  const p=_revPos(it);
  if(p>=0){
    const g=MP.segs[p];
    if(action==='edit'){
      g.text=body.text;g.edited=true;
      const sp=MP.spkOpts.find(s=>s.id===body.speaker);
      if(sp){g.who=sp.display;g.speaker=sp.id;}
    }
    g.flags=[];
    mRefreshSeg(p);
  }
  if(!MR||!MR.active){mSyncFlagStrip();refresh();return;}  // exited mid-save:
                                                           // counts stay honest
  MR.items.splice(MR.i,1);           // resolved: counts update optimistically
  if(!MR.items.length){reviewExit();refresh();return;}
  if(MR.i>=MR.items.length)MR.i=0;   // the next unresolved is the first: wrap
  renderReviewCard();
}
/* Save from the card: the card is a workspace, so the position HOLDS. The
 * card stays open at this segment, updated in place; the counter and strip
 * still drop optimistically when the save resolves the flag; stepping
 * moves on only from the bar (or Enter/n) afterwards. */
async function reviewSave(){
  if(!MR||!MR.active||MR.busy)return;
  const at=MR.at;if(!at)return;
  const v=$('#mcspk')?$('#mcspk').value:null;
  if(v==='__new__'){mCardErr('Pick or name the speaker first.');return;}
  const body={base:MP.base,index:at.index,start:at.start,action:'edit',
    text:$('#mctext')?$('#mctext').value:'',speaker:v};
  MR.busy=true;
  let r;
  try{r=await api('/api/review',body);}
  catch(e){r={error:'Save failed'};}
  if(MR)MR.busy=false;
  if(!MP||route.view!=='meeting')return;   // navigated away while saving
  if(!r.ok){mCardErr(r.error||'Save failed');return;}
  if(r.merged){await mReloadSegs(r.index);return;}   // position re-anchored inside
  if((v||'').startsWith('name:')){await mReloadSegs(at.index);return;}
  const p=_revPos(at);
  if(p>=0){
    const g=MP.segs[p];
    g.text=body.text;g.edited=true;g.flags=[];
    const sp=MP.spkOpts.find(s=>s.id===v);
    if(sp){g.who=sp.display;g.speaker=sp.id;}
    mRefreshSeg(p);
    const el=document.getElementById('mseg'+p);
    if(el)el.classList.add('mrev');                  // still the held spot
  }
  if(!MR||!MR.active){mSyncFlagStrip();refresh();return;}  // exited mid-save
  // resolve the item WITHOUT moving: MR.i comes to name the next unresolved
  const k=MR.items.findIndex(x=>x.index===at.index);
  if(k>=0){
    MR.items.splice(k,1);
    if(MR.i>k)MR.i--;
    else if(MR.i===k){MR.hold=true;if(MR.i>=MR.items.length)MR.i=0;}
  }
  const f=document.querySelector('#mcard .mcflag');
  if(f)f.innerHTML='&#10003; saved &middot; edit on, or step ahead with the bar';
  document.querySelectorAll('#mcard .mcalt').forEach(x=>x.remove());
  mSyncFlagStrip();reviewBarSync();refresh();
}
/* the bar's one-click alternative: swap the second engine's text into the
 * card and save it -- a verdict, so it advances like Accept */
function reviewUseAltGo(){
  if(!MR||!MR.active||MR.busy||MR.hold)return;
  const it=MR.items[MR.i];
  if(!it||!it.alt||!it.alt.length||!$('#mctext'))return;
  it.alt.forEach((_,k)=>reviewUseAlt(k));
  reviewApply('edit');
}
/* accept-all: the FAR-end escape hatch behind the house two-step (never a
 * native dialog). Accepts every remaining flag as-is, one existing accept
 * POST per item, with the bar's counts dropping as each one lands. */
function reviewAcceptAllAsk(btn){
  if(!MR||!MR.active||MR.busy||!MR.items.length)return;
  const n=MR.items.length;
  const pop=$('#rowmenu');
  openPop(pop,btn,()=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Accept all ${n} remaining flag${n>1?'s':''}?</div>
      <div class="ppcnote">Every remaining flagged line is kept exactly as transcribed,
        wrong words and uncertain speakers included, and its flag is cleared.
        Lines you have not listened to are trusted as-is. Any line can still be
        edited later with its pencil.</div>
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn primary mini" type="button" onclick="reviewAcceptAllGo(this)">Accept all ${n}</button>
      </div></div>`;
  });
}
async function reviewAcceptAllGo(btn){
  if(!MR||!MR.active||MR.busy)return;
  if(btn){btn.disabled=true;btn.innerHTML='Accepting&#8230;';}
  MR.busy=true;
  const base=MP.base;
  for(const it of MR.items.slice()){
    let r;
    try{r=await api('/api/review',{base,index:it.index,start:it.start,action:'accept'});}
    catch(e){r={error:'The server did not answer.'};}
    if(route.view!=='meeting'||route.base!==base||!MR||!MR.active)return;
    if(!r.ok){
      MR.busy=false;
      const pop=$('#rowmenu');
      if(pop&&!pop.hidden)pop.innerHTML=`<div class="ppconfirm">
        <div class="ppctitle">Could not accept every flag</div>
        <div class="ppcnote err">${esc(r.error||'A line failed to accept.')} The rest stay flagged.</div>
        <div class="ppcrow"><button class="btn mini" type="button" onclick="closePop()">Close</button></div></div>`;
      mSyncFlagStrip();reviewBarSync();refresh();
      return;
    }
    const p=_revPos(it);
    if(p>=0){MP.segs[p].flags=[];mRefreshSeg(p);}
    const k=MR.items.indexOf(it);
    if(k>=0){MR.items.splice(k,1);if(MR.i>k)MR.i--;}
    reviewBarSync();                 // the count drops as each accept lands
  }
  MR.busy=false;
  closePop();
  reviewExit();
  refresh();
}
// one click on the strip accepts every sub-second crumb; substantial items stay
async function reviewAcceptMinor(btn){
  if(!MP)return;
  if(btn){btn.disabled=true;btn.innerHTML='Accepting&#8230;';}
  const r=await api('/api/review',{base:MP.base,action:'accept_minor'});
  if(!r.ok){
    if(btn){btn.disabled=false;btn.textContent='Retry';btn.title=r.error||'Could not accept the minor lines.';}
    return;
  }
  MP.segs.forEach((g,p)=>{           // clear the amber wash on each crumb locally
    if(g.flags&&g.flags.length&&mIsMinor(g)){g.flags=[];mRefreshSeg(p);}
  });
  if(MR&&MR.active){
    const cur=MR.hold?null:MR.items[MR.i];
    MR.items=MR.items.filter(x=>!x.minor);
    if(!MR.items.length&&!MR.hold)reviewExit();
    else if(cur&&!cur.minor){
      // the spot we are on survives: NEVER rebuild its card (it may hold an
      // edit in progress); only the counts move
      MR.i=MR.items.indexOf(cur);
      reviewBarSync();
    }else if(cur){
      // the spot we were on was itself a crumb and just got accepted
      MR.i=Math.min(MR.i,MR.items.length-1);
      renderReviewCard();
    }else{
      // held position: stay put, re-aim MR.i at a surviving item
      MR.i=Math.max(0,Math.min(MR.i,MR.items.length-1));
      reviewBarSync();
    }
  }
  mSyncFlagStrip();
  refresh();
}

/* ----------------------------------------------------- the edit card ------- */
function mEdit(i,ev){
  if(ev)ev.stopPropagation();
  // ONE card at a time (mCardEl swaps any open review card out), but stepping
  // stays armed: the editor is workspace at a spot the user chose, and the
  // bar still owns moving on
  const g=MP.segs[i],el=document.getElementById('mseg'+i);
  if(!g||!el)return;
  const defEng=(S&&S.model==='parakeet')?'mlxwhisper:large-v3':'parakeet';
  const card=mCardEl(`
    <div class="mcrow mchead">
      ${mSpkSelect('mcspk',g.speaker)}
      <select id="mceng" class="mcsel" title="Which engine listens again; a different one from the original gives an independent second opinion">
        ${ENGINES.map(([v,l])=>`<option value="${v}" ${v===defEng?'selected':''}>${l}</option>`).join('')}
      </select>
      <button class="iact" type="button" onclick="mRetrans(${i},event)"
        title="Listen to this span again with the chosen engine and propose corrected text">&#8635; Re-transcribe</button>
      <span id="mcrx" class="mcnote"></span>
    </div>
    <textarea id="mctext" class="mctext" aria-label="Corrected text">${esc(g.text)}</textarea>
    <div class="mcrow mcbtns">
      <button class="iact" type="button" onclick="mDeleteAsk(${i},event)"
        title="Remove this line entirely (echo, noise heard as speech)">Remove line</button>
      <button class="iact" type="button" onclick="mSplitUI(${i},event)"
        title="Split this line in two: click inside the text where the second voice starts, then press this">&#9986; Split line</button>
      <span class="grow"></span>
      ${MP.hasAudio?`<button class="iact" type="button" onclick="mPlaySpanBtn(${i},event)">&#9654; Play span</button>`:''}
      <button class="btn mini" type="button" onclick="mCloseCard()">Cancel</button>
      <button class="btn primary mini" type="button" onclick="mEditSave(${i},event)">Save</button>
    </div>
    <div id="mcerr" class="mcerr" hidden></div>`,el);
  el.classList.add('editing');
  mWireNew(card.querySelector('#mcspk'));
}
async function mEditSave(i,ev){
  if(ev)ev.stopPropagation();
  const g=MP.segs[i],spk=$('#mcspk')?$('#mcspk').value:null;
  if(spk==='__new__'){mCardErr('Pick or name the speaker first.');return;}
  const text=$('#mctext')?$('#mctext').value:g.text;
  // the segment's ORIGINAL json index rides along, never the array position
  const r=await api('/api/review',{base:MP.base,index:g.index,start:g.start,action:'edit',text,speaker:spk});
  if(!r.ok){mCardErr(r.error||'Save failed');return;}
  if(r.merged){mReloadSegs(r.index);return;}     // neighbors folded into one turn
  if(spk&&spk.startsWith('name:')){mReloadSegs(g.index);return;}  // fresh legend/colors
  const sp=MP.spkOpts.find(s=>s.id===spk);
  g.text=text;g.flags=[];g.edited=true;
  if(sp){g.who=sp.display;g.speaker=sp.id;}
  mCloseCard();
  mRefreshSeg(i);
  if(MR&&MR.active){
    // editing resolved this line's flag server-side: drop it from the walk
    // WITHOUT moving (card work holds position; the bar moves on)
    const k=MR.items.findIndex(x=>x.index===g.index);
    if(k>=0){
      MR.items.splice(k,1);
      if(MR.i>k)MR.i--;
      else if(MR.i===k){MR.hold=true;if(MR.i>=MR.items.length)MR.i=0;}
    }
    const el=document.getElementById('mseg'+i);
    if(el&&MR.at&&Math.abs(MR.at.start-g.start)<0.25)el.classList.add('mrev');
    reviewBarSync();
  }
  mSyncFlagStrip();
}
// two-step removal, inside the card (house style: never a native dialog)
function mDeleteAsk(i,ev){
  if(ev)ev.stopPropagation();
  const e=document.getElementById('mcerr');
  if(!e)return;
  e.hidden=false;
  e.innerHTML=`<span>Remove this line from the transcript? Its audio stays; only the text line goes.</span>
    <button class="btn mini" type="button" onclick="mCardErr('')">Keep it</button>
    <button class="btn danger mini" type="button" onclick="mDeleteGo(${i})">Remove line</button>`;
}
async function mDeleteGo(i){
  const g=MP.segs[i];
  const r=await api('/api/review',{base:MP.base,action:'delete',index:g.index,start:g.start});
  if(!r.ok){mCardErr(r.error||'Could not remove the line.');return;}
  mReloadSegs();
}
// split: the card morphs into two speaker/text halves, cut at the caret
function mSplitUI(i,ev){
  if(ev)ev.stopPropagation();
  const ta=$('#mctext'),pos=ta?(ta.selectionStart||0):0,full=ta?ta.value:'';
  const a=full.slice(0,pos).trim(),b=full.slice(pos).trim();
  if(!a||!b){mCardErr('Click inside the text where the split should happen (some words before the cursor, some after), then press Split again.');return;}
  const g=MP.segs[i],card=document.getElementById('mcard');
  if(!card)return;
  card.innerHTML=`
    <div class="mcnote">Splitting this line in two: set each half&#8217;s speaker. If a half matches its neighbor&#8217;s speaker, they join into one turn automatically.</div>
    <div class="mcrow">${mSpkSelect('mcsa',g.speaker)}<textarea id="mcta1" class="mctext" aria-label="First half">${esc(a)}</textarea></div>
    <div class="mcrow">${mSpkSelect('mcsb',g.speaker)}<textarea id="mcta2" class="mctext" aria-label="Second half">${esc(b)}</textarea></div>
    <div class="mcrow mcbtns"><span class="grow"></span>
      <button class="btn mini" type="button" onclick="mCloseCard()">Cancel</button>
      <button class="btn primary mini" type="button" onclick="mSplitSave(${i})">Split</button></div>
    <div id="mcerr" class="mcerr" hidden></div>`;
  mWireNew($('#mcsa'));mWireNew($('#mcsb'));
  $('#mcsb').focus();
}
async function mSplitSave(i){
  const g=MP.segs[i],sa=$('#mcsa').value,sb=$('#mcsb').value;
  if(sa==='__new__'||sb==='__new__'){mCardErr('Pick or name both speakers first.');return;}
  const r=await api('/api/review',{base:MP.base,action:'split',index:g.index,start:g.start,
    text_a:$('#mcta1').value,text_b:$('#mcta2').value,speaker_a:sa,speaker_b:sb});
  if(!r.ok){mCardErr(r.error||'Split failed');return;}
  mReloadSegs(r.index);
}
// second listen over just this span; failure surfaces inline, never a crash.
// The estimate comes from THIS machine's own past re-transcriptions
// (localStorage median per engine), exactly like the old reader.
async function mRetrans(i,ev){
  if(ev)ev.stopPropagation();
  const g=MP.segs[i],eng=$('#mceng')?$('#mceng').value:'parakeet';
  const hist=JSON.parse(localStorage.getItem('stt_retrans_secs')||'{}');
  const past=(hist[eng]||[]).slice().sort((a,b)=>a-b);
  const estTxt=past.length?` (~${Math.round(past[Math.floor(past.length/2)])}s on this Mac)`:'';
  const rx=$('#mcrx');
  if(rx)rx.innerHTML='<span class="spin"></span> listening again&#8230;'+esc(estTxt);
  const t0=Date.now();
  let r;
  try{r=await api('/api/retranscribe',{base:MP.base,start:g.start,end:g.end,engine:eng});}
  catch(e){r={error:'re-transcription failed'};}
  hist[eng]=((hist[eng]||[]).concat((Date.now()-t0)/1000)).slice(-5);
  localStorage.setItem('stt_retrans_secs',JSON.stringify(hist));
  const rx2=$('#mcrx');if(!rx2)return;             // the card closed while waiting
  if(r.error){rx2.textContent='failed: '+r.error;return;}
  const ta=$('#mctext');if(ta)ta.value=r.text;
  rx2.textContent='proposed by '+(r.engine||'second engine')+': edit if needed, then Save';
}

/* ------------------------------------------------- inserting missed lines -- */
function mFmtT(t){const m=Math.floor(t/60),s=Math.floor(t%60);return m+':'+String(s).padStart(2,'0');}
function mParseT(v){
  const p=String(v).trim().split(':').map(Number);
  if(!p.length||p.some(isNaN))return null;
  return p.reverse().reduce((acc,x,k)=>acc+x*Math.pow(60,k),0);
}
function mInsertAt(i,at){            // i = the gap's index (-1 before the first line)
  // stepping survives an insert: the position re-anchors after the reload
  const g=MP.segs[i];                // undefined for i=-1
  const start=at!=null?at:(g?g.end:0);
  const gap=document.getElementById('mgap'+i);
  if(!gap)return;
  const card=mCardEl(`
    <div class="mcrow mchead">
      ${mSpkSelect('mcnspk',null)}
      <label class="mcnote">at <input id="mcnat" class="mcat-t" value="${mFmtT(start)}" aria-label="Time the line starts (m:ss)"></label>
      ${MP.hasAudio?`<button class="iact" type="button" onclick="mListenAt()" title="Play from just before this time">&#9654; Listen here</button>`:''}
      <span class="mcnote">(tip: pause the audio where you heard it; the audio bar&#8217;s &#65291; button fills this in)</span>
    </div>
    <textarea id="mcntext" class="mctext" placeholder="What they said" aria-label="What they said"></textarea>
    <div class="mcrow mcbtns"><span class="grow"></span>
      <button class="btn mini" type="button" onclick="mCloseCard()">Cancel</button>
      <button class="btn primary mini" type="button" onclick="mInsertSave()">Add</button></div>
    <div id="mcerr" class="mcerr" hidden></div>`,gap);
  mWireNew(card.querySelector('#mcnspk'));
  $('#mcntext').focus();
}
function mListenAt(){
  const t=mParseT($('#mcnat')?$('#mcnat').value:'');
  if(t!=null)mSeek(Math.max(0,t-2));
}
async function mInsertSave(){
  const spk=$('#mcnspk').value,text=$('#mcntext').value,start=mParseT($('#mcnat').value);
  if(spk==='__new__'){mCardErr('Pick or name the speaker first.');return;}
  if(start==null){mCardErr('Time should look like 12:34.');return;}
  if(!text.trim()){mCardErr('Type what they said.');return;}
  const r=await api('/api/review',{base:MP.base,action:'insert',start,end:start+3,speaker:spk,text});
  if(!r.ok){mCardErr(r.error||'Could not add the line.');return;}
  mReloadSegs(r.index);
}
// the audio bar's affordance: insert at the playhead's moment
function mAddAtPlayhead(){
  if(!MP)return;
  const a=MP.audio,t=a?a.currentTime:0;
  const after=MP.segs.findIndex(g=>g.start>t);      // first line past the playhead
  const i=after<0?MP.segs.length-1:after-1;         // its gap (-1 = before line one)
  const el=document.getElementById('mgap'+i);
  if(el)el.scrollIntoView({block:'center'});
  mInsertAt(i,t);
}

/* ------------------------------------------------- voice-naming panel ------ *
 * A right slide-over (paper ground, hairline edge, soft shadow) replacing the
 * old page's who-is-this dialog bridge. Ported: /api/voice_clips (incl the
 * reason:"sources_deleted" contract), per-meeting snippet players, the
 * enrolled-name autocomplete (ArrowUp/Down/Enter; Escape closes the dropdown
 * only), Save name -> /api/name, "Not a real speaker" -> /api/forget. After a
 * save the panel closes and the quiet relabel note rides the polled state
 * (relabel_pending) as the lowest-priority pill. */
let NP=null;    // {uid} or {meeting, speaker} while the panel is open
// `base` is OPTIONAL context, not identity: a uid names a voice across every
// meeting, but a caller that knows WHICH meeting it is looking at (a row, the
// transcript legend) lets the panel also show what the voice said there. The
// speakers drawer has no meeting in hand, so it passes none and the lines
// block is simply omitted.
function openNamePanelByUid(uid,base){
  const u=(S&&S.unknowns||[]).find(x=>x.uid===uid);
  // the uid is GONE from the registry: this chip was rendered before the voice
  // was named (or merged, or forgotten) and the page has not caught up. Opening
  // the panel anyway produced a DEAD one -- the raw "U049" as both the title and
  // the speaker key, so no lines, no audio, and a Save that failed on a voice
  // that had in fact just been saved. Refresh the surfaces instead and say so.
  if(!u){
    toast(S&&(S.relabel_running||S.relabel_pending)
      ?'Already named. Names are being applied now.'
      :'This voice was already named.');
    refresh();
    if(route&&route.view==='meeting')mReloadSegs();
    return;
  }
  openNamePanel(uid,u.display,null,base);
}
// naming a voice the registry does NOT track (kept transcript-local by the
// minting floor, or suppressed by a "not a real speaker" tombstone): the same
// panel, but the save enrolls straight from this meeting's cluster embedding
// `local` says this cluster already carries a meeting-only name, which changes
// what the panel may offer: a locally NAMED voice is a person the human already
// identified, so "Not a real speaker" is nonsense there (the legend has no
// named-and-dismissed rendering, so the click produced no visible change at
// all). It gets Clear name instead, the documented undo.
// `ids`: every cluster of the voice (mVoiceIds), so a meeting-only name, its
// Clear, and a dismissal reach a split voice whole
function openNamePanelByCluster(meeting,speaker,display,local){
  openNamePanel(null,display,{meeting,speaker,local:!!local,
    ids:mVoiceIds(meeting,speaker)});
}
async function openNamePanel(uid,display,cluster,base){
  const panel=$('#namepanel'),veil=$('#nameveil');
  if(!panel||!veil)return;
  // uid mode keeps the meeting the caller was looking at as `base` (context,
  // not identity): the save uses it to rebuild that transcript's legend
  // immediately. It is deliberately NOT `meeting`, which is cluster mode's
  // discriminant (npDismiss guards on it and must keep failing here).
  NP=cluster?{meeting:cluster.meeting,speaker:cluster.speaker,
              local:!!cluster.local,ids:cluster.ids||[cluster.speaker]}:{uid,base};
  veil.hidden=false;panel.hidden=false;
  // FORM FIRST: the name box, the buttons, and the two messages that answer
  // them (the quality-gate confirm and the error line) sit at the top, so the
  // control you came to use is never below a scroll of evidence. The clip
  // player and the lines follow as the evidence you consult if unsure.
  panel.innerHTML=`
    <div class="nphead">
      <h2 class="nptitle">Who is ${esc(display)}?</h2>
      <button class="iact" type="button" title="Close" onclick="closeNamePanel()">&#10005;</button>
    </div>
    <p class="npnote">${cluster
      ?`Name this voice, or listen first: the clip below is their longest turn
      in this meeting. Typing an <b>existing</b> name merges this voice into
      that person. Every past and future meeting relabels automatically.`
      :`Name this voice, or listen first: the clips below are their longest turn
      in each meeting they were heard in. Typing an <b>existing</b> name merges
      this voice into that person. Every past and future meeting relabels
      automatically.`}</p>
    <div class="npfield">
      <input type="text" id="npname" placeholder="Person&#8217;s name" autocomplete="off" spellcheck="false"
        aria-label="This voice belongs to"
        oninput="pnFilter()" onfocus="pnFilter()" onblur="setTimeout(pnClose,150)" onkeydown="pnKey(event)">
      <div id="npdd" class="npdd" hidden></div>
    </div>
    <div id="nppreview" class="nppreview" hidden></div>
    <div class="npbtns">
      ${cluster
        ?(cluster.local
          // already named for this meeting only: the documented undo. An empty
          // name in the local branch clears the entry, and the voice becomes
          // an ordinary unnamed cluster again.
          ?`<button class="btn mini" type="button" id="npclear" onclick="npClearName()"
        title="Remove the meeting-only name; this voice goes back to being unnamed here">Clear name</button>`
          // per-MEETING dismissal. There is no registry entry to tombstone
          // here, and the lines this cluster owns are real audio: dismissing
          // keeps the transcript exactly as it reads and only stops the naming
          // prompts for this voice in this meeting.
          :`<button class="btn danger mini" type="button" onclick="npDismiss()"
        title="Grumbling, music, an echo: leave these lines as unknown and stop asking me to name this voice in this meeting">Not a real speaker</button>`)
        :`<button class="btn danger mini" type="button" onclick="npForget()"
        title="A false detection (music, crosstalk, an echo): remove this voice entirely">Not a real speaker</button>`}
      <span class="grow"></span>
      <button class="btn mini" type="button" onclick="closeNamePanel()">Cancel</button>
      <button class="btn primary mini" id="npsave" type="button" onclick="npSave()">Save name</button>
    </div>
    <div id="npconfirm" class="dckconfirm" hidden></div>
    <div id="nperr" class="mcerr" hidden></div>
    <div id="npclips" class="npclips"><span class="spin"></span></div>
    <div id="nplines" class="nplines"></div>`;
  $('#npname').focus();
  // what this save would reach, and who the voice already resembles. Fired and
  // forgotten: the panel is usable the instant it opens, the line appears when
  // the answer lands, and a preview that fails shows nothing at all.
  npPreview();
  if(cluster){
    // one clip, cut from THIS meeting's cluster (no registry entry to walk)
    const box0=$('#npclips');
    if(box0)box0.innerHTML=`<audio controls
      onplay="document.querySelectorAll('audio').forEach(a=>{if(a!==this)a.pause()})"
      src="/api/snippet?meeting=${encodeURIComponent(cluster.meeting)}&speaker=${encodeURIComponent(cluster.speaker)}&secs=45"></audio>`;
    npLines(cluster.meeting,cluster.speaker);
    return;
  }
  // uid mode: the lines come from whichever meeting the caller was looking at.
  // Fire and forget alongside the clips fetch, keyed on the panel identity so a
  // reopen for another voice can never paint the previous one's lines.
  npLines(base,display);
  let r;
  try{r=await api('/api/voice_clips?speaker='+encodeURIComponent(uid));}
  catch(e){r={clips:[]};}
  const box=$('#npclips');
  if(!box||!NP||NP.uid!==uid)return;   // closed, or reopened for another voice
  const clips=r.clips||[];
  box.innerHTML=clips.map(c=>{
    const m=Math.floor(c.start/60),s=String(Math.floor(c.start%60)).padStart(2,'0');
    return `<div class="npclip">
      <div class="npclip-m" title="${esc(c.meeting)}">${esc(c.meeting)}</div>
      <div class="npclip-sub">their longest turn &middot; at <span class="mono">${m}:${s}</span> &middot; ${Math.round(c.dur)}s
        <a class="npopen" href="#m/${encodeURIComponent(c.meeting)}"
          onclick="npOpenAt('${escJs(c.meeting)}',${Number(c.start)||0});return false;"
          title="Open the transcript at this moment, with the conversation around it">Open at this moment &#8594;</a></div>
      <audio controls preload="none"
        onplay="document.querySelectorAll('audio').forEach(a=>{if(a!==this)a.pause()})"
        src="/api/snippet?meeting=${encodeURIComponent(c.meeting)}&speaker=${encodeURIComponent(uid)}&secs=45"></audio>
    </div>`;}).join('')
    ||(r.reason==='sources_deleted'
       ?'<div class="npnote muted">No audio available. The source recordings were deleted.</div>'
       // every meeting this voice was heard in is ARCHIVED. The audio is still
       // there, but the clip endpoints gate on live membership, so the bare
       // player below would 404 with no explanation. Point at Restore instead.
       :r.reason==='sources_archived'
       ?'<div class="npnote muted">No audio available here. Every meeting this voice was heard in is archived. Restore one from the drawer to hear them.</div>'
       :`<audio controls
           onplay="document.querySelectorAll('audio').forEach(a=>{if(a!==this)a.pause()})"
           src="/api/snippet?speaker=${encodeURIComponent(uid)}&secs=45"></audio>`);
}
// which voice the panel is open for, as a comparable string: an async render
// that lands after the panel closed (or reopened for someone else) must drop
// its result instead of painting the wrong voice's evidence
// The MEETING is part of the identity in uid mode too: the same uid opened
// from a second meeting shows THAT meeting's lines, so a uid-only token let a
// late response from the FIRST meeting pass the guard and overwrite the lines
// block the reader was looking at, silently and with the wrong text.
function npToken(){
  if(!NP)return '';
  return NP.uid?'uid\x00'+NP.uid+'\x00'+(NP.base||'')
               :'cl\x00'+NP.meeting+'\x00'+NP.speaker;
}
// The evidence block: what this voice actually SAID. Most unnamed voices are
// grumbling, a cough, or a stray "mhm", and that is obvious from the text in a
// second -- without it the only way to tell noise from a person is to play the
// clip. Longest turns first, because that is where a real sentence shows up.
// SILENT on failure: an error message where the evidence goes helps nobody. On
// a successful fetch that returns NOTHING it says so in a dim line -- "no text"
// is real evidence about the voice (that is what a cough looks like), and an
// empty gap reads as a block that failed to load.
async function npLines(base,speaker){
  if(!base||!speaker)return;
  const tok=npToken();
  let r;
  try{r=await api('/api/voice_lines?base='+encodeURIComponent(base)
    +'&speaker='+encodeURIComponent(speaker));}
  catch(e){return;}
  const box=$('#nplines');
  if(!box||!NP||npToken()!==tok)return;
  // only a SUCCESSFUL fetch may assert "no text": the server answers errors
  // with parseable JSON ({error}, non-200), which lands here too -- and a
  // mid-relabel 404 must not read as "this voice is just a cough"
  if(!r||r.error||!Array.isArray(r.lines))return;
  const lines=r.lines;
  if(!lines.length){
    box.innerHTML='<div class="npnote muted">No transcript text was attributed to this voice.</div>';
    return;
  }
  const n=r.n||lines.length,secs=Math.round(r.talk_secs||0);
  box.innerHTML=`<div class="nplhead">What this voice said (longest first)</div>`
    +lines.map(l=>{
      const m=Math.floor(l.start/60),s=String(Math.floor(l.start%60)).padStart(2,'0');
      return `<div class="npline"><span class="npltime mono">${m}:${s}</span><span
        class="npltext">${esc(l.text)}</span></div>`;
    }).join('')
    +`<div class="npltot">${n} segment${n!==1?'s':''}, ${secs}s of speech</div>`;
}
/* ------------------------------------------------- the naming preview ----- *
 * One quiet line under the name box saying what Save will actually reach, and
 * -- when this voice already resembles somebody enrolled -- a row of chips
 * carrying that evidence. Before this, a save said what it had done only after
 * it was done, and the nineteen clusters the margin rule refuses (0.61 to 0.84
 * against an enrolled print, audit of 2026-09-13) were invisible: the panel
 * showed a stranger where the server already held a strong guess.
 * NON-BLOCKING by construction: the panel renders and focuses first, this
 * fills in behind it, and a failed preview paints nothing rather than an
 * apology where the evidence goes. */
function npPreviewLine(p){
  const here=(p&&p.here)||null;
  const n=(((p&&p.targets)||[]).filter(t=>t!==here)).length;
  return n?`Names this voice here and in ${n} other meeting${n!==1?'s':''}`
          :'Names this voice here only';
}
function npPreviewHTML(p){
  const chips=((p&&p.looks_like)||[]).map(x=>
    `<button class="nchip" type="button" onclick="npUseName('${escJs(x.name)}')"
      title="Fill the name box with this match; nothing is saved until you press Save name"
      >Looks like ${esc(x.name)} &middot; <span class="mono">${(+x.score).toFixed(2)}</span></button>`).join('');
  return `<div class="nppline">${esc(npPreviewLine(p))}</div>`
    +(chips?`<div class="nchips nppchips">${chips}</div>`:'');
}
// a chip fills the box and leaves the save to the human: this is a guess the
// margin rule already refused to act on by itself
function npUseName(name){
  const i=$('#npname');
  if(!i)return;
  i.value=name;
  i.focus();
  pnClose();
}
async function npPreview(){
  if(!NP)return;
  const tok=npToken();
  const payload=NP.uid?{uid:NP.uid}:{meeting:NP.meeting,speaker:NP.speaker};
  let r;
  try{r=await api('/api/name_preview',payload);}
  catch(e){return;}
  const box=$('#nppreview');
  // closed, or reopened for another voice, or the server said no: paint nothing
  if(!box||!NP||npToken()!==tok||!r||r.error||!Array.isArray(r.targets))return;
  box.innerHTML=npPreviewHTML(r);
  box.hidden=false;
}
function npOpenAt(meeting,t){closeNamePanel();openHit(meeting,t);}
function closeNamePanel(){
  NP=null;
  const panel=$('#namepanel'),veil=$('#nameveil');
  if(!panel)return;
  panel.querySelectorAll('audio').forEach(a=>{try{a.pause();}catch(e){}});
  panel.hidden=true;panel.innerHTML='';
  if(veil)veil.hidden=true;
}
/* ONE naming request at a time. The Save button disables itself, but the floor
 * strip's "Name for this meeting" and the warn strip's "Save anyway" are
 * separate buttons carrying no guard of their own: a double click on either
 * fired two independent saves, and the local-name and non-uid enroll paths have
 * no registry lock server-side to catch the second one. npBusy owns the flag
 * AND disables whichever confirm buttons are on screen. */
let NPBUSY=false;
function npConfirmBtns(){
  const w=$('#npconfirm');
  const btns=(w&&w.querySelectorAll)?[...w.querySelectorAll('button')]:[];
  const c=$('#npclear');
  return c?btns.concat([c]):btns;
}
function npBusy(on){
  NPBUSY=!!on;
  npConfirmBtns().forEach(b=>{b.disabled=!!on;});
}
async function npSave(force,local){
  if(!NP||NPBUSY)return;
  // captured before the await, exactly like npDismiss: the panel can close
  // (Cancel/Escape) or reopen for another voice while the request is in flight,
  // and NP would then be null or someone else's identity when the answer lands.
  // The BUTTON too: a fresh lookup after a reopen would re-enable the NEW
  // voice's Save button, and leave this one stuck reading "Saving...".
  const uid=NP.uid,meeting=NP.meeting||NP.base||'',speaker=NP.speaker,speakers=NP.ids;
  const tok=npToken();   // WHICH panel this save belongs to (see npToken)
  const r=await saveName({uid,meeting,speaker,speakers,force,local,again:'npSave',busy:npBusy,
    ui:{name:$('#npname'),err:$('#nperr'),warn:$('#npconfirm'),warnId:'npconfirm',
        btn:$('#npsave'),label:'Save name'}});
  if(!r)return;
  const onIt=MP&&MP.base===meeting;
  // close ONLY if the panel still shows the voice this save acted on. A slow
  // request used to force-close whatever panel happened to be open when it
  // landed, wiping a name the user was in the middle of typing for a second,
  // unrelated voice.
  if(npToken()===tok)closeNamePanel();
  refresh();   // the quiet relabel note rides the next poll
  // the transcript for that meeting is open: rebuild it NOW. The relabel that
  // applies the new name everywhere takes a while, but the legend's "?" chip
  // must stop asking the moment the save succeeds, or the click reads as failed
  // and the next one lands on a uid that no longer exists.
  if(onIt)mReloadSegs();
}
// the after-save sentence, from the response's own fields: the one thing a
// save is allowed to say (the slide-over, the finish strip and the drawer's
// Link all say it). "here" only when the server says it APPLIED `here`, the
// meeting the save was made from: a busy relabel lock, or no page open,
// leaves that meeting queued with the rest, and the toast must not call it
// done. ['--all'] is the fallback pass when the index could not be built.
function namedToast(r,here){
  if(!r||!r.targets)return;
  const t=r.targets,n=t.length,done=!!here&&r.applied===here;
  const rest=t.includes('--all')?'Updating all meetings.'
    :n?`Updating ${n} ${done?'other ':''}meeting${n===1?'':'s'}.`:'';
  toast((done?'Named here.':'Named.')+(rest?' '+rest:''));
}
/* ONE naming save, shared by the slide-over (npSave) and the finish strip's
 * Voices step (fvSave). The caller captures the voice's identity before the
 * await and hands over its own elements: the name box, the error line, the
 * confirm strip (and its id, for the strip's Cancel), and the Save button with
 * its resting label. `again` names the caller's own save function, which the
 * floor strip and the warn strip call back with (false,true) and (true).
 * `busy` owns the one-request-at-a-time flag and disables the confirm buttons.
 * Resolves to the response on success, after the toast that says what it did,
 * or null after painting why not. */
async function saveName(o){
  const force=o.force,local=o.local,ui=o.ui||{};
  const n=(ui.name?ui.name.value:'').trim();
  const err=ui.err,warn=ui.warn;
  if(!n){if(err){err.hidden=false;err.textContent='Type a name first.';}return null;}
  const uid=o.uid,meeting=o.meeting,speaker=o.speaker,speakers=o.speakers;
  // the meeting this save relabels first. A cluster save names its own
  // meeting. A uid save names a registry voice, but the page open under it
  // still has to change FIRST: sent along, the server relabels it inside the
  // response instead of queueing it behind a toast that said "here".
  // Captured before the await, like the identity above.
  const here=uid?((MP&&MP.base)||''):meeting;
  const body=uid?{uid:uid,name:n,confirm:!!force}
    :{meeting:meeting,speaker:speaker,name:n,confirm:!!force,local:!!local};
  if(uid&&here)body.meeting=here;
  // a meeting-only name for a split voice with no uid names EVERY cluster of
  // it in one request: sent for the first alone, the other cluster still
  // counted as unnamed. An enrollment keeps one speaker: the relabel it
  // triggers finds the twin by its voice.
  if(!uid&&local&&speakers&&speakers.length>1)body.speakers=speakers.slice();
  const btn=ui.btn;if(btn){btn.disabled=true;btn.innerHTML='Saving&#8230;';}
  o.busy(true);
  let r=null;
  try{
    r=await api('/api/name',body);
  }catch(e){
    r=null;    // the fetch itself failed (server gone, connection dropped)
  }finally{
    o.busy(false);
    if(btn){btn.disabled=false;btn.textContent=ui.label;}
  }
  if(!r){
    if(err){err.hidden=false;err.textContent='Could not reach the server.';}
    return null;
  }
  if(!r.ok){
    if(r.floor&&warn){
      // the quality floor refused to LEARN the voice, but the transcript is
      // still the human's to label: offer the meeting-only save. This is the
      // opposite of the panel's merge promise, so it says so plainly.
      if(err)err.hidden=true;
      warn.hidden=false;
      warn.innerHTML=`${esc(r.error||'Too little speech to learn this voice.')}
        You can still label them here. Save for this meeting only. The system
        does not learn this voice.
        <button class="btn mini" type="button" onclick="$('#${ui.warnId}').hidden=true">Cancel</button>
        <button class="btn primary mini" type="button" onclick="${o.again}(false,true)">Name for this meeting</button>`;
      return null;
    }
    if(r.warn&&warn){
      // the enrollment quality gate: the house two-step confirm, numbers shown
      if(err)err.hidden=true;
      warn.hidden=false;
      warn.innerHTML=`This voice matches ${esc(n)}&#8217;s saved samples at only
        <b>${(+r.own).toFixed(2)}</b>${r.cross_name&&+r.cross>+r.own
          ?` &mdash; and matches ${esc(r.cross_name)} better (<b>${(+r.cross).toFixed(2)}</b>)`:''}.
        It may be the wrong person.
        <button class="btn mini" type="button" onclick="$('#${ui.warnId}').hidden=true">Cancel</button>
        <button class="btn primary mini" type="button" onclick="${o.again}(true)">Save anyway</button>`;
      return null;
    }
    if(err){err.hidden=false;err.textContent=r.error||'Could not save the name.';}
    return null;
  }
  if(warn)warn.hidden=true;
  // the response says what it did and WHERE: the meeting it relabeled here,
  // if any, and the short list of other meetings the naming resolved to
  // instead of the whole library. Said, so a save never reads as a silent
  // seven-minute wait.
  namedToast(r,here);
  return r;
}
// the documented undo for a meeting-local label: an EMPTY name in the local
// branch drops the entry, and the server allows an empty name only there
async function npClearName(){
  if(!NP||!NP.meeting||NPBUSY)return;
  const meeting=NP.meeting,speaker=NP.speaker,tok=npToken();
  const ids=NP.ids||[speaker];
  const err=$('#nperr');
  npBusy(true);
  let r=null;
  const body={meeting:meeting,speaker:speaker,name:'',local:true};
  if(ids.length>1)body.speakers=ids.slice();   // the whole voice, as the save named it
  try{r=await api('/api/name',body);}
  catch(e){r=null;}
  finally{npBusy(false);}
  if(!r||!r.ok){
    if(err){err.hidden=false;
      err.textContent=(r&&r.error)||'Could not clear this name.';}
    return;
  }
  const onIt=MP&&MP.base===meeting;
  if(npToken()===tok)closeNamePanel();
  refresh();
  if(onIt)mReloadSegs();
}
async function npForget(){
  if(!NP||NPBUSY)return;
  const uid=NP.uid,tok=npToken();
  npBusy(true);
  let r=null;
  try{r=await api('/api/forget',{uid:uid});}
  catch(e){r=null;}
  finally{npBusy(false);}
  if(!r||!r.ok){const err=$('#nperr');if(err){err.hidden=false;err.textContent=(r&&r.error)||'Could not remove this voice.';}return;}
  if(npToken()===tok)closeNamePanel();   // only if this voice is still shown
  refresh();
}
// cluster mode's "Not a real speaker": nothing is deleted and nothing is
// relabeled. The lines keep their attribution ("leave it as unknown"); the
// server drops the id out of unnamed_clusters / speaker_options, which is what
// makes every "?" for this voice disappear in this meeting.
async function npDismiss(){
  if(!NP||!NP.meeting||NPBUSY)return;
  // captured before the await: the panel can close (Cancel/Escape) or reopen
  // for a different voice while this request is in flight, and NP would then
  // be null or someone else's identity by the time the response lands
  const meeting=NP.meeting,speaker=NP.speaker,tok=npToken();
  const ids=NP.ids||[speaker];
  // every cluster of the voice, as fvForget does, or the one left over keeps
  // asking under the same chip. ONE request: the server dismisses all or none
  const body={base:meeting,speaker:speaker};
  if(ids.length>1)body.speakers=ids.slice();
  npBusy(true);
  let r=null;
  try{r=await api('/api/dismiss_voice',body);}
  catch(e){r=null;}
  finally{npBusy(false);}
  if(!r||!r.ok){const err=$('#nperr');if(err){err.hidden=false;err.textContent=(r&&r.error)||'Could not dismiss this voice.';}return;}
  const onIt=MP&&MP.base===meeting;
  // only when the panel still shows the voice that was dismissed
  if(npToken()===tok)closeNamePanel();
  refresh();
  // if the transcript for that meeting is open, rebuild its legend NOW: the
  // whole promise is that the "?" stops asking, and a chip that keeps asking
  // until the next page load reads as a failed click
  if(onIt)mReloadSegs();
}
// Escape closes the panel (the autocomplete's own Escape stops propagation
// while its dropdown is open, so that closes first)
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&NP){e.preventDefault();closeNamePanel();}
});

/* enrolled-name autocomplete, ported: ranked whole-name prefix > any word's
 * prefix > substring; ArrowUp/Down cycle; Enter picks (or saves when the
 * dropdown is closed); Escape closes only the dropdown. */
let PN={items:[],cur:-1};
function pnFilter(){
  const f=$('#npname');if(!f)return;
  const q=(f.value||'').trim().toLowerCase();
  const names=(S&&S.enrolled||[]).map(e=>e.name).sort((a,b)=>a.localeCompare(b));
  let items=q?names.filter(n=>n.toLowerCase().includes(q)):names;
  if(q){
    const rank=n=>{const l=n.toLowerCase();
      return l.startsWith(q)?0:(l.split(/\s+/).some(w=>w.startsWith(q))?1:2);};
    items=items.slice().sort((a,b)=>rank(a)-rank(b)||a.localeCompare(b));
  }
  PN={items,cur:items.length&&q?0:-1};
  pnRender();
}
function pnRender(){
  const dd=$('#npdd');if(!dd)return;
  const q=($('#npname').value||'').trim();
  const rx=q?new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i'):null;
  dd.innerHTML=PN.items.map((n,i)=>
    `<div class="pnitem${i===PN.cur?' cur':''}" onmousedown="event.preventDefault();pnPick(${i})">${
      rx?esc(n).replace(rx,m=>'<mark>'+m+'</mark>'):esc(n)}</div>`).join('');
  dd.hidden=!PN.items.length;
  const c=dd.querySelector('.pnitem.cur');if(c)c.scrollIntoView({block:'nearest'});
}
function pnPick(i){const f=$('#npname');if(!f)return;f.value=PN.items[i];pnClose();f.focus();}
function pnClose(){const dd=$('#npdd');if(dd)dd.hidden=true;PN.cur=-1;}
function pnKey(e){
  const dd=$('#npdd'),open=dd&&!dd.hidden;
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault();
    if(!open){pnFilter();return;}
    if(!PN.items.length)return;
    PN.cur=(PN.cur+(e.key==='ArrowDown'?1:-1)+PN.items.length)%PN.items.length;
    pnRender();
  }else if(e.key==='Enter'){
    e.preventDefault();
    if(open&&PN.cur>=0)pnPick(PN.cur);
    else npSave();
  }else if(e.key==='Escape'&&open){
    e.preventDefault();e.stopPropagation();pnClose();
  }
}

/* ============================================================================
   THE FINISH FLOW (docs/ui-refresh/proposal.md, phase 1). A meeting with open
   steps shows one strip at the top of its page, under the header and audio:
   Title, Voices (n), Flags (n), Done. The current step's controls render under
   the strip and each step advances on its own. Every endpoint is the one the
   rest of the shell already uses: /api/accept_meeting for the title, the
   shared saveName for a voice, /api/forget or /api/dismiss_voice for "Not a
   real speaker", and the verdict bar (reviewStart) for flags. A meeting whose
   finish record was already done when the page opened shows no strip at all.
   ============================================================================ */
const FS_LABEL={title:'Title',voices:'Voices',flags:'Flags',done:'Done'};
let FS=null;        // the open meeting's flow: {base,row,fin,past,voices,vi,
                    // cur,live,hand,looks,clip,busy}. live: the flow may take
                    // focus and play the clip on its own (a recent meeting, or
                    // one reached by a hand-off or an advance in this session)
let FS_CARRY=null;  // {base,past,live,keep,mode?,seen?,resolved?,passed?}: a flow
                    // crossing a route change (the title accept re-stamped the
                    // folder, ] / Next, or the voice queue's hand-off)

// the ordered steps with their state: done (closed), cur (the first open step
// not in `past`), open (the rest). `past` holds steps the user moved on from
// with work left (a skipped voice). Done is done only when everything is.
function finishSteps(row,past){
  const f=(row&&row.finish)||{title:true,voices:0,flags:0};
  const skip=new Set(past||[]);
  const open={title:!f.title,voices:(f.voices||0)>0,flags:(f.flags||0)>0};
  let cur=null;
  const steps=['title','voices','flags'].map(k=>{
    let state=open[k]?'open':'done';
    if(state==='open'&&!cur&&!skip.has(k)){state='cur';cur=k;}
    return {key:k,label:FS_LABEL[k],state,count:k==='title'?0:(f[k]||0)};
  });
  const all=!open.title&&!open.voices&&!open.flags;
  steps.push({key:'done',label:'Done',state:all?'done':(cur?'open':'cur'),count:0});
  return steps;
}
function fsCur(steps){const c=(steps||[]).find(s=>s.state==='cur');return c?c.key:null;}
// in Needs you: the server computes the one short list on read (a silent
// recording, audio shorter than its transcript, a title with only the
// model's draft) over meetings that arrived in the last 14 days, at most 5
// rows, and marks each row it holds. Older backlog is never handed over, and
// flags and voices never put a meeting here (unified plan rows 8 and 10).
function toFinish(row,now){
  return !!(row&&row.needs_you);
}
// the next row after the current one, in timeline order, that is to finish;
// wraps at the end and never answers the current row. dir -1 walks backward.
function nextToFinish(timeline,currentBase,dir,now){
  const rows=timeline||[],n=rows.length;
  if(!n)return null;
  const d=dir<0?-1:1;
  const i=rows.findIndex(r=>r.id===currentBase);
  for(let k=1;k<=n;k++){
    const j=i<0?(d>0?k-1:n-k):(((i+d*k)%n)+n)%n;
    const r=rows[j];
    if(!r||r.id===currentBase)continue;
    if(toFinish(r,now))return r;
  }
  return null;
}

// the voice queue ("Name N voices" on the To finish eyebrow): the meeting not
// yet visited in this run that holds the unnamed voice with the most talk
// time (finish.voice_secs). Ties keep the timeline's order. Any meeting with
// an unnamed voice counts, however old: the queue walks the whole library.
// `asked` holds the registry uids this run already named, dropped, or
// skipped: a meeting whose every open voice is one of them has nothing left
// to ask (a name's relabel lands a little after the save), so the queue
// passes it by instead of opening it for nothing.
function nextVoiceMeeting(timeline,seen,asked){
  const skip=new Set(seen||[]),done=new Set(asked||[]);
  let best=null;
  for(const r of timeline||[]){
    const f=r&&r.finish;
    if(!f||!(f.voices>0)||skip.has(r.id))continue;
    if(f.voices-(f.voice_uids||[]).filter(u=>done.has(u)).length<=0)continue;
    if(!best||(f.voice_secs||0)>(best.finish.voice_secs||0))best=r;
  }
  return best;
}
// "Name N voices": how many voices the queue will ask about, each once. A
// registry voice (finish.voice_uids) counts once however many meetings heard
// it, because naming it names it everywhere and the queue never asks about it
// again. A transcript-local voice counts once in its own meeting. The sum over
// meetings (finish.voices) overcounts the first; the tray's unknown_voice
// count misses the second and every hidden voice.
function voiceQueueCount(timeline){
  const uids=new Set();
  let local=0;
  for(const r of timeline||[]){
    const f=r&&r.finish;
    if(!f||!(f.voices>0))continue;
    const ids=f.voice_uids||[];
    for(const u of ids)uids.add(u);
    local+=Math.max(0,f.voices-ids.length);
  }
  return uids.size+local;
}
// the queue's order inside one meeting: the most talk time first, and never a
// voice this run already named or dropped (the relabel that carries a name to
// the other meetings lands a little after the save). A voice this run skipped
// in another meeting stays open here but arrives already skipped: the queue
// asks about each voice once, as many times as "Name N voices" counted it.
function fsVoiceOrder(voices,segs,resolved,passed){
  const done=new Set(resolved||[]),again=new Set(passed||[]),talk={};
  for(const g of segs||[])
    talk[g.speaker]=(talk[g.speaker]||0)+Math.max(0,(g.end||0)-(g.start||0));
  // a voice split over clusters talks for all of them
  const t=v=>(v.ids||[v.id]).reduce((a,id)=>a+(talk[id]||0),0);
  return (voices||[]).filter(v=>!(v.uid&&done.has(v.uid)))
    .map(v=>v.uid&&again.has(v.uid)?Object.assign({},v,{st:'skip'}):v)
    .sort((a,b)=>t(b)-t(a));
}

/* ------------------------------------------------------------- markup ---- */
function fsStripHTML(steps){
  return `<div class="fstrip" role="list" aria-label="Steps to finish this meeting">`
    +steps.map((s,i)=>{
      const no=s.state==='done'?'&#10003;':String(i+1);
      const c=s.state!=='done'&&s.count?` <span class="c mono">${s.count}</span>`:'';
      return (i?'<span class="fsep" aria-hidden="true">&#8250;</span>':'')
        +`<span class="fstep ${s.state}" role="listitem" data-step="${s.key}"`
        +`${s.state==='cur'?' aria-current="step"':''}><span class="no">${no}</span>${s.label}${c}</span>`;
    }).join('')
    +`</div>`;
}
function _fsNextBtn(next,primary){
  return next
    ?`<button class="btn ${primary?'primary ':''}mini" id="fsnext" type="button" onclick="fsGo(1)"
      title="${esc(next.title||next.id)} (])">Next to finish &#8594;</button>`
    :`<span class="fsnone">Nothing else to finish.</span>`;
}
function fsDoneHTML(next){
  return `<div class="doneline" id="fsdone"><span>&#10003; Finished: title, voices, and flags are done.</span>
    <span class="grow"></span>${_fsNextBtn(next,false)}</div>`;
}
function fsTitleHTML(row){
  const t=(row&&(row.suggested_title||row.title))||'';
  const d=(row&&(row.suggested_date||row.date))||'';
  const stem=srcStem((row&&row.source_file)||'');
  return `<div class="fbody fstitle">
    <div class="vhead"><h3>Name this meeting</h3><span class="vsub">suggested title and date</span></div>
    <div class="ftrow">
      <input class="renameinput" id="fstitle" type="text" value="${esc(t)}" aria-label="Meeting title"
        onkeydown="fsTitleKey(event)">
      <input class="dateinput" id="fsdate" type="date" value="${esc(d)}" aria-label="Meeting date"
        onkeydown="fsTitleKey(event)">
      <button class="btn primary mini" id="fsaccept" type="button" onclick="fsAccept()"
        title="Accept this title and date (Enter)">Accept</button>
      ${stem&&stem.trim().toLowerCase()!==t.trim().toLowerCase()
        ?`<button class="btn mini" type="button" onclick="fsUseFile()"
        title="${esc(stem)}">Use the file name</button>`:''}
    </div>
    <div id="fserr" class="mcerr" hidden></div>
  </div>`;
}
function fsVoiceHTML(voices,i,live){
  const v=voices[i],n=voices.length;
  const ticks=voices.map((x,k)=>`<i class="${k===i?'cur':(x.st==='named'||x.st==='gone')?'done':''}"></i>`).join('');
  return `<div class="fbody fsvoice">
    <div class="vhead"><h3>Who is ${esc(v.display)}?</h3>
      <span class="vsub">voice ${i+1} of ${n} in this meeting</span>
      <span class="vq" aria-hidden="true">${ticks}</span></div>
    <div class="vgrid">
      <div>
        <input class="namebox" id="fvname" type="text" autocomplete="off" spellcheck="false"
          placeholder="Person&#8217;s name" aria-label="This voice belongs to" onkeydown="fvBoxKey(event)">
        <div class="preview" id="fvpreview" hidden></div>
        <div class="looks" id="fvlooks" hidden></div>
        <div class="vbtns">
          <button class="btn primary" id="fvsave" type="button" onclick="fvSave()"
            title="Save this name and go to the next voice (Enter)">Save and next</button>
          <button class="btn" type="button" onclick="fvSkip()"
            title="Leave this voice unnamed for now and go to the next (s)">Skip</button>
          <button class="btn danger mini" type="button" onclick="fvForget()"
            title="Grumbling, music, an echo: stop asking me to name this voice">Not a real speaker</button>
        </div>
        <div id="fvconfirm" class="dckconfirm" hidden></div>
        <div id="fverr" class="mcerr" hidden></div>
      </div>
      <div>
        <div class="clip" id="fvclip"><span class="src">${live?'Finding their longest turn&#8230;':''}</span></div>
        <div class="said" id="fvsaid"></div>
      </div>
    </div>
  </div>`;
}
function fsFlagsHTML(n){
  return `<div class="fbody fsflags" id="fsflags"><span>${n} flagged line${n!==1?'s':''} to check.</span>
    <button class="btn primary mini" type="button" onclick="fsFlagsStart()"
      title="Step through them in document order (n)">Step through them</button></div>`;
}
function fsLeftHTML(fin,next){
  const bits=[];
  if(!fin.title)bits.push('the title');
  if(fin.voices)bits.push(`${fin.voices} voice${fin.voices!==1?'s':''}`);
  if(fin.flags)bits.push(`${fin.flags} flagged line${fin.flags!==1?'s':''}`);
  return `<div class="fbody fsleft"><span>Left for later: ${bits.join(', ')}.</span>
    <button class="btn mini" type="button" onclick="fsResume()">Go back to them</button>
    <span class="grow"></span>${_fsNextBtn(next,true)}</div>`;
}

/* ------------------------------------------------------------ the flow ---- */
function fsNext(dir){
  return nextToFinish(S&&S.timeline,FS?FS.base:(route&&route.base),dir);
}
// the page opened: build the flow from the row's finish record, or none at all
function fsInit(base){
  const carry=FS_CARRY&&FS_CARRY.base===base?FS_CARRY:null;
  const quiet=RQUIET===base;   // two panes: a j/k walk or the default open
  FS_CARRY=null;FS=null;RQUIET=null;
  const row=rowById(base),box=document.getElementById('mfinish');
  if(!row||!row.finish||(row.finish.done&&!(carry&&carry.keep))){
    if(box){box.hidden=true;box.innerHTML='';}
    return;
  }
  FS={base,row,fin:{title:!!row.finish.title,voices:row.finish.voices||0,flags:row.finish.flags||0},
      past:(carry&&carry.past)||[],voices:null,vi:0,cur:null,
      live:!!(carry&&carry.live)||(toFinish(row)&&!quiet),hand:!!(carry&&carry.live),
      looks:[],clip:null,busy:false,first:true,
      // the voice queue: its mode, the meetings it has visited, the uids it
      // has named or dropped so far, and the uids it has skipped
      mode:(carry&&carry.mode)||null,seen:((carry&&carry.seen)||[]).slice(),
      resolved:((carry&&carry.resolved)||[]).slice(),
      passed:((carry&&carry.passed)||[]).slice()};
  fsShow(false);
}
// paint the strip and the current step's body; `auto` means the flow moved
// on by itself, which is what may start the flag review
function fsShow(auto){
  if(!FS)return;
  // the voice queue: once this meeting's Voices step is behind it, hand off
  // to the next meeting with an unnamed voice instead of the Title or Flags
  // steps. At the end of the queue the flow drops back to the ordinary strip
  // and starts nothing on its own.
  if(FS.mode==='voices'&&fsCur(finishSteps({finish:FS.fin},FS.past))!=='voices'){
    if(fsVoiceGo())return;
    FS.mode=null;FS.past=FS.past.filter(k=>k==='voices');auto=false;
    toast('No more voices to name.');
  }
  const prev=FS.cur;
  fsRender();
  if(FS.cur==='title'&&FS.live){const t=$('#fstitle');if(t)t.focus({preventScroll:true});}
  else if(FS.cur==='voices')fvOpen();
  else if(FS.cur==='flags'&&auto&&prev!=='flags'&&!(MR&&MR.active))reviewStart(0);
}
function fsRender(){
  const box=document.getElementById('mfinish');
  if(!box||!FS)return;
  const steps=finishSteps({finish:FS.fin},FS.past);
  const cur=fsCur(steps);
  FS.cur=cur;
  box.hidden=false;
  if(steps[3].state==='done'){box.innerHTML=fsDoneHTML(fsNext(1));return;}
  let body='';
  if(cur==='title')body=fsTitleHTML(FS.row);
  else if(cur==='voices'){
    if(FS.voices&&!FS.voices[FS.vi]){const j=FS.voices.findIndex(x=>!x.st);if(j>=0)FS.vi=j;}
    const v=FS.voices&&FS.voices[FS.vi];
    body=v?fsVoiceHTML(FS.voices,FS.vi,FS.live)
         :'<div class="fbody"><span class="spin"></span> Loading voices&#8230;</div>';
  }
  else if(cur==='flags')body=fsFlagsHTML(FS.fin.flags);
  else body=fsLeftHTML(FS.fin,fsNext(1));
  box.innerHTML=`<div class="finish">${fsStripHTML(steps)}${body}</div>`;
}
// counts moved but the step did not: repaint the strip alone, so a half-typed
// name or title under it survives
function fsStripSync(){
  if(!FS)return;
  const el=document.querySelector('#mfinish .fstrip');
  if(el)el.outerHTML=fsStripHTML(finishSteps({finish:FS.fin},FS.past));
}
// re-derive the step from FS.fin: a changed step repaints, the same one keeps
// its body and only its counts move
function fsSync(){
  if(!FS)return;
  const cur=fsCur(finishSteps({finish:FS.fin},FS.past));
  const all=FS.fin.title&&!FS.fin.voices&&!FS.fin.flags;
  if(cur!==FS.cur||(all&&!document.getElementById('fsdone'))){fsShow(false);return;}
  if(cur==='voices'){
    if(!document.getElementById('fvname')){fsShow(false);return;}   // loading -> the voice
    const v=FS.voices[FS.vi],box=$('#fvname');
    // a relabel named the voice on screen before anyone typed: move on
    if(v&&v.st==='gone'&&box&&!box.value.trim()){fvNext();return;}
  }
  fsStripSync();
}
function _fsSubstantial(){
  return MP?MP.segs.filter(g=>g.flags&&g.flags.length&&!mIsMinor(g)).length:0;
}
// the voices this meeting still asks about, straight off the transcript's
// speaker options: the same set the legend's "?" chips and the server's
// finish count read (unnamed, not dismissed, id not its own display). One
// VOICE, not one cluster: the diarizer can split one person into two
// clusters that carry the same global id (both "Speaker 68"), and naming
// that id names both, so they are one voice to ask about. Clusters sharing
// a uid, or with no uid the same display, collapse into one entry whose
// `ids` lists every cluster. The server's finish count keys by display, so
// the strip and the To finish row count the same voices.
function fsVoicesFrom(opts){
  const out=[],by=new Map();
  for(const o of (opts||[]).filter(o=>!o.named&&!o.dismissed&&o.id!==o.display)){
    const v={id:o.id,ids:[o.id],display:o.display,uid:mUnknownUid(o.display),st:''};
    const k=v.uid?'u\x00'+v.uid:'d\x00'+v.display;
    if(by.has(k)){by.get(k).ids.push(o.id);continue;}
    by.set(k,v);out.push(v);
  }
  return out;
}
function _fsOpenVoices(){return FS.voices?FS.voices.filter(v=>!v.st||v.st==='skip').length:FS.fin.voices;}
// the transcript (re)loaded: the page's own segments are the freshest truth
function fsSegsLoaded(){
  if(!FS||!MP||FS.base!==MP.base)return;
  const fresh=fsVoicesFrom(MP.spkOpts);
  if(!FS.voices){
    FS.voices=FS.mode==='voices'?fsVoiceOrder(fresh,MP.segs,FS.resolved,FS.passed):fresh;
    FS.vi=Math.max(0,FS.voices.findIndex(v=>!v.st));
    // every open voice here was skipped earlier in the run: the queue has
    // nothing to ask, so Voices is moved past and the hand-off follows
    if(FS.mode==='voices'&&FS.voices.length&&!FS.voices.some(v=>!v.st)
       &&!FS.past.includes('voices'))FS.past.push('voices');
  }else{
    // a voice is gone once none of its clusters still asks
    const live=new Set([].concat(...fresh.map(v=>v.ids)));
    FS.voices.forEach(v=>{if((!v.st||v.st==='skip')&&!(v.ids||[v.id]).some(id=>live.has(id)))v.st='gone';});
  }
  FS.fin.voices=_fsOpenVoices();
  FS.fin.flags=_fsSubstantial();
  const first=FS.first;FS.first=false;
  fsSync();
  // a hand-off landing on a meeting whose next step is its flags starts the
  // stepping, as an advance inside one meeting does
  if(first&&FS&&FS.hand&&FS.cur==='flags'&&!(MR&&MR.active))reviewStart(0);
}
// the verdict bar resolved or exited: the flag count follows the page's own
// segments, and a review that leaves no substantial flag moves on to Done
function fsFlagsSync(){
  if(!FS||!MP||FS.base!==MP.base)return;
  FS.fin.flags=_fsSubstantial();
  if(FS.cur==='flags'&&!(MR&&MR.active)&&!FS.fin.flags){FS.live=true;fsShow(true);return;}
  fsStripSync();
  const fb=document.querySelector('#fsflags span');
  if(fb)fb.textContent=`${FS.fin.flags} flagged line${FS.fin.flags!==1?'s':''} to check.`;
}
function fsFlagsStart(){if(MP&&MP.built)reviewStart(0);}
// two panes: Enter (or a click) on the reader's own row, after a j/k walk
// mounted it quietly. The flow takes the keyboard as if the meeting had just
// been opened: the step's input takes focus, the voice clip plays, the flags
// start stepping. Nothing typed is re-rendered away.
function fsWake(){
  if(!FS)return false;
  FS.live=true;
  if(FS.cur==='title'){const t=$('#fstitle');if(t)t.focus({preventScroll:true});return true;}
  if(FS.cur==='voices'){const b=$('#fvname');if(b){b.focus({preventScroll:true});fvPlay();}return true;}
  if(FS.cur==='flags'&&!(MR&&MR.active)){reviewStart(0);return true;}
  return false;
}
function fsResume(){
  if(!FS)return;
  FS.past=[];
  if(FS.voices){
    FS.voices.forEach(v=>{if(v.st==='skip')v.st='';});
    FS.vi=Math.max(0,FS.voices.findIndex(v=>!v.st));
    FS.fin.voices=_fsOpenVoices();
  }
  FS.live=true;fsShow(true);
}
// "Name N voices": the first meeting of the voice queue, straight into its
// Voices step. With no live meeting holding an unnamed voice (every one of
// them heard only in archived meetings), the tray's old path: the naming
// panel on the first unknown voice.
function fsVoicesStart(){
  const r=nextVoiceMeeting(S&&S.timeline,[],[]);
  if(r){fsVoiceCarry(r.id,[],[],[]);return;}
  const u=((S&&S.tray)||[]).find(t=>t.kind==='unknown_voice');
  if(u)openNamePanelByUid(u.target);
}
function fsVoiceCarry(base,seen,resolved,passed){
  FS_CARRY={base,past:['title','flags'],live:true,keep:true,mode:'voices',
    seen:seen.concat(base),resolved:resolved.slice(),passed:passed.slice()};
  location.hash='#m/'+encodeURIComponent(base);
}
// the queue's hand-off, after this meeting's last voice
function fsVoiceGo(){
  const resolved=FS.resolved||[],passed=FS.passed||[];
  const r=nextVoiceMeeting(S&&S.timeline,FS.seen,resolved.concat(passed));
  if(!r)return false;
  fsVoiceCarry(r.id,FS.seen,resolved,passed);
  return true;
}
// ] / [ and the Next button: the next meeting to finish, by the #m route
function fsGo(dir){
  const r=nextToFinish(S&&S.timeline,FS?FS.base:(route&&route.base),dir);
  if(!r)return false;
  FS_CARRY={base:r.id,past:[],live:true,keep:false};
  location.hash='#m/'+encodeURIComponent(r.id);
  return true;
}
// the finish flow's keys, from mKey. Inactive while typing, while the naming
// panel, a popover, or an edit card is open; the flag keys stay mKey's.
function fsKey(e){
  if(!route||route.view!=='meeting'||e.metaKey||e.ctrlKey||e.altKey)return false;
  if(e.defaultPrevented)return false;   // two panes: the list took it (Enter on a row)
  const ae=document.activeElement;
  const typing=ae&&(/^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)||ae.isContentEditable);
  if(typing||kbLayerOpen()||_popClose||document.getElementById('mcard'))return false;
  if(e.key===']'||e.key==='['){
    if(fsGo(e.key===']'?1:-1))e.preventDefault();
    return true;
  }
  if(!FS||FS.cur!=='voices'||!FS.voices||!FS.voices[FS.vi]||(MR&&MR.active))return false;
  if(e.key==='1'||e.key==='2'||e.key==='3'){
    if(fvUse(+e.key-1)){e.preventDefault();return true;}
    return false;
  }
  if(e.key==='s'){e.preventDefault();fvSkip(true);return true;}
  const onBtn=ae&&ae.tagName==='BUTTON';
  if(e.key===' '&&!onBtn){e.preventDefault();fvPlay();return true;}
  if(e.key==='Enter'&&!onBtn){e.preventDefault();fvSave();return true;}
  return false;
}

/* ------------------------------------------------------- title step ------ */
function fsTitleKey(e){
  if(e.key==='Enter'){e.preventDefault();fsAccept();}
  else if(e.key==='Escape'){e.preventDefault();e.target.blur();}
}
function fsUseFile(){
  const t=$('#fstitle');if(!t||!FS)return;
  t.value=srcStem(FS.row&&FS.row.source_file||'');t.focus();
}
async function fsAccept(){
  if(!FS||FS.busy||FS.cur!=='title')return;
  const base=FS.base,ti=$('#fstitle'),di=$('#fsdate'),btn=$('#fsaccept'),err=$('#fserr');
  // the date goes only when it differs from the stored one: sending the
  // prefilled date back would claim a person chose it (date_source=manual)
  const stored=(FS.row&&FS.row.date)||'',picked=(di&&di.value)||'';
  const name=(ti&&ti.value.trim())||'',date=(picked&&picked!==stored)?picked:'';
  FS.busy=true;if(btn){btn.disabled=true;btn.innerHTML='Saving&#8230;';}
  let r;
  try{r=await api('/api/accept_meeting',{base,name,date});}
  catch(e){r={ok:false,error:'Could not reach the server.'};}
  if(!FS||FS.base!==base||!route||route.base!==base)return;   // navigated away
  FS.busy=false;
  if(!r||!r.ok){
    if(btn){btn.disabled=false;btn.textContent='Accept';}
    if(err){err.hidden=false;err.textContent=(r&&r.error)||'Could not accept this meeting.';}
    return;
  }
  FS.fin.title=true;FS.live=true;
  if(r.base&&r.base!==base){
    // the folder re-stamped: follow it, and keep the flow going there
    FS_CARRY={base:r.base,past:FS.past.slice(),live:true,keep:true};
    await refresh();
    location.hash='#m/'+encodeURIComponent(r.base);
    return;
  }
  if(name){
    const m0=meetingByBase(base);if(m0)m0.title=name;
    const r0=rowById(base);if(r0)r0.title=name;
    const h=document.querySelector('#meetingpage .mtitle');if(h)h.textContent=name;
  }
  refresh();
  fsShow(true);
}

/* ------------------------------------------------------ voices step ------ */
function fvToken(){
  const v=FS&&FS.voices&&FS.voices[FS.vi];
  return v?FS.base+'\x00'+v.id+'\x00'+FS.vi:'';
}
function fvBoxKey(e){
  if(e.key==='Enter'){e.preventDefault();fvSave();}
  else if(e.key==='Escape'){e.preventDefault();e.target.blur();}
}
function fvBusy(on){
  NPBUSY=!!on;
  const w=$('#fvconfirm');
  ((w&&w.querySelectorAll)?[...w.querySelectorAll('button')]:[]).forEach(b=>{b.disabled=!!on;});
}
// the voice on screen: focus the box, then fetch the preview and the lines
// behind it (the step is usable the instant it opens, as the panel is)
function fvOpen(){
  const v=FS&&FS.voices&&FS.voices[FS.vi];
  if(!v||!document.getElementById('fvname'))return;
  FS.looks=[];FS.clip=null;
  if(FS.live&&!FS.keepBlur){const b=$('#fvname');if(b)b.focus({preventScroll:true});}
  FS.keepBlur=false;
  const tok=fvToken();
  fvPreview(v,tok);
  fvLines(v,tok);
}
async function fvPreview(v,tok){
  let r;
  try{r=await api('/api/name_preview',v.uid?{uid:v.uid}:{meeting:FS.base,speaker:v.id});}
  catch(e){return;}
  if(!FS||fvToken()!==tok||!r||r.error||!Array.isArray(r.targets))return;
  const line=$('#fvpreview'),looks=$('#fvlooks');
  if(line){line.textContent=npPreviewLine(r);line.hidden=false;}
  FS.looks=(r.looks_like||[]).slice(0,3);
  if(looks&&FS.looks.length){
    looks.innerHTML=FS.looks.map((x,k)=>`<button class="lchip" type="button" onclick="fvUse(${k})"
      title="Fill the name box (${k+1}); nothing is saved until you press Enter"
      >Looks like ${esc(x.name)} <span class="sc mono">${(+x.score).toFixed(2)}</span></button>`).join('');
    looks.hidden=false;
  }
}
// every cluster of the voice: a split voice's longest turn and its lines may
// sit in either cluster, so the lines of all of them merge, longest first
async function fvLines(v,tok){
  let rs;
  try{rs=await Promise.all((v.ids||[v.id]).map(id=>
    api('/api/voice_lines?base='+encodeURIComponent(FS.base)+'&speaker='+encodeURIComponent(id))));}
  catch(e){return;}
  if(!FS||fvToken()!==tok)return;
  const clip=$('#fvclip'),said=$('#fvsaid');
  if(rs.some(r=>!r||r.error||!Array.isArray(r.lines))){if(clip)clip.innerHTML='';return;}
  const r={lines:[].concat(...rs.map(x=>x.lines))
    .sort((a,b)=>(+b.dur||0)-(+a.dur||0))};
  if(!r.lines.length){
    if(clip)clip.innerHTML='<span class="src">No transcript text was attributed to this voice.</span>';
    return;
  }
  const top=r.lines[0];            // longest first: the evidence
  FS.clip={start:+top.start||0,end:(+top.start||0)+(+top.dur||0)};
  if(clip)clip.innerHTML=MP&&MP.hasAudio
    ?`<button class="iact play" type="button" onclick="fvPlay()" title="Play their longest turn (Space)">&#9654;</button>
      <span class="src">Longest turn here, at <span class="mono">${mFmtT(FS.clip.start)}</span>. Space replays.</span>`
    :`<span class="src">Longest turn here, at <span class="mono">${mFmtT(FS.clip.start)}</span>. This meeting has no audio.</span>`;
  if(said)said.innerHTML=r.lines.slice(0,3).sort((a,b)=>a.start-b.start).map(l=>
    `<p><span class="ts mono">${mFmtT(l.start)}</span><span>${esc(l.text)}</span></p>`).join('');
  if(FS.live)fvPlay();
}
// the longest turn, through the page's own span playback (the verdict bar's
// Play uses the same mechanism)
function fvPlay(){
  if(!FS||!FS.clip||!MP||!MP.hasAudio)return;
  mPlaySpan(FS.clip.start,FS.clip.end);
}
function fvUse(k){
  const x=FS&&FS.looks&&FS.looks[k],b=$('#fvname');
  if(!x||!b)return false;
  b.value=x.name;b.focus();
  return true;
}
// the next voice still open after this one; past the last, the strip moves
// on (a skipped voice keeps Voices open, so it is marked as moved past)
function fvNext(){
  if(!FS||!FS.voices)return;
  if(MP&&MP.audio&&MP.spanStop!=null){try{MP.audio.pause();}catch(e){}MP.spanStop=null;mSyncPP();}
  FS.fin.voices=_fsOpenVoices();
  let j=-1;
  for(let k=FS.vi+1;k<FS.voices.length;k++)if(!FS.voices[k].st){j=k;break;}
  if(j>=0)FS.vi=j;
  else{
    FS.vi=FS.voices.length;
    if(FS.fin.voices&&!FS.past.includes('voices'))FS.past.push('voices');
  }
  fsShow(true);
}
// from the key, the next voice leaves the keyboard where it is: the user is
// driving from outside the box, so a second s skips again instead of typing
// A skipped registry voice is remembered for the run (FS.passed), so the
// voice queue never asks about it again in another meeting.
function fvSkip(byKey){
  const v=FS&&FS.voices&&FS.voices[FS.vi];
  if(!v||NPBUSY||FS.cur!=='voices')return;
  v.st='skip';FS.live=true;FS.keepBlur=!!byKey;
  if(v.uid&&!(FS.passed||(FS.passed=[])).includes(v.uid))FS.passed.push(v.uid);
  fvNext();
}
async function fvSave(force,local){
  if(!FS||NPBUSY||FS.cur!=='voices')return;
  const v=FS.voices&&FS.voices[FS.vi];if(!v)return;
  const base=FS.base,tok=fvToken();
  const r=await saveName({uid:v.uid,meeting:base,speaker:v.id,speakers:v.ids,force,local,
    again:'fvSave',busy:fvBusy,
    ui:{name:$('#fvname'),err:$('#fverr'),warn:$('#fvconfirm'),warnId:'fvconfirm',
        btn:$('#fvsave'),label:'Save and next'}});
  if(!r)return;
  v.st='named';
  refresh();
  if(MP&&MP.base===base)mReloadSegs();
  if(!FS||FS.base!==base)return;
  if(v.uid)(FS.resolved||(FS.resolved=[])).push(v.uid);
  FS.live=true;
  if(fvToken()===tok)fvNext();
  else{FS.fin.voices=_fsOpenVoices();fsStripSync();}
}
// "Not a real speaker", as the panel does it: /api/forget for a registry
// voice, /api/dismiss_voice for a cluster the registry never tracked. A
// forgotten uid is also dismissed here, so this meeting stops counting it.
async function fvForget(){
  if(!FS||NPBUSY||FS.cur!=='voices')return;
  const v=FS.voices&&FS.voices[FS.vi];if(!v)return;
  const base=FS.base,tok=fvToken();
  fvBusy(true);
  let r=null;
  // every cluster of a split voice is dismissed here, or the one left over
  // keeps the voice counted. ONE request: the server dismisses all or none
  const ids=v.ids||[v.id];
  const body={base:base,speaker:ids[0]};
  if(ids.length>1)body.speakers=ids.slice();
  try{
    r=v.uid?await api('/api/forget',{uid:v.uid})
           :await api('/api/dismiss_voice',body);
    if(r&&r.ok&&v.uid)await api('/api/dismiss_voice',body);
  }catch(e){r=r&&r.ok?r:null;}
  finally{fvBusy(false);}
  if(!r||!r.ok){
    const err=$('#fverr');
    if(err&&fvToken()===tok){err.hidden=false;err.textContent=(r&&r.error)||'Could not remove this voice.';}
    return;
  }
  v.st='gone';
  refresh();
  if(MP&&MP.base===base)mReloadSegs();
  if(!FS||FS.base!==base)return;
  if(v.uid)(FS.resolved||(FS.resolved=[])).push(v.uid);
  FS.live=true;
  if(fvToken()===tok)fvNext();
  else{FS.fin.voices=_fsOpenVoices();fsStripSync();}
}

/* --------------------------------------------------------- route wiring ---- */
window.addEventListener('hashchange',()=>{route=parseHash();applyRoute();});
window.addEventListener('resize',()=>{paneSync();if(route.view==='meeting')mStickyTop();});
paneSync();     // two panes or one, before the first route decides what to mount
route=parseHash();
applyRoute();   // deep link (#m/<base>) opens here; if S is not ready yet, render()
                // finishes the build via maybeBuildPending once the first poll lands

/* ============================================================================
   Builder D: THE DRAWER. The last surface: one right slide-over (gear opens,
   x / veil / Escape close) with a pinned section nav -- Settings, Speakers,
   History, Archive. Settings and Speakers render from the polled S behind a
   change signature plus a focused-input guard, so the 2s poll never closes the
   drawer, resets its scroll, or wipes a half-typed field. History and Archive
   fetch their own endpoints on entry and after their own actions. Every
   mutation reuses an EXISTING endpoint; confirmations are the house two-step,
   never a native dialog. This retires the gear's bridge to the old page.
   ============================================================================ */

const DRAWER={open:false,section:'settings',
  sub:null,          // settings subview: null | 'schedule' | 'cloudkeys'
  spkOpen:null,      // the ONE expanded enrolled person (name), or null
  reassign:null,     // {name,idx} while a sample's inline reassign select is up
  showHidden:false,  // the unknowns' "N hidden" expansion
  dconfirm:null,     // armed two-step confirm key ('ck:prov'|'rmspk:name'|'renmerge:name'|'adel:base')
  renameTo:null,     // the typed rename target while its merge confirm is armed
  spkErr:'',         // last speaker-action error, rendered in the section
  updNote:'',updBusy:false,   // the model-update check's client-side note
  dupes:null,dupeScan:false,  // the duplicate-transcript review list, fetched on open
  dupeScanned:undefined,      // has a scan EVER run? (undefined = not asked yet)
  dupeBusy:false,dupeTrunc:false,  // fetch in flight; last scan hit its pair budget
  dupeSeen:undefined,   // tray pair count at the last fetch: the poll refetches on change
  hist:null,archived:null};   // fetched lists (results / items)

function openDrawer(section){
  const d=$('#drawer'),v=$('#drawerveil');
  if(!d||!v)return;
  if(!d.dataset.built){
    d.dataset.built='1';
    d.innerHTML=`<div class="dhead">
        <nav class="dnav" id="dnav" aria-label="Drawer sections"></nav>
        <button class="iact" type="button" title="Close" aria-label="Close" onclick="closeDrawer()">&#10005;</button>
      </div>
      <div class="dbody" id="dbody">
        <section id="dsec-settings" hidden aria-label="Settings"></section>
        <section id="dsec-speakers" hidden aria-label="Speakers"></section>
        <section id="dsec-history" hidden aria-label="Processing history"></section>
        <section id="dsec-archive" hidden aria-label="Archived meetings"></section>
        <section id="dsec-dupes" hidden aria-label="Duplicate transcripts"></section>
      </div>`;
  }
  DRAWER.open=true;v.hidden=false;d.hidden=false;
  drawerGo(section||DRAWER.section||'settings');
}
// the setup check's buttons: open the drawer at the section that fixes the
// line, scrolled to its row, with the row's first control focused
function openDrawerAt(section,anchor){
  if(section==='settings'&&DRAWER.sub){DRAWER.sub=null;DRAWER.dconfirm=null;}
  openDrawer(section);
  const el=anchor&&document.getElementById(anchor);
  if(!el)return;
  el.scrollIntoView({block:'start'});
  const row=el.classList.contains('dgroup')?el.nextElementSibling:el;
  const c=row&&row.querySelector('select,button,input');
  if(c)c.focus({preventScroll:true});
}
function closeDrawer(){
  if(!DRAWER.open)return;
  DRAWER.open=false;
  dvStop();
  const d=$('#drawer'),v=$('#drawerveil');
  if(d)d.hidden=true;
  if(v)v.hidden=true;
}
// a route change (opening a meeting from the naming panel, back/forward)
// closes the drawer, exactly like the naming slide-over
window.addEventListener('hashchange',()=>{if(DRAWER.open)closeDrawer();});
// Escape closes the drawer -- unless the naming panel just consumed the key
// (its handler runs first and preventDefaults) or a popover is open (its own
// Escape closes it; that listener registered later, so ours checks first)
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape'||!DRAWER.open)return;
  if(e.defaultPrevented)return;
  if(_popClose)return;
  e.preventDefault();closeDrawer();
});

function drawerGo(sec){
  DRAWER.section=sec;
  drawerNavSync(S||{});
  ['settings','speakers','history','archive','dupes'].forEach(k=>{
    const el=document.getElementById('dsec-'+k);
    if(el)el.hidden=(k!==sec);
  });
  if(sec==='settings')dSettingsDraw(S||{});
  else if(sec==='speakers')dSpeakersDraw(S||{});
  else if(sec==='history')dHistLoad();     // (re)fetches; the filter values persist
  else if(sec==='archive')dArchLoad();
  else if(sec==='dupes')dDupesLoad();
}
// the poll's entry point: cheap when closed; only live sections rebuild, and
// only when their signature changed (History/Archive own their own fetches)
function drawDrawer(s){
  if(!DRAWER.open)return;
  drawerNavSync(s);
  dSettingsDraw(s);
  dSpeakersDraw(s);
  dDupesPoll(s);
}
function drawerNavSync(s){
  const nav=$('#dnav');if(!nav)return;
  const n=(s&&s.archived_count)||0;   // the old page's "Archived · N" lives on the tab
  // the duplicate-transcript count rides its tab the same way, so the section
  // is findable without the Now row that sent you there
  const dn=((s&&s.tray||[]).find(t=>t.kind==='dupe_meetings')||{}).count||0;
  const sig=DRAWER.section+'|'+n+'|'+dn;
  if(nav.dataset.sig===sig)return;
  nav.dataset.sig=sig;
  const tabs=[['settings','Settings'],['speakers','Speakers'],['history','History'],
    ['archive','Archive'+(n?' &middot; '+n:'')],
    ['dupes','Duplicates'+(dn?' &middot; '+dn:'')]];
  nav.innerHTML=tabs.map(([k,l])=>
    `<button class="dtab${DRAWER.section===k?' on':''}" type="button" onclick="drawerGo('${k}')">${l}</button>`).join('');
}
// never rebuild under the user's cursor: a focused field inside the section
// blocks the rebuild; an unchanged later poll (or a blur) lets it land
function dFocusGuard(el){
  const ae=document.activeElement;
  return !!(ae&&el.contains(ae)&&/^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName));
}
// inline error line: shows r.error when a call failed, hides again on success
function dErr(id,r){
  const e=document.getElementById(id);if(!e)return;
  if(r&&r.ok===false){e.hidden=false;e.textContent=r.error||'failed';}
  else e.hidden=true;
}

/* ------------------------------------------------------------ settings ----- *
 * Order per DESIGN.md: Automation (ONE master switch over the two indented
 * triggers), Transcription (model + cloud keys subview), Summaries and Ask,
 * Recorder (your-voice picker + the stall fix), Housekeeping. */
function _drow(name,sub,ctrl,id){
  return `<div class="drow"${id?` id="${id}"`:''}><div class="grow"><div class="dname">${name}</div>
    ${sub?`<div class="dsub">${sub}</div>`:''}</div>${ctrl||''}</div>`;
}
function _dtog(on,call,dis,label){
  return `<button class="dtog${on?' on':''}" type="button" role="switch"
    aria-checked="${on?'true':'false'}" aria-label="${label}"
    ${dis?'disabled':''} onclick="${call}"></button>`;
}
function dSettingsSig(s){
  const sc=s.schedule||{};
  return JSON.stringify([s.paused,sc.watch,sc.nightly,sc.hour,sc.minute,sc.installed,
    s.model,(s.asr_choices||[]).map(c=>c.id).join(','),s.cloud_keys,
    s.llm_backend,s.llm_backends,s.punctuate,s.mic_speaker,
    (s.enrolled||[]).map(e=>e.name).join(','),
    !!(s.recording&&s.recording.stalled),s.rates,s.paths,
    DRAWER.sub,DRAWER.updNote,DRAWER.updBusy,DRAWER.dconfirm]);
}
function dSettingsDraw(s){
  const el=document.getElementById('dsec-settings');
  if(!el||el.hidden)return;
  const sig=dSettingsSig(s);
  if(el.dataset.sig===sig)return;
  if(dFocusGuard(el))return;
  el.dataset.sig=sig;
  el.innerHTML=DRAWER.sub==='schedule'?dScheduleHTML(s)
    :DRAWER.sub==='cloudkeys'?dCloudKeysHTML(s)
    :dSettingsHTML(s);
}
function dSettingsForce(){
  const el=document.getElementById('dsec-settings');
  if(el)el.dataset.sig='';
  dSettingsDraw(S||{});
}
function dSub(v){DRAWER.sub=v;DRAWER.dconfirm=null;dSettingsForce();}

function dSettingsHTML(s){
  const sc=s.schedule||{};
  const masterOn=!s.paused;
  const timeTxt=(sc.nightly&&sc.hour!=null)
    ?new Date(2000,0,1,sc.hour,sc.minute||0).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}):'';
  let h=`<div class="dgroup">Automation</div>`;
  // the master switch IS /api/pause -- the same state the pill and the Process
  // popover read, so a switch never reads On while doing nothing
  h+=_drow('Automatic runs',
    'The master switch over the folder watch, the nightly run, and login catch-up. Manual runs always work.',
    _dtog(masterOn,'dTogMaster(this)',false,'Automatic runs'));
  const watchSub=!sc.installed?'Not installed. Run ./setup.sh install-agent.'
    :sc.watch?'New recordings process within moments of landing (while the Mac is awake).'
    :'Off. New files wait for the nightly run or a manual click.';
  const nightSub=!sc.installed?'Not installed. Run ./setup.sh install-agent.'
    :sc.nightly?`${esc(timeTxt)} each night. If the Mac is asleep then, the run happens at the next wake.`
    :'Off. Turn on to process everything new at a set time.';
  h+=`<div class="dindent${masterOn?'':' inert'}">
    ${masterOn?'':'<div class="dinert-note">off while automatic runs are paused</div>'}
    ${_drow('Folder watch',watchSub,_dtog(!!sc.watch,'dTogWatch(this)',!sc.installed||!masterOn,'Folder watch'))}
    ${_drow('Nightly run',nightSub,
      (sc.nightly?`<button class="btn mini" type="button" ${(!sc.installed||!masterOn)?'disabled':''} onclick="dSub('schedule')">Change&#8230;</button>`:'')
      +_dtog(!!sc.nightly,'dTogNightly(this)',!sc.installed||!masterOn,'Nightly run'))}
  </div>
  <div id="dautoerr" class="derr" hidden></div>`;

  h+=`<div class="dgroup">Transcription</div>`;
  // cloud engines appear only when their provider key is set, exactly like old
  const pickable=(s.asr_choices||[]).filter(c=>!c.cloud||(s.cloud_keys||{})[c.cloud]);
  const modelNote=((s.asr_choices||[]).find(c=>c.id===s.model)||{}).note||'';
  h+=_drow('Model',esc(modelNote),
    `<select id="dmodelsel" class="dsel" onchange="dSetModel(this)" aria-label="Transcription model">${
      pickable.map(c=>`<option value="${esc(c.id)}" ${c.id===s.model?'selected':''}>${esc(c.label)}</option>`).join('')}</select>`);
  const nk=['scribe','openai','voxtral'].filter(p=>(s.cloud_keys||{})[p]).length;
  h+=_drow('Cloud transcription',
    nk?`${nk} provider key${nk>1?'s':''} set. Cloud engines appear in the model picker.`
      :'Optional: bring your own API key (ElevenLabs &middot; OpenAI &middot; Mistral).',
    `<button class="btn mini" type="button" onclick="dSub('cloudkeys')">Cloud keys&#8230;</button>`);
  h+=`<div id="dmodelerr" class="derr" hidden></div>`;

  h+=`<div class="dgroup">Summaries and Ask</div>`;
  const LB={local:'Local Qwen3-8B',anthropic:'Claude Haiku &middot; cloud',openai:'OpenAI GPT &middot; cloud'};
  const av=s.llm_backends||{};
  const llmNote=s.llm_backend==='local'
    ?(av.local?'Runs on this Mac. Transcripts never leave it.'
              :'Local model not installed. Pick a cloud assistant or install .venv-llm.')
    :'Cloud assistant: transcript text uploads for summaries and Ask. Strict recordings always stay local.';
  h+=_drow('Assistant',llmNote,
    `<select id="dllmsel" class="dsel" onchange="dSetLlm(this)" aria-label="Summaries and Ask assistant">${
      Object.keys(LB).map(b=>`<option value="${b}" ${b===s.llm_backend?'selected':''} ${av[b]?'':'disabled'}>${LB[b]}${av[b]?'':' (no key)'}</option>`).join('')}</select>`);
  h+=`<div id="dllmerr" class="derr" hidden></div>`;

  h+=`<div class="dgroup" id="dg-recorder">Recorder</div>`;
  // the your-voice picker is an inline enrolled-name select (the old page used
  // a native prompt; the house rule bans those)
  const names=(s.enrolled||[]).map(e=>e.name);
  if(s.mic_speaker&&!names.includes(s.mic_speaker))names.unshift(s.mic_speaker);
  const micNote=s.mic_speaker
    ?`Recorded calls separate ${esc(s.mic_speaker)} (you) from the others. Enroll ${esc(s.mic_speaker)} as a speaker for this to take effect.`
    :'Your name on recorded calls, so the recorder separates you from the others. You must be enrolled as a speaker too.';
  h+=_drow('Your voice',micNote,
    `<select id="dmicsel" class="dsel" onchange="dSetMic(this)" aria-label="Your name on recorded meetings">
      <option value="">off</option>${
      names.map(n=>`<option value="${esc(n)}" ${n===s.mic_speaker?'selected':''}>${esc(n)}</option>`).join('')}</select>`);
  if(s.recording&&s.recording.stalled)
    h+=`<div class="dstall"><b>Not capturing audio.</b> macOS is not delivering the
      microphone / system sound (a recorder rebuild resets its permissions).
      <button class="btn mini" type="button" onclick="dFixPerms(this)">Fix permissions</button>
      <span id="dfixnote" class="derr" hidden></span></div>`;

  h+=`<div class="dgroup">Housekeeping</div>`;
  h+=_drow('Punctuation cleanup','Restore punctuation &amp; casing (never changes words).',
    _dtog(!!s.punctuate,'dTogPunct(this)',false,'Punctuation cleanup'));
  h+=_drow('Model updates',esc(DRAWER.updNote||'Check HuggingFace for newer versions.'),
    `<button class="btn mini" type="button" ${DRAWER.updBusy?'disabled':''} onclick="dCheckUpdates()">${DRAWER.updBusy?'Checking&#8230;':'Check'}</button>`);
  h+=_drow('Speed calibration',
    (s.rates&&s.rates.runs)
      ?`Measured from ${s.rates.runs} run${s.rates.runs>1?'s':''}: ${esc(s.rates.text)} realtime. Estimates improve automatically.`
      :'Estimates use factory measurements until a few runs complete.','');
  const home=p=>esc((p||'').replace(/^\/Users\/[^/]+/,'~'));
  h+=_drow('Watched folder',home(s.paths&&s.paths.source),
    `<button class="btn mini" type="button" onclick="dPickFolder('source',this)">Change&#8230;</button>`,
    'drow-source');
  h+=_drow('Transcripts folder',home(s.paths&&s.paths.dest),
    `<button class="btn mini" type="button" onclick="dPickFolder('dest',this)">Change&#8230;</button>`);
  return h;
}
function dTogMaster(btn){if(!S)return;btn.disabled=true;
  api(S.paused?'/api/resume':'/api/pause',{}).then(refresh);}
function dTogWatch(btn){if(!S)return;btn.disabled=true;
  api('/api/automation',{watch:!S.schedule.watch}).then(r=>{dErr('dautoerr',r);refresh();});}
function dTogNightly(btn){if(!S)return;btn.disabled=true;
  api('/api/automation',{nightly:!S.schedule.nightly}).then(r=>{dErr('dautoerr',r);refresh();});}
// the three select setters clear the section signature after their refresh:
// a rejected (or externally reverted) choice must not leave the control
// showing a value the state never took. The focus guard still defers the
// repaint while the user is ON the control; the next poll then re-syncs it.
function dSetModel(sel){
  api('/api/model',{model:sel.value}).then(async r=>{dErr('dmodelerr',r);await refresh();dSettingsForce();});}
function dSetLlm(sel){
  api('/api/llm_backend',{backend:sel.value}).then(async r=>{dErr('dllmerr',r);await refresh();dSettingsForce();});}
function dSetMic(sel){
  api('/api/mic_speaker',{name:sel.value}).then(async()=>{await refresh();dSettingsForce();});}
async function dFixPerms(btn){
  btn.disabled=true;btn.textContent='Resetting…';
  const r=await api('/api/fix_recorder_permissions',{});
  if(!r.ok){
    btn.disabled=false;btn.textContent='Fix permissions';
    const n=$('#dfixnote');if(n){n.hidden=false;n.textContent=r.error||'permission reset failed';}
  }
  refresh();
}
function dTogPunct(btn){if(!S)return;btn.disabled=true;
  api('/api/punctuate',{on:!S.punctuate}).then(refresh);}
async function dCheckUpdates(){
  DRAWER.updBusy=true;dSettingsForce();
  let r;
  try{r=await api('/api/check_updates');}
  catch(e){r={error:'the update check failed'};}
  const ups=(r.models||[]).filter(m=>m.update_available);
  DRAWER.updNote=r.error?('Check failed: '+r.error)
    :(ups.length?'Updates available: '+ups.map(u=>u.label).join(', '):'All models are current.');
  DRAWER.updBusy=false;
  dSettingsForce();
}
// a native FOLDER picker via the server is fine: it is the OS's picker, not a
// browser dialog (same /api/pick_folder as the old page)
async function dPickFolder(which,btn){
  btn.disabled=true;
  const prompt=which==='source'?'Choose the folder to watch for new recordings'
    :'Choose where transcripts are stored';
  await api('/api/pick_folder',{which,prompt});
  await refresh();
  dSettingsForce();
}

/* nightly time picker: an in-drawer subview, not a dialog */
function dScheduleHTML(s){
  const sc=s.schedule||{},hh=sc.hour??2,mm=sc.minute??0;
  return `<button class="dback" type="button" onclick="dSub(null)">&#8592; Settings</button>
  <h3 class="dtitle">Nightly run time</h3>
  <p class="dnote">Everything new processes at this time each night. If the Mac is
    asleep at that moment, the run happens automatically at the next wake. (The
    folder watch is a separate switch: it picks files up the moment they land.)</p>
  <div class="dtimegrid">
    <select id="dsh" class="dsel" aria-label="Hour">${[...Array(24).keys()].map(i=>
      `<option value="${i}" ${i===hh?'selected':''}>${(i%12)||12} ${i<12?'AM':'PM'}</option>`).join('')}</select>
    <b>:</b>
    <select id="dsm" class="dsel" aria-label="Minute">${[0,15,30,45].map(i=>
      `<option value="${i}" ${i===mm?'selected':''}>${String(i).padStart(2,'0')}</option>`).join('')}</select>
  </div>
  <p class="dnote">Best between 1 and 5 AM, plugged in. Overnight runs need AC power.</p>
  <div class="dbtns">
    <button class="btn mini" type="button" onclick="dSub(null)">Cancel</button>
    <button class="btn primary mini" type="button" onclick="dSchedSave(this)">Save</button>
  </div>
  <div id="dschederr" class="derr" hidden></div>`;
}
async function dSchedSave(btn){
  btn.disabled=true;
  const r=await api('/api/schedule',{hour:+$('#dsh').value,minute:+$('#dsm').value});
  if(r&&r.ok===false){btn.disabled=false;dErr('dschederr',r);return;}
  DRAWER.sub=null;
  await refresh();
  dSettingsForce();
}

/* cloud keys: an in-drawer subview. Password fields render EMPTY every time --
 * the server only ever sends presence booleans (cloud_keys[prov] is true/false)
 * and this view never writes any state into an input's value, so a key is never
 * echoed anywhere after Save. */
const CK_PROVIDERS=[
  ['scribe','ElevenLabs Scribe','elevenlabs.io &#8594; Profile &#8594; API keys'],
  ['openai','OpenAI','platform.openai.com &#8594; API keys &middot; also used by the OpenAI assistant below'],
  ['voxtral','Mistral Voxtral','console.mistral.ai &#8594; API keys']];
function _ckRow(s,prov,label,hint){
  const has=!!((s.cloud_keys||{})[prov]);   // presence boolean, never the key
  const arm=DRAWER.dconfirm==='ck:'+prov;
  return `<div class="dckrow">
    <span class="dcklabel">${label}</span>
    <div class="grow">
      <div class="dckline">
        <input type="password" id="dck_${prov}" class="dinput" autocomplete="off"
          spellcheck="false" placeholder="${has?'saved: paste to replace':'paste API key'}"
          aria-label="${label} API key">
        <span class="dcktick" title="${has?'A key is saved':''}">${has?'&#10003;':''}</span>
        ${has?`<button class="btn mini" type="button" title="Remove the saved ${label} key from this Mac"
          onclick="dCkClearAsk('${prov}')">Clear</button>`:''}
      </div>
      ${arm?`<div class="dckconfirm">Remove the saved ${label} key? Cloud transcription
          with this provider stops working until a new key is pasted.
        <button class="btn mini" type="button" onclick="dConfirmClear()">Keep</button>
        <button class="btn danger mini" type="button" onclick="dCkClearGo('${prov}')">Clear it</button></div>`:''}
      <div class="dsub">${hint}</div>
    </div></div>`;
}
function dCloudKeysHTML(s){
  const ck=s.cloud_keys||{};
  return `<button class="dback" type="button" onclick="dSub(null)">&#8592; Settings</button>
  <h3 class="dtitle">Cloud transcription keys</h3>
  <p class="dnote">Optional: transcribe with a cloud engine instead of the local
    models. Only the audio is uploaded; speaker identification and voiceprints
    stay on this Mac. <b>Strict-mode recordings never upload</b>, whatever engine
    is selected. Keys are stored in stt.env on this machine and never shown again.</p>
  ${CK_PROVIDERS.map(([p,l,hint])=>_ckRow(s,p,l,hint)).join('')}
  <h3 class="dtitle">Assistant (summaries &amp; Ask)</h3>
  <p class="dnote">The assistant drafts summaries and answers Ask questions. The
    local model needs no key. Choosing a cloud assistant in Settings sends
    transcript text to that provider for these features only; <b>strict-mode
    recordings always use the local model</b>.</p>
  ${_ckRow(s,'anthropic','Anthropic (Claude)','console.anthropic.com &#8594; API keys')}
  <div class="dckrow"><span class="dcklabel">OpenAI (GPT)</span>
    <div class="grow dsub">uses the OpenAI key from the transcription section above
      &middot; ${ck.openai?'&#10003; key saved':'no key yet'}</div></div>
  <div class="dbtns">
    <button class="btn mini" type="button" onclick="dSub(null)">Cancel</button>
    <button class="btn primary mini" type="button" onclick="dCkSave(this)">Save</button>
  </div>
  <div id="dckerr" class="derr" hidden></div>`;
}
async function dCkSave(btn){
  btn.disabled=true;
  const val=p=>{const i=document.getElementById('dck_'+p);return i?i.value:'';};
  const r=await api('/api/cloud_keys',{scribe:val('scribe'),openai:val('openai'),
    voxtral:val('voxtral'),anthropic:val('anthropic')});
  if(!r.ok){btn.disabled=false;dErr('dckerr',r);return;}
  if(S)S.cloud_keys=r.set;   // fresh presence booleans: ticks flip, fields clear
  DRAWER.dconfirm=null;
  dSettingsForce();          // re-render the subview from state (stays open)
  refresh();
}
function dCkClearAsk(prov){DRAWER.dconfirm='ck:'+prov;dSettingsForce();}
async function dCkClearGo(prov){
  const r=await api('/api/cloud_keys',{clear:[prov]});
  if(!r.ok){dErr('dckerr',r);return;}
  if(S)S.cloud_keys=r.set;
  DRAWER.dconfirm=null;
  dSettingsForce();
  refresh();
}
// clears whichever two-step confirm is armed and repaints its section --
// EVERY section that renders a confirm, or Cancel leaves a live armed button
function dConfirmClear(){
  DRAWER.dconfirm=null;DRAWER.renameTo=null;
  dSettingsForce();dSpeakersForce();dArchRender();dDupesRender();
}

/* ------------------------------------------------------------ speakers ----- *
 * Enrolled people (snippet play, expandable per-sample rows with play /
 * reassign / remove, inline rename, merge, two-step remove) and unknown voices
 * ("Who is this?" reuses the EXISTING naming slide-over; hide / "N hidden" /
 * restore). Every edit spawns a relabel server-side; refresh() lets the quiet
 * relabel pill ride the next poll, same as the old card. */
function dSpeakersSig(s){
  return JSON.stringify([
    (s.enrolled||[]).map(e=>[e.name,e.samples,(e.sources||[]).join('|')]),
    // the two suggestion lines ride the signature: they land a poll or two
    // after the row first painted (a relabel writes them), so without this the
    // "possibly the same voice" line never appears on a row already on screen
    (s.unknowns||[]).map(u=>[u.uid,u.display,!!u.archived,(u.meetings||[]).length,
      (u.suggested||[]).map(x=>x.uid+':'+x.score).join('|'),
      (u.looks_like||[]).map(x=>x.name+':'+x.score).join('|')]),
    s.max_samples,!!s.relabel_pending,
    DRAWER.spkOpen,DRAWER.showHidden,DRAWER.dconfirm,DRAWER.spkErr,
    DRAWER.reassign&&(DRAWER.reassign.name+':'+DRAWER.reassign.idx)]);
}
function dSpeakersDraw(s){
  const el=document.getElementById('dsec-speakers');
  if(!el||el.hidden)return;
  const sig=dSpeakersSig(s);
  if(el.dataset.sig===sig)return;
  if(dFocusGuard(el))return;
  el.dataset.sig=sig;
  el.innerHTML=dSpeakersHTML(s);
  dvSync();   // the rebuild redrew the play buttons; restore the stop glyph
}
function dSpeakersForce(){
  const el=document.getElementById('dsec-speakers');
  if(el)el.dataset.sig='';
  dSpeakersDraw(S||{});
}
function dSpeakersHTML(s){
  const cap=s.max_samples||5;
  let h='';
  if(s.relabel_pending)
    h+=`<div class="dnote drelnote">Applying names to all transcripts&#8230; (moments)</div>`;
  if(DRAWER.spkErr)h+=`<div class="derr">${esc(DRAWER.spkErr)}</div>`;
  h+=`<div class="dgroup">People</div>`;
  const enr=s.enrolled||[];
  h+=enr.map(e=>{
    const open=DRAWER.spkOpen===e.name;
    const srcs=e.sources||[];
    const from=srcs.length?' &middot; from '+esc(srcs[srcs.length-1])
      +(srcs.length>1?' +'+(srcs.length-1):''):'';
    return `<div class="drow">
      <button class="iact play dplay" type="button" data-k="p:${esc(e.name)}"
        data-speaker="${esc(e.name)}" data-meeting=""
        onclick="dvPlay(this)" title="Play a short sample of this voice">&#9654;</button>
      <div class="grow"><div class="dname">${esc(e.name)}</div>
        <div class="dsub" title="${esc(srcs.join(', '))}">${e.samples} voice sample${e.samples>1?'s':''}${from}</div></div>
      <button class="iact" type="button" title="Samples, rename, merge, remove"
        aria-expanded="${open?'true':'false'}"
        onclick="dSpkToggle('${escJs(e.name)}')">${open?'&#9652;':'&#9662;'}</button>
    </div>${open?dSpkPanel(s,e,cap):''}`;
  }).join('')||`<div class="dempty">No one enrolled yet.</div>`;

  h+=`<div class="dgroup">Unknown voices</div>`;
  const vis=(s.unknowns||[]).filter(u=>!u.archived);
  const hid=(s.unknowns||[]).filter(u=>u.archived);
  h+=vis.map(u=>{
    const n=(u.meetings||[]).length;
    return `<div class="drow">
      <button class="iact play dplay" type="button" data-k="u:${esc(u.uid)}"
        data-speaker="${esc(u.uid)}" data-meeting="${esc((u.meetings||[])[0]||'')}"
        onclick="dvPlay(this)" title="Play a short sample of this voice">&#9654;</button>
      <div class="grow"><div class="dname">${esc(u.display)}</div>
        <div class="dsub">heard in ${n} meeting${n!==1?'s':''}</div></div>
      <button class="btn primary mini" type="button"
        onclick="openNamePanelByUid('${escJs(u.uid)}')">Who is this?</button>
      <button class="iact" type="button"
        title="One-time voice (focus group): keep it matchable but out of the way; restore any time"
        onclick="dHideUnknown('${escJs(u.uid)}',true)">Hide</button>
    </div>${dSuggHTML(u)}`;
  }).join('')||`<div class="dempty">No unidentified voices right now.</div>`;
  if(hid.length){
    h+=`<div class="drow dhiddenhdr"><button class="dlink" type="button"
      onclick="dToggleHidden()">${DRAWER.showHidden?'&#9662;':'&#9656;'} ${hid.length} hidden</button></div>`;
    if(DRAWER.showHidden)h+=hid.map(u=>{
      const n=(u.meetings||[]).length;
      return `<div class="drow dhid">
        <button class="iact play dplay" type="button" data-k="u:${esc(u.uid)}"
          data-speaker="${esc(u.uid)}" data-meeting="${esc((u.meetings||[])[0]||'')}"
          onclick="dvPlay(this)" title="Play a short sample of this voice">&#9654;</button>
        <div class="grow"><div class="dname">${esc(u.display)}</div>
          <div class="dsub">hidden &middot; heard in ${n} meeting${n!==1?'s':''}</div></div>
        <button class="iact" type="button" onclick="dHideUnknown('${escJs(u.uid)}',false)">Restore</button>
      </div>`;
    }).join('');
  }
  return h;
}
/* Under an unknown row: what the system already suspects about that voice, and
 * the one click that acts on it. Two kinds, one line each, best candidate only
 * -- a list of five guesses is a decision, and this is meant to be a glance.
 *   - "Possibly the same voice as Speaker 12" : phase 4 wrote this when the
 *     cluster missed the link bar and minted its own number. Link merges them.
 *   - "Looks like Avery Sample"             : an enrolled person this voice
 *     resembles above MATCH_MIN but under the automatic margin (the audit's
 *     nineteen). Name opens the panel with the name already typed, so the
 *     human confirms rather than the machine guessing. */
function dSuggHTML(u){
  const sug=((u&&u.suggested)||[])[0];
  const look=((u&&u.looks_like)||[])[0];
  if(!sug&&!look)return '';
  let h='';
  if(sug)h+=`<div class="dsugg"><span class="grow">Possibly the same voice as
    ${esc(sug.display||sug.uid)} &middot; <span class="mono">${(+sug.score).toFixed(2)}</span></span>
    <button class="btn mini" type="button"
      title="Treat these as one voice: this number folds into that one everywhere"
      onclick="dLinkUnknown('${escJs(u.uid)}','${escJs(sug.uid)}')">Link</button></div>`;
  if(look)h+=`<div class="dsugg"><span class="grow">Looks like ${esc(look.name)}
    &middot; <span class="mono">${(+look.score).toFixed(2)}</span></span>
    <button class="btn mini" type="button"
      title="Open the naming panel with this name filled in; nothing is saved until you press Save name"
      onclick="dNameUnknownAs('${escJs(u.uid)}','${escJs(look.name)}')">Name</button></div>`;
  return h;
}
// the same merge endpoint the drawer's people section uses, both sides by uid
async function dLinkUnknown(src,dst){
  DRAWER.spkErr=null;
  let r;
  try{r=await api('/api/merge_speakers',{src:'uid:'+src,dst:'uid:'+dst});}
  catch(e){r=null;}
  if(!r||!r.ok){
    DRAWER.spkErr='Could not link those voices. They may have been named already.';
    dSpeakersForce();
    return;
  }
  // the same sentence a save gets, from the same response: the link
  // relabels exactly the meetings it can reach, and none of them here (a
  // merge applies no meeting inside the request)
  namedToast(r);
  refresh();
}
// the panel, prefilled. openNamePanel paints its markup before its first await,
// so the box exists by the time this line runs.
function dNameUnknownAs(uid,name){
  openNamePanelByUid(uid);
  npUseName(name);
}
// the expandable management panel under ONE enrolled person at a time
function dSpkPanel(s,e,cap){
  const n=e.samples,srcs=e.sources||[];
  const others=(s.enrolled||[]).map(x=>x.name).filter(x=>x!==e.name);
  const ra=(DRAWER.reassign&&DRAWER.reassign.name===e.name)?DRAWER.reassign.idx:null;
  const rows=Array.from({length:n},(_,i)=>{
    // sources align to the newest samples when the list is shorter than n
    const src=srcs.length===n?srcs[i]:(srcs[srcs.length-n+i]||null);
    return `<div class="dsamp">
      ${src?`<button class="iact play dplay" type="button" data-k="s:${esc(e.name)}#${i}"
          data-speaker="${esc(e.name)}" data-meeting="${esc(src)}"
          onclick="dvPlay(this)" title="Play this sample">&#9654;</button>`
        :`<span class="dsamp-b" title="Source unknown (enrolled before tracking)"></span>`}
      <div class="grow dsub">Sample ${i+1} &middot; ${src?esc(src):'source unknown'}</div>
      ${others.length?`<button class="iact" type="button"
        title="Reassign: this sample is really someone else&#8217;s voice; move it to the right person instead of deleting it"
        onclick="dReassignAsk('${escJs(e.name)}',${i})">&#8594;</button>`:''}
      ${n>1?`<button class="iact" type="button"
        title="Remove this sample (e.g. a bad recording); the person keeps their other samples"
        onclick="dRemoveSample('${escJs(e.name)}',${i})">&#10005;</button>`:''}
      ${ra===i?`<div class="dinline">really
          <select class="dsel dreasel" aria-label="Move this sample to">${
            others.map(o=>`<option value="${esc(o)}">${esc(o)}</option>`).join('')}</select>
        <button class="btn primary mini" type="button" onclick="dReassignGo('${escJs(e.name)}',${i},this)">Move</button>
        <button class="btn mini" type="button" onclick="dReassignCancel()">Cancel</button></div>`:''}
    </div>`;
  }).join('');
  const clashArm=DRAWER.dconfirm==='renmerge:'+e.name;
  const rmArm=DRAWER.dconfirm==='rmspk:'+e.name;
  return `<div class="dpanel">
    <div class="dsub dpanel-h">Voice samples (${n} of ${cap})</div>
    <div class="dsub dpanel-n">A profile keeps up to ${cap} samples. A varied set,
      from different meetings, rooms, and mics, identifies this person more
      reliably than several clips from one recording.</div>
    ${rows}
    <div class="dsamp">
      <div class="grow dsub"><b>Rename</b>: fix the name everywhere</div>
      <input type="text" class="dinput drn" value="${esc(e.name)}" aria-label="New name for ${esc(e.name)}">
      <button class="iact" type="button" onclick="dRenameSpeaker('${escJs(e.name)}',this)">Apply</button>
    </div>
    ${clashArm?`<div class="dckconfirm">${esc(DRAWER.renameTo||'')} is already a saved
        person. Renaming &#8220;${esc(e.name)}&#8221; to ${esc(DRAWER.renameTo||'')} MERGES
        their voice samples into one profile.
      <button class="btn mini" type="button" onclick="dConfirmClear()">Cancel</button>
      <button class="btn primary mini" type="button" onclick="dRenameGo('${escJs(e.name)}')">Merge profiles</button></div>`:''}
    <div class="dsamp">
      <div class="grow dsub"><b>Merge into&#8230;</b>: this voice is really the same person as</div>
      <select class="dsel dmg" aria-label="Merge ${esc(e.name)} into" ${others.length?'':'disabled'}>${
        others.map(o=>`<option value="${esc(o)}">${esc(o)}</option>`).join('')}</select>
      <button class="iact" type="button" ${others.length?'':'disabled'}
        onclick="dMergeSpeaker('${escJs(e.name)}',this)">Merge</button>
    </div>
    <div class="dsamp">
      <div class="grow dsub"><b>Remove</b>: un-enroll; their lines revert to Speaker N</div>
      <button class="btn danger mini" type="button" onclick="dRemoveAsk('${escJs(e.name)}')">Remove&#8230;</button>
    </div>
    ${rmArm?`<div class="dckconfirm">Remove ${esc(e.name)}? Their lines revert to
        Speaker N in every transcript, and their voice samples are deleted.
      <button class="btn mini" type="button" onclick="dConfirmClear()">Cancel</button>
      <button class="btn danger mini" type="button" onclick="dRemoveGo('${escJs(e.name)}')">Remove</button></div>`:''}
  </div>`;
}
function dSpkToggle(name){
  DRAWER.spkOpen=DRAWER.spkOpen===name?null:name;
  DRAWER.reassign=null;DRAWER.dconfirm=null;DRAWER.renameTo=null;DRAWER.spkErr='';
  dSpeakersForce();
}
function dToggleHidden(){DRAWER.showHidden=!DRAWER.showHidden;dSpeakersForce();}
function dSpkFail(r){DRAWER.spkErr=(r&&r.error)||'failed';dSpeakersForce();}
function dHideUnknown(uid,hide){
  api('/api/hide_unknown',{uid,hide}).then(r=>{
    if(!r.ok){dSpkFail(r);return;}
    DRAWER.spkErr='';refresh();
  });
}
function dRemoveSample(name,idx){
  api('/api/remove_sample',{name,index:idx}).then(r=>{
    if(!r.ok){dSpkFail(r);return;}
    DRAWER.spkErr='';refresh();
  });
}
function dReassignAsk(name,idx){DRAWER.reassign={name,idx};DRAWER.spkErr='';dSpeakersForce();}
function dReassignCancel(){DRAWER.reassign=null;dSpeakersForce();}
function dReassignGo(name,idx,btn){
  const sel=btn.closest('.dinline').querySelector('.dreasel');
  const to=sel?sel.value:'';
  if(!to||to===name)return;
  api('/api/reassign_sample',{name,index:idx,to}).then(r=>{
    if(!r.ok){dSpkFail(r);return;}
    DRAWER.reassign=null;DRAWER.spkErr='';
    dSpeakersForce();refresh();
  });
}
function dRenameSpeaker(old,btn){
  const panel=btn.closest('.dpanel');
  const inp=panel&&panel.querySelector('.drn');
  const n=inp?inp.value.trim():'';
  if(!n||n===old)return;
  // renaming onto an existing person silently merges their voiceprints: say so
  const clash=(S&&S.enrolled||[]).find(e=>e.name.toLowerCase()===n.toLowerCase()&&e.name!==old);
  if(clash){DRAWER.renameTo=n;DRAWER.dconfirm='renmerge:'+old;dSpeakersForce();return;}
  dRenameGo(old,n);
}
async function dRenameGo(old,n){
  n=n||DRAWER.renameTo;
  DRAWER.dconfirm=null;DRAWER.renameTo=null;
  if(!n)return;
  const r=await api('/api/rename_speaker',{name:old,new:n});
  if(!r.ok){dSpkFail(r);return;}
  if(DRAWER.spkOpen===old)DRAWER.spkOpen=n;   // the panel follows the new name
  DRAWER.spkErr='';
  dSpeakersForce();refresh();
}
function dMergeSpeaker(name,btn){
  const sel=btn.closest('.dsamp').querySelector('.dmg');
  const dst=sel?sel.value:'';
  if(!dst||dst===name)return;
  api('/api/merge_speakers',{src:'name:'+name,dst:'name:'+dst}).then(r=>{
    if(!r.ok){dSpkFail(r);return;}
    DRAWER.spkOpen=dst;DRAWER.spkErr='';   // the samples moved: show the survivor
    dSpeakersForce();refresh();
  });
}
function dRemoveAsk(name){DRAWER.dconfirm='rmspk:'+name;dSpeakersForce();}
async function dRemoveGo(name){
  DRAWER.dconfirm=null;
  const r=await api('/api/remove_speaker',{name});
  if(!r.ok){dSpkFail(r);return;}
  if(DRAWER.spkOpen===name)DRAWER.spkOpen=null;
  DRAWER.spkErr='';
  dSpeakersForce();refresh();
}

/* voice snippet playback: EXCLUSIVE (one clip anywhere), tracked by a data key
 * rather than the DOM node so the stop glyph survives every rebuild -- the
 * same discipline as the old card's playVoice */
let dvAudio=null,dvKey=null;
function dvSync(){
  const playing=dvAudio&&!dvAudio.paused;
  document.querySelectorAll('.dplay').forEach(b=>{
    b.innerHTML=(playing&&b.dataset.k===dvKey)?'&#9724;':'&#9654;';
  });
}
function dvStop(){
  if(dvAudio)dvAudio.pause();
  dvAudio=null;dvKey=null;dvSync();
}
function dvPlay(btn){
  const k=btn.dataset.k;
  if(dvKey===k&&dvAudio&&!dvAudio.paused){dvStop();return;}   // toggle off
  if(dvAudio)dvAudio.pause();
  stopClip();
  document.querySelectorAll('audio').forEach(a=>a.pause());   // exclusive playback
  const spk=btn.dataset.speaker,mtg=btn.dataset.meeting||'';
  dvKey=k;btn.innerHTML='&#8230;';
  dvAudio=new Audio('/api/snippet?speaker='+encodeURIComponent(spk)
    +(mtg?'&meeting='+encodeURIComponent(mtg):''));
  dvAudio.onplaying=dvSync;
  dvAudio.onended=dvAudio.onerror=()=>{if(dvKey===k)dvStop();};
  dvAudio.play().catch(()=>{if(dvKey===k)dvStop();});
}

/* ------------------------------------------------------------- history ----- *
 * The permanent processing log (same /api/history): day groups newest first,
 * name filter + all/processed/failed select, failures keep their FULL error
 * text, capped at 400 rows with a note. The skeleton (and the filter values in
 * it) persists across visits; each entry re-fetches the list. */
const DHIST_CAP=400;
function dHistLoad(){
  const el=document.getElementById('dsec-history');
  if(!el)return;
  if(!el.dataset.built){
    el.dataset.built='1';
    el.innerHTML=`<p class="dnote">Every file this pipeline has processed, newest
      first. Failures keep their full error text.</p>
    <div class="dhfilter">
      <input type="text" id="dhistq" class="dinput" placeholder="Filter by name&#8230;"
        autocomplete="off" spellcheck="false" oninput="dHistRender()"
        aria-label="Filter history by name">
      <select id="dhistok" class="dsel" onchange="dHistRender()"
        title="Show everything, or only one outcome" aria-label="Filter history by outcome">
        <option value="">all</option><option value="ok">processed</option><option value="fail">failed</option>
      </select>
    </div>
    <div class="dsub" id="dhistcount"></div>
    <div id="dhistlist"><div class="dloading"><span class="spin"></span></div></div>`;
  }
  api('/api/history').then(r=>{DRAWER.hist=r.results||[];dHistRender();});
}
function dHistRender(){
  if(DRAWER.hist===null)return;
  const box=document.getElementById('dhistlist');if(!box)return;
  const q=(($('#dhistq')||{}).value||'').trim().toLowerCase();
  const f=(($('#dhistok')||{}).value)||'';
  const rows=DRAWER.hist.filter(r=>
    (!q||(r.name||'').toLowerCase().includes(q))&&(!f||(f==='ok')===!!r.ok));
  const nOk=rows.filter(r=>r.ok).length;
  const cnt=document.getElementById('dhistcount');
  if(cnt)cnt.textContent=rows.length
    ?`${rows.length} result${rows.length===1?'':'s'} · ${nOk} processed · ${rows.length-nOk} failed`:'';
  let day='';
  box.innerHTML=rows.slice(0,DHIST_CAP).map(r=>{
    const d=(r.at||'').slice(0,10);
    const hdr=d!==day?`<div class="dgroup">${d
      ?esc(new Date(d+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'long',day:'numeric',year:'numeric'}))
      :'Undated'}</div>`:'';
    day=d;
    return hdr+`<div class="drow">
      <span class="dchip ${r.ok?'ok':'bad'}">${r.ok?'&#10003;':'failed'}</span>
      <div class="grow"><div class="dname">${esc((r.name||'').replace(/\.[^.]+$/,''))}</div>
        ${r.summary?`<div class="dsub dhsum">${esc(r.summary)}</div>`:''}</div>
      <span class="dsub mono dhat">${esc((r.at||'').slice(11,16))}</span></div>`;
  }).join('')
  +(rows.length>DHIST_CAP?`<div class="dnote">Showing the first ${DHIST_CAP}.
      Narrow the filter to see older results.</div>`:'')
  ||'<div class="dempty">No matching results.</div>';
}

/* ------------------------------------------------------------- archive ----- *
 * Archived meetings (same /api/archived): per-row Restore and a two-step
 * in-drawer Delete. Restore puts the meeting straight back into the library
 * (the next poll shows its row); the nav tab count rides archived_count. */
function dArchLoad(){
  const el=document.getElementById('dsec-archive');
  if(!el)return;
  if(!el.dataset.built){
    el.dataset.built='1';
    el.innerHTML=`<p class="dnote">Set aside: out of the list, search, and Ask.
      Restore brings one back exactly as it was.</p>
    <div id="darchlist"><div class="dloading"><span class="spin"></span></div></div>
    <div id="darcherr" class="derr" hidden></div>`;
  }
  api('/api/archived').then(r=>{DRAWER.archived=r.items||[];dArchRender();});
}
function dArchRender(){
  const box=document.getElementById('darchlist');
  if(!box||DRAWER.archived===null)return;
  box.innerHTML=DRAWER.archived.map(it=>{
    const day=it.date?new Date(it.date+'T12:00:00')
      .toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}):'';
    const arm=DRAWER.dconfirm==='adel:'+it.base;
    return `<div class="drow">
      <div class="grow"><div class="dname">${esc(it.title||it.base)}</div>
        <div class="dsub">${esc(day)}${it.minutes?' &middot; '+it.minutes+' min':''}${
          it.category?' &middot; '+(it.category==='work'?'Work':'Personal'):''}</div></div>
      <button class="iact" type="button" title="Bring it back into the library exactly as it was"
        onclick="dRestore('${escJs(it.base)}',this)">Restore</button>
      <button class="btn danger mini" type="button" onclick="dArchDelAsk('${escJs(it.base)}')">Delete&#8230;</button>
    </div>${arm?`<div class="dckconfirm">Delete &#8220;${esc(it.title||it.base)}&#8221;?
        This permanently removes the transcript, the stored audio, and every cache.
        It cannot be undone.
      <button class="btn mini" type="button" onclick="dConfirmClear()">Cancel</button>
      <button class="btn danger mini" type="button" onclick="dArchDelGo('${escJs(it.base)}',this)">Delete forever</button></div>`:''}`;
  }).join('')||'<div class="dempty">Nothing archived.</div>';
}
async function dRestore(base,btn){
  btn.disabled=true;
  const r=await api('/api/restore_meeting',{base});
  if(!r.ok){btn.disabled=false;dErr('darcherr',r);return;}
  // a live meeting can already hold this name, and restore_meeting then
  // uniquifies. Saying so is the difference between "it worked" and hunting
  // the library for a meeting that is no longer called what you clicked.
  const e=document.getElementById('darcherr');
  if(r.base&&r.base!==base&&e){
    e.hidden=false;
    e.textContent='That name was taken, so it came back as "'+r.base+'".';
  }else dErr('darcherr',{ok:true});
  dArchLoad();refresh();
}
function dArchDelAsk(base){DRAWER.dconfirm='adel:'+base;dArchRender();}
async function dArchDelGo(base,btn){
  btn.disabled=true;
  const r=await api('/api/delete_meeting',{base,confirm:true});
  if(!r.ok){btn.disabled=false;dErr('darcherr',r);return;}
  DRAWER.dconfirm=null;
  dErr('darcherr',{ok:true});
  dArchLoad();refresh();
}

/* ------------------------------------------------------ duplicates -------- *
 * Two transcripts of the same recording. The server scores every pair of
 * same-length meetings by how much text they share and lists the ones over the
 * threshold; this section shows BOTH sides with everything needed to tell them
 * apart, and the human decides. Nothing is ever deleted automatically: a pair
 * can be a genuine re-run, a re-recording of the same standup, or two different
 * meetings that happen to read alike, and only a person can tell. "Keep both"
 * is a real answer and it sticks. */
function dDupesLoad(){
  const el=document.getElementById('dsec-dupes');
  if(!el)return;
  if(!el.dataset.built){
    el.dataset.built='1';
    el.innerHTML=`<p class="dnote">Transcripts that read like the same meeting.
      Compare the two, delete the copy you do not want, or keep both and this
      pair stops being offered.</p>
    <div id="ddupelist"><div class="dloading"><span class="spin"></span></div></div>
    <div id="ddupeerr" class="derr" hidden></div>`;
  }
  DRAWER.dupeBusy=true;
  api('/api/dupes').then(r=>{
    DRAWER.dupes=r.pairs||[];DRAWER.dupeScan=!!r.scanning;
    DRAWER.dupeScanned=!!r.scanned;
    DRAWER.dupeTrunc=!!r.truncated;dDupesRender();
    dErr('ddupeerr',{ok:true});
  }).catch(()=>{
    // a genuine fetch failure. dupeScan stayed TRUE here, so dDupesPoll
    // refetched every 2s forever with no stopping condition while the empty
    // state claimed "Comparing transcripts..." the whole time.
    DRAWER.dupes=DRAWER.dupes||[];DRAWER.dupeScan=false;
    dDupesRender();
    dErr('ddupeerr',{ok:false,error:'The duplicate list could not be loaded.'});
  }).finally(()=>{DRAWER.dupeBusy=false;});
}
// the poll's hook: the section owns its fetch, so the 2s poll only re-fetches
// when the answer may have changed -- a scan was running on the last fetch
// (this is how "Comparing transcripts..." ever resolves), or the tab's polled
// pair count disagrees with what is rendered. Self-stopping either way.
function dDupesPoll(s){
  if(DRAWER.section!=='dupes'||DRAWER.dupeBusy||DRAWER.dupes===null)return;
  const el=document.getElementById('dsec-dupes');
  if(!el||el.hidden)return;
  const dn=(((s&&s.tray)||[]).find(t=>t.kind==='dupe_meetings')||{}).count||0;
  // keyed on the count CHANGING, never on it disagreeing with the rendered
  // list: the tray counts raw cached pairs while the drawer resolves both
  // sides, so one meeting with unparseable metadata would make the two
  // disagree forever and a disagreement condition would refetch every 2s
  if(DRAWER.dupeScan||dn!==DRAWER.dupeSeen){DRAWER.dupeSeen=dn;dDupesLoad();}
}
async function dDupeRescan(btn){
  if(btn)btn.disabled=true;
  try{
    const r=await api('/api/dupe_scan',{go:true});
    DRAWER.dupes=r.pairs||[];DRAWER.dupeScan=!!r.scanning;
    DRAWER.dupeScanned=!!r.scanned;
    DRAWER.dupeTrunc=!!r.truncated;dDupesRender();
  }catch(e){}
  finally{if(btn)btn.disabled=false;}
}
function _dupeSide(side,other,score){
  const day=side.date?new Date(side.date+'T12:00:00')
    .toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}):'';
  const who=(side.speakers||[]).join(', ');
  const arm=DRAWER.dconfirm==='dupdel:'+side.base;
  return `<div class="dupside">
    <div class="grow"><div class="dname">${esc(side.title||side.base)}</div>
      <div class="dsub">${esc(day)}${side.minutes?' &middot; '+side.minutes+' min':''}${
        who?' &middot; '+esc(who):''}</div>
      ${side.source_file?`<div class="dsub">from ${esc(side.source_file)}</div>`:''}</div>
    <a class="dlink" href="#m/${encodeURIComponent(side.base)}"
      onclick="closeDrawer()">Open</a>
    <button class="btn danger mini" type="button"
      onclick="dDupeDelAsk('${escJs(side.base)}')">Delete&#8230;</button>
    ${arm?`<div class="dckconfirm">Delete &#8220;${esc(side.title||side.base)}&#8221;?
      This permanently removes the transcript, the stored audio, and every cache.
      It cannot be undone.
      <button class="btn mini" type="button" onclick="dConfirmClear()">Cancel</button>
      <button class="btn danger mini" type="button"
        onclick="dDupeDelGo('${escJs(side.base)}',this)">Delete forever</button></div>`:''}
  </div>`;
}
function dDupesRender(){
  const box=document.getElementById('ddupelist');
  if(!box||DRAWER.dupes===null)return;
  box.innerHTML=(DRAWER.dupes.map(p=>`<div class="dupepair">
      <div class="dupehdr">${Math.round((p.score||0)*100)}% of the words match</div>
      ${_dupeSide(p.a,p.b,p.score)}
      ${_dupeSide(p.b,p.a,p.score)}
      <div class="dupeacts">
        <button class="btn mini" type="button"
          title="Two different meetings: stop offering this pair"
          onclick="dDupeKeep('${escJs(p.a.base)}','${escJs(p.b.base)}',this)">Keep both</button>
      </div>
    </div>`).join('')
    ||`<div class="dempty">${DRAWER.dupeScan
      ?'Comparing transcripts&#8230;'
      // never-scanned and scanned-and-empty are DIFFERENT answers, and only
      // the server can tell them apart (the cache carries a signature once a
      // scan has written one). The scan is idle-only, so a long batch can keep
      // it from ever running, and asserting "none found" about a comparison
      // that never happened is a claim the drawer cannot support.
      :DRAWER.dupeScanned===false
      ?'Not compared yet. Rescan reads every transcript and compares them.'
      :'No duplicate transcripts found.'}</div>`)
    +`<div class="dupefoot">${DRAWER.dupeTrunc
      ?`<span class="dnote">Pair budget reached: this list may be incomplete.</span>`:''}
      <button class="btn mini" type="button" onclick="dDupeRescan(this)">Rescan</button></div>`;
}
function dDupeDelAsk(base){DRAWER.dconfirm='dupdel:'+base;dDupesRender();}
async function dDupeDelGo(base,btn){
  // the confirm may have been disarmed since this button rendered (Cancel,
  // Escape, another confirm arming): a click on a stale button deletes nothing
  if(DRAWER.dconfirm!=='dupdel:'+base){dDupesRender();return;}
  btn.disabled=true;
  const r=await api('/api/delete_meeting',{base,confirm:true});
  if(!r.ok){btn.disabled=false;dErr('ddupeerr',r);return;}
  DRAWER.dconfirm=null;
  dErr('ddupeerr',{ok:true});
  dDupesLoad();refresh();
}
async function dDupeKeep(a,b,btn){
  btn.disabled=true;
  const r=await api('/api/dupe_ignore',{a,b});
  if(!r.ok){btn.disabled=false;dErr('ddupeerr',r);return;}
  dErr('ddupeerr',{ok:true});
  dDupesLoad();refresh();
}

/* the quiet "archived · view" hint an Archive action leaves behind: small,
 * bottom-center, gone on its own; "view" opens the drawer's Archive section */
let _archHintT=null;
function archHint(){
  let el=document.getElementById('archhint');
  if(!el){
    el=document.createElement('div');
    el.id='archhint';el.className='archhint';
    document.body.appendChild(el);
  }
  el.innerHTML=`archived &middot; <button class="dlink" type="button"
    onclick="openDrawer('archive')">view</button>`;
  el.hidden=false;
  clearTimeout(_archHintT);
  _archHintT=setTimeout(()=>{el.hidden=true;},6000);
}

/* ============================================================================
   Finale: THE KEYBOARD LAYER and DRAG-AND-DROP QUEUEING. The last build pass
   before the default flips. One keydown dispatcher (kbKey) owns every new
   shortcut behind the typing guard; the meeting page's own mKey keeps n/p,
   its slash, and Cmd-F, so the dispatcher bows out of everything but Escape
   there. Dropping audio anywhere uploads it through POST /api/upload and a
   synthetic timeline row holds the spot until the REAL waiting row lands.
   The layer stays invisible until asked for: ? opens the key sheet (phase 4),
   the one place key caps show.
   ============================================================================ */

/* ------------------------------------------------------- keyboard layer ---- *
 * j/k walk a visible focus ring down/up the timeline (the rows are already
 * tabindex=0 and :focus-visible styled). Section headers are BUTTONS and ride
 * the ring too: a folded month is otherwise unreachable from the keyboard, and
 * Enter on a header is its own native click. Enter acts by row state, e peeks
 * a ready row, / focuses search, Escape in search clears it back to the list,
 * Enter in search opens the first Said in line, ? opens the key sheet, and
 * Escape on the meeting page (with nothing else open) returns to the list.
 * html{scroll-behavior:smooth} owns the scroll feel and the reduced-motion
 * block flips it to auto, so plain scrollIntoView honors both. */
let kbLast=null;             // the row id that last held the ring
const KB_RING='#timeline .row[tabindex], #timeline .lrow, #timeline .ghead';
// the edit layers that slide over the list and the page: the naming panel and
// the drawer, and the key sheet over both. While one is open it owns every
// key, so j/k, /, Enter and the page's own keys never reach the rows or the
// transcript underneath it (focus used to jump off the panel onto a hidden
// row), and the ring is never put back under it. Escape stays each layer's
// own, and closes it.
function kbLayerOpen(){return !!NP||!!(DRAWER&&DRAWER.open)||KSHEET;}

/* the key sheet (docs/ui-refresh phase 4): ? opens one card listing the keys
 * phases 1 to 3 shipped. It never opens while typing (a ? in a field is a
 * character), under the naming panel or the drawer, or over an open popover.
 * While it is open it owns every key: Escape or ? closes it, Tab stays on its
 * close button, and a click outside (the veil) closes it too. Closing hands
 * focus back to where it was. */
let KSHEET=false,KSHEET_BACK=null;
function keySheetKey(e,typing,blocked){
  return e.key==='?'&&!e.metaKey&&!e.ctrlKey&&!e.altKey&&!typing&&!blocked;
}
function keySheetOpen(){
  const k=$('#keysheet'),v=$('#keyveil');if(!k||!v)return;
  KSHEET=true;KSHEET_BACK=document.activeElement;
  v.hidden=false;k.hidden=false;
  const b=k.querySelector('button');if(b)b.focus();
}
function keySheetClose(){
  if(!KSHEET)return;
  KSHEET=false;
  const k=$('#keysheet'),v=$('#keyveil');if(k)k.hidden=true;if(v)v.hidden=true;
  const b=KSHEET_BACK;KSHEET_BACK=null;
  if(b&&b.focus&&document.body.contains(b))b.focus({preventScroll:true});
}
function kbRows(){return [...document.querySelectorAll(KB_RING)];}
function kbFocusRow(el){
  if(!el)return;
  el.focus({preventScroll:true});
  kbLast=el.dataset.id||null;     // a header carries no id: the ring is not restored to it
  if(paned())paneReveal(el);      // two panes: the list pane scrolls, never the reader
  else el.scrollIntoView({block:'nearest'});
}
// a 2s rebuild wipes the DOM (and the ring with it): put the ring back on the
// remembered row when focus fell to <body>. A real click clears the memory,
// so the ring never fights the mouse.
function kbRestore(){
  if(!kbLast||(route.view!=='timeline'&&!PANES)||kbLayerOpen())return;
  if(document.activeElement&&document.activeElement!==document.body)return;
  const k=CSS.escape(kbLast);
  const el=document.querySelector('#timeline .row[data-id="'+k+'"], #timeline .lrow[data-id="'+k+'"]');
  if(el)el.focus({preventScroll:true});
}
document.addEventListener('mousedown',()=>{kbLast=null;});
function kbMove(d){
  const rows=kbRows();if(!rows.length)return;
  const ae=document.activeElement;
  let i=rows.indexOf(ae&&ae.closest?ae.closest(KB_RING):null);
  if(i<0&&kbLast)i=rows.findIndex(r=>r.dataset.id===kbLast);
  // two panes: j/k move the selection, so a ring that is elsewhere (a click
  // in the reader clears it) starts from the reader's own row
  if(i<0&&PANES&&RSEL)i=rows.findIndex(r=>r.dataset.id===RSEL);
  if(i<0){
    // nothing holds the ring yet: land on the first row in view, so j
    // mid-page never yanks the list back to the top
    const top=($('.hdr')||{}).offsetHeight||64;
    i=rows.findIndex(r=>r.getBoundingClientRect().bottom>top);
    kbLand(rows[i<0?0:i]);return;
  }
  kbLand(rows[Math.max(0,Math.min(rows.length-1,i+d))]);
}
// the ring lands on a row; with two panes a meeting row is selected as well,
// and the reader follows once the walk pauses (selectRow's WALK_MS)
function kbLand(el){
  kbFocusRow(el);
  if(PANES&&el&&el.dataset&&el.dataset.id&&rowOpens(el.dataset.state))selectRow(el.dataset.id,true);
}
function kbKey(e){
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  // the open key sheet owns every key: Escape or ? closes it, and Tab stays
  // on its one control
  if(KSHEET){
    if(e.key==='Escape'||e.key==='?'||e.key==='Tab')e.preventDefault();
    if(e.key==='Escape'||e.key==='?')keySheetClose();
    return;
  }
  const ae=document.activeElement,se=$('#search');
  // the typing exceptions, both in the search field: Escape clears it and
  // puts the ring back on the list; Enter opens the first line of the Said
  // in group at its moment, as a click on it does
  if(e.key==='Escape'&&ae===se){
    e.preventDefault();
    se.value='';render();scheduleSearch();se.blur();
    if(route.view==='timeline'||PANES)kbFocusRow(kbRows()[0]);
    return;
  }
  if(e.key==='Enter'&&ae===se){
    if(SAID_TOP){e.preventDefault();openHit(SAID_TOP.base,Number(SAID_TOP.start)||0);}
    return;
  }
  const typing=ae&&(/^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)||ae.isContentEditable);
  // ? opens the key sheet from the list or the page, never while typing and
  // never under a layer or a popover
  if(keySheetKey(e,typing,kbLayerOpen()||!!_popClose)){e.preventDefault();keySheetOpen();return;}
  if(typing)return;   // no shortcut fires while any input has focus
  if(route.view==='meeting'){
    // mKey owns the page's keys (n/p stepping, slash, find): only the Escape
    // cascade's LAST step lives here -- nothing else open means back to the
    // list. Two panes: the list never left, so the ring goes to its row.
    if(e.key==='Escape'&&!e.defaultPrevented&&!_popClose&&!NP&&!DRAWER.open
       &&!(MR&&MR.active)&&!document.getElementById('mcard')){
      e.preventDefault();
      if(PANES)kbFocusRow(selRowEl());else location.hash='';
      return;
    }
    if(!PANES)return;
  }
  // the naming panel, the drawer, or an open popover (Process, a ⋯ menu, a
  // bulk confirm) owns the keys: j, k, x, Enter, e and / never move the
  // ring, the bulk selection, or the reader under it. Its Escape closes it.
  if(kbLayerOpen()||_popClose)return;
  // two panes: / is the list's search from the list, the reader's find elsewhere
  const inList=!!(ae&&ae.closest&&ae.closest('.wrap'));
  if(e.key==='/'){
    if(route.view==='meeting'&&!inList)return;
    e.preventDefault();se.focus();return;}
  if(e.key==='j'||e.key==='k'){e.preventDefault();kbMove(e.key==='j'?1:-1);return;}
  // x: the focused row joins or leaves the bulk selection (its checkbox)
  if(e.key==='x'){
    const r=(e.target instanceof Element)?e.target.closest('#timeline .row[tabindex]'):null;
    if(r&&r.querySelector('.chk:not(.spacer)')){
      e.preventDefault();toggleSel(r.dataset.id,!SEL.has(r.dataset.id));}
    return;
  }
  // two panes: Enter on a meeting row (a To finish link too) selects it into
  // the reader, or wakes the reader's own; the list takes the key, so the
  // reader's Enter (a flag accept, a voice save) never fires with it
  if(PANES&&e.key==='Enter'&&e.target instanceof Element
     &&e.target.matches('#timeline .row[tabindex], #timeline .lrow')
     &&rowOpens(e.target.dataset.state)){
    e.preventDefault();readerOpen(e.target.dataset.id);return;
  }
  // a section header holds the ring too: Enter on one is the button's own
  // click (grpToggle), so stand back and let the browser fire it
  if(e.key==='Enter'&&e.target instanceof Element
     &&e.target.matches('#timeline .ghead'))return;
  // Enter / e act on the focused row itself, never on a focused button in it.
  // A To finish row is a link: Enter there is the link's own, and opens it.
  const row=(e.target instanceof Element&&e.target.matches('#timeline .row[tabindex]'))
    ?e.target:null;
  if(!row)return;
  const id=row.dataset.id,st=row.dataset.state;
  if(e.key==='Enter'){
    e.preventDefault();
    if(st==='ready')openMeeting(id);
    else{
      // waiting / held / failed: focus-within already reveals the hover
      // actions, so Enter just hands focus to the first one
      const b=row.querySelector('.ractions .iact');if(b)b.focus();
    }
  }else if(e.key==='e'&&st==='ready'){
    e.preventDefault();toggleExpand(id);
  }
}
document.addEventListener('keydown',kbKey);

/* ------------------------------------------------ drag-and-drop queueing --- *
 * Window-level dragenter/over shows the full-page drop affordance; drop
 * uploads each audio file SEQUENTIALLY via fetch with the raw File body
 * (fetch exposes no clean upload progress over HTTP/1.1, so the synthetic
 * row carries a spinner, not a percentage). Non-audio files get the inline
 * dismissible note naming the accepted extensions. */
// mirror of the server's allowlist (config.AUDIO_EXTS -- the SAME set the
// folder watcher accepts, so anything droppable is anything watchable);
// test-enforced to match the server exactly
const UPLOAD_EXTS=['.aac','.aiff','.avi','.caf','.flac','.m4a','.m4b','.m4v',
  '.mkv','.mov','.mp3','.mp4','.ogg','.opus','.wav','.webm','.wma'];
function _uploadExtOk(name){
  const i=name.lastIndexOf('.');
  return i>0&&UPLOAD_EXTS.includes(name.slice(i).toLowerCase());
}

/* synthetic timeline rows: one per upload, keyed, pinned above the pinned
 * cluster. In flight it reads like a waiting row; done, it holds the spot
 * (title already the server's FINAL name) until the next poll's real
 * src:<name> row replaces it in the same rebuild; failed, it shows the
 * server's reason with a dismiss x. */
const UPLOADS=[];
function uploadsSig(){return JSON.stringify(UPLOADS.map(u=>[u.key,u.status,u.error,u.name]));}
function uploadsPrune(){
  for(let i=UPLOADS.length;i--;){
    const u=UPLOADS[i];
    if(u.status==='done'&&rowById('src:'+u.name))UPLOADS.splice(i,1);
  }
}
function uploadRowHTML(u){
  const err=u.status==='error';
  return `<div class="row upl" data-state="${err?'failed':'waiting'}" data-upkey="${esc(u.key)}">
    <span class="chk spacer" aria-hidden="true"></span>
    <div class="rbody"><div class="rtitle">${esc(u.title)}</div>
      ${err?`<div class="rmeta err">${esc(u.error)}</div>`:''}</div>
    <div class="rslot">${err
      ?`<button class="iact" type="button" title="Dismiss"
          onclick="uploadDismiss('${escJs(u.key)}')">&#10005;</button>`
      :u.status==='done'
        ?`<span class="rstate spill">queued</span>`
        :`<span class="rstate spill"><span class="spin"></span> uploading&#8230;</span>`}
    </div></div>`;
}
function uploadDismiss(key){
  const i=UPLOADS.findIndex(u=>u.key===key);
  if(i>=0)UPLOADS.splice(i,1);
  render();
}

let _upSeq=0;
async function uploadOne(file){
  const u={key:'up'+(++_upSeq),title:file.name,status:'uploading'};
  UPLOADS.push(u);render();
  let r;
  try{
    const res=await fetch('/api/upload?name='+encodeURIComponent(file.name),
      {method:'POST',body:file});
    r=await res.json();
  }catch(e){r={error:'upload failed: connection lost'};}
  if(r&&r.ok){
    u.status='done';u.name=r.name;u.title=r.name;   // the final (uniquified) name
    refresh();                                       // the next state has the real row
  }else{
    u.status='error';u.error=(r&&r.error)||'upload failed';
    render();
  }
}
async function uploadFiles(files){
  const audio=files.filter(f=>_uploadExtOk(f.name));
  const other=files.filter(f=>!_uploadExtOk(f.name));
  if(other.length)
    dropNote(`Not audio: ${other.map(f=>f.name).join(', ')}. `
      +`Accepted: ${UPLOAD_EXTS.join(', ')}.`);
  for(const f of audio)await uploadOne(f);   // sequential, one row at a time
}
function dropNote(msg){
  const n=$('#dropnote');if(!n)return;
  n.hidden=false;
  n.innerHTML=`<span class="grow">${esc(msg)}</span>
    <button class="iact" type="button" title="Dismiss" onclick="dropNoteClose()">&#10005;</button>`;
}
function dropNoteClose(){const n=$('#dropnote');if(n){n.hidden=true;n.innerHTML='';}}

/* the full-page affordance: dragenter/leave nest, so a counter decides
 * visibility; dragover must preventDefault or the drop never fires */
let _dragDepth=0;
function _dragFiles(e){
  return !!(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files'));
}
window.addEventListener('dragenter',e=>{
  if(!_dragFiles(e))return;
  e.preventDefault();
  _dragDepth++;$('#dropveil').hidden=false;
});
window.addEventListener('dragover',e=>{if(_dragFiles(e))e.preventDefault();});
window.addEventListener('dragleave',e=>{
  if(!_dragFiles(e))return;
  if(--_dragDepth<=0){_dragDepth=0;$('#dropveil').hidden=true;}
});
window.addEventListener('drop',e=>{
  if(!_dragFiles(e))return;
  e.preventDefault();
  _dragDepth=0;$('#dropveil').hidden=true;
  uploadFiles([...e.dataTransfer.files]);
});
