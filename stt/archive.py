"""Archive, restore, and permanently delete meetings.

Archiving MOVES a meeting's folder into <meetings_dir>/.archive/<base>/ — same
volume, so the move is one atomic os.rename, and the dot-prefix means
config.meeting_bases() (and therefore every live-membership-gated surface:
the list, search, Ask, export, voice clips, relabel --all) excludes it with no
per-endpoint work. Restore moves it back; nothing inside the folder is touched
in either direction, so a restored meeting is byte-identical.

Delete is permanent and goes through purge_meeting_dir — the ONE removal path
that both rmtree's the folder and scrubs the unknowns registry's references
(run_batch's shell sweep uses it too). An archived meeting keeps its refs so a
restore brings voice-clip playback straight back.
"""
import json
import os
import shutil
import sys
from pathlib import Path

from . import config, manifest


def purge_meeting_dir(path: Path):
    """The one shared exit for PERMANENTLY removing a meeting folder: rmtree
    plus a scrub of the unknowns registry's references to that base. Every
    permanent removal — the user's Delete (delete_meeting) AND the batch's
    interrupted-run shell sweep (run_batch._sweep_shells) — must come through
    here: a removal path that skips the scrub leaves unknowns advertising
    'heard in N meetings' against folders that no longer exist, with zero
    playable clips behind the claim. The scrub is best-effort: the folder is
    already gone, so a registry hiccup must not turn the delete into an error
    (the panel re-derives liveness per poll and hides dead refs anyway)."""
    shutil.rmtree(path)
    try:
        from . import unknowns
        unknowns.forget_meeting_refs(path.name)
    except Exception as e:
        print(f"   delete: unknown-speaker references not scrubbed ({e})",
              file=sys.stderr)


def _is_active(base: str) -> bool:
    """Is the batch writing this meeting right now? (Same guard as rename:
    process_file computed its output paths at start and writes them at the end,
    so moving the folder mid-run would resurrect it at the old location.)"""
    from . import status
    return status.meeting_active(base)


def _unique_slot(parent: Path, base: str) -> str:
    if not (parent / base).exists():
        return base
    i = 2
    while (parent / f"{base} ({i})").exists():
        i += 1
    return f"{base} ({i})"


def _move(base: str, src_dir: Path, dst_parent: Path) -> str:
    """Move a meeting folder under dst_parent, keeping the <base>/<base>.*
    invariant. Uniquifies on collision (renaming the inner files by prefix,
    like rename_meeting). Returns the base it landed under. Same-volume
    os.rename, so the move itself is atomic."""
    new_base = _unique_slot(dst_parent, base)
    dst_parent.mkdir(parents=True, exist_ok=True)
    dst = dst_parent / new_base
    os.rename(src_dir, dst)
    if new_base != base:
        prefix = base + "."
        for f in sorted(dst.iterdir()):
            if f.is_file() and f.name.startswith(prefix):
                f.rename(dst / (new_base + f.name[len(base):]))
    return new_base


def archive_meeting(base: str) -> dict:
    """Move a live meeting into the archive. Returns {ok, base?, error?}."""
    from . import review
    if base not in config.meeting_bases():
        return {"ok": False, "error": f"no meeting '{base}'"}
    if _is_active(base):
        return {"ok": False,
                "error": "this meeting is being processed right now — try again in a moment"}
    old_dir = config.meeting_dir(base)
    with review.lock_meeting(base):  # serialize with relabel/review/rename
        if not old_dir.is_dir():
            return {"ok": False, "error": f"no meeting folder for '{base}'"}
        new_base = _move(base, old_dir, config.archive_dir())
    manifest.retarget(old_dir, config.archive_dir() / new_base, base, new_base)
    return {"ok": True, "base": new_base}


