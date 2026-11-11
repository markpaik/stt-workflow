/* ============================================================================
   The meeting page (plan Phase 4D). Top to bottom: back link, the title with
   its Draft title chip and Keep, previous and next, the row menu; one meta
   line; the category, date source, and location note; one notice line when
   voices are open; the sticky tab row (Summary, Transcript, Ask); the tab
   panel, with a People rail at 1200px and wider; the audio bar docked at the
   bottom. The title is the header chip, the voices are the notice and the
   voice sheet (next-voice.js), and the flags are the Transcript tab's
   Uncertain lines filter and its Check.

   The page is built once per meeting. The 2s poll repaints the header, the
   notice, the People rail, and the Summary tab in place when their markup
   changes, and never the transcript: a card, a find, or a typed question
   survives every poll. A finished relabel reloads the lines, but never
   while a card, the voice sheet, a find, or a title edit is open.

   Every write is an existing endpoint, with the server's payload contract:
   /api/review with the segment's ORIGINAL json index plus its start as the
   server's cross-check, /api/accept_meeting for a Keep or an edited title,
   /api/suggest (POST) for Generate, /api/ask for a question.
   ============================================================================ */

let MP=null;               // the open meeting page
const ASK={};              // Ask threads, per meeting, in memory until reload
const MRATES=[1,1.25,1.5,2,0.75];
const MTABS=[['summary','Summary'],['transcript','Transcript'],['ask','Ask']];

// #m/<base> and #m/<base>/t/<secs>: a meeting, or a meeting at a second
function parseMeetingHash(h){
 const x=/^m\/([^/]+)(?:\/t\/(\d+(?:\.\d+)?))?$/.exec(h||'');if(!x)return null;
 let base;try{base=decodeURIComponent(x[1]);}catch(e){return null;}
 return {base,seek:x[2]!=null?+x[2]:null};
}
const mRow=()=>MP?byBase(MP.base):null;
const mOpenHere=base=>!!(MP&&MP.base===base&&UI.route==='meeting');

/* -------------------------------------------------------------- voices ---- */
// a voice the unknown registry tracks, heard in this meeting. A hidden
// (archived) unknown keeps its chip: hiding stops the nagging, not the naming.
function mUnknownUid(display,base){
 const u=((S&&S.unknowns)||[]).find(u=>u.display===display&&(u.meetings||[]).includes(base));
 return u?u.uid:null;
}
// one entry per VOICE, never per cluster: the diarizer can split one person
// into two clusters that share a uid (both "Speaker 68"). Clusters collapse
// by uid, or with no uid by display, and only inside one kind, so a
// meeting-only "Ana" never merges with an enrolled "Ana" and a dismissed
// cluster keeps its own undo. `ids` lists every cluster, the first first.
function legendVoices(opts,base){
 const out=[],by=new Map();
 for(const o of (opts||[])){
  const uid=o.named?null:mUnknownUid(o.display,base);
  const kind=uid?'uid':(!o.named&&!o.dismissed&&o.id!==o.display)?'cluster'
   :(o.named&&o.local)?'local':(!o.named&&o.dismissed)?'dismissed':'plain';
  const k=kind+'\x00'+(uid?'u\x00'+uid:'d\x00'+o.display);
  if(by.has(k)){by.get(k).ids.push(o.id);continue;}
  const v=Object.assign({},o,{uid,kind,ids:[o.id],st:''});by.set(k,v);out.push(v);
 }
 return out;
}
// the voices that still ask for a name: unnamed, not dismissed, and not a
// bare diarizer id with no display of its own
const openVoices=voices=>(voices||[]).filter(v=>v.kind==='uid'||v.kind==='cluster');
// the walk's order inside one meeting: the most talk time first, and never a
// voice this run already named or dropped. A voice this run skipped in another
// meeting stays open here but arrives already skipped.
function voiceOrder(voices,segs,resolved,passed){
 const done=new Set(resolved||[]),again=new Set(passed||[]),talk={};
 for(const g of segs||[])talk[g.speaker]=(talk[g.speaker]||0)+Math.max(0,(g.end||0)-(g.start||0));
 const t=v=>(v.ids||[v.id]).reduce((a,id)=>a+(talk[id]||0),0);
 return (voices||[]).filter(v=>!(v.uid&&done.has(v.uid)))
  .map(v=>Object.assign({},v,{st:v.uid&&again.has(v.uid)?'skip':'',talk:t(v)}))
  .sort((a,b)=>b.talk-a.talk);
}
// every cluster of the voice `speaker` belongs to on the open page
function mVoiceOf(speaker){return MP&&(MP.voices||[]).find(v=>v.ids.includes(speaker))||null;}
function mVoiceIds(speaker){const v=mVoiceOf(speaker);return v?v.ids.slice():[speaker];}

/* ------------------------------------------------------------- header ---- */
// the whole label per source: 'Date from typed in the name' and 'Date from
// set by hand' read badly as 'Date from ' + a phrase
const DATE_FROM={typed:'Date from the name',recorder:'Date from the recorder',short_typed:'Date from a short date in the name',
 mtime:'Date from the file time',manual:'Date set by hand',voice_memo:'Date from Voice Memos'};
function audioNoteText(mins){return mins?`The audio is ${plural(mins,'minute')} shorter than the transcript: audio may be lost.`:'';}
function mPeopleBits(m){
 const named=m.named||[];let s=named.slice(0,3).join(', ');if(named.length>3)s+=` +${named.length-3}`;
 const bits=[esc(whenText(m)),durText(m)];if(s)bits.push(esc(s));
 // the unnamed count keys by display, so a split voice counts once
 const u=Object.keys(m.unnamed_clusters||{}).length;if(u)bits.push(`<span class="attn-t nb">${u} unnamed</span>`);
 return bits;
}
function mHeadHTML(m){
 const title=MP.editTitle
  ?`<input class="m-title-input" type="text" id="m-title-in" value="${esc(MP.titleDraft!=null?MP.titleDraft:m.title)}" aria-label="Title">
    <button class="btn primary" type="button" data-act="m-title-save">Save</button><button class="btn plain" type="button" data-act="m-title-cancel">Cancel</button>`
  // the page's one h1, as every other route has: the title button inside it
  :`<h1 class="m-h1"><button class="m-title" type="button" data-act="m-title" title="Rename">${esc(m.title)}</button></h1>`;
 const draft=!MP.editTitle&&isDraft(m)
  ?`<span class="chip ${m.title_state==='needs_title'?'attn':'draft'}" title="${m.title_state==='needs_title'?'Only the model drafted this title':'An automatic title. Keep it or edit it.'}">Draft title</span><button class="btn quiet" type="button" data-act="m-keep">Keep</button>`:'';
 const cat=m.category==='work'?'Work':m.category==='personal'?'Personal':'';
 const sub=[`<button class="chip catbtn" type="button" data-act="m-cat" title="Change the tag">${cat||'No tag'}</button>`];
 if(m.date_source){
  if(dateUncertain(m))sub.push('<span class="chip attn" title="This date may be the transfer day. Change it from the menu if it is wrong">Date uncertain</span>');
  sub.push(`<span>${esc(DATE_FROM[m.date_source]||'Date from '+m.date_source)}</span>`);
 }
 if(m.location_note)sub.push(`<span title="The place Voice Memos named">Recorded at ${esc(m.location_note)}</span>`);
 if(m.strict)sub.push('<span>Strict: stays on this Mac</span>');
 if(MP.audioShort)sub.push(`<span class="attn-t">${esc(audioNoteText(MP.audioShort))}</span>`);
 return `<a class="btn quiet back h-desk" href="#recordings">${ic('back')}Recordings</a>
  <div class="m-title-row">${title}${draft}<span class="grow"></span>
   <span class="m-nav"><button class="icon-btn" type="button" data-act="m-prev" aria-label="Previous recording" title="Previous recording ([)">${ic('back')}</button><button class="icon-btn" type="button" data-act="m-next" aria-label="Next recording" title="Next recording (])">${ic('next')}</button><button class="icon-btn" type="button" data-act="row-menu" data-base="${esc(m.base)}" aria-label="More actions for ${esc(m.title)}">${ic('more')}</button></span></div>
  ${errLine('m-title')}<div class="meta">${mPeopleBits(m).join(' · ')}</div>
  <div class="meta m-sub">${sub.join('')}</div>`;
}
// one notice line, only when voices are open
function mNoticeHTML(m){
 if(m.no_speech)return '';
 const ask=Object.keys(m.ask_clusters||{}).length,brief=m.short_voices||0;
 if(!ask)return '';
 return `<div class="notice"><span><strong class="attn-t">${ask} ${ask===1?'voice':'voices'} unnamed.</strong> <span class="sub">${brief?`${plural(brief,'brief voice')} under a minute ${brief===1?'is':'are'} hidden.`:'Name a voice once and every meeting it is in updates.'}</span></span><button class="btn primary" type="button" data-act="voices">Name voices</button></div>`;
}
function mSideHTML(m){
 const vs=MP.voices||[];
 if(!MP.loaded)return '<div class="meta">Loading voices…</div>';
 const rows=vs.filter(v=>v.kind!=='dismissed').map(v=>v.kind==='uid'||v.kind==='cluster'
  ?`<div class="person"><button type="button" data-act="m-voice" data-id="${esc(v.id)}">${esc(v.display)}</button></div>`
  :`<div class="person"><span>${esc(v.display)}</span></div>`).join('');
 const brief=m.short_voices||0;
 return `<div class="eyebrow">People</div>${rows||'<div class="meta">No voices found.</div>'}${brief?`<div class="meta">${plural(brief,'brief voice')} under a minute</div>`:''}`;
}

