#!/usr/bin/env python3
"""Turn the tidy observations into the dashboard's metrics.

Reads   data/processed/observations.csv, data/processed/series.csv   (from fetch.py)
Writes  data/processed/derived_monthly.csv    one row per month, every monthly metric
        data/processed/derived_quarterly.csv  one row per quarter: FHFA house price indexes
        data/processed/supersectors.csv       DFW supersector levels and 12-month changes
        docs/data/dashboard.json              what docs/index.html draws
        docs/data/derived_monthly.csv, derived_quarterly.csv   the panels, offered as downloads

Only pandas and the standard library are used (the scheduled job installs pandas
and requests only). dashboard.json is rewritten only when its content changes,
so the monthly job commits nothing when no new data were published.

Methods (all documented on the page and in the README):
  * Year-over-year (YoY) % change = value / value 12 months earlier - 1, matched
    by calendar month, so a missing month never shifts the comparison.
  * Real wage growth = wage YoY - CPI YoY (percentage points; the usual
    approximation of (1 + w) / (1 + p) - 1).
  * House prices (FHFA) are quarterly: the change is measured against the same
    quarter a year earlier, on a quarterly index. They are never spread to months.
  * BLS publishes the DFW unemployment rate only without seasonal adjustment.
    This script estimates a seasonally adjusted version with a classical
    additive decomposition: a centered 2x12 moving-average trend, then the
    median deviation from trend for each calendar month (Mar 2020 - Dec 2021
    left out), centered to sum to zero. The same method is applied to the
    Texas and U.S. rates and compared with BLS's official seasonally adjusted
    series as a check (mean absolute error is reported).
  * Sahm-rule-style gauge (a heuristic, not a recession call): 3-month average
    of the seasonally adjusted unemployment rate minus its lowest 3-month
    average in the prior 12 months. The 0.5-point threshold was calibrated on
    national data by Claudia Sahm; it has not been tested on metro areas.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data" / "processed"
DOCS_DATA = ROOT / "docs" / "data"
OBS_PATH = PROCESSED / "observations.csv"
SERIES_PATH = PROCESSED / "series.csv"
DERIVED_PATH = PROCESSED / "derived_monthly.csv"
QUARTERLY_PATH = PROCESSED / "derived_quarterly.csv"
SUPERSECTOR_PATH = PROCESSED / "supersectors.csv"
JSON_PATH = DOCS_DATA / "dashboard.json"
DOWNLOAD_PATH = DOCS_DATA / "derived_monthly.csv"
DOWNLOAD_Q_PATH = DOCS_DATA / "derived_quarterly.csv"
QUARTERLY_KEYS = ("hpi_dallas", "hpi_fortworth")   # FHFA house price indexes (quarterly)

REPO_URL = "https://github.com/ratemyguy/dfw-economy-dashboard"
PANDEMIC = ("2020-03-01", "2021-12-01")    # left out when estimating seasonal factors
SAHM_THRESHOLD = 0.5
SAHM_MIN_MONTHS = 9                        # 3-month averages needed in the prior-12 window
MINUS = "−"

SUPERSECTORS = {
    "ss_mlc": "Mining, logging & construction",
    "ss_mfg": "Manufacturing",
    "ss_ttu": "Trade, transportation & utilities",
    "ss_info": "Information",
    "ss_fin": "Financial activities",
    "ss_pbs": "Professional & business services",
    "ss_edh": "Private education & health services",
    "ss_lh": "Leisure & hospitality",
    "ss_oth": "Other services",
    "ss_gov": "Government",
}

# Labels used in the "data as of" list on the page.
ASOF_LABELS = {
    "ur_dfw_nsa": "DFW unemployment rate",
    "ur_tx_sa": "Texas unemployment rate",
    "ur_us_sa": "U.S. unemployment rate",
    "jobs_dfw_sa": "DFW payroll jobs",
    "jobs_tx_sa": "Texas payroll jobs",
    "jobs_us_sa": "U.S. payroll jobs",
    "ahe_dfw": "DFW average hourly earnings",
    "cpi_dfw": "DFW CPI-U (bimonthly)",
    "cpi_us": "U.S. CPI-U",
    "ss_pbs": "DFW jobs by supersector",
    "hpi_dallas": "Home prices, Dallas division (FHFA, quarterly)",
    "hpi_fortworth": "Home prices, Fort Worth division (FHFA, quarterly)",
    "mortgage30": "30-year mortgage rate (weekly)",
    "permits_dfw": "DFW building permits",
}


# ------------------------------------------------------------------ helpers --
def is_num(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x)) and not pd.isna(x)


def rnd(x, nd=3):
    """JSON-safe rounded float (None for NaN, and no negative zero)."""
    if not is_num(x):
        return None
    r = round(float(x), nd)
    return 0.0 if r == 0 else r


def month_label(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %Y")


def quarter_label(ts) -> str:
    ts = pd.Timestamp(ts)
    return f"Q{ts.quarter} {ts.year}"


def day_label(ts) -> str:
    ts = pd.Timestamp(ts)
    return f"{ts.strftime('%b')} {ts.day}, {ts.year}"


def fmt_num(x, nd=1, sign=False, unit="") -> str:
    if not is_num(x):
        return "n/a"
    x = float(x)
    if round(x, nd) == 0:
        x = 0.0
    body = f"{abs(x):,.{nd}f}"
    prefix = ("+" if x > 0 else MINUS if x < 0 else "") if sign else (MINUS if x < 0 else "")
    return f"{prefix}{body}{unit}"


def yoy_pct(s: pd.Series) -> pd.Series:
    """12-month % change on a complete monthly index (so shift(12) is 12 calendar months)."""
    return (s / s.shift(12) - 1.0) * 100.0


def seasonal_adjust(s: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Classical additive seasonal adjustment with fixed (median) monthly factors.

    trend  = centered 2x12 moving average (needs 13 consecutive months)
    factor = median of (value - trend) for each calendar month, pandemic months
             excluded, re-centered so the 12 factors sum to zero
    SA     = value - factor
    Returns (seasonally adjusted series, factors indexed 1..12).
    """
    m12 = s.rolling(12).mean()
    trend = 0.5 * (m12.shift(-5) + m12.shift(-6))
    dev = s - trend
    keep = ~((dev.index >= PANDEMIC[0]) & (dev.index <= PANDEMIC[1]))
    dev = dev[keep].dropna()
    factors = dev.groupby(dev.index.month).median()
    factors = factors - factors.mean()
    return s - s.index.month.map(factors).to_numpy(), factors


