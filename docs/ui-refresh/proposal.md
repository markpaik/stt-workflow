# Panel refresh: List and Reader

Proposal, 2026-09-27. See `mockup.html` and `flows.md`.

## Direction

Keep the meeting as the one object, and put the list and the open meeting side
by side. The left pane lists what is happening now, what is left to finish,
and the library. The right pane is the reader: the selected meeting, with a
short "finish" strip at the top that walks title, voices, and flags in order,
then hands over to the next unfinished meeting. Naming a voice and checking a
flag happen inline in the reader. For one new meeting, the everyday loop
drops from 21 actions across four surfaces to 16 across two, and naming a
backlog voice drops from three actions to one.

## What the audit found

The live panel on 2026-09-27 holds 105 meetings: 11 waiting for a title, 49
with an unnamed voice (31 voices in the unknown list), and 92 with flagged
lines (577 substantial, 2,331 minor). Three problems cause most of the extra
clicks.

1. One meeting's work is spread over four places. The title is a form in the
   timeline row, each voice opens the naming slide-over, flags need the
   meeting page (which replaces the list), and the summary is in the row peek.
2. Nothing hands over to the next item. The slide-over closes after each save,
   and the tray's "Name →" on 31 voices opens only the first, so every voice
   is a fresh trip to the tray. After the last flag the user stays on the
   page; the next meeting means Escape, find the row, click its count.
3. Pending items crowd the library. Each naming row is about 170px tall, so
   11 of them fill two screens before the first finished meeting.

Waiting mostly has feedback: a processing row shows stage, percent, and time
left. Two gaps remain. "naming: 3 of 7" is the pill's lowest priority, so it
vanishes whenever a file is transcribing. A meeting that finishes while the
panel is a background tab gives no sign.

## Information architecture

One screen with two panes, plus the existing drawer and Process popover.

- **List pane** (left, 380px, paper ground): a "Now" block for recording,
  processing, and the tray's urgent kinds (recorder stall, failed file, one
  row each as today), then "To finish", then the library in the existing
  year-to-month accordion. The tray band goes away.
- **Reader** (the rest, card ground): today's meeting page nearly whole,
  mounted beside the list instead of in place of it. Header with inline
  rename, docked audio, finish strip, summary and next steps, transcript, ask
  bar.
- The naming slide-over stays for the drawer's Speakers section, where no
  meeting is in hand.

Below 900px the panes stack and the reader opens full width with a back
control, which is today's layout.

## Object model

The unit stays the meeting; a recording is its first state, as now. The day
is a label inside the list ("Today", "Yesterday"). New is a derived finish
record per meeting: title accepted, unnamed voices, substantial flags, all
already in `/api/state`. A meeting is "to finish" when one is open.

To keep the 40-row wall from returning, "To finish" holds meetings with an
open title or processed in the last 14 days. Older flagged meetings keep
their teal count in the library, reachable through a "Has flags" filter. The
backlog is a filter, never a to-do list.

## The naming and review loop

A meeting with open steps shows one line at the top of the reader:
`1 Title · 2 Voices (2) · 3 Flags (6) · Done`. The current step's controls
render under it, and each step advances on its own.

- **Title.** Suggested title and date, prefilled, the file name one click
  away. Enter accepts.
- **Voices.** One voice at a time, inline. The clip plays when the step opens
  (Space replays), the name box has focus, and the preview line and
  "looks like" chips sit under it as in the slide-over today. Enter saves and
  moves on; `s` skips; "Not a real speaker" stays.
- **Flags.** Today's verdict bar and keys, unchanged, starting at the first
  flag in document order. "Accept 20 minor" stays one click.
- **Done.** The strip turns into a green "Finished" line with "Next to finish
  →" (`]`). The summary is already on screen below it.

"Name 31 voices" in the To finish header runs the same Voices step across the
library, largest voice first.

## Progress and status

The pill stays the one summary of pipeline state. Naming becomes its second
segment instead of its lowest priority: `transcribing 1 · ≈6 min | naming 3
of 7`. The Now block gives detail: the recording clock, and for a processing
file its stages (Preparing, Transcribing, Speakers, Summary) with the current
one marked. When a meeting joins "To finish" in a hidden tab, the page title
reads `(1) Meetings`. No banners, as now.

