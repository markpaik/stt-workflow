# Meeting store contract

This is what the meeting store promises to the programs that read it: the panel, the batch, `chief-of-staff`, `202610_meeting_followup`, and `board-meetings`. If a change breaks a rule here, update this file in the same commit.

## Layout

- The store is one folder, `~/Projects/brain/meetings` by default (`config.meetings_dir()`, or `STT_MEETINGS_DIR`, or `STT_HOME/meetings`).
- A meeting is the folder `<store>/<base>/` that holds `<base>/<base>.json`. The store is one level deep. There are no year folders.
- A folder with no `<base>.json`, or one whose json is not readable, is no meeting: no reader lists it. `tools/doctor.py check` reports each one (`folder_without_json`, `unreadable_json`), and the batch's start-up sweep removes a folder that holds neither a json nor audio.
- Readers skip every folder whose name starts with a dot. `.archive/` holds archived meetings, and `.locks/` holds lock files.
- Every file of a meeting carries the folder's name: `<base>.json` (the record of truth), `<base>.txt` (the readable transcript), the audio (`.m4a`, or another type from `config.AUDIO_SUFFIXES`), and optional caches and sidecars (`.diar.npz`, `.emb.npz`, `.reviews.json`, `.verify.json`).
- The folder name is `YYYY-MM-DD <title>`, plus ` (2)`, ` (3)`, and so on for a same-name twin on the same date: `2026-08-03 Weekly Check-in (2)`. The date is the stored `date`. Twin numbers are never shuffled when a sibling moves or is deleted (the registries name meetings by folder), so a ` (2)` can stand alone. Its next title or date edit drops the number once the plain name is free.
- Folders named before that rule read `<title> MMDDYYYY`. Every reader takes both forms (`stt/dates.py`), so an old folder, a typed name, or a dropped file still parses. Only the ISO form is written: the next title or date edit moves an old folder to it, and `tools/iso_rename.py` moves the whole store.
- A stamp typed in a title, in either form (`Review 06152026` or `2026-06-15 Review`), becomes the stored date and moves to the front. A future typed stamp names an event, so it stays in the title and is not stored. The older recorder name `Recording 07112026 1814` loses its mid-name date: `2026-07-11 Recording 1814`.
- The `.txt` header line opens with the meeting's date and title (`output.header_name`), and every rename refreshes it.

## Stable keys

A reader may rely on these keys in `<base>.json`.

| Key | Meaning | Changes when |
|---|---|---|
| `meeting_id` | 16 hex characters, the meeting's identity. It is the first key in the file | Never |
| `title` | The folder name without its date stamp and twin suffix | A title edit, a date edit that renames, or a Redo, always to match the folder |
| `date` | The meeting day, ISO. It is the day in the folder stamp | A date edit or a date correction, which also renames the folder |
| `date_source` | Where `date` came from: `typed`, `recorder`, `short_typed`, `mtime`, or `manual` | With `date` |
| `recorded_at` | The true start, ISO with offset, or null when no source gives one. When set, its day equals `date` | With `date` |
| `source_name` | The raw arrival name, or null when no record keeps it | Never, once written |
| `audio_fp` | `stt.dupes.fingerprint` of the stored audio, taken once | Never, once written |
| `processed_at` | When transcription last ran | A Redo |
| `arrived_at` | When the meeting first arrived | Never |
| `reviewed` | A person looked at the meeting | A person's action only |
| `title_state`, `title_source`, `route` | See `stt/titles.py` | A person's Keep or edit, or the amnesty |
| `ai_summary` | The description as one text: the gist, then the Decisions and Open questions lists (an older meeting holds a prose summary). Read from the whole transcript since Phase 4G | A summary run (Generate, the batch after a meeting, or tools/retitle_all.py --apply) |
| `ai_gist`, `ai_decisions`, `ai_open_questions`, `ai_tags`, `ai_series`, `ai_topic`, `ai_person`, `ai_tmpl`, `ai_model` | The description's parts, its catalog series, and its speaker template (stt/speaker_text.py) | A summary run. A speaker change re-renders the names with no model call |
| `ai_stale` | A Redo left the description out of date. The batch regenerates it when idle | A Redo sets it; a summary run clears it |
| `ai_next_steps` | Removed 2026-11-05. Older meetings may still hold the list. Every summary run deletes it | A summary run deletes it |
| `ai_summary_parts` | 1 when the summary read the transcript in one prompt, else how many parts the model noted first. Absent on a summary from before Phase 4G | With `ai_summary` |
| `location_note` | The default place name Voice Memos gave a synced memo (a street address or a listed place), kept as a note because it is not a title | Never, once written |
| `manual_next` | The number the next person added by hand to a line takes (`MANUAL_n`). A line edit drops a hand-added person once no line names them, and that number is never handed out again | A line edit adds or drops a hand-added person |
| `vm_uuid` | The Voice Memos id of a synced memo. A purge blocks it so the sync never copies it again, and the delete after ingest moves a memo only while a library meeting carries its id | Never, once written |

Old code can ignore any key it does not know. A meeting from before a key simply lacks it, and every reader must accept that.

A `date_source` of `mtime` or `short_typed` means the date is a guess. The panel shows those dates with a "Date uncertain" chip.

## Invariants

