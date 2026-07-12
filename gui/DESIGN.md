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

Type (revised again 2026-07-12: Mark approved the Boardroom Wide direction and
asked to adopt Monday's font stylings). UI face: **Figtree**, Monday's own,
vendored locally as the OFL-licensed variable woff2 in gui/static/ (its
OFL.txt alongside; loaded via @font-face, served by the panel itself, never a
CDN; -apple-system remains the fallback stack). Weight does the hierarchy:
titles 600, wordmark 700, body 400-500, letter-spacing -0.005em at 16px+.
Sizes keep the calibrated middle landing: titles 17.5px, meta 13px, summary
previews 14.5px at two clamped lines, floor 13px (test-enforced). Mono
(`ui-monospace`, SF Mono) stays demoted to true data only: timestamps,
durations, live clocks, the status pill. Nothing is serif.

Color and surfaces: the "Boardroom Wide" restyle (approved 2026-07-12 from
the Monday/Asana research and the revised mockup; supersedes Signal). Rows
are floating white cards on a cool gray ground; Monday's violet-blue is the
one interactive accent; states speak in colored pills; attention items wear
a colored left edge.

Light theme:

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

Dark theme: ground #131417, cards #1B1D23 keeping their lift (deeper
shadows), ink #E9EAEE, sub #9AA0AB, accent #8F94FF (text on accent #10122E),
accent-soft #23244A, status/chips shift to their dark-soft pairs; every text
pair verified 4.5:1 programmatically, adjusting tokens hue-true when a pair
misses (the Signal precedent).

Surfaces: cards radius 14px with a soft shadow (0 1px 4px, hover deepens to
0 4px 16px), 10px gaps between rows, tray as a white card with a 4px amber
left edge, naming rows with a 4px accent left edge, primary buttons
pill-shaped with a soft accent shadow (0 4px 14px accent at 30%). The old
glow touches retire with Signal; the focus ring stays accent for
accessibility. Progress hairline becomes the busy blue. Hairlines separate
nothing on the gray ground; the cards themselves do the separating.

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

Rhythm: 8px grid. Layout and surfaces are governed by the Boardroom Wide
section above (fluid min(1360px, 94vw) cap, card rows with 10px gaps, 14px
card radius, soft shadows); type sizes by its Type section (titles 17.5px,
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
