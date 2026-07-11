#!/bin/bash
# Install/manage the launchd agents (nightly batch + menu-bar app).
# Plists are GENERATED here with your paths — nothing personal lives in the repo.
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"
U="$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
BATCH_LABEL="com.stt-workflow.batch"
GUI_LABEL="com.stt-workflow.menubar"
BATCH_PLIST="$AGENTS/$BATCH_LABEL.plist"
GUI_PLIST="$AGENTS/$GUI_LABEL.plist"

source_dir() {
  local d=""
  [ -f "$BASE/stt.env" ] && d="$(grep -E '^STT_ICLOUD_DIR=' "$BASE/stt.env" | cut -d= -f2- || true)"
  echo "${d:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/Voice Recordings}"
}

recordings_dir() {
  local d=""
  [ -f "$BASE/stt.env" ] && d="$(grep -E '^STT_RECORDINGS_DIR=' "$BASE/stt.env" | cut -d= -f2- || true)"
  echo "${d:-$HOME/Library/Application Support/com.stt-workflow/recordings}"
}

write_batch_plist() {
  mkdir -p "$AGENTS" "$BASE/logs"
  mkdir -p "$(recordings_dir)"   # launchd can only watch a path that exists
  cat > "$BATCH_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$BATCH_LABEL</string>
  <!-- caffeinate holds the Mac awake for the whole run; venv python by absolute path -->
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string><string>-s</string>
    <string>$BASE/.venv/bin/python</string>
    <string>$BASE/run_batch.py</string>
  </array>
  <key>WorkingDirectory</key><string>$BASE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONPATH</key><string>$BASE</string>
    <key>PYTORCH_ENABLE_MPS_FALLBACK</key><string>1</string>
  </dict>
  <!-- Layered triggers (the in-code flock makes them all safe):
       nightly 02:00 (runs at next wake if asleep) + WatchPaths (new recordings
       while awake) + RunAtLoad (login catch-up; manifest makes it a no-op). -->
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>0</integer></dict>
  <key>WatchPaths</key>
  <array>
    <string>$(source_dir)</string>
    <string>$(recordings_dir)</string>
  </array>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$BASE/logs/stt.out.log</string>
  <key>StandardErrorPath</key><string>$BASE/logs/stt.err.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>5</integer>
</dict>
</plist>
EOF
}

write_gui_plist() {
  mkdir -p "$AGENTS" "$BASE/logs"
  cat > "$GUI_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$GUI_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BASE/.venv/bin/python</string>
    <string>$BASE/gui/menubar.py</string>
  </array>
  <key>WorkingDirectory</key><string>$BASE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONPATH</key><string>$BASE</string>
  </dict>
  <!-- Start at login; restart on crash, but respect a clean Quit (exit 0). -->
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$BASE/logs/gui.out.log</string>
  <key>StandardErrorPath</key><string>$BASE/logs/gui.err.log</string>
</dict>
</plist>
EOF
}