/* ------------------------------------------------------------ summary ---- */
function mSummaryHTML(m){
 if(m.no_speech)return `<div class="empty"><strong>No speech found</strong><span>This recording is silent, so it has no summary. The assistant does not write one from an empty transcript.</span></div>`;
 const g=MP.gen||{};const has=(m.summary||'').trim();
 const where=S.llm_backend==='local'||m.strict?'Runs on this Mac. Nothing leaves it.':'Uses your cloud assistant.';
 const err=g.err?`<div class="err">${esc(g.err)}</div>`:'';
 if(!has)return `<div class="empty"><strong>No summary yet</strong><span>${where}</span><button class="btn primary" type="button" data-act="m-gen" ${g.busy?'disabled':''}>${g.busy?'Writing…':'Generate'}</button>${err}</div>`;
 const sec=(t,xs)=>xs.length?`<section class="reading"><div class="eyebrow">${t} <span class="n">${xs.length}</span></div><ul class="steps">${xs.map(s=>`<li>${esc(s)}</li>`).join('')}</ul></section>`:'';
 const stale=m.summary_stale&&!g.busy?'<div class="meta">This summary is from before the last Redo. It refreshes by itself when the batch is idle, or press Regenerate.</div>':'';
 const busy=g.busy?'<div class="meta">Writing a new summary. This one stays until the new one is ready.</div>':'';
 // a description with a gist (stt/summarize.py) reads as the gist, then
 // Decisions and Open questions, then the tags. One written before it is
 // its summary paragraphs. Next steps are not shown (removed 2026-11-05).
 if((m.gist||'').trim()){
  const tags=(m.tags||[]).length?`<div class="tags" aria-label="Topics">${m.tags.map(t=>`<button class="chip tag" type="button" data-act="m-tag" data-tag="${esc(t)}" title="Show recordings tagged ${esc(t)}">${esc(t)}</button>`).join('')}</div>`:'';
  return `${busy}${stale}${err}<p class="gist reading">${esc(m.gist)}</p>
   ${sec('Decisions',m.decisions||[])}${sec('Open questions',m.open_questions||[])}${tags}`;
 }
 return `${busy}${stale}${err}
  <div class="prose reading">${has.split(/\n{2,}/).map(p=>`<p>${esc(p)}</p>`).join('')}</div>`;
}
function mTabToolHTML(m){
 if(MP.tab==='summary'&&!m.no_speech&&(m.summary||'').trim())
  return `<button class="btn quiet" type="button" data-act="m-gen" ${MP.gen&&MP.gen.busy?'disabled':''}>${MP.gen&&MP.gen.busy?'Writing…':'Regenerate'}</button>`;
 return '';
}

/* --------------------------------------------------------- transcript ---- */
function mIsMinor(g){return (g.end-g.start)<1.0&&String(g.text||'').split(/\s+/).filter(Boolean).length<=3;}
const mFlagged=()=>((MP&&MP.segs)||[]).filter(g=>g.flags&&g.flags.length);
// the flag codes in words. An unknown code shows as itself.
const FLAG_WORDS={overlap:'Voices overlap here',disagreement:'The second pass heard this differently',
 'possible:engine_disagreement':'The second pass heard this differently',id_mismatch:'The speaker may be wrong',
 name_mismatch:'The speaker may be wrong',short_low_confidence:'A short, quiet stretch',duration_mismatch:'The timing looks off'};
function mFlagWhy(g){const f=[...new Set((g.flags||[]).map(x=>FLAG_WORDS[x]||x))];return f.length?f.join('. ')+'.':'Uncertain.';}
// the text with the find marks: match in the raw text, escape each slice
function mMarked(text,i){
 const F=MP.findState;const t=String(text||'');
 if(!F||!F.rx)return esc(t);
 let out='',last=0,k=0;
 t.replace(F.rx,(mm,off)=>{const n=F.byLine[i]?F.byLine[i][k++]:-1;
  out+=esc(t.slice(last,off))+`<mark${n===F.cur?' class="cur"':''}>${esc(mm)}</mark>`;last=off+mm.length;return mm;});
 return out+esc(t.slice(last));
}
function mLineHTML(g,i){
 const fl=g.flags&&g.flags.length;
 const v=mVoiceOf(g.speaker);const un=v&&(v.kind==='uid'||v.kind==='cluster');
 const ts=MP.hasAudio?`<button class="ts" type="button" data-act="m-seek" data-t="${+g.start||0}" title="Play from here">${fmtT(g.start)}</button>`:`<span class="ts">${fmtT(g.start)}</span>`;
 // never the raw diarizer id: `who` is the server's display label
 const who=un?`<button class="who un" type="button" data-act="m-voice" data-id="${esc(g.speaker)}" title="Who is this?">${esc(g.who)}</button>`
  :`<span class="who">${esc(g.who)}</span>`;
 return `<div class="line${fl?' flag':''}" id="ln-${i}" data-i="${i}">${ts}${who}<span class="tx">${mMarked(g.text,i)}${g.edited?' <span class="sub" title="Edited by you">(edited)</span>':''}</span>
  <button class="icon-btn ed" type="button" data-act="m-edit" data-i="${i}" aria-label="Edit this line" title="Fix the speaker or the text">${ic('edit')}</button>
  ${fl?`<span class="flagnote">${esc(mFlagWhy(g))} <button class="ts lnk" type="button" data-act="m-check" data-i="${i}">Check</button></span>`:''}</div>`;
}
function mLinesHTML(){
 if(MP.err)return `<div class="err">${esc(MP.err)}</div>`;
 if(!MP.loaded)return '<div class="none-line sub">Loading the transcript…</div>';
 if(!MP.segs.length)return MP.noSpeech?'<div class="empty"><strong>No speech found</strong><span>This recording is silent.</span></div>':'<div class="empty">This transcript has no lines yet.</div>';
 const rows=MP.segs.map((g,i)=>[g,i]).filter(([g])=>!MP.onlyFlag||(g.flags&&g.flags.length));
 if(!rows.length)return '<div class="empty">No uncertain lines are left.</div>';
 return rows.map(([g,i])=>mLineHTML(g,i)).join('');
}
function mLegendHTML(){
 return (MP.voices||[]).map(v=>{
  if(v.kind==='uid'||v.kind==='cluster')return `<button class="chip attn" type="button" data-act="m-voice" data-id="${esc(v.id)}" title="Who is this? Listen and name this voice">${esc(v.display)}</button>`;
  // named for this meeting only: the chip stays editable, since re-saving is
  // the only way to fix a typo in a name the registry never learned
  if(v.kind==='local')return `<button class="chip" type="button" data-act="m-voice" data-id="${esc(v.id)}" title="Named for this meeting only. Change or clear it">${esc(v.display)}</button>`;
  // dismissed as not a real speaker: the way back
  if(v.kind==='dismissed')return `<button class="chip" type="button" data-act="m-restore" data-id="${esc(v.id)}" title="Dismissed as not a real speaker. Make it nameable again">${esc(v.display)} <span class="sub">(dismissed, undo)</span></button>`;
  return `<span class="chip">${esc(v.display)}</span>`;}).join('');
}
function mFlagChipHTML(){
 const n=mFlagged().length;if(!n&&!MP.onlyFlag)return '';
 return `<button class="chip ${MP.onlyFlag?'on':''}" type="button" data-act="m-onlyflag" aria-pressed="${MP.onlyFlag}" title="Lines the second pass was unsure about. Optional.">Uncertain lines <span class="mono">${n}</span></button>`;
}
function mTranscriptShell(){
 return `<div class="tools"><input type="search" id="m-find" placeholder="Find in transcript" aria-label="Find in transcript" autocomplete="off" spellcheck="false"><span class="meta mono" id="m-findn"></span><span id="m-flagchip"></span></div>
  <div class="chips legend" id="m-legend"></div><div id="m-revbar"></div><div id="m-lines" class="lines"></div>`;
}

