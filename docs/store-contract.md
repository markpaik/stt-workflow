# Meeting store contract

This is what the meeting store promises to the programs that read it: the panel, the batch, `chief-of-staff`, `202610_meeting_followup`, and `board-meetings`. If a change breaks a rule here, update this file in the same commit.

## Layout

- The store is one folder, `~/Projects/brain/meetings` by default (`config.meetings_dir()`, or `STT_MEETINGS_DIR`, or `STT_HOME/meetings`).
- A meeting is the folder `<store>/<base>/` that holds `<base>/<base>.json`. The store is one level deep. There are no year folders.
- Readers skip every folder whose name starts with a dot. `.archive/` holds archived meetings, and `.locks/` holds lock files.
- Every file of a meeting carries the folder's name: `<base>.json` (the record of truth), `<base>.txt` (the readable transcript), the audio (`.m4a`, or another type from `config.AUDIO_SUFFIXES`), and optional caches and sidecars (`.diar.npz`, `.emb.npz`, `.reviews.json`, `.verify.json`).
- The folder name is `<title> MMDDYYYY`, plus ` (2)`, ` (3)`, and so on for a same-name twin. A date already inside the title, such as the recorder's `Recording 07112026 1814`, is replaced in place, so no name carries two dates.

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

Old code can ignore any key it does not know. A meeting from before a key simply lacks it, and every reader must accept that.

A `date_source` of `mtime` or `short_typed` means the date is a guess. The panel shows those dates with a "Date uncertain" chip.

## Invariants

1. The folder name equals `dates.restamp(title, date)` plus the twin suffix. `stt/doctor.py` reports any folder that breaks this as `name_mismatch`.
2. A meeting has exactly one live folder. `meetings_index.json` maps `meeting_id` to that folder, and it is a cache that a scan can rebuild.
3. `search.db` (`config.SEARCH_DB`, under `PROJECT_DIR`, mode 600) is the full-text index behind `/api/search`. It holds the live meetings only, and it is a cache: every search first re-reads each `<base>.json` whose mtime or size changed and drops the rows of a folder that is gone. `python -m stt.search --rebuild` builds it again from the jsons alone. When the index finds nothing or fails, the search falls back to a substring scan.
4. `generated_at` changes on every write by the pipeline or the panel. A store repair (`tools/backfill_keys.py`, `tools/amnesty.py`) leaves it as it was, so an open panel page does not reload.

## Who writes

- `stt/pipeline.py` writes a new meeting and a Redo. A Redo carries every key in `titles.CARRY_KEYS`.
- `summarize.apply_meeting_edits` is the one place a title, date, category, review state, or title state changes. A date edit also writes `date_source`, which is `manual` from the panel and the real source from a repair.
- Store repairs run as tools. Each one has a dry run that writes nothing, a journal in `config.migration_dir()` (mode 700, files mode 600), and an undo that restores a value only where it still holds what the tool wrote:
  - `tools/amnesty.py` relabels titles that wait for a name.
  - `tools/backfill_keys.py` adds `title`, `source_name`, `audio_fp`, `date_source`, and `recorded_at` where they are missing.
  - `tools/fix_dates.py` corrects a file-time date that a recorder tag or a short typed date contradicts.
  - `tools/doctor.py fix` repairs the registries, the manifest, holds, dismissals, and a stale stop flag. `tools/doctor.py check` never writes.
  - `tools/restamp_folders.py` renames a folder whose name breaks the `<title> MMDDYYYY` rule. It keeps the stored date and its `date_source`. It has a dry run but no journal: undo a rename with a date edit in the panel.
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
