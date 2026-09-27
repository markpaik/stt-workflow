# Panel redesign: One Timeline, Newsprint

The approved direction for the panel overhaul, now fully shipped. This file
is the single source of truth for the shell's visual language and interaction
rules; builders implement it, they do not reinterpret it. The old UI was
deleted 2026-07-12 after the shell reached full parity.

## The idea

A meeting is one object. Its row is its queue entry, its progress bar, its
naming form, and its transcript link. The default screen is the library plus
at most one amber tray of things needing the user. All machinery lives behind
one popover (runs) and one drawer (settings).

## Shell mechanics

- The shell is the ONLY frontend: `GET /` serves it. The retired `ui` query
  flag is ignored entirely, so a stale bookmark carrying it still gets the
  shell, never a 404. No flags, no bridges, no second page.
- The frontend lives in `gui/static/{page.html,app.css,app.js}` (vendored
  fonts in `gui/static/fonts/`), composed via marker comments and cached by
  mtime in `gui/server.py`, so edits to the parts reload live without a
  server restart.
- The shell consumes the `timeline` and `tray` keys of `/api/state` and
  reuses the existing action endpoints unchanged. Meetings, review, and
  naming all open inside the shell; no deep-link query params remain.

## Newsprint tokens

> Colour, surface, geometry, and grouping are now governed by "Indigo
> Blueprint (approved 2026-09-08)" at the foot of this file; the palettes and
> surface rules below are kept for the record and are superseded where the
> two disagree.

Type (revised again 2026-07-12: Mark approved the Boardroom Wide direction and
asked to adopt Monday's font stylings). UI face: **Figtree**, Monday's own,
vendored locally as the OFL-licensed variable woff2 in gui/static/ (its
OFL.txt alongside; loaded via @font-face, served by the panel itself, never a
CDN; -apple-system remains the fallback stack). Weight does the hierarchy:
titles 600, wordmark 700, body 400-500, letter-spacing -0.005em at 16px+.
Sizes keep the calibrated middle landing: titles 17.5px, meta 13px, summary
previews 14.5px at two clamped lines, floor 13px (test-enforced). Mono
(`ui-monospace`, SF Mono) stays demoted to true data only: timestamps,
durations, live clocks, the status pill, and the group counts. Nothing is
serif. Group eyebrows carry .08em tracking, not the .06em of the Boardroom
round (Indigo).

Color and surfaces: SUPERSEDED by Indigo Blueprint. The "Boardroom Wide"
restyle (approved 2026-07-12 from the Monday/Asana research and the revised
mockup; superseded Signal) put floating white cards on a cool gray ground
with Monday's violet-blue as the one interactive accent. Indigo keeps the
white-card-on-cool-ground idea and the colored left edge, and replaces the
palette, the shadows, and the radii.

Light theme (Boardroom, superseded):

    --paper  #F6F7FB   page ground (cool gray)
    --card   #FFFFFF   cards, popovers, tray
    --ink    #1C1E26   text
    --sub    #6A6F7E   secondary text (verify 4.5:1 on card)
    --hair   #ECEDF4   card borders
    --line   #E0E2EC   control borders
    --inset  #F3F4F9   hover wells inside cards
    --accent #6161FF   Monday violet-blue: buttons, links, focus, naming edge
    --acc-ink #FFFFFF  --accent-soft #EEF0FF
    --ok     #2E9E5B on #E8F6EE     ready/status-good pill
    --busy   #2264D1 on #E8EFFC     transcribing pill + progress
    --amber  #8F5F10   attention    soft #FBF1DC
    --rec    #CC3F38   record light ONLY   soft #FBE7E5
    Category chips: Work #4B4FD9 on #EEF0FF, Personal #B4690E on #FFF1E5.

Dark theme (Boardroom, superseded): ground #131417, cards #1B1D23 keeping
their lift (deeper shadows), ink #E9EAEE, sub #9AA0AB, accent #8F94FF (text
on accent #10122E), accent-soft #23244A, status/chips shift to their
dark-soft pairs. The verification rule itself is NOT superseded and carries
into Indigo: every text pair is verified 4.5:1 programmatically
(`tools/token_contrast.py`, gated by tests/test_new_shell.py), adjusting
tokens hue-true when a pair misses (the Signal precedent).

Surfaces: SUPERSEDED by Indigo Blueprint, which is geometric: 6px cards with
a 1px --hair border and no drop shadow, 4px chips and pills, 6px controls
with a 1px --line border, shadows only on the popovers, the drawer, and the
bulk bar. Boardroom's 14px radii and card shadows (0 1px 4px, hover 0 4px
16px) and the primary button's accent glow are all retired. What carries
forward unchanged: 10px gaps between rows, the tray's 4px attention left
edge (teal now, not amber), the naming row's 4px accent left edge, the
accent focus ring, and the rule that the cards do the separating rather than
hairlines drawn between them. The progress hairline is 2px of accent.

