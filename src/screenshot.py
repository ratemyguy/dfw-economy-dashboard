#!/usr/bin/env python3
"""Save a PNG preview of the dashboard for the README (GitHub cannot show the live page
inside a README).

Serves docs/ on a free local port, opens it in headless Google Chrome (a throwaway
profile, nothing saved), captures the top of the page and writes
images/dashboard_preview.png. Exits 0 with a message if Chrome is not installed.
Needs network access for Plotly.js (cdn.plot.ly), exactly like the live page.
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = ROOT / "images" / "dashboard_preview.png"
WIDTH, HEIGHT = 1280, 1560          # header, the six KPI cards, derived signals and the first chart

CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
]


def find_chrome() -> str | None:
    for c in CANDIDATES:
        path = c if Path(c).exists() else shutil.which(c)
        if path:
            return path
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    chrome = find_chrome()
    if chrome is None:
        print("Google Chrome/Chromium not found; skipping the dashboard preview.")
        return 0
    port = free_port()
    server = subprocess.Popen([sys.executable, "-I", "-m", "http.server", str(port), "--bind", "127.0.0.1",
                               "--directory", str(DOCS)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}/"
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "data/dashboard.json", timeout=1).read(1)
                break
            except OSError:
                time.sleep(0.1)
        OUT.parent.mkdir(exist_ok=True)
        OUT.unlink(missing_ok=True)      # so a failed run cannot leave an old image looking new
        with tempfile.TemporaryDirectory(prefix="dfw-preview-") as profile:
            cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                   "--no-default-browser-check", f"--user-data-dir={profile}", f"--window-size={WIDTH},{HEIGHT}",
                   "--virtual-time-budget=25000", f"--screenshot={OUT}", url]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if OUT.exists():
            print(f"Wrote {OUT.relative_to(ROOT)}")
            return 0
        print("Chrome did not write a screenshot; skipping.")
        return 0
    finally:
        server.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
