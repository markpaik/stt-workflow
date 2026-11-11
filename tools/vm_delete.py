#!/usr/bin/env python
"""Show, or run once, the Voice Memos delete after ingest (stt/voicememos.py).

The batch moves each memo it has stored to Voice Memos' Recently Deleted
when STT_VM_DELETE_AFTER_INGEST=1. This tool shows what that step would do
for every memo the sync has copied, and why a memo stays.

  ./run.sh py tools/vm_delete.py                # dry run: the list, nothing written
  ./run.sh py tools/vm_delete.py --dry-run      # the same
  ./run.sh py tools/vm_delete.py --apply        # one delete pass now
  ./run.sh py tools/vm_delete.py --clear-halt   # resume after a stop

The dry run never runs the Shortcut and writes nothing. It may run
`shortcuts list` to say whether the Shortcut is installed.

--apply runs one pass even while the setting is off. It runs the same rules
as the batch: a fresh read of the database before each delete, the title
checks, the Shortcut, and the check afterwards. It refuses while a batch
runs. A memo moves to Recently Deleted, never further.

After a stop (a delete that left another memo missing, or a memo that left
the database), look at Voice Memos and its Recently Deleted first, then run
--clear-halt.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt import config, status, voicememos  # noqa: E402

WAITING = {
    "not_ingested": "the batch has not stored it yet (no meeting in the library carries its id)",
    "meeting_gone": "its meeting is not in the library",
    "no_audio": "its meeting has no stored audio",
    "duration_mismatch": "the stored audio and the memo differ in length",
}


def _line(item) -> str:
    title = item.get("title") or item.get("name") or item["uuid"]
    st, why = item["state"], item.get("reason")
    if st == "candidate":
        if item.get("would") == "delete":
            what = f"would move to Recently Deleted (stored as {item['base']})"
        else:
            code = item["would"].split(":", 1)[1]
            what = "needs a manual delete: " + voicememos.MANUAL_REASONS.get(code, code)
    elif st == "done":
        what = {"deleted": "moved to Recently Deleted earlier",
                "owner": "was already in Recently Deleted",
                "gone": "is no longer in Voice Memos"}.get(why, why)
    elif st == "late":
        what = "is in Recently Deleted now (the last try is confirmed)"
    elif st == "owner":
        what = "is in Recently Deleted already, so nothing runs"
    elif st == "gone":
        what = "is no longer in Voice Memos"
    elif st == "manual":
        what = ("needs a manual delete: "
                + voicememos.MANUAL_REASONS.get(why, why or "")
                + (f" ({item['detail']})" if item.get("detail") else ""))
    elif st == "backoff":
        what = f"waits until {item['detail']} after a failed try ({why})"
    else:
        what = "stays: " + WAITING.get(why, why or "")
        if item.get("detail"):
            what += f" ({item['detail']})"
    return f"  {title}: {what}"


def _setting_lines():
    on = voicememos.enabled() and voicememos.delete_enabled()
    out = [f"{voicememos.DELETE_SETTING} is {'on' if on else 'off'}"
           + ("" if on else ": the batch deletes nothing")]
    why = voicememos.delete_setting_problem()
    if why:
        out.append(f"  {voicememos.DELETE_SETTING}={why}")
    if not voicememos.enabled():
        out.append("  STT_VOICEMEMOS_SYNC=0 also turns the delete off")
    ok, why = voicememos.shortcut_status()
    name = voicememos.shortcut_name()
    out.append(f"Shortcut \"{name}\": " + ("installed" if ok else {
        "missing": "NOT installed (see the README, Voice Memos)",
        "no_shortcuts": f"{voicememos.SHORTCUTS_BIN} is not here",
        "list_failed": "`shortcuts list` failed"}.get(why, why)))
    return out


def _batch_running() -> bool:
    """True while status.json says a batch runs and that batch is alive:
    its pid, or its process group (status.batch_running, as every other
    tool reads it). A hard kill or a power loss skips end_run, so the flag
    stayed true with a dead pid, and --apply refused until the next batch
    ran. This copy checked only the pid, so a --parallel worker that
    outlived a killed parent read as no batch here. A flag with no pid, or
    a pid that cannot be checked, still counts."""
    return status.batch_running()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="the default")
    g.add_argument("--apply", action="store_true", help="run one delete pass now")
    g.add_argument("--clear-halt", action="store_true",
                   help="resume deletes after a stop")
    a = ap.parse_args(argv)

    if a.clear_halt:
        was = voicememos.clear_halt()
        if was is None:
            print("No stop is recorded. Nothing changed.")
        else:
            print(f"Cleared the stop from {was.get('at')}: {was.get('reason')}")
        return 0

    for ln in _setting_lines():
        print(ln)

    if a.apply:
        if _batch_running():
            print("A batch is running. Try again when it ends.")
            return 2
        r = voicememos.delete_pass(force=True, log=print,
                                   warn=lambda s: print(s, file=sys.stderr))
        st = r.get("status")
        print(f"Pass: {st}" + (f" ({r['detail']})" if r.get("detail") else ""))
        print(f"  moved {len(r['deleted'])}, new manual deletes {len(r['manual'])}, "
              f"failed {len(r['failed'])}")
        return 0 if st in ("ok", "idle") and not r["failed"] else 1

    r = voicememos.delete_pass(dry_run=True, force=True)
    st = r.get("status")
    if st != "ok":
        print(f"Dry run stopped: {st}" + (f" ({r['detail']})" if r.get("detail") else ""))
        return 1
    for b in r.get("blocked") or []:
        print({"halted": f"Deletes are STOPPED: {r.get('detail')}. Check Voice Memos, "
                         "then run --clear-halt.",
               "sandbox": "This home reads a library other than this Mac's own "
                          "(STT_HOME or STT_VOICEMEMOS_DIR), so the real Shortcut "
                          "never runs here."}.get(b, b))
    plan = r["plan"]
    if not plan:
        print("No memo has come from Voice Memos yet.")
        return 0
    order = [("would delete", lambda i: i["state"] == "candidate"
              and i.get("would") == "delete"),
             ("needs a manual delete", lambda i: i["state"] == "manual" or (
                 i["state"] == "candidate" and i.get("would") != "delete")),
             ("waiting", lambda i: i["state"] in ("waiting", "backoff")),
             ("resolved on the next pass", lambda i: i["state"] in ("late", "owner", "gone")),
             ("done", lambda i: i["state"] == "done")]
    for head, pick in order:
        rows = [i for i in plan if pick(i)]
        if rows:
            print(f"\n{head} ({len(rows)}):")
            for i in rows:
                print(_line(i))
    n = sum(1 for i in plan if i["state"] == "candidate" and i.get("would") == "delete")
    print(f"\nDry run, nothing written. A pass moves at most "
          f"{voicememos.MAX_DELETES_PER_PASS} memo(s); {n} would go now.")
    return 0


if __name__ == "__main__":
    config.private_umask()
    sys.exit(main())
