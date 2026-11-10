/* ============================================================================
   The voice sheet (plan Phase 4D): the speaker labeling step. A right
   slide-over 560px wide, full screen below 900px, opened from Home, the
   meeting notice, the transcript legend or a line's speaker, the People rail,
   and Speakers. It keeps the rule "form before evidence":

   1. "Who is Speaker 58?", the meeting title, "voice 1 of 2", progress marks.
   2. The name box with the enrolled names, focused on open.
   3. The preview line: "Names this voice here and in 4 other meetings."
   4. Up to three Looks like chips, which only fill the box.
   5. The longest turn, with its play button and the voice's talk time.
   6. Up to three lines of what the voice said.
   7. "Already recognized: ..." and the count of brief voices hidden.
   8. Footer: Save and next, Skip, Not a real speaker (Clear name for a voice
      named for this meeting only).

   Saves go through vsSave: the identity is captured before the await, one
   naming request runs at a time (VBUSY),
   the floor refusal offers the meeting-only save, the quality warning offers
   Save anyway, and the toast says what the server applied. The walk asks
   about each voice once per run: most talk time first, and never a voice the
   run already named, dropped, or skipped (VRUN). The end state counts what
   happened and offers "Next meeting: <title>".
   ============================================================================ */

let VS=null;                                   // the open sheet
let VBUSY=false;                               // one naming request at a time
let VCARRY=null;                               // a hand-off to the next meeting
const VRUN={seen:[],resolved:[],passed:[]};     // this run: never ask twice

// which voice the sheet shows, as a comparable string. An answer that lands
// after the sheet closed or moved on must drop its result. The meeting is part
// of a uid's identity too: the same uid opened from a second meeting shows that
// meeting's lines.
function vsToken(){
 if(!VS||VS.done)return '';const v=VS.list[VS.i];if(!v)return '';
 return (v.uid?'u\x00'+v.uid:'c\x00'+(v.ids||[v.id]).join(','))+'\x00'+(VS.base||'')+'\x00'+VS.i;
}
const vsCur=()=>VS&&!VS.done?VS.list[VS.i]||null:null;

/* ---------------------------------------------------------------- open ---- */
// "Name voices": this meeting's voices that ask, the most talk first. Brief
// voices under a minute stay behind their count unless no other voice is
// open. A walk from the queue keeps the run's skips; one the reader started
// asks again about a voice skipped earlier.
function vsOpenMeeting(o){
 if(!MP||!MP.loaded)return false;
 const m=mRow()||{};const run=o.run||null;
 const open=openVoices(MP.voices);
 const ask=open.filter(v=>Object.prototype.hasOwnProperty.call(m.ask_clusters||{},v.display));
 let list=voiceOrder(ask.length?ask:open,MP.segs,VRUN.resolved,o.queue?VRUN.passed:[]);
 if(!list.length){toast('Every voice here has a name.');return false;}
 const i=list.findIndex(v=>!v.st);
 if(i<0){
  // every open voice here was skipped earlier in this run: the queue has
  // nothing to ask, so it hands off to the next meeting
  if(o.queue&&vsHandOff())return true;
  list=list.map(v=>Object.assign(v,{st:''}));
 }
 if(!VRUN.seen.includes(MP.base))VRUN.seen.push(MP.base);
 vsShow({base:MP.base,list,i:Math.max(0,i),single:false,queue:!!o.queue,
  brief:ask.length?open.length-ask.length:0,named:(MP.voices||[]).filter(v=>v.kind==='plain'||v.kind==='local').map(v=>v.display)});
 return true;
}
// one voice from the open page: the legend, a line's speaker, the rail
function vsOpenVoice(id){
 if(!MP)return;const v=mVoiceOf(id);
 if(!v){toast('That voice is not on this page any more.');return;}
 if(v.kind==='uid'&&!vsLiveUid(v.uid,MP.base))return;
 vsShow({base:MP.base,list:[Object.assign({},v,{st:'',talk:0})],i:0,single:true,local:v.kind==='local',named:[],brief:0});
}
// a registry voice from Home or Speakers. A uid that is no longer unknown was
// rendered before the voice was named: say so and catch up, never open a dead
// sheet on the raw id.
function vsOpenUid(uid,base){
 const u=vsLiveUid(uid,base);if(!u)return;
 vsShow({base:base||null,list:[{uid,id:null,ids:null,display:u.display,kind:'uid',st:''}],i:0,single:true,named:[],brief:0});
}
function vsLiveUid(uid,base){
 const u=((S&&S.unknowns)||[]).find(x=>x.uid===uid);
 if(u)return u;
 toast(S&&(S.relabel_running||S.relabel_pending)?'Already named. Names are being applied now.':'This voice was already named.');
 refresh();if(base&&mOpenHere(base))mReload();
 return null;
}
function vsShow(o){
 vsStopClip();
 VS=Object.assign({i:0,value:'',err:'',warn:null,preview:null,looks:[],lines:null,clip:null,clipNote:'',saved:0,skipped:0,dropped:0,done:false},o);
 vsRender(true);vsFetch();
}
function vsClose(){
 vsStopClip();VS=null;const s=$('#sheet');if(s)s.innerHTML='';
 if(MP&&UI.route==='meeting'){const m=mRow();if(m)mSync(m,false);}
}

