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

// ---- small formatters (digits align via the body's tabular-nums) ----
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

// two-zone ordering (by-date view). The actionable states pin to an UNLABELED
// cluster above every date group, in this fixed order, so a failed April file
// sits at the top rather than buried in April. Everything else (ready) groups
// by its own meeting date. Sorting by one key while grouping by another is what
// fragmented the months; the ready zone therefore sorts AND groups by dateKey.
const PIN_ORDER={recording:0,processing:1,needs_name:2,failed:3,held:4,waiting:5};
const PINNED=new Set(Object.keys(PIN_ORDER));
function dateKey(r){return r.date||((r.when||'').slice(0,10));}   // fall back to last-activity

/* ============================ status pill ============================ *
 * The ONLY place pipeline state appears, one line, by priority:
 *   REC ticking  ->  transcribing N  ->  paused N waiting  ->  N waiting  ->  hidden
 */
function drawPill(s){
  const pill=$('#pill');
  const rec=s.recording;
  const waiting=(s.timeline||[]).filter(r=>r.state==='waiting').length;
  // paused shows the whole queued backlog (waiting + held), not just waiting
  const queued=(s.timeline||[]).filter(r=>r.state==='waiting'||r.state==='held').length;
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
    html=`&#9208; paused${queued?` &middot; ${queued} waiting`:''}`;
  }else if(waiting){
    cls='pill';
    html=`${waiting} waiting`;
  }else if(s.relabel_pending){
    // the quiet relabel-in-progress note (a voice was just named): sub-colored,
    // lowest priority, gone on its own when the relabel finishes
    cls='pill';
    html='applying names&#8230;';
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
 * Amber "needs you" band. Rare, urgent kinds (recorder stall, failures) get one
 * line PER ITEM. Chronic kinds aggregate to one line PER KIND that expands in
 * place: flagged reviews -> "Flagged lines in N meetings", unknown voices ->
 * "N voices need names". With a single meeting/voice, no aggregation: the line
 * links straight through. trayOpen (toggled by trayExpand) folds into the
 * signature so an expanded group survives the 2s polls.
 */
function _trayVerb(label,onclick){
  return `<button class="btn mini tw-verb" type="button" onclick="${onclick}">${label}</button>`;
}
function _trayRow(title,detail,verb){
  return `<div class="trayrow">
    <span class="tw-title">${esc(title)}</span>
    <span class="tw-detail">${esc(detail)}</span>
    ${verb}</div>`;
}
function _trayAgg(label,verb){
  return `<div class="trayrow"><span class="tw-title agg">${esc(label)}</span>${verb}</div>`;
}
function _traySub(title,meta,count,call){
  return `<div class="traysub" onclick="${call}" tabindex="0">
    <span class="ts-title">${esc(title)}</span>
    <span class="ts-meta">${meta}</span>
    ${count?`<span class="ts-count">${esc(count)}</span>`:''}</div>`;
}
// an aggregate of MORE than this many items never expands in the tray: the
// first expansion just rebuilt the 40-row wall inside it (DESIGN, 2026-07-12)
const TRAY_EXPAND_MAX=8;
function drawTray(s){
  const tray=$('#tray');
  const items=s.tray||[];
  if(!items.length){tray.hidden=true;tray.dataset.sig='';tray.innerHTML='';return;}
  const stalls=items.filter(t=>t.kind==='recorder_stall');
  const fails=items.filter(t=>t.kind==='failed');
  const reviews=items.filter(t=>t.kind==='review');
  const voices=items.filter(t=>t.kind==='unknown_voice');
  // the expand flags AND the library filter fold into the signature so a poll
  // keeps an open group open and an active line active
  const sig=JSON.stringify(items.map(t=>[t.kind,t.title,t.detail,t.target,t.count]))
    +'|'+trayOpen.review+'|'+trayOpen.voices+'|'+flaggedOnly;
  if(!tray.hidden&&tray.dataset.sig===sig)return;   // unchanged: don't rebuild
  tray.dataset.sig=sig;tray.hidden=false;

  let h=`<div class="trayhdr">Needs you</div>`;
  // rare + urgent: one line each, acted on in place
  for(const t of stalls)
    h+=_trayRow(t.title,t.detail,_trayVerb('Fix',`trayAct('recorder_stall','${escJs(t.target)}')`));
  for(const t of fails)
    h+=_trayRow(t.title,t.detail,_trayVerb('Retry',`trayAct('failed','${escJs(t.target)}')`));

  // flagged reviews: 1 -> direct; 2..8 -> one line that expands to per-meeting
  // rows; MORE than 8 -> the line FILTERS the library to flagged rows instead
  // (full-size rows out there, never a second smaller library in here) and
  // reads as active while the filter is on
  if(reviews.length===1)
    h+=_trayRow(reviews[0].title,reviews[0].detail,
        _trayVerb('Review &#8594;',`trayAct('review','${escJs(reviews[0].target)}')`));
  else if(reviews.length>TRAY_EXPAND_MAX){
    h+=`<div class="trayrow${flaggedOnly?' active':''}"><span class="tw-title agg">${
      esc(`Flagged lines in ${reviews.length} meetings`)}</span>${
      _trayVerb(flaggedOnly?'Showing &#10005;':'Review &#8594;','flaggedToggle()')}</div>`;
  }
  else if(reviews.length>1){
    h+=_trayAgg(`Flagged lines in ${reviews.length} meetings`,
        _trayVerb(trayOpen.review?'Hide':'Review &#8594;',`trayExpand('review')`));
    if(trayOpen.review)h+=reviews.map(t=>{
      const r=rowById(t.target);
      const meta=r&&r.date?esc(shortDate(r.date)):'';
      return _traySub(t.title,meta,t.count+' to check',
        `openReviewBadge('${escJs(t.target)}')`);
    }).join('');
  }

  // unknown voices: 1 -> direct; 2..8 -> one line that expands to per-voice
  // rows; more than 8 never expands (name them one at a time, largest first)
  if(voices.length===1)
    h+=_trayRow(voices[0].title,voices[0].detail,
        _trayVerb('Name &#8594;',`trayAct('unknown_voice','${escJs(voices[0].target)}')`));
  else if(voices.length>TRAY_EXPAND_MAX){
    h+=_trayAgg(`${voices.length} voices need names`,
        _trayVerb('Name &#8594;',`trayAct('unknown_voice','${escJs(voices[0].target)}')`));
  }
  else if(voices.length>1){
    h+=_trayAgg(`${voices.length} voices need names`,
        _trayVerb(trayOpen.voices?'Hide':'Name &#8594;',`trayExpand('voices')`));
    if(trayOpen.voices)h+=voices.map(t=>{
      const n=t.count;
      return _traySub(t.title,'heard in '+n+' meeting'+(n!==1?'s':''),'',
        `openNamePanelByUid('${escJs(t.target)}')`);
    }).join('');
  }
  tray.innerHTML=h;
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
      // the elapsed clock is the one piece of true mono data in a row slot
      const state=row.paused
        ?`&#9208; paused <span id="recRowClock" class="mono">${clock(row.elapsed_secs)}</span>`
        :`<span class="capdot"></span>capturing <span id="recRowClock" class="mono">${clock(row.elapsed_secs)}</span>`;
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
             <button class="iact" type="button" onclick="rowDelete('${escJs(row.id)}',event)" title="Delete">&#10005;</button>`)}
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
             <button class="iact" type="button" onclick="rowDelete('${escJs(row.id)}',event)" title="Delete">&#10005;</button>`)}
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
      // the review count lives INSIDE the meta line as plain amber TEXT
      // (no chip); forty chip-wearing rows read as a wall of warnings. Still
      // click-to-review via the same bridge.
      if(row.review_substantial)
        bits.push(`<span class="rev" onclick="openReviewBadge('${escJs(row.id)}')" title="Step through the flagged segments">${row.review_substantial} to check</span>`);
      else if(row.review_minor)
        bits.push(`<span class="rev minor" onclick="openReviewBadge('${escJs(row.id)}')" title="Minor crumbs to skim">${row.review_minor} minor</span>`);
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
          <div class="rtitle">${esc(row.title)}</div>
          <div class="rmeta">${bits.join(' &middot; ')}</div>
          ${row.has_summary&&row.summary?`<div class="rsummary">${esc(row.summary)}</div>`:''}
          ${exp}
        </div>
        <div class="rslot">
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
  const open=row.state==='ready'&&OPEN.has(row.id);
  return `<div class="row${open?' open':''}" data-state="${esc(row.state)}" data-id="${esc(row.id)}" tabindex="0">`
    +gutter(row)+bodyAndSlot(row)+`</div>`;
}

// fields that decide whether a rebuild is needed -- elapsed_secs is deliberately
// excluded (the 1s ticker owns it) so a recording never thrashes the list.
// An expanded row folds its open flag (and the full summary/steps it shows)
// into the signature, so the 2s poll re-renders it still open.
function sigOf(r){
  const open=r.state==='ready'&&OPEN.has(r.id);
  const m=open?(meetingByBase(r.id)||{}):null;
  return [r.id,r.state,r.title,r.date,r.pct,r.stage,r.eta,r.category,
  r.review_substantial,r.review_minor,r.has_summary,r.summary,r.size_mb,r.est_minutes,
  r.held,r.error,r.suggested_title,r.suggested_date,r.paused,r.has_audio,r.minutes,
  (r.speakers||[]).join(','),
  open?1:0,m?(m.summary||''):'',m?(m.next_steps||[]).join(''):''];}

function drawTimeline(s){
  const tl=$('#timeline');
  const cat=$('#filter').value;
  const q=$('#search').value.trim().toLowerCase();
  const sort=$('#sort').value;

  const all=s.timeline||[];
  const rows=all.filter(r=>{
    if(cat&&r.state==='ready'&&r.category!==cat)return false;   // tag filter: ready rows only
    // the tray's >8 flagged aggregate: ready rows with nothing to check drop
    // out; the pinned actionable cluster (recording..waiting) stays visible
    if(flaggedOnly&&r.state==='ready'&&!(r.review_substantial>0))return false;
    if(q){
      // match the visible name (a needs_name row shows its suggested title) plus speakers
      const hay=((r.title||'')+' '+(r.suggested_title||'')+' '+((r.speakers||[]).join(' '))).toLowerCase();
      if(!hay.includes(q))return false;
    }
    return true;
  });

  // synthetic upload rows: prune the ones whose REAL waiting row has landed
  // (the same rebuild adds one and drops the other, so the swap never
  // flickers), then pin the survivors above everything
  uploadsPrune();
  const upHTML=UPLOADS.map(uploadRowHTML).join('');

  // empty states (quiet, centered), rendered inside the timeline
  if(all.length===0&&!UPLOADS.length){
    tl.innerHTML=`<p class="empty">Record a meeting from the menu bar, or drop an audio file here.</p>`;
    $('#rail').hidden=true;tl.dataset.sig='EMPTY';return;
  }
  if(rows.length===0&&!UPLOADS.length){
    tl.innerHTML=`<p class="empty">No matches.</p>`;
    $('#rail').hidden=true;tl.dataset.sig='NOMATCH:'+cat+':'+q+':'+flaggedOnly;return;
  }

  // ---- ordering + grouping ----
  // by-name: one flat alphabetical list, letter groups (unchanged).
  // by-date: the pinned actionable cluster first (one unlabeled group), then the
  // ready rows sorted AND grouped by the SAME key (meeting date, newest first).
  const groups=[];
  if(sort==='name'){
    const ordered=rows.slice().sort((a,b)=>
      (a.title||'').toLowerCase().localeCompare((b.title||'').toLowerCase())
      ||(b.date||'').localeCompare(a.date||''));
    for(const r of ordered){
      const key=nameBucket(r.title);
      if(!groups.length||groups[groups.length-1].key!==key)groups.push({key,rows:[]});
      groups[groups.length-1].rows.push(r);
    }
  }else{
    const pinned=rows.filter(r=>PINNED.has(r.state)).sort((a,b)=>
      (PIN_ORDER[a.state]-PIN_ORDER[b.state])||(b.when||'').localeCompare(a.when||''));
    const rest=rows.filter(r=>!PINNED.has(r.state)).sort((a,b)=>
      dateKey(b).localeCompare(dateKey(a))||(b.when||'').localeCompare(a.when||''));
    if(pinned.length)groups.push({key:'',pinned:true,rows:pinned});
    for(const r of rest){
      const key=dateBucket(dateKey(r));
      const last=groups[groups.length-1];
      if(!last||last.pinned||last.key!==key)groups.push({key,rows:[]});
      groups[groups.length-1].rows.push(r);
    }
  }

  const ordered=[].concat(...groups.map(g=>g.rows));   // flat, for the change signature
  const sig=JSON.stringify(ordered.map(sigOf))+'|'+sort+'|'+cat+'|'+q+'|'+flaggedOnly
    +'|'+uploadsSig();
  if(tl.dataset.sig===sig){drawRail(groups,q.length>0,sort);return;}  // nothing changed
  // never wipe a half-typed name in a needs_name field; a later unchanged poll
  // (or a blur) lets the rebuild happen. Only INPUT/SELECT focus blocks it, so
  // a merely focused row still refreshes.
  const ae=document.activeElement;
  if(ae&&tl.contains(ae)&&/^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName))return;
  tl.dataset.sig=sig;
  tl.innerHTML=upHTML+groups.map((g,i)=>
    (g.pinned?'':`<div class="mgroup" id="grp-${i}">${esc(g.key)} &middot; ${g.rows.length}</div>`)
    +g.rows.map(rowHTML).join('')).join('');
  drawRail(groups,q.length>0,sort);
}