/* ---------------------------------------------------------------- ask ---- */
function mAskThread(base){return ASK[base]||(ASK[base]={hist:[],busy:false,note:'',reason:''});}
function mAskHTML(m){
 const t=mAskThread(m.base);
 const noText=m.no_speech||(MP.loaded&&!MP.segs.length);
 const priv=m.strict?'A strict meeting always runs on this Mac.':S.llm_backend==='local'?'Answers come only from this meeting and run on this Mac. The thread is not saved.':'Answers come only from this meeting. Your cloud assistant reads the transcript for this. The thread is not saved.';
 if(noText)return `<div class="ask-note">${esc(priv)}</div><div class="empty"><strong>Nothing to ask about</strong><span>This recording has no transcript text, so Ask has nothing to read.</span></div>`;
 const off=!S.llm_available?'Ask needs the local model installed.':'';
 const starters=['What was decided?','Who owes what?','What is still open?'];
 const thread=t.hist.length?`<div class="thread">${t.hist.map(h=>`<div class="msg q">${esc(h.q)}</div><div class="msg a${h.err?' err':''}">${h.a?esc(h.a):'Reading the transcript…'}</div>`).join('')}</div>`
  :`<div class="chips">${starters.map(s=>`<button class="chip" type="button" data-act="m-starter" data-q="${esc(s)}" ${off||t.busy?'disabled':''}>${esc(s)}</button>`).join('')}</div>`;
 const why=off||t.reason||(t.busy?'Answering. The next question waits for this one.':'');
 return `<div class="ask-note">${esc(priv)}</div>${thread}${t.note?`<div class="meta">${esc(t.note)}</div>`:''}
  <div class="ask-form"><input type="text" id="m-askq" placeholder="Ask about this meeting" aria-label="Ask about this meeting" autocomplete="off" value="${esc(t.draft||'')}"><button class="btn primary" type="button" data-act="m-ask" ${off||t.busy?'disabled':''}>Ask</button></div>
  ${why?`<div class="meta" id="m-askwhy">${esc(why)}</div>`:''}`;
}
async function mAskSend(q){
 if(!MP)return;const base=MP.base,t=mAskThread(base);
 const inp=document.getElementById('m-askq');
 q=(q!=null?q:(inp&&inp.value!=null?inp.value:t.draft||'')).trim();
 if(!q||t.busy||!S.llm_available)return;
 const hist=t.hist.filter(h=>h.a&&!h.err).slice(-3).map(h=>({q:h.q,a:h.a}));
 t.hist.push({q,a:''});t.busy=true;t.reason='';t.note='';t.draft=q;mAskPaint();
 let r;try{r=await api('/api/ask',{base,question:q,history:hist});}catch(e){r={error:'The assistant is unavailable.'};}
 const cur=t.hist[t.hist.length-1];t.busy=false;
 if(r&&r.answer){cur.a=r.answer;t.draft='';t.note=r.truncated?'Long meeting: the middle was sampled, so details from the middle may be missing.':'';}
 else if(r&&/busy/i.test(r.error||'')){
  // the model is busy: no error bubble, the typed question stays in the box
  t.hist.pop();t.reason=r.error;
 }else{cur.a=(r&&r.error)||'No answer came back.';cur.err=true;}
 if(mOpenHere(base))mAskPaint(true);
}
function mAskPaint(focus){
 const box=document.getElementById('tp-ask');const m=mRow();if(!box||!m)return;
 box.innerHTML=mAskHTML(m);
 if(focus){const i=document.getElementById('m-askq');if(i&&i.focus)i.focus();}
}

/* ------------------------------------------------------------- build ---- */
function meetingView(){
 return `<div class="m-head" id="m-head"></div><div id="m-notice"></div>
  <div class="tabbar"><div class="tablist" role="tablist" aria-label="Meeting views">${MTABS.map(([k,l])=>`<button class="tabbtn" role="tab" type="button" id="tab-${k}" aria-controls="tp-${k}" data-act="m-tab" data-k="${k}">${l}</button>`).join('')}</div><span id="m-tabtool"></span></div>
  <div class="m-grid"><div class="m-panels">
   <div class="panel" role="tabpanel" id="tp-summary" aria-labelledby="tab-summary"></div>
   <div class="panel" role="tabpanel" id="tp-transcript" aria-labelledby="tab-transcript">${mTranscriptShell()}</div>
   <div class="panel" role="tabpanel" id="tp-ask" aria-labelledby="tab-ask"></div></div>
  <aside class="m-side" id="m-side" aria-label="People"></aside></div>`;
}
// open or keep the page for UI.mbase. Called by render() on every poll.
function mRender(force){
 const base=UI.mbase;const m=byBase(base);
 if(!MP||MP.base!==base)mOpen(base,UI.mseek);
 const view=$('#view');
 if(!m){
  // renamed, archived, or deleted under us (a rename the page made itself
  // follows the new base before this runs)
  if(MP.built!=='gone'){view.innerHTML=`<div class="empty"><strong>This recording is not in the library.</strong><span>It may have been renamed, archived, or deleted.</span><a class="btn quiet" href="#recordings">Recordings</a></div>`;MP.built='gone';mDock();}
  return;
 }
 if(MP.built!==true){view.innerHTML=meetingView();MP.built=true;MP.sig={};mTabSync();mPaintLines();mDock();force=true;}
 mSync(m,force);
 // the poll's own path only: a reload repaints through mSync, and must
 // never start another reload from there
 mRelabelWatch(m);
}
function mOpen(base,seek){
 mTeardown();
 const m=byBase(base)||{};
 MP={base,built:false,tab:seek!=null?'transcript':'summary',loaded:false,err:null,segs:[],noSpeech:false,spkOpts:[],people:[],voices:[],
  stamp:null,audioShort:null,hasAudio:!!m.audio,audio:null,rate:1,follow:true,nowIdx:null,spanStop:null,seek,
  findState:null,onlyFlag:false,rev:null,gen:null,editTitle:false,titleDraft:null,sig:{},tick:null};
 mLoad(false);
}
function mTeardown(){
 if(!MP)return;
 if(MP.audio){try{MP.audio.pause();}catch(e){}MP.audio.src='';}
 if(MP.tick)clearInterval(MP.tick);
 MP=null;
 const d=$('#dock');if(d)d.innerHTML='';
}
// the parts the poll may repaint, each only when its own markup changed and
// nobody is typing inside it
function mSync(m,force){
 // `hold`: never replace this part while a field inside it has the focus,
 // even on a forced repaint (a typed question survives a reload)
 const put=(id,html,hold)=>{const el=document.getElementById(id);if(!el)return;
  if(!force&&MP.sig[id]===html)return;
  const a=document.activeElement;if((!force||hold)&&a&&el.contains&&el.contains(a)&&a.matches&&a.matches('input,textarea,select'))return;
  el.innerHTML=html;MP.sig[id]=html;};
 put('m-head',mHeadHTML(m));put('m-notice',mNoticeHTML(m));put('m-side',mSideHTML(m));
 put('tp-summary',mSummaryHTML(m));put('m-tabtool',mTabToolHTML(m));
 if(force||!MP.sig.ask){put('tp-ask',mAskHTML(m),true);MP.sig.ask=1;}
 if(force&&MP.editTitle){const i=document.getElementById('m-title-in');if(i&&i.focus)i.focus();}
}
// a relabel rewrote the transcript on screen: reload the lines, unless the
// reader is mid-edit. The stamp is not recorded then, so the change is held,
// not lost: the next 2s tick tries again.
function mRelabelWatch(m){
 if(!MP||!MP.loaded||!m||m.base!==MP.base)return;
 const st=m.generated_at;if(!st||!MP.stamp||st===MP.stamp)return;
 const busy=VS||document.getElementById('mcard')||(MP.findState&&MP.findState.q)||MP.editTitle||(MP.rev&&MP.rev.busy);
 if(busy)return;
 MP.stamp=st;mReload();
}
function mTabSync(){
 for(const [k] of MTABS){
  const b=document.getElementById('tab-'+k),p=document.getElementById('tp-'+k);
  if(b){b.setAttribute('aria-selected',String(MP.tab===k));b.tabIndex=MP.tab===k?0:-1;}
  if(p)p.hidden=MP.tab!==k;
 }
 const m=mRow();const tt=document.getElementById('m-tabtool');if(tt&&m){const h=mTabToolHTML(m);tt.innerHTML=h;MP.sig['m-tabtool']=h;}
}
function mTab(k){if(!MP||!MTABS.some(([x])=>x===k))return;MP.tab=k;mTabSync();}

