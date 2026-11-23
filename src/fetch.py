#!/usr/bin/env python3
"""Fetch the Dallas-Fort Worth economy series and write tidy files.

Sources (no API keys needed):
  * BLS Public Data API v1 (https://www.bls.gov/developers/). v1 allows
    25 series and 10 years per query and 25 queries per day per IP address,
    so every BLS series is batched into ONE request per 10-year window
    (two requests per run today: 2016-2025 and 2026).
  * FRED graph CSV (https://fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>),
    run by the Federal Reserve Bank of St. Louis.

Outputs:
  data/processed/observations.csv  tidy long: series_id, date, value, preliminary, footnote
  data/processed/series.csv        one row per series: id, title, source, units, ...
  data/raw/bls/*.json, data/raw/fred/*.csv   raw responses (gitignored)

Failure policy. The monthly GitHub Actions job must not fail (and email) on a
data outage. If a source or a single series fails, comes back empty, or comes
back older than what we already have, that series keeps its rows from the
previous observations.csv, a warning is printed (as a GitHub Actions
::warning:: annotation), and the script still exits 0. The page shows a
"data as of" date for every series, so a stale series is visible.

Only pandas, requests and the standard library are used: the scheduled job
installs just those two packages (requirements-ci.txt).

Usage:
  python -I src/fetch.py             download, then write the processed files
  python -I src/fetch.py --offline   re-parse the raw files already in data/raw/
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW_BLS = ROOT / "data" / "raw" / "bls"
RAW_FRED = ROOT / "data" / "raw" / "fred"
PROCESSED = ROOT / "data" / "processed"
OBS_PATH = PROCESSED / "observations.csv"
SERIES_PATH = PROCESSED / "series.csv"

START_YEAR = 2016
BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_MAX_YEARS = 10      # v1 limit per query
BLS_MAX_SERIES = 25     # v1 limit per query
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
USER_AGENT = "econ-data-fetch/1.0 (python-requests)"
ATTEMPTS = 3

BLS = "U.S. Bureau of Labor Statistics"
LAUS = f"{BLS}, Local Area Unemployment Statistics (LAUS)"
CPS = f"{BLS}, Current Population Survey (CPS)"
CES_SA = f"{BLS}, Current Employment Statistics, State and Metro Area (CES)"
CES_N = f"{BLS}, Current Employment Statistics, national (CES)"
CPI = f"{BLS}, Consumer Price Index (CPI-U)"
SA, NSA = "Seasonally adjusted", "Not seasonally adjusted"
DFW = "Dallas-Fort Worth-Arlington, TX MSA"
DFW_CES = "Dallas-Fort Worth-Arlington, TX"


def bls_url(sid: str) -> str:
    return f"https://data.bls.gov/timeseries/{sid}"


def fred_url(sid: str) -> str:
    return f"https://fred.stlouisfed.org/series/{sid}"


def _bls(sid, key, title, source, geography, units, sa, frequency="Monthly", notes=""):
    return dict(series_id=sid, key=key, api="bls", title=title, source=source,
                geography=geography, units=units, frequency=frequency,
                seasonal_adjustment=sa, url=bls_url(sid), notes=notes)


def _fhfa(sid: str, key: str, area: str, geography: str):
    """FHFA all-transactions house price index for one DFW metropolitan division (via FRED)."""
    return dict(series_id=sid, key=key, api="fred",
                title=f"All-Transactions House Price Index for {area}",
                source="U.S. Federal Housing Finance Agency (FHFA), House Price Index, via FRED "
                       "(Federal Reserve Bank of St. Louis)",
                geography=geography, units="Index, 1995:Q1=100", frequency="Quarterly",
                seasonal_adjustment=NSA, url=fred_url(sid),
                notes="Weighted repeat-sales index of single-family homes with mortgages bought or "
                      "securitized by Fannie Mae or Freddie Mac; the all-transactions version adds "
                      "appraisal data. Revised with each release. Public domain (U.S. government data); "
                      "FRED requests a citation.")


def _supersector(code: str, key: str, name: str):
    # SM + U (not seasonally adjusted) + state 48 + area 19100 + supersector + industry 000000
    # + data type 01 (all employees, thousands) = 20 characters
    sid = f"SMU4819100{code}00000001"
    assert len(sid) == 20, sid
    return _bls(sid, key, f"All employees, {name}: {DFW_CES}", CES_SA, DFW,
                "Thousands of jobs", NSA)


# Every series the dashboard uses. `key` is the role name transform.py reads.
SERIES: list[dict] = [
    _bls("LAUMT481910000000003", "ur_dfw_nsa",
         "Unemployment rate: Dallas-Fort Worth-Arlington, TX Metropolitan Statistical Area",
         LAUS, DFW, "Percent", NSA,
         notes="BLS does not publish a seasonally adjusted series for this metro "
               "(the API reports LASMT481910000000003 does not exist)."),
    _bls("LASST480000000000003", "ur_tx_sa", "Unemployment rate: Texas", LAUS, "Texas", "Percent", SA),
    _bls("LAUST480000000000003", "ur_tx_nsa", "Unemployment rate: Texas", LAUS, "Texas", "Percent", NSA),
    _bls("LNS14000000", "ur_us_sa", "Unemployment rate, 16 years and over: United States",
         CPS, "United States", "Percent", SA),
    _bls("LNU04000000", "ur_us_nsa", "Unemployment rate, 16 years and over: United States",
         CPS, "United States", "Percent", NSA),
    _bls("SMS48191000000000001", "jobs_dfw_sa", f"All employees, total nonfarm: {DFW_CES}",
         CES_SA, DFW, "Thousands of jobs", SA),
    _bls("SMU48191000000000001", "jobs_dfw_nsa", f"All employees, total nonfarm: {DFW_CES}",
         CES_SA, DFW, "Thousands of jobs", NSA),
    _bls("SMS48000000000000001", "jobs_tx_sa", "All employees, total nonfarm: Texas",
         CES_SA, "Texas", "Thousands of jobs", SA),
    _bls("CES0000000001", "jobs_us_sa", "All employees, total nonfarm: United States",
         CES_N, "United States", "Thousands of jobs", SA),
    _bls("SMU48191000500000003", "ahe_dfw",
         f"Average hourly earnings of all employees, total private: {DFW_CES}",
         CES_SA, DFW, "Dollars per hour", NSA,
         notes="No seasonally adjusted version exists for this metro series."),
    _bls("CUURS37ASA0", "cpi_dfw",
         "CPI-U, all items in Dallas-Fort Worth-Arlington, TX, all urban consumers",
         CPI, "Dallas-Fort Worth-Arlington, TX (CPI index area)", "Index, 1982-84=100", NSA,
         frequency="Bimonthly (odd months: Jan, Mar, May, Jul, Sep, Nov)",
         notes="BLS area code S37A. (S37B is Houston-The Woodlands-Sugar Land, TX.)"),
    _bls("CUUR0000SA0", "cpi_us", "CPI-U, all items in U.S. city average, all urban consumers",
         CPI, "United States", "Index, 1982-84=100", NSA),
    _supersector("15", "ss_mlc", "mining, logging, and construction"),
    _supersector("30", "ss_mfg", "manufacturing"),
    _supersector("40", "ss_ttu", "trade, transportation, and utilities"),
    _supersector("50", "ss_info", "information"),
    _supersector("55", "ss_fin", "financial activities"),
    _supersector("60", "ss_pbs", "professional and business services"),
    _supersector("65", "ss_edh", "private education and health services"),
    _supersector("70", "ss_lh", "leisure and hospitality"),
    _supersector("80", "ss_oth", "other services"),
    _supersector("90", "ss_gov", "government"),
    _fhfa("ATNHPIUS19124Q", "hpi_dallas", "Dallas-Plano-Irving, TX (MSAD)",
          "Dallas-Plano-Irving, TX Metropolitan Division"),
    _fhfa("ATNHPIUS23104Q", "hpi_fortworth", "Fort Worth-Arlington-Grapevine, TX (MSAD)",
          "Fort Worth-Arlington-Grapevine, TX Metropolitan Division"),
    dict(series_id="MORTGAGE30US", key="mortgage30", api="fred",
         title="30-Year Fixed Rate Mortgage Average in the United States",
         source="Freddie Mac Primary Mortgage Market Survey, via FRED (Federal Reserve Bank of St. Louis)",
         geography="United States", units="Percent", frequency="Weekly, ending Thursday",
         seasonal_adjustment=NSA, url=fred_url("MORTGAGE30US"),
         notes="National rate (not DFW-specific). Copyright Freddie Mac, reprinted on FRED with permission."),
    dict(series_id="DALL148BPPRIV", key="permits_dfw", api="fred",
         title="New Private Housing Units Authorized by Building Permits for Dallas-Fort Worth-Arlington, TX (MSA)",
         source="U.S. Census Bureau, Building Permits Survey, via FRED (Federal Reserve Bank of St. Louis)",
         geography=DFW, units="Housing units", frequency="Monthly",
         seasonal_adjustment=NSA, url=fred_url("DALL148BPPRIV"),
         notes="All structure types. All data are subject to revision."),
]

OBS_COLUMNS = ["series_id", "date", "value", "preliminary", "footnote"]


# ----------------------------------------------------------------- logging --
def log(msg: str) -> None:
    print(msg, flush=True)


WARNINGS: list[str] = []


def warn(msg: str) -> None:
    """Print a warning; inside GitHub Actions this becomes an annotation."""
    WARNINGS.append(msg)
    print(f"::warning title=fetch.py::{msg}", flush=True)


# -------------------------------------------------------------------- http --
def http(method: str, url: str, *, json_body: dict | None = None, timeout: int = 90) -> requests.Response:
    """Request with retries on network errors, 429 and 5xx. Other 4xx fail at once."""
    last_error: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = requests.request(method, url, json=json_body, timeout=timeout,
                                 headers={"User-Agent": USER_AGENT})
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code} from {url}")
            return r
        except requests.RequestException as e:
            last_error = e
            if attempt < ATTEMPTS:
                wait = 5 * 2 ** (attempt - 1)
                log(f"    attempt {attempt}/{ATTEMPTS} failed ({e}); retrying in {wait}s")
                time.sleep(wait)
    raise RuntimeError(f"{method} {url} failed after {ATTEMPTS} attempts: {last_error}")


# ----------------------------------------------------------------- parsing --
def to_float(text) -> float:
    try:
        x = float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return math.nan
    return x if math.isfinite(x) else math.nan


def year_windows(start: int, end: int, size: int = BLS_MAX_YEARS) -> list[tuple[int, int]]:
    return [(y, min(y + size - 1, end)) for y in range(start, end + 1, size)]


def parse_bls(payload: dict) -> list[dict]:
    """Monthly rows (M01-M12) from one BLS API response. M13 = annual average, dropped."""
    rows = []
    for s in (payload.get("Results") or {}).get("series", []):
        sid = s.get("seriesID")
        for item in s.get("data", []):
            period = item.get("period", "")
            if not (len(period) == 3 and period.startswith("M") and period != "M13"):
                continue
            prelim, notes = False, []
            for fn in item.get("footnotes") or []:
                if not fn:
                    continue
                if (fn.get("code") or "").strip().upper() == "P":
                    prelim = True
                text = (fn.get("text") or "").strip()
                if text and text.lower().rstrip(".") != "preliminary":
                    notes.append(text)
            rows.append({
                "series_id": sid,
                "date": f"{int(item['year']):04d}-{int(period[1:]):02d}-01",
                "value": to_float(item.get("value")),
                "preliminary": prelim,
                "footnote": "; ".join(dict.fromkeys(notes)),
            })
    return rows


def fetch_bls(series_ids: list[str], offline: bool) -> list[dict]:
    """All BLS rows. Raises if any request fails (the caller then keeps the old data)."""
    RAW_BLS.mkdir(parents=True, exist_ok=True)
    if offline:
        paths = sorted(RAW_BLS.glob("bls_*.json"))
        if not paths:
            raise RuntimeError(f"--offline: no raw BLS files in {RAW_BLS}")
        payloads = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in paths]
    else:
        end_year = dt.date.today().year
        payloads = []
        for y0, y1 in year_windows(START_YEAR, end_year):
            for part, i in enumerate(range(0, len(series_ids), BLS_MAX_SERIES), start=1):
                chunk = series_ids[i:i + BLS_MAX_SERIES]
                log(f"  BLS: {len(chunk)} series, {y0}-{y1}")
                r = http("POST", BLS_URL, json_body={
                    "seriesid": chunk, "startyear": str(y0), "endyear": str(y1)})
                path = RAW_BLS / f"bls_{y0}_{y1}_part{part}.json"
                path.write_bytes(r.content)
                payloads.append((path, r.json()))
                time.sleep(1)
    rows = []
    for path, payload in payloads:
        status = payload.get("status")
        if status != "REQUEST_SUCCEEDED":
            raise RuntimeError(f"BLS {path.name}: status {status}: {payload.get('message')}")
        for m in payload.get("message") or []:
            log(f"    BLS message ({path.name}): {m}")
        rows.extend(parse_bls(payload))
    return rows


def fetch_fred(series_id: str, offline: bool) -> list[dict]:
    """Rows from one FRED graph CSV, from START_YEAR on. FRED marks missing values as '.'."""
    RAW_FRED.mkdir(parents=True, exist_ok=True)
    path = RAW_FRED / f"{series_id}.csv"
    if offline:
        text = path.read_text(encoding="utf-8")
    else:
        log(f"  FRED: {series_id}")
        r = http("GET", FRED_URL.format(id=series_id))
        path.write_bytes(r.content)
        text = r.text
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    if df.shape[1] != 2 or df.columns[1] != series_id:
        raise RuntimeError(f"unexpected FRED CSV header for {series_id}: {list(df.columns)[:3]}")
    dates = pd.to_datetime(df.iloc[:, 0], errors="coerce")
    rows = []
    for d, v in zip(dates, df.iloc[:, 1]):
        if pd.isna(d) or d.year < START_YEAR:
            continue
        rows.append({"series_id": series_id, "date": d.strftime("%Y-%m-%d"),
                     "value": to_float(v), "preliminary": False, "footnote": ""})
    return rows


# -------------------------------------------------------------- validation --
def read_previous() -> pd.DataFrame:
    if not OBS_PATH.exists():
        return pd.DataFrame(columns=OBS_COLUMNS)
    df = pd.read_csv(OBS_PATH, dtype=str, keep_default_na=False)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["preliminary"] = df["preliminary"].str.lower().eq("true")
    return df[OBS_COLUMNS]


def check_series(meta: dict, fresh: pd.DataFrame, prev: pd.DataFrame) -> str | None:
    """Return a reason to reject the fresh rows, or None if they look sane."""
    numeric = fresh[fresh["value"].notna()]
    freq = meta["frequency"]
    min_obs = 100 if freq.startswith("Weekly") else 16 if freq.startswith("Quarterly") else 24
    if len(numeric) < min_obs:
        return f"only {len(numeric)} numeric observations (expected at least {min_obs})"
    if (numeric["value"] < 0).any():
        return "negative values"
    prev_num = prev[prev["value"].notna()]
    if len(prev_num) and numeric["date"].max() < prev_num["date"].max():
        return (f"fresh data end {numeric['date'].max()} but the saved data end "
                f"{prev_num['date'].max()} (stale response)")
    return None


# -------------------------------------------------------------------- main --
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true",
                    help="re-parse the raw files in data/raw/ instead of downloading")
    args = ap.parse_args()

    PROCESSED.mkdir(parents=True, exist_ok=True)
    previous = read_previous()
    fresh_rows: list[dict] = []

    log("Fetching BLS series (one request per 10-year window)...")
    failed: set[str] = set()   # series ids whose whole request failed (already warned)
    bls_ids = [m["series_id"] for m in SERIES if m["api"] == "bls"]
    try:
        fresh_rows += fetch_bls(bls_ids, args.offline)
    except Exception as e:  # noqa: BLE001 - any failure means "keep the old BLS data"
        warn(f"BLS source failed, keeping the previous BLS data: {e}")
        failed.update(bls_ids)

    log("Fetching FRED series...")
    for m in SERIES:
        if m["api"] != "fred":
            continue
        try:
            fresh_rows += fetch_fred(m["series_id"], args.offline)
        except Exception as e:  # noqa: BLE001
            warn(f"FRED {m['series_id']} failed, keeping the previous data: {e}")
            failed.add(m["series_id"])
        if not args.offline:
            time.sleep(1)

    fresh = pd.DataFrame(fresh_rows, columns=OBS_COLUMNS)
    fresh = fresh.drop_duplicates(subset=["series_id", "date"], keep="last")

    kept_frames, status = [], {}
    for m in SERIES:
        sid = m["series_id"]
        f = fresh[fresh["series_id"] == sid]
        p = previous[previous["series_id"] == sid]
        problem = check_series(m, f, p) if len(f) else "no rows returned"
        if problem is None:
            kept_frames.append(f)
            status[sid] = "updated"
            continue
        note = warn if sid not in failed else log   # a failed request was already warned about
        if len(p):
            note(f"{sid}: {problem}; keeping the previous {len(p)} rows")
            kept_frames.append(p)
            status[sid] = "kept previous"
        else:
            warn(f"{sid}: {problem}; no previous data either, series left out")
            status[sid] = "missing"

    if not any(len(k) for k in kept_frames):
        warn("No data at all; nothing written.")
        return 0

    obs = pd.concat([k for k in kept_frames if len(k)], ignore_index=True)
    obs = obs.sort_values(["series_id", "date"], kind="stable").reset_index(drop=True)
    obs["preliminary"] = obs["preliminary"].map(lambda b: "True" if bool(b) else "False")
    obs["footnote"] = obs["footnote"].fillna("")
    obs.to_csv(OBS_PATH, index=False, lineterminator="\n")

    meta_rows = []
    for m in SERIES:
        s = obs[(obs["series_id"] == m["series_id"]) & obs["value"].notna()]
        last = s.iloc[-1] if len(s) else None
        meta_rows.append({
            **{k: m[k] for k in ("series_id", "key", "title", "source", "geography", "units",
                                 "frequency", "seasonal_adjustment", "url", "notes")},
            "first_date": s["date"].min() if len(s) else "",
            "last_date": last["date"] if last is not None else "",
            "n_obs": len(s),
            "latest_value": last["value"] if last is not None else "",
            "latest_preliminary": last["preliminary"] if last is not None else "",
        })
    pd.DataFrame(meta_rows).to_csv(SERIES_PATH, index=False, lineterminator="\n")

    log(f"\nWrote {OBS_PATH.relative_to(ROOT)} ({len(obs):,} rows) and "
        f"{SERIES_PATH.relative_to(ROOT)} ({len(meta_rows)} series).")
    for m in meta_rows:
        log(f"  {m['series_id']:<22} {status[m['series_id']]:<14} "
            f"{m['first_date']} .. {m['last_date']}  n={m['n_obs']}")
    if WARNINGS:
        log(f"\n{len(WARNINGS)} warning(s); exiting 0 so the scheduled job does not fail.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
