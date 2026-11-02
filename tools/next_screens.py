#!/usr/bin/env python3
"""Screenshot the /next frame in headless Chrome (dev-only, plan Phase 4C).

Every route at 700px and 1440px, in light and dark, against a panel that
is ALREADY running on a synthetic home (tools/demo_seed.py). The tool only
reads pages: it starts Chrome with a throwaway profile, loads
/next?theme=<t>#<route>, and writes one PNG per shot. It never starts or
stops the panel, and it refuses the live panel's port.

    STT_HOME=/tmp/x .venv/bin/python tools/demo_seed.py --dir /tmp/demo
    STT_HOME=/tmp/demo STT_PANEL_PORT=8791 .venv/bin/python -m gui.server &
    .venv/bin/python tools/next_screens.py --port 8791 --out /tmp/shots
    # then stop the panel you started

Each shot is also checked for a horizontal page scroll: the page must not
be wider than the window (the narrow-window fallback's main promise).
"""
import argparse
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LIVE_PORT = 8737
ROUTES = ("home", "recordings", "activity", "speakers", "archive", "settings")
WIDTHS = (700, 1440)
THEMES = ("light", "dark")


def shot(port, route, width, theme, out: Path, profile: Path, height=1000) -> Path:
    url = f"http://127.0.0.1:{port}/next?theme={theme}#{route}"
    png = out / f"{route}-{width}-{theme}.png"
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--user-data-dir={profile}", "--no-first-run",
                    f"--window-size={width},{height}", "--virtual-time-budget=6000",
                    f"--screenshot={png}", url],
                   check=True, capture_output=True, timeout=90)
    return png


def overflow(port, route, width, theme, profile: Path) -> dict:
    """The page's scrollWidth against the window width, after the page
    settles. With ?probe=1, next.js writes both onto <body data-probe>, and
    --dump-dom reads the attribute back."""
    url = f"http://127.0.0.1:{port}/next?theme={theme}&probe=1#{route}"
    r = subprocess.run([CHROME, "--headless=new", "--disable-gpu",
                        f"--user-data-dir={profile}", "--no-first-run",
                        f"--window-size={width},1000", "--virtual-time-budget=6000",
                        "--dump-dom", url],
                       check=True, capture_output=True, text=True, timeout=90)
    m = re.search(r'data-probe="([^"]*)"', r.stdout)
    return json.loads(html.unescape(m.group(1))) if m else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.port == LIVE_PORT:
        print("refusing: that is the live panel's port", file=sys.stderr)
        return 2
    a.out.mkdir(parents=True, exist_ok=True)
    bad = []
    with tempfile.TemporaryDirectory(prefix="next-shots-") as prof:
        for route in ROUTES:
            for width in WIDTHS:
                for theme in THEMES:
                    png = shot(a.port, route, width, theme, a.out, Path(prof))
                    m = overflow(a.port, route, width, theme, Path(prof))
                    wide = m and m.get("sw", 0) > m.get("iw", width)
                    if wide:
                        bad.append((route, width, theme, m))
                    print(f"{'WIDE' if wide else 'ok  '} {png.name} {m}")
    if bad:
        print(f"\n{len(bad)} shot(s) scroll sideways", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