/* ---------------------------------------------------------- the lines ---- */
async function mLoad(reload){
 const base=MP.base;
 let d;try{d=await api('/api/transcript?base='+encodeURIComponent(base));}catch(e){d={error:'The transcript could not be loaded.'};}
 if(!MP||MP.base!==base)return d;           // left the page while loading
 if(d.error){MP.err=d.error;MP.loaded=true;mPaintLines();return d;}
 MP.err=null;MP.loaded=true;MP.segs=d.segments||[];MP.noSpeech=!!d.no_speech;
 MP.spkOpts=d.speaker_options||[];MP.people=d.people||[];
 MP.engines=d.listen_engines||[];
 if(d.generated_at)MP.stamp=d.generated_at;
 MP.audioShort=d.audio_short_by||null;
 MP.voices=legendVoices(MP.spkOpts,base);
 mPaintLines();
 const m=mRow();if(m&&MP.built===true)mSync(m,true);
 if(!reload&&MP.seek!=null){const t=MP.seek;MP.seek=null;mSeekLine(t);}
 // the voice sheet's hand-off landed on this meeting: open its walk
 if(typeof VCARRY!=='undefined'&&VCARRY&&VCARRY.base===base){const c=VCARRY;VCARRY=null;vsOpenMeeting({queue:true,run:c});}
 return d;
}
function mPaintLines(){
 if(!MP)return;
 const box=document.getElementById('m-lines');if(!box)return;
 box.innerHTML=mLinesHTML();
 const lg=document.getElementById('m-legend');if(lg)lg.innerHTML=mLegendHTML();
 const fc=document.getElementById('m-flagchip');if(fc)fc.innerHTML=mFlagChipHTML();
 mFindCount();mRevBarPaint();
}
// a STRUCTURAL change (insert, split, delete, a merge, a new person, a name):
// the server re-issues segments with fresh ORIGINAL indexes. While stepping,
// the walk re-anchors to the nearest surviving flag by START TIME, held.
async function mReload(target){
 if(!MP)return;
 const base=MP.base,stepping=!!(MP.rev&&MP.rev.active),anchor=stepping&&MP.rev.at?MP.rev.at.start:null;
 const y=window.scrollY||0;
 mCloseCard();
 const q=MP.findState&&MP.findState.q;MP.findState=null;
 const d=await mLoad(true);
 if(!MP||MP.base!==base||!d||d.error)return;
 if(q){const f=document.getElementById('m-find');if(f&&f.value!=null)f.value=q;mFindRun(q,false);}
 if(stepping&&MP.rev){
  let rv;try{rv=await api('/api/review?base='+encodeURIComponent(base));}catch(e){rv=null;}
  if(!MP||MP.base!==base||!MP.rev||!MP.rev.active)return;
  if(!rv||rv.error||!rv.items||!rv.items.length){mRevExit();}
  else{
   if(rv.speakers&&rv.speakers.length)MP.spkOpts=rv.speakers;if(rv.people)MP.people=rv.people;
   const items=revOrder(rv.items);const a=anchor!=null?anchor:items[0].start;
   let bi=items.findIndex(x=>x.start>=a-0.25);if(bi<0)bi=0;
   Object.assign(MP.rev,{items,i:bi,hold:true});
   if(anchor!=null){const p=MP.segs.findIndex(g=>Math.abs(g.start-anchor)<0.25);const el=p>=0?document.getElementById('ln-'+p):null;if(el)el.classList.add('cur');}
   mRevBarPaint();
  }
  try{window.scrollTo(0,y);}catch(e){}
  return;
 }
 if(target!=null){const p=MP.segs.findIndex(g=>g.index===target);const el=p>=0?document.getElementById('ln-'+p):null;if(el&&el.scrollIntoView)el.scrollIntoView({block:'center'});}
}
function mRefreshLine(i){
 const el=document.getElementById('ln-'+i);if(!el||!MP.segs[i])return;
 const keep=['now','cur','editing'].filter(c=>el.classList&&el.classList.contains(c));
 el.outerHTML=mLineHTML(MP.segs[i],i);
 const nw=document.getElementById('ln-'+i);if(nw&&nw.classList)keep.forEach(c=>nw.classList.add(c));
 const fc=document.getElementById('m-flagchip');if(fc)fc.innerHTML=mFlagChipHTML();
}
function mSeekLine(t){
 mTab('transcript');
 const i=MP.segs.findIndex(g=>t>=g.start&&t<g.end);
 const el=i>=0?document.getElementById('ln-'+i):null;
 if(MP.hasAudio)mSeek(t,false);
 if(i>=0)MP.nowIdx=i;
 if(el){el.classList.add('now');if(el.scrollIntoView)el.scrollIntoView({block:'center'});}
}

/* --------------------------------------------------------------- find ---- */
let mFindT=null;
function mFindRun(q,reset){
 const prev=MP.findState;q=(q||'').trim();
 if(q.length<2){MP.findState=null;mRepaintFind(prev);mFindCount();return;}
 const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'ig');
 const hits=[],byLine={};
 MP.segs.forEach((g,i)=>{const mm=String(g.text||'').match(rx);if(mm)for(let k=0;k<mm.length;k++){(byLine[i]=byLine[i]||[]).push(hits.length);hits.push(i);}});
 const cur=hits.length?(reset?0:Math.min(prev&&prev.cur>=0?prev.cur:0,hits.length-1)):-1;
 MP.findState={q,rx,hits,byLine,cur};
 mRepaintFind(prev);mFindCount();if(reset&&cur>=0)mFindShow();
}
// repaint only the lines whose marks changed
function mRepaintFind(prev){
 const ids=new Set([...(prev?prev.hits:[]),...(MP.findState?MP.findState.hits:[])]);
 ids.forEach(i=>mRefreshLine(i));
}
function mFindNav(d){const F=MP.findState;if(!F||!F.hits.length)return;const prev={hits:F.hits.slice()};F.cur=(F.cur+d+F.hits.length)%F.hits.length;mRepaintFind(prev);mFindCount();mFindShow();}
function mFindShow(){const F=MP.findState;const i=F&&F.hits[F.cur];const el=i!=null?document.getElementById('ln-'+i):null;if(el&&el.scrollIntoView)el.scrollIntoView({block:'center'});}
function mFindCount(){const n=document.getElementById('m-findn');if(!n)return;const F=MP.findState;n.textContent=F?(F.hits.length?`${F.cur+1} of ${F.hits.length}`:'No matches.'):'';}
function mFindClear(){const prev=MP.findState;MP.findState=null;const f=document.getElementById('m-find');if(f)f.value='';mRepaintFind(prev);mFindCount();}

/* -------------------------------------------------------------- audio ---- */
function mDock(){
 const d=$('#dock');if(!d)return;
 const m=mRow();
 if(!MP||!m||!m.audio||MP.built!==true){d.innerHTML='';return;}
 d.innerHTML=`<div class="audio" role="group" aria-label="Audio"><button class="playbtn" id="a-pp" type="button" data-act="a-pp" aria-label="Play">${ic('play')}</button>
  <span class="clock mono" id="a-cur">0:00</span><input type="range" class="scrub" id="a-seek" min="0" max="0" step="0.1" value="0" aria-label="Position in the recording"><span class="clock mono" id="a-dur">0:00</span>
  <button class="btn plain" id="a-rate" type="button" data-act="a-rate" title="Playback speed">1x</button>
  <button class="btn plain h-desk" id="a-follow" type="button" data-act="a-follow" aria-pressed="true" title="Scroll the transcript with the playing line">Follow</button>
  <button class="btn plain h-desk" type="button" data-act="m-add" title="Add a line the pipeline missed, at the playhead">Add line</button></div>`;
 // the length is known before the player exists: show it, not 0:00
 const dur=+m.duration_sec||0;
 if(dur>0){const u=document.getElementById('a-dur');if(u)u.textContent=fmtT(dur);const s=document.getElementById('a-seek');if(s)s.max=dur;}
}
function mAudio(){
 if(!MP||!MP.hasAudio)return null;
 if(MP.audio)return MP.audio;
 const a=new Audio('/api/audio?base='+encodeURIComponent(MP.base));a.preload='metadata';MP.audio=a;
 a.ontimeupdate=a.onloadedmetadata=mTick;a.onplay=a.onpause=a.onended=mSyncPP;
 MP.tick=setInterval(mHighlight,300);
 return a;
}
function mTick(){
 const a=MP&&MP.audio;if(!a)return;
 if(MP.spanStop!=null&&a.currentTime>=MP.spanStop){a.pause();MP.spanStop=null;}
 const d=isFinite(a.duration)?a.duration:0;
 const s=document.getElementById('a-seek');if(s&&!s.matches(':active')){s.max=d||0;s.value=a.currentTime||0;}
 const c=document.getElementById('a-cur');if(c)c.textContent=fmtT(a.currentTime);
 const u=document.getElementById('a-dur');if(u)u.textContent=fmtT(d);
 mHighlight();
}
function mSyncPP(){const b=document.getElementById('a-pp');const on=MP&&MP.audio&&!MP.audio.paused;if(b){b.innerHTML=ic(on?'pause':'play');b.setAttribute('aria-label',on?'Pause':'Play');}}
function mPlayPause(){const a=mAudio();if(!a)return;MP.spanStop=null;if(a.paused)a.play().catch(()=>{});else a.pause();mSyncPP();}
function mSeek(t,play){
 const a=mAudio();if(!a)return;MP.spanStop=null;MP.follow=true;mFollowUI();
 const go=()=>{a.currentTime=Math.max(0,+t||0);if(play!==false)a.play().catch(()=>{});mSyncPP();};
 a.readyState>=1?go():a.addEventListener('loadedmetadata',go,{once:true});
}
// span playback: a flag's stretch, a voice's longest turn. Manual control ends it.
function mPlaySpan(start,end){
 const a=mAudio();if(!a)return;MP.spanStop=end+0.7;
 const go=()=>{a.currentTime=Math.max(0,start-0.7);a.play().catch(()=>{});mSyncPP();};
 a.readyState>=1?go():a.addEventListener('loadedmetadata',go,{once:true});
}
function mHighlight(){
 const a=MP&&MP.audio;if(!a||!MP.segs.length)return;
 const t=a.currentTime,i=MP.segs.findIndex(g=>t>=g.start&&t<g.end);
 if(i===MP.nowIdx)return;
 if(MP.nowIdx!=null){const p=document.getElementById('ln-'+MP.nowIdx);if(p)p.classList.remove('now');}
 MP.nowIdx=i;if(i<0)return;
 const el=document.getElementById('ln-'+i);if(!el)return;el.classList.add('now');
 if(MP.follow&&!a.paused&&MP.tab==='transcript'&&el.scrollIntoView){MP.autoScroll=Date.now();el.scrollIntoView({block:'center'});}
}
function mFollowUI(){const b=document.getElementById('a-follow');if(b)b.setAttribute('aria-pressed',String(!!(MP&&MP.follow)));}

