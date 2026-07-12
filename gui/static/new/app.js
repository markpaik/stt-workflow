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
function _traySub(title,meta,count,href){
  return `<div class="traysub" onclick="location.href=${href}" tabindex="0">
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
        `'/?review='+encodeURIComponent('${escJs(t.target)}')`);
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
        `'/?who='+encodeURIComponent('${escJs(t.target)}')`);
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

  // empty states (quiet, centered), rendered inside the timeline
  if(all.length===0){
    tl.innerHTML=`<p class="empty">Record a meeting from the menu bar.</p>`;
    $('#rail').hidden=true;tl.dataset.sig='EMPTY';return;
  }
  if(rows.length===0){
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
  const sig=JSON.stringify(ordered.map(sigOf))+'|'+sort+'|'+cat+'|'+q+'|'+flaggedOnly;
  if(tl.dataset.sig===sig){drawRail(groups,q.length>0,sort);return;}  // nothing changed
  // never wipe a half-typed name in a needs_name field; a later unchanged poll
  // (or a blur) lets the rebuild happen. Only INPUT/SELECT focus blocks it, so
  // a merely focused row still refreshes.
  const ae=document.activeElement;
  if(ae&&tl.contains(ae)&&/^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName))return;
  tl.dataset.sig=sig;
  tl.innerHTML=groups.map((g,i)=>
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
function render(){if(!S)return;drawPill(S);drawTray(S);
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
// review/who still bridge to the old page until Builder B ports them into the shell
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
// Ask a question: open the meeting page and land focused in its ask input.
// pendingOpen is the focus-after-build flag buildMeeting consumes (no timeouts).
function rmAsk(id){
  closePop();
  if(route.view==='meeting'&&route.base===id){const q=$('#maskq');if(q)q.focus();return;}
  pendingOpen={base:id,ask:true};
  location.hash='#m/'+encodeURIComponent(id);
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
      if(!r.ok){                          // keep the field open, surface the error inline (no alert)
        done=false;
        let err=titleEl.querySelector('.nameerr');
        if(!err){err=document.createElement('div');err.className='nameerr';titleEl.appendChild(err);}
        err.textContent=r.error||'Rename failed';
        inp.focus();
        return;
      }
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
  const pop=$('#rowmenu');
  if(!r.ok){                              // failure stays in the popover, never a browser alert
    if(!pop.hidden)pop.innerHTML=`<div class="ppconfirm">
      <div class="ppctitle">Could not delete</div>
      <div class="ppcnote err">${esc(r.error||'The meeting could not be deleted.')}</div>
      <div class="ppcrow"><button class="btn mini" type="button" onclick="closePop()">Close</button></div>
    </div>`;
    return;
  }
  closePop();refresh();
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
function bulkRename(btn){                  // name input in a popover, no prompt()
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
function bulkDate(btn){                     // date input in a popover, no prompt()
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
function bulkDropAudio(btn){                // confirm popover, no confirm()
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
function bulkDelete(btn){                   // two-step confirm popover, no confirm()
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
  MF=null;MP=null;
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
      hasAudio:!!m.audio,tick:null,autoScroll:false,nowIdx:null};

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
  // after the segments load, below) or focus the ask input
  if(pendingOpen){
    const p=pendingOpen;pendingOpen=null;    // stale-for-another-base also drops here
    if(p.base===base){
      if(p.seek!=null){MP.seekTo=p.seek;if(MP.hasAudio)mSeek(p.seek);}
      if(p.ask){const q=$('#maskq');if(q)q.focus();}
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
  (d.speakers||[]).forEach((w,i)=>MP.color[w]=HUES[i%HUES.length]);
  body.innerHTML=MP.segs.map(mSegHTML).join('')
    ||'<div class="mterr">This transcript has no lines yet.</div>';
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
  const flagged=m.flagged>0?`<span class="mflagnote">${m.flagged} flagged</span>`:'';
  return `<div class="mhead">
    <a class="mback" href="#" onclick="mBack(event)">&#8592; Meetings</a>
    <h1 class="mtitle">${esc(m.title||base)}</h1>
    <div class="mmeta">${bits.join(' &middot; ')}
      <span class="mcat ${dot}" title="${dotTitle}"></span>${strict}${flagged}</div>
  </div>`;
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
  if(a.paused)a.play().catch(()=>{});else a.pause();mSyncPP();}
function mScrub(v){const a=MP&&MP.audio;if(!a)return;a.currentTime=parseFloat(v)||0;mAudioTick();}
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
 * Rendering only. Stepping/actions are Builder B's: reviewStep() is the seam. */
function _mFlagged(m){
  if(!(m.flagged>0))return '';
  return `<button class="mflagged" type="button" onclick="reviewStep()"
    title="Step through the flagged lines">&#9888; ${m.flagged} flagged line${m.flagged>1?'s':''}</button>`;
}
function reviewStep(){/* Builder B: step through and act on the flagged segments */}

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
  box.innerHTML=(d.speakers||[]).map(w=>
    `<span class="mlg"><span class="msdot" style="background:${MP.color[w]}"></span>${esc(w)}</span>`).join('');
}
function mSegHTML(g,i){
  const mm=Math.floor(g.start/60),ss=String(Math.floor(g.start%60)).padStart(2,'0');
  const hue=MP.color[g.who]||'var(--sub)';
  const flag=g.flags&&g.flags.length;
  const seek=MP.hasAudio?` onclick="mSeek(${g.start})"`:'';
  return `<div class="mseg${flag?' flagged':''}" id="mseg${i}"${seek}>
    <span class="mtime mono">${mm}:${ss}</span>
    <span class="mspk"><span class="msdot" style="background:${hue}"></span>${esc(g.who)}${flag?` <span class="mstar" title="Uncertain: ${esc((g.flags||[]).join(', '))}">*</span>`:''}</span>
    <span class="mtext">${esc(g.text)}</span>
  </div>`;
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

/* --------------------------------------------------------- route wiring ---- */
window.addEventListener('hashchange',()=>{route=parseHash();applyRoute();});
window.addEventListener('resize',()=>{if(route.view==='meeting')mStickyTop();});
route=parseHash();
applyRoute();   // deep link (#m/<base>) opens here; if S is not ready yet, render()
                // finishes the build via maybeBuildPending once the first poll lands