Layout, use-the-space rule (Mark, 2026-07-12): the column cap goes fluid,
max-width min(1360px, 94vw). Card content spreads: the left block (title,
meta, summary) takes all available width with summaries running the card's
width, and a right-aligned rail carries the state pill with the category
chip and amber check-count beneath it, so no dead zone opens between text
and state. Inputs grow: the naming row's title input flexes to all available
width. The meeting page and drawer live on the same gray ground as cards at
the same fluid cap. Category interaction: the chip itself cycles
untagged/work/personal on click (untagged rows show a hollow dot that
cycles, growing the chip once tagged).

Theme plumbing: a pre-paint snippet and the `stt_theme` localStorage key, plus
`prefers-color-scheme` default. Red appears nowhere except the recording
state and destructive confirmation buttons.

Rhythm: 8px grid. Layout stays as this section sets it (fluid
min(1360px, 94vw) cap, card rows with 10px gaps); surfaces are governed by
Indigo Blueprint (6px card radius, 1px hairline borders, no card shadow);
type sizes by its Type section (titles 17.5px,
meta 13px, summaries 14.5px at two clamped lines, floor 13px,
test-enforced). Superseded for the record: the 920px reading column and
18.5px titles of the Newsprint/Signal era. Rows keep 15-16px vertical
padding inside their cards; month headers carry ~30px top margin so months
read as chapters. Mark's display is the calibration target, not preview
screenshots taken in narrow tabs. Motion 150-200ms ease for state morphs
and hover lifts; honor `prefers-reduced-motion`.

## Anatomy

Header (sticky, paper ground, hairline bottom): sans-semibold wordmark "Meetings" ·
status pill · search field (right-aligned) · Process ▾ · theme dot · gear
(opens the drawer).

Status pill: the ONLY place pipeline state appears. Mono, one at a time, by
priority: `● REC 12:41` (rec red, ticking) → `transcribing 2 · ≈14 min`
(accent) → `⏸ paused · 3 waiting` (amber, always with the waiting count when
nonzero) → `3 waiting` → nothing when idle (the pill disappears; idle needs
no announcement). Clicking the pill opens the Process ▾ popover.

Tray (only when non-empty): amber-soft band under the header, uppercase
semibold sans header "NEEDS YOU". Rare, urgent kinds (recorder stall, failures) get one
line PER ITEM; chronic kinds aggregate to one line PER KIND. Revised
2026-07-11 after real data showed 40 flagged meetings turning the tray into a
wall of 46 asks; a tray that shouts daily is worse than no tray. The tray
therefore never exceeds a handful of lines no matter the backlog.

Aggregate expansion (revised 2026-07-12 after the first expansion rebuilt the
wall inside the tray): an aggregate with more than 8 items does NOT expand
in the tray. "Flagged lines in N meetings" instead applies a flagged-only
FILTER to the library (full-size rows; a small "flagged only ✕" chip appears
beside the category filter to clear it, and the tray line reads as active).
Aggregates of 8 or fewer (e.g. "5 voices need names") expand in place, but
their sub-rows use the library's meta scale (13px, roomy padding), never a
denser one. The tray must never contain a second, smaller library.

Timeline: a centered list on paper in two zones. First an UNLABELED pinned
cluster of everything actionable, in this order: recording, processing,
needs_name, failed, held, waiting — these never sink into date groups no
matter how old their files are (a failed April file must sit at the top, not
buried in April). Below it, ready meetings sorted AND grouped by the same
key: meeting date, newest first, with month group headers in uppercase semibold sans
(`JULY 2026 · 6`) and Today/Yesterday labels only for rows actually dated
today/yesterday. Sorting by one key while grouping by another fragments the
months; never do it. Jump rail regenerates from the ordered groups (years as
anchors, months beneath), small semibold sans, visible only when 3+ groups and no active
search.

