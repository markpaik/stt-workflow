# Panel redesign: One Timeline, Newsprint

The approved direction for the panel overhaul. This file is the single source
of truth for the new UI's visual language and interaction rules; builders
implement it, they do not reinterpret it. The old UI stays untouched and
default until the final phase flips the flag.

## The idea

A meeting is one object. Its row is its queue entry, its progress bar, its
naming form, and its transcript link. The default screen is the library plus
at most one amber tray of things needing the user. All machinery lives behind
one popover (runs) and one drawer (settings, final phase).

## Flag mechanics

- `GET /?ui=new` serves the new shell; plain `/` serves the old page until the
  final phase flips the default. `/?ui=old` always serves the old page.
- New frontend lives in `gui/static/new/{page.html,app.css,app.js}`, composed
  and cached by the same marker/mtime mechanism as the old page.
- The new shell consumes the `timeline` and `tray` keys of `/api/state` and
  reuses the existing action endpoints unchanged.
- Until the meeting page ships, opening a meeting bridges to the old UI via
  its `?open=<base>` deep link. Tray actions bridge via two SMALL additive
  params in the old app.js: `?review=<base>` (opens the review dialog) and
  `?who=<uid>` (opens the who-is-this dialog).

## Newsprint tokens

Type (revised 2026-07-12: Mark found the serif "a bit much"; the house style
follows the modern workflow-tool consensus — Linear, Notion, Height, Things —
one neutral sans, hierarchy from weight and size, no second typeface).
Display and titles: `-apple-system` (SF Pro), weight 600, letter-spacing
-0.01em at 16px and above. Body and ALL meta/labels/group headers/tray text:
the same sans with `font-variant-numeric: tabular-nums` wherever digits
align; group headers are 13px semibold uppercase sans with +0.06em
tracking, not mono (13px floor holds everywhere, test-enforced). Mono (`ui-monospace`, SF Mono) is DEMOTED to true data
only: timestamps, durations, live clocks, and the status pill. Nothing else
is mono, nothing is serif. System faces only; nothing downloads. (If a true
Inter ever becomes wanted, vendor the OFL woff2 into gui/static/ as a local
asset; never a CDN.)

Color, light theme:

    --paper  #FBFBF9   page ground
    --card   #FFFFFF   rows, popovers
    --ink    #1A1C1F   text
    --sub    #5D6167   secondary text
    --hair   #E8E6E0   hairline rules
    --line   #D9D6CE   control borders
    --inset  #F4F3EF   hover, wells
    --accent #1E6B50   the ONE interactive green (buttons, links, progress)
    --accent-soft #E4EFEA
    --amber  #9A5B10   attention (tray, review counts)  soft #F7EEDF
    --rec    #C43C35   record light ONLY                soft #F7E4E2

Color, dark theme:

    --paper #121416  --card #1A1D20  --ink #E9E7E2  --sub #9BA0A6
    --hair  #2A2E32  --line #383D42  --inset #212528
    --accent #43B28A (text on accent #0E1512)  --accent-soft #1D2E27
    --amber #D79A4E  soft #2B2216     --rec #E06058  soft #331D1B

Theme plumbing: same pre-paint snippet and `stt_theme` localStorage key as the
old page, plus `prefers-color-scheme` default. Red appears nowhere except the
recording state and destructive confirmation buttons.