case "${1:-}" in
  install-agent)
    write_batch_plist
    launchctl bootout "gui/$U/$BATCH_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$U" "$BATCH_PLIST"
    echo "Installed + loaded $BATCH_LABEL (nightly 02:00 + folder watch + login catch-up)."
    echo "Grant Full Disk Access to this binary (resolve the symlink):"
    echo "  $(readlink -f "$BASE/.venv/bin/python")"
    echo "Force a test run:  ./setup.sh kick"
    ;;
  uninstall-agent)
    launchctl bootout "gui/$U/$BATCH_LABEL" 2>/dev/null || true
    rm -f "$BATCH_PLIST"
    echo "Removed $BATCH_LABEL."
    ;;
  reload)
    launchctl bootout "gui/$U/$BATCH_LABEL" 2>/dev/null || true
    write_batch_plist
    launchctl bootstrap "gui/$U" "$BATCH_PLIST"
    echo "Reloaded (plists cache — always bootout+bootstrap after editing)."
    ;;
  kick)
    # NEVER use kickstart -k here: -k KILLS an in-flight run mid-file.
    if launchctl print "gui/$U/$BATCH_LABEL" 2>/dev/null | grep -q "state = running"; then
      echo "A run is already in progress — not kicking. Watch: tail -f '$BASE/logs/stt.err.log'"
    else
      launchctl kickstart "gui/$U/$BATCH_LABEL"
      echo "Kicked. Watch: tail -f '$BASE/logs/stt.err.log'"
    fi
    ;;
  status)
    launchctl print "gui/$U/$BATCH_LABEL" 2>/dev/null | sed -n '1,25p' || echo "not loaded"
    ;;
  gui-install)
    write_gui_plist
    launchctl bootout "gui/$U/$GUI_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$U" "$GUI_PLIST"
    echo "Menu-bar app installed + started (look for the waveform icon in your menu bar)."
    ;;
  gui-uninstall)
    launchctl bootout "gui/$U/$GUI_LABEL" 2>/dev/null || true
    rm -f "$GUI_PLIST"
    echo "Menu-bar app removed."
    ;;
  gui-restart)
    launchctl kickstart -k "gui/$U/$GUI_LABEL" && echo "Menu-bar app restarted."
    ;;
  build-recorder)
    # The meeting recorder helper (mic + system audio); Swift via the CLT.
    # Built as a REAL .app bundle, not a bare binary: LaunchServices only knows
    # bundles, and TCC resets address apps by bundle id — a bare binary made
    # "tccutil reset ... com.stt-workflow.recorder" fail with 'No such bundle
    # identifier', which left no way to recover permissions except Settings
    # surgery. As a bundle, the panel's Fix-permissions button works and the
    # privacy panes show 'STT Recorder' instead of an anonymous binary.
    # STILL TRUE: the ad-hoc signature is pinned to the exact build (cdhash), so
    # any rebuild that changes the binary orphans the grants — silently, no
    # prompt, zero frames. Detected and announced below; the menu bar and panel
    # also warn live when a recording is not capturing.
    if ! xcrun --find swiftc >/dev/null 2>&1; then
      echo "swiftc not found — install the Command Line Tools: xcode-select --install"; exit 1
    fi
    APP="$BASE/native/STT Recorder.app"
    BIN="$APP/Contents/MacOS/stt-recorder"
    OLD_CDHASH="$(codesign -dvvv "$BIN" 2>&1 | awk -F= '/^CDHash/{print $2}' || true)"
    echo "Compiling the meeting recorder…"
    mkdir -p "$APP/Contents/MacOS"
    cp "$BASE/native/Recorder-Info.plist" "$APP/Contents/Info.plist"
    swiftc "$BASE/native/recorder.swift" -O -o "$BIN" \
      -framework CoreAudio -framework AudioToolbox
    codesign --force --sign - --identifier com.stt-workflow.recorder "$APP"
    # register the bundle so tccutil/Settings can address it by id
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" || true
    rm -f "$BASE/native/stt-recorder"   # the pre-bundle bare binary
    mkdir -p "$(recordings_dir)"
    echo "Built $APP (recordings land in: $(recordings_dir))"
    NEW_CDHASH="$(codesign -dvvv "$BIN" 2>&1 | awk -F= '/^CDHash/{print $2}' || true)"
    if [ -n "$OLD_CDHASH" ] && [ "$OLD_CDHASH" != "$NEW_CDHASH" ]; then
      echo
      echo "*** The recorder CHANGED — macOS has silently dropped its microphone and"
      echo "*** system-audio permissions. Use the panel's Fix permissions button, or:"
      echo "***   tccutil reset Microphone com.stt-workflow.recorder"
      echo "***   tccutil reset AudioCapture com.stt-workflow.recorder"
      echo "*** then start a recording and grant the prompts again."
    fi
    ;;
  *)
    echo "usage: setup.sh {install-agent|uninstall-agent|reload|kick|status|gui-install|gui-uninstall|gui-restart|build-recorder}"; exit 2 ;;
esac