/* ------------------------------------------------------- the line card ---- *
 * ONE card at a time, mounted as a sibling after its line, so the line stays
 * in the document. Two flows share it: the flag walk (Check, n, p) with the
 * sticky verdict bar, and the editor (the pencil on any line). Bar verdicts
 * ADVANCE; card work (Save, split, remove, insert) HOLDS the position.
 * Confirms are the house two-step, never a native dialog. */
function spkOptions(speakers,people,sel){
 const seen=new Set((speakers||[]).map(s=>s.display));
 let h=(speakers||[]).map(s=>`<option value="${esc(s.id)}" ${s.id===sel?'selected':''}>${esc(s.display)}</option>`).join('');
 const others=(people||[]).filter(p=>!seen.has(p));
 if(others.length)h+=`<optgroup label="Someone else">${others.map(p=>`<option value="name:${esc(p)}">${esc(p)}</option>`).join('')}</optgroup>`;
 return h+'<option value="__new__">New person…</option>';
}
function mSpkSelect(id,sel){return `<select id="${id}" class="mcsel" aria-label="Who said this">${spkOptions(MP.spkOpts,MP.people,sel)}</select><input type="text" id="${id}-new" class="mnew" placeholder="Who said this?" aria-label="New person name" hidden>`;}
// the speaker a card's select names: "New person" reads its own box
function mSpkValue(id){
 const s=document.getElementById(id);if(!s)return null;
 if(s.value!=='__new__')return s.value;
 const n=document.getElementById(id+'-new');const v=n&&n.value?n.value.trim():'';
 return v?'name:'+v:'__new__';
}
function mCardEl(html,after){
 mCloseCard();
 const d=document.createElement('div');d.id='mcard';d.className='mcard';d.innerHTML=html;
 after.insertAdjacentElement('afterend',d);return d;
}
function mCloseCard(){const d=document.getElementById('mcard');if(d)d.remove();document.querySelectorAll('.line.editing').forEach(e=>e.classList.remove('editing'));}
function mCardErr(msg){const e=document.getElementById('mc-err');if(e){e.hidden=!msg;e.textContent=msg||'';}}

