# The panel: design rules

This file is the source of truth for the panel's visual language and
interaction rules. Builders implement it. They do not reinterpret it.

Rewritten 2026-11-02 for the new shell (plan Phase 4D, the swap). The design
came from the approved mockup
(`qa/rethink-2026-11-01/rethink/mockup-plaud-style-v2.html`) and the unified
plan.

## The idea

A meeting is one object with one address, `#m/<base>`. The sidebar holds the
places: Home, Recordings, Speakers, then Activity, Archive, Settings. Home
says what is running and what needs you. Recordings is the library. A meeting
page reads one meeting, with three tabs. The voice sheet names voices from
any page. Machinery (runs, history, duplicates) lives on Activity, never in
the way of reading.

## Shell mechanics

- `GET /` serves the panel. `GET /next`, the Phase 4C preview address,
  serves the same page, and `GET /classic` answers 302 to `/`, so an old
  bookmark still lands on the panel. The retired `ui` query flag is
  ignored.
- The shell lives in `gui/static/next.html`, `next.css`, and three scripts:
  `next.js` (the frame and the list pages), `next-meeting.js` (the meeting
  page), `next-voice.js` (the voice sheet). `gui/server.py` joins the
  scripts in that fixed order into one inline script, with no `import` line,
  no module route, and no build step. The parts are cached by mtime, so an
  edit reloads live.
- Every page reads the same `GET /api/state` poll every 2 seconds and calls
  only existing endpoints. Every write is a POST. No GET writes anything:
  `GET /api/suggest` answers 405.
- Fonts are vendored (Figtree, OFL) and served from `/static/fonts/` behind
  an exact-name allowlist. No CDN, no remote script.

## Tokens

Indigo Blueprint (approved 2026-09-08), with the mockup's two changes: light
`--attn` is `#0A6C7E`, and `--personal` is retired. Four hues, each owned by
one meaning: indigo acts, teal asks, green is done, red records or destroys.

Light:

    --paper   #F4F6FB   sidebar ground
    --card    #FFFFFF   page, sheet, popovers
    --ink     #101828   text
    --sub     #5D6B85   secondary text
    --hair    #E1E6F2   rules and borders
    --line    #C7D0E4   control borders
    --inset   #EEF1F8   hover wells, the clip well, a question bubble
    --accent  #2F4BD6   buttons, links, focus, the selected tab
    --acc-ink #FFFFFF   --accent-soft #E6EAFB   --accent-text #2F4BD6
    --sel     #EBEEFC   a selected row, the line under review
    --ok      #1E7B45 on #E3F4EA
    --attn    #0A6C7E on #DDF2F6   a voice that asks, an uncertain line
    --rec     #C2362F on #FAE3E0   recording and destructive only

Dark: paper #0F1320, card #161B2B, ink #E8EBF5, sub #98A2BF, hair #232A3D,
line #33405A, inset #1D2437, accent #8B9DFF (ink on it #0E1330, soft and sel
#232C55), ok #5BC98C on #16291E, attn #5FD0E3 on #0F2A33, rec #F0605A on
#34191B.

Every text pair measures 4.5:1 or more in all four token blocks (light, dark,
and the two `data-theme` overrides). `tools/token_contrast.py`
measures them, and `tests/test_next_shell.py` gates it. A pair that misses is
fixed hue-true. Theme: `auto` follows macOS, and the theme button cycles auto,
light, dark under the `stt_theme` key, applied before paint.

## Type and surfaces

- One face: Figtree, weight for hierarchy. Page titles 22px 600, section
  titles 18px, row titles 15px 600, body 14px, meta 13px. The floor is 13px,
  test-enforced. Mono (the system mono) is for true data only: timestamps,
  clocks, counts, scores.
- No em dashes in any UI string.
- Fewer boxes. Rows sit flat on the page, and hover brings the inset well.
  Status words are coloured text, never a filled badge. A card border appears
  only on the line card. Shadows only on the sheet, popovers, the palette,
  the bulk bar, and the toast.
- Radii: 6px controls and cards, 4px chips. Focus ring: 2px accent.
- One filled primary button per region.
- Motion: 160ms rise on overlays, none under reduced motion.

## Frame

- A 232px sidebar on `--paper`: the wordmark and the theme button, the status
  pill, New, Search, then Home, Recordings, Groups, Ask, Speakers, a gap, Activity,
  Archive, Settings. Home carries the Needs you count and Activity the
  attention count, in teal. The foot holds Keys.