/* ------------------------------------------------------------- evidence ---- */
function vsPreviewLine(p){
 const here=(p&&p.here)||null;
 const n=(((p&&p.targets)||[]).filter(t=>t!==here)).length;
 return n?`Names this voice here and in ${plural(n,'other meeting')}`:'Names this voice here only';
}
function vsFetch(){
 const v=vsCur();if(!v)return;const tok=vsToken(),base=VS.base;
 // fired and forgotten: the sheet is usable the instant it opens
 api('/api/name_preview',v.uid?{uid:v.uid}:{meeting:base,speaker:v.id}).then(r=>{
  if(vsToken()!==tok||!r||r.error||!Array.isArray(r.targets))return;   // closed, moved on, or refused: paint nothing
  VS.preview=vsPreviewLine(r);VS.looks=(r.looks_like||[]).slice(0,3);vsRender();
 }).catch(()=>{});
 vsLines(v,tok);
 if(v.uid&&!(base&&MP&&MP.base===base&&MP.hasAudio))vsClips(v,tok);
}
// what the voice said, from every cluster of a split voice, longest first.
// Only a successful fetch may say "no text": an error is not silence.
async function vsLines(v,tok){
 const base=VS.base;if(!base)return;
 const keys=v.ids&&v.ids.length?v.ids:[v.display];
 let rs;try{rs=await Promise.all(keys.map(id=>api('/api/voice_lines?base='+encodeURIComponent(base)+'&speaker='+encodeURIComponent(id))));}catch(e){return;}
 if(vsToken()!==tok)return;
 if(rs.some(r=>!r||r.error||!Array.isArray(r.lines)))return;
 const lines=[].concat(...rs.map(r=>r.lines)).sort((a,b)=>(+b.dur||0)-(+a.dur||0));
 const talk=rs.reduce((a,r)=>a+(+r.talk_secs||0),0);
 VS.lines=lines;VS.talk=talk;
 if(lines.length&&MP&&MP.base===base&&MP.hasAudio)VS.clip={start:+lines[0].start||0,end:(+lines[0].start||0)+(+lines[0].dur||0),span:true};
 vsRender();
}
// a registry voice seen from outside its meeting: its longest turn anywhere
async function vsClips(v,tok){
 let r;try{r=await api('/api/voice_clips?speaker='+encodeURIComponent(v.uid));}catch(e){return;}
 if(vsToken()!==tok||!r)return;
 const c=(r.clips||[])[0];
 if(c)VS.clip={start:+c.start||0,end:(+c.start||0)+(+c.dur||0),src:'/api/snippet?meeting='+encodeURIComponent(c.meeting)+'&speaker='+encodeURIComponent(v.uid)+'&secs=45',meeting:c.meeting};
 else VS.clipNote=r.reason==='sources_deleted'?'No audio is left. The source recordings were deleted.'
  :r.reason==='sources_archived'?'Every meeting this voice was heard in is archived. Restore one to hear it.':'';
 vsRender();
}
// the clip's meeting by its title, never its folder name, and only when the
// sheet is not already about that meeting
function vsClipWhere(){
 const c=VS&&VS.clip;if(!c||!c.meeting||c.meeting===VS.base)return '';
 const m=byBase(c.meeting);return m?m.title:'';
}
let VCLIP=null;
const fmtTalk=s=>{s=Math.round(+s||0);return s<60?s+'s':Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s';};
function vsPlay(){
 const c=VS&&VS.clip;if(!c)return;
 if(c.span){mPlaySpan(c.start,c.end);return;}
 vsStopClip();VCLIP=new Audio(c.src);VCLIP.play().catch(()=>{});
}
function vsStopClip(){
 if(VCLIP){try{VCLIP.pause();}catch(e){}VCLIP=null;}
 if(MP&&MP.audio&&MP.spanStop!=null){try{MP.audio.pause();}catch(e){}MP.spanStop=null;mSyncPP();}
}

/* -------------------------------------------------------------- markup ---- */
function vsHTML(){
 if(!VS)return '';
 const close=`<button class="icon-btn" type="button" data-act="vs-close" aria-label="Close">${ic('close')}</button>`;
 if(VS.done){
  const nx=vsNextMeeting();
  const said=VS.skipped?`${VS.saved} named, ${VS.skipped} skipped`:`${plural(VS.saved,'voice')} named`;
  return `<div class="veil" data-act="vs-close"></div><div class="sheet" role="dialog" aria-modal="true" aria-label="Name voices">
   <div class="sheet-head"><strong class="sheet-title">All set</strong>${close}</div>
   <div class="sheet-body"><div class="empty"><span class="mark ok">✓</span><strong>${said}${VS.dropped?`, ${VS.dropped} not real`:''}</strong><span>Names reach the transcript and the other meetings in the background.</span></div></div>
   <div class="sheet-foot">${nx?`<button class="btn primary" type="button" data-act="vs-nextm" data-base="${esc(nx.base)}">Next meeting: ${esc(nx.title)}</button>`:''}<button class="btn plain" type="button" data-act="vs-close">Done</button></div></div>`;
 }
 const v=vsCur();const n=VS.list.length;const m=VS.base?byBase(VS.base):null;
 const dots=VS.single?'':`<div class="progress-dots" aria-hidden="true">${VS.list.map((x,k)=>`<i class="${k===VS.i?'c':(x.st==='named'||x.st==='gone')?'d':''}"></i>`).join('')}</div>`;
 const names=((S&&S.enrolled)||[]).map(e=>`<option value="${esc(e.name)}">`).join('');
 const chips=(VS.looks||[]).map((x,k)=>`<button class="nchip" type="button" data-act="vs-fill" data-k="${k}" title="Fill the name box (${k+1}). Nothing is saved until you press Save">Looks like ${esc(x.name)} · <span class="mono">${(+x.score).toFixed(2)}</span></button>`).join('');
 const busy=VBUSY;
 const W=VS.warn;
 const confirm=W?`<div class="confirm" id="vs-confirm"><span>${esc(W.text)}</span><span class="tools"><button class="btn plain" type="button" data-act="vs-unconfirm" ${busy?'disabled':''}>Cancel</button><button class="btn primary" type="button" data-act="${W.kind==='floor'?'vs-local':'vs-force'}" ${busy?'disabled':''}>${W.kind==='floor'?'Name for this meeting':'Save anyway'}</button></span></div>`:'';
 const clip=VS.clip?`<div class="clip"><button class="playbtn q" type="button" data-act="vs-play" aria-label="Play their longest turn">${ic('play')}</button><div style="min-width:0"><div style="font-weight:500">Longest turn · <span class="mono">${fmtT(VS.clip.start)}</span></div><div class="meta">${VS.talk?fmtTalk(VS.talk)+' of talk':''}${vsClipWhere()?(VS.talk?' · ':'')+esc(vsClipWhere()):''}</div></div></div>`
  :VS.clipNote?`<div class="meta">${esc(VS.clipNote)}</div>`:'';
 const lines=VS.lines==null?'':VS.lines.length
  ?`<div class="samples"><div class="eyebrow">What this voice said</div>${VS.lines.slice(0,3).sort((a,b)=>a.start-b.start).map(l=>`<div class="sample"><span class="mono sub">${fmtT(l.start)}</span><span>${esc(l.text)}</span></div>`).join('')}</div>`
  :'<div class="meta">No transcript text was attributed to this voice.</div>';
 const known=(VS.named&&VS.named.length)||VS.brief?`<div class="meta">${VS.named&&VS.named.length?`Already recognized: ${esc(VS.named.join(', '))}. `:''}${VS.brief?`${plural(VS.brief,'brief voice')} under a minute ${VS.brief===1?'is':'are'} hidden.`:''}</div>`:'';
 const drop=VS.local
  ?`<button class="btn plain" type="button" data-act="vs-clear" ${busy?'disabled':''} title="Remove the meeting-only name. The voice goes back to unnamed here">Clear name</button>`
  :`<button class="btn plain" type="button" data-act="vs-drop" ${busy?'disabled':''} title="Grumbling, music, an echo: stop asking about this voice">Not a real speaker</button>`;
 return `<div class="veil" data-act="vs-close"></div><div class="sheet" role="dialog" aria-modal="true" aria-label="Name voices">
  <div class="sheet-head"><div style="min-width:0;display:grid;gap:2px"><strong class="sheet-title">Who is ${esc(v.display)}?</strong><div class="meta">${m?esc(m.title)+(VS.single?'':' · '):''}${VS.single?'':`voice ${VS.i+1} of ${n}`}</div>${dots}</div>${close}</div>
  <div class="sheet-body">
   <div><input class="name-input" type="text" id="vs-in" list="vs-list" placeholder="Type a name" aria-label="Name for ${esc(v.display)}" autocomplete="off" value="${esc(VS.value)}"><datalist id="vs-list">${names}</datalist>
    <div class="meta" id="vs-preview">${esc(VS.preview||'')}</div>${confirm}${VS.err?`<div class="err" id="vs-err">${esc(VS.err)}</div>`:''}</div>
   ${chips?`<div class="chips">${chips}</div>`:''}${clip}${lines}${known}
  </div>
  <div class="sheet-foot"><button class="btn primary" type="button" data-act="vs-save" ${busy?'disabled':''}>${busy?'Saving…':VS.single?'Save':'Save and next'}</button>${VS.single?'':`<button class="btn plain" type="button" data-act="vs-skip" ${busy?'disabled':''}>Skip</button>`}${drop}</div></div>`;
}
function vsRender(focus){
 const s=$('#sheet');if(!s)return;
 if(!VS){s.innerHTML='';return;}
 const a=document.activeElement;const had=a&&a.id==='vs-in';const pos=had?a.selectionStart:null;
 s.innerHTML=vsHTML();
 const i=document.getElementById('vs-in');
 if(i&&(focus||had)){i.focus();try{if(pos!=null)i.setSelectionRange(pos,pos);}catch(e){}}
 if(VS&&VS.done&&focus){const b=s.querySelector('.sheet-foot .btn');if(b&&b.focus)b.focus();}
}

/* ---------------------------------------------------------------- walk ---- */
// the next voice still open after this one. Past the last: the end state,
// or the sheet closes when it held one voice.
function vsNext(){
 if(!VS)return;vsStopClip();
 let j=-1;for(let k=VS.i+1;k<VS.list.length;k++)if(!VS.list[k].st){j=k;break;}
 if(j>=0){Object.assign(VS,{i:j,value:'',err:'',warn:null,preview:null,looks:[],lines:null,clip:null,clipNote:'',talk:0});vsRender(true);vsFetch();return;}
 if(VS.single){vsClose();return;}
 VS.done=true;vsRender(true);
}
// the next meeting with a voice to name, in Needs you order first, never one
// this run already walked
function vsNextMeeting(){
 const seen=new Set(VRUN.seen);if(VS&&VS.base)seen.add(VS.base);
 const asks=needsYou().filter(x=>x.kind==='voice').map(x=>x.target);
 const pool=[...asks,...meetings().filter(m=>Object.keys(m.ask_clusters||{}).length&&arrivedDays(m)<=NEEDS_DAYS).map(m=>m.base)];
 const b=pool.find(x=>!seen.has(x)&&byBase(x)&&Object.keys(byBase(x).ask_clusters||{}).length);
 return b?byBase(b):null;
}
function vsHandOff(base){
 const nx=base?byBase(base):vsNextMeeting();if(!nx)return false;
 VRUN.seen.push(nx.base);VCARRY={base:nx.base};vsClose();
 if(MP&&MP.base===nx.base&&MP.loaded){VCARRY=null;vsOpenMeeting({queue:true});}
 else location.hash='m/'+encodeURIComponent(nx.base);
 return true;
}
function vsSkip(){
 const v=vsCur();if(!v||VBUSY)return;
 v.st='skip';VS.skipped++;
 if(v.uid&&!VRUN.passed.includes(v.uid))VRUN.passed.push(v.uid);
 vsNext();
}

/* ---------------------------------------------------------------- save ---- */
// the after-save sentence, from the response's own fields. "here" only when
// the server says it APPLIED the meeting the save was made from.
function namedToast(r,here){
 if(!r||!r.targets)return;
 const t=r.targets,n=t.length,done=!!here&&r.applied===here;
 const rest=t.includes('--all')?'Updating all meetings.':n?`Updating ${n} ${done?'other ':''}meeting${n===1?'':'s'}.`:'';
 toast((done?'Named here.':'Named.')+(rest?' '+rest:''));
}
function vsBusy(on){VBUSY=!!on;if(VS)vsRender();}
async function vsSave(force,local){
 const v=vsCur();if(!v||VBUSY)return;
 const i=document.getElementById('vs-in');
 const n=((i&&i.value!=null)?i.value:VS.value||'').trim();VS.value=n;
 if(!n){VS.err='Type a name first.';vsRender(true);return;}
 // captured before the await: the sheet can close, move on, or reopen for
 // another voice while the request is in flight
 const tok=vsToken(),uid=v.uid,meeting=VS.base||'',ids=v.ids||(v.id?[v.id]:[]);
 const here=uid?((MP&&MP.base)||meeting||''):meeting;
 const body=uid?{uid,name:n,confirm:!!force}:{meeting,speaker:v.id,name:n,confirm:!!force,local:!!local};
 if(uid&&here)body.meeting=here;
 if(!uid&&local&&ids.length>1)body.speakers=ids.slice();
 VS.err='';VS.warn=null;vsBusy(true);
 let r=null;
 try{r=await api('/api/name',body);}catch(e){r=null;}
 finally{vsBusy(false);}
 const still=vsToken()===tok;
 if(!r){if(still){VS.err='Could not reach the panel.';vsRender();}return;}
 if(!r.ok){
  if(!still)return;
  // the quality floor refused to LEARN the voice: the transcript is still
  // the reader's to label, so the meeting-only save is offered
  if(r.floor)VS.warn={kind:'floor',text:`${r.error||'Too little speech to learn this voice.'} You can still name them in this meeting only. The panel does not learn this voice.`};
  else if(r.warn)VS.warn={kind:'warn',text:`This voice matches ${n}'s saved samples at only ${(+r.own).toFixed(2)}${r.cross_name&&+r.cross>+r.own?`, and matches ${r.cross_name} better (${(+r.cross).toFixed(2)})`:''}. It may be the wrong person.`};
  else VS.err=r.error||'Could not save the name.';
  vsRender();return;
 }
 namedToast(r,here);
 v.st='named';if(uid&&!VRUN.resolved.includes(uid))VRUN.resolved.push(uid);
 refresh();
 // the open transcript rebuilds now, so its "?" stops asking
 if(here&&mOpenHere(here))mReload();
 if(still&&VS){VS.saved++;vsNext();}
}
// "Not a real speaker": /api/forget for a registry voice, then the meeting's
// own dismissal so this meeting stops counting it; /api/dismiss_voice alone
// for a cluster the registry never tracked. Every cluster of a split voice,
// in ONE request: the server dismisses all or none.
async function vsDrop(){
 const v=vsCur();if(!v||VBUSY)return;
 const tok=vsToken(),base=VS.base,ids=v.ids||(v.id?[v.id]:[]);
 const body=base&&ids.length?{base,speaker:ids[0]}:null;if(body&&ids.length>1)body.speakers=ids.slice();
 vsBusy(true);let r=null;
 try{
  r=v.uid?await api('/api/forget',{uid:v.uid}):body?await api('/api/dismiss_voice',body):{error:'This voice has no meeting to dismiss it from.'};
  if(r&&r.ok&&v.uid&&body)await api('/api/dismiss_voice',body);
 }catch(e){r=r&&r.ok?r:null;}
 finally{vsBusy(false);}
 const still=vsToken()===tok;
 if(!r||!r.ok){if(still){VS.err=(r&&r.error)||'Could not remove this voice.';vsRender();}return;}
 v.st='gone';if(v.uid&&!VRUN.resolved.includes(v.uid))VRUN.resolved.push(v.uid);
 refresh();if(base&&mOpenHere(base))mReload();
 if(still&&VS){VS.dropped++;vsNext();}
}
// the documented undo for a meeting-only name: an EMPTY name in the local
// branch drops the entry, the whole voice at once
async function vsClear(){
 const v=vsCur();if(!v||VBUSY||!VS.base)return;
 const tok=vsToken(),base=VS.base,ids=v.ids||[v.id];
 const body={meeting:base,speaker:ids[0],name:'',local:true};if(ids.length>1)body.speakers=ids.slice();
 vsBusy(true);let r=null;
 try{r=await api('/api/name',body);}catch(e){r=null;}finally{vsBusy(false);}
 const still=vsToken()===tok;
 if(!r||!r.ok){if(still){VS.err=(r&&r.error)||'Could not clear this name.';vsRender();}return;}
 refresh();if(mOpenHere(base))mReload();
 if(still)vsClose();
}

/* ------------------------------------------------------------- actions ---- */
Object.assign(A,{
 'vs-close'(){vsClose();},
 'vs-save'(){vsSave();},
 'vs-force'(){vsSave(true);},
 'vs-local'(){vsSave(false,true);},
 'vs-unconfirm'(){if(VS){VS.warn=null;vsRender(true);}},
 'vs-skip'(){vsSkip();},
 'vs-drop'(){vsDrop();},
 'vs-clear'(){vsClear();},
 'vs-play'(){vsPlay();},
 'vs-fill'(el){vsFill(+el.dataset.k);},
 'vs-nextm'(el){vsHandOff(el.dataset.base);}
});
// a chip fills the box and leaves the save to the reader: it is a guess the
// margin rule already refused to act on by itself
function vsFill(k){
 const x=VS&&(VS.looks||[])[k];if(!x)return false;
 VS.value=x.name;VS.warn=null;vsRender(true);return true;
}
// the sheet owns every key while it is open. Escape closes it. Enter in the
// box saves. Outside the box: 1 2 3 fill from a chip, s skips, Space replays.
function vsKey(e){
 if(!VS)return false;
 const t=e.target,typing=t&&t.matches&&t.matches('input,textarea,select');
 if(e.key==='Escape'){e.preventDefault();if(VS.warn){VS.warn=null;vsRender(true);}else vsClose();return true;}
 // Enter never confirms a warning: Save anyway and the meeting-only save
 // stay one deliberate click each
 if(t&&t.id==='vs-in'&&e.key==='Enter'){e.preventDefault();VS.value=t.value;if(!VS.warn)vsSave();return true;}
 // no palette over the sheet: Cmd K waits until the sheet closes
 if((e.metaKey||e.ctrlKey)&&String(e.key).toLowerCase()==='k'){e.preventDefault();return true;}
 if(typing||e.metaKey||e.ctrlKey||e.altKey)return false;
 if(VS.done)return true;   // a focused button still takes its own Enter and Space
 if(e.key==='1'||e.key==='2'||e.key==='3'){if(vsFill(+e.key-1))e.preventDefault();return true;}
 if(e.key==='s'&&!VS.single){e.preventDefault();vsSkip();return true;}
 if(e.key===' '&&!(t&&t.tagName==='BUTTON')){e.preventDefault();vsPlay();return true;}
 if(e.key==='Enter'&&!(t&&t.tagName==='BUTTON')){e.preventDefault();vsSave();return true;}
 return true;    // an open layer owns the plain keys
}
document.addEventListener('input',e=>{if(VS&&e.target&&e.target.id==='vs-in'){VS.value=e.target.value;if(VS.warn&&VS.warn.kind==='warn'){VS.warn=null;vsRender();}}});