def sahm_gauge(sa: pd.Series) -> tuple[pd.Series, pd.Series]:
    """3-month average minus its minimum over the previous 12 months."""
    ma3 = sa.rolling(3, min_periods=3).mean()
    prior_low = ma3.shift(1).rolling(12, min_periods=SAHM_MIN_MONTHS).min()
    return ma3 - prior_low, ma3


def value_at(s: pd.Series, date) -> float:
    return s.get(pd.Timestamp(date), float("nan"))


def last_valid(s: pd.Series):
    s = s.dropna()
    return (s.index[-1], s.iloc[-1]) if len(s) else (None, float("nan"))


def compare(a, b, nd=1) -> str:
    """'above' / 'below' / 'about the same as' with a tolerance of half the last digit."""
    if not (is_num(a) and is_num(b)):
        return "n/a"
    d = round(float(a), nd) - round(float(b), nd)
    if abs(d) < 0.5 * 10 ** (-nd):
        return "about the same as"
    return "above" if d > 0 else "below"


# --------------------------------------------------------------------- load --
def load():
    obs = pd.read_csv(OBS_PATH, dtype={"series_id": "string", "footnote": "string"},
                      keep_default_na=True)
    obs["date"] = pd.to_datetime(obs["date"])
    obs["value"] = pd.to_numeric(obs["value"], errors="coerce")
    obs["preliminary"] = obs["preliminary"].astype(str).str.lower().eq("true")
    obs["footnote"] = obs["footnote"].fillna("")
    meta = pd.read_csv(SERIES_PATH, dtype=str, keep_default_na=False)
    key_of = dict(zip(meta["series_id"], meta["key"]))
    obs["key"] = obs["series_id"].map(key_of)
    return obs, meta.set_index("key")


def build_monthly(obs: pd.DataFrame, keys) -> pd.DataFrame:
    not_monthly = {"mortgage30", *QUARTERLY_KEYS}
    monthly = obs[~obs["key"].isin(not_monthly)]
    wide = monthly.pivot(index="date", columns="key", values="value").sort_index()
    # Every configured series gets a column, even one that is missing entirely this run,
    # so a failed source blanks its charts instead of crashing the scheduled job.
    wide = wide.reindex(columns=sorted(set(wide.columns) | {k for k in keys if k not in not_monthly}))
    return wide.asfreq("MS")        # complete monthly index; gaps become NaN


def build_quarterly(obs: pd.DataFrame) -> pd.DataFrame:
    """FHFA house price indexes on a complete quarterly index (dated at the quarter's first day)
    and their change from the same quarter a year earlier."""
    q = obs[obs["key"].isin(QUARTERLY_KEYS)].pivot(index="date", columns="key", values="value").sort_index()
    q = q.reindex(columns=list(QUARTERLY_KEYS))
    if len(q):
        q = q.asfreq("QS")
    out = pd.DataFrame(index=q.index)
    for k in QUARTERLY_KEYS:
        out[k] = q[k]
        out[f"{k}_yoy_pct"] = (q[k] / q[k].shift(4) - 1.0) * 100.0
    return out


def mortgage_monthly(obs: pd.DataFrame):
    w = obs[(obs["key"] == "mortgage30") & obs["value"].notna()].set_index("date")["value"].sort_index()
    if w.empty:
        return pd.DataFrame(columns=["mortgage30_avg", "mortgage30_weeks"]), None, float("nan")
    g = w.groupby(w.index.to_period("M"))
    out = pd.DataFrame({"mortgage30_avg": g.mean(), "mortgage30_weeks": g.size()})
    # A month counts as complete only once a survey dated in a later month exists.
    out = out[out.index < w.index[-1].to_period("M")]
    out.index = out.index.to_timestamp()
    return out, w.index[-1], w.iloc[-1]


