# Speaker naming redesign

Status: plan approved 2026-09-14. Phases ship in order, each gated by a QA
pass and a review before commit.

## Why

Every identity change today (enroll, rename a person, merge, remove a sample)
spawns `run.sh relabel --all`: 99 meetings in alphabetical order, about seven
seconds each, seven minutes a pass. The meeting the user just named is not
done first. The pass snapshots basenames at start, so a meeting renamed
mid-pass is skipped with "missing .json" and waits for a queued follow-up
pass. The panel refreshes the open meeting only on the falling edge of one
global "relabel running" boolean, which back-to-back passes keep high. The
meeting basename is the identity in six stores, so every rename has to chase
references, and the chase is best-effort. Unknown-voice ids reuse freed
numbers and link clusters by a greedy first-come rule.

Measured on 2026-09-13: of the seven seconds per meeting, 4.6 are punctuation
restoration, and 4.0 of those are the punctuator reloading its tokenizer once
per segment. One batched call over the same 273 segments takes 0.7 seconds.

## Design

Five changes, in the order they ship.

1. Targeted, fast relabel. Batch punctuation. Keep a centroid index of every
   cluster in the library so an identity change resolves to the handful of
   meetings it can affect. The source meeting relabels synchronously in the
   request. A full pass re-lists basenames per step so a rename mid-pass is
   picked up, not skipped.
2. One job queue. A queue thread inside the panel process owns every relabel.
   Jobs carry a list of meeting ids, coalesce, and report progress in state.
   The work runs in one long-lived child worker that keeps the models warm and
   exits when idle, so the panel process never holds them. The pending flag,
   the sixty-second kick, and the falling-edge watcher go away. Each timeline
   row carries its `generated_at`, and the open meeting reloads when its own
   stamp changes.
3. Stable meeting id. Every meeting json carries an immutable `meeting_id`.
   An index maps id to basename. Registries, review decisions, locks, and job
   payloads reference the id. Rename becomes a folder move plus one index
   update.
4. Unknown-voice linkage. Ids never reuse a freed number. A link records its
   evidence (meeting, cluster, score). Automatic linking needs a high bar, and
   near-misses surface in the panel as "possibly the same voice". A doctor
   command reports and repairs duplicates, dangling refs, and lock leaks.
5. Panel feedback. A naming response says what will change and where. The
   pill shows "naming: 3 of 7". Completion is per meeting, not global.

## Phases and gates

Each phase: Opus builds against the spec below. QA runs behavioral probes
against the shipped code, not regexes, and the full suite. The plan owner
reviews the diff and the QA report, then commits.

### Phase 1: fast and targeted (ships first)

- `stt/punctuate.py`: `restore_segments` normalizes every segment, calls the
  model once with the whole list, then applies `_repair_unk` and the
  word-identity safety check per segment. A segment that fails the check
  keeps its text. `restore` stays for single use. Model load turns on
  `HF_HUB_OFFLINE` when the cached files exist, so a relabel makes no network
  requests.
