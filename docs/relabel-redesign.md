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
   Jobs carry a list of meeting names, coalesce, and report progress in state.
   The work runs on one daemon worker THREAD in the panel process, where the
   models are already warm because the in-request relabel loaded them there.
   The sixty-second kick and the falling-edge watcher go away, and the pending
   flag becomes something the poll consumes once instead of something it
   respawns against. Each timeline row carries its `generated_at`, and the open
   meeting reloads when its own stamp changes.
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

The worker is an IN-PROCESS THREAD, not the child process this plan first
called for. `gui/server._relabel_now` already imports `relabel` and runs
`relabel_one` inside the request, so the punctuator and the speaker models are
already resident in the panel process. A child would load a second copy of them
(~1 GB) to do work this process can do warm, and would need a pipe protocol to
report progress that a thread reports by reading an attribute. `relabel.lock`
still does all the excluding, so nothing outside the panel can tell the
difference.

- `gui/jobqueue.py` (new): `RelabelQueue` with `submit(bases, reason)`,
  `submit_all(reason)`, `snapshot()`, `start()`, `stop(timeout)`, and one
  daemon worker thread. Coalescing: a queued `relabel` job unions the bases of
  later relabel submits for as long as it has not started; a queued `all` job
  absorbs every queued and every later relabel. A RUNNING job is never mutated,
  so its progress can never run backwards or past its own total. Each job holds
  `control.relabel_lock_path()` with a BLOCKING `fcntl.flock` for its whole run
  and publishes `control.relabel_marker()` inside it, so the CLI `relabel.py`,
  `run_batch`'s end-of-run pass, and the panel's own `_relabel_now` still
  exclude it and each other. A `relabel` job calls `relabel.relabel_one` per
  base through `relabel._safe_one` (the same per-meeting boundary a CLI pass
  uses, now returning the exception it swallowed so the job can name the
  meetings that did not take); an `all` job calls
  `relabel._pass_over_everything()`. The worker catches every exception per
  job, records it in `last`, and goes on to the next one.
  `snapshot()` returns `{"running": {kind, reason, total, done, current,
  started} | None, "queued": [{kind, reason, n}], "last": {kind, reason, done,
  finished, failed: [bases], error} | None}`. An `all` job's `total` is None:
  the pass re-lists the library before every step, so its length is not known
  in advance and must not be guessed.
- `gui/server.py`: `start_server` creates and starts the queue into the module
  global `RELABEL_QUEUE`. `_relabel_targets` submits the remaining bases to it;
  `/api/relabel` submits `all`; a meeting-local name that loses the lock
  submits its one meeting. The temp-file `relabel --bases-from` spawn survives
  as the FALLBACK for a process with no queue (`python -m gui.server` without
  `start_server`, and tests that drive `Handler` directly), where dropping the
  work would mean a save that silently never applied.
- The pending flag stays where it is: `relabel.py` still writes it when a CLI
  pass loses the lock, and `run_batch` keeps its end-of-run pass. What changes
  is the panel's side. `gather_state` no longer respawns `relabel --all` on a
  60-second cooldown and hopes; it CONSUMES the flag once — unlink, then
  `submit_all("queued follow-up")` — so the follow-up can neither be dropped
  nor run twice. `_relabel_kicked` is gone.
- `/api/state` gains `jobs` (the snapshot), and `relabel_running` becomes
  `control.relabel_running() or bool(jobs["running"])`: an in-process job has
  no child to discover. `relabel_running.json` stays — it is how a CLI pass
  and a `run_batch` pass announce themselves to the panel.
- Timeline rows gain `generated_at`, the save clock `output.write_json` stamps
  on every transcript write. `/api/transcript` returns it too, so a reload
  records the stamp of exactly the bytes it rendered.
- `app.js`: `relabelWatch` stops watching the falling edge of a library-wide
  boolean (back-to-back passes kept it high, and it said nothing about whether
  THIS meeting had changed) and watches the open meeting's own row stamp
  instead, reloading when it moves and no edit is in progress — the same guard
  list as before: `NP`, `#mcard`, a non-empty `#mfind`, `.renameinput,
  .dateinput`. A held change is not lost: the stamp is only recorded when the
  reload actually happens. The pill reads `jobs.running`: `naming: 3 of 7` when
  a total is known, `applying names…` when it is not.
- Tests (`tests/test_jobqueue.py`, plus the updated
  `tests/test_relabel_targets.py`, `tests/test_new_shell.py`,
  `tests/test_timeline_feed.py`): coalescing into one job; an `all` submit
  absorbing queued relabels; a running job never mutated; progress moving
  mid-job; one raising meeting not abandoning the rest and landing in `last`;
  a job that blows up recorded without killing the worker; the lock and marker
  held for the life of a job; the pending flag consumed into one queued `all`;
  the `jobs` shape in `/api/state`; every timeline row carrying its stamp; and
  the node tests for the watcher — same stamp no reload, another meeting's
  stamp no reload, changed stamp reloads, changed stamp mid-edit holds.

Shipped 2026-09-14.

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

Shipped 2026-09-14. Two design choices are worth stating, because both were
the cheaper of two honest options:

- **Parallel id lists, not a new entry shape.** The plan said registries store
  `{id, base}` pairs. They store `source_ids` beside `sources`
  (`voiceprints/registry.json`) and `meeting_ids` beside `meetings`
  (`voiceprints/unknowns.json`), index-aligned, written through one padding
  helper per file (`identify.aligned_source_ids`, `unknowns.aligned_meeting_ids`).
  Every registry on disk carries plain-string lists and several readers index
  straight into them, so a pair shape would have had to be migrated everywhere
  at once; a parallel list is additive, and an entry with no id list reads
  exactly as it did — base-only. Readers that turn a reference into a path or a
  clip (`review.find_voice_clips`, the panel's enrolled and unknown lists,
  `/api/voice_clips`) go through `identify.resolved_sources` /
  `unknowns.resolved_meetings`, which fall back to `config.base_of(id)` only
  when the stored base no longer exists. The name-chase stays: it keeps the
  stored name readable. It is simply no longer the only thing between a rename
  and a dead ▶ button.
- **Locks key on the id.** `review.lock_meeting` names its file
  `config.lock_key(base)` — the id when the meeting has one, the base when it
  does not. A rename runs UNDER this lock, so the key must not change mid-hold;
  the id does not change when the folder moves, and a second caller who knows
  only the new name still queues on the same file. A rename therefore leaves no
  orphan lock behind, which is what the 262-files-for-101-meetings item below
  was. `config.prune_lock_files` (run by the backfill, so at every panel start
  and every batch) removes any lock whose stem is not the live key of a live
  meeting — including a base-named lock belonging to a meeting that now has an
  id, since `lock_key` can never ask for that name again. A lock somebody is
  holding is never unlinked: it is taken non-blocking first.

Also: `meetings_index.json` maps id to base and is rebuilt by a scan whenever a
lookup misses, so an index deleted or left behind by an out-of-band move heals
itself. `config.id_of` finds the id in a bounded 64 KB head read, which is why
the id is written as the FIRST key of the json — a full parse of every
transcript in the library would make a rebuild cost seconds. The backfill
preserves every other field, `generated_at` included, so the migration does not
reload the open meeting page.

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

- 262 lock files under `.locks` against 101 meetings. Phase 3 cleans them:
  `config.prune_lock_files` runs at the end of the id backfill, which
  `start_server` and `run_batch` call at startup.
- Two PSSP meetings have no `.diar.npz`, so relabel can never update them.
  They need a Redo.
- Nineteen unnamed clusters score above threshold but under the margin. Phase
  5 surfaces them as suggestions.
