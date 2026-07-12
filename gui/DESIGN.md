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

Type. Display: `ui-serif` ("New York"), weight 600, tight leading, for the
wordmark, group headers rendered as small caps mono (see below), and meeting
titles at detail sizes. Body: `-apple-system` (SF Pro Text). Data (timers,
counts, dates, states): `ui-monospace` (SF Mono) with `tabular-nums`.
System faces only; nothing downloads.

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

Rhythm: 8px grid. Rows 46px tall (denser than the old cards). Radius 8px on
controls, 12px on floating surfaces. Borders are hairlines, not shadows;
shadows only on floating surfaces (popover, bulk bar): a soft two-layer
`0 1px 2px …, 0 10px 34px …`. Motion 150–200ms ease, used for state morphs and
hover reveals; honor `prefers-reduced-motion`.

## Anatomy

Header (sticky, paper ground, hairline bottom): serif wordmark "Meetings" ·
status pill · search field (right-aligned) · Process ▾ · theme dot · gear
(bridges to old settings until the drawer ships).

Status pill: the ONLY place pipeline state appears. Mono, one at a time, by
priority: `● REC 12:41` (rec red, ticking) → `transcribing 2 · ≈14 min`
(accent) → `⏸ paused · 3 waiting` (amber) → `3 waiting` → nothing when idle
(the pill disappears; idle needs no announcement). Clicking the pill opens the
Process ▾ popover.

Tray (only when non-empty): amber-soft band under the header, small-caps mono
header "NEEDS YOU · N", one line per item, ranked (stall → failed → review →
unknown voice), each with a right-aligned action verb. The tray never scrolls;
if more than 4 items, the 4 highest with "and N more…" expanding in place.

Timeline: full-width list on paper, month group headers in small-caps mono
(`JULY 2026 · 6`), Today/Yesterday labels for the current groups when
grouping by date. Jump rail (year/month) survives from the old UI, restyled
mono, visible only when 3+ groups and no active search.

Row, common skeleton: category dot · title · meta (mono, sub) · right slot.
The right slot is the state:

- recording   `● capturing` in rec red, elapsed ticking; title "Recording now…"
- waiting     `size · ≈est min` + hover actions [▶ listen] [hold ❚❚] [Process] [✕]
- held        `❚❚ held` + same hover actions with [release]
- processing  stage + % + eta in accent mono; 2px accent hairline progress
              bar along the row's bottom edge; no actions (Stop lives in the
              Process ▾ popover)
- needs_name  the row IS the form, accent-soft ground: title input (prefilled
              with suggested_title), date input (suggested_date), [▶] [Accept].
              Enter accepts. No separate inbox card exists.
- ready       meta = `MMM D · NN min · speakers`; badges: review counts in
              amber mono (`6 to check`); right slot shows state text that
              yields to hover actions [▶] [Open] [⋯]
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
timestamps, click bridges to `?open=` at that moment. Filter select (all /
work / personal) and sort (by month / by name) sit left of the search field,
borderless until hover.

Empty states, in sub text, centered, serif: library empty → "Record a meeting
from the menu bar, or drop an audio file here."; search empty → "No matches.";
tray absent entirely when empty.

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