# ------------------------------------------------------------------ metrics --
def derive(wide: pd.DataFrame, mort: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    d = pd.DataFrame(index=wide.index)
    col = wide.get

    # Unemployment
    d["ur_dfw_nsa"] = col("ur_dfw_nsa")
    d["ur_tx_sa"], d["ur_us_sa"] = col("ur_tx_sa"), col("ur_us_sa")
    d["ur_tx_nsa"], d["ur_us_nsa"] = col("ur_tx_nsa"), col("ur_us_nsa")
    check = {}
    d["ur_dfw_sa_est"], f_dfw = seasonal_adjust(d["ur_dfw_nsa"])
    for geo in ("tx", "us"):
        d[f"ur_{geo}_sa_est"], _ = seasonal_adjust(d[f"ur_{geo}_nsa"])
        err = (d[f"ur_{geo}_sa_est"] - d[f"ur_{geo}_sa"]).dropna()
        check[geo] = {"mae_pp": err.abs().mean(), "max_abs_pp": err.abs().max(),
                      "share_within_0_1": (err.abs() <= 0.1 + 1e-9).mean(), "months": len(err)}
    d["sahm_dfw"], d["ur_dfw_3mma"] = sahm_gauge(d["ur_dfw_sa_est"])
    d["sahm_us"], d["ur_us_3mma"] = sahm_gauge(d["ur_us_sa"])
    d["ur_dfw_nsa_12m_change_pp"] = d["ur_dfw_nsa"] - d["ur_dfw_nsa"].shift(12)

    # Payroll jobs (seasonally adjusted, thousands)
    for geo in ("dfw", "tx", "us"):
        lv = col(f"jobs_{geo}_sa")
        d[f"jobs_{geo}_sa"] = lv
        d[f"jobs_{geo}_yoy_pct"] = yoy_pct(lv)
        d[f"jobs_{geo}_yoy_k"] = lv - lv.shift(12)
        d[f"jobs_{geo}_mom_k"] = lv.diff()
        d[f"jobs_{geo}_mom_k_3mma"] = d[f"jobs_{geo}_mom_k"].rolling(3, min_periods=3).mean()
        d[f"jobs_{geo}_3m_ann_pct"] = ((lv / lv.shift(3)) ** 4 - 1.0) * 100.0
    d["jobs_gap_dfw_us_pp"] = d["jobs_dfw_yoy_pct"] - d["jobs_us_yoy_pct"]
    d["jobs_gap_dfw_us_pp_3mma"] = d["jobs_gap_dfw_us_pp"].rolling(3, min_periods=3).mean()

    # Wages and prices
    d["ahe_dfw"] = col("ahe_dfw")
    d["ahe_dfw_yoy_pct"] = yoy_pct(d["ahe_dfw"])
    d["ahe_dfw_yoy_pct_3mma"] = d["ahe_dfw_yoy_pct"].rolling(3, min_periods=3).mean()
    d["cpi_dfw"], d["cpi_us"] = col("cpi_dfw"), col("cpi_us")
    d["cpi_dfw_yoy_pct"] = yoy_pct(d["cpi_dfw"])
    d["cpi_us_yoy_pct"] = yoy_pct(d["cpi_us"])
    d["real_ahe_vs_dfw_cpi_pp"] = d["ahe_dfw_yoy_pct"] - d["cpi_dfw_yoy_pct"]
    d["real_ahe_vs_us_cpi_pp"] = d["ahe_dfw_yoy_pct"] - d["cpi_us_yoy_pct"]

    # Housing (monthly series only; FHFA house prices are quarterly, see build_quarterly)
    d = d.join(mort, how="left")
    d["mortgage30_yoy_pp"] = d["mortgage30_avg"] - d["mortgage30_avg"].shift(12)
    d["permits_dfw"] = col("permits_dfw")
    d["permits_dfw_12m"] = d["permits_dfw"].rolling(12, min_periods=12).sum()
    d["permits_dfw_12m_yoy_pct"] = yoy_pct(d["permits_dfw_12m"])

    # Supersector breadth: share of the 10 supersectors with more jobs than a year earlier
    ss = pd.DataFrame({k: col(k) for k in SUPERSECTORS})
    ss_yoy = ss - ss.shift(12)
    have = ss_yoy.notna().sum(axis=1)
    d["breadth_share_pos"] = (ss_yoy.gt(0).sum(axis=1) / have).where(have == len(SUPERSECTORS)) * 100.0
    d["ss_sum_minus_total_k"] = ss.sum(axis=1, min_count=len(SUPERSECTORS)) - col("jobs_dfw_nsa")

    check["dfw_factors"] = f_dfw
    return d, check


def supersector_table(wide: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, name in SUPERSECTORS.items():
        s = wide.get(key)
        if s is None:
            continue
        for date, v in s.dropna().items():
            prev = value_at(s, date - pd.DateOffset(months=12))
            rows.append({"date": date.strftime("%Y-%m-%d"), "key": key, "supersector": name,
                         "jobs_k": v, "yoy_k": v - prev if is_num(prev) else float("nan"),
                         "yoy_pct": (v / prev - 1) * 100 if is_num(prev) else float("nan")})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------- KPIs --
def kpis(d: pd.DataFrame, dq: pd.DataFrame, prelim: dict, mort_last_date, mort_last_value) -> list[dict]:
    out = []

    def is_prelim(key, date):
        return date is not None and pd.Timestamp(date) in prelim.get(key, set())

    # 1. DFW unemployment rate (as published, NSA)
    s = d["ur_dfw_nsa"]
    t, v = last_valid(s)
    if t is not None:
        p, y = t - pd.DateOffset(months=1), t - pd.DateOffset(months=12)
        out.append({
            "id": "ur_dfw", "label": "DFW unemployment rate",
            "value_fmt": fmt_num(v, 1, unit="%"), "period": month_label(t),
            "preliminary": is_prelim("ur_dfw_nsa", t),
            "delta_prior": f"{fmt_num(v - value_at(s, p), 1, sign=True, unit=' pts')} vs {month_label(p)}",
            "delta_year": f"{fmt_num(v - value_at(s, y), 1, sign=True, unit=' pts')} vs {month_label(y)}",
            "adjustment": "Not seasonally adjusted (as published)",
            "context": (f"Seasonally adjusted estimate: {fmt_num(value_at(d['ur_dfw_sa_est'], t), 1, unit='%')}. "
                        f"Texas {fmt_num(value_at(d['ur_tx_sa'], t), 1, unit='%')}, "
                        f"U.S. {fmt_num(value_at(d['ur_us_sa'], t), 1, unit='%')} "
                        f"(seasonally adjusted, {month_label(t)})."),
            "value": rnd(v, 2), "date": t.strftime("%Y-%m-%d"),
        })

    # 2. DFW payroll jobs (SA, thousands)
    s = d["jobs_dfw_sa"]
    t, v = last_valid(s)
    if t is not None:
        p, y = t - pd.DateOffset(months=1), t - pd.DateOffset(months=12)
        yv = value_at(s, y)
        out.append({
            "id": "jobs_dfw", "label": "DFW payroll jobs",
            "value_fmt": f"{v / 1000:,.2f} million", "period": month_label(t),
            "preliminary": is_prelim("jobs_dfw_sa", t),
            "delta_prior": f"{fmt_num(v - value_at(s, p), 1, sign=True)}K vs {month_label(p)}",
            "delta_year": (f"{fmt_num(v - yv, 1, sign=True)}K "
                           f"({fmt_num((v / yv - 1) * 100, 1, sign=True, unit='%')}) vs {month_label(y)}"),
            "adjustment": "Seasonally adjusted",
            "context": (f"U.S. payroll jobs: {fmt_num(value_at(d['jobs_us_yoy_pct'], t), 1, sign=True, unit='%')} "
                        f"over the same 12 months."),
            "value": rnd(v, 1), "date": t.strftime("%Y-%m-%d"),
        })

    # 3. DFW average hourly earnings (NSA)
    s = d["ahe_dfw"]
    t, v = last_valid(s)
    if t is not None:
        p, y = t - pd.DateOffset(months=1), t - pd.DateOffset(months=12)
        pv, yv = value_at(s, p), value_at(s, y)
        rt, rv = last_valid(d["real_ahe_vs_dfw_cpi_pp"])
        out.append({
            "id": "ahe_dfw", "label": "DFW average hourly earnings",
            "value_fmt": f"${v:,.2f}", "period": month_label(t),
            "preliminary": is_prelim("ahe_dfw", t),
            "delta_prior": (f"{'+' if v - pv >= 0 else MINUS}${abs(v - pv):,.2f} vs {month_label(p)}"
                            if is_num(pv) else "n/a"),
            "delta_year": f"{fmt_num((v / yv - 1) * 100, 1, sign=True, unit='%')} vs {month_label(y)}",
            "adjustment": "Not seasonally adjusted; all private-sector employees",
            "context": (f"After DFW inflation: {fmt_num(rv, 1, sign=True, unit=' pts')} "
                        f"({month_label(rt)}, wage growth minus CPI inflation)." if rt is not None else ""),
            "value": rnd(v, 2), "date": t.strftime("%Y-%m-%d"),
        })

    # 4. DFW inflation (CPI-U, 12-month %, bimonthly)
    s = d["cpi_dfw_yoy_pct"]
    t, v = last_valid(s)
    if t is not None:
        p, y = t - pd.DateOffset(months=2), t - pd.DateOffset(months=12)
        us_t = value_at(d["cpi_us_yoy_pct"], t)
        out.append({
            "id": "cpi_dfw", "label": "DFW inflation (CPI-U, 12-month)",
            "value_fmt": fmt_num(v, 1, unit="%"), "period": month_label(t),
            "preliminary": False,
            "delta_prior": f"{fmt_num(v - value_at(s, p), 1, sign=True, unit=' pts')} vs {month_label(p)}",
            "delta_year": f"{fmt_num(v - value_at(s, y), 1, sign=True, unit=' pts')} vs {month_label(y)}",
            "adjustment": "Not seasonally adjusted; published every other month",
            "context": f"U.S. CPI-U: {fmt_num(us_t, 1, unit='%')} over the year to {month_label(t)}.",
            "value": rnd(v, 2), "date": t.strftime("%Y-%m-%d"),
        })

    # 5. Dallas home prices (FHFA all-transactions index, change from the same quarter a year earlier)
    s = dq["hpi_dallas_yoy_pct"] if "hpi_dallas_yoy_pct" in dq else pd.Series(dtype=float)
    t, v = last_valid(s)
    if t is not None:
        p, y = t - pd.DateOffset(months=3), t - pd.DateOffset(months=12)
        fw = value_at(dq["hpi_fortworth_yoy_pct"], t)
        out.append({
            "id": "hpi_dallas", "label": "Dallas home prices (vs a year earlier)",
            "value_fmt": fmt_num(v, 1, sign=True, unit="%"), "period": quarter_label(t),
            "preliminary": False,
            "delta_prior": f"{fmt_num(v - value_at(s, p), 1, sign=True, unit=' pts')} vs {quarter_label(p)}",
            "delta_year": f"{fmt_num(v - value_at(s, y), 1, sign=True, unit=' pts')} vs {quarter_label(y)}",
            "adjustment": "FHFA all-transactions index, Dallas-Plano-Irving division; quarterly, "
                          "not seasonally adjusted",
            "context": (f"Fort Worth-Arlington-Grapevine division: {fmt_num(fw, 1, sign=True, unit='%')} over the "
                        f"same year. Dallas index level {value_at(dq['hpi_dallas'], t):,.1f} (1995 Q1 = 100)."),
            "value": rnd(v, 2), "date": t.strftime("%Y-%m-%d"),
        })

    # 6. 30-year mortgage rate (monthly average of weekly survey)
    s = d["mortgage30_avg"]
    t, v = last_valid(s)
    if t is not None:
        p, y = t - pd.DateOffset(months=1), t - pd.DateOffset(months=12)
        out.append({
            "id": "mortgage30", "label": "30-year mortgage rate (U.S.)",
            "value_fmt": fmt_num(v, 2, unit="%"), "period": f"{month_label(t)} average",
            "preliminary": False,
            "delta_prior": f"{fmt_num(v - value_at(s, p), 2, sign=True, unit=' pts')} vs {month_label(p)}",
            "delta_year": f"{fmt_num(v - value_at(s, y), 2, sign=True, unit=' pts')} vs {month_label(y)}",
            "adjustment": "Monthly average of Freddie Mac's weekly survey",
            "context": (f"Latest weekly reading: {fmt_num(mort_last_value, 2, unit='%')} "
                        f"({day_label(mort_last_date)})." if mort_last_date is not None else ""),
            "value": rnd(v, 3), "date": t.strftime("%Y-%m-%d"),
        })
    return out


# ----------------------------------------------------------- chart payloads --
HOVER_NAMES = {   # short labels for the hover box (legend keeps the full names)
    "DFW, as published (not seasonally adjusted)": "DFW, as published",
    "DFW, seasonally adjusted (this project's estimate)": "DFW, adjusted",
    "Texas (seasonally adjusted)": "Texas", "U.S. (seasonally adjusted)": "U.S.",
    "Gap: DFW minus U.S. (points)": "Gap vs U.S.",
    "DFW hourly earnings, monthly": "Hourly earnings", "DFW hourly earnings, 3-month avg.": "Earnings, 3-mo avg.",
    "DFW CPI-U (every other month)": "DFW CPI-U",
    "Deflated by U.S. CPI-U (monthly)": "vs U.S. CPI", "Deflated by DFW CPI-U (every other month)": "vs DFW CPI",
    "Dallas-Plano-Irving division": "Dallas division", "Fort Worth-Arlington-Grapevine division": "Fort Worth division",
    "Monthly average of weekly survey": "30-year rate", "Units authorized, 12-month total": "Units, 12-month total",
    "DFW (from this project's seasonally adjusted rate)": "DFW gauge",
    "U.S. (from BLS's seasonally adjusted rate)": "U.S. gauge", "3-month average": "3-month avg.",
}


def trace(d: pd.DataFrame, column: str, name: str, *, kind="line", color="#1F3A5F",
          width=2.5, dash="solid", nd=2, dropna=False, hover_unit="", opacity=1.0, hover_decimals=1):
    s = d[column]
    if dropna:
        s = s.dropna()
    else:
        first, last = s.first_valid_index(), s.last_valid_index()
        s = s.loc[first:last] if last is not None else s.iloc[:0]
    return {"name": name, "hover_name": HOVER_NAMES.get(name, name), "kind": kind, "color": color,
            "width": width, "dash": dash,
            "opacity": opacity, "hover_unit": hover_unit, "hover_decimals": hover_decimals,
            "x": [t.strftime("%Y-%m-%d") for t in s.index], "y": [rnd(v, nd) for v in s]}


def p_tag(prelim: dict, key: str, t) -> str:
    """' (preliminary)' when the source flags that month as preliminary."""
    return " (preliminary)" if t is not None and pd.Timestamp(t) in prelim.get(key, set()) else ""


def headline_unemployment(d, prelim) -> str:
    t, v = last_valid(d["ur_dfw_nsa"])
    if t is None:
        return ""
    chg = value_at(d["ur_dfw_nsa_12m_change_pp"], t)
    tt, tx = last_valid(d["ur_tx_sa"])
    tu, us = last_valid(d["ur_us_sa"])
    direction = ("unchanged from" if is_num(chg) and round(chg, 1) == 0 else
                 f"{'up' if chg > 0 else 'down'} {fmt_num(abs(chg), 1)} points from")
    quals = "preliminary; " if p_tag(prelim, "ur_dfw_nsa", t) else ""
    return (f"DFW's unemployment rate was {fmt_num(v, 1, unit='%')} in {month_label(t)} "
            f"({quals}not seasonally adjusted), {direction} a year earlier. "
            f"Texas: {fmt_num(tx, 1, unit='%')} ({month_label(tt)}); U.S.: {fmt_num(us, 1, unit='%')} "
            f"({month_label(tu)}), both seasonally adjusted.")


def headline_jobs(d, prelim) -> str:
    t, dfw = last_valid(d["jobs_dfw_yoy_pct"])
    if t is None:
        return ""
    us = value_at(d["jobs_us_yoy_pct"], t)
    tx = value_at(d["jobs_tx_yoy_pct"], t)
    gap = value_at(d["jobs_gap_dfw_us_pp"], t)
    return (f"Over the year to {month_label(t)}{p_tag(prelim, 'jobs_dfw_sa', t)}, DFW payroll jobs grew "
            f"{fmt_num(dfw, 1, unit='%')}, {compare(dfw, us)} the U.S. ({fmt_num(us, 1, unit='%')}) and "
            f"{compare(dfw, tx)} Texas ({fmt_num(tx, 1, unit='%')}). "
            f"Gap vs the U.S.: {fmt_num(gap, 1, sign=True, unit=' points')}.")


def headline_monthly_jobs(d, prelim) -> str:
    t, v = last_valid(d["jobs_dfw_mom_k"])
    if t is None:
        return ""
    a3 = value_at(d["jobs_dfw_mom_k_3mma"], t)
    verb = "added" if v >= 0 else "lost"
    quals = "preliminary; " if p_tag(prelim, "jobs_dfw_sa", t) else ""
    return (f"DFW {verb} {fmt_num(abs(v), 1)}K jobs in {month_label(t)} ({quals}seasonally adjusted); "
            f"the 3-month average change is {fmt_num(a3, 1, sign=True)}K a month.")


def headline_wages(d, prelim) -> str:
    t, w = last_valid(d["ahe_dfw_yoy_pct"])
    tc, c = last_valid(d["cpi_dfw_yoy_pct"])
    if t is None or tc is None:
        return ""
    w_c = value_at(d["ahe_dfw_yoy_pct"], tc)
    us_c = value_at(d["cpi_us_yoy_pct"], tc)
    return (f"DFW average hourly earnings were {fmt_num(w, 1, sign=True, unit='%')} over the year to "
            f"{month_label(t)}{p_tag(prelim, 'ahe_dfw', t)}. In {month_label(tc)}, wages were "
            f"{fmt_num(w_c, 1, sign=True, unit='%')} against DFW inflation of {fmt_num(c, 1, unit='%')} "
            f"(U.S.: {fmt_num(us_c, 1, unit='%')}).")


def headline_real(d) -> str:
    t, r = last_valid(d["real_ahe_vs_dfw_cpi_pp"])
    if t is None:
        return ""
    r_us = value_at(d["real_ahe_vs_us_cpi_pp"], t)

    def word(x):
        return "rose" if x >= 0.05 else "fell" if x <= -0.05 else "was roughly flat"
    return (f"In {month_label(t)}, real hourly pay {word(r)} measured against DFW prices "
            f"({fmt_num(r, 1, sign=True, unit=' points')}) and {word(r_us)} against U.S. prices "
            f"({fmt_num(r_us, 1, sign=True, unit=' points')}). Metro wage data are noisy month to month.")


def headline_hpi(dq) -> str:
    if "hpi_dallas_yoy_pct" not in dq:
        return ""
    t, v = last_valid(dq["hpi_dallas_yoy_pct"])
    if t is None:
        return ""
    fw = value_at(dq["hpi_fortworth_yoy_pct"], t)
    peak_t = dq["hpi_dallas_yoy_pct"].idxmax()
    return (f"In {quarter_label(t)}, home prices were {fmt_num(v, 1, sign=True, unit='%')} vs a year earlier in "
            f"the Dallas-Plano-Irving division and {fmt_num(fw, 1, sign=True, unit='%')} in Fort "
            f"Worth-Arlington-Grapevine (FHFA all-transactions index). The largest gain since 2017 was "
            f"{fmt_num(dq['hpi_dallas_yoy_pct'].max(), 1, sign=True, unit='%')} for Dallas in {quarter_label(peak_t)}.")


def headline_mortgage(d, last_date, last_value) -> str:
    t, v = last_valid(d["mortgage30_avg"])
    if t is None:
        return ""
    y = value_at(d["mortgage30_avg"], t - pd.DateOffset(months=12))
    return (f"The 30-year fixed rate averaged {fmt_num(v, 2, unit='%')} in {month_label(t)}, "
            f"{fmt_num(abs(v - y), 2)} points {'above' if v >= y else 'below'} a year earlier; "
            f"latest weekly reading {fmt_num(last_value, 2, unit='%')} ({day_label(last_date)}).")


def headline_permits(d) -> str:
    t, v = last_valid(d["permits_dfw_12m"])
    if t is None:
        return ""
    yy = value_at(d["permits_dfw_12m_yoy_pct"], t)
    return (f"{v:,.0f} new homes were authorized in DFW in the 12 months to {month_label(t)}, "
            f"{fmt_num(abs(yy), 1, unit='%')} {'fewer' if yy < 0 else 'more'} than in the 12 months before.")


def headline_sahm(d) -> str:
    t, v = last_valid(d["sahm_dfw"])
    if t is None:
        return ""
    tu, u = last_valid(d["sahm_us"])
    side = "below" if v < SAHM_THRESHOLD else "at or above"
    return (f"DFW's gauge is {fmt_num(v, 2, unit=' points')} ({month_label(t)}), {side} the "
            f"{SAHM_THRESHOLD} threshold used for the national Sahm rule; the U.S. reading is "
            f"{fmt_num(u, 2, unit=' points')} ({month_label(tu)}). A heuristic, not a recession call.")


def headline_supersectors(table: pd.DataFrame, prelim) -> tuple[str, str]:
    if table.empty:
        return "", ""
    last = table["date"].max()
    cur = table[table["date"] == last].dropna(subset=["yoy_k"]).sort_values("yoy_k")
    if cur.empty:
        return "", last
    n_pos = int((cur["yoy_k"] > 0).sum())
    top, bottom = cur.iloc[-1], cur.iloc[0]
    text = (f"Over the year to {month_label(last)}{p_tag(prelim, 'ss_pbs', last)}, {n_pos} of {len(cur)} "
            f"DFW supersectors added jobs. "
            f"Largest gain: {top['supersector']} ({fmt_num(top['yoy_k'], 1, sign=True)}K). ")
    text += (f"Largest decline: {bottom['supersector']} ({fmt_num(bottom['yoy_k'], 1, sign=True)}K)."
             if bottom["yoy_k"] < 0 else
             f"Smallest gain: {bottom['supersector']} ({fmt_num(bottom['yoy_k'], 1, sign=True)}K).")
    return text, last


# -------------------------------------------------------------------- notes --
GAP_LABELS = {   # (measure, geography) used in the notes on missing months
    "ur_dfw_nsa": ("unemployment rate", "DFW"), "ur_tx_sa": ("unemployment rate", "Texas"),
    "ur_tx_nsa": ("unemployment rate", "Texas"), "ur_us_sa": ("unemployment rate", "U.S."),
    "ur_us_nsa": ("unemployment rate", "U.S."), "cpi_us": ("CPI-U", "U.S."),
    "cpi_dfw": ("CPI-U", "DFW"), "ahe_dfw": ("average hourly earnings", "DFW"),
    "jobs_dfw_sa": ("payroll jobs", "DFW"), "jobs_tx_sa": ("payroll jobs", "Texas"),
    "jobs_us_sa": ("payroll jobs", "U.S."), "hpi_dallas": ("home prices", "Dallas"),
}
YOY_SHOWN = {"ur_dfw_nsa", "cpi_us", "cpi_dfw", "ahe_dfw", "jobs_dfw_sa", "jobs_tx_sa",
             "jobs_us_sa", "hpi_dallas"}   # series whose 12-month change the page shows


def gap_notes(obs: pd.DataFrame, meta: pd.DataFrame) -> list[str]:
    """One note per month in which a source published a value as unavailable."""
    miss = obs[obs["value"].isna() & obs["footnote"].ne("")].copy()
    miss["foot"] = miss["footnote"].str.strip().str.rstrip(".")
    notes = []
    geo_order = ["DFW", "Dallas", "Texas", "U.S."]
    measure_order = list(dict.fromkeys(m for m, _ in GAP_LABELS.values()))
    for date, grp in miss.groupby("date", sort=True):
        measures: dict[str, list[str]] = {}
        for k in sorted(grp["key"], key=lambda k: list(GAP_LABELS).index(k) if k in GAP_LABELS else 99):
            measure, geo = GAP_LABELS.get(k, (meta.loc[k, "title"] if k in meta.index else str(k), ""))
            geos = measures.setdefault(measure, [])
            if geo and geo not in geos:
                geos.append(geo)
        ordered = sorted(measures.items(),
                         key=lambda kv: measure_order.index(kv[0]) if kv[0] in measure_order else 99)
        what = "; ".join(
            f"{m} ({', '.join(sorted(g, key=lambda x: geo_order.index(x) if x in geo_order else 9))})"
            if g else m for m, g in ordered)
        later = date + pd.DateOffset(months=12)
        knock_on = []
        for k in grp["key"].unique():
            if k in YOY_SHOWN and len(obs[(obs["key"] == k) & (obs["date"] == later) & obs["value"].notna()]):
                m, g = GAP_LABELS.get(k, (str(k), ""))
                knock_on.append(f"{g} {m}".strip())
        foots = "; ".join(f"“{f}.”" for f in sorted(grp["foot"].unique()))
        text = (f"{month_label(date)}: no value was published for {what}. BLS footnote: {foots} "
                f"Charts leave a gap, and 12-month changes and 3-month averages that need "
                f"{month_label(date)} are left blank")
        text += (f", including the 12-month change for {month_label(later)} ({', '.join(knock_on)})."
                 if knock_on else ".")
        notes.append(text)
    return notes


def asof_list(obs, meta, mort_last_date) -> list[dict]:
    out = []
    for key, label in ASOF_LABELS.items():
        if key not in meta.index:
            continue
        s = obs[(obs["key"] == key) & obs["value"].notna()]
        if s.empty:
            continue
        last = s.sort_values("date").iloc[-1]
        when = (day_label(last["date"]) if key == "mortgage30" else
                quarter_label(last["date"]) if key in QUARTERLY_KEYS else month_label(last["date"]))
        out.append({"label": label, "as_of": when, "preliminary": bool(last["preliminary"]),
                    "series_id": meta.loc[key, "series_id"], "url": meta.loc[key, "url"],
                    "adjustment": meta.loc[key, "seasonal_adjustment"]})
    return out


# --------------------------------------------------------------------- main --
def main() -> int:
    obs, meta = load()
    wide = build_monthly(obs, meta.index)
    mort, mort_last_date, mort_last_value = mortgage_monthly(obs)
    d, check = derive(wide, mort)
    d = d.loc[d.dropna(how="all").index.min():d.dropna(how="all").index.max()]
    ss_table = supersector_table(wide)
    dq = build_quarterly(obs)
    prelim = {k: set(g["date"]) for k, g in obs[obs["preliminary"]].groupby("key")}

    # ---- files for analysis and download
    PROCESSED.mkdir(parents=True, exist_ok=True)
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    out = d.copy()
    out.index = out.index.strftime("%Y-%m-%d")
    out.index.name = "date"
    out.round(4).to_csv(DERIVED_PATH, lineterminator="\n")
    out.round(4).to_csv(DOWNLOAD_PATH, lineterminator="\n")
    ss_table.round(4).to_csv(SUPERSECTOR_PATH, index=False, lineterminator="\n")
    q_out = dq.copy()
    q_out.insert(0, "quarter", [quarter_label(t) for t in q_out.index])
    q_out.index = q_out.index.strftime("%Y-%m-%d")
    q_out.index.name = "date"
    q_out.round(4).to_csv(QUARTERLY_PATH, lineterminator="\n")
    q_out.round(4).to_csv(DOWNLOAD_Q_PATH, lineterminator="\n")

    # ---- chart payloads
    navy, teal, yellow, orange, gray = "#1F3A5F", "#2A9D8F", "#E9C46A", "#E76F51", "#8D99AE"
    ss_text, ss_date = headline_supersectors(ss_table, prelim)
    ss_cur = (ss_table[ss_table["date"] == ss_date].dropna(subset=["yoy_k"]).sort_values("yoy_k")
              if ss_date else ss_table.iloc[:0])
    charts = {
        "unemployment": {
            "title": "Unemployment rate: DFW vs Texas vs U.S.",
            "headline": headline_unemployment(d, prelim), "y_title": "Percent of labor force",
            "traces": [
                trace(d, "ur_dfw_nsa", "DFW, as published (not seasonally adjusted)", color=gray,
                      width=1.2, dash="dot", nd=1, hover_unit="%"),
                trace(d, "ur_dfw_sa_est", "DFW, seasonally adjusted (this project's estimate)",
                      color=navy, width=3, hover_unit="%"),
                trace(d, "ur_tx_sa", "Texas (seasonally adjusted)", color=teal, nd=1, hover_unit="%"),
                trace(d, "ur_us_sa", "U.S. (seasonally adjusted)", color=orange, nd=1, hover_unit="%"),
            ],
        },
        "jobs": {
            "title": "Payroll job growth, 12-month % change",
            "headline": headline_jobs(d, prelim), "y_title": "Percent / percentage points",
            "traces": [
                trace(d, "jobs_gap_dfw_us_pp", "Gap: DFW minus U.S. (points)", kind="bar", color=gray,
                      opacity=0.45, hover_unit=" pts"),
                trace(d, "jobs_dfw_yoy_pct", "DFW", color=navy, width=3, hover_unit="%"),
                trace(d, "jobs_tx_yoy_pct", "Texas", color=teal, hover_unit="%"),
                trace(d, "jobs_us_yoy_pct", "U.S.", color=orange, hover_unit="%"),
            ],
            "zero_line": True, "start": "2017-01-01",
        },
        "monthly_jobs": {
            "title": "DFW monthly job change (seasonally adjusted)",
            "headline": headline_monthly_jobs(d, prelim), "y_title": "Thousands of jobs",
            "traces": [
                trace(d, "jobs_dfw_mom_k", "Monthly change", kind="bar", color=teal, opacity=0.6,
                      nd=1, hover_unit="K"),
                trace(d, "jobs_dfw_mom_k_3mma", "3-month average", color=navy, width=2.5,
                      nd=1, hover_unit="K"),
            ],
            "zero_line": True,
        },
        "wages": {
            "title": "Wages vs inflation, 12-month % change",
            "headline": headline_wages(d, prelim), "y_title": "Percent",
            "traces": [
                trace(d, "ahe_dfw_yoy_pct", "DFW hourly earnings, monthly", color=navy, width=1.2,
                      opacity=0.45, hover_unit="%"),
                trace(d, "ahe_dfw_yoy_pct_3mma", "DFW hourly earnings, 3-month avg.", color=navy,
                      width=3, hover_unit="%"),
                trace(d, "cpi_dfw_yoy_pct", "DFW CPI-U (every other month)", kind="line+markers", color=teal,
                      dropna=True, hover_unit="%"),
                trace(d, "cpi_us_yoy_pct", "U.S. CPI-U", color=orange, dash="dash", hover_unit="%"),
            ],
            "zero_line": True, "start": "2017-01-01",
        },
        "real_wages": {
            "title": "Real wage growth: wage growth minus inflation",
            "headline": headline_real(d), "y_title": "Percentage points",
            "traces": [
                trace(d, "real_ahe_vs_us_cpi_pp", "Deflated by U.S. CPI-U (monthly)", kind="bar",
                      color=gray, opacity=0.5, hover_unit=" pts"),
                trace(d, "real_ahe_vs_dfw_cpi_pp", "Deflated by DFW CPI-U (every other month)",
                      kind="line+markers", color=navy, dropna=True, hover_unit=" pts"),
            ],
            "zero_line": True, "start": "2017-01-01",
        },
        "hpi": {
            "title": "Home prices, % change from a year earlier (FHFA, quarterly)",
            "headline": headline_hpi(dq), "y_title": "Percent", "x_hoverformat": "Q%q %Y",
            "traces": [trace(dq, "hpi_dallas_yoy_pct", "Dallas-Plano-Irving division", kind="line+markers",
                             color=navy, width=2.5, hover_unit="%"),
                       trace(dq, "hpi_fortworth_yoy_pct", "Fort Worth-Arlington-Grapevine division",
                             kind="line+markers", color=teal, width=2, hover_unit="%")],
            "zero_line": True, "start": "2017-01-01",
        },
        "mortgage": {
            "title": "30-year fixed mortgage rate (U.S.)",
            "headline": headline_mortgage(d, mort_last_date, mort_last_value),
            "y_title": "Percent (monthly average)",
            "traces": [trace(d, "mortgage30_avg", "Monthly average of weekly survey", color=orange,
                             width=3, hover_unit="%", hover_decimals=2)],
        },
        "permits": {
            "title": "DFW housing units authorized, past 12 months",
            "headline": headline_permits(d), "y_title": "Housing units (12-month total)",
            "traces": [trace(d, "permits_dfw_12m", "Units authorized, 12-month total", color=teal,
                             width=3, nd=0, hover_unit="", hover_decimals=0)],
            "start": "2016-12-01",
        },
        "sahm": {
            "title": "Sahm-rule-style gauge (heuristic)",
            "headline": headline_sahm(d), "y_title": "Percentage points",
            "traces": [
                trace(d, "sahm_dfw", "DFW (from this project's seasonally adjusted rate)", color=navy,
                      width=3, hover_unit=" pts", hover_decimals=2),
                trace(d, "sahm_us", "U.S. (from BLS's seasonally adjusted rate)", color=orange,
                      hover_unit=" pts", hover_decimals=2),
            ],
            "threshold": SAHM_THRESHOLD, "zero_line": True,
        },
        "supersectors": {
            "title": f"DFW jobs by supersector: change over the year to {month_label(ss_date)}"
                     if ss_date else "DFW jobs by supersector",
            "headline": ss_text, "y_title": "", "x_title": "Change in jobs over 12 months (thousands)",
            "categories": ss_cur["supersector"].tolist(),
            "values": [rnd(v, 1) for v in ss_cur["yoy_k"]],
            "pct": [rnd(v, 1) for v in ss_cur["yoy_pct"]],
            "levels": [rnd(v, 1) for v in ss_cur["jobs_k"]],
            "preliminary": bool(ss_date) and pd.Timestamp(ss_date) in prelim.get("ss_pbs", set()),
        },
    }

    # ---- dotted markers where a source published "unavailable" for a month
    missing = obs[obs["value"].isna() & obs["footnote"].ne("")]
    chart_keys = {"unemployment": {"ur_dfw_nsa", "ur_tx_sa", "ur_us_sa"},
                  "sahm": {"ur_dfw_nsa", "ur_us_sa"}, "wages": {"cpi_us"}, "real_wages": {"cpi_us"}}
    for name, keys in chart_keys.items():
        dates = sorted(set(missing.loc[missing["key"].isin(keys), "date"]))
        charts[name]["gap_marks"] = [{"x": t.strftime("%Y-%m-%d"), "text": f"{month_label(t)}: not published"}
                                     for t in dates]

    # ---- derived signals strip
    signals = []
    t, gap = last_valid(d["jobs_gap_dfw_us_pp"])
    if t is not None:
        signals.append({"label": "Job growth gap, DFW minus U.S.",
                        "value_fmt": fmt_num(gap, 1, sign=True, unit=" pts"), "period": month_label(t),
                        "note": (f"3-month average {fmt_num(value_at(d['jobs_gap_dfw_us_pp_3mma'], t), 1, sign=True)}"
                                 f" pts")})
    t, ann = last_valid(d["jobs_dfw_3m_ann_pct"])
    if t is not None:
        signals.append({"label": "DFW job growth, last 3 months (annualized)",
                        "value_fmt": fmt_num(ann, 1, sign=True, unit="%"), "period": month_label(t),
                        "note": f"vs {fmt_num(value_at(d['jobs_dfw_yoy_pct'], t), 1, sign=True, unit='%')} over 12 months"})
    t, r = last_valid(d["real_ahe_vs_dfw_cpi_pp"])
    if t is not None:
        signals.append({"label": "Real wage growth (vs DFW CPI)",
                        "value_fmt": fmt_num(r, 1, sign=True, unit=" pts"), "period": month_label(t),
                        "note": f"vs U.S. CPI: {fmt_num(value_at(d['real_ahe_vs_us_cpi_pp'], t), 1, sign=True)} pts"})
    t, sg = last_valid(d["sahm_dfw"])
    if t is not None:
        signals.append({"label": "Sahm-style gauge, DFW (heuristic)",
                        "value_fmt": fmt_num(sg, 2, unit=" pts"), "period": month_label(t),
                        "note": f"threshold {SAHM_THRESHOLD}; U.S. {fmt_num(last_valid(d['sahm_us'])[1], 2)}"})
    t, br = last_valid(d["breadth_share_pos"])
    if t is not None:
        signals.append({"label": "Supersectors adding jobs (12 months)",
                        "value_fmt": f"{round(br / 10):.0f} of 10", "period": month_label(t),
                        "note": "share of the 10 supersectors with more jobs than a year earlier"})

    sa_check = (f"Check of the seasonal adjustment: the same method applied to the Texas and U.S. "
                f"rates matches BLS's official seasonally adjusted rates with a mean absolute error of "
                f"{check['tx']['mae_pp']:.2f} and {check['us']['mae_pp']:.2f} percentage points "
                f"({check['tx']['months']} and {check['us']['months']} months).")
    payload = {
        "title": "DFW Economy Dashboard",
        "repo_url": REPO_URL,
        "as_of": asof_list(obs, meta, mort_last_date),
        "kpis": kpis(d, dq, prelim, mort_last_date, mort_last_value),
        "signals": signals,
        "charts": charts,
        "notes": {
            "gaps": gap_notes(obs, meta),
            "seasonal_adjustment_check": sa_check,
            "seasonal_adjustment_check_values": {
                geo: {k: rnd(v, 3) for k, v in check[geo].items()} for geo in ("tx", "us")},
            "dfw_seasonal_factors_pp": {str(k): rnd(v, 3) for k, v in check["dfw_factors"].items()},
        },
        "sources": [
            {"name": "U.S. Bureau of Labor Statistics (BLS), Public Data API",
             "detail": "LAUS (unemployment), CES (payroll jobs, hourly earnings), CPS (U.S. unemployment), CPI-U",
             "url": "https://www.bls.gov/developers/"},
            {"name": "FRED, Federal Reserve Bank of St. Louis",
             "detail": "Distribution of the FHFA house price, mortgage-rate and building-permit series",
             "url": "https://fred.stlouisfed.org/"},
            {"name": "U.S. Federal Housing Finance Agency (FHFA), All-Transactions House Price Index",
             "detail": "Dallas-Plano-Irving and Fort Worth-Arlington-Grapevine divisions, quarterly; "
                       "public domain (U.S. government data), retrieved from FRED",
             "url": "https://fred.stlouisfed.org/series/ATNHPIUS19124Q"},
            {"name": "Freddie Mac Primary Mortgage Market Survey",
             "detail": "30-year fixed rate, U.S., weekly",
             "url": "https://fred.stlouisfed.org/series/MORTGAGE30US"},
            {"name": "U.S. Census Bureau, Building Permits Survey",
             "detail": "New private housing units authorized, Dallas-Fort Worth-Arlington MSA",
             "url": "https://fred.stlouisfed.org/series/DALL148BPPRIV"},
        ],
    }

    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if JSON_PATH.exists():
        try:
            if json.loads(JSON_PATH.read_text(encoding="utf-8")).get("content_hash") == content_hash:
                print(f"{JSON_PATH.relative_to(ROOT)} unchanged (same data); not rewritten.")
                print_summary(payload, check)
                return 0
        except json.JSONDecodeError:
            pass
    payload["content_hash"] = content_hash
    payload["built_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    JSON_PATH.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {JSON_PATH.relative_to(ROOT)} ({JSON_PATH.stat().st_size / 1024:.0f} KB).")
    print_summary(payload, check)
    return 0


def print_summary(payload: dict, check: dict) -> None:
    print(f"Wrote {DERIVED_PATH.relative_to(ROOT)}, {QUARTERLY_PATH.relative_to(ROOT)}, "
          f"{SUPERSECTOR_PATH.relative_to(ROOT)}, {DOWNLOAD_PATH.relative_to(ROOT)}, "
          f"{DOWNLOAD_Q_PATH.relative_to(ROOT)}.")
    for k in payload["kpis"]:
        print(f"  KPI {k['label']}: {k['value_fmt']} ({k['period']}{', preliminary' if k['preliminary'] else ''})")
    print(f"  {payload['notes']['seasonal_adjustment_check']}")


if __name__ == "__main__":
    raise SystemExit(main())