- Below 900px the sidebar hides. A top bar carries the menu button, a back
  arrow on a meeting, the page name, the pill, and Search. The menu opens the
  same navigation as a sheet. Rows wrap, one column replaces two, and nothing
  scrolls sideways. Touch targets grow to 40px and 44px. Bottom tabs wait for
  phone access.
- Routes: `#home`, `#recordings`, `#groups`, `#groups/<key>`, `#ask`,
  `#ask/<thread>`, `#activity`,
  `#speakers`, `#archive`, `#settings`, `#m/<base>`, and
  `#m/<base>/t/<secs>`, which opens the Transcript tab at that second.
- The tab title is the page name plus " · Meetings". `(N)` leads it when N
  Needs you items joined while the tab was hidden.
- Groups (Phase 4E) and Ask (Phase 4F) live in `next-library.js`, joined
  after the voice sheet.

## Status pill

One line in the sidebar and the top bar. A click opens Activity. By priority:
`Recording 12:41` (red dot, ticking; `Recording paused` when paused), then
the active file's stage word with its percent and `about 18m` left, then
`Applying names` while a relabel runs, then `Paused · 3 waiting`, then
`3 waiting`. Idle shows nothing. The stage words, from the pipeline's
reported stages: `Downloading`, `Preparing`, `Transcribing`, `Speakers`,
`Verifying`, `Writing`, `Summary`. A stage it does not know reads `Working`.

## Pages

**Home.** Running now (active rows with progress, then one line for the
waiting files, shortest first). Needs attention (recorder stalls, a failed
last recording, failures, duplicates) when there is any. A failed recording
stays there until the next capture starts, as in the menu bar. Needs you, the computed list from
`/api/state` (unified plan row 10): a needs-title card with the title field
prefilled and Keep title, a voice card with Name (opens the voice sheet on
that voice, with its meeting as context), a no-speech card with Archive, and
an audio-may-be-lost card. "Nothing needs you right now." when it is empty.
Draft titles, optional: provisional titles from the last 14 days with Keep
title and a two-step Keep all. They block nothing. Recent: five rows and a
link to Recordings. An empty library shows the setup check instead: recorder,
watched folder, your voice, and the line "Record from the menu bar, let Voice
Memos sync, or drop an audio file here." No calendar.

**Recordings.** A filter field (title, person, or topic tag), a sort menu (newest,
oldest, longest, A to Z), and the chips All, Work, Personal, Draft title, Has
voices. Sections: Today, Yesterday, This week, then months. This month and
the newest older month stay open, and older months fold. A filter opens
every section. A row: title with its chips (No speech, Draft title, Date
uncertain), one meta line (when, minutes, up to three named people with +N,
"N unnamed" in teal, the tag), and a one-line preview: the gist, or the
first two sentences of an older summary. The filter matches a title, a
person, or a whole topic tag. The row menu:
Open, Select, Rename, Set date, the tags, Export Word and PDF, Copy
transcript, Show files, Redo, Archive, Delete. Selecting shows the bulk bar:
Keep titles, Work, Personal, Clear tag, Rename, Set date, Archive, Delete
audio, Delete, Select all shown. Delete and Delete audio are two-step.

**Groups.** The group list beside the open group, each with a mono count.
The open group shows its name, count, hours, and one line on why these
meetings group, then its rows. Groups form on the server (`stt/groups.py`)
from title words (average linkage, weak words such as "weekly" never join
alone), then the same people at the same hour for a meeting with a default
title. The hour comes from `recorded_at`, else from an HHMM stamp in the
folder name or `source_name`. A group needs two meetings. A grouped row shows its group on the meta
line, and its row menu ends with "Not part of this group", the one write
this page makes (`POST /api/group_override`, keyed on `meeting_id`), with an
Undo line. Nothing on disk moves. Opening a group posts one
`{event: group_open}` line to `/api/usage`, with no title or key, for the
one-month use count. Empty: "No groups yet" with one sentence on how groups
form.