/* the flag walk */
const revOrder=items=>(items||[]).slice().sort((a,b)=>(a.start-b.start)||(a.index-b.index));
function revPos(it){let p=MP.segs.findIndex(g=>g.index===it.index);if(p<0)p=MP.segs.findIndex(g=>Math.abs(g.start-it.start)<0.25);return p;}
async function mRevStart(atIndex){
 if(!MP||!MP.loaded)return;const base=MP.base;
 let d;try{d=await api('/api/review?base='+encodeURIComponent(base));}catch(e){d={error:'The review list could not be loaded.'};}
 if(!MP||MP.base!==base||d.error)return;
 if(!d.items||!d.items.length){MP.rev=null;mRevBarPaint();return;}
 const items=revOrder(d.items);
 let i=0;if(atIndex!=null){const k=items.findIndex(x=>x.index===atIndex);if(k>=0)i=k;}
 MP.rev={items,i,active:true,busy:false,hold:false,at:null,confirmAll:false};
 if(d.speakers&&d.speakers.length)MP.spkOpts=d.speakers;if(d.people)MP.people=d.people;
 mTab('transcript');mRevCard();
}
function mRevGo(d){
 const R=MP&&MP.rev;if(!R||!R.active||R.busy)return;
 if(!R.items.length){mRevExit();return;}
 if(R.hold){R.hold=false;R.i=d<0?(R.i-1+R.items.length)%R.items.length:R.i%R.items.length;}
 else R.i=(R.i+d+R.items.length)%R.items.length;
 mRevCard();
}
function mRevExit(){if(MP){MP.rev=null;}mCloseCard();document.querySelectorAll('.line.cur').forEach(e=>e.classList.remove('cur'));mRevBarPaint();}
function mRevBarHTML(){
 const R=MP.rev;if(!R||!R.active)return '';
 const n=R.items.length,it=R.hold?null:R.items[R.i];const minors=R.items.filter(x=>x.minor).length;
 const alt=!!(it&&it.alt&&it.alt.length);
 return `<div class="revbar" role="toolbar" aria-label="Check the uncertain lines">
  <span class="meta mono">${R.hold?`Saved · ${n} left`:`${R.i+1} of ${n}`}</span>
  <button class="icon-btn" type="button" data-act="rev-go" data-d="-1" aria-label="Previous uncertain line (p)">${ic('back')}</button><button class="icon-btn" type="button" data-act="rev-go" data-d="1" aria-label="Next uncertain line (n)">${ic('next')}</button>
  ${MP.hasAudio?'<button class="btn plain" type="button" data-act="rev-play">Play</button>':''}
  <button class="btn primary" type="button" data-act="rev-accept" title="This line is right: clear the flag and go on (Enter or a)">Accept</button>
  <button class="btn" type="button" data-act="rev-alt" ${alt?'':'disabled'} title="Use the second engine's version and go on (u)">Use alt</button>
  <button class="btn plain" type="button" data-act="rev-go" data-d="1" title="Leave it flagged (n)">Skip</button>
  <button class="btn plain" type="button" data-act="rev-exit" title="Stop. Unresolved lines stay flagged (Esc)">Done</button>
  <span class="grow"></span>
  ${minors?`<button class="btn plain" type="button" data-act="rev-minor" title="Sub-second crumbs: accept them all; substantial lines stay">Accept ${minors} minor</button>`:''}
  ${n?(R.confirmAll?`<span class="meta">Keep all ${n} as transcribed?</span><button class="btn plain" type="button" data-act="rev-all-cancel">Cancel</button><button class="btn danger" type="button" data-act="rev-all-go">Accept all ${n}</button>`
   :`<button class="btn plain" type="button" data-act="rev-all" title="Keep every remaining flagged line exactly as transcribed">Accept all ${n} remaining…</button>`):''}</div>`;
}
function mRevBarPaint(){const b=document.getElementById('m-revbar');if(b&&MP)b.innerHTML=mRevBarHTML();}
function mRevCard(){
 const R=MP.rev,it=R.items[R.i];const p=revPos(it);
 const el=p>=0?document.getElementById('ln-'+p):null;
 if(!el){mRevExit();return;}                      // the transcript changed underneath
 R.hold=false;R.at={index:it.index,start:it.start,end:it.end};
 // the quote closes the sentence when the line already ends in one: a
 // period after it read 'two.”.'
 const alts=(it.alt||[]).map((a,k)=>`<div class="meta">The second engine heard “<b>${esc(a.theirs||'(nothing)')}</b>” where this says “${esc(a.ours||'(nothing)')}”${/[.!?…]["”’)]*$/.test(String(a.ours||'').trim())?'':'.'} <button class="btn quiet" type="button" data-act="rev-usealt" data-k="${k}">Use it</button></div>`).join('');
 mCardEl(`<div class="mc-flag attn-t">${esc(mFlagWhy(it))}${it.minor?' Minor.':''}</div>${alts}
  <div class="mc-row">${mSpkSelect('mc-spk',it.speaker)}</div>
  <textarea id="mc-text" class="mc-text" aria-label="Corrected text">${esc(it.text)}</textarea>
  <div class="mc-btns"><span class="grow"></span><button class="btn primary" type="button" data-act="rev-save" title="Save and stay here (Cmd Enter)">Save changes</button></div>
  <div class="err" id="mc-err" hidden></div>`,el);
 document.querySelectorAll('.line.cur').forEach(e=>e.classList.remove('cur'));el.classList.add('cur');
 if(el.scrollIntoView)el.scrollIntoView({block:'center'});
 if(MP.hasAudio)mPlaySpan(it.start,it.end);
 mRevBarPaint();
}
function mUseAlt(k){
 const it=MP.rev&&MP.rev.items[MP.rev.i];const a=it&&(it.alt||[])[k];const ta=document.getElementById('mc-text');if(!a||!ta)return;
 const pat=String(a.ours||'').trim().split(/\s+/).filter(Boolean).map(t=>t.replace(/[.*+?^$()|[\]\\{}]/g,'\\$&')).join("[^A-Za-z0-9']+");
 const re=pat?new RegExp(pat,'i'):null;
 if(re&&re.test(ta.value))ta.value=ta.value.replace(re,a.theirs);else ta.value=(ta.value+' '+a.theirs).trim();
}
// resolve item k of the walk without moving: R.i comes to name the next one
function mRevDrop(index){
 const R=MP.rev;if(!R)return;const k=R.items.findIndex(x=>x.index===index);if(k<0)return;
 R.items.splice(k,1);if(R.i>k)R.i--;else if(R.i===k){R.hold=true;if(R.i>=R.items.length)R.i=0;}
}
/* a BAR verdict: resolves the current flag and ADVANCES (wrapping) */
async function mRevApply(action){
 const R=MP&&MP.rev;if(!R||!R.active||R.busy)return;
 if(R.hold){mRevGo(1);return;}
 const it=R.items[R.i];if(!it)return;
 const body={base:MP.base,index:it.index,start:it.start,end:it.end,action};
 if(action==='edit'){const v=mSpkValue('mc-spk');if(v==='__new__'){mCardErr('Pick or name the speaker first.');return;}
  const ta=document.getElementById('mc-text');body.text=ta?ta.value:it.text;body.speaker=v;}
 R.busy=true;let r;try{r=await api('/api/review',body);}catch(e){r={error:'Save failed.'};}
 R.busy=false;
 if(!MP||MP.rev!==R)return;
 if(!r.ok){mCardErr(r.error||'Save failed.');return;}
 if(r.merged||(action==='edit'&&String(body.speaker||'').startsWith('name:'))){await mReload(r.index);return;}
 const p=revPos(it);
 if(p>=0){const g=MP.segs[p];if(action==='edit'){g.text=body.text;g.edited=true;const sp=MP.spkOpts.find(s=>s.id===body.speaker);if(sp){g.who=sp.display;g.speaker=sp.id;}}g.flags=[];mRefreshLine(p);}
 R.items.splice(R.i,1);
 if(!R.items.length){mRevExit();refresh();return;}
 if(R.i>=R.items.length)R.i=0;
 mRevCard();
}
/* Save from the card: the card is a workspace, so the position HOLDS */
async function mRevSave(){
 const R=MP&&MP.rev;if(!R||!R.active||R.busy||!R.at)return;
 const at=R.at,v=mSpkValue('mc-spk');if(v==='__new__'){mCardErr('Pick or name the speaker first.');return;}
 const ta=document.getElementById('mc-text');
 const body={base:MP.base,index:at.index,start:at.start,end:at.end,action:'edit',text:ta?ta.value:'',speaker:v};
 R.busy=true;let r;try{r=await api('/api/review',body);}catch(e){r={error:'Save failed.'};}
 R.busy=false;
 if(!MP||MP.rev!==R)return;
 if(!r.ok){mCardErr(r.error||'Save failed.');return;}
 if(r.merged||String(v||'').startsWith('name:')){await mReload(r.merged?r.index:at.index);return;}
 const p=revPos(at);
 if(p>=0){const g=MP.segs[p];g.text=body.text;g.edited=true;g.flags=[];const sp=MP.spkOpts.find(s=>s.id===v);if(sp){g.who=sp.display;g.speaker=sp.id;}
  const card=document.getElementById('mcard');mRefreshLine(p);
  const el=document.getElementById('ln-'+p);if(el){el.classList.add('cur');if(card)el.insertAdjacentElement('afterend',card);}}
 mRevDrop(at.index);
 const f=document.querySelector('#mcard .mc-flag');if(f)f.textContent='Saved. Edit on, or step ahead with the bar.';
 mRevBarPaint();refresh();
}
function mRevUseAltGo(){
 const R=MP&&MP.rev;if(!R||!R.active||R.busy||R.hold)return;
 const it=R.items[R.i];if(!it||!it.alt||!it.alt.length||!document.getElementById('mc-text'))return;
 it.alt.forEach((_,k)=>mUseAlt(k));mRevApply('edit');
}
async function mRevAllGo(){
 const R=MP&&MP.rev;if(!R||!R.active||R.busy)return;
 R.busy=true;R.confirmAll=false;const base=MP.base;
 for(const it of R.items.slice()){
  let r;try{r=await api('/api/review',{base,index:it.index,start:it.start,end:it.end,action:'accept'});}catch(e){r={error:'The panel did not answer.'};}
  if(!MP||MP.base!==base||MP.rev!==R)return;
  if(!r.ok){R.busy=false;mRevBarPaint();mCardErr((r.error||'A line failed to accept.')+' The rest stay flagged.');refresh();return;}
  const p=revPos(it);if(p>=0){MP.segs[p].flags=[];mRefreshLine(p);}
  const k=R.items.indexOf(it);if(k>=0){R.items.splice(k,1);if(R.i>k)R.i--;}
  mRevBarPaint();
 }
 R.busy=false;mRevExit();refresh();
}
async function mRevMinor(){
 if(!MP)return;const base=MP.base;
 const r=await api('/api/review',{base,action:'accept_minor'}).catch(()=>({error:'Could not reach the panel.'}));
 if(!MP||MP.base!==base)return;
 if(!r.ok){toast(r.error||'Could not accept the minor lines.');return;}
 MP.segs.forEach((g,p)=>{if(g.flags&&g.flags.length&&mIsMinor(g)){g.flags=[];mRefreshLine(p);}});
 const R=MP.rev;
 if(R&&R.active){
  const cur=R.hold?null:R.items[R.i];R.items=R.items.filter(x=>!x.minor);
  if(!R.items.length&&!R.hold)mRevExit();
  else if(cur&&!cur.minor){R.i=R.items.indexOf(cur);mRevBarPaint();}   // never rebuild a card mid-edit
  else if(cur){R.i=Math.min(R.i,R.items.length-1);mRevCard();}
  else{R.i=Math.max(0,Math.min(R.i,R.items.length-1));mRevBarPaint();}
 }
 refresh();
}