- `stt/centroids.py` (new): `index()` returns `{base: {cluster: vector}}` from
  every `.diar.npz`, cached in `centroid_index.npz` under the project dir and
  refreshed per base by the cache file's mtime. `targets(print_vec=None,
  names=(), uids=())` returns the sorted set of bases where any cluster scores
  at or above `NAMING_THRESHOLD - 0.10` against the vector, plus every base
  whose json names one of `names`, plus every base in the `meetings` list of
  one of `uids`.
- `relabel.py`: `relabel_all` and `--all` re-list `all_bases()` before each
  step and process any base not yet done, so a meeting renamed mid-pass is
  processed under its new name. A new `--bases-from FILE` reads one basename
  per line, for long target lists.
- `gui/server.py`: every endpoint that spawns `relabel --all` today (name,
  rename_speaker, remove_speaker, merge_speakers, remove_sample,
  reassign_sample) computes targets first. The meeting in the request, when
  there is one, relabels synchronously via `_relabel_now`. The rest spawn as
  `relabel --bases-from`. `/api/relabel` keeps `--all`. The response carries
  `applied` (the base done in the request) and `targets` (the list spawned).
- `gui/static/app.js`: after a save, toast "Named here. Updating N other
  meetings." from the response.
- Tests: batched punctuation equals per-segment output on a fixture and
  refuses a word change; targets finds the right bases on a sandbox library;
  a rename mid-pass is processed under its new name; each endpoint spawns the
  target list and not `--all`.

Exit gate: a naming in a 100-meeting library updates the open meeting within
one poll and the rest within thirty seconds.

Shipped 2026-09-14. Measured on a synthetic 273-segment sandbox meeting (the
real library is not readable from a build session): `relabel_one` with the
model warm takes **0.28 s**, against the 7.1 s profiled on 2026-09-13. The
punctuation step alone is 0.17 s batched against 4.56 s per-segment over the
same 273 segments, byte-identical output. A cold start adds 0.48 s to load the
model, and `HF_HUB_OFFLINE` is on, so the pass makes no network requests.

### Phase 2: one job queue

- `gui/jobqueue.py` (new): a queue thread started by `start_server`. Job
  kinds: `relabel(bases)`, `relabel_all`. Coalescing: a new relabel unions
  its bases into a queued relabel; a queued `relabel_all` absorbs everything.
  Progress: `{kind, total, done, current, started}`.
- `stt/relabel_worker.py` (new): a child process the queue starts on demand
  with `.venv/bin/python -m stt.relabel_worker`. It reads JSON lines on stdin
  (`{"bases": [...]}`), relabels each, writes one JSON line per meeting on
  stdout, and exits after five idle minutes. It takes `relabel.lock` for the
  life of a job, so `run_batch` and the CLI still exclude it.
- `/api/state` gains `jobs`. Timeline rows gain `generated_at`. The pending
  flag, `_relabel_kicked`, `relabel_running.json`, and `relabelWatch` are
  removed. `run_batch` drops its end-of-run `relabel_all`.
- `app.js`: the open meeting reloads when its row's `generated_at` changes
  and no edit is in progress. The pill reads the job progress.
- Tests: coalescing; the worker protocol with a fake worker; a job that dies
  mid-way leaves the queue consistent; the meeting page reloads on its own
  stamp and not on another meeting's.

### Phase 3: stable meeting id

- `meeting_id` (uuid4) written by the pipeline. A one-time backfill stamps
  every existing meeting. `meetings_index.json` maps id to base, rebuilt by a
  scan when missing and updated by rename, archive, restore, delete.
- `config.base_of(meeting_id)` and `config.id_of(base)`. Registries store
  `{id, base}` pairs, reading legacy string entries as base-only. Review
  decisions and verify sidecars keep their filename convention because they
  move with the folder. Locks key on the id, and stale lock files are removed
  on rename and delete.
- Job payloads carry ids. A relabel resolves the current base at the moment
  it processes the meeting.
- Tests: rename with a running relabel job still relabels the meeting once;
  registry references survive two renames; the index rebuilds from a scan.

### Phase 4: unknown-voice linkage

- `unknowns.json` `next` becomes the true monotonic counter. Freed numbers
  are never reused.
- Each unknown carries `links: [{meeting, cluster, score}]`. Auto-link needs
  best at or above 0.70 and, after appending, a minimum pairwise cosine of
  0.60 among the stored samples. Below that the cluster mints and the pair is
  recorded under `suggested: [{uid, score}]` for the panel.
- `tools/registry_doctor.py`: reports duplicates (pairwise at or above 0.95),
  split links (pairwise below 0.60), dangling refs, and orphan lock files.
  `--fix` merges duplicates, unlinks split clusters into fresh ids, and
  removes orphans. The hygiene test runs the doctor's checks.
- Tests: no reuse after a merge; a 0.48 link is refused and recorded as a
  suggestion; the doctor repairs a seeded duplicate.

### Phase 5: panel feedback

- The naming panel previews targets before save: "This names Speaker 3 here
  and in 4 other meetings" with the list.
- The pill shows job progress. A per-row "updated" flash when a meeting's
  stamp changes while the list is visible.
- The unknown-voice panel shows "possibly the same voice" suggestions with a
  one-click link.
- DESIGN.md gains the naming section. Node tests cover the three flows.

## Open items carried from the audit of 2026-09-13

- 262 lock files under `.locks` against 101 meetings. Phase 3 cleans them.
- Two PSSP meetings have no `.diar.npz`, so relabel can never update them.
  They need a Redo.
- Nineteen unnamed clusters score above threshold but under the margin. Phase
  5 surfaces them as suggestions.
