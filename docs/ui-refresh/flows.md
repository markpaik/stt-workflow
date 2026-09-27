# Everyday flows: before and after

Counts come from the live panel on 2026-09-27 (`/api/state`, the rendered
page, and `gui/static/app.js`). An action is one click or one command key;
typing a name does not count. A surface is a distinct place the eye has to
go: the menu bar, the timeline, the naming slide-over, the meeting page, the
reader pane. The worked case is one new 47-minute meeting (the library's
median) with a good suggested title, two unknown voices, 6 substantial flags,
and 20 minor ones.

| Task | Today: actions | Today: surfaces | Today: where it stalls | Proposed: actions | Proposed: surfaces | What changes |
|---|---|---|---|---|---|---|
| 1. Record | 4 (menu bar icon, Start; icon, Stop and save) | 1 (menu bar; pill mirrors it) | Nothing in the panel starts a recording | 4 (same) | 1 | Unchanged until a panel Record button is shown to work with the mic grant (phase 4) |
| 2. Wait for processing | 0 | 2 (header pill, pinned row) | "naming 3 of 7" hides while anything transcribes; a background tab gives no sign when a meeting is ready | 0 | 1 (Now block) | Stages listed in one place; naming is its own pill segment; tab title shows `(1)` |
| 3. Name the meeting and its voices | 7 (per voice: "?" chip, play, Save; then Accept) | 3 (row form, slide-over opened twice) | Slide-over closes after each save; ready rows lose their "?" chips, so voices must be named before Accept or from the transcript legend | 4 (open, Enter title, Enter, Enter) | 1 (reader) | Inline Voices step; clip plays on step entry; Enter saves and advances |
| 3b. Name one backlog voice (31 waiting) | 3 each, 93 total | 2 (tray, slide-over) | Tray "Name →" opens only the first of 31; each save sends the user back to the tray | 1 each, 32 total (one click to start the queue) | 1 | Voice queue with auto-advance |
| 4. Review the flags | 9 ("6 to check", Accept minor, 6 verdicts, Escape) | 2 (timeline, meeting page) | After the last flag the user stays put; the next meeting takes Escape, a hunt, and a click; the >8 tray line only filters | 8 (Accept minor, 6 verdicts, `]`) | 1 | Flags step follows Voices on its own; `]` moves to the next meeting to finish |
| 5. Read the summary | 1 (click row to peek, or hover and Open) | 1 (row peek) or 2 (page) | Peek and page show the summary in two different layouts | 0 after finishing; 1 (select) for any other meeting | 1 | Summary is under the finish strip in the reader; `j`/`k` walk summaries with the list in view |
| 6. Find a past meeting | 3 (`/`, click hit, Escape back); browsing a folded month is also 3 (year, month, row) | 2 (timeline, meeting page) | Opening a hit replaces the list | 2 (`/`, Enter) | 1 | Reader opens beside the list; filters become visible chips |
| **Whole loop, tasks 1 to 5** | **21** | **4** | | **16** | **2** | |

## What stays from the current design

- A meeting is one object whose row changes state in place; no separate queue
  or results screens.
- The status pill as the one summary of pipeline state, hidden when idle.
- The pinned actionable cluster above the dated library, in its fixed order.
- The year-to-month accordion, its defaults, `stt_groups` persistence, and
  unmounted folded sections.
- Indigo Blueprint: palette, the four hue meanings, Figtree, radii,
  hairlines instead of shadows, the dot grid, and the 4.5:1 contrast gate.
- Type rules: mono only for true data, uppercase eyebrows at .08em, the 13px
  floor.
- The verdict bar: fixed position, document order, auto-advance on verdicts,
  hold on edits, "Accept N minor", the two-step "Accept all", and its keys.
- The naming preview line, the "looks like" chips, the after-save toast, and
  the row flash on relabel.
- Form before evidence in every naming surface.
- Inline click-to-edit title and date.
- Hover reveals actions without covering text; checkboxes and the floating
  bulk bar.
- The drawer (Settings, Speakers, History, Archive) and the Process popover as
  the only machinery surfaces.
- Drag-and-drop queueing and the drop veil.
- No banners; outcomes are row states; urgent items one per line, chronic
  items aggregated.
- Keys never fire while typing; Escape backs out one layer.
- The `#m/<base>` route, kept for deep links and narrow windows.