/* jump rail: regenerated from the ORDERED date groups (the pinned cluster has no
 * rail entry). By date: TODAY/YST first, then a year anchor before each year's
 * first month, months beneath. By name: the letter groups. 3+ groups, no search. */
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
    if(g.key==='Today')return _railBtn(i,g,'TDY');
    if(g.key==='Yesterday')return _railBtn(i,g,'YST');
    if(g.key==='Undated')return _railBtn(i,g,'NA');
    // a month group: anchor its year the first time that year appears
    const yr=(g.key.match(/\d{4}/)||[])[0]
      ||(g.rows[0]&&g.rows[0].date?g.rows[0].date.slice(0,4):'');
    let head='';
    if(yr&&yr!==lastYr){head=`<div class="railyr">${yr}</div>`;lastYr=yr;}
    return head+_railBtn(i,g,g.key.slice(0,3));
  }).join('');
}
function railJump(i){const el=document.getElementById('grp-'+i);
  if(el)el.scrollIntoView({behavior:'smooth',block:'start'});}

/* ============================ render loop ============================ */
// While a meeting page is open the 2s poll keeps the pill/tray/bulk regions
// live (drawPill/drawTray/applySel) but must NOT rebuild the meeting document
// (guard on route); a pending deep-link build completes once S has arrived.
function render(){if(!S)return;drawPill(S);drawTray(S);drawDrawer(S);
  const fc=$('#flagchip');if(fc)fc.hidden=!flaggedOnly;
  if(route&&route.view==='meeting'){applySel();maybeBuildPending();return;}
  drawTimeline(S);afterRender();}
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
$('#flagchip').onclick=()=>flaggedClear();
// ready-row click-to-expand: clicking the row body toggles the summary peek;
// its controls (buttons, links, checkbox, category dot, inputs, the review
// count) never do, and neither does a click that is selecting text
$('#timeline').addEventListener('click',e=>{
  const row=e.target.closest('.row');
  if(!row||row.dataset.state!=='ready')return;
  if(e.target.closest('button,a,input,select,textarea,label,.rev'))return;
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
// which aggregate groups are expanded; folded into drawTray's signature so a
// poll never collapses one the user just opened
let trayOpen={review:false,voices:false};
function trayExpand(group){trayOpen[group]=!trayOpen[group];drawTray(S);}
// the >8 flagged aggregate's library filter: only ready rows with lines to
// check show (the pinned actionable cluster stays); the header chip clears it
let flaggedOnly=false;
function flaggedToggle(){flaggedOnly=!flaggedOnly;render();}
function flaggedClear(){flaggedOnly=false;render();}
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
  if(kind==='review'){openReviewBadge(target);return;}
  if(kind==='unknown_voice')openNamePanelByUid(target);
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
function rowRetry(id){                    // re-run a failed source still in the folder
  const r=rowById(id);if(!r||!r.source_file)return;
  api('/api/run',{files:[r.source_file],...runOpts()}).then(refresh);
}
// in-shell route: set the hash and let the hashchange handler open the page
// (Builder A, at the bottom). No navigation away; back/forward and deep links work.
function openMeeting(id){location.hash='#m/'+encodeURIComponent(id);}
// a row's review count / a tray review verb: open the meeting page with the
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
  if(route.view==='meeting'&&route.base===id)location.hash='';
  archHint();   // the quiet "archived · view" note pointing at the drawer's Archive
  refresh();});}
