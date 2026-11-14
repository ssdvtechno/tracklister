"""Capture README screenshots (2x) from the dev server using an installed Chromium-based browser.

    python dev/devserver.py --fake-spotify &      # in another terminal
    python dev/screenshots.py [path-to-chrome-or-brave]
"""

import sys
import urllib.parse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs"
BROWSER = sys.argv[1] if len(sys.argv) > 1 else "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
SET = "https://www.youtube.com/watch?v=0dSsRJFil2Q"
TECHNO = """0:00 | Franco Rossi - Phyllophaga
1:40 | Hurdslenk – Holding On
4:40 | Dax J – Sphinx
8:40 | Dynamic Forces – Ander mixed with Lars Huismann - Grid
23:30 | Nemesis - Camino Al Fondo with JALA - Resonant Core
46:40 | Sounds like an unreleased Dax track
1:19:15 | The Tuss - Rushup I Bank 12
1:23:50 | Rama 1 - C'est La Vie (Hard Mix)"""

SHOTS = [
    ("results", 860, {"demo": "results", "src": SET, "open": "2"}),
    ("elsewhere", 760, {"demo": "results", "src": TECHNO, "filter": "missing"}),
    ("setup", 980, {"demo": "setup"}),
    ("start", 520, {"demo": "start", "src": SET}),
]

OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=BROWSER)
    only = set(sys.argv[2:])
    for name, height, params in SHOTS:
        if only and name not in only:
            continue
        page = browser.new_page(viewport={"width": 1100, "height": height}, device_scale_factor=2, color_scheme="dark")
        page.goto("http://127.0.0.1:8765/#" + urllib.parse.urlencode(params))
        page.wait_for_function("document.title === 'ready'", timeout=120_000)
        page.wait_for_timeout(1500)  # let cover art load
        page.screenshot(path=str(OUT / f"{name}.png"))
        print("saved", name)
        page.close()
    browser.close()
