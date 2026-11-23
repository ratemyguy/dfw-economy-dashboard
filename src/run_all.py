#!/usr/bin/env python3
"""Rebuild everything from the source APIs, in order:

  1. src/fetch.py       BLS + FRED  -> data/raw/ (raw), data/processed/observations.csv, series.csv
  2. src/transform.py   metrics     -> docs/data/dashboard.json, data/processed/derived_monthly.csv, ...
  3. src/report.py      snapshot    -> results/*.csv, images/*.png (the numbers and charts in the README)
  4. the notebook       analysis    -> notebooks/dfw_labor_market.ipynb, executed in place
  5. src/screenshot.py  preview     -> images/dashboard_preview.png (skipped if Google Chrome is missing)
  6. src/check_readme_numbers.py    -> reports any README number that no longer matches results/

Usage:
  python -I src/run_all.py                  full rebuild (2 BLS API queries + 3 FRED downloads)
  python -I src/run_all.py --offline        re-parse the raw files already in data/raw/ (no network
                                            for the data; the preview still loads Plotly.js)
  python -I src/run_all.py --no-screenshot  skip the browser preview
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
NOTEBOOK = ROOT / "notebooks" / "dfw_labor_market.ipynb"


def run(label: str, cmd: list[str], *, cwd: Path = ROOT, fatal: bool = True) -> bool:
    print(f"\n=== {label} ===\n$ {' '.join(cmd)}", flush=True)
    t0 = time.time()
    result = subprocess.run(cmd, cwd=cwd)
    print(f"--- {label}: exit {result.returncode} ({time.time() - t0:.1f}s)", flush=True)
    if result.returncode != 0 and fatal:
        sys.exit(f"{label} failed; stopping.")
    return result.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the DFW Economy Dashboard project.")
    ap.add_argument("--offline", action="store_true", help="re-parse data/raw/ instead of downloading")
    ap.add_argument("--no-notebook", action="store_true", help="skip executing the notebook")
    ap.add_argument("--no-screenshot", action="store_true", help="skip the dashboard preview image")
    args = ap.parse_args()
    py = [sys.executable, "-I"]

    run("1/6 fetch", py + [str(SRC / "fetch.py")] + (["--offline"] if args.offline else []))
    run("2/6 transform", py + [str(SRC / "transform.py")])
    run("3/6 report (results + charts)", py + [str(SRC / "report.py")])
    if not args.no_notebook:
        # "-m nbconvert" (not "-m jupyter nbconvert"): the jupyter launcher looks up
        # jupyter-nbconvert on PATH and can pick up a different Python installation.
        run("4/6 execute notebook", [sys.executable, "-m", "nbconvert", "--to", "notebook",
                                     "--execute", "--inplace", "--ExecutePreprocessor.timeout=600",
                                     str(NOTEBOOK)], cwd=NOTEBOOK.parent)
    if not args.no_screenshot:
        run("5/6 dashboard preview", py + [str(SRC / "screenshot.py")], fatal=False)
    ok = run("6/6 check README numbers", py + [str(SRC / "check_readme_numbers.py")], fatal=False)
    if not ok:
        print("\nThe README quotes numbers that differ from results/: the data have changed since the README "
              "was written. Update the README (it is a dated snapshot) or ignore if intended.")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