function rmRename(id){                    // inline: the title becomes an input,
  closePop();                             // on the row OR on the meeting page
  const onPage=route.view==='meeting'&&route.base===id;
  const titleEl=onPage
    ?document.querySelector('#meetingpage .mtitle')
    :(r=>r&&r.querySelector('.rtitle'))(document.querySelector('.row[data-id="'+CSS.escape(id)+'"]'));
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
      if(!r.ok){                          // keep the field open, surface the error inline (no alert)
        done=false;
        let err=titleEl.querySelector('.nameerr');
        if(!err){err=document.createElement('div');err.className='nameerr';titleEl.appendChild(err);}
        err.textContent=r.error||'Rename failed';
        inp.focus();
        return;
      }
      // a rename re-stamps the folder, so the base CHANGES: re-route the open
      // page to the new base (the hashchange handler rebuilds it there)
      if(onPage&&r.base&&r.base!==id){location.hash='#m/'+encodeURIComponent(r.base);return;}
    }
    if(onPage)titleEl.textContent=cur;    // cancelled / unchanged: restore the title
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
  if(route.view==='meeting'&&route.base===id)location.hash='';
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
  const r=await api('/api/bulk',{bases,action,value,...(extra||{})});
  const fails=(r.results||[]).filter(x=>!x.ok);
  if(fails.length)
    bulkResult(`${fails.length} of ${bases.length} could not be done`,
      fails.slice(0,8).map(f=>esc(f.base)+': '+esc(f.error||'failed')).join('<br>'),true);
  else if(r.freed_mb)
    bulkResult('Done',`Freed ${r.freed_mb} MB of audio.`,false);
  if(action==='archive'&&!fails.length)archHint();   // quiet pointer to the drawer's Archive
  SEL.clear();refresh();
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
  const n=_selReady().length;if(!n)return;
  const pop=$('#rowmenu');pop.dataset.rowid='';
  openPop(pop,btn,()=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Rename ${n} selected</div>
      <div class="ppcnote">One name for all of them. Each keeps its own date in the filename, so recurring meetings stay separate folders.</div>
      <input class="ppinput" type="text" placeholder="New name" aria-label="New name for all selected">
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn primary mini" type="button" onclick="bulkRenameGo()">Rename</button>
      </div></div>`;
  });
  const inp=pop.querySelector('.ppinput');
  if(inp){inp.focus();inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();bulkRenameGo();}};}
}
function bulkRenameGo(){
  const inp=$('#rowmenu .ppinput'),nm=inp?inp.value.trim():'';
  if(!nm)return;
  closePop();bulk('rename',nm);
}
function bulkDate(btn){                     // date input in a popover, never a native prompt
  const n=_selReady().length;if(!n)return;
  const pop=$('#rowmenu');pop.dataset.rowid='';
  openPop(pop,btn,()=>{
    pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Set the date for ${n} selected</div>
      <div class="ppcnote">The folder name is re-stamped to match. Each keeps its own name.</div>
      <input class="ppinput" type="date" aria-label="New date for all selected">
      <div class="ppcrow">
        <button class="btn mini" type="button" onclick="closePop()">Cancel</button>
        <button class="btn primary mini" type="button" onclick="bulkDateGo()">Set date</button>
      </div></div>`;
  });
  const inp=pop.querySelector('.ppinput');
  if(inp){inp.focus();inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();bulkDateGo();}};}
}
function bulkDateGo(){
  const inp=$('#rowmenu .ppinput'),d=inp?inp.value.trim():'';
  if(!d)return;
  closePop();bulk('date',d);
}
function bulkDropAudio(btn){                // confirm popover, never a native dialog
  const n=_selReady().length;if(!n)return;
  const pop=$('#rowmenu');pop.dataset.rowid='';
  openPop(pop,btn,()=>{
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
  const n=_selReady().length;if(!n)return;
  const pop=$('#rowmenu');pop.dataset.rowid='';
  openPop(pop,btn,()=>{
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
 * Debounced full-text (>=3 chars) into #searchhits; a hit opens the in-shell
 * meeting page (#m/<base>) and seeks the audio to the hit's moment via mSeek
 * once the page is built. The client-side title filter is A's, on #search. */
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
        return `<div class="hit" onclick="openHit('${escJs(h.base)}',${Number(h.start)||0})" title="Open the transcript at this moment">
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
  kbRestore();
  const rm=$('#rowmenu');
  if(rm.dataset.open&&rm.dataset.rowid){
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
  if(route.view==='meeting')enterMeeting(route.base);
  else exitMeeting();
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
  MF=null;MP=null;MR=null;
}
// called by the poll's render() while a meeting is open: finish a deep-link build
// that was waiting for S, without ever rebuilding an already-built page
function maybeBuildPending(){
  if(route.view!=='meeting')return;
  if(!MP||MP.base!==route.base||MP.pending)buildMeeting(route.base);
}
function mBack(ev){if(ev)ev.preventDefault();location.hash='';}   // fires hashchange -> timeline

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
      spkOpts:[],people:[],spanStop:null,pendingReview:false};

  page.innerHTML=
    _mHeader(m,base)
    +(m.audio?_mAudioBar():'')
    +_mSummary(m)
    +_mFlagged(m)
    +_mTranscriptShell()
    +_mAsk(base);

  if(m.audio){mAudioInit(base);mStickyTop();}
  mAskInit(base);
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
  MP.spkOpts=d.speaker_options||[];
  MP.people=d.people||[];
  (d.speakers||[]).forEach((w,i)=>MP.color[w]=HUES[i%HUES.length]);
  body.innerHTML=mBodyHTML();
  mLegend(d);
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
  // a review chip / tray verb opened this page: start stepping on the first flag
  if(MP.pendingReview){MP.pendingReview=false;if(MP.segs.length)reviewStart(0);}
}

/* --------------------------------------------------------- header block ---- */
function _mHeader(m,base){
  const bits=[];
  if(m.date)bits.push(esc(shortDate(m.date)));
  if(m.minutes!=null)bits.push(m.minutes+' min');
  if(m.speakers&&m.speakers.length)bits.push(esc(m.speakers.join(', ')));
  const dot=(m.category==='work'||m.category==='personal')?m.category:'none';
  const dotTitle=dot==='work'?'Work':dot==='personal'?'Personal':'Untagged';
  const strict=m.strict?'<span class="mchip">strict</span>':'';
  const nfl=(m.flagged||0)+(m.flagged_minor||0);
  const flagged=nfl>0?`<span class="mflagnote">${nfl} flagged</span>`:'';
  // everything a row can do, the page can do: the same ⋯ menu, and the same
  // cyclable category dot (untagged -> work -> personal), optimistic
  return `<div class="mhead">
    <a class="mback" href="#" onclick="mBack(event)">&#8592; Meetings</a>
    <button class="iact mhmenu" id="mmenu" type="button" title="Export, copy, rename, redo&#8230;"
      aria-label="Meeting actions" onclick="mMenu(event)">&#8943;</button>
    <h1 class="mtitle">${esc(m.title||base)}</h1>
    <div class="mmeta">${bits.join(' &middot; ')}
      <button class="mcat ${dot}" id="mcatdot" type="button"
        title="${dotTitle} (click to change tag)" onclick="mCycleCat(event)"></button>${strict}${flagged}</div>
  </div>`;
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
function mLegend(d){
  const box=$('#mlegend');if(!box)return;
  box.innerHTML=(d.speakers||[]).map(w=>{
    const dot=`<span class="msdot" style="background:${MP.color[w]}"></span>`;
    const uid=mUnknownUid(w);
    // an unnamed voice ("Speaker N" with an unknown-registry entry that lists
    // this meeting): its legend chip opens the naming slide-over
    return uid
      ?`<button class="mlg unk" type="button" title="Who is this? Listen and name this voice"
          onclick="openNamePanelByUid('${escJs(uid)}')">${dot}${esc(w)}<span class="mlgq">?</span></button>`
      :`<span class="mlg">${dot}${esc(w)}</span>`;
  }).join('');
}
function mUnknownUid(display){
  const u=(S&&S.unknowns||[]).find(u=>u.display===display&&!u.archived
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
  if(!MP.segs.length)return '<div class="mterr">This transcript has no lines yet.</div>';
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
  const ae=document.activeElement,typing=ae&&/^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName);
  if((e.metaKey||e.ctrlKey)&&(e.key==='f'||e.key==='F')){
    e.preventDefault();const f=$('#mfind');if(f){f.focus();f.select();}
  }else if(e.key==='/'&&!typing){
    e.preventDefault();const f=$('#mfind');if(f)f.focus();
  }else if(!typing&&(e.key==='n'||e.key==='p')){
    // n/p step the flag review in place; both skip while typing in any input
    if(MR&&MR.active){e.preventDefault();reviewGo(e.key==='n'?1:-1);}
    else if(e.key==='n'&&document.getElementById('mflagbar')){e.preventDefault();reviewStep();}
  }else if(e.key==='Escape'&&!typing&&!NP){
    // leave the stepper / close an open card (the naming panel owns its Escape)
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
  const hdr=$('.hdr'),bar=$('#maudio');
  if(hdr&&bar)bar.style.top=hdr.offsetHeight+'px';
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
let MR=null;    // flag stepper state: {items,i,active} (speaker options ride on MP)

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
 * target (an original index from the response) scrolls into view. */
async function mReloadSegs(target){
  if(!MP)return;
  const base=MP.base;
  let d;
  try{d=await api('/api/transcript?base='+encodeURIComponent(base));}
  catch(e){d={error:'The transcript could not be reloaded.'};}
  if(route.view!=='meeting'||route.base!==base)return;
  const body=$('#mtbody');if(!body)return;
  if(d.error){body.innerHTML='<div class="mterr">'+esc(d.error)+'</div>';return;}
  mCloseCard();
  MF=null;{const f=$('#mfind');if(f)f.value='';}mFindCount();
  MP.segs=d.segments||[];
  MP.spkOpts=d.speaker_options||[];
  MP.people=d.people||[];
  MP.color={};(d.speakers||[]).forEach((w,i)=>MP.color[w]=HUES[i%HUES.length]);
  MP.nowIdx=null;
  body.innerHTML=mBodyHTML();
  mLegend(d);
  mSyncFlagStrip();
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

/* ------------------------------------------------------- the flag stepper -- */
// the strip is the controller: the first click starts on the first flag,
// later clicks (and n/p) step through the rest
function reviewStep(){if(MR&&MR.active)reviewGo(1);else reviewStart(0);}
async function reviewStart(at){
  if(!MP||!MP.built)return;
  const base=MP.base;
  let d;
  try{d=await api('/api/review?base='+encodeURIComponent(base));}
  catch(e){d={error:'The review list could not be loaded.'};}
  if(route.view!=='meeting'||route.base!==base)return;
  if(d.error)return;
  if(!d.items||!d.items.length){MR=null;mSyncFlagStrip();refresh();return;}
  MR={items:d.items,i:Math.min(at||0,d.items.length-1),active:true};
  // the review list's speaker options are fresher than the page load's copy
  if(d.speakers&&d.speakers.length)MP.spkOpts=d.speakers;
  if(d.people)MP.people=d.people;
  renderReviewCard();
}
function reviewGo(d){
  if(!MR||!MR.active)return;
  const j=MR.i+d;
  if(j<0)return;
  if(j>=MR.items.length){reviewExit();return;}   // stepped past the last: done
  MR.i=j;renderReviewCard();
}
function reviewExit(){MR=null;mCloseCard();mSyncFlagStrip();}
// the array position of a review item's segment: matched by ORIGINAL index,
// with start-time proximity as the fallback (same spirit as the server's own
// _locate_segment cross-check)
function _revPos(it){
  let p=MP.segs.findIndex(g=>g.index===it.index);
  if(p<0)p=MP.segs.findIndex(g=>Math.abs(g.start-it.start)<0.25);
  return p;
}
function renderReviewCard(){
  const it=MR.items[MR.i];
  const p=_revPos(it);
  const el=p>=0?document.getElementById('mseg'+p):null;
  if(!el){reviewExit();return;}    // transcript changed underneath: bow out quietly
  mCloseCard();
  el.classList.add('mrev');
  const alts=(it.alt||[]).map((a,k)=>`<div class="mcalt">Second engine heard
      &#8220;<b>${esc(a.theirs||'(nothing)')}</b>&#8221; where this says
      &#8220;${esc(a.ours||'(nothing)')}&#8221;
      <button class="iact" type="button" onclick="reviewUseAlt(${k})"
        title="Swap the second engine&#8217;s version into the text below">Use it</button></div>`).join('');
  const card=mCardEl(`
    <div class="mcrow mchead">
      <span class="mcflag">&#9888; ${esc(it.flags.join(', '))}${it.minor?' &middot; minor':''}</span>
      <span class="mcpos">${MR.i+1} of ${MR.items.length}</span>
    </div>
    ${alts}
    <div class="mcrow">
      ${mSpkSelect('mcspk',it.speaker)}
      <textarea id="mctext" class="mctext" aria-label="Corrected text">${esc(it.text)}</textarea>
    </div>
    <div class="mcrow mcbtns">
      ${MP.hasAudio?`<button class="iact" type="button" onclick="reviewPlay()" title="Play just this stretch of the recording">&#9654; Play clip</button>`:''}
      <span class="grow"></span>
      <button class="btn mini" type="button" onclick="reviewGo(1)" title="Leave it flagged and look at the next one">Skip</button>
      <button class="btn mini" type="button" onclick="reviewApply('accept')" title="The speaker and text are right: clear the flag">Accept as-is</button>
      <button class="btn primary mini" type="button" onclick="reviewApply('edit')" title="Save the corrected speaker/text back to the transcript files">Save changes</button>
    </div>
    <div id="mcerr" class="mcerr" hidden></div>`,el,'mrevcard');
  mWireNew(card.querySelector('#mcspk'));
  el.scrollIntoView({block:'center'});
  reviewPlay();
  mSyncFlagStrip();
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
async function reviewApply(action){
  const it=MR.items[MR.i];
  // the item carries the segment's ORIGINAL json index; start is the server's
  // cross-check in case the file changed since the list was fetched
  const body={base:MP.base,index:it.index,start:it.start,action};
  if(action==='edit'){
    const v=$('#mcspk').value;
    if(v==='__new__'){mCardErr('Pick or name the speaker first.');return;}
    body.text=$('#mctext').value;body.speaker=v;
  }
  const r=await api('/api/review',body);
  if(!r.ok){mCardErr(r.error||'Save failed');return;}
  if(r.merged){
    // the reassignment folded neighbors into one turn: every later index in
    // this pre-fetched list may be stale. Reload, then refetch the list.
    await mReloadSegs(r.index);
    reviewStart(0);
    return;
  }
  if(action==='edit'&&(body.speaker||'').startsWith('name:')){
    await mReloadSegs();             // a new person joined: fresh legend/colors
  }else{
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
  }
  MR.items.splice(MR.i,1);           // resolved: counts update optimistically
  if(!MR.items.length){reviewExit();refresh();return;}
  if(MR.i>=MR.items.length)MR.i=MR.items.length-1;
  renderReviewCard();
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
    const cur=MR.items[MR.i];
    MR.items=MR.items.filter(x=>!x.minor);
    if(!MR.items.length)reviewExit();
    else{
      let ni=MR.items.indexOf(cur);
      if(ni<0)ni=Math.min(MR.i,MR.items.length-1);
      MR.i=ni;renderReviewCard();
    }
  }
  mSyncFlagStrip();
  refresh();
}

/* ----------------------------------------------------- the edit card ------- */
function mEdit(i,ev){
  if(ev)ev.stopPropagation();
  if(MR&&MR.active)reviewExit();     // one repair mode at a time
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
  if(MR&&MR.active)reviewExit();
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
let NP=null;    // {uid} while the panel is open
function openNamePanelByUid(uid){
  const u=(S&&S.unknowns||[]).find(x=>x.uid===uid);
  openNamePanel(uid,u?u.display:uid);
}
async function openNamePanel(uid,display){
  const panel=$('#namepanel'),veil=$('#nameveil');
  if(!panel||!veil)return;
  NP={uid};
  veil.hidden=false;panel.hidden=false;
  panel.innerHTML=`
    <div class="nphead">
      <h2 class="nptitle">Who is ${esc(display)}?</h2>
      <button class="iact" type="button" title="Close" onclick="closeNamePanel()">&#10005;</button>
    </div>
    <p class="npnote">Listen to this voice: the clip is their longest turn in each meeting
      they were heard in. Typing an <b>existing</b> name merges this voice into that person.
      Every past and future meeting relabels automatically.</p>
    <div id="npclips" class="npclips"><span class="spin"></span></div>
    <div class="npfield">
      <input type="text" id="npname" placeholder="Person&#8217;s name" autocomplete="off" spellcheck="false"
        aria-label="This voice belongs to"
        oninput="pnFilter()" onfocus="pnFilter()" onblur="setTimeout(pnClose,150)" onkeydown="pnKey(event)">
      <div id="npdd" class="npdd" hidden></div>
    </div>
    <div class="npbtns">
      <button class="btn danger mini" type="button" onclick="npForget()"
        title="A false detection (music, crosstalk, an echo): remove this voice entirely">Not a real speaker</button>
      <span class="grow"></span>
      <button class="btn mini" type="button" onclick="closeNamePanel()">Cancel</button>
      <button class="btn primary mini" id="npsave" type="button" onclick="npSave()">Save name</button>
    </div>
    <div id="nperr" class="mcerr" hidden></div>`;
  $('#npname').focus();
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
       :`<audio controls
           onplay="document.querySelectorAll('audio').forEach(a=>{if(a!==this)a.pause()})"
           src="/api/snippet?speaker=${encodeURIComponent(uid)}&secs=45"></audio>`);
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
async function npSave(){
  if(!NP)return;
  const n=($('#npname')?$('#npname').value:'').trim();
  const err=$('#nperr');
  if(!n){if(err){err.hidden=false;err.textContent='Type a name first.';}return;}
  const btn=$('#npsave');if(btn){btn.disabled=true;btn.innerHTML='Saving&#8230;';}
  const r=await api('/api/name',{uid:NP.uid,name:n});
  if(!r.ok){
    if(btn){btn.disabled=false;btn.textContent='Save name';}
    if(err){err.hidden=false;err.textContent=r.error||'Could not save the name.';}
    return;
  }
  closeNamePanel();refresh();   // the quiet relabel note rides the next poll
}
async function npForget(){
  if(!NP)return;
  const r=await api('/api/forget',{uid:NP.uid});
  if(!r.ok){const err=$('#nperr');if(err){err.hidden=false;err.textContent=r.error||'Could not remove this voice.';}return;}
  closeNamePanel();refresh();
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

/* --------------------------------------------------------- route wiring ---- */
window.addEventListener('hashchange',()=>{route=parseHash();applyRoute();});
window.addEventListener('resize',()=>{if(route.view==='meeting')mStickyTop();});
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
      </div>`;
  }
  DRAWER.open=true;v.hidden=false;d.hidden=false;
  drawerGo(section||DRAWER.section||'settings');
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
  ['settings','speakers','history','archive'].forEach(k=>{
    const el=document.getElementById('dsec-'+k);
    if(el)el.hidden=(k!==sec);
  });
  if(sec==='settings')dSettingsDraw(S||{});
  else if(sec==='speakers')dSpeakersDraw(S||{});
  else if(sec==='history')dHistLoad();     // (re)fetches; the filter values persist
  else if(sec==='archive')dArchLoad();
}
// the poll's entry point: cheap when closed; only live sections rebuild, and
// only when their signature changed (History/Archive own their own fetches)
function drawDrawer(s){
  if(!DRAWER.open)return;
  drawerNavSync(s);
  dSettingsDraw(s);
  dSpeakersDraw(s);
}
function drawerNavSync(s){
  const nav=$('#dnav');if(!nav)return;
  const n=(s&&s.archived_count)||0;   // the old page's "Archived · N" lives on the tab
  const sig=DRAWER.section+'|'+n;
  if(nav.dataset.sig===sig)return;
  nav.dataset.sig=sig;
  const tabs=[['settings','Settings'],['speakers','Speakers'],['history','History'],
    ['archive','Archive'+(n?' &middot; '+n:'')]];
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
function _drow(name,sub,ctrl){
  return `<div class="drow"><div class="grow"><div class="dname">${name}</div>
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

  h+=`<div class="dgroup">Recorder</div>`;
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
    `<button class="btn mini" type="button" onclick="dPickFolder('source',this)">Change&#8230;</button>`);
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
// clears whichever two-step confirm is armed and repaints its section
function dConfirmClear(){
  DRAWER.dconfirm=null;DRAWER.renameTo=null;
  dSettingsForce();dSpeakersForce();dArchRender();
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
    (s.unknowns||[]).map(u=>[u.uid,u.display,!!u.archived,(u.meetings||[]).length]),
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
    </div>`;
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
  dErr('darcherr',{ok:true});
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
   No hint overlay anywhere: the layer stays invisible.
   ============================================================================ */

/* ------------------------------------------------------- keyboard layer ---- *
 * j/k walk a visible focus ring down/up the timeline rows (the rows are
 * already tabindex=0 and :focus-visible styled; group headers are divs, so
 * they are skipped by construction). Enter acts by row state, e peeks a ready
 * row, / focuses search, Escape in search clears it back to the list, and
 * Escape on the meeting page (with nothing else open) returns to the list.
 * html{scroll-behavior:smooth} owns the scroll feel and the reduced-motion
 * block flips it to auto, so plain scrollIntoView honors both. */
let kbLast=null;             // the row id that last held the ring
function kbRows(){return [...document.querySelectorAll('#timeline .row[tabindex]')];}
function kbFocusRow(el){
  if(!el)return;
  el.focus({preventScroll:true});
  kbLast=el.dataset.id||null;
  el.scrollIntoView({block:'nearest'});
}
// a 2s rebuild wipes the DOM (and the ring with it): put the ring back on the
// remembered row when focus fell to <body>. A real click clears the memory,
// so the ring never fights the mouse.
function kbRestore(){
  if(!kbLast||route.view!=='timeline')return;
  if(document.activeElement&&document.activeElement!==document.body)return;
  const el=document.querySelector('#timeline .row[data-id="'+CSS.escape(kbLast)+'"]');
  if(el)el.focus({preventScroll:true});
}
document.addEventListener('mousedown',()=>{kbLast=null;});
function kbMove(d){
  const rows=kbRows();if(!rows.length)return;
  const ae=document.activeElement;
  let i=rows.indexOf(ae&&ae.closest?ae.closest('#timeline .row[tabindex]'):null);
  if(i<0&&kbLast)i=rows.findIndex(r=>r.dataset.id===kbLast);
  if(i<0){
    // nothing holds the ring yet: land on the first row in view, so j
    // mid-page never yanks the list back to the top
    const top=($('.hdr')||{}).offsetHeight||64;
    i=rows.findIndex(r=>r.getBoundingClientRect().bottom>top);
    kbFocusRow(rows[i<0?0:i]);return;
  }
  kbFocusRow(rows[Math.max(0,Math.min(rows.length-1,i+d))]);
}
function kbKey(e){
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  const ae=document.activeElement,se=$('#search');
  // the ONE typing exception: Escape in the search field clears it and puts
  // the ring back on the list
  if(e.key==='Escape'&&ae===se){
    e.preventDefault();
    se.value='';render();scheduleSearch();se.blur();
    if(route.view==='timeline')kbFocusRow(kbRows()[0]);
    return;
  }
  const typing=ae&&(/^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)||ae.isContentEditable);
  if(typing)return;   // no shortcut fires while any input has focus
  if(route.view==='meeting'){
    // mKey owns the page's keys (n/p stepping, slash, find): only the Escape
    // cascade's LAST step lives here -- nothing else open means back to the list
    if(e.key==='Escape'&&!e.defaultPrevented&&!_popClose&&!NP&&!DRAWER.open
       &&!(MR&&MR.active)&&!document.getElementById('mcard')){
      e.preventDefault();location.hash='';
    }
    return;
  }
  if(e.key==='/'){e.preventDefault();se.focus();return;}
  if(e.key==='j'||e.key==='k'){e.preventDefault();kbMove(e.key==='j'?1:-1);return;}
  // Enter / e act on the focused row itself, never on a focused button in it
  const row=(e.target instanceof Element&&e.target.matches('#timeline .row[tabindex]'))
    ?e.target:null;
  if(!row)return;
  const id=row.dataset.id,st=row.dataset.state;
  if(e.key==='Enter'){
    e.preventDefault();
    if(st==='ready')openMeeting(id);
    else if(st==='needs_name')acceptMeeting(id);
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
    <span class="cat spacer" style="visibility:hidden"></span>
    <div class="rbody"><div class="rtitle">${esc(u.title)}</div>
      ${err?`<div class="rmeta err">${esc(u.error)}</div>`:''}</div>
    <div class="rslot">${err
      ?`<button class="iact" type="button" title="Dismiss"
          onclick="uploadDismiss('${escJs(u.key)}')">&#10005;</button>`
      :u.status==='done'
        ?`<span class="rstate">queued</span>`
        :`<span class="rstate"><span class="spin"></span> uploading&#8230;</span>`}
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
