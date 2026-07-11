"""Archive, restore, and permanently delete meetings.

Archiving MOVES a meeting's folder into <meetings_dir>/.archive/<base>/ — same
volume, so the move is one atomic os.rename, and the dot-prefix means
config.meeting_bases() (and therefore every live-membership-gated surface:
the list, search, Ask, export, voice clips, relabel --all) excludes it with no
per-endpoint work. Restore moves it back; nothing inside the folder is touched
in either direction, so a restored meeting is byte-identical.

Delete is permanent (shutil.rmtree) and is the only operation that scrubs the
speaker registries' references — an archived meeting keeps its refs so a
restore brings voice-clip playback straight back.
"""
import json
import os
import shutil
from pathlib import Path

from . import config, manifest


def _is_active(base: str) -> bool:
    """Is the batch writing this meeting right now? (Same guard as rename:
    process_file computed its output paths at start and writes them at the end,
    so moving the folder mid-run would resurrect it at the old location.)"""
    from . import status
    return base in {Path(k).stem for k in status.read().get("active", {})}


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


def _retarget_manifest(old_dir: Path, new_dir: Path):
    """Point manifest entries whose outputs lived in old_dir at new_dir.
    Without this, a source file still sitting in a watched folder
    (keep-original setups) would read as unprocessed after an archive — the
    outputs went missing, so is_processed self-heals to False — and the next
    batch run would silently re-transcribe the meeting the user just archived,
    resurrecting it in the main view. (A batch running right now holds the
    manifest in memory and its next save can clobber this edit; the worst case
    is that narrow keep-original resurrection, never corruption.)"""
    try:
        old_dir, new_dir = Path(old_dir), Path(new_dir)
        m = manifest.load()
        changed = False
        for rec in m["processed"].values():
            outs = rec.get("outputs") or []
            new = [str(new_dir / Path(o).name) if Path(o).parent == old_dir else o
                   for o in outs]
            if new != outs:
                rec["outputs"] = new
                changed = True
        if changed:
            manifest.save(m)
    except Exception:
        pass  # manifest hygiene must never block the move itself


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
    _retarget_manifest(old_dir, config.archive_dir() / new_base)
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
    _retarget_manifest(src_dir, config.meeting_dir(new_base))
    if new_base != base:
        from . import identify, unknowns
        try:
            unknowns.rename_meeting_refs(base, new_base)
            identify.rename_source_refs(base, new_base)
        except Exception as e:
            import sys
            print(f"   restore: registry references not updated ({e})",
                  file=sys.stderr)
    return {"ok": True, "base": new_base}


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
        shutil.rmtree(target)
    try:
        from . import unknowns
        unknowns.forget_meeting_refs(base)
    except Exception:
        pass

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
