const $=q=>document.querySelector(q);
function recElapsed(startedAt){
  const t=Date.parse((startedAt||'').replace(' ','T'));
  if(isNaN(t))return'';
  let s=Math.max(0,Math.floor((Date.now()-t)/1000));
  const h=Math.floor(s/3600);s-=h*3600;const m=Math.floor(s/60);s-=m*60;
  const mm=String(m).padStart(2,'0'),ss=String(s).padStart(2,'0');
  return h?`${h}:${mm}:${ss}`:`${mm}:${ss}`;
}
function fmtEta(sec){if(sec==null)return'';if(sec<90)return'1 min';
  if(sec<3600)return Math.round(sec/60)+' min';
  return Math.floor(sec/3600)+'h '+String(Math.round(sec%3600/60)).padStart(2,'0')+'m'}
function fmtM(sec){if(sec==null)return'?';if(sec<60)return Math.max(1,Math.round(sec))+'s';
  return Math.round(sec/60)+'m'}
function stageLine(st){ // done: actual · active: elapsed of expected · ahead: expected
  if(!st)return'';
  const parts=st.filter(x=>x.state==='active'||(x.secs||x.est||0)>=30).map(x=>{
    const nice=STAGE_NICE[x.stage]||x.stage;
    if(x.state==='done')return`${nice} ${fmtM(x.secs)} ✓`;
    if(x.state==='active'){
      const over=x.est&&x.secs>x.est;
      return`<b>${nice} ${fmtM(x.secs)} of ~${fmtM(x.est)}</b>${over?' (running long — still working)':''}`;
    }
    return`then ${nice} ~${fmtM(x.est)}`;
  });
  return parts.length?`<div class="sub" style="margin-top:3px">${parts.join(' · ')}</div>`:'';
}
let S=null, selected=new Set(), showHidden=false;
// collapsible transcript groups: overrides persist per sort mode; the newest
// month (or first letter) is open unless the user collapsed it
const MG={ov:JSON.parse(localStorage.getItem('stt_mgroups')||'{}'),keys:[],sort:'date'};
function mgToggle(key,open){
  MG.ov[MG.sort+':'+key]=open?1:0;
  localStorage.setItem('stt_mgroups',JSON.stringify(MG.ov));
  render();
}
function mgJump(i){
  const key=MG.keys[i];
  if(key===undefined)return;
  if(MG.ov[MG.sort+':'+key]!==1){mgToggle(key,1)}
  const el=document.getElementById('mg-'+i),c=$('#meetings');
  if(el&&c)c.scrollTop=el.offsetTop-6;
}
async function api(p,body){const r=await fetch(p,body?{method:'POST',body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return (s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// For values embedded as a JS string literal INSIDE an onclick="..." attribute (e.g. onclick="f('${escJs(x)}')").
// esc() alone breaks there: an apostrophe closes the JS string early, and HTML-entity-encoding it
// (&#39;) doesn't help — the browser HTML-decodes the attribute before compiling it as JS, so the
// entity turns back into a literal quote first. Backslash-escaping survives that decode step.
function escJs(s){return esc(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'")}

const STAGES=['downloading','converting','transcribing','diarizing','verifying','writing','summarizing'];
const STAGE_NICE={downloading:'Downloading',converting:'Preparing',transcribing:'Transcribing',diarizing:'Speakers',verifying:'Verifying',writing:'Writing',summarizing:'Summary'};

// ---- clip playback (inbox rows, queue rows) ----
// ▶ EXPANDS an inline player under the row: a seek bar, play/pause, stop, and a
// clock. A bare play button is useless on a 40-minute recording — you need to
// jump around it to know what it is.
//
// CLIPKEY (not a DOM node) is the state, because the panel re-renders every 2s
// and rebuilds these rows. The <audio> element is therefore kept OUTSIDE the
// re-rendered markup and re-attached to the row on each render — otherwise every
// poll would rip the playing element out of the document and the audio would die
// two seconds in. The server serves byte ranges, so seeking streams rather than
// re-downloading.
let clipAudio=null, clipKey=null, clipUrl='';
function _fmtClock(t){
  t=Math.max(0,Math.floor(t||0));
  const m=Math.floor(t/60),s=t%60;
  return m+':'+String(s).padStart(2,'0');
}
function _clipRow(){return document.querySelector(`[data-clip="${(clipKey||'').replace(/"/g,'')}"]`)}
function _syncClipBtns(){
  const playing=clipAudio&&!clipAudio.paused;
  document.querySelectorAll('.clipbtn').forEach(b=>{
    const mine=b.dataset.clip===clipKey;
    b.textContent=mine?'▾':'▶';
    b.classList.toggle('on',mine);
  });
  const pp=$('#clip-pp');
  if(pp)pp.textContent=playing?'❚❚':'▶';
}
function _clipTick(){
  if(!clipAudio)return;
  const bar=$('#clip-seek'), cur=$('#clip-cur'), dur=$('#clip-dur');
  if(!bar)return;
  const d=isFinite(clipAudio.duration)?clipAudio.duration:0;
  if(!bar.dataset.dragging){
    bar.max=d||0;
    bar.value=clipAudio.currentTime||0;
  }
  if(cur)cur.textContent=_fmtClock(clipAudio.currentTime);
  if(dur)dur.textContent=d?_fmtClock(d):'—:—';
}
// the player markup lives in ONE detached element, moved under whichever row is
// playing — so a re-render can never destroy the element that owns the audio
let clipBox=null;
function _clipBox(){
  if(clipBox)return clipBox;
  clipBox=document.createElement('div');
  clipBox.className='clipplayer';
  clipBox.innerHTML=`<button id="clip-pp" title="Play / pause" onclick="clipToggle()">❚❚</button>
    <span class="sub" id="clip-cur">0:00</span>
    <input type="range" id="clip-seek" min="0" max="0" step="0.1" value="0"
      title="Scrub through the recording"
      onpointerdown="this.dataset.dragging=1"
      onpointerup="this.removeAttribute('data-dragging')"
      oninput="clipSeek(this.value)">
    <span class="sub" id="clip-dur">—:—</span>
    <button id="clip-stop" title="Stop and close" onclick="stopClip()">✕</button>`;
  return clipBox;
}
function clipToggle(){
  if(!clipAudio)return;
  if(clipAudio.paused)clipAudio.play().catch(()=>{}); else clipAudio.pause();
  _syncClipBtns();
}
function clipSeek(v){if(clipAudio)clipAudio.currentTime=parseFloat(v)||0;_clipTick()}
function stopClip(){
  if(clipAudio){clipAudio.pause();clipAudio.src='';}
  clipAudio=null;clipKey=null;clipUrl='';
  if(clipBox&&clipBox.parentNode)clipBox.parentNode.removeChild(clipBox);
  _syncClipBtns();
}
// re-attach the player under its row after every render (the row is new markup)
function mountClip(){
  if(!clipKey)return;
  const btn=document.querySelector(`.clipbtn[data-clip="${CSS.escape(clipKey)}"]`);
  if(!btn){return}                       // its row scrolled out of the list
  const row=btn.closest('.row');
  const box=_clipBox();
  if(row&&box.previousSibling!==row){
    row.insertAdjacentElement('afterend',box);
  }
  _clipTick();_syncClipBtns();
}
function playClip(btn,url){
  const key=btn.dataset.clip;
  if(clipKey===key){stopClip();return}          // ▾ collapses it
  stopClip();
  document.querySelectorAll('audio').forEach(a=>a.pause());   // exclusive
  stopVoice();                                                // and vs speaker samples
  clipKey=key;clipUrl=url;
  clipAudio=new Audio(url);
  clipAudio.ontimeupdate=_clipTick;
  clipAudio.onloadedmetadata=_clipTick;
  clipAudio.onplay=clipAudio.onpause=_syncClipBtns;
  clipAudio.onended=()=>{_syncClipBtns();_clipTick()};       // keep the bar; let them replay
  clipAudio.onerror=()=>{if(clipKey===key){alert('Could not play this audio.');stopClip()}};
  mountClip();
  clipAudio.play().catch(()=>{});
}
async function delQueued(name){
  if(!confirm(`Delete “${name}”?\n\nThe audio file is removed and never becomes a `
    +`meeting. This cannot be undone.`))return;
  stopClip();
  const r=await api('/api/queue_delete',{name,confirm:true});
  if(!r.ok){alert(r.error||'failed');return}
  refresh();
}

// ---- recorder permissions ----
async function fixRecPerms(btn){
  btn.disabled=true;btn.textContent='Resetting…';
  const r=await api('/api/fix_recorder_permissions',{});
  const host=btn.parentElement;
  host.innerHTML=r.ok?('✓ '+esc(r.message)):('⚠ '+esc(r.error||'failed'));
  host.className='recstrip '+(r.ok?'good':'bad');
}

// ---- live recording clock ----
// The state poll is 2s, which made the recording timer jump in 2s steps. A 1s
// local tick advances the displayed clock between polls; each poll then
// overwrites it with the server's number (which excludes paused spans), so any
// local drift lasts at most one poll.
function drawRecClock(recg){
  const secs=recg.elapsed_secs||0;
  const h=Math.floor(secs/3600),m=Math.floor(secs%3600/60),ss=secs%60;
  const t=(h?h+':'+String(m).padStart(2,'0'):String(m))+':'+String(ss).padStart(2,'0');
  $('#rectime').textContent=(recg.paused?'Paused · ':'Elapsed ')+t;
}
setInterval(()=>{
  const recg=S&&S.recording;
  if(!recg||recg.paused)return;
  recg.elapsed_secs=(recg.elapsed_secs||0)+1;
  if($('#recbanner').style.display!=='none')drawRecClock(recg);
},1000);

// ---- inbox: a new transcript is named/dated before it joins the list ----
// The gate exists so that at 100+ transcripts a fresh one can't land in the middle
// of the pile unnoticed. Rendering is SKIPPED while a field in here has focus —
// the panel re-renders every 2s and would otherwise eat what you're typing.
function renderInbox(s,mtit){
  const inbox=s.meetings.filter(m=>m.needs_review);
  $('#inboxcard').style.display=inbox.length?'':'none';
  if(!inbox.length)return;
  $('#inboxcount').textContent='Needs naming · '+inbox.length;
  const box=$('#inbox');
  if(document.activeElement&&box.contains(document.activeElement))return;  // typing
  // rebuild ONLY when the server data actually changed — the old focus check
  // alone meant clicking anywhere outside the inbox let the next 2s poll wipe
  // everything typed into the name/date fields back to the prefill
  const sig=JSON.stringify(inbox.map(m=>[m.base,m.suggested,m.date,m.category]));
  if(box.dataset.sig===sig)return;
  box.dataset.sig=sig;
  box.innerHTML=inbox.map(m=>{
    const nm=m.suggested||mtit(m);
    const who=m.speakers.map(esc).join(', ')||'no speakers identified';
    return `<div class="row" data-ib="${esc(m.base)}">
      <div class="grow">
        <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
          <input type="text" class="ibname" value="${esc(nm)}" placeholder="Name this meeting"
                 style="width:min(320px,44vw)" onkeydown="if(event.key==='Enter')ibAccept('${escJs(m.base)}',this)">
          <input type="date" class="ibdate" value="${esc(m.date||'')}">
          <select class="ibcat">
            <option value="">no tag</option>
            <option value="work"${m.category==='work'?' selected':''}>Work</option>
            <option value="personal"${m.category==='personal'?' selected':''}>Personal</option>
          </select>
        </div>
        <div class="sub" style="margin-top:4px">${m.minutes} min · ${who}${m.summary?' · '+esc(m.summary.slice(0,90)):''}</div>
      </div>
      <button class="clipbtn" data-clip="m:${escJs(m.base)}" title="Listen — the quickest way to know what this meeting was"
        onclick="playClip(this,'/api/audio?base='+encodeURIComponent('${escJs(m.base)}'))">▶</button>
      <button class="primary" onclick="ibAccept('${escJs(m.base)}',this)">Accept</button>
    </div>`;}).join('');
}
async function ibAccept(base,el){
  const row=el.closest('[data-ib]');
  const btn=row.querySelector('button.primary');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>';
  const r=await api('/api/accept_meeting',{base,
    name:row.querySelector('.ibname').value.trim(),
    date:row.querySelector('.ibdate').value,
    category:row.querySelector('.ibcat').value});
  if(!r.ok){alert(r.error||'failed');btn.disabled=false;btn.textContent='Accept';return}
  refresh();
}
async function acceptAll(){
  const rows=[...document.querySelectorAll('#inbox [data-ib]')];
  if(!rows.length)return;
  if(!confirm('Accept all '+rows.length+' with the name and date shown?'))return;
  const fails=[];
  for(const row of rows){
    const r=await api('/api/accept_meeting',{base:row.dataset.ib,
      name:row.querySelector('.ibname').value.trim(),
      date:row.querySelector('.ibdate').value,
      category:row.querySelector('.ibcat').value});
    if(!r.ok)fails.push((row.dataset.ib)+' — '+(r.error||'failed'));
  }
  // per-item failures must not vanish silently: the rows that failed stay in
  // the inbox (their typed values survive — the DOM is only rebuilt when the
  // server data changes) and the reason is shown once
  if(fails.length)alert(fails.length+' could not be accepted:\n\n'+fails.slice(0,8).join('\n'));
  refresh();
}

// ---- multi-select + bulk actions ----
let SEL=new Set();
function selTog(b,on){on?SEL.add(b):SEL.delete(b);render()}
function selClear(){SEL.clear();render()}
function selAllShown(list){
  const all=list.every(m=>SEL.has(m.base));
  list.forEach(m=>all?SEL.delete(m.base):SEL.add(m.base));
  render();
}
function renderSelbar(shown){
  const bar=$('#selbar');
  bar.style.display=SEL.size?'flex':'none';
  if(!SEL.size)return;
  bar.innerHTML=`<b>${SEL.size} selected</b>
    <button onclick="bulk('category','work')">Work</button>
    <button onclick="bulk('category','personal')">Personal</button>
    <button onclick="bulk('category','')">Clear tag</button>
    <button onclick="bulkRename()" title="One name for all of them — each still keeps its own date, so they stay separate meetings">Rename…</button>
    <button onclick="bulkDate()">Set date…</button>
    <button onclick="bulk('archive')">Archive</button>
    <button onclick="bulkDropAudio()" title="Keep the transcript, delete the audio file">Delete audio…</button>
    <button style="color:var(--bad)" onclick="bulkDelete()">Delete…</button>
    <span class="grow"></span>
    <button onclick="selAllShown(LASTSHOWN)">Select all shown</button>
    <button onclick="selClear()">Cancel</button>`;
}
let LASTSHOWN=[];
async function bulk(action,value,extra){
  const bases=[...SEL];
  if(!bases.length)return;
  const r=await api('/api/bulk',{bases,action,value,...(extra||{})});
  const fails=(r.results||[]).filter(x=>!x.ok);
  if(fails.length)alert(`${fails.length} of ${bases.length} could not be done:\n\n`
    +fails.slice(0,8).map(f=>'• '+f.base+' — '+(f.error||'failed')).join('\n'));
  else if(r.freed_mb)alert(`Done. Freed ${r.freed_mb} MB of audio.`);
  SEL.clear();refresh();
}
function bulkRename(){
  const n=prompt(`One name for all ${SEL.size} selected.\n\nEach keeps its own date in the filename, so recurring meetings stay separate folders.`);
  if(n&&n.trim())bulk('rename',n.trim());
}
function bulkDate(){
  const d=prompt(`Set the date for all ${SEL.size} selected (YYYY-MM-DD).\n\nThe folder name is re-stamped to match.`);
  if(d&&d.trim())bulk('date',d.trim());
}
function bulkDropAudio(){
  if(!confirm(`Delete the stored AUDIO for ${SEL.size} meeting(s)? The transcripts are kept.\n\n`
    +`This frees most of the space, but it cannot be undone:\n`
    +`• Reprocess (Redo) becomes impossible — it re-transcribes from that file\n`
    +`• ▶ voice-sample playback stops for speakers first heard in these meetings\n\n`
    +`Speaker identification itself is unaffected.`))return;
  bulk('drop_audio',null,{confirm:true});
}
function bulkDelete(){
  if(!confirm(`Permanently delete ${SEL.size} meeting(s) — transcript, audio, and caches?\n\n`
    +`This cannot be undone. Archive instead if you might want them back.`))return;
  bulk('delete',null,{confirm:true});
}

// ---- Work/Personal tag, one click on the row (no menu) ----
// Cycles untagged -> Work -> Personal -> untagged. The update is applied to the
// local state and re-rendered BEFORE the request lands, so the chip flips
// instantly instead of waiting for the next 2s poll.
const CAT_NEXT={'':'work',work:'personal',personal:''};
const CAT_LABEL={work:'Work',personal:'Personal'};
function catChip(m){
  const c=m.category||'';
  const nxt=CAT_NEXT[c];
  const lbl=CAT_LABEL[c]||'+ tag';
  const t=c?`Tagged ${CAT_LABEL[c]} — click for ${nxt?CAT_LABEL[nxt]:'no tag'}`
          :'Tag as Work — click again for Personal, once more to clear';
  const call=`cycleCat(event,'${escJs(m.base)}','${nxt}')`;
  return `<span class="chip cat ${c||'none'}" role="button" tabindex="0" title="${t}"
    onclick="${call}" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();${call}}">${lbl}</span>`;
}
async function cycleCat(ev,base,next){
  ev.stopPropagation();  // the row itself is clickable — don't open anything
  const m=S.meetings.find(x=>x.base===base);
  if(m){m.category=next||null;render()}
  const r=await api('/api/set_category',{base,category:next});
  if(!r.ok)alert(r.error||'failed');
  refresh();
}
function render(){
  const s=S;
  // header
  $('#statusdot').className='dot '+(s.running?'run':(s.paused?'paused':''));
  $('#statustext').textContent=s.running?'processing':(s.paused?'automatic runs paused':'idle');
  $('#battery').textContent=s.battery;
  const gb=s.mem_mb>=1024?(s.mem_mb/1024).toFixed(1)+' GB':(s.mem_mb+' MB');
  $('#mem').style.display=s.mem_mb>0?'inline':'none';
  $('#mem').textContent='mem '+gb;
  // active
  const act=Object.entries(s.active||{});
  const orphaned=s.running&&!act.length&&s.mem_mb>500;
  // recording banner (independent of processing — a call can record while a
  // batch runs, or with nothing else happening)
  const recg=s.recording;
  $('#recbanner').style.display=recg?'block':'none';
  if(recg){
    // elapsed_secs comes from the server (recorder.elapsed_seconds), so it already
    // excludes paused spans — the panel and the menu bar read the same number
    drawRecClock(recg);
    $('#recbanner').classList.toggle('paused',!!recg.paused);
    $('#recstall').style.display=recg.stalled?'block':'none';
  }
  // the last recording's outcome — saved, recovered, or captured nothing. This
  // (plus the menu-bar line) replaced notification banners, which either failed
  // to display from this unbundled app or arrived as unwanted osascript alerts.
  const rn=s.recorder_note;
  const rnEl=$('#recnote');
  rnEl.style.display=rn?'block':'none';
  if(rn&&rnEl.dataset.at!==rn.at){
    rnEl.dataset.at=rn.at;
    rnEl.className='recstrip '+(rn.ok?'good':'bad');
    rnEl.innerHTML=`${rn.ok?'✓':'⚠'} ${esc(rn.text)}
      ${!rn.ok&&/[Mm]icrophone|audio/.test(rn.text)?'<button onclick="fixRecPerms(this)">Fix permissions</button>':''}
      <button class="dismiss" title="Dismiss" onclick="api('/api/recorder_note',{clear:true}).then(refresh)">✕</button>`;
    // a SUCCESS is just an acknowledgement — it clears itself. A FAILURE needs a
    // decision (grant permissions, look at the log), so it stays until dismissed.
    clearTimeout(rnEl._t);
    if(rn.ok)rnEl._t=setTimeout(()=>{api('/api/recorder_note',{clear:true}).then(refresh)},8000);
  }
  $('#activecard').style.display=s.running?'block':'none';
  $('#active').innerHTML=(orphaned?`<div class="row"><span class="chip warn">recovering</span>
    <div class="grow"><div class="name">Background workers are still running</div>
    <div class="sub">They hold ${gb} of memory — “Stop processing” shuts down the whole group and verifies it.</div></div></div>`:'')
  +(act.length?act.map(([n,a])=>{
    const idx=STAGES.indexOf(a.stage);
    const pct=a.pct!=null?a.pct:null;
    return `<div class="row"><div class="grow"><div class="name">${esc(n.replace(/\.[^.]+$/,''))}</div>
    <div class="stagechips">${STAGES.filter(st=>(st!=='verifying'||a.stage==='verifying')&&(st!=='summarizing'||a.stage==='summarizing')).map((st,i)=>`<span class="s ${i<=idx?'on':''}">${STAGE_NICE[st]}</span>`).join('')}</div>
    ${pct!=null?`<div class="bar"><i style="width:${pct}%"></i></div>`:''}
    ${stageLine(a.stages)}
    </div><div style="text-align:right;min-width:86px">${pct!=null?`<div class="name">${pct}%</div><div class="sub">≈ ${fmtEta(a.eta_sec)} left</div>`:'<span class="spin"></span>'}</div></div>`;
  }).join(''):(orphaned?'':'<div class="sub">Starting…</div>'))
  +(s.overall_eta_sec?`<div class="sub" style="padding-top:10px">Everything queued: ≈ ${fmtEta(s.overall_eta_sec)} remaining</div>`:'');
  // queued panel runs (redos / hand-picked) — waiting for the current run to finish
  const qjobs=(s.queued_jobs||[]).map(j=>
    `<div class="row"><span style="width:17px"></span><div class="grow"><div class="name">↻ ${esc(j.label)}</div><div class="sub">requested run${j.strict?' · strict':''}${j.verify?' · verify':''}</div></div><span class="chip live">${s.running?'starts after current run':'starting…'}</span><button class="segbtn" title="Cancel this queued run" onclick="api('/api/unqueue',{at:${j.at}}).then(r=>{if(!r.ok)alert('Could not cancel — it may have already started.');refresh()})">✕</button></div>`).join('');
  // queue
  const newFiles=s.queue.filter(f=>!f.processed);
  $('#qcount').textContent=newFiles.length+' new';
  $('#queue').innerHTML=qjobs+(s.queue.length?s.queue.map(f=>{
    const running=s.active&&s.active[f.name];
    const pend=(s.pending||[]).includes(f.name);
    const chip=running?'<span class="chip live">processing</span>':pend?'<span class="chip live">queued</span>':f.processed?'<span class="chip done">done</span>':f.held?'<span class="chip warn">on hold</span>':(f.video?'<span class="chip">video</span>':'');
    const box=(!f.processed&&!running&&!pend)?`<input type="checkbox" class="checkbox" ${selected.has(f.name)?'checked':''} onchange="tog('${escJs(f.name)}',this.checked)">`:'<span style="width:17px"></span>';
    const est=(!f.processed&&f.est_min&&!f.held)?` · <span ${f.est_detail?`title="${esc(f.est_detail)}"`:''}>~${f.est_min} min to process</span>`
      :(f.held?' · automatic runs skip this until you release it':'');
    const hold=(!f.processed&&!running&&!pend)?`<button class="segbtn${f.held?' on':''}"
      title="${f.held?'Release — automatic runs can process it again':'Hold — keep it in the queue and let automatic runs skip it'}"
      onclick="api('/api/queue_hold',{name:'${escJs(f.name)}'}).then(r=>{if(!r.ok)alert(r.error||'failed');refresh()})">${f.held?'▶':'❚❚'}</button>`:'';
    // listen before it costs you minutes of transcription, and bin a bad take
    // without going to Finder. Not offered while the file is being processed.
    const play=f.video?'':`<button class="clipbtn" data-clip="q:${escJs(f.name)}" title="Listen to this recording"
      onclick="playClip(this,'/api/queue_audio?name='+encodeURIComponent('${escJs(f.name)}'))">▶</button>`;
    const del=(!running&&!pend)?`<button class="segbtn" title="Delete this file — it never becomes a meeting"
      onclick="delQueued('${escJs(f.name)}')">✕</button>`:'';
    return `<div class="row">${box}<div class="grow"><div class="name">${esc(f.name)}</div><div class="sub">${f.size_mb} MB${est}</div></div>${chip}${play}${hold}${del}</div>`;
  }).join(''):(qjobs?'':'<div class="sub">Nothing waiting.</div>'));
  $('#runsel').disabled=!selected.size||s.running;
  $('#runall').disabled=s.running||!newFiles.length;
  $('#runother').disabled=false;  // extra picks queue behind the current run now
  const selectable=newFiles.filter(f=>!(s.active&&s.active[f.name])&&!(s.pending||[]).includes(f.name));
  $('#selall').style.display=selectable.length>1?'inline':'none';
  $('#selall').textContent=selected.size>=selectable.length&&selectable.length?'Deselect all':'Select all';
  // pause button
  $('#pausebtn').textContent=s.paused?'Resume automatic runs':'Pause automatic runs';
  $('#pausebtn').title=s.paused
    ?'Automatic runs are paused: the folder watch and the nightly run both still fire, but each run quits without processing. Manual runs still work.'
    :'Master switch: stops the folder watch, the nightly run, and login catch-up from processing anything. Manual runs still work.';
  $('#pausebtn').onclick=()=>api(s.paused?'/api/resume':'/api/pause',{}).then(refresh);
  // recent results (successes and failures)
  const rec=s.recent||[];
  $('#recentwrap').style.display=rec.length?'block':'none';
  $('#recent').innerHTML=rec.slice(0,5).map(r=>`<div class="row">
    <span class="chip ${r.ok?'done':'warn'}">${r.ok?'✓':'failed'}</span>
    <div class="grow"><div class="name">${esc(r.name.replace(/\.[^.]+$/,''))}</div>
    <div class="sub">${esc(r.summary||'')}${r.ok?'':' · original kept in iCloud — will retry next run'}</div></div>
    <span class="sub">${(r.at||'').slice(5,16).replace('T',' ')}</span></div>`).join('');
  // punctuation toggle
  $('#punctbtn').textContent=s.punctuate?'On':'Off';
  $('#punctbtn').style.color=s.punctuate?'var(--ok)':'var(--sub)';
  // recorder: mic speaker (channel-aware separation)
  if($('#micbtn')){
    $('#micbtn').textContent=s.mic_speaker?'Change…':'Set…';
    $('#micnote').textContent=s.mic_speaker
      ?`Recorded calls separate ${s.mic_speaker} (you) from the others. Enroll ${s.mic_speaker} as a speaker for this to take effect.`
      :'Your name on recorded calls, so the recorder separates you from the others. You must be enrolled as a speaker too.';
  }
  // learned speed rates
  $('#ratesnote').textContent=s.rates&&s.rates.runs
    ?`Measured from ${s.rates.runs} run${s.rates.runs>1?'s':''}: ${s.rates.text} realtime — estimates improve automatically`
    :'Estimates use factory measurements until a few runs complete';
  // speakers (▶ = one-tap voice playback, found automatically in recent meetings)
  const sq=($('#spkfilter').value||'').trim().toLowerCase();
  const unkOnly=$('#spkunk').checked;
  const smatch=t=>!sq||(t||'').toLowerCase().includes(sq);
  const enr=unkOnly?[]:s.enrolled.filter(e=>smatch(e.name));
  $('#enrolled').innerHTML=enr.map(e=>`<div class="row">
    <button class="playbtn" data-key="${esc(e.name)}" onclick="playVoice(this)">▶</button>
    <div class="grow"><div class="name">${esc(e.name)}</div>
    <div class="sub" title="${esc((e.sources||[]).join(', '))}">${e.samples} voice sample${e.samples>1?'s':''}${e.sources&&e.sources.length?' · from '+esc(e.sources[e.sources.length-1])+(e.sources.length>1?' +'+(e.sources.length-1):''):''}</div></div>
    <button onclick="openSpeakerActions('name:${escJs(e.name)}','${escJs(e.name)}','')">⋯</button></div>`).join('')
    ||(unkOnly?'':`<div class="sub">${sq?'No enrolled speaker matches “'+esc(sq)+'”.':'No one enrolled yet.'}</div>`);
  // the unknowns search also matches the meetings a voice was heard in, so
  // "who was that in Tuesday's sync?" is findable by the meeting's name
  const vis=s.unknowns.filter(u=>!u.archived&&(smatch(u.display)||u.meetings.some(smatch))),
        hid=s.unknowns.filter(u=>u.archived&&(smatch(u.display)||u.meetings.some(smatch)));
  $('#unknowns').innerHTML=vis.map(u=>`<div class="row">
    <button class="playbtn" data-key="${u.uid}" data-meeting="${esc(u.meetings[0]||'')}" onclick="playVoice(this)">▶</button>
    <div class="grow"><div class="name">${esc(u.display)}</div>
    <div class="sub">heard in ${u.meetings.length} meeting${u.meetings.length>1?'s':''}</div></div>
    <button class="primary" onclick="openName('${escJs(u.uid)}','${escJs(u.display)}','${escJs(u.meetings[0]||'')}')">Who is this?</button>
    <button onclick="openSpeakerActions('uid:${escJs(u.uid)}','${escJs(u.display)}','${escJs(u.meetings[0]||'')}')">⋯</button></div>`).join('')
    +(hid.length?`<div class="sub" style="padding:8px 0 2px"><button class="link" onclick="showHidden=!showHidden;render()">${showHidden?'▾':'▸'} ${hid.length} hidden</button></div>`
      +(showHidden?hid.map(u=>`<div class="row" style="opacity:.6">
        <button class="playbtn" data-key="${u.uid}" data-meeting="${esc(u.meetings[0]||'')}" onclick="playVoice(this)">▶</button>
        <div class="grow"><div class="name">${esc(u.display)}</div>
        <div class="sub">hidden · heard in ${u.meetings.length} meeting${u.meetings.length>1?'s':''}</div></div>
        <button onclick="api('/api/hide_unknown',{uid:'${escJs(u.uid)}',hide:false}).then(refresh)">Restore</button></div>`).join(''):''):'')
    ||`<div class="sub" style="padding-top:8px">${sq?'No unidentified voice matches “'+esc(sq)+'”.':'No unidentified voices right now.'}</div>`;
  $('#relnote').style.display=s.relabel_pending?'block':'none';
  $('#relnote').textContent='Applying names to all transcripts… (moments)';
  // settings — the two automatic TRIGGERS. Pause is not a third trigger: it is a
  // master gate OVER them (run_batch exits on startup while paused.flag exists,
  // whatever fired it). So a healthy-looking "Folder watch: On" is inert while
  // paused — the trigger still fires, the run just quits. Say so, loudly, rather
  // than leave a switch that looks live and does nothing.
  const sc=s.schedule;
  $('#pausebanner').style.display=s.paused?'flex':'none';
  $('#watchbtn').textContent=sc.watch?'On':'Off';
  $('#watchbtn').style.color=s.paused?'var(--sub)':(sc.watch?'var(--ok)':'var(--sub)');
  $('#watchbtn').disabled=!sc.installed;
  $('#watchnote').textContent=!sc.installed
    ?'Not installed — run ./setup.sh install-agent'
    :(s.paused?(sc.watch?'On, but PAUSED — a new file wakes the agent and it exits without processing'
                        :'Off (and automatic runs are paused anyway)')
              :(sc.watch?'New recordings process within moments of landing (while the Mac is awake)'
                        :'Off — new files wait for the nightly run or a manual click'));
  $('#nightbtn').textContent=sc.nightly?'On':'Off';
  $('#nightbtn').style.color=s.paused?'var(--sub)':(sc.nightly?'var(--ok)':'var(--sub)');
  $('#nightbtn').disabled=!sc.installed;
  $('#schedchg').style.display=sc.nightly?'inline-block':'none';
  $('#schedtext').textContent=!sc.installed?'Not installed'
    :(sc.nightly
      ?new Date(2000,0,1,sc.hour,sc.minute).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'})+' · runs at next wake if the Mac is asleep'
      :'Off — turn on to process everything new at a set time');
  const sel=$('#modelsel');
  const pickable=s.asr_choices.filter(c=>!c.cloud||(s.cloud_keys||{})[c.cloud]);
  sel.innerHTML=pickable.map(c=>`<option value="${c.id}" ${c.id===s.model?'selected':''}>${c.label}</option>`).join('');
  $('#modelnote').textContent=(s.asr_choices.find(c=>c.id===s.model)||{}).note||'';
  const nk=['scribe','openai','voxtral'].filter(p=>(s.cloud_keys||{})[p]).length;
  $('#cloudnote').textContent=nk?`${nk} provider key${nk>1?'s':''} set — cloud engines appear in the model picker`:'Optional: bring your own API key (ElevenLabs · OpenAI · Mistral)';
  // assistant backend picker (summaries & Ask)
  const LB={local:'Local Qwen3-8B',anthropic:'Claude Haiku · cloud',openai:'OpenAI GPT · cloud'};
  const av=s.llm_backends||{};
  $('#llmsel').innerHTML=Object.keys(LB).map(b=>
    `<option value="${b}" ${b===s.llm_backend?'selected':''} ${av[b]?'':'disabled'}>${LB[b]}${av[b]?'':' (no key)'}</option>`).join('');
  $('#llmnote').textContent=s.llm_backend==='local'
    ?(av.local?'Runs on this Mac — transcripts never leave it':'Local model not installed — pick a cloud assistant or install .venv-llm')
    :'Cloud assistant: transcript text uploads for summaries and Ask. Strict recordings always stay local.';
  $('#srcpath').textContent=s.paths.source.replace(/^\/Users\/[^/]+/,'~');
  $('#dstpath').textContent=s.paths.dest.replace(/^\/Users\/[^/]+/,'~');
  // meetings: filter by title/speaker, newest meeting-date first, in
  // collapsible groups (month, or first letter when sorted by name)
  const mq=($('#mfilter').value||'').toLowerCase();
  const msort=($('#msort')&&$('#msort').value)||'date';
  const mcat=($('#mcat')&&$('#mcat').value)||'';
  const ab=$('#archbtn');
  if(ab){ab.style.display=s.archived_count?'':'none';ab.textContent='Archived · '+(s.archived_count||0)}
  const mtit=m=>m.title||m.base;  // clean display name (date stripped)
  renderInbox(s,mtit);
  // unreviewed meetings live in the inbox above, NOT in this list — that is the
  // whole point of the gate: a new transcript cannot slip into the pile unseen
  const shown=s.meetings.filter(m=>!m.needs_review
    &&(!mcat||m.category===mcat)
    &&(!mq||mtit(m).toLowerCase().includes(mq)
    ||m.base.toLowerCase().includes(mq)
    ||m.speakers.join(' ').toLowerCase().includes(mq)))
    .slice().sort(msort==='name'
      ?(a,b)=>mtit(a).toLowerCase().localeCompare(mtit(b).toLowerCase())
        ||(b.date||'').localeCompare(a.date||'')  // recurring names: newest first
      :(a,b)=>(b.date||'').localeCompare(a.date||''));
  if(document.querySelector('#meetings .inline-edit'))return;  // typing in place — don't wipe it
  const groups=[];
  for(const m of shown){
    const key=msort==='name'
      ?(/^[a-z]/i.test(mtit(m))?mtit(m)[0].toUpperCase():'#')
      :(m.date?new Date(m.date+'T12:00:00').toLocaleDateString([],{month:'long',year:'numeric'}):'Undated');
    if(!groups.length||groups[groups.length-1].key!==key)groups.push({key,rows:[]});
    groups[groups.length-1].rows.push(m);
  }
  MG.keys=groups.map(g=>g.key);MG.sort=msort;
  const mgOpen=(k,i)=>{
    if(mq)return true;  // searching: every group with matches shows its rows
    const ov=MG.ov[msort+':'+k];
    return ov!==undefined?!!ov:i===0;  // newest month / first letter open by default
  };
  // prune selections whose base no longer exists — a date/title edit re-stamps
  // the folder name, which would otherwise leave a phantom entry inflating the
  // toolbar count and silently failing inside every later bulk action
  {const live=new Set(s.meetings.map(m=>m.base));
   [...SEL].forEach(b=>{if(!live.has(b))SEL.delete(b)})}
  LASTSHOWN=shown;
  renderSelbar(shown);
  const mrow=m=>{
    const day=m.date?new Date(m.date+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'}):'';
    return `<div class="row"><input type="checkbox" class="checkbox mchk" ${SEL.has(m.base)?'checked':''}
      title="Select for bulk actions" onclick="event.stopPropagation();selTog('${escJs(m.base)}',this.checked)">
    <div class="grow${m.summary?' hastip':''}" data-base="${esc(m.base)}">
    <div class="name"><span class="mtitle" onclick="inlineRename('${escJs(m.base)}','${escJs(mtit(m))}',event)" title="Click to rename">${esc(mtit(m))}</span></div>
    <div class="sub">${day?`<span class="mdate" onclick="inlineDate('${escJs(m.base)}','${esc(m.date)}',event)" title="Click to change the meeting date">${day}</span> · `:''}${m.minutes} min · ${m.speakers.map(esc).join(', ')}${m.strict?' · strict':''} ${catChip(m)}
      ${m.flagged?` <span class="chip warn" style="cursor:pointer" onclick="openReview('${escJs(m.base)}')" title="Step through each uncertain segment with its audio — accept or fix it">⚠ ${m.flagged} to review</span>`:(m.flagged_minor?` <span class="chip" style="cursor:pointer" onclick="openReview('${escJs(m.base)}')" title="Only sub-second crosstalk crumbs — bulk-accept or skim them">${m.flagged_minor} minor</span>`:'')}</div>
    ${m.summary?`<div class="sub" style="margin-top:3px;font-style:italic">${esc(m.summary.length>150?m.summary.slice(0,150)+'…':m.summary)}</div>`:''}</div>
    ${m.audio?`<button class="clipbtn" data-clip="t:${escJs(m.base)}" title="Listen without opening it"
      onclick="playClip(this,'/api/audio?base='+encodeURIComponent('${escJs(m.base)}'))">▶</button>`:''}
    <button class="primary" onclick="openTranscript('${escJs(m.base)}')">Read</button>
    <button onclick="openSummary('${escJs(m.base)}')">Summary</button>
    <button onclick="openAsk('${escJs(m.base)}')" ${S.llm_available?'':'disabled'} title="${S.llm_available?'Ask questions about this meeting, answered on this Mac':'Needs the local model (.venv-llm) installed'}">Ask</button>
    <button onclick="openMeetingMenu('${escJs(m.base)}')" title="Export, rename, reprocess…">⋯</button>
  </div>`};
  $('#meetings').innerHTML=groups.map((g,i)=>{
    const openG=mgOpen(g.key,i);
    return `<div class="mgroup mghdr" id="mg-${i}" onclick="mgToggle('${escJs(g.key)}',${openG?0:1})" title="${openG?'Collapse':'Expand'} ${esc(g.key)}"><span style="display:inline-block;width:15px">${openG?'▾':'▸'}</span>${esc(g.key)} · ${g.rows.length}</div>`
      +(openG?g.rows.map(mrow).join(''):'');
  }).join('')||`<div class="sub">${mq?'No transcript titles match “'+esc(mq)+'”.':'No transcripts yet — process something above.'}</div>`;
  // jump rail: one entry per group, year markers between months
  const rail=$('#mrail');
  if(groups.length>=3&&!mq){
    let lastYr='';
    rail.innerHTML=groups.map((g,i)=>{
      let h='';
      if(msort==='date'){
        const yr=(g.key.match(/\d{4}/)||[''])[0];
        if(yr&&yr!==lastYr){h=`<div class="yr">${yr}</div>`;lastYr=yr;}
        return h+`<button onclick="mgJump(${i})" title="Jump to ${esc(g.key)} (${g.rows.length})">${g.key==='Undated'?'—':esc(g.key.slice(0,3))}</button>`;
      }
      return `<button onclick="mgJump(${i})" title="Jump to ${esc(g.key)} (${g.rows.length})">${esc(g.key)}</button>`;
    }).join('');
    rail.style.display='flex';
  }else rail.style.display='none';
  _syncVoiceBtns();  // re-render rebuilt the ▶ buttons; restore ◼ on the playing one
  mountClip();       // and re-attach the inline player under its (rebuilt) row
}
// Voice-sample playback is tracked by speaker KEY (not DOM node), because the
// panel re-renders every 2s and rebuilds the buttons — the stop (◼) state must
// survive that, so a click always stops the sample that's playing.
let voiceAudio=null, voiceKey=null;
function _syncVoiceBtns(){
  const playing=voiceAudio&&!voiceAudio.paused;
  document.querySelectorAll('.playbtn').forEach(b=>{
    b.textContent=(playing&&b.dataset.key===voiceKey)?'◼':'▶';
    b.title=(playing&&b.dataset.key===voiceKey)?'Stop':'Play a short sample of this voice';
  });
}
function stopVoice(){
  if(voiceAudio)voiceAudio.pause();
  voiceAudio=null;voiceKey=null;_syncVoiceBtns();
}
function playVoice(btn){
  const key=btn.dataset.key;
  if(voiceKey===key&&voiceAudio&&!voiceAudio.paused){stopVoice();return}  // toggle off
  if(voiceAudio)voiceAudio.pause();
  document.querySelectorAll('audio').forEach(a=>a.pause());  // exclusive playback
  const mtg=btn.dataset.meeting||'';
  voiceKey=key;btn.textContent='…';
  voiceAudio=new Audio('/api/snippet?speaker='+encodeURIComponent(key)+(mtg?'&meeting='+encodeURIComponent(mtg):''));
  voiceAudio.onplaying=_syncVoiceBtns;
  voiceAudio.onended=voiceAudio.onerror=()=>{if(voiceKey===key)stopVoice()};
  voiceAudio.play().catch(()=>{if(voiceKey===key)stopVoice()});
}
function tog(n,on){on?selected.add(n):selected.delete(n);$('#runsel').disabled=!selected.size}
function selAll(){
  const sel=S.queue.filter(f=>!f.processed&&!(S.active&&S.active[f.name])&&!(S.pending||[]).includes(f.name)).map(f=>f.name);
  if(selected.size>=sel.length){selected.clear()}else{sel.forEach(n=>selected.add(n))}
  render();
}
function runOpts(){return {parallel:$('#par2').checked?2:1,strict:$('#strict').checked,verify:$('#verify').checked,onetime:$('#onetime').checked}}
function runSelected(){api('/api/run',{files:[...selected],...runOpts()}).then(()=>{selected.clear();refresh()})}
function runAll(){api('/api/run',runOpts()).then(refresh)}
async function pickFiles(){
  const r=await api('/api/pick_files',{});
  if(r.cancelled||!r.paths?.length)return;
  await api('/api/run',{paths:r.paths,...runOpts()});
  refresh();
}
function togglePunct(){api('/api/punctuate',{on:!S.punctuate}).then(refresh)}
function setMicSpeaker(){
  const cur=S.mic_speaker||'';
  const n=prompt('Your name as it should appear on recorded meetings (exactly as you are enrolled in Speakers). Leave blank to turn this off.',cur);
  if(n===null)return;
  api('/api/mic_speaker',{name:n.trim()}).then(refresh);
}

// ---- full-text search across all transcripts ----
let searchTimer=null;
function scheduleSearch(){
  clearTimeout(searchTimer);
  const q=$('#mfilter').value.trim();
  if(q.length<3){$('#searchhits').innerHTML='';return}
  searchTimer=setTimeout(async()=>{
    const r=await api('/api/search?q='+encodeURIComponent(q));
    if($('#mfilter').value.trim().toLowerCase()!==r.query)return; // stale response
    const rx=new RegExp(r.query.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
    const hl=s=>esc(s).replace(rx,m=>'<mark>'+m+'</mark>');
    $('#searchhits').innerHTML=r.hits.length?`
      <div class="mgroup">Said in transcripts — ${r.total} match${r.total>1?'es':''}</div>
      <div class="inset" style="max-height:30vh;margin-bottom:10px">
      ${r.hits.map(h=>{const mm=Math.floor(h.start/60),ss=String(Math.floor(h.start%60)).padStart(2,'0');
        return `<div class="tseg" onclick="openTranscript('${escJs(h.base)}',${h.index})" title="Open the transcript at this moment">
        <span class="t">${mm}:${ss}</span><span class="w">${esc(h.who)}</span>
        <span class="x">${hl(h.snippet)}<span class="sub"> — ${esc(h.base)}</span></span></div>`}).join('')}
      </div>`:`<div class="sub" style="padding:8px 0">Nothing in any transcript matches “${esc(r.query)}”.</div>`;
  },250);
}

// ---- per-meeting menu: export / copy / reveal / rename / reprocess ----
function openMeetingMenu(base){
  const m=S.meetings.find(x=>x.base===base)||{};
  const title=m.title||base;
  $('#dlg').classList.remove('wide');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(title)}</h1>
  <div class="row" style="margin-top:8px"><div class="grow"><div class="name">Export as Word</div>
    <div class="sub">Styled .docx → Downloads</div></div>
    <button onclick="doExport('${escJs(base)}','docx',this)">Export</button></div>
  <div class="row"><div class="grow"><div class="name">Export as PDF</div>
    <div class="sub">Print-ready → Downloads</div></div>
    <button onclick="doExport('${escJs(base)}','pdf',this)">Export</button></div>
  <div class="row"><div class="grow"><div class="name">Copy transcript</div>
    <div class="sub">Plain text to the clipboard</div></div>
    <button onclick="copyTxt('${escJs(base)}',this)">Copy</button></div>
  <div class="row"><div class="grow"><div class="name">Show files</div>
    <div class="sub">Reveal the .txt / .json in Finder</div></div>
    <button onclick="api('/api/export',{base:'${escJs(base)}',fmt:'reveal'})">Reveal</button></div>
  <div class="row"><div class="grow"><div class="name">Rename</div>
    <div class="sub">Retitle this recording (updates all files)</div></div>
    <button onclick="dlg.close();openRename('${escJs(base)}')">Rename…</button></div>
  <div class="row"><div class="grow"><div class="name">Category</div>
    <div class="sub">Also one click on the row itself. The transcripts list filters by it.</div></div>
    <button ${m.category==='work'?'class="primary"':''} onclick="setCategory('${escJs(base)}','${m.category==='work'?'':'work'}')">Work</button>
    <button ${m.category==='personal'?'class="primary"':''} onclick="setCategory('${escJs(base)}','${m.category==='personal'?'':'personal'}')">Personal</button></div>
  ${m.audio?`<div class="row"><div class="grow"><div class="name">Reprocess</div>
    <div class="sub">Re-run transcription + speakers from the stored audio</div></div>
    <button onclick="dlg.close();openRedo('${escJs(base)}','${escJs(m.audio)}')">Redo…</button></div>`:''}
  <div class="row"><div class="grow"><div class="name">Archive</div>
    <div class="sub">Set aside, out of the main view — restorable anytime</div></div>
    <button onclick="doArchive('${escJs(base)}')">Archive</button></div>
  <div class="row" style="border:0"><div class="grow"><div class="name" style="color:var(--bad,#e5484d)">Delete</div>
    <div class="sub">Remove the transcript, audio copy, and caches permanently</div></div>
    <button style="color:var(--bad,#e5484d)" onclick="dlg.close();openDelete('${escJs(base)}','${escJs(title)}',0)">Delete…</button></div>
  <div style="display:flex;justify-content:flex-end;margin-top:12px"><button onclick="dlg.close()">Close</button></div>`;
  dlg.showModal();
}
async function setCategory(base,cat){
  const r=await api('/api/set_category',{base,category:cat});
  if(!r.ok){alert(r.error||'failed');return}
  dlg.close();refresh();
}
async function doArchive(base){
  const r=await api('/api/archive_meeting',{base});
  if(!r.ok){alert(r.error||'failed');return}
  dlg.close();refresh();
}
function openDelete(base,title,fromArchive){
  $('#dlg').classList.remove('wide');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Delete “${esc(title)}”?</h1>
  <p class="sub" style="margin-top:8px">This permanently removes the transcript, the stored audio, and every cache. It cannot be undone.</p>
  ${fromArchive?'':'<p class="sub">If you might want it back, <b>Archive</b> instead — it moves the meeting out of the main view but keeps everything restorable.</p>'}
  <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">
    <button onclick="${fromArchive?'openArchived()':'dlg.close()'}">Cancel</button>
    ${fromArchive?'':`<button onclick="doArchive('${escJs(base)}')">Archive instead</button>`}
    <button style="color:#fff;background:var(--bad,#e5484d);border-color:var(--bad,#e5484d)" onclick="doDelete('${escJs(base)}',${fromArchive?1:0})">Delete forever</button></div>`;
  dlg.showModal();
}
async function doDelete(base,fromArchive){
  const r=await api('/api/delete_meeting',{base,confirm:true});
  if(!r.ok){alert(r.error||'failed');return}
  if(r.note)alert('Deleted. One note: '+r.note);
  if(fromArchive){openArchived()}else{dlg.close()}
  refresh();
}
async function openArchived(){
  const d=await api('/api/archived');
  const items=d.items||[];
  $('#dlg').classList.remove('wide');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Archived meetings</h1>
  <p class="sub" style="margin-top:4px">Set aside — out of the list, search, and Ask. Restore brings one back exactly as it was.</p>
  <div class="inset" style="max-height:50vh;overflow:auto;margin-top:8px">`+
  (items.map(it=>{
    const day=it.date?new Date(it.date+'T12:00:00').toLocaleDateString([],{year:'numeric',month:'short',day:'numeric'}):'';
    return `<div class="row"><div class="grow"><div class="name">${esc(it.title||it.base)}</div>
      <div class="sub">${day}${it.minutes?` · ${it.minutes} min`:''}${it.category?` · ${it.category==='work'?'Work':'Personal'}`:''}</div></div>
      <button onclick="doRestore('${escJs(it.base)}')">Restore</button>
      <button style="color:var(--bad,#e5484d)" onclick="openDelete('${escJs(it.base)}','${escJs(it.title||it.base)}',1)">Delete…</button></div>`;
  }).join('')||'<div class="sub" style="padding:8px">Nothing archived.</div>')+
  `</div><div style="display:flex;justify-content:flex-end;margin-top:12px"><button onclick="dlg.close()">Close</button></div>`;
  dlg.showModal();
}
async function doRestore(base){
  const r=await api('/api/restore_meeting',{base});
  if(!r.ok){alert(r.error||'failed');return}
  openArchived();refresh();
}
async function doExport(base,fmt,btn){
  btn.disabled=true;btn.innerHTML='<span class="spin"></span>';
  const r=await api('/api/export',{base,fmt});
  btn.disabled=false;btn.textContent=r.ok?'Done ✓':'Failed';
  if(r.error)alert(r.error);
}
async function copyTxt(base,btn){
  const txt=await fetch('/api/txt?base='+encodeURIComponent(base)).then(r=>r.text());
  await navigator.clipboard.writeText(txt);
  btn.textContent='Copied ✓';
}

// ---- review flow: step through flagged segments with their audio ----
let RV=null;
async function openReview(base){
  const d=await api('/api/review?base='+encodeURIComponent(base));
  if(d.error){alert(d.error);return}
  if(!d.items||!d.items.length){alert('Nothing left to review — all resolved.');refresh();return}
  RV={...d,i:0};
  $('#dlg').classList.add('wide');
  dlg.onkeydown=e=>{ // ←/→ flip between flagged segments unless typing
    if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName))return;
    if(e.key==='ArrowLeft'){e.preventDefault();rvGo(-1)}
    if(e.key==='ArrowRight'){e.preventDefault();rvGo(1)}
  };
  dlg.onclose=()=>{const a=$('#rva');if(a)a.pause();dlg.onclose=null;dlg.onkeydown=null;$('#dlg').classList.remove('wide');refresh()};
  renderReview();
  dlg.showModal();
}
function renderReview(){
  const it=RV.items[RV.i];
  const minorLeft=RV.items.slice(RV.i).filter(x=>x.minor).length;
  const alts=(it.alt||[]).map((a,k)=>`<div class="sub" style="margin-top:4px">Second engine heard “<b>${esc(a.theirs||'(nothing)')}</b>” where this says “${esc(a.ours||'(nothing)')}” <button style="font-size:12px;padding:2px 8px" onclick="rvUseAlt(${k})" title="Swap the second engine’s version into the text below">Use it</button></div>`).join('');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Review — ${esc(RV.base)}</h1>
  <div class="sub" style="margin-top:4px;display:flex;gap:8px;align-items:center">
    <button class="rvnav" onclick="rvGo(-1)" ${RV.i===0?'disabled':''} title="Previous flagged segment (nothing is changed when you flip)">‹</button>
    <button class="rvnav" onclick="rvGo(1)" ${RV.i>=RV.items.length-1?'disabled':''} title="Next flagged segment (nothing is changed when you flip)">›</button>
    <span>${RV.i+1} of ${RV.items.length} · ${esc(it.flags.join(', '))}${it.minor?' · minor':''}</span>
    ${minorLeft?`<button style="font-size:12px;padding:3px 10px" onclick="rvAcceptMinor()" title="Sub-second crosstalk crumbs (“like”, “so”…) — accept them all in one click; substantial items stay">✓ Accept ${minorLeft} minor</button>`:''}</div>
  <audio id="rva" controls src="/api/audio?base=${encodeURIComponent(RV.base)}"></audio>
  ${it.prev?`<div class="muted" style="margin-top:8px">…${esc(it.prev)}</div>`:''}
  <div style="display:flex;gap:8px;align-items:flex-start;margin:8px 0">
    <select id="rvspk" title="Who actually said this?">${spkOptions(RV.speakers,RV.people,it.speaker)}</select>
    <textarea id="rvtext" style="flex:1;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:60px">${esc(it.text)}</textarea>
  </div>
  ${alts}
  ${it.next?`<div class="muted">${esc(it.next)}…</div>`:''}
  <div style="display:flex;gap:8px;justify-content:space-between;margin-top:14px">
    <button onclick="rvPlay()">▶ Play clip</button>
    <div style="display:flex;gap:8px">
      <button onclick="rvNext()">Skip</button>
      <button onclick="rvApply('accept')" title="The speaker and text are right — clear the flag">Accept as-is</button>
      <button class="primary" onclick="rvApply('edit')" title="Save the corrected speaker/text back to the transcript files">Save changes</button>
    </div>
  </div>`;
  spkWireNew($('#rvspk'));
  rvPlay();
}
function rvGo(d){
  const j=RV.i+d;
  if(j<0||j>=RV.items.length)return;
  RV.i=j;renderReview();
}
function rvUseAlt(k){
  const a=RV.items[RV.i].alt[k],ta=$('#rvtext');
  // "ours" is normalized tokens — match them loosely against the display text
  const pat=a.ours.trim().split(/\s+/).map(t=>t.replace(/[.*+?^$()|[\]\\{}]/g,'\\$&')).join("[^A-Za-z0-9']+");
  const re=pat?new RegExp(pat,'i'):null;
  if(re&&re.test(ta.value))ta.value=ta.value.replace(re,a.theirs);
  else ta.value=(ta.value+' '+a.theirs).trim();
}
function rvPlay(){
  const it=RV.items[RV.i],a=$('#rva');
  if(!a)return;
  const stopAt=it.end+0.7;
  const go=()=>{a.currentTime=Math.max(0,it.start-0.7);a.play();
    a.ontimeupdate=()=>{if(a.currentTime>=stopAt)a.pause()}};
  a.readyState>=1?go():a.onloadedmetadata=go;
}
async function rvAcceptMinor(){
  const r=await api('/api/review',{base:RV.base,action:'accept_minor'});
  if(!r.ok){alert(r.error||'failed');return}
  RV.items=RV.items.filter((x,idx)=>idx<RV.i||!x.minor);
  if(RV.i>=RV.items.length){dlg.close();return}
  renderReview();
}
async function rvApply(action){
  const it=RV.items[RV.i];
  const body={base:RV.base,index:it.index,start:it.start,action};
  if(action==='edit'){
    const v=$('#rvspk').value;
    if(v==='__new__'){alert('Pick or name the speaker first.');return}
    body.text=$('#rvtext').value;body.speaker=v}
  const r=await api('/api/review',body);
  if(!r.ok){alert(r.error||'Save failed');return}
  if(r.merged){
    // the reassignment folded neighbors into one turn — every later item's
    // index/start in this pre-fetched list may now be stale; refetch
    const d=await api('/api/review?base='+encodeURIComponent(RV.base));
    RV.items=d.items;RV.i=0;
    if(!RV.items.length){dlg.close();return}
    renderReview();return}
  rvNext();
}
function rvNext(){
  RV.i++;
  if(RV.i>=RV.items.length){dlg.close();return}
  renderReview();
}

// Only one thing plays at a time: starting ANY audio pauses all the others.
document.addEventListener('play',e=>{
  document.querySelectorAll('audio').forEach(a=>{if(a!==e.target)a.pause()});
  if(voiceAudio&&!voiceAudio.paused&&e.target!==voiceAudio)stopVoice();
},true);
async function stopRun(){
  const b=$('#stopbtn');b.disabled=true;b.innerHTML='<span class="spin"></span> Stopping…';
  const r=await api('/api/stop',{});   // server verifies the whole group is gone
  b.disabled=false;b.textContent='Stop processing';
  const cq=r.cleared_jobs?` Also cancelled ${r.cleared_jobs} queued run${r.cleared_jobs>1?'s':''}.`:'';
  if(r.survivors&&r.survivors.length){
    $('#stopnote').textContent='Some processes would not die (pids '+r.survivors.join(', ')+') — try again, or reboot if it persists.';
  }else{
    $('#stopnote').textContent=(r.forced?'Stopped (had to force-kill a stuck worker). Memory is released.':'Stopped and verified — nothing left running, memory released.')+cq;
  }
  refresh();
}
async function openRedo(base,audio){
  const ed=await api('/api/edits?base='+encodeURIComponent(base));
  const title=(S.meetings.find(x=>x.base===base)||{}).title||base;
  $('#dlg').classList.remove('wide');
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Reprocess “${esc(title)}”</h1>
  <p class="muted" style="margin-top:8px">Re-runs transcription + speaker detection from the stored audio with the current model and speaker library. The existing transcript is replaced.</p>
  ${ed.n?`<p class="muted" style="margin-top:8px;color:var(--warn,#c60)"><b>⚠ This meeting has ${ed.n} manual edit${ed.n>1?'s':''}</b> (corrections, added or removed lines). A redo rebuilds everything from the audio, so they will no longer apply — they’re archived to a “.reviews.superseded.json” file next to the transcript, not deleted.</p>`:''}
  <label class="sub" style="display:block;margin-top:10px"><input type="checkbox" id="redostrict" class="checkbox" style="vertical-align:-3px"> strict mode — never guess an uncertain speaker (for confidential conversations)</label>
  <label class="sub" style="display:block;margin-top:6px"><input type="checkbox" id="redoverify" class="checkbox" style="vertical-align:-3px"> verify — a second engine listens too; disagreements get flagged with both versions</label>
  <label class="sub" style="display:block;margin-top:6px"><input type="checkbox" id="redoonetime" class="checkbox" style="vertical-align:-3px"> one-time speakers — don’t add this meeting’s unnamed voices to the Speakers list (focus groups)</label>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="dlg.close()">Cancel</button>
    <button class="primary" onclick="api('/api/run',{paths:['${escJs(audio)}'],force:true,strict:$('#redostrict').checked,verify:$('#redoverify').checked,onetime:$('#redoonetime').checked}).then(()=>{dlg.close();refresh()})">Reprocess</button>
  </div>`;
  dlg.showModal();
}
// Speaker picker used by the viewer editor and the review dialog: this
// meeting's speakers, then every enrolled person, then "New person…" —
// so a voice the diarizer missed (crosstalk) can still be credited correctly.
function spkOptions(speakers,people,sel){
  const seen=new Set(speakers.map(s=>s.display));
  let h=speakers.map(s=>`<option value="${s.id}" ${s.id===sel?'selected':''}>${esc(s.display)}</option>`).join('');
  const others=(people||[]).filter(p=>!seen.has(p));
  if(others.length)h+=`<optgroup label="Someone else">${others.map(p=>`<option value="name:${esc(p)}">${esc(p)}</option>`).join('')}</optgroup>`;
  return h+`<option value="__new__">＋ New person…</option>`;
}
function spkWireNew(sel){
  // remember the last real selection so cancelling the New-person prompt
  // restores the segment's actual speaker instead of jumping to option 0
  sel.addEventListener('focus',()=>{if(sel.value!=='__new__')sel._prev=sel.value});
  sel.addEventListener('change',()=>{
    if(sel.value!=='__new__'){sel._prev=sel.value;return}
    const nm=(prompt('Who said this? (name as it should appear in the transcript)')||'').trim();
    if(!nm){if(sel._prev!=null)sel.value=sel._prev;else sel.selectedIndex=0;return}
    const o=document.createElement('option');o.value='name:'+nm;o.textContent=nm;
    sel.insertBefore(o,sel.lastElementChild);sel.value='name:'+nm;
  });
}
let _tipTimer=null;
function fmtWhen(iso){
  if(!iso)return '';
  try{return new Date(iso).toLocaleString([],{month:'short',day:'numeric',
    year:'numeric',hour:'numeric',minute:'2-digit'});}catch(e){return iso;}
}
function _tipHtml(m){
  let h=esc(m.summary||'');
  if((m.next_steps||[]).length){
    h+='<div class="tiphead">Committed next steps</div><ul>'
      +m.next_steps.slice(0,5).map(s=>`<li>${esc(s)}</li>`).join('')
      +(m.next_steps.length>5?`<li>+${m.next_steps.length-5} more…</li>`:'')+'</ul>';
  }
  if(m.processed_at)h+=`<div class="tiphead">Processed</div>${esc(fmtWhen(m.processed_at))}`;
  return h;
}
document.addEventListener('mouseover',e=>{
  const row=e.target.closest&&e.target.closest('.hastip');
  const tip=$('#tipbox');
  if(!row||!row.dataset.base){clearTimeout(_tipTimer);tip.style.display='none';return}
  if(tip.dataset.for===row.dataset.base&&tip.style.display==='block')return;
  clearTimeout(_tipTimer);
  _tipTimer=setTimeout(()=>{
    const m=(S&&S.meetings||[]).find(x=>x.base===row.dataset.base);
    if(!m||!m.summary)return;
    tip.innerHTML=_tipHtml(m);tip.dataset.for=m.base;
    tip.style.display='block';
    const r=row.getBoundingClientRect(),tw=tip.offsetWidth,th=tip.offsetHeight;
    let x=Math.min(r.left,window.innerWidth-tw-12);
    let y=r.bottom+8;
    if(y+th>window.innerHeight-8)y=Math.max(8,r.top-th-8);  // flip above
    tip.style.left=Math.max(8,x)+'px';tip.style.top=y+'px';
  },250);
});
document.addEventListener('scroll',()=>{$('#tipbox').style.display='none'},true);
function inlineRename(base,title,ev){
  ev.stopPropagation();
  $('#tipbox').style.display='none';
  const el=ev.currentTarget;
  // edit the CLEAN name; the date is re-appended to the filename on save
  el.outerHTML=`<input class="inline-edit" id="ire" value="${esc(title)}" size="${Math.min(60,title.length+4)}">`;
  const inp=$('#ire');inp.focus();inp.select();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;
    const nm=inp.value.trim();
    if(save&&nm&&nm!==title){
      const r=await api('/api/rename',{base,new:nm});
      if(!r.ok)alert(r.error||'Rename failed');
    }
    inp.remove();refresh();
  };
  inp.onkeydown=e=>{if(e.key==='Enter')finish(true);else if(e.key==='Escape')finish(false)};
  inp.onblur=()=>finish(true);
}
function inlineDate(base,iso,ev){
  ev.stopPropagation();
  $('#tipbox').style.display='none';
  const el=ev.currentTarget;
  el.outerHTML=`<input type="date" class="inline-edit" id="ide" value="${esc(iso)}">`;
  const inp=$('#ide');inp.focus();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;
    if(save&&inp.value&&inp.value!==iso){
      const r=await api('/api/set_date',{base,date:inp.value});
      if(!r.ok)alert(r.error||'Date not saved');
    }
    inp.remove();refresh();
  };
  inp.onkeydown=e=>{if(e.key==='Enter')finish(true);else if(e.key==='Escape')finish(false)};
  inp.onblur=()=>finish(true);
}
const HUES=['#0071e3','#34c759','#ff9f0a','#ff375f','#bf5af2','#64d2ff','#ffd60a','#ac8e68'];
let tvTimer=null,TV=null;
function tvRow(g,i){
  const mm=Math.floor(g.start/60),ss=String(Math.floor(g.start%60)).padStart(2,'0');
  return `<div class="tseg${g.flags.length?' flagged':''}" id="ts${i}" onclick="tvSeek(${g.start})" title="${g.flags.length?'Uncertain: '+esc(g.flags.join(', '))+' — tap to listen':'Tap to listen from here'}">
  <span class="t">${mm}:${ss}</span>
  <span class="w" title="${esc(g.who)}" style="color:color-mix(in srgb, ${TV.color[g.who]||'currentColor'} 65%, var(--ink))">${esc(g.who)}${g.flags.length?' *':''}${g.edited?' ✎':''}</span>
  <span class="x">${esc(g.text)}</span>
  <button class="segbtn" onclick="tvEdit(${i},event)" title="Fix this line (speaker or text)">✎</button></div>`;
}
async function openTranscript(base,target=null){
  // re-render IN PLACE — a close()+showModal() pair races its own queued
  // close event on a fast API: the old view's onclose fires after the new
  // dialog opens, nulling TV and closing it (seen as a blank flash after
  // any merge/split/insert reload)
  if(tvTimer){clearInterval(tvTimer);tvTimer=null}
  TVF=null;  // any re-render rebuilds the rows, so old find marks are gone
  dlg.onclose=null;
  const d=await api('/api/transcript?base='+encodeURIComponent(base));
  if(d.error){alert(d.error);return}
  const color={};d.speakers.forEach((w,i)=>color[w]=HUES[i%HUES.length]);
  TV={base,segs:d.segments,speakers:d.speaker_options,people:d.people||[],color};
  const legend=d.speakers.map(w=>`<span class="chip"><span class="sdot" style="background:${color[w]}"></span>${esc(w)}</span>`).join(' ');
  $('#dlg').classList.add('wide');
  const _mm=S.meetings.find(x=>x.base===base)||{};
  const proc=_mm.processed_at;
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(_mm.title||base)}</h1>
  <div class="sub" style="margin:6px 0 2px">${legend}${d.strict?' <span class="chip warn">strict</span>':''}</div>
  ${proc?`<div class="sub" style="color:var(--muted,#8a8f98);margin-bottom:4px">Processed ${esc(fmtWhen(proc))}</div>`:''}
  <div style="display:flex;gap:8px;align-items:center">
    <audio id="tva" controls src="/api/audio?base=${encodeURIComponent(base)}" style="flex:1"></audio>
    <button onclick="tvAddAt()" title="Add a line the pipeline missed, at the audio’s current position — pause where you heard it, then click">＋ Line at playhead</button>
  </div>
  <div style="display:flex;gap:6px;align-items:center;margin-top:8px">
    <input id="tvfind" type="search" placeholder="Find in transcript… (⌘F)" autocomplete="off" spellcheck="false"
      style="flex:1;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:6px 8px"
      oninput="tvFindInput()" onkeydown="tvFindKey(event)">
    <span id="tvfindn" class="sub" style="min-width:64px;text-align:right"></span>
    <button onclick="tvFindNav(-1)" title="Previous match (Shift+Enter)">‹</button>
    <button onclick="tvFindNav(1)" title="Next match (Enter)">›</button>
  </div>
  <div id="tvlist" style="max-height:46vh;overflow:auto;margin-top:8px">${tvGap(-1)}${TV.segs.map((g,i)=>tvRow(g,i)+tvGap(i)).join('')}</div>
  <div style="display:flex;justify-content:flex-end;margin-top:12px"><button onclick="tvClose()">Close</button></div>`;
  if(!dlg.open)dlg.showModal();
  dlg.onclose=tvClose;
  dlg.onkeydown=e=>{if((e.metaKey||e.ctrlKey)&&e.key==='f'){e.preventDefault();const f=$('#tvfind');if(f){f.focus();f.select()}}};
  if(target!=null){
    const i=TV.segs.findIndex(g=>g.index===target);
    if(i>=0)setTimeout(()=>{const el=$('#ts'+i);
      if(el){el.scrollIntoView({block:'center'});el.classList.add('now')}
      tvSeek(TV.segs[i].start)},80);
  }
  const audio=$('#tva');
  tvTimer=setInterval(()=>{
    if(!audio||audio.paused||!TV)return;
    const t=audio.currentTime;
    document.querySelectorAll('.tseg.now').forEach(e=>e.classList.remove('now'));
    const i=TV.segs.findIndex(g=>t>=g.start&&t<g.end);
    if(i>=0){const el=$('#ts'+i);if(el)el.classList.add('now')}
  },500);
}
function tvSeek(t){const a=$('#tva');if(!a)return;
  const go=()=>{a.currentTime=t;a.play()};
  a.readyState>=1?go():a.addEventListener('loadedmetadata',go,{once:true})}
function tvClose(){if(tvTimer){clearInterval(tvTimer);tvTimer=null}TV=null;TVF=null;const a=$('#tva');if(a)a.pause();dlg.onclose=null;dlg.onkeydown=null;dlg.close();$('#dlg').classList.remove('wide');refresh()}
// ---- find within the open transcript (⌘F): occurrence count, next/prev, highlights ----
let TVF=null,tvFindT=null;  // {q, rx, hits:[{i,k}], cur} — hits are occurrences, not rows
function tvFindInput(){clearTimeout(tvFindT);tvFindT=setTimeout(()=>tvFindRun(true),200)}
function tvFindKey(e){
  if(e.key==='Enter'){
    e.preventDefault();clearTimeout(tvFindT);
    const q=$('#tvfind').value.trim();
    if(!TVF||TVF.q!==q)tvFindRun(true);
    else tvFindNav(e.shiftKey?-1:1);
  }else if(e.key==='Escape'){
    // clear the find, NOT the dialog — swallow it before the native <dialog>
    // cancel behavior closes the whole viewer
    e.preventDefault();e.stopPropagation();
    tvFindClear();$('#tvfind').blur();
  }
}
function tvFindRun(reset){
  const f=$('#tvfind'),q=f?(f.value||'').trim():'';
  const prev=TVF;
  tvFindUnmark();TVF=null;
  if(q.length<2){tvFindCount();return}
  const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
  const hits=[];
  TV.segs.forEach((g,i)=>{const m=g.text.match(rx);if(m)for(let k=0;k<m.length;k++)hits.push({i,k})});
  const cur=hits.length?(reset?0:Math.min(prev?Math.max(prev.cur,0):0,hits.length-1)):-1;
  TVF={q,rx,hits,cur};
  tvFindMark();tvFindCount();
  if(reset&&cur>=0)tvFindShow();
}
function tvFindMark(){
  if(!TVF||!TVF.hits.length)return;
  const rows={};TVF.hits.forEach((h,n)=>{(rows[h.i]=rows[h.i]||[]).push(n)});
  for(const i in rows){
    const el=$('#ts'+i),g=TV.segs[i];
    if(!el||el.classList.contains('editing'))continue;   // mid-edit: textarea owns the row
    const x=el.querySelector('.x');if(!x)continue;
    const t=g.text;let out='',last=0,k=0;
    t.replace(TVF.rx,(m,off)=>{                          // escape AROUND matches, so a query
      const n=rows[i][k++];                              // containing & < > still highlights
      out+=esc(t.slice(last,off))+`<mark${n===TVF.cur?' class="cur"':''}>${esc(m)}</mark>`;
      last=off+m.length;return m;
    });
    x.innerHTML=out+esc(t.slice(last));
  }
}
function tvFindUnmark(){
  if(!TVF)return;
  new Set(TVF.hits.map(h=>h.i)).forEach(i=>{
    const el=$('#ts'+i),g=TV.segs[i];
    if(!el||!g||el.classList.contains('editing'))return;
    const x=el.querySelector('.x');if(x)x.innerHTML=esc(g.text);
  });
}
function tvFindNav(d){
  if(!TVF||!TVF.hits.length)return;
  TVF.cur=(TVF.cur+d+TVF.hits.length)%TVF.hits.length;
  tvFindMark();tvFindCount();tvFindShow();
}
function tvFindShow(){
  const h=TVF.hits[TVF.cur];if(!h)return;
  const el=$('#ts'+h.i);if(!el)return;
  (el.querySelector('mark.cur')||el).scrollIntoView({block:'center'});
}
function tvFindCount(){
  const n=$('#tvfindn');if(!n)return;
  n.textContent=TVF?(TVF.hits.length?`${TVF.cur+1} of ${TVF.hits.length}`:'0 of 0'):'';
}
function tvFindClear(){
  clearTimeout(tvFindT);
  tvFindUnmark();TVF=null;
  const f=$('#tvfind');if(f)f.value='';
  tvFindCount();
}
function tvFindRefresh(){  // an edited row was rebuilt bare — recompute and repaint
  const f=$('#tvfind');
  if(TVF&&f&&f.value.trim().length>=2)tvFindRun(false);
}
function tvEdit(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i],el=$('#ts'+i);
  el.onclick=null;el.classList.add('editing');
  el.innerHTML=`<div style="flex:1;min-width:0">
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
      <select id="tvspk">${spkOptions(TV.speakers,TV.people,g.speaker)}</select>
      <select id="tvengine" title="Which engine listens again — a different one from the original gives an independent second opinion">
        ${[['parakeet','Parakeet · fast'],['mlxwhisper:large-v3','Whisper v3 · thorough'],['mlxwhisper:turbo','Whisper turbo']].map(([v,l])=>`<option value="${v}" ${v===(S.model==='parakeet'?'mlxwhisper:large-v3':'parakeet')?'selected':''}>${l}</option>`).join('')}
      </select>
      <button onclick="tvRetrans(${i},event)" title="Listen to this span again with the chosen engine and propose corrected text">↻ Re-transcribe</button>
      <span id="tvrx" class="sub"></span></div>
    <textarea id="tvta" style="width:100%;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:64px">${esc(g.text)}</textarea>
    <div style="display:flex;gap:6px;margin-top:6px">
      <button onclick="tvDelete(${i},event)" title="Remove this line entirely (echo, noise heard as speech)">Remove line</button>
      <button onclick="tvSplitUI(${i},event)" title="Split this line in two — click inside the text where the second voice starts, then press this">✂ Split line</button>
      <span class="grow"></span>
      <button onclick="tvPlaySpan(${i},event)">▶ Play span</button>
      <button onclick="tvRestore(${i},event)">Cancel</button>
      <button class="primary" onclick="tvSave(${i},event)">Save</button></div></div>`;
  spkWireNew($('#tvspk'));
}
function tvSplitUI(i,ev){
  ev.stopPropagation();
  const ta=$('#tvta'),pos=ta.selectionStart||0,full=ta.value;
  const a=full.slice(0,pos).trim(),b=full.slice(pos).trim();
  if(!a||!b){alert('Click inside the text where the split should happen — some words before the cursor, some after — then press Split again.');return}
  const g=TV.segs[i],el=$('#ts'+i);
  const box='width:100%;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:48px';
  el.innerHTML=`<div style="flex:1;min-width:0">
    <div class="sub" style="margin-bottom:6px">Splitting this line in two — set each half’s speaker. If a half matches its neighbor’s speaker, they join into one turn automatically.</div>
    <div style="display:flex;gap:6px;align-items:flex-start;margin-bottom:6px">
      <select id="tvsa">${spkOptions(TV.speakers,TV.people,g.speaker)}</select>
      <textarea id="tvta1" style="${box}">${esc(a)}</textarea></div>
    <div style="display:flex;gap:6px;align-items:flex-start">
      <select id="tvsb">${spkOptions(TV.speakers,TV.people,g.speaker)}</select>
      <textarea id="tvta2" style="${box}">${esc(b)}</textarea></div>
    <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:6px">
      <button onclick="tvRestore(${i},event)">Cancel</button>
      <button class="primary" onclick="tvSplitSave(${i},event)">Split</button></div></div>`;
  spkWireNew($('#tvsa'));spkWireNew($('#tvsb'));
  $('#tvsb').focus();
}
async function tvSplitSave(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i],sa=$('#tvsa').value,sb=$('#tvsb').value;
  if(sa==='__new__'||sb==='__new__'){alert('Pick or name both speakers first.');return}
  const r=await api('/api/review',{base:TV.base,action:'split',index:g.index,start:g.start,
    text_a:$('#tvta1').value,text_b:$('#tvta2').value,speaker_a:sa,speaker_b:sb});
  if(!r.ok){alert(r.error||'Split failed');return}
  openTranscript(TV.base,r.index);
}
function tvGap(i){
  return `<div class="tgap" id="tg${i}" tabindex="0" role="button"
    aria-label="Add a line here — a voice the pipeline missed"
    onclick="tvAddLine(${i})"
    onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();tvAddLine(${i})}"
    title="Add a line here — a voice the pipeline missed">＋ add line</div>`;
}
function tvFmt(t){const m=Math.floor(t/60),s=Math.floor(t%60);return m+':'+String(s).padStart(2,'0')}
function tvParseT(v){
  const p=String(v).trim().split(':').map(Number);
  if(p.some(isNaN))return null;
  return p.reverse().reduce((acc,x,k)=>acc+x*Math.pow(60,k),0);
}
function tvAddLine(i,at=null){
  document.querySelectorAll('.tgap.editing').forEach(e=>{const k=+e.id.slice(2);e.outerHTML=tvGap(k)});
  const g=TV.segs[i];  // undefined for i=-1 (the gap before the first line)
  const start=at!=null?at:(g?g.end:0);
  const el=$('#tg'+i);
  el.classList.add('editing');el.onclick=null;
  el.innerHTML=`<div style="text-align:left">
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
      <select id="tvnspk">${spkOptions(TV.speakers,TV.people,null)}</select>
      <label class="sub">at <input id="tvnat" value="${tvFmt(start)}" style="width:56px;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:6px;padding:3px 6px;text-align:center"></label>
      <span class="sub">(tip: pause the audio where you heard it — “＋ Line at playhead” fills this in)</span></div>
    <textarea id="tvnta" placeholder="What they said" style="width:100%;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:8px;padding:8px;min-height:48px"></textarea>
    <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:6px">
      <button onclick="tvSeek(Math.max(0,tvParseT($('#tvnat').value)-2))">▶ Listen here</button>
      <button onclick="const k=${i};$('#tg'+k).outerHTML=tvGap(k)">Cancel</button>
      <button class="primary" onclick="tvSaveNew()">Add</button></div></div>`;
  spkWireNew($('#tvnspk'));
  $('#tvnta').focus();
}
function tvAddAt(){
  const a=$('#tva'),t=a?a.currentTime:0;
  let i=TV.segs.findIndex(g=>g.start>t)-1;
  if(i<-1)i=TV.segs.length-1;
  i=Math.max(0,i);
  const el=$('#tg'+i);if(el)el.scrollIntoView({block:'center'});
  tvAddLine(i,t);
}
async function tvSaveNew(){
  const spk=$('#tvnspk').value,text=$('#tvnta').value,start=tvParseT($('#tvnat').value);
  if(spk==='__new__'){alert('Pick or name the speaker first.');return}
  if(start==null){alert('Time should look like 12:34.');return}
  if(!text.trim()){alert('Type what they said.');return}
  const r=await api('/api/review',{base:TV.base,action:'insert',start,end:start+3,speaker:spk,text});
  if(!r.ok){alert(r.error||'failed');return}
  openTranscript(TV.base,r.index);
}
async function tvDelete(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i];
  if(!confirm('Remove this line from the transcript? Its audio stays; only the text line goes.'))return;
  const r=await api('/api/review',{base:TV.base,action:'delete',index:g.index,start:g.start});
  if(!r.ok){alert(r.error||'failed');return}
  openTranscript(TV.base);
}
function tvPlaySpan(i,ev){ev.stopPropagation();const g=TV.segs[i],a=$('#tva');
  if(!a)return;a.currentTime=Math.max(0,g.start-0.5);a.play();
  a.ontimeupdate=()=>{if(a.currentTime>=g.end+0.5){a.pause();a.ontimeupdate=null}}}
function tvRestore(i,ev){if(ev)ev.stopPropagation();
  const el=$('#ts'+i);el.outerHTML=tvRow(TV.segs[i],i);tvFindRefresh()}
async function tvRetrans(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i];
  const eng=$('#tvengine').value;
  // estimate from THIS machine's own past re-transcriptions (localStorage
  // median per engine) — no hardcoded numbers that lie on faster/slower Macs
  const hist=JSON.parse(localStorage.getItem('stt_retrans_secs')||'{}');
  const past=(hist[eng]||[]).slice().sort((a,b)=>a-b);
  const estTxt=past.length?` (~${Math.round(past[Math.floor(past.length/2)])}s on this Mac)`:'';
  $('#tvrx').innerHTML='<span class="spin"></span> listening again…'+estTxt;
  const t0=Date.now();
  const r=await api('/api/retranscribe',{base:TV.base,start:g.start,end:g.end,engine:eng});
  hist[eng]=((hist[eng]||[]).concat((Date.now()-t0)/1000)).slice(-5);
  localStorage.setItem('stt_retrans_secs',JSON.stringify(hist));
  if(r.error){$('#tvrx').textContent='failed: '+r.error;return}
  $('#tvta').value=r.text;
  $('#tvrx').textContent='proposed by '+(r.engine||'second engine')+' — edit if needed, then Save';
}
async function tvSave(i,ev){
  ev.stopPropagation();
  const g=TV.segs[i],spk=$('#tvspk').value;
  if(spk==='__new__'){alert('Pick or name the speaker first.');return}
  const r=await api('/api/review',{base:TV.base,index:g.index,start:g.start,
    action:'edit',text:$('#tvta').value,speaker:spk});
  if(!r.ok){alert(r.error||'Save failed');return}
  if(r.merged){openTranscript(TV.base,r.index);return}  // rows changed: neighbors folded into one turn
  if(spk.startsWith('name:')){openTranscript(TV.base,g.index);return}  // new person → fresh legend/colors
  const sp=TV.speakers.find(s=>s.id===spk);
  g.text=$('#tvta').value;g.flags=[];g.edited=true;
  if(sp){g.who=sp.display;g.speaker=sp.id}
  tvRestore(i);
}
async function pickFolder(which){
  const prompt=which==='source'?'Choose the folder to watch for new recordings':'Choose where transcripts are stored';
  const r=await api('/api/pick_folder',{which,prompt});
  if(!r.cancelled)refresh();
}

function openSchedule(){
  const sc=S.schedule;const h=sc.hour??2,m=sc.minute??0;
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Nightly run time</h1>
  <p class="muted" style="margin-top:8px">Everything new processes at this time each night. If the Mac is asleep at that moment, the run happens automatically at the next wake. (The folder watch is a separate switch — it picks files up the moment they land.)</p>
  <div class="timegrid">
    <select id="sh">${[...Array(24).keys()].map(i=>`<option value="${i}" ${i===h?'selected':''}>${(i%12)||12} ${i<12?'AM':'PM'}</option>`).join('')}</select>
    <b>:</b>
    <select id="sm">${[0,15,30,45].map(i=>`<option value="${i}" ${i===m?'selected':''}>${String(i).padStart(2,'0')}</option>`).join('')}</select>
  </div>
  <p class="muted">Best between 1–5 AM, plugged in. Overnight runs need AC power.</p>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="dlg.close()">Cancel</button>
    <button class="primary" onclick="api('/api/schedule',{hour:+$('#sh').value,minute:+$('#sm').value}).then(()=>{dlg.close();refresh()})">Save</button>
  </div>`;
  dlg.showModal();
}
function mmss(t){const m=Math.floor(t/60),s=String(Math.floor(t%60)).padStart(2,'0');return m+':'+s}
// ---- enrolled-name autocomplete for "Who is this?" (native datalist can't be
// styled, scrolled, or ranked — with a long roster it overflowed the page) ----
let PN={items:[],cur:-1};
function pnameFilter(){
  const q=($('#pname').value||'').trim().toLowerCase();
  const names=S.enrolled.map(e=>e.name).sort((a,b)=>a.localeCompare(b));
  let items=q?names.filter(n=>n.toLowerCase().includes(q)):names;
  if(q){ // closest match first: whole-name prefix, then any word's prefix, then substring
    const rank=n=>{const l=n.toLowerCase();
      return l.startsWith(q)?0:(l.split(/\s+/).some(w=>w.startsWith(q))?1:2)};
    items=items.slice().sort((a,b)=>rank(a)-rank(b)||a.localeCompare(b));
  }
  PN={items,cur:items.length&&q?0:-1};
  pnameRender();
}
function pnameRender(){
  const dd=$('#pnamedd');if(!dd)return;
  const q=($('#pname').value||'').trim();
  const rx=q?new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'i'):null;
  dd.innerHTML=PN.items.map((n,i)=>
    `<div class="pnitem${i===PN.cur?' cur':''}" onmousedown="event.preventDefault();pnamePick(${i})">${rx?esc(n).replace(rx,m=>'<mark>'+m+'</mark>'):esc(n)}</div>`).join('');
  dd.style.display=PN.items.length?'block':'none';
  const c=dd.querySelector('.pnitem.cur');if(c)c.scrollIntoView({block:'nearest'});
}
function pnamePick(i){const f=$('#pname');if(!f)return;f.value=PN.items[i];pnameClose();f.focus()}
function pnameClose(){const dd=$('#pnamedd');if(dd)dd.style.display='none';PN.cur=-1}
function pnameKey(e){
  const dd=$('#pnamedd'),open=dd&&dd.style.display!=='none';
  if(e.key==='ArrowDown'||e.key==='ArrowUp'){
    e.preventDefault();
    if(!open){pnameFilter();return}
    if(!PN.items.length)return;
    PN.cur=(PN.cur+(e.key==='ArrowDown'?1:-1)+PN.items.length)%PN.items.length;
    pnameRender();
  }else if(e.key==='Enter'){
    e.preventDefault();
    if(open&&PN.cur>=0)pnamePick(PN.cur);
    else $('#pnamesave').click();
  }else if(e.key==='Escape'&&open){
    // close only the dropdown, not the whole dialog
    e.preventDefault();e.stopPropagation();pnameClose();
  }
}
async function openName(uid,display,meeting){
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Who is ${esc(display)}?</h1>
  <p class="muted" style="margin-top:8px">Listen to this voice — the clip is their longest turn in each meeting they were heard in, and “Read” opens the transcript at that exact moment for full context. Typing an <b>existing</b> name merges this voice into that person. Every past and future meeting relabels automatically.</p>
  <div id="nameclips"><span class="spin"></span></div>
  <input type="text" id="pname" placeholder="Person’s name" autocomplete="off" spellcheck="false" style="width:100%;margin-top:12px"
    oninput="pnameFilter()" onfocus="pnameFilter()" onblur="setTimeout(pnameClose,150)" onkeydown="pnameKey(event)">
  <div id="pnamedd" class="inset" style="display:none;max-height:170px;overflow-y:auto;margin-top:6px;padding:4px"></div>
  <div style="display:flex;gap:8px;justify-content:space-between;margin-top:16px">
    <button class="danger" onclick="api('/api/forget',{uid:'${escJs(uid)}'}).then(()=>{dlg.close();refresh()})">Not a real speaker</button>
    <div style="display:flex;gap:8px">
      <button onclick="dlg.close()">Cancel</button>
      <button class="primary" id="pnamesave" onclick="const n=$('#pname').value.trim();if(n)api('/api/name',{uid:'${escJs(uid)}',name:n}).then(()=>{dlg.close();refresh()})">Save name</button>
    </div>
  </div>`;
  dlg.showModal();
  $('#pname').focus();
  const r=await api('/api/voice_clips?speaker='+encodeURIComponent(uid));
  const box=$('#nameclips');
  if(!box)return;  // dialog closed while loading
  const clips=(r.clips||[]);
  box.innerHTML=clips.map(c=>`<div class="row" style="display:block">
    <div class="name" title="${esc(c.meeting)}">${esc(c.meeting)}</div>
    <div class="sub">their longest turn · at ${mmss(c.start)} · ${Math.round(c.dur)}s</div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
      <audio controls preload="none" style="height:32px;flex:1;min-width:0" onplay="document.querySelectorAll('audio').forEach(a=>{if(a!==this)a.pause()})"
        src="/api/snippet?meeting=${encodeURIComponent(c.meeting)}&speaker=${encodeURIComponent(uid)}&secs=45"></audio>
      <button onclick="openTranscript('${escJs(c.meeting)}',${c.index})" title="Open the transcript at this moment — hear as much as you like, with the conversation around it">Read</button>
    </div>
  </div>`).join('')
    ||`<audio controls src="/api/snippet?speaker=${encodeURIComponent(uid)}&secs=45"></audio>`;
}
function reassignSample(name,index){
  // move a misattributed sample to the right person instead of discarding it
  const others=S.enrolled.map(e=>e.name).filter(x=>x!==name);
  const hint=others.length?`\nExisting people: ${others.slice(0,6).join(', ')}`:'';
  const to=(prompt(`This voice sample is really whose? Type an existing name to add it there, or a new name to start a profile.${hint}`)||'').trim();
  if(!to||to===name)return;
  api('/api/reassign_sample',{name,index,to}).then(r=>{
    if(!r.ok){alert(r.error||'failed');return;}
    dlg.close();refresh();
  });
}
function renameSpeaker(oldName){
  const n=$('#rname').value.trim();
  if(!n||n===oldName)return;
  // renaming onto an existing person silently merges their voiceprints — say so
  const clash=S.enrolled.find(e=>e.name.toLowerCase()===n.toLowerCase()&&e.name!==oldName);
  if(clash&&!confirm(`${n} is already a saved person. Renaming “${oldName}” to “${n}” MERGES their voice samples into one profile. Continue?`))return;
  api('/api/rename_speaker',{name:oldName,new:n}).then(()=>{dlg.close();refresh()});
}
function openSpeakerActions(key,display,meeting){
  const isName=key.startsWith('name:');
  const others=[...S.enrolled.map(e=>({k:'name:'+e.name,d:e.name})),
                ...S.unknowns.map(u=>({k:'uid:'+u.uid,d:u.display}))]
               .filter(o=>o.k!==key&&(isName?o.k.startsWith('name:'):true));
  const enr=isName?S.enrolled.find(x=>x.name===key.slice(5)):null;
  let samplerows='';
  if(enr){
    const n=enr.samples,srcs=enr.sources||[],cap=S.max_samples||5;
    samplerows=`<div class="sub" style="margin-top:8px;font-weight:600">Voice samples (${n} of ${cap})</div>
      <div class="sub" style="margin:2px 0 6px;color:var(--muted,#8a8f98)">A profile keeps up to ${cap} samples. A varied set, from different meetings, rooms, and mics, identifies this person more reliably than several clips from one recording.</div>`+
      Array.from({length:n},(_,i)=>{
        const src=srcs.length===n?srcs[i]:(srcs[srcs.length-n+i]||null);
        return `<div class="row">
          ${src?`<button class="playbtn" data-key="${esc(enr.name)}" data-meeting="${esc(src)}" onclick="playVoice(this)">▶</button>`:'<span style="width:30px" title="Source unknown (enrolled before tracking)"></span>'}
          <div class="grow"><div class="sub">Sample ${i+1} — ${src?esc(src):'source unknown'}</div></div>
          <button title="Reassign — this sample is really someone else's voice; move it to the right person instead of deleting it" onclick="reassignSample('${escJs(enr.name)}',${i})">→</button>
          ${n>1?`<button title="Remove this sample (e.g. a bad recording); the person keeps their other samples" onclick="api('/api/remove_sample',{name:'${escJs(enr.name)}',index:${i}}).then(r=>{if(!r.ok)alert(r.error||'failed');dlg.close();refresh()})">✕</button>`:''}
        </div>`}).join('');
  }
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(display)}</h1>
  ${meeting?`<audio controls src="/api/snippet?meeting=${encodeURIComponent(meeting)}&speaker=${encodeURIComponent(key.split(':')[1])}" style="margin-top:10px"></audio>`:''}
  ${samplerows}
  ${isName?`
  <div class="row"><div class="grow"><div class="name">Rename</div><div class="sub">Fix the name everywhere</div></div>
    <input type="text" id="rname" value="${esc(display)}" style="width:150px">
    <button onclick="renameSpeaker('${escJs(display)}')">Apply</button></div>`:''}
  <div class="row"><div class="grow"><div class="name">Merge into…</div>
    <div class="sub">This voice is really the same person as</div></div>
    <select id="mtarget">${others.map(o=>`<option value="${esc(o.k)}">${esc(o.d)}</option>`).join('')}</select>
    <button ${others.length?'':'disabled'} onclick="api('/api/merge_speakers',{src:'${escJs(key)}',dst:$('#mtarget').value}).then(()=>{dlg.close();refresh()})">Merge</button></div>
  ${isName?'':`<div class="row"><div class="grow"><div class="name">Hide from this list</div>
    <div class="sub">One-time voice (focus group) — keep it matchable but out of the way; restore any time</div></div>
    <button onclick="api('/api/hide_unknown',{uid:'${escJs(key.split(':')[1])}',hide:true}).then(()=>{dlg.close();refresh()})">Hide</button></div>`}
  <div class="row" style="border:0"><div class="grow"><div class="name">Remove</div>
    <div class="sub">${isName?'Un-enroll; their lines revert to Speaker N':'Forget this voice entirely'}</div></div>
    <button class="danger" onclick="if(confirm('Remove ${escJs(display)}?'))api(${isName?`'/api/remove_speaker',{name:'${escJs(display)}'}`:`'/api/forget',{uid:'${escJs(key.split(':')[1])}'}`}).then(()=>{dlg.close();refresh()})">Remove</button></div>
  <div style="display:flex;justify-content:flex-end;margin-top:14px"><button onclick="dlg.close()">Close</button></div>`;
  dlg.showModal();
}
function openRename(base){
  const cur=(S.meetings.find(x=>x.base===base)||{}).title||base;
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Rename recording</h1>
  <p class="muted" style="margin-top:8px">Suggest a name from what was actually discussed (runs locally — nothing leaves this Mac), or type your own. The date is kept in the filename automatically, so a recurring name is fine.</p>
  <input type="text" id="newname" value="${esc(cur)}" style="width:100%;margin-top:10px">
  <label class="sub" style="display:block;margin-top:10px">Meeting date
    <input type="date" id="mdate" value="${esc((S.meetings.find(x=>x.base===base)||{}).date||'')}"
      style="margin-left:8px;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:6px;padding:3px 6px">
    <span class="muted" style="margin-left:6px">groups the list by month — fix it here if the recording was exported late</span></label>
  <div id="sumnote" class="muted" style="margin-top:8px"></div>
  <div style="display:flex;gap:8px;justify-content:space-between;margin-top:16px">
    <button onclick="suggest('${escJs(base)}')" ${S.llm_available?'':'disabled'}>${S.llm_available?'✨ Suggest from content':'(local LLM not installed)'}</button>
    <div style="display:flex;gap:8px">
      <button onclick="dlg.close()">Cancel</button>
      <button class="primary" onclick="doRename('${escJs(base)}')">Rename</button>
    </div>
  </div>`;
  dlg.showModal();
}
function openSummary(base){
  const m=S.meetings.find(x=>x.base===base)||{};
  const steps=(m.next_steps||[]).length?`<div style="font-weight:600;margin-top:10px">Committed next steps</div><ul style="margin:6px 0 0 18px">${m.next_steps.map(s=>`<li class="muted">${esc(s)}</li>`).join('')}</ul>`:'';
  $('#dlg').innerHTML=`<h1 style="font-size:18px">${esc(m.title||base)}</h1>
  <div style="margin-top:10px;max-height:340px;overflow:auto"><div id="sumbody" class="muted">${m.summary?esc(m.summary):('No summary yet — generate one below. '+(S.llm_backend==='local'?'Runs locally; nothing leaves this Mac.':'Uses your cloud assistant.'))}</div>${steps}</div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="openAsk('${escJs(base)}')" ${S.llm_available?'':'disabled'}>Ask a question…</button>
    <button onclick="genSummary('${escJs(base)}')" ${S.llm_available?'':'disabled'}>${m.summary?'Regenerate':'✨ Generate summary'}</button>
    <button onclick="dlg.close()">Close</button>
  </div>`;
  dlg.showModal();
}
// ---- Ask: questions about one meeting, answered locally from its transcript ----
let ASK=null;  // {base, hist:[{q,a,err}], busy} — per-session only, never persisted
function openAsk(base){
  if(dlg.open)dlg.close();  // launched from the Summary dialog, for instance
  // same meeting: the thread survives close/reopen (it dies with the page,
  // or when you ask about a different meeting — never written to disk)
  if(!ASK||ASK.base!==base)ASK={base,hist:[],busy:false};
  $('#askmeet').textContent=(S.meetings.find(x=>x.base===base)||{}).title||base;
  $('#askpriv').textContent='Answers come from this transcript only. '
    +(S.llm_backend==='local'?'They are generated on this Mac; nothing leaves the machine.':'They are generated by your cloud assistant (the transcript text is sent to it for this).')
    +' Follow-up questions understand the earlier ones. The thread is never saved.';
  $('#asknote').textContent='';
  $('#askq').disabled=ASK.busy;$('#askbtn').disabled=ASK.busy;
  flyToggle('#askfly',true);
  askRender();
  $('#askq').focus();
}
function askRender(){
  if(!ASK)return;
  $('#askthread').innerHTML=ASK.hist.length?ASK.hist.map(h=>`
    <div class="bub q">${esc(h.q)}</div>
    <div class="bub a${h.err?' warn':''}">${
      h.a?esc(h.a):'<span class="spin"></span> Reading the transcript and thinking… '+(S.llm_backend==='local'?'usually 20-60s (the model loads fresh for each question)':'usually a few seconds')}</div>`).join('')
    :'<div class="sub" style="padding:18px 2px">Ask anything about this meeting: decisions, who said what, commitments…</div>';
  $('#askthread').scrollTop=$('#askthread').scrollHeight;
}
async function askSend(){
  if(!ASK||ASK.busy)return;
  const q=$('#askq').value.trim();
  if(!q)return;
  const t=ASK;  // this thread — the flyout may switch meetings while we wait
  // last few successful exchanges ride along so follow-ups make sense
  const hist=t.hist.filter(h=>h.a&&!h.err).slice(-3).map(h=>({q:h.q,a:h.a}));
  t.hist.push({q,a:''});t.busy=true;
  $('#askq').value='';$('#askq').disabled=true;$('#askbtn').disabled=true;
  askRender();
  const r=await api('/api/ask',{base:t.base,question:q,history:hist});
  const cur=t.hist[t.hist.length-1];
  if(r.answer){cur.a=r.answer}
  else{cur.a='⚠ '+(r.error||'No answer produced.');cur.err=true}
  t.busy=false;
  if(ASK!==t)return;  // a different meeting's thread is open now — drop quietly
  $('#asknote').textContent=r.truncated
    ?'Long meeting: middle portions were sampled, so details from the middle may be missing.':'';
  $('#askq').disabled=false;$('#askbtn').disabled=false;
  askRender();$('#askq').focus();
}
async function genSummary(base){
  $('#sumbody').innerHTML='<span class="spin"></span> Reading the transcript… '+(S.llm_backend==='local'?'(~10-20s)':'(a few seconds)');
  const r=await api('/api/suggest?base='+encodeURIComponent(base));
  $('#sumbody').textContent=r.summary||r.error||'No summary produced.';
  await refresh();
  openSummary(base);  // re-render with next steps from fresh state
}
async function suggest(base){
  $('#sumnote').innerHTML='<span class="spin"></span> Reading the transcript…';
  const r=await api('/api/suggest?base='+encodeURIComponent(base));
  if(r.title||r.suggested_name){$('#newname').value=r.title||r.suggested_name;$('#sumnote').textContent=r.summary||''}
  else $('#sumnote').textContent=r.error||'Could not suggest a name.';
}
async function doRename(base){
  const m=S.meetings.find(x=>x.base===base)||{};
  const title=m.title||base;
  const nm=$('#newname').value.trim(),dt=$('#mdate').value;
  let cur=base;
  if(nm&&nm!==title){
    const r=await api('/api/rename',{base,new:nm});
    if(!r.ok){$('#sumnote').textContent=r.error||'Rename failed';return}
    cur=r.base||nm;  // the date was re-appended to the filename
  }
  if(dt&&dt!==m.date){
    const r=await api('/api/set_date',{base:cur,date:dt});
    if(!r.ok){$('#sumnote').textContent=r.error||'Date not saved';return}
  }
  dlg.close();refresh();
}
async function checkUpdates(){
  $('#updbtn').innerHTML='<span class="spin"></span>';
  const r=await api('/api/check_updates');
  const ups=(r.models||[]).filter(m=>m.update_available);
  $('#updnote').textContent=ups.length?('Updates available: '+ups.map(u=>u.label).join(', ')):'All models are current.';
  $('#updbtn').textContent='Check';
}
function openCloudKeys(){
  const ck=S.cloud_keys||{};
  // fixed columns (label · input · status · clear) so every row lines up, with
  // the status/clear slots RESERVED even when empty — mixed-width rows read
  // as misalignment. Hints sit under their own input.
  const row=(prov,label,hint)=>`<div style="display:flex;gap:10px;align-items:flex-start;margin-top:12px">
    <span class="sub" style="width:118px;flex:none;padding-top:7px">${label}</span>
    <div class="grow" style="min-width:0">
      <div style="display:flex;gap:8px;align-items:center">
        <input type="password" id="ck_${prov}" placeholder="${ck[prov]?'saved — paste to replace':'paste API key'}" style="flex:1;min-width:0;font:inherit;background:var(--card);color:var(--ink);border:1px solid var(--hairline);border-radius:6px;padding:6px 8px">
        <span class="sub" style="width:16px;flex:none;text-align:center" title="${ck[prov]?'A key is saved':''}">${ck[prov]?'✓':''}</span>
        <span style="width:54px;flex:none;text-align:right">${ck[prov]?`<button onclick="clearCloudKey('${prov}','${label}')" title="Remove the saved ${label} key from this Mac">Clear</button>`:''}</span>
      </div>
      <div class="sub" style="opacity:.75;margin-top:3px">${hint}</div>
    </div></div>`;
  $('#dlg').classList.add('wide');  // room for full placeholders and hints
  $('#dlg').innerHTML=`<h1 style="font-size:18px">Cloud transcription keys</h1>
  <p class="muted" style="margin-top:8px">Optional: transcribe with a cloud engine instead of the local models. Only the audio is uploaded — speaker identification and voiceprints stay on this Mac. <b>Strict-mode recordings never upload</b>, whatever engine is selected. Keys are stored in stt.env on this machine and never shown again.</p>
  ${row('scribe','ElevenLabs Scribe','elevenlabs.io → Profile → API keys')}
  ${row('openai','OpenAI','platform.openai.com → API keys · also used by the OpenAI assistant below')}
  ${row('voxtral','Mistral Voxtral','console.mistral.ai → API keys')}
  <h1 style="font-size:15px;margin-top:20px">Assistant (summaries &amp; Ask)</h1>
  <p class="muted" style="margin-top:6px">The assistant drafts summaries and answers Ask questions. The local model needs no key. Choosing a cloud assistant in Settings sends transcript text to that provider for these features only; <b>strict-mode recordings always use the local model</b>.</p>
  ${row('anthropic','Anthropic (Claude)','console.anthropic.com → API keys')}
  <div style="display:flex;gap:10px;align-items:center;margin-top:12px">
    <span class="sub" style="width:118px;flex:none">OpenAI (GPT)</span>
    <div class="grow sub">uses the OpenAI key from the transcription section above · ${ck.openai?'✓ key saved':'no key yet'}</div>
  </div>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
    <button onclick="dlg.close()">Cancel</button>
    <button class="primary" onclick="saveCloudKeys()">Save</button>
  </div>`;
  dlg.onclose=()=>{dlg.onclose=null;$('#dlg').classList.remove('wide')};
  dlg.showModal();
}
async function saveCloudKeys(){
  const r=await api('/api/cloud_keys',{scribe:$('#ck_scribe').value,openai:$('#ck_openai').value,voxtral:$('#ck_voxtral').value,anthropic:$('#ck_anthropic').value});
  if(!r.ok){alert(r.error||'Could not save');return}
  dlg.close();refresh();
}
async function clearCloudKey(prov,label){
  if(!confirm(`Remove the saved ${label} key? Cloud transcription with this provider stops working until a new key is pasted.`))return;
  const r=await api('/api/cloud_keys',{clear:[prov]});
  if(!r.ok){alert(r.error||'Could not clear the key');return}
  S.cloud_keys=r.set;   // re-render the dialog from fresh state, keep it open
  openCloudKeys();
}
const THEME_META={auto:["◐","Theme: matching macOS — click for light"],
light:["☀","Theme: light — click for dark"],dark:["☾","Theme: dark — click to match macOS"]};
function themeNow(){const q=new URLSearchParams(location.search).get("theme");
  return q==="light"||q==="dark"?q:(localStorage.getItem("stt_theme")||"auto")}
function applyTheme(t){
  if(t==="light"||t==="dark"){localStorage.setItem("stt_theme",t);document.documentElement.dataset.theme=t}
  else{localStorage.removeItem("stt_theme");delete document.documentElement.dataset.theme}
  const b=$('#themebtn');if(b){b.textContent=THEME_META[t][0];b.title=THEME_META[t][1]}
}
function cycleTheme(){
  const order=["auto","light","dark"];
  applyTheme(order[(order.indexOf(themeNow())+1)%3]);
}
applyTheme(themeNow());
{const ms=localStorage.getItem('stt_msort');if(ms&&$('#msort'))$('#msort').value=ms}
{const mc=localStorage.getItem('stt_mcat');if(mc&&$('#mcat'))$('#mcat').value=mc}
function setModel(){api('/api/model',{model:$('#modelsel').value}).then(r=>{if(!r.ok)alert(r.error||'Could not switch model');refresh()})}
function setLlm(){api('/api/llm_backend',{backend:$('#llmsel').value}).then(r=>{if(!r.ok)alert(r.error||'Could not switch the assistant');refresh()})}
function flyToggle(id,open){
  const el=$(id);
  const want=open===undefined?!el.classList.contains('open'):open;
  if(want)document.querySelectorAll('.fly.open').forEach(f=>{if(f!==el)f.classList.remove('open')});
  el.classList.toggle('open',want);
  $('#flyveil').classList.toggle('open',!!document.querySelector('.fly.open'));
}
function flyCloseAll(){
  document.querySelectorAll('.fly.open').forEach(f=>f.classList.remove('open'));
  $('#flyveil').classList.remove('open');
}
function toggleSettings(open){flyToggle('#setfly',open)}
document.addEventListener('keydown',e=>{
  // Escape closes whichever flyout is up — but never while a dialog is open
  // (the dialog's own Escape handling owns that case)
  if(e.key==='Escape'&&!dlg.open&&document.querySelector('.fly.open')){
    e.preventDefault();flyCloseAll();
  }
});
// ---- processing history flyout: the complete, permanent results list ----
let HIST=null;
async function openHistory(){
  flyToggle('#histfly',true);
  $('#histlist').innerHTML='<div style="padding:12px 0"><span class="spin"></span></div>';
  $('#histcount').textContent='';
  const r=await api('/api/history');
  HIST=r.results||[];
  histRender();
}
function histRender(){
  if(HIST===null)return;
  const q=($('#histq').value||'').trim().toLowerCase();
  const f=$('#histok').value;
  const rows=HIST.filter(r=>(!q||(r.name||'').toLowerCase().includes(q))&&(!f||(f==='ok')===!!r.ok));
  const CAP=400, nOk=rows.filter(r=>r.ok).length;
  $('#histcount').textContent=rows.length?`${rows.length} result${rows.length===1?'':'s'} · ${nOk} processed · ${rows.length-nOk} failed`:'';
  let day='';
  $('#histlist').innerHTML=rows.slice(0,CAP).map(r=>{
    const d=(r.at||'').slice(0,10);
    const hdr=d!==day?`<div class="mgroup">${d?new Date(d+'T12:00:00').toLocaleDateString([],{weekday:'short',month:'long',day:'numeric',year:'numeric'}):'Undated'}</div>`:'';
    day=d;
    return hdr+`<div class="row"><span class="chip ${r.ok?'done':'warn'}">${r.ok?'✓':'failed'}</span>
      <div class="grow" style="min-width:0"><div class="name">${esc((r.name||'').replace(/\.[^.]+$/,''))}</div>
      ${r.summary?`<div class="sub" style="word-break:break-word">${esc(r.summary)}</div>`:''}</div>
      <span class="sub" style="flex:none">${(r.at||'').slice(11,16)}</span></div>`;
  }).join('')+(rows.length>CAP?`<div class="sub" style="padding:10px 0">Showing the first ${CAP} — narrow the filter to see older results.</div>`:'')
  ||'<div class="sub" style="padding:10px 0">No matching results.</div>';
}
function togWatch(){api('/api/automation',{watch:!S.schedule.watch}).then(r=>{if(!r.ok)alert(r.error||'Could not change the folder watch');refresh()})}
function togNightly(){api('/api/automation',{nightly:!S.schedule.nightly}).then(r=>{if(!r.ok)alert(r.error||'Could not change the nightly run');refresh()})}
async function refresh(){try{S=await api('/api/state');render()}catch(e){}}
refresh().then(()=>{
  // deep links, resolved once against the first loaded state. The new shell
  // (?ui=new) bridges its tray actions here until the meeting page ships:
  //   ?open=<meeting>   opens that transcript directly
  //   ?review=<meeting> opens that meeting's review dialog
  //   ?who=<uid>        opens the who-is-this dialog for that unknown voice
  const p=new URLSearchParams(location.search);
  const o=p.get('open');
  if(o&&(S.meetings||[]).some(m=>m.base===o))openTranscript(o);
  const rv=p.get('review');
  if(rv&&(S.meetings||[]).some(m=>m.base===rv))openReview(rv);
  const who=p.get('who');
  if(who){const u=(S.unknowns||[]).find(x=>x.uid===who);
    if(u)openName(u.uid,u.display,u.meetings[0]||'');}
});
setInterval(refresh,2000);