def restore_meeting(base: str) -> dict:
    """Bring an archived meeting back into the main view. If a live meeting has
    taken the name since (possible for pre-feature names; new names are kept
    unique across live + archive), it restores as '<base> (2)' and the speaker
    registries' references follow the new name."""
    from . import review
    if base not in config.archived_bases():  # membership gate — never a raw path
        return {"ok": False, "error": f"no archived meeting '{base}'"}
    src_dir = config.archive_dir() / base
    with review.lock_meeting(base):
        new_base = _move(base, src_dir, config.meetings_dir())
    manifest.retarget(src_dir, config.meeting_dir(new_base), base, new_base)
    if new_base != base:
        from . import identify, unknowns
        try:
            unknowns.rename_meeting_refs(base, new_base)
            identify.rename_source_refs(base, new_base)
        except Exception as e:
            import sys
            print(f"   restore: registry references not updated ({e})",
                  file=sys.stderr)
    # a meeting archived before the naming convention restores under its plain
    # legacy name — which would shadow its whole recurring series again. Stamp
    # it on the way back in (apply_meeting_edits moves the folder AND drags the
    # registries/manifest along); best-effort, the restore itself already stands.
    try:
        from . import dates, summarize
        d = json.loads(config.meeting_file(new_base, ".json").read_text())
        if d.get("date") and dates.meeting_date(new_base) is None:
            r = summarize.apply_meeting_edits(new_base, date=d["date"])
            if r.get("ok"):
                new_base = r["base"]
    except Exception:
        pass
    return {"ok": True, "base": new_base}


def drop_audio(base: str) -> dict:
    """Delete a meeting's stored audio, keeping the transcript. The audio is the
    bulk of a meeting's size, so this is the cheap win once a transcript is good.

    Two consequences, neither reversible, both worth stating plainly to the caller:
    Reprocess (Redo) becomes impossible — it re-transcribes FROM this file — and
    the ▶ voice-sample playback for any speaker whose sample was sourced from this
    meeting stops working, since those clips are cut from this audio on demand.
    The voiceprints themselves are embeddings and keep identifying people
    perfectly; only the ability to hear the clip dies."""
    from . import review
    if base not in config.meeting_bases():
        return {"ok": False, "error": f"no meeting '{base}'"}
    if _is_active(base):
        return {"ok": False,
                "error": "this meeting is being processed right now — try again in a moment"}
    with review.lock_meeting(base):
        p = config.meeting_audio(base)
        if p is None:
            return {"ok": True, "freed_mb": 0.0, "note": "no stored audio"}
        mb = round(p.stat().st_size / 1e6, 1)
        p.unlink()
    return {"ok": True, "freed_mb": mb}


def delete_meeting(base: str) -> dict:
    """Permanently delete a meeting — live or archived. Scrubs the unknowns
    registry's references (the audio is gone; enrolled voiceprint samples are
    KEPT, their embeddings still identify people in future meetings — only the
    clip playback for samples sourced here dies, and the clip endpoints skip
    non-live meetings gracefully). Returns {ok, note?, error?}."""
    from . import review
    if base in config.meeting_bases():
        if _is_active(base):
            return {"ok": False,
                    "error": "this meeting is being processed right now — try again in a moment"}
        target = config.meeting_dir(base)
    elif base in config.archived_bases():
        target = config.archive_dir() / base
    else:
        return {"ok": False, "error": f"no meeting '{base}'"}

    # read source_file BEFORE the folder is gone: if the original audio still
    # sits in a watched folder, the next run will re-transcribe it — say so
    # honestly instead of letting the meeting silently reappear.
    src_name = None
    try:
        src_name = json.loads((target / f"{base}.json").read_text()).get("source_file")
    except (OSError, ValueError):
        pass

    with review.lock_meeting(base):
        # lock_meeting -> lock_registry is the established order (summarize's
        # rename path nests the same way), so the scrub inside is deadlock-free
        purge_meeting_dir(target)

    note = None
    if src_name:
        for folder in (config.source_dir(), config.recordings_dir()):
            try:
                if (folder / src_name).exists():
                    note = (f"the original audio '{src_name}' is still in a watched "
                            "folder — it will be re-transcribed on the next run "
                            "unless you remove it")
                    break
            except OSError:
                pass
    return {"ok": True, "note": note}
