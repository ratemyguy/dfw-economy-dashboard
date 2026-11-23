#!/usr/bin/env python3
"""Check that every data number quoted in README.md appears in results/key_numbers.csv.

The README prose is scanned with code blocks, inline code, link targets and image paths
removed. Dates, years, list numbering, method settings and API limits (STRIP below) are
not results and are skipped. Every other number must equal a value in key_numbers.csv
rounded to the precision the README shows (sign ignored, since prose says "fell 10.6K"
for -10.6). Prints one line per number and exits 1 if any number has no match.

Usage: python -I src/check_readme_numbers.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
KEYS = ROOT / "results" / "key_numbers.csv"

MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
          "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
# Phrases whose numbers are definitions, settings or limits rather than results.
STRIP = [
    r"25 series and 10 years per query and 25 queries a day",      # BLS v1 API limits
    r"25 queries a day",                                            # same limit, restated
    r"a run uses 2 queries",                                        # follows from the limits
    r"6 KPI cards", r"10 interactive charts",                       # page design
    r"12th of each month, 15:00 UTC", r"12th of each month",        # schedule
    r"fewer than 5 of 10", r"\bof 10\b", r"\b10 supersectors\b",    # trigger setting; supersector count
    r"\b(?:reaching|crossed|the) 0\.5\b", r"0\.5 threshold",        # Sahm threshold (a constant)
    r"\b12 lags\b", r"95% CI",                                      # Newey-West setting; interval level
    r"\b12 months\b",                                               # period wording ("the latest 12 months")
    r"exits 0", r"X-13ARIMA-SEATS",                                 # exit code; software name
    r"\b\d+-(?:month|year|day|week|point)s?\b", r"2×12",       # method windows
    rf"\b(?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}",                      # dates like November 19, 2026
    rf"\b(?:{MONTHS})\s+\d{{4}}",                                   # months like October 2026
    r"\b(?:19|20)\d{2}(?:[–-](?:19|20)?\d{2})?\b",              # years and ranges like 2017-2019
]
TOKEN = re.compile(r"(?<![\w.])[+\-−]?\d[\d,]*(?:\.\d+)?")


def prose(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)            # code blocks
    text = re.sub(r"`[^`]*`", " ", text)                            # inline code
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)               # images
    text = re.sub(r"\]\([^)]*\)", "]", text)                        # link targets
    text = re.sub(r"https?://\S+", " ", text)                       # bare URLs
    text = re.sub(r"(?m)^\s*\d+\.\s", " ", text)                    # ordered-list numbers
    text = re.sub(r"\*\*\d+\.\s", "**", text)                       # numbered finding titles
    return text


def main() -> int:
    keys = pd.read_csv(KEYS, dtype={"display": str})
    text = prose(README.read_text(encoding="utf-8"))
    for pattern in STRIP:
        text = re.sub(pattern, " ", text)
    rows, missing = [], 0
    for m in TOKEN.finditer(text):
        raw = m.group(0)
        num = float(raw.replace(",", "").replace("−", "-").lstrip("+"))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        hits = keys[(keys["value"].abs().round(decimals) - round(abs(num), decimals)).abs() < 1e-9]
        context = " ".join(text[max(0, m.start() - 40):m.end() + 25].split())
        rows.append((raw, ", ".join(hits["key"].tolist()) or "NO MATCH", context))
        missing += hits.empty
    width = max(len(r[0]) for r in rows) if rows else 5
    for raw, hit, context in rows:
        print(f"{raw:>{width}}  {hit:<60.60}  ...{context}...")
    print(f"\n{len(rows)} numbers checked, {missing} without a match in {KEYS.relative_to(ROOT)}.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