1. The folder name equals `dates.restamp(title, date)` plus the twin suffix. `stt/doctor.py` reports any folder that breaks this as `name_mismatch`.
2. A meeting has exactly one live folder. `meetings_index.json` maps `meeting_id` to that folder, and it is a cache that a scan can rebuild.
3. `search.db` (`config.SEARCH_DB`, under `PROJECT_DIR`, mode 600) is the full-text index behind `/api/search`. It holds the live meetings only, and it is a cache: every search first re-reads each `<base>.json` whose mtime or size changed and drops the rows of a folder that is gone. `python -m stt.search --rebuild` builds it again from the jsons alone. A json that parses but has a shape the index cannot read is recorded with no lines until it changes, so one bad meeting never turns the index off for the rest. When the index finds nothing or fails, the search falls back to a substring scan.
4. `group_overrides.json` (`PROJECT_DIR`, mode 600) holds the "Not part of this group" corrections, `{meeting_id: {"not_in": [group key]}}`, keyed on the folder name only for a meeting with no id. Groups themselves are computed on read (`stt/groups.py`) and stored nowhere. `group_usage.jsonl` (mode 600) counts group opens and group questions, `{at, event}`, with no title or key.
5. `ask_threads/<id>.json` (`PROJECT_DIR`, folder 700, files 600) holds the saved library Ask threads: the scope, and per exchange the question, the answer, its citations (`meeting_id`, the folder name at save time, the line index, and the second), and the closest hits of a nothing-found answer. A first question that finds nothing starts no thread: the page shows its reply and closest hits unsaved. A nothing-found follow-up inside a saved thread is stored there. A citation resolves through `meeting_id`, so a rename keeps it working. `tools/purge.py` deletes every thread that cites a purged meeting.
6. `generated_at` changes on every write by the pipeline or the panel. A store repair (`tools/backfill_keys.py`, `tools/amnesty.py`) leaves it as it was, so an open panel page does not reload.

## Who writes

- `stt/pipeline.py` writes a new meeting and a Redo. A new meeting gets `source_name`, `audio_fp`, `date_source`, and `recorded_at` at its first write, from the same rule that stamps the folder (`provenance.fresh`). A Redo carries every key in `titles.CARRY_KEYS` and writes none of these keys on a meeting that lacks them.
- `run_batch.summarize_one` gives a meeting with no typed title its automatic title once, after the summary (`summarize.apply_auto_title`): the named speakers plus the topic, else the model's draft. The title stays `provisional` with `title_source` `model`.
- `stt/voicememos.py` copies synced memos into `PROJECT_DIR/voicememos/` (mode 700) with a `.vm.json` sidecar, and keeps its ledger in `voicememos_state.json` (mode 600). It reads the Voice Memos library and never writes a file there. With `STT_VM_DELETE_AFTER_INGEST=1`, it moves a stored memo to Recently Deleted through a Shortcut, so the Voice Memos app makes that change itself.
- `tools/purge.py` removes one meeting from every store, `search.db` included. It has a dry run and no undo.
- `summarize.apply_meeting_edits` is the one place a title, date, category, review state, or title state changes. A date edit also writes `date_source`, which is `manual` from the panel and the real source from a repair.
- Store repairs run as tools. Each one has a dry run that writes nothing, a journal in `config.migration_dir()` (mode 700, files mode 600), and an undo that restores a value only where it still holds what the tool wrote:
  - `tools/amnesty.py` relabels titles that wait for a name.
  - `tools/backfill_keys.py` adds `title`, `source_name`, `audio_fp`, `date_source`, and `recorded_at` where they are missing.
  - `tools/fix_dates.py` corrects a file-time date that a recorder tag or a short typed date contradicts.
  - `tools/doctor.py fix` repairs the registries, the manifest, holds, dismissals, and a stale stop flag. `tools/doctor.py check` never writes.
  - `tools/iso_rename.py` moves every folder from `<title> MMDDYYYY` to `YYYY-MM-DD <title>` and points each `.txt` header at the date and title. It refuses while the batch or a relabel runs, resumes from its journal, and has `--undo`.
  - `tools/restamp_folders.py` renames a folder whose name breaks the `YYYY-MM-DD <title>` rule. It keeps the stored date and its `date_source`. It has a dry run but no journal: undo a rename with a date edit in the panel.
- A date passed to `apply_meeting_edits` that equals the stored date changes no `date_source`, unless the caller passes one.

## Run order for the store repairs

Run each step as a dry run first. Then run it with `--apply`.

1. `tools/doctor.py fix`. It relinks the manifest records first, so the backfill finds the raw name of each relinked meeting.
2. `tools/backfill_keys.py`. A `source_name` stored as null fills in later if a manifest record links the meeting, but the `date_source` it chose then stays.
3. `tools/fix_dates.py`.
4. `tools/restamp_folders.py`.
5. `tools/doctor.py fix` again, then `tools/doctor.py check`. The check should report no `name_mismatch` and no `missing_keys`.
- `chief-of-staff`, `202610_meeting_followup`, and `board-meetings` only read the store.

## Rename rules for readers

A title or date edit renames the folder and every file inside it. The rename then updates the voice registries, the manifest, the duplicate list, the id index, and the search index. `renamed_from` holds the previous folder name. A reader that keys on the folder name should fall back to `renamed_from`, and then to `audio_fp` or a live fingerprint of the audio, which a rename never changes.

## Size caps

- `logs/*` rotate at 10 MB and keep 3 gzipped generations.
- `calibration.jsonl` is capped at 10 MB and keeps 1 gzip.

The batch applies both caps at start, by copy and truncate, because launchd keeps its log files open.