Row, common skeleton (corrected 2026-07-12 to match the Boardroom Wide
sections above, which govern where they and this list disagree): checkbox
gutter · title · meta (sans, tabular numerals, sub) · right rail. The rail
carries the state pill on top and, on ready rows, the category chip (hollow
dot while untagged) with the amber check-count beneath it.
The right rail's pill is the state:

- recording   `● capturing` in rec red, elapsed ticking; title "Recording now…"
- waiting     `size · ≈est min` + hover actions [▶ listen] [hold ❚❚] [Process] [✕]
- held        `❚❚ held` + same hover actions with [release]
- processing  stage + % + eta in a busy-blue pill, tabular numerals; 2px
              busy-blue hairline progress bar along the card's bottom edge
              (Boardroom; supersedes the accent progress); no actions (Stop
              lives in the Process ▾ popover)
- needs_name  the row IS the form, a white card wearing the 4px accent left
              edge (Boardroom; supersedes the accent-soft ground): title input
              (prefilled with suggested_title, flexing to all available
              width), date input (suggested_date), [▶] [Accept].
              Enter accepts. No separate inbox card exists.
- ready       meta = `MMM D · NN min · speakers`; the review count sits in
              the right rail beneath the state pill (Boardroom; supersedes
              the in-meta placement) as plain amber TEXT, no chip
              (forty chip-wearing rows read as a wall of warnings); the
              rail's green `ready` pill yields to hover actions
              [▶] [Open] [⋯]. The TITLE and the meta's DATE are
              click-to-edit in place (the retired page's quick path, restored
              2026-07-12): the title becomes a text input, the date a date
              input; Enter saves (`/api/rename` / `/api/set_date`, the folder
              re-stamps server-side and the row re-sorts on the next poll),
              Escape cancels, and hover marks both targets with an edit
              cursor plus a subtle underline. Neither click expands the row.
              The meeting page's header title renames inline the same way
              (it shares the row's rename path). Clicking the row body (not its controls)
              expands it in place: full summary + committed next steps +
              an "Open transcript →" link, collapse on second click,
              multiple rows may stay open, expansion survives polls, height
              animates (none under reduced motion). The full summary is
              already in the polled meetings state; no extra fetch. This
              replaces the old hover tooltip: hover reveals actions, click
              peeks the summary, Open reads the meeting.
- failed      `failed` in rec red + one-line error, sub; hover: [Retry] [✕];
              note "original stays in the watched folder"

Hover rule: rows show at most ONE line of quiet state text at rest; buttons
appear on hover/focus-within only. Revealed buttons JOIN the row's flow at
the right edge (clarified 2026-07-12 after they shipped as an absolute
overlay that sat on top of long meta lines and summaries): the state text
yields its space entirely and the row text re-truncates; hover actions must
never render on top of visible text, in either theme, at any width.
Checkboxes for bulk selection appear on
row hover (left edge) and stay visible while any selection exists.

Category mark (corrected 2026-07-12 to the Boardroom chips, superseding the
filled dots): hollow dot = untagged; tagged rows wear the colored chip (Work
violet, Personal orange) in the right rail. Click cycles either form,
optimistic update (same /api/set_category endpoint as ever).

Bulk bar: floats bottom-center when selection ≥ 1 (card, shadow): count,
Work · Personal · Clear tag · Rename… · Set date… · Archive · Delete audio… ·
Delete… · Select all shown · ✕. All through the same `/api/bulk` endpoint.

Process ▾ popover (from header): Process all new · Process selected · Other
files… · a Stop processing row while running · Pause/Resume automatic runs ·
the four run option checkboxes (two at a time, strict, verify, one-time
speakers) with their one-line explanations, persisted exactly like today.
This popover is the ONLY machinery surface in the shell.

⋯ menu (per ready row, popover not modal): Export Word · Export PDF · Copy
transcript · Show files · Rename… · Redo… · Archive · Delete…. All existing
endpoints; Redo and Delete keep their confirm steps.

Search: client filter + debounced full-text at ≥3 chars;
full-text hits render as a quiet sub-list under the search field, mono
timestamps, click opens the meeting page at that moment (#m route + seek). Filter select (all /
work / personal) and sort (by month / by name) sit left of the search field,
borderless until hover.

Empty states, in sub text, centered: library empty → "Record a meeting from
the menu bar, or drop an audio file here."; search empty → "No matches.";
tray absent entirely when empty.

## Reviewing and editing on the meeting page

Stepping is a conveyor; editing is a workspace (Mark's rule, 2026-07-12,
after live feedback: the stepping controls traveled with the inline card so
the mouse chased them down the page, and approving an accurate flag took too
many moves). Clicking the amber strip (or n) starts stepping and docks a
compact verdict bar directly under the sticky audio bar — under the shell
header when the meeting has no audio — whose controls NEVER move while the
transcript scrolls beneath: `⚠ 2 of 6 · ‹ › · ▶ Play · ✓ Accept · Use alt ·
Skip · ✕ done`, plus "Accept N minor" when crumbs exist and, at the far end,
"Accept all N remaining…" behind the house two-step confirm (its copy says
plainly that unheard lines are trusted as-is). ✓ Accept is the one primary
accent button; Use alt enables only when the flag carries second-engine
text. Flags step in DOCUMENT ORDER, by segment start time, never the server
list's grouping (Mark's rule, same day: hopping around the transcript loses
the reader); advancing lands on the next unresolved flag BELOW the current
position. Bar actions are verdicts and AUTO-ADVANCE to the next unresolved
flag (stepping wraps at both ends; ✕ or Escape exits and the strip returns).
Keyboard, never while typing: Enter or `a` accepts-and-advances, `u` takes
the second engine, n/p step, Escape exits; keys appear as title-attribute
hints only, never visible clutter.