/* the editor: the pencil on any line */
// the second listen's engines, from the server: the local engines only,
// the default first. Today that is Qwen3-ASR alone.
function mEngines(){return MP&&MP.engines&&MP.engines.length?MP.engines:[{id:'qwen3',label:'Qwen3-ASR',note:'Runs on this Mac.'}];}
// one engine: no menu, a line that says what Listen again does
function mEngPick(){
 const e=mEngines();
 if(e.length>1)return `<select id="mc-eng" class="mcsel" aria-label="Engine for a second listen">${e.map(x=>`<option value="${esc(x.id)}" title="${esc(x.note||'')}">${esc(x.label)}</option>`).join('')}</select>`;
 return `<span class="meta" id="mc-eng-note">${esc(`Listen again runs ${e[0].label} on this line, on this Mac.`)}</span>`;
}
function mEdit(i){
 const g=MP.segs[i],el=document.getElementById('ln-'+i);if(!g||!el)return;
 mCardEl(`<div class="mc-row">${mSpkSelect('mc-spk',g.speaker)}
   ${mEngPick()}
   <button class="btn plain" type="button" data-act="mc-retrans" data-i="${i}">Listen again</button><span class="meta" id="mc-rx"></span></div>
  <textarea id="mc-text" class="mc-text" aria-label="Corrected text">${esc(g.text)}</textarea>
  <div class="mc-btns"><button class="btn plain" type="button" data-act="mc-del" data-i="${i}">Remove line</button><button class="btn plain" type="button" data-act="mc-split" data-i="${i}" title="Put the cursor where the second voice starts">Split line</button>
   <span class="grow"></span>${MP.hasAudio?`<button class="btn plain" type="button" data-act="mc-span" data-i="${i}">Play</button>`:''}
   <button class="btn plain" type="button" data-act="mc-close">Cancel</button><button class="btn primary" type="button" data-act="mc-save" data-i="${i}">Save</button></div>
  <div class="err" id="mc-err" hidden></div>`,el);
 el.classList.add('editing');
 const ta=document.getElementById('mc-text');if(ta&&ta.focus)ta.focus();
}
async function mEditSave(i){
 const g=MP.segs[i];if(!g)return;const spk=mSpkValue('mc-spk');
 if(spk==='__new__'){mCardErr('Pick or name the speaker first.');return;}
 const ta=document.getElementById('mc-text');const text=ta?ta.value:g.text;
 // the ORIGINAL json index rides along, never the array position
 const r=await api('/api/review',{base:MP.base,index:g.index,start:g.start,end:g.end,action:'edit',text,speaker:spk}).catch(()=>({error:'Save failed.'}));
 if(!r.ok){mCardErr(r.error||'Save failed.');return;}
 if(r.merged){mReload(r.index);return;}
 if(spk&&spk.startsWith('name:')){mReload(g.index);return;}
 const sp=MP.spkOpts.find(s=>s.id===spk);g.text=text;g.flags=[];g.edited=true;if(sp){g.who=sp.display;g.speaker=sp.id;}
 mCloseCard();mRefreshLine(i);
 if(MP.rev&&MP.rev.active){mRevDrop(g.index);mRevBarPaint();}
}
function mDeleteAsk(i){
 const e=document.getElementById('mc-err');if(!e)return;e.hidden=false;
 e.innerHTML=`<span>Remove this line? Its audio stays. Only the text line goes.</span> <button class="btn plain" type="button" data-act="mc-errclear">Keep it</button><button class="btn danger" type="button" data-act="mc-delgo" data-i="${i}">Remove line</button>`;
}
async function mDeleteGo(i){
 const g=MP.segs[i];const r=await api('/api/review',{base:MP.base,action:'delete',index:g.index,start:g.start,end:g.end}).catch(()=>({error:'Could not remove the line.'}));
 if(!r.ok){mCardErr(r.error||'Could not remove the line.');return;}
 mReload();
}
function mSplitUI(i){
 const ta=document.getElementById('mc-text'),pos=ta?(ta.selectionStart||0):0,full=ta?ta.value:'';
 const a=full.slice(0,pos).trim(),b=full.slice(pos).trim();
 if(!a||!b){mCardErr('Click inside the text where the second voice starts, then press Split again.');return;}
 const g=MP.segs[i],card=document.getElementById('mcard');if(!card)return;
 card.innerHTML=`<div class="meta">Set each half's speaker. A half that matches its neighbor's speaker joins that turn.</div>
  <div class="mc-row">${mSpkSelect('mc-sa',g.speaker)}</div><textarea id="mc-ta1" class="mc-text" aria-label="First half">${esc(a)}</textarea>
  <div class="mc-row">${mSpkSelect('mc-sb',g.speaker)}</div><textarea id="mc-ta2" class="mc-text" aria-label="Second half">${esc(b)}</textarea>
  <div class="mc-btns"><span class="grow"></span><button class="btn plain" type="button" data-act="mc-close">Cancel</button><button class="btn primary" type="button" data-act="mc-splitgo" data-i="${i}">Split</button></div>
  <div class="err" id="mc-err" hidden></div>`;
}
async function mSplitSave(i){
 const g=MP.segs[i],sa=mSpkValue('mc-sa'),sb=mSpkValue('mc-sb');
 if(sa==='__new__'||sb==='__new__'){mCardErr('Pick or name both speakers first.');return;}
 const r=await api('/api/review',{base:MP.base,action:'split',index:g.index,start:g.start,end:g.end,
  text_a:(document.getElementById('mc-ta1')||{}).value,text_b:(document.getElementById('mc-ta2')||{}).value,speaker_a:sa,speaker_b:sb}).catch(()=>({error:'Split failed.'}));
 if(!r.ok){mCardErr(r.error||'Split failed.');return;}
 mReload(r.index);
}
async function mRetrans(i){
 const g=MP.segs[i];const eng=(document.getElementById('mc-eng')||{}).value||mEngines()[0].id;
 const rx=document.getElementById('mc-rx');if(rx)rx.textContent='Listening again…';
 const base=MP.base;
 const r=await api('/api/retranscribe',{base,start:g.start,end:g.end,engine:eng}).catch(()=>({error:'The second listen failed.'}));
 const rx2=document.getElementById('mc-rx');if(!rx2||!MP||MP.base!==base)return;   // the card closed while waiting
 if(r.error){rx2.textContent=r.error;return;}
 const ta=document.getElementById('mc-text');if(ta)ta.value=r.text;
 rx2.textContent='Proposed by '+(r.engine||'the second engine')+'. Edit if needed, then Save.';
}
/* a line the pipeline missed, at the playhead */
function mParseT(v){const p=String(v).trim().split(':').map(Number);if(!p.length||p.some(isNaN))return null;return p.reverse().reduce((acc,x,k)=>acc+x*Math.pow(60,k),0);}
function mInsertAt(t){
 if(!MP||!MP.loaded)return;mTab('transcript');
 const after=MP.segs.findIndex(g=>g.start>t);const i=after<0?MP.segs.length-1:after-1;
 const el=document.getElementById('ln-'+Math.max(0,i))||document.getElementById('m-lines');if(!el)return;
 mCardEl(`<div class="mc-row">${mSpkSelect('mc-nspk',null)}<label class="meta">At <input type="text" id="mc-nat" value="${fmtT(t)}" aria-label="Time the line starts" size="6"></label></div>
  <textarea id="mc-ntext" class="mc-text" placeholder="What they said" aria-label="What they said"></textarea>
  <div class="mc-btns"><span class="grow"></span><button class="btn plain" type="button" data-act="mc-close">Cancel</button><button class="btn primary" type="button" data-act="mc-insert">Add</button></div>
  <div class="err" id="mc-err" hidden></div>`,el);
 const ta=document.getElementById('mc-ntext');if(ta&&ta.focus)ta.focus();
}
async function mInsertSave(){
 const spk=mSpkValue('mc-nspk'),text=(document.getElementById('mc-ntext')||{}).value||'',start=mParseT((document.getElementById('mc-nat')||{}).value||'');
 if(spk==='__new__'){mCardErr('Pick or name the speaker first.');return;}
 if(start==null){mCardErr('Time should look like 12:34.');return;}
 if(!text.trim()){mCardErr('Type what they said.');return;}
 const r=await api('/api/review',{base:MP.base,action:'insert',start,end:start+3,speaker:spk,text}).catch(()=>({error:'Could not add the line.'}));
 if(!r.ok){mCardErr(r.error||'Could not add the line.');return;}
 mReload(r.index);
}

/* ------------------------------------------------------ header actions ---- */
// a Keep or an edited title on one opened meeting confirms the title, and a
// person did look at it. A re-stamped folder takes the page with it.
async function mAccept(name){
 if(!MP)return;const base=MP.base;
 const r=await api('/api/accept_meeting',{base,name:name||undefined}).catch(()=>({error:'Could not reach the panel.'}));
 if(!MP||MP.base!==base)return;
 if(!r||r.ok===false||r.error){UI.err['m-title']=(r&&r.error)||'That did not work.';mSync(mRow()||{},true);return;}
 delete UI.err['m-title'];MP.editTitle=false;MP.titleDraft=null;
 await refresh();
 if(r.base&&r.base!==base)mFollow(base,r.base);else render(true);
 toast(r.note||(name?'Title saved.':'Title kept.'));
}
// the folder moved (a rename re-stamps it): carry the page state across
function mFollow(oldBase,newBase){
 if(!MP||MP.base!==oldBase)return;
 // the audio URL names the old folder: drop the player, the dock rebuilds
 if(MP.audio){try{MP.audio.pause();}catch(e){}MP.audio=null;}
 if(MP.tick){clearInterval(MP.tick);MP.tick=null;}
 MP.base=newBase;UI.mbase=newBase;MP.built=false;
 const m=byBase(newBase);if(m)MP.hasAudio=!!m.audio;
 if(ASK[oldBase]){ASK[newBase]=ASK[oldBase];delete ASK[oldBase];}
 history.replaceState(null,'','#m/'+encodeURIComponent(newBase));
 render(true);mReload();
}
function mCycleCat(){
 const m=mRow();if(!m)return;const cur=m.category==='work'||m.category==='personal'?m.category:'';
 const next={'':'work',work:'personal',personal:''}[cur];m.category=next||null;mSync(m,true);
 api('/api/set_category',{base:m.base,category:next}).then(()=>refresh().then(()=>render()));
}
async function mGenerate(){
 if(!MP)return;const base=MP.base;MP.gen={busy:true};mSync(mRow()||{},true);
 let r;try{r=await api('/api/suggest',{base});}catch(e){r={error:'The summary service is unavailable.'};}
 if(!MP||MP.base!==base)return;
 MP.gen=r&&r.summary!=null&&!r.error?null:{err:(r&&r.error)||'No summary came back.'};
 await refresh();if(mOpenHere(base))render(true);
}
// previous and next in the Recordings list's current order
function mStep(d){
 const order=recOrder();const k=order.indexOf(UI.mbase);
 if(!order.length)return false;
 const j=k<0?0:k+d;if(j<0||j>=order.length)return false;
 location.hash='m/'+encodeURIComponent(order[j]);return true;
}
function mRestore(id){
 const ids=mVoiceIds(id);const base=MP.base;
 const body={base,speaker:ids[0],restore:true};if(ids.length>1)body.speakers=ids;
 api('/api/dismiss_voice',body).then(r=>{if(r&&r.ok){refresh();if(mOpenHere(base))mReload();}else toast((r&&r.error)||'Could not restore the voice.');});
}