Rhythm: 8px grid, reading-room density (revised 2026-07-11 after review on
real data: the first cut read compressed; type scale bumped again 2026-07-12
on Mark's feedback that fonts still read small on his display). The content
column caps at 920px, centered; shorter lines read bigger and summaries wrap
instead of truncating mid-clause. Type floor 13px: titles 18.5px sans semibold, meta
13.5px sans (tabular numerals), summary previews 15px with 1.5 leading, clamped to TWO lines
(the full summary lives on the meeting page); buttons and pill 13-13.5px.
Rows get 15-16px vertical padding; month headers carry ~30px top margin so
months read as chapters, not rows in one continuous ruled list. Mark's
display is the calibration target, not preview screenshots taken in narrow
tabs. Radius 8px on controls, 12px on floating surfaces. Borders are
hairlines, not shadows; shadows only on floating surfaces (popover, bulk
bar): a soft two-layer `0 1px 2px …, 0 10px 34px …`. Motion 150–200ms ease,
used for state morphs and hover reveals; honor `prefers-reduced-motion`.

## Anatomy

Header (sticky, paper ground, hairline bottom): sans-semibold wordmark "Meetings" ·
status pill · search field (right-aligned) · Process ▾ · theme dot · gear
(bridges to old settings until the drawer ships).

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

Row, common skeleton: category dot · title · meta (sans, tabular numerals,
sub) · right slot.
The right slot is the state:

- recording   `● capturing` in rec red, elapsed ticking; title "Recording now…"
- waiting     `size · ≈est min` + hover actions [▶ listen] [hold ❚❚] [Process] [✕]
- held        `❚❚ held` + same hover actions with [release]
- processing  stage + % + eta in accent sans, tabular numerals; 2px accent hairline progress
              bar along the row's bottom edge; no actions (Stop lives in the
              Process ▾ popover)
- needs_name  the row IS the form, accent-soft ground: title input (prefilled
              with suggested_title), date input (suggested_date), [▶] [Accept].
              Enter accepts. No separate inbox card exists.
- ready       meta = `MMM D · NN min · speakers · 6 to check`, the review
              count as plain amber TEXT inside the meta line, no chip
              (forty chip-wearing rows read as a wall of warnings); right
              slot shows state text that yields to hover actions
              [▶] [Open] [⋯]. Clicking the row body (not its controls)
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
appear on hover/focus-within only. Checkboxes for bulk selection appear on
row hover (left edge) and stay visible while any selection exists.

Category dot: hollow = untagged, accent-filled = work, amber-filled =
personal; click cycles, optimistic update (same endpoint as old chip).

Bulk bar: floats bottom-center when selection ≥ 1 (card, shadow): count,
Work · Personal · Clear tag · Rename… · Set date… · Archive · Delete audio… ·
Delete… · Select all shown · ✕. Same `/api/bulk` calls as the old selbar.

Process ▾ popover (from header): Process all new · Process selected · Other
files… · a Stop processing row while running · Pause/Resume automatic runs ·
the four run option checkboxes (two at a time, strict, verify, one-time
speakers) with their one-line explanations, persisted exactly like today.
This popover is the ONLY machinery surface in the shell.

⋯ menu (per ready row, popover not modal): Export Word · Export PDF · Copy
transcript · Show files · Rename… · Redo… · Archive · Delete…. All existing
endpoints; Redo and Delete keep their confirm steps.

Search: same behavior as old (client filter + debounced full-text ≥3 chars);
full-text hits render as a quiet sub-list under the search field, mono
timestamps, click opens the meeting page at that moment (#m route + seek). Filter select (all /
work / personal) and sort (by month / by name) sit left of the search field,
borderless until hover.

Empty states, in sub text, centered: library empty → "Record a meeting from
the menu bar." (the drop line returns when the upload endpoint ships); search empty → "No matches.";
tray absent entirely when empty.

## The drawer

One right slide-over, width min(560px, 94vw), Newsprint-styled (paper ground,
hairline left edge, soft shadow), opened by the gear, closed by ✕ / veil /
Escape. A compact section nav pins at its top: Settings · Speakers · History ·
Archive. Nothing in the drawer navigates to the old page.

Settings section, in order:
- Automation: ONE master switch ("Automatic runs"), with Folder watch and
  Nightly run (+ time) indented beneath it and visibly inert (greyed, "off
  while automatic runs are paused") when the master is off. The master state
  is the same /api/pause//api/resume the pill and Process popover use. This
  kills the old page's lying toggles: a switch never reads On while doing
  nothing.
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