**Ask.** Saved threads on the left, newest first, and the open thread on
the right. "Answer from" picks All recordings, a group, or Last 30 days for
a new question, and a follow-up keeps its thread's scope. The scope line
says "Runs on this Mac. Threads save here." Each answer lists its
citations (number, meeting, day, mono time), and a citation opens
`#m/<base>/t/<secs>`. A citation whose meeting is gone is plain text.
`POST /api/ask_library` retrieves lines from the FTS5 index, sends them to
the local model under excerpt ids, and keeps only answers that cite one.
States: an empty library says Ask starts after the first meeting; nothing
found reads "I did not find this in your meetings." with the closest search
hits and no invented answer; a busy assistant (a 503 with `busy`) disables
nothing permanently, shows its reason, and keeps the typed question; a
missing assistant disables Ask and says search still works. With a cloud
assistant set, strict meetings are left out and the answer says how many.
"Ask this group" on a group opens Ask with that scope. Delete thread is
two-step.

**Activity.** Process all new, Import files, Pause or Resume, Stop
(two-step), the four run options for the next manual run, then Needs
attention, Running now, Queued runs with Cancel, Waiting (Process, Hold,
two-step Delete), Failed (full error text, Retry, Dismiss), Finished
recently, and Removed from Voice Memos (the memos the delete after ingest
moved in the last 14 days, with any manual delete that waits). Chips switch to Running, Waiting, Failed, History, Duplicates.

**Speakers.** Unknown voices, most meetings first, each with play, Name (the
voice sheet), Hide, and up to two "Same voice as Speaker N · 0.72" Link
buttons. Named people with play, Samples (remove or move one), Rename, Merge,
and a two-step Remove. Hidden voices with Restore. Empty: "Voices appear
after the first meeting."

**Archive.** Restore and a two-step Delete. The copy says Delete is final
until a backup exists.

**Settings.** Automation (the master switch, folder watch, nightly run,
Voice Memos sync status), Transcription, Summaries and Ask, Cloud keys
(values never rendered), Recorder, Storage, Appearance.

## The meeting page

Top to bottom:

