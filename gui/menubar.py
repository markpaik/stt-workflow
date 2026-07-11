#!/usr/bin/env python
"""Menu-bar app for the STT pipeline: glanceable queue + stage, manual trigger,
and schedule control. Lives in the menu bar (no dock icon), polls status/queue,
and stays out of the way.

Run:  ./run.sh gui     (or via the menu-bar LaunchAgent (setup.sh gui-install))
"""
import os
import plistlib
import subprocess
from pathlib import Path

import rumps

from stt import config, control, recorder, status
from gui import server as panel

ICON = str(config.PROJECT_DIR / "gui" / "waveform.png")
AGENT = Path.home() / "Library/LaunchAgents/com.stt-workflow.batch.plist"
LABEL = "com.stt-workflow.batch"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STAGE_LABEL = {
    "downloading": "Downloading from iCloud",
    "converting": "Preparing audio",
    "transcribing": "Transcribing",
    "diarizing": "Identifying speakers",
    "verifying": "Verifying (second opinion)",
    "writing": "Writing transcript",
    "summarizing": "Writing summary",
    "done": "Done",
    "queued": "Queued",
}


def _short(name, n=34):
    name = name.rsplit(".", 1)[0]
    return name if len(name) <= n else name[: n - 1] + "…"


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _batch_running():
    # group-aware: also true while orphaned --parallel workers are alive, so the
    # menu bar never shows idle while multi-GB processes are still running.
    # snapshot() shares a short-lived cache with the control panel's poll.
    try:
        from stt import control
        return bool(control.snapshot()["pids"])
    except Exception:
        return False


def read_schedule():
    src = AGENT
    try:
        d = plistlib.loads(src.read_bytes())
        sci = d.get("StartCalendarInterval", {})
        return {"hour": sci.get("Hour"), "minute": sci.get("Minute", 0) or 0,
                "installed": AGENT.exists()}
    except Exception:
        return {"hour": None, "minute": 0, "installed": AGENT.exists()}


def gather():
    """Combine live status + iCloud queue + panel-queued runs + meetings into
    one display model."""
    from stt import jobs, manifest
    st = status.read()
    running = (bool(st.get("running")) and _pid_alive(st.get("pid"))) or _batch_running()
    active = st.get("active", {}) if running else {}
    m = manifest.load()
    queued = []
    try:
        for p in sorted(config.ICLOUD_DIR.iterdir()):
            if (p.is_file() and p.suffix.lower() in config.AUDIO_EXTS
                    and not p.name.startswith(".") and p.name not in active
                    and not manifest.is_processed(m, p.name, p.stat().st_mtime)):
                queued.append(p.name)
    except Exception:
        pass
    try:
        queued_jobs = jobs.items()  # Redo / hand-picked runs waiting behind this one
    except Exception:
        queued_jobs = []
    try:
        done_count = len(config.meeting_bases())
    except Exception:
        done_count = 0
    # one shared source of truth with the panel (they used to answer this
    # separately, and drifted): only a capture that is genuinely running shows.
    rec = recorder.live_recording()
    return {"running": running, "active": active, "queued": queued,
            "queued_jobs": queued_jobs,
            "paused": control.is_paused(), "recording": rec,
            "recent": st.get("recent", [])[:6], "done_count": done_count,
            "schedule": read_schedule()}


# Flat, monochrome recording glyphs. Text (not emoji), so they take the menu
# bar's own tint and stay legible in light, dark, and over a tinted wallpaper —
# where the 🔴 emoji always stamped the same saturated red.
REC_LIVE = "●"      # ● a filled dot: the universal "recording" mark
REC_PAUSED = "‖"    # ‖ two bars: paused


def _notify(title, subtitle, message):
    """Show a user notification RELIABLY, and never raise.

    rumps.notification needs a bundled app (an Info.plist for
    UNUserNotificationCenter) and THROWS from this bare python process — so
    every banner this app ever posted ('Recording saved', 'Recording problem',
    'Run started') silently died. osascript displays from any process; the
    argv-safe on-run pattern (same as gui/server._osascript) keeps meeting
    names and error text out of the script source."""
    try:
        subprocess.run(
            ["/usr/bin/osascript",
             "-e", "on run argv",
             "-e", "display notification (item 1 of argv) "
                   "with title (item 2 of argv) subtitle (item 3 of argv)",
             "-e", "end run",
             "--", str(message or ""), str(title or ""), str(subtitle or "")],
            capture_output=True, timeout=10)
    except Exception:
        pass  # a notification must never break the app it is notifying for