## Keyboard

Keys stay invisible until asked for; `?` opens a one-card key sheet. In the
list: `/` search, `j`/`k` move the selection and the reader follows, `x`
selects for bulk. In the finish flow: Enter accepts the step, Space replays
the clip, `1` to `3` pick a "looks like" chip, `s` skips a voice,
`a`/`u`/`n`/`p` work on flags as today, `]` and `[` move between meetings to
finish. Escape backs out one layer. No key fires while typing.

## Navigation and search

Search moves to the top of the list pane. Typing filters the list; full-text
hits appear as a "Said in" group with mono timestamps, and Enter opens the hit
in the reader cued to that moment. The two selects become chips under the box
(All, Work, Personal, Has flags, Has voices), each visible and one click to
clear. Selecting a row never leaves the list.

## Empty and first-run states

An empty library shows a three-line setup check in the reader (recorder
permission, watched folder, your own voice enrolled, each with its drawer fix
button), then "Record from the menu bar, or drop an audio file here." Otherwise the
reader opens the top "To finish" meeting, or the newest one. An empty search reads "No matches."

## Tokens

Indigo Blueprint carries over whole: palette, four hue meanings (indigo acts,
teal asks, green is done, red records or destroys), Figtree, 6px cards and
4px chips, hairlines instead of shadows, the header dot grid, and the
`tools/token_contrast.py` gate. Additions: `--sel` (accent-soft ground and a
3px accent edge) marks the selected row, and the finish strip reuses teal for
open steps, green for done, indigo for current. One change: list rows get
denser (title 15px, meta 13px, one summary line), and the 17.5px title moves
to the reader header at 22px. The 13px floor holds; the size test in
`tests/test_new_shell.py` needs the new values.

## Two alternates, for contrast

**Finish mode.** Keep today's single-column timeline and add a full-screen
focus overlay that walks every open title, voice, and flag across meetings as
one queue, like flashcards. It needs the least layout work. It loses the library and the summary while
working, and it makes the everyday loop a mode the user must enter and leave.

**Day journal.** Make the day the unit: each day is one page holding its
meetings' summaries and next steps in sequence, with naming and flags as
margin notes. It reads well for "what happened Tuesday". It fits poorly with
search and with voices that span many days, and it breaks the one-object rule
that the rest of the system already depends on.

## What stays

The list is in `flows.md`; in short, every rule this proposal does not name.

## Build plan

Each phase ships alone, gated as the relabel phases were: Opus builds,
Sonnet runs QA and the full suite, then review and commit.

1. **Finish flow on today's meeting page.** The step strip, the inline Voices
   step with auto-advance, and "Next to finish". No layout change, every
   endpoint reused, and most of the click savings. New pure functions
   (`finishSteps(meeting)`, `nextToFinish(timeline, current)`) get node tests
   over extracted source, as `npSave` has now. If the timeline row gains a
   `finish` field, a pytest over `gather_state` on the demo seed pins it.
   Shipped 2026-09-27: the strip, the inline Title and Voices steps, the
   Flags hand-off to the verdict bar, and "Next to finish" (`]` / `[`), on
   the unchanged meeting page. See "Finish flow" in `gui/DESIGN.md`.
2. **To finish section and status.** Compact rows replace the tall naming
   rows, the tray folds into Now and To finish, the pill gains its naming
   segment, the tab title counts. Node tests on the section builder and the
   pill text; the tray tests move to the new section.
   Shipped 2026-09-27: Now (the tray's urgent kinds, then the live rows) and
   To finish (compact rows with step dots, "Name N voices" and "Review
   flags") above the accordion, the tray band retired, the pill's naming
   segment, and the `(N) Meetings` tab title. See "To finish and Now" in
   `gui/DESIGN.md`.
3. **Two panes.** The reader mounts beside the list at 900px and up; the
   `#m/<base>` route stays for deep links and narrow windows. Node tests on
   selection and routing; the size and contrast gates update.
4. **Search chips and first run.** Filter chips, the "Said in" group, the
   setup check. A panel Record button waits until someone confirms the macOS
   microphone grant lets the server signal the menu-bar recorder; until then
   recording stays in the menu bar.