1. A back link to Recordings (the top bar's arrow below 900px).
2. The title, a button that turns into a field (Enter or Save, Escape or
   Cancel). A draft title wears the Draft title chip (teal when only the
   model drafted it) and Keep. Keep and an edited title post
   `/api/accept_meeting` for this one meeting: the title is confirmed and a
   person looked at it. When the folder re-stamps, the page follows the new
   base. Then previous, next, and the row menu.
3. One meta line: when, minutes, up to three named people with +N, and "N
   unnamed" in teal (a split voice counts once).
4. One sub line: the tag (a click cycles none, Work, Personal), "Date
   uncertain" for a file-time or short typed date, "Date from ..." for any
   stored date source, "Recorded at ..." for a Voice Memos location note,
   "Strict" for a strict meeting, and the audio-may-be-lost note.
5. One notice line when voices ask: "2 voices unnamed." with Name voices,
   and the count of brief voices hidden. Only voices with a minute of talk
   ask, and a public download asks nothing.
6. The sticky tab row: Summary, Transcript, Ask. Regenerate sits at its right
   on the Summary tab. Arrow keys move along it.
7. The panel. At 1200px and wider a People rail sits beside it: named people,
   unnamed voices as teal buttons that open the sheet, and the brief count.
8. The audio bar, docked at the bottom: play, time, scrubber, length, rate
   (1x, 1.25x, 1.5x, 2x, 0.75x), Follow, and Add line. A meeting with no
   audio has no bar.

The page is built once per meeting. The poll repaints the header, the notice,
the rail, and the Summary tab only when their markup changes, and never a
part that holds the focus. It never repaints the transcript. A finished
relabel (the row's `generated_at` moved) reloads the lines, but not while a
card, the voice sheet, a find, or a title edit is open. The change is held,
not lost: the next poll tries again. A reload never starts another reload.

**Summary.** The gist (one sentence of 20 words or fewer, weight 500),
then two sections, each an eyebrow with a mono count over a list:
Decisions and Open questions. An empty section is left out. Next steps
are not shown (removed 2026-11-05). Then
the topic tags, 3 to 5 from the fixed vocabulary in `stt/series.py`, as
quiet bordered chips; a click opens Recordings filtered to that tag. A
description written before the gist existed shows its summary paragraphs. A description a Redo left out of date says
so in one meta line ("It refreshes by itself when the batch is idle, or
press Regenerate.") and stays readable. Speaker renames, namings, merges
and relabels re-render the names in it with no model call. States: no summary
yet (Generate, and where it runs); writing (the old summary stays until the
new one lands, "Writing a new summary"); failed (the error in red, Generate
again); silent ("No speech found", no summary, no Generate, never invented).
Generate and Regenerate post `/api/suggest`.

**Transcript.** A find field with a match count (Enter and Shift Enter step,
Escape clears), the Uncertain lines chip with its count (a filter, optional),
the voice legend, then the lines: time (a button that seeks when there is
audio), speaker, text, and an edit button. A line's speaker is its roster
display, never a raw diarizer id. An unnamed speaker is a teal button that
opens the sheet. An uncertain line carries its reason in words ("Voices
overlap here", "The speaker may be wrong", "The second pass heard this
differently") and Check. The playing line wears `--inset`. Follow scrolls
with it until the reader scrolls against it.

**Ask.** One line on where the answer runs. Starter chips on an empty thread:
What was decided, Who owes what, What is still open. The thread, questions
right, answers left. The last three good turns ride along as history. States:
busy (the model is busy: no error bubble, the typed question stays in the
box, and one line says why); off (no local model: Ask disables and says so);
no text (a silent or empty transcript: Ask explains, no box); long meeting
(the sampled-middle note). Threads live in memory until reload. Saved
threads are Phase 4F.

## Reviewing and editing

Stepping is a conveyor, editing is a workspace (the owner's rule,
2026-07-12). Check on an uncertain line, or `n`, starts the walk at that
line and docks the verdict bar under the tab row, where its controls never
move: the position ("2 of 6", or "Saved · 5 left"), previous and next, Play,
Accept (the one primary), Use alt (enabled only when the flag carries the
second engine's words), Skip, Done, then "Accept N minor" when sub-second
crumbs exist and "Accept all N remaining…" behind an inline two-step that
says unheard lines are kept as transcribed.

- The walk reads the transcript top to bottom, by start time, never the
  server list's grouping.
- Bar verdicts resolve the flag and advance to the next unresolved one,
  wrapping. Card work (Save changes, split, remove, insert) holds the
  position and waits for an explicit next.
- A structural change (split, remove, insert, a merge, a new person)
  reloads the lines. The walk re-anchors to the nearest surviving flag by
  start time, held, and the scroll stays.
- Every review POST carries the segment's original JSON index and its start
  as the server's cross-check, never the array position.
- One card at a time, mounted after its line. The editor (the pencil on any
  line): speaker, engine and Listen again, text, Remove line (two-step,
  inline), Split line (at the cursor, then each half's speaker), Play,
  Cancel, Save. Add line in the audio bar inserts at the playhead with a time
  field.
- The engine menu is the transcript's `listen_engines`: Parakeet first, the
  default, then each cloud engine with a key. A strict meeting gets
  Parakeet alone. With one engine there is no menu, and a line says what
  Listen again does.
- The speaker menu lists this meeting's speakers, then enrolled people not
  already here, then "New person…", which opens a name box beside it. An
  empty box refuses the save. It never falls back to the first speaker.
- Keys, never while typing: Enter or `a` accept and advance, `u` takes the
  second reading, `n` and `p` step, Escape leaves the card, then the walk.
  Cmd Enter saves the card from inside its text. A focused button keeps its
  own Enter.

## The voice sheet

A right slide-over, 560px wide, full screen below 900px, over a veil. It
opens from Home's voice card, the notice's Name voices, `v`, the legend, a
line's speaker, the People rail, and Speakers. Form before evidence, top to
bottom:

1. "Who is Speaker 58?", the meeting title, "voice 1 of 2", and the progress
   marks.
2. The name box with the enrolled names, focused on open.
3. The preview line: "Names this voice here and in 4 other meetings", or
   "Names this voice here only", from `/api/name_preview`. The sheet works
   before it lands, and a failed preview paints nothing.
4. Up to three chips, "Looks like Mina Okafor · 0.84", which only fill the
   box. They are guesses the margin rule refused to act on alone.
5. The longest turn with its play button and the voice's talk time. On the
   open meeting it plays through the page's audio. From Speakers it plays the
   registry clip, or says the meetings are archived (Restore one) or the
   audio was deleted.
6. Up to three lines of what the voice said, from every cluster of a split
   voice. Only a successful fetch may say "No transcript text was attributed
   to this voice." An error paints nothing.
7. "Already recognized: Ana Ortiz." and "2 brief voices under a minute are
   hidden."
8. The footer: Save and next (Save for one voice), Skip, and Not a real
   speaker. A voice named for this meeting only gets Clear name instead.

Naming rules:

- One naming request at a time. Every button that can save disables while
  one is in flight.
- The voice's identity is captured before the await. An answer that lands
  after the sheet closed, moved on, or reopened for another voice never
  paints, closes, or advances that sheet. The open transcript rebuilds on
  success, so its "?" stops asking.
- A uid that is no longer unknown never opens a dead sheet: "Already named.
  Names are being applied now." or "This voice was already named.", and the
  page catches up.
- The quality floor refuses to learn a voice with too little speech. The
  sheet then offers "Name for this meeting", a meeting-only name, and says
  the panel does not learn the voice.
- The quality warning ("matches Ada's samples at only 0.41, and matches Bo
  better (0.77)") offers Save anyway. Enter never confirms it.
- The toast says what the server applied: "Named here. Updating 4 other
  meetings.", "Named here." only when the server applied the meeting the save
  came from, "Named. Updating 3 meetings." otherwise.
- A split voice is one entry. Its meeting-only name, Clear name, Not a real
  speaker, and the undo reach every cluster in one request.
- Not a real speaker: `/api/forget` for a registry voice, then the meeting's
  own `/api/dismiss_voice` so this meeting stops counting it.
  `/api/dismiss_voice` alone for a cluster the registry never tracked. The
  lines keep their words. The legend shows a dismissed voice with an undo.
- Clear name posts an empty meeting-only name, the documented undo.
- The walk: the most talk first, and never a voice this run already named,
  dropped, or skipped. A voice skipped in one meeting arrives skipped in the
  next. When every voice in a queued meeting was skipped, the walk moves on.
  The end state counts what happened ("1 named, 1 skipped") and offers "Next
  meeting: <title>", from Needs you first.
- Keys: the sheet owns every key while open. Escape closes the warning, then
  the sheet. Enter in the box saves. Outside the box: `1` `2` `3` fill from a
  chip, `s` skips, Space replays the clip, Enter saves.

## Keys

Keys never fire while typing. An open layer (the sheet, a popover, the
palette, the menu, the key sheet) owns every plain key. Escape backs out one
layer at a time, then back to the list. Key caps show only in the `?` sheet.

| Key | Where | Does |
|---|---|---|
| `/`, Cmd K | Anywhere | Search palette (`/` on a Transcript tab focuses find) |
| `?` | Anywhere | Key sheet |
| `g` then `h` `r` `g` `q` `a` `s` `e` `,` | Anywhere | Home, Recordings, Groups, Ask, Activity, Speakers, Archive, Settings |
| `j` `k`, Enter, `x` | A list | Move, open, select |
| `1` `2` `3` | Meeting | Summary, Transcript, Ask |
| `[` `]` | Meeting | Previous, next recording in the Recordings order |
| Space | Meeting | Play or pause |
| `v` | Meeting | Name voices |
| `n` `p` | Meeting | Next, previous uncertain line |
| Enter, `a`, `u` | The walk | Accept and go on, use the second reading |
| Cmd F | Meeting | Find in the transcript |
| Cmd Enter | Line card | Save |
| `1` `2` `3`, `s`, Space | Voice sheet | Fill from a chip, skip, replay |
| Esc | Anywhere | Back out one layer, then back to the list |

## Search

`/` or Cmd K opens one palette over any page. Titles first (from the loaded
list and `search.meetings`, the FTS5 title and summary index), then "Said
in" lines from `GET /api/search` (the FTS5 line index, with the scan as
fallback), each with its mono timestamp. A hit opens `#m/<base>/t/<secs>`.
Arrow keys move, Enter opens the first or the chosen hit.

Filters (Phase 4E) are typed inline and read by the server:
`after:2026-10-01` (on or after), `before:2026-10-31` (before that day),
`route:voice_memo,native`, `dated:typed,recorder` (`dated:none` is a
meeting from before the `date_source` key), `from:<speaker>`, and
`in:<group key>`. The day is the date part of `recorded_at` when it is
set, else the stored date. `/api/search` and `/api/search_meetings` also take
them as query parameters. A bad value answers with `error` and no hits, and
the palette shows that line.

## Rules that hold everywhere

- No notification banners inside the panel. Outcomes are states, counts, and
  one toast. The menu bar's ready count is the only signal outside the page.
- No panel Record button until someone confirms the microphone grant.
- Every confirm is the house two-step, never a native dialog.
- The panel never writes a store file it did not ask an endpoint to write.
- Everything is tested against the seeded demo home (`tools/demo_seed.py`)
  and synthetic data, never the live store. `tools/next_screens.py` shoots
  every page and a meeting at 700px and 1440px in both themes and fails on a
  sideways scroll.