def _fmt_hms(secs):
    """mm:ss (or h:mm:ss) for the recording readout."""
    secs = max(0, int(secs or 0))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class STTMenuBar(rumps.App):
    def __init__(self):
        super().__init__("STT", icon=ICON, template=True, quit_button=None)
        self._frame = 0
        self._last_sig = None
        self._stall_warned = None  # caf path already warned about (once per capture)
        panel.start_server()  # local control panel at http://127.0.0.1:8737
        try:  # a recording orphaned by a crash/forced quit: salvage its audio
            saved = recorder.recover_orphans()
            if saved:
                _notify("STT workflow", "Recovered a recording",
                        f"“{saved[0]}” was saved after an interrupted session.")
        except Exception:
            pass
        self.timer = rumps.Timer(self.refresh, 2.0)
        self.timer.start()
        self.refresh(None)

    @staticmethod
    def _signature(s):
        rec = s.get("recording") or {}
        return (s["running"], s["paused"], bool(s.get("recording")),
                bool(rec.get("paused")),  # pause flips the menu item, so rebuild
                tuple(sorted((n, a["stage"]) for n, a in s["active"].items())),
                tuple(s["queued"]), tuple(j.get("at") for j in s.get("queued_jobs", [])),
                tuple((r["name"], r.get("ok")) for r in s["recent"]),
                (s["schedule"].get("hour"), s["schedule"].get("minute")))

    def refresh(self, _):
        try:
            s = gather()
            self._frame = (self._frame + 1) % len(SPINNER)
            # title (badge/spinner) updates every tick; it doesn't disturb an open menu
            rec = s.get("recording")
            if rec:
                # a live recording owns the title (its clock ticks every 2s);
                # a background batch still shows its spinner alongside.
                # Flat monochrome glyphs, not the 🔴 emoji: they inherit the menu
                # bar's own tint (so they read correctly in light, dark, and under
                # a tinted wallpaper) instead of stamping a saturated red dot.
                busy = SPINNER[self._frame] if s["running"] else ""
                glyph = REC_PAUSED if rec.get("paused") else REC_LIVE
                clock = _fmt_hms(recorder.elapsed_seconds(rec))
                stalled = recorder.capture_stalled(rec)
                if stalled and self._stall_warned != rec.get("caf"):
                    # say it NOW, ten seconds in — not at stop, when the meeting
                    # audio is already gone for good
                    self._stall_warned = rec.get("caf")
                    _notify(
                        "STT workflow", "Recording is NOT capturing audio",
                        "Grant Microphone and 'System Audio Recording Only' in "
                        "System Settings > Privacy & Security, then stop and "
                        "start again. (Rebuilding the recorder resets these.)")
                self.title = f" {glyph}{' ⚠' if stalled else ''} {clock} {busy}".rstrip()
            elif s["running"]:
                n_act = max(1, len(s["active"]))
                etas = [status.estimate_progress(a, n_act)[1] for a in s["active"].values()]
                etas = [e for e in etas if e is not None]
                eta_txt = f" {_fmt_eta(sum(etas) / n_act)}" if etas else ""
                self.title = f" {SPINNER[self._frame]}{eta_txt}"
            elif s["paused"]:
                self.title = " ⏸"
            elif s["queued"]:
                self.title = f" {len(s['queued'])}"
            else:
                self.title = ""
            # rebuild the dropdown ONLY when its contents change (no flicker while open)
            sig = self._signature(s)
            if sig != self._last_sig:
                self._render(s)
                self._last_sig = sig
        except Exception:
            pass  # a menu-bar app must never crash on a transient read

    def _render(self, s):
        spin = SPINNER[self._frame]
        self.menu.clear()
        items = []

        # --- status header ---
        active = s["active"]
        if active:
            n_act = max(1, len(active))
            for name, a in sorted(active.items()):
                pct, eta = status.estimate_progress(a, n_act)
                extra = (f"  ·  {round(pct * 100)}%  ·  ≈{_fmt_eta(eta)}"
                         if pct is not None else "")
                items.append(_disabled(f"{spin}  {_short(name, 24)}  —  "
                                       f"{STAGE_LABEL.get(a['stage'], a['stage']).lower()}{extra}"))
        elif s["running"]:
            items.append(_disabled(f"{spin}  Processing…"))
        elif s["paused"]:
            items.append(_disabled("⏸  Automatic runs paused"))
        elif s["queued"]:
            items.append(_disabled(f"○  {len(s['queued'])} waiting"))
        else:
            sched = s["schedule"]
            when = (f"next run {_fmt12(sched['hour'], sched['minute'])}"
                    if sched.get("hour") is not None and sched.get("installed") else "idle")
            items.append(_disabled(f"✓  Up to date  ·  {when}"))
        items.append(rumps.separator)

        # --- queue ---
        qj = s.get("queued_jobs", [])
        qj_status = "starts after current run" if active else "starting…"
        for j in qj[:8]:
            items.append(_disabled(f"↻  {_short(j.get('label') or 'run', 30)}  —  {qj_status}"))
        for f in s["queued"][:8]:
            items.append(_disabled(f"○  {_short(f)}  —  queued"))
        if len(s["queued"]) > 8:
            items.append(_disabled(f"    +{len(s['queued']) - 8} more"))
        if not active and not s["queued"] and not qj:
            items.append(_disabled("○  Nothing waiting"))

        # --- recent ---
        if s["recent"]:
            items.append(rumps.separator)
            items.append(_disabled("Recent"))
            for r in s["recent"]:
                glyph = "✓" if r.get("ok") else "⚠"
                mi = rumps.MenuItem(f"{glyph}  {_short(r['name'])}", callback=self._open_recent)
                mi._stt_name = r["name"]
                items.append(mi)

        # --- recorder ---
        rec = s.get("recording")
        items.append(rumps.separator)
        if rec:
            paused = bool(rec.get("paused"))
            clock = _fmt_hms(recorder.elapsed_seconds(rec))
            items.append(_disabled(
                f"{REC_PAUSED if paused else REC_LIVE}  "
                f"{'Paused' if paused else 'Recording'}  ·  {clock}"))
            items.append(rumps.MenuItem(
                "▶  Resume recording" if paused else "‖  Pause recording",
                callback=self.toggle_pause_recording))
            items.append(rumps.MenuItem("■  Stop and save", callback=self.stop_recording))
        elif recorder.available():
            items.append(rumps.MenuItem(f"{REC_LIVE}  Start recording",
                                        callback=self.start_recording))
        else:
            items.append(_disabled(f"{REC_LIVE}  Recording unavailable — build it (see Open logs)"))

        # --- controls ---
        items.append(rumps.separator)
        items.append(rumps.MenuItem("Open Control Panel…", callback=self.open_panel))
        if s["running"]:
            items.append(rumps.MenuItem("Stop processing", callback=self.stop_run))
        else:
            items.append(rumps.MenuItem("Run now", callback=self.run_now))
        pause_label = "Resume automatic runs" if s["paused"] else "Pause automatic runs"
        items.append(rumps.MenuItem(pause_label, callback=self.toggle_pause))
        items.append(rumps.separator)
        items.append(rumps.MenuItem("Open transcripts folder", callback=self.open_meetings))
        items.append(rumps.MenuItem("Open logs", callback=self.open_logs))
        items.append(rumps.separator)
        items.append(rumps.MenuItem("Quit", callback=rumps.quit_application))

        for it in items:
            self.menu.add(it)

    def open_panel(self, _):
        subprocess.Popen(["open", f"http://127.0.0.1:{panel.PORT}/"])

    def stop_run(self, _):
        res = control.stop_run()  # blocks briefly: group-kill, verify, escalate
        if not res["stopped"]:
            _notify("STT workflow", "Nothing to stop",
                               "No processing was running.")
        elif res["survivors"]:
            _notify("STT workflow", "Stop incomplete",
                               f"Processes still alive: {res['survivors']} — "
                               "try again or check the control panel.")
        else:
            cq = (f" Cancelled {res['cleared_jobs']} queued run(s)."
                  if res.get("cleared_jobs") else "")
            _notify("STT workflow", "Stopped — verified",
                               ("Had to force-kill a stuck worker. " if res["forced"] else "")
                               + "Nothing left running; memory released. "
                               "The in-flight file will re-run next time." + cq)

    def toggle_pause(self, _):
        if control.is_paused():
            control.resume()
            _notify("STT workflow", "Resumed", "Automatic runs are back on.")
        else:
            control.pause()
            _notify("STT workflow", "Paused",
                               "Nightly/automatic runs will skip until resumed. "
                               "Manual runs still work.")

    # ---- recorder ----
    def start_recording(self, _):
        r = recorder.start()
        if not r.get("ok"):
            _notify("STT workflow", "Could not start recording",
                               r.get("error", "unknown error"))
            return
        self.refresh(None)  # flip the menu/title immediately
        _notify("STT workflow", "Recording",
                           "Capturing this meeting. Grant Microphone and "
                           "'System Audio Recording Only' if macOS asks.")

    def toggle_pause_recording(self, _):
        rec = (gather() or {}).get("recording") or {}
        r = recorder.resume() if rec.get("paused") else recorder.pause()
        if not r.get("ok"):
            _notify("STT workflow", "Recording", r.get("error", "unknown error"))
        self._last_sig = None  # force the menu to redraw with the flipped item
        self.refresh(None)

    def stop_recording(self, _):
        caf = recorder.halt()  # end capture first, so we don't record the stop pause
        if caf is None:
            self.refresh(None)
            return
        # NO naming dialog here. rumps.Window blocks the main thread inside
        # NSAlert.runModal, which FROZE the entire menu bar until it was answered
        # — and it can open behind another window or on another Space, where it is
        # easy to never see. The capture is saved immediately under a default name
        # and queued; you name it in the panel, which also suggests a title from
        # the transcript and stamps the date into the filename for you.
        r = recorder.finalize(caf, None)
        self.refresh(None)
        if r.get("ok"):
            self._spawn_batch()  # process it now; a no-op if a batch holds the lock
            _notify("STT workflow", "Recording saved",
                               f"“{r['name']}” — processing. Rename it in the panel.")
        else:
            # a failure must NEVER be silent — an empty capture (TCC denied, e.g.
            # after a recorder rebuild reset the grants) looked like "nothing
            # happened", and the user only found out the meeting was lost later
            _notify("STT workflow", "Recording could NOT be saved",
                    r.get("error", "unknown error"))

    # ---- actions ----
    def _spawn_batch(self):
        """Kick a run. Harmless when one is already going: the spawn hits the
        single-instance lock and exits, and the running batch's end-of-run rescan
        picks up anything that landed while it worked (a recording finished
        mid-batch queues itself this way instead of waiting for the next trigger)."""
        # manual runs override the pause flag and hold the Mac awake
        subprocess.Popen(["caffeinate", "-i", "-s",
                          str(config.PROJECT_DIR / "run.sh"), "batch", "--ignore-pause"],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run_now(self, _):
        self._spawn_batch()
        _notify("STT workflow", "Run started", "Processing any new recordings…")

    def _open_recent(self, sender):
        name = getattr(sender, "_stt_name", "")
        txt = config.meeting_file(Path(name).stem, ".txt")
        subprocess.Popen(["open", str(txt if txt.exists() else config.meetings_dir())])

    def open_meetings(self, _):
        subprocess.Popen(["open", str(config.MEETINGS_DIR)])

    def open_logs(self, _):
        log = config.LOG_DIR / "stt.err.log"
        subprocess.Popen(["open", str(log if log.exists() else config.LOG_DIR)])


def _disabled(title):
    return rumps.MenuItem(title, callback=None)


def _fmt_eta(sec):
    if sec is None:
        return ""
    sec = max(60, int(sec))
    if sec < 3600:
        return f"{round(sec / 60)}m"
    return f"{sec // 3600}h{round(sec % 3600 / 60):02d}m"


def _fmt12(hour, minute):
    if hour is None:
        return "not set"
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {ampm}"


if __name__ == "__main__":
    STTMenuBar().run()