/* ------------------------------------------------------------ actions ---- */
Object.assign(A,{
 'm-tab'(el){mTab(el.dataset.k);},
 'm-title'(){if(!MP)return;MP.editTitle=true;MP.titleDraft=null;mSync(mRow(),true);const i=document.getElementById('m-title-in');if(i&&i.select)i.select();},
 'm-title-cancel'(){if(!MP)return;MP.editTitle=false;MP.titleDraft=null;delete UI.err['m-title'];mSync(mRow(),true);},
 'm-title-save'(){const i=document.getElementById('m-title-in');const v=i?i.value.trim():'';if(!v){UI.err['m-title']='Type a title first.';mSync(mRow(),true);return;}mAccept(v);},
 'm-keep'(){mAccept('');},
 'm-cat'(){mCycleCat();},
 'm-prev'(){mStep(-1);},'m-next'(){mStep(1);},
 'm-gen'(){mGenerate();},
 // a topic tag opens Recordings filtered to that tag
 'm-tag'(el){UI.q=el.dataset.tag||'';UI.chip='all';go('recordings');},
 'm-seek'(el){const t=+el.dataset.t||0;mSeek(t);const i=MP.segs.findIndex(g=>t>=g.start&&t<g.end);if(i>=0){MP.nowIdx=null;mHighlight();}},
 'm-voice'(el){vsOpenVoice(el.dataset.id);},
 'm-restore'(el){mRestore(el.dataset.id);},
 voices(){vsOpenMeeting({});},
 'm-onlyflag'(){MP.onlyFlag=!MP.onlyFlag;mCloseCard();if(MP.rev)mRevExit();mPaintLines();},
 'm-check'(el){const g=MP.segs[+el.dataset.i];if(g)mRevStart(g.index);},
 'm-edit'(el){mEdit(+el.dataset.i);},
 'm-add'(){const a=MP&&MP.audio;mInsertAt(a?a.currentTime:0);},
 'm-ask'(){mAskSend();},
 'm-starter'(el){mAskSend(el.dataset.q);},
 'a-pp'(){mPlayPause();},
 'a-rate'(el){if(!MP)return;MP.rate=MRATES[(MRATES.indexOf(MP.rate)+1)%MRATES.length];const a=mAudio();if(a)a.playbackRate=MP.rate;el.textContent=MP.rate+'x';},
 'a-follow'(){if(!MP)return;MP.follow=!MP.follow;mFollowUI();if(MP.follow){MP.nowIdx=null;mHighlight();}},
 'rev-go'(el){mRevGo(+el.dataset.d||1);},
 'rev-play'(){const it=MP.rev&&MP.rev.items[MP.rev.i];if(it)mPlaySpan(it.start,it.end);},
 'rev-accept'(){mRevApply('accept');},
 'rev-alt'(){mRevUseAltGo();},
 'rev-usealt'(el){mUseAlt(+el.dataset.k);},
 'rev-save'(){mRevSave();},
 'rev-exit'(){mRevExit();},
 'rev-minor'(){mRevMinor();},
 'rev-all'(){if(MP.rev){MP.rev.confirmAll=true;mRevBarPaint();}},
 'rev-all-cancel'(){if(MP.rev){MP.rev.confirmAll=false;mRevBarPaint();}},
 'rev-all-go'(){mRevAllGo();},
 'mc-close'(){mCloseCard();},
 'mc-save'(el){mEditSave(+el.dataset.i);},
 'mc-del'(el){mDeleteAsk(+el.dataset.i);},
 'mc-delgo'(el){mDeleteGo(+el.dataset.i);},
 'mc-errclear'(){mCardErr('');},
 'mc-split'(el){mSplitUI(+el.dataset.i);},
 'mc-splitgo'(el){mSplitSave(+el.dataset.i);},
 'mc-retrans'(el){mRetrans(+el.dataset.i);},
 'mc-span'(el){const g=MP.segs[+el.dataset.i];if(g)mPlaySpan(g.start,g.end);},
 'mc-insert'(){mInsertSave();}
});

/* --------------------------------------------------------------- keys ---- *
 * The meeting page's keys, from the one keydown dispatcher in next.js. The
 * voice sheet, a popover, the palette, and the menu each own every key while
 * they are open, so this never runs under them. Returns true when it took the
 * key. Keys never fire while typing, except Cmd F, Cmd Enter, and the find
 * box's own Enter and Escape. */
function mKey(e){
 if(UI.route!=='meeting'||!MP)return false;
 const t=e.target,typing=t&&t.matches&&t.matches('input,textarea,select');
 const cmd=e.metaKey||e.ctrlKey;
 if(cmd&&e.key==='Enter'&&document.getElementById('mcard')){e.preventDefault();const b=document.querySelector('#mcard .btn.primary');if(b)b.click();return true;}
 if(cmd&&(e.key==='f'||e.key==='F')){e.preventDefault();mTab('transcript');const f=document.getElementById('m-find');if(f){f.focus();if(f.select)f.select();}return true;}
 if(t&&t.id==='m-find'){
  if(e.key==='Enter'){e.preventDefault();clearTimeout(mFindT);const q=t.value.trim();if(!MP.findState||MP.findState.q!==q)mFindRun(q,true);else mFindNav(e.shiftKey?-1:1);return true;}
  if(e.key==='Escape'){e.preventDefault();mFindClear();t.blur();return true;}
  return false;
 }
 if(t&&t.id==='m-title-in'){if(e.key==='Enter'){e.preventDefault();A['m-title-save']();return true;}if(e.key==='Escape'){e.preventDefault();A['m-title-cancel']();return true;}return false;}
 if(t&&t.id==='m-askq'&&e.key==='Enter'){e.preventDefault();mAskSend();return true;}
 if(t&&t.classList&&t.classList.contains('tabbtn')&&(e.key==='ArrowRight'||e.key==='ArrowLeft')){
  e.preventDefault();const k=MTABS.findIndex(([x])=>x===MP.tab);const n=MTABS[(k+(e.key==='ArrowRight'?1:-1)+MTABS.length)%MTABS.length][0];
  mTab(n);const b=document.getElementById('tab-'+n);if(b&&b.focus)b.focus();return true;}
 if(e.key==='Escape'&&!typing){
  if(document.getElementById('mcard')&&!(MP.rev&&MP.rev.active)){e.preventDefault();mCloseCard();return true;}
  if(MP.rev&&MP.rev.active){e.preventDefault();mRevExit();return true;}
  if(MP.editTitle){A['m-title-cancel']();return true;}
  e.preventDefault();go('recordings');return true;
 }
 if(typing||cmd||e.altKey)return false;
 const R=MP.rev&&MP.rev.active?MP.rev:null;
 if(R&&(e.key==='Enter'||e.key==='a'||e.key==='u')){
  // a focused button keeps its own Enter, so the two-step confirms stay reachable
  if(e.key==='Enter'&&t&&t.tagName==='BUTTON')return false;
  e.preventDefault();if(e.key==='u'){const it=R.hold?null:R.items[R.i];if(it&&it.alt&&it.alt.length)mRevUseAltGo();}else mRevApply('accept');return true;
 }
 if(e.key==='n'||e.key==='p'){
  if(R){e.preventDefault();mRevGo(e.key==='n'?1:-1);return true;}
  if(e.key==='n'&&mFlagged().length){e.preventDefault();mRevStart(null);return true;}
  return false;
 }
 if(e.key==='1'||e.key==='2'||e.key==='3'){e.preventDefault();mTab(MTABS[+e.key-1][0]);return true;}
 if(e.key===']'||e.key==='['){e.preventDefault();mStep(e.key===']'?1:-1);return true;}
 if(e.key===' '&&!(t&&t.tagName==='BUTTON')){if(!MP.hasAudio)return false;e.preventDefault();mPlayPause();return true;}
 if(e.key==='v'){e.preventDefault();vsOpenMeeting({});return true;}
 if(e.key==='/'&&MP.tab==='transcript'){e.preventDefault();const f=document.getElementById('m-find');if(f)f.focus();return true;}
 return false;
}
document.addEventListener('input',e=>{
 const t=e.target;if(!MP||!t)return;
 if(t.id==='m-find'){clearTimeout(mFindT);const q=t.value;mFindT=setTimeout(()=>{if(MP)mFindRun(q,true);},200);}
 else if(t.id==='m-askq'){mAskThread(MP.base).draft=t.value;}
 else if(t.id==='m-title-in'){MP.titleDraft=t.value;}
 else if(t.id==='a-seek'){const a=mAudio();if(a){MP.spanStop=null;a.currentTime=parseFloat(t.value)||0;}}
});
document.addEventListener('change',e=>{
 const t=e.target;if(!t||!t.classList||!t.classList.contains('mcsel'))return;
 const n=document.getElementById(t.id+'-new');if(!n)return;
 n.hidden=t.value!=='__new__';if(!n.hidden&&n.focus)n.focus();
});
// the reader scrolled against the follow: it stops until Follow or a line click
addEventListener('scroll',()=>{if(MP&&MP.follow&&MP.audio&&!MP.audio.paused&&(!MP.autoScroll||Date.now()-MP.autoScroll>400)){MP.follow=false;mFollowUI();}},{passive:true});