Card actions are work at a spot the user chose and NEVER advance: the inline
card keeps the flag reason, alt preview, speaker + text editing, and Save,
and Save, split, remove, and insert all hold both scroll and stepping
position (structural reloads re-anchor by segment start time), update the
counters in place, and wait for an explicit next. Cmd+Enter saves from the
textarea; plain Enter stays a newline. Polls and bar resolutions never
collapse an open editor. Counts everywhere (bar, strip, row meta, tray)
update optimistically.

## The drawer

One right slide-over, width min(560px, 94vw), Newsprint-styled (paper ground,
hairline left edge, soft shadow), opened by the gear, closed by ✕ / veil /
Escape. A compact section nav pins at its top: Settings · Speakers · History ·
Archive. Nothing in the drawer navigates away from the shell.

Settings section, in order:
- Automation: ONE master switch ("Automatic runs"), with Folder watch and
  Nightly run (+ time) indented beneath it and visibly inert (greyed, "off
  while automatic runs are paused") when the master is off. The master state
  is the same /api/pause//api/resume the pill and Process popover use. A
  switch never reads On while doing nothing (the retired page's toggles
  lied; these must not).
- Transcription: model picker; cloud engines listed only when their key is
  set; Cloud keys… opens an in-drawer subview (password fields, saved ticks,
  clear buttons; same /api/cloud_keys).
- Summaries and Ask: backend picker with the local/cloud privacy note.
- Recorder: your-voice picker (enrolled names), permission fix button when
  stalled.
- Housekeeping: punctuation toggle, model-update check, speed-calibration
  note, watched folder and transcripts folder with Change… pickers.

Speakers section: enrolled people (voice snippet ▶, sample count, per-sample
play/reassign/remove, rename, merge, remove) and unknown voices (Who is
this? opens the shell's naming panel, hide/restore). Same endpoints as the
old card; relabel-in-progress note surfaces here and as a quiet pill note.

History section: the permanent processing log, name filter + all/ok/failed
select, day groups, failures keep full error text (same /api/history).

Archive section: archived meetings with Restore and Delete… (two-step,
in-drawer confirm; same endpoints).

## Naming a voice

One slide-over (the naming panel, z 60/61, above the drawer) is where every
voice gets its name, opened from the drawer's Speakers section, a timeline
row's "?" chip, or the transcript legend. Anatomy, top to bottom, form before
evidence:

- **The preview line.** One quiet sub line directly under the name box:
  "Names this voice here and in 4 other meetings", or "Names this voice here
  only". It is the answer to "what does Save actually touch", asked before the
  save rather than reported after it, and it comes from `/api/name_preview`
  (the same centroid targeting the save itself uses). The panel opens and
  focuses the box FIRST; the line fills in when the response lands. A preview
  that fails paints nothing. A save is never blocked on it.
- **The looks-like chips.** Under the line, when the voice already resembles
  somebody enrolled: ordinary `.nchip` buttons reading "Looks like Riley Stone · 0.84", best first, at most three, score in mono. Clicking one fills
  the name box and nothing else. These are the clusters the automatic margin
  rule refuses on purpose (at or above MATCH_MIN, under the margin) — a machine
  must not name them, and a human should not have to rediscover them. Evidence,
  never an action.
- **The after-save toast.** "Named here. Updating 4 other meetings." from the
  response's own `targets`, or "Named here." when there are none. The one
  sentence a save is allowed to say; the panel then closes.
- **The pill.** The naming's own progress, lowest priority in the status pill:
  `naming: 3 of 7` while the relabel queue knows its total, `applying names…`
  while it does not (a whole-library pass re-lists as it goes). Gone on its own
  when the queue empties. No banner, ever.
- **The row flash.** A meeting rewritten by that relabel tints for two seconds
  in `--accent-soft` (class `updated`, the row's own transition, none under
  reduced motion). The list's rows carry `generated_at`, so the flash marks
  exactly the meetings whose bytes moved between two polls. It never enters the
  list's change signature: a flash may not cause a rebuild.
- **The suggestion lines.** In the drawer's Speakers section, under an unknown
  voice that has one: "Possibly the same voice as Speaker 12 · 0.72" with a
  [Link] button (one `/api/merge_speakers` by uid, toasted with the save's own
  sentence), and "Looks like Riley Stone · 0.84" with a [Name] button that
  opens this panel with the name already typed. Best candidate only, one line
  each, inset ground, no chips — a wall of guesses is a decision, and this is a
  glance.

### Finish flow (shipped 2026-09-27, phase 1 of docs/ui-refresh)

A meeting page carries one strip at its top, under the header and the docked
audio bar and above the summary, while the meeting has an open step:
`1 Title · 2 Voices (2) · 3 Flags (6) · Done`. The steps come from the row's
`finish` record in `/api/state` (`{title, voices, flags, done}`: the name and
date accepted, the unnamed voices not dismissed, the substantial flags). Teal
marks an open step, indigo the current one, green a done one; counts are mono.
The current step's controls render under the strip, and each step advances on
its own:

- **Title.** The suggested title and date, prefilled in the inline rename and
  date inputs, with "Use the file name" beside them. Enter accepts through
  `/api/accept_meeting`; a re-stamped folder re-routes the page and the flow
  follows it.
- **Voices.** One voice at a time, form before evidence: "Who is Speaker 58?",
  "voice 1 of 2 in this meeting", the name box, the preview line and at most
  three looks-like chips, then Save and next, Skip, and Not a real speaker.
  Beside it, the voice's longest turn (played through the page's own span
  playback) and up to three of its lines with timestamps. The save is the
  naming panel's own (`saveName`), floor and warn confirms included. A skipped
  voice keeps Voices open and the strip moves on past it.
- **Flags.** The verdict bar, unchanged, from the first flag in document
  order. A review that ends with no substantial flag left moves to Done.
- **Done.** The strip becomes one green line, "Finished: title, voices, and
  flags are done.", with "Next to finish →".

A meeting is to finish when its title is open, or when any step is open and it
was processed in the last 14 days. Only those take the keyboard and play the
clip on their own; an older meeting with open steps shows the strip and waits.
A meeting whose record was already done when the page opened shows no strip,
so a finished meeting reads as it did before. Keys, never while typing: Enter
accepts the step, Space replays the clip, `1` to `3` fill the box from a chip,
`s` skips a voice (and leaves the box unfocused, so a second `s` skips again),
`]` and `[` open the next or previous meeting to finish by the `#m/<base>`
route. Escape in a step's input releases the keyboard to these keys. Key hints
live in title attributes, never as visible key caps.

## What deliberately does not exist in the new shell

No Queue card, Processing card, Recent results, or naming inbox (row states
replace all four). No Speakers card (tray surfaces unknown voices; management
moves to the drawer phase). No permanent run-option checkboxes. No status
text beyond the pill. No notification banners of any kind: outcomes are row
states, and the recorder's last-outcome note appears as a tray `failed` item
only when it needs a decision.

## Parity checklist source

The 25-surface inventory in the proposal artifact (2026-07-11) is the parity
checklist; each phase's gate walks the rows it absorbs. Everything tested
against the seeded demo home (`tools/demo_seed.py`), never real data.

## Indigo Blueprint (approved 2026-09-08; supersedes Boardroom Wide)

Mark chose this direction from six on the design canvas
(https://claude.ai/code/artifact/59bf675a-a2a6-4070-a13f-7d09b69b75e4): a
tech-forward palette with an official register and geometric surfaces, and
a two-level year-to-month accordion for a library past 100 meetings. Every
anatomy rule above still holds; this section governs color, surface,
geometry, and grouping where they disagree.

Type stays Figtree at the calibrated sizes (titles 17.5px, meta 13px,
summaries 14.5px at two clamped lines, floor 13px, test-enforced). Group
eyebrows are uppercase with .08em tracking; counts are mono, tabular.

Light theme tokens (text pairs verified 4.5:1 programmatically, adjusting
hue-true when a pair misses, exactly as the Signal and Boardroom rounds did):

    --paper   #F4F6FB   page ground (pale cool gray)
    --card    #FFFFFF   cards, popovers, tray, drawer
    --ink     #101828   text
    --sub     #5D6B85   secondary text
    --hair    #E1E6F2   card borders and month rules
    --line    #C7D0E4   control borders and year rules
    --inset   #EEF1F8   hover wells
    --accent  #2F4BD6   blueprint indigo: buttons, links, focus, naming edge,
                        the busy state (busy shares the accent hue)
    --acc-ink #FFFFFF   --accent-soft #E6EAFB   --accent-text: the accent,
                        darkened hue-true only if it misses 4.5:1 on ground
    --ok      #1E7B45 on #E3F4EA      ready pill
    --busy    = accent on accent-soft  transcribing pill and progress hairline
    --attn    #0B7285 on #DDF2F6      attention: the Needs-you tray edge and
                                       eyebrow, flagged counts, held state
                                       (teal replaces every former amber use;
                                       the --amber token name is kept as an
                                       alias of --attn so no rule breaks)
    --rec     #C2362F on #FAE3E0      record light and destructive buttons ONLY
    Category chips: Work = accent on accent-soft; Personal #A8327A on #F8E4F0
    (magenta replaces the former orange). Four hues, each owned by one
    meaning: indigo acts, teal asks, green is done, red records or destroys.

Dark theme: ground #0F1320, cards #161B2B, ink #E8EBF5, sub #98A2BF, hair
#232A3D, line #33405A, accent #8B9DFF (text on accent #0E1330, accent-soft
#232C55), ok #5BC98C on #16291E, attention #5FD0E3 on #0F2A33, personal
#E58BC4 on #331A2A, rec #F0605A on #34191B. Verified the same way; adjust
hue-true.

Surfaces, geometric: cards are white with a 1px --hair border and a 6px
radius, NO drop shadow (the border does the separating; shadows survive only
on popovers, the drawer, and the bulk bar). Chips and pills carry a 4px
radius. Controls carry the 6px radius and a 1px --line border. The sticky
header keeps the paper ground and adds a faint dot grid behind it
(radial-gradient of --line at 1px on a 14px cell, at reduced alpha so text
contrast is untouched). The attention tray keeps its 4px left edge, now
--attn; naming rows keep the 4px accent edge. Progress is a 2px accent
hairline. Focus ring stays accent. Motion rules unchanged.

Grouping, the year-to-month accordion (replaces flat month headers):
- Ready meetings group by meeting date into MONTH sections, and month
  sections nest under YEAR sections when the library spans two or more
  years; a single-year library shows month sections only (no year header
  for a year that has nothing to fold against).
- Every section header is one line: chevron (right when collapsed, down
  when open), the eyebrow (`2026` at 14px 700 ink for a year, `SEPTEMBER
  2026` at 13px 600 sub for a month), a hairline rule filling the width
  (--line under a year, --hair under a month), and the meeting count in
  mono at the right edge. The whole line is the click target.
- Defaults: the current month and the previous month open; every older
  month collapsed; the current year open; every prior year collapsed with
  its months hidden inside it. A prior year ALSO opens when it holds the
  previous month, so that on the fifth of January December and Yesterday
  are not swallowed by a folded 2025. Collapsing a year hides its month
  headers too, so a five-year library reads as five lines.
- State persists per section in localStorage (`stt_groups`, keyed by the
  section id, values open or closed), survives polls and reloads, and a
  new month appearing opens by default.
- Search or the flagged filter expands every section holding a match and
  restores the persisted state when the filter clears. Nothing is written
  to that state while a filter is on, including a click on a header the
  filter forced open. A jump-rail click expands the target section and its
  year before scrolling.
- The pinned actionable cluster (recording, processing, needs_name, failed,
  held, waiting) stays above every group and never folds.
- Collapsed sections render only their header line: the rows are not in
  the DOM, so a 400-meeting library costs the browser only its open
  months.
