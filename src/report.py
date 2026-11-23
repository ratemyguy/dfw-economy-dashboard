#!/usr/bin/env python3
"""README snapshot: the results tables and static charts quoted in the README.

Reads   data/processed/derived_monthly.csv, supersectors.csv, observations.csv, series.csv
        docs/data/dashboard.json
Writes  results/*.csv   every number quoted in the README (results/key_numbers.csv is the index)
        images/*.png    the four README charts

This runs locally (matplotlib). The monthly GitHub Actions job does not run it, so
the README, results/ and images/ are a dated snapshot while the live page refreshes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import statsmodels.api as sm  # noqa: E402

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))           # explicit: reuse transform.py's methods (scripts run with -I)
import transform as tf  # noqa: E402

ROOT = SRC.parent
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "results"
IMAGES = ROOT / "images"
NAVY, TEAL, YELLOW, ORANGE, GRAY = "#1F3A5F", "#2A9D8F", "#E9C46A", "#E76F51", "#8D99AE"
BLS_NOTE = "Source: U.S. Bureau of Labor Statistics (CES, LAUS, CPS, CPI) via the BLS Public Data API."

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "font.size": 10.5, "axes.titlesize": 12.5, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": "#e6eaf0",
    "grid.linewidth": 0.8, "axes.edgecolor": "#9aa5b4", "legend.frameon": False,
})


def mlabel(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %Y")


# ------------------------------------------------------------------ numbers --
class Numbers:
    """Collects every number the README quotes, with the period and a description."""

    def __init__(self):
        self.rows = []

    def add(self, key, value, unit, period, description, decimals=1):
        v = float(value)
        shown = round(v, decimals) + 0.0          # + 0.0 turns -0.0 into 0.0
        display = str(int(v)) if unit == "year" else f"{shown:,.{decimals}f}"
        self.rows.append({"key": key, "value": round(v, 4), "decimals": decimals, "display": display,
                          "unit": unit, "period": period, "description": description})
        return value

    def frame(self):
        return pd.DataFrame(self.rows)


def period_avg(s: pd.Series, start: str, end: str) -> float:
    return s.loc[start:end].mean()


def main() -> int:
    RESULTS.mkdir(exist_ok=True)
    IMAGES.mkdir(exist_ok=True)
    d = pd.read_csv(PROCESSED / "derived_monthly.csv", parse_dates=["date"], index_col="date")
    ss = pd.read_csv(PROCESSED / "supersectors.csv", parse_dates=["date"])
    obs, meta = tf.load()
    dash = json.loads((ROOT / "docs" / "data" / "dashboard.json").read_text(encoding="utf-8"))
    prelim = {k: set(g["date"]) for k, g in obs[obs["preliminary"]].groupby("key")}
    N = Numbers()

    # ---------------------------------------------------------- inventory --
    inv = []
    for name, path in [("observations.csv", PROCESSED / "observations.csv"),
                       ("series.csv", PROCESSED / "series.csv"),
                       ("derived_monthly.csv", PROCESSED / "derived_monthly.csv"),
                       ("supersectors.csv", PROCESSED / "supersectors.csv"),
                       ("derived_quarterly.csv", PROCESSED / "derived_quarterly.csv")]:
        df = pd.read_csv(path)
        inv.append({"file": f"data/processed/{name}", "rows": len(df), "columns": df.shape[1],
                    "first_date": df["date"].min() if "date" in df else "",
                    "last_date": df["date"].max() if "date" in df else ""})
    pd.DataFrame(inv).to_csv(RESULTS / "data_inventory.csv", index=False)
    n_series = len(meta)
    N.add("n_series", n_series, "series", "", "Series fetched (BLS + FRED)", 0)
    N.add("n_bls_series", (meta["series_id"].str.match(r"^(LA|LN|SM|CE|CU)")).sum(), "series", "",
          "BLS series", 0)
    N.add("n_observation_rows", inv[0]["rows"], "rows", "", "Rows in data/processed/observations.csv", 0)
    N.add("n_months_panel", inv[2]["rows"], "months", f"{inv[2]['first_date']} to {inv[2]['last_date']}",
          "Months in the derived monthly panel", 0)
    N.add("n_derived_columns", inv[2]["columns"] - 1, "columns", "", "Metric columns in derived_monthly.csv", 0)
    N.add("n_supersector_rows", inv[3]["rows"], "rows", "", "Rows in data/processed/supersectors.csv", 0)
    N.add("n_quarters_panel", inv[4]["rows"], "quarters", f"{inv[4]['first_date']} to {inv[4]['last_date']}",
          "Quarters in data/processed/derived_quarterly.csv (FHFA house prices)", 0)
    N.add("n_fred_series", (~meta["series_id"].str.match(r"^(LA|LN|SM|CE|CU)")).sum(), "series", "",
          "FRED series", 0)

    # --------------------------------------------------------------- jobs --
    t_jobs, v = tf.last_valid(d["jobs_dfw_yoy_pct"])
    P = mlabel(t_jobs)
    N.add("jobs_dfw_yoy_latest", v, "%", P, "DFW payroll jobs, 12-month % change (SA, preliminary)")
    N.add("jobs_us_yoy_latest", d.at[t_jobs, "jobs_us_yoy_pct"], "%", P, "U.S. payroll jobs, 12-month % change")
    N.add("jobs_tx_yoy_latest", d.at[t_jobs, "jobs_tx_yoy_pct"], "%", P, "Texas payroll jobs, 12-month % change")
    N.add("jobs_gap_latest", d.at[t_jobs, "jobs_gap_dfw_us_pp"], "pts", P, "DFW minus U.S. job growth")
    N.add("jobs_dfw_yoy_k_latest", d.at[t_jobs, "jobs_dfw_yoy_k"], "thousand jobs", P,
          "DFW jobs added over 12 months (SA)")
    N.add("jobs_dfw_level_latest_m", d.at[t_jobs, "jobs_dfw_sa"] / 1000, "million jobs", P,
          "DFW payroll jobs (SA)", 2)
    N.add("jobs_dfw_mom_latest", d.at[t_jobs, "jobs_dfw_mom_k"], "thousand jobs", P,
          "DFW monthly job change (SA, preliminary)")
    N.add("jobs_dfw_mom_3mma_latest", d.at[t_jobs, "jobs_dfw_mom_k_3mma"], "thousand jobs/month", P,
          "DFW 3-month average monthly job change")
    N.add("jobs_dfw_3m_ann_latest", d.at[t_jobs, "jobs_dfw_3m_ann_pct"], "%", P,
          "DFW job growth over the latest 3 months, annualized")
    base = t_jobs - pd.DateOffset(months=12)
    N.add("jobs_dfw_mom_base_month", d.at[base, "jobs_dfw_mom_k"], "thousand jobs", mlabel(base),
          "DFW monthly job change in the base month of the latest 12-month comparison")
    N.add("jobs_us_mom_base_month", d.at[base, "jobs_us_mom_k"], "thousand jobs", mlabel(base),
          "U.S. monthly job change in the base month of the latest 12-month comparison", 0)
    last12 = (t_jobs - pd.DateOffset(months=11)).strftime("%Y-%m-%d"), t_jobs.strftime("%Y-%m-%d")
    p12 = f"{mlabel(last12[0])} to {mlabel(last12[1])}"
    periods = {"2017-2019": ("2017-01-01", "2019-12-01"), "2022": ("2022-01-01", "2022-12-01"),
               "2023": ("2023-01-01", "2023-12-01"), "2024": ("2024-01-01", "2024-12-01"),
               "2025": ("2025-01-01", "2025-12-01"), f"last 12 months ({p12})": last12}
    rows = []
    for label, (a, b) in periods.items():
        rows.append({"period": label, **{c: round(period_avg(d[c], a, b), 3) for c in
                     ("jobs_dfw_yoy_pct", "jobs_tx_yoy_pct", "jobs_us_yoy_pct", "jobs_gap_dfw_us_pp",
                      "jobs_dfw_mom_k", "jobs_us_mom_k")}})
    by_period = pd.DataFrame(rows)
    by_period.to_csv(RESULTS / "job_growth_by_period.csv", index=False)
    pre = by_period.iloc[0]
    rec = by_period.iloc[-1]
    N.add("jobs_dfw_yoy_avg_2017_2019", pre["jobs_dfw_yoy_pct"], "%", "2017-2019",
          "DFW average 12-month job growth, 2017-2019")
    N.add("jobs_us_yoy_avg_2017_2019", pre["jobs_us_yoy_pct"], "%", "2017-2019",
          "U.S. average 12-month job growth, 2017-2019")
    N.add("jobs_dfw_yoy_avg_last12", rec["jobs_dfw_yoy_pct"], "%", p12,
          "DFW average 12-month job growth over the latest 12 months")
    N.add("jobs_us_yoy_avg_last12", rec["jobs_us_yoy_pct"], "%", p12,
          "U.S. average 12-month job growth over the latest 12 months", 2)
    N.add("jobs_dfw_pace_share_of_2017_2019", rec["jobs_dfw_yoy_pct"] / pre["jobs_dfw_yoy_pct"] * 100, "%",
          p12, "Latest-12-month average growth as a share of the 2017-2019 average", 0)
    N.add("jobs_dfw_mom_avg_2017_2019", pre["jobs_dfw_mom_k"], "thousand jobs/month", "2017-2019",
          "DFW average monthly job gain, 2017-2019")
    gap = d.loc["2017-01-01":, "jobs_gap_dfw_us_pp"].dropna()
    N.add("jobs_gap_positive_months", (gap > 0).sum(), "months", f"Jan 2017 to {P}",
          "Months in which DFW job growth beat the U.S.", 0)
    N.add("jobs_gap_months_total", len(gap), "months", f"Jan 2017 to {P}", "Months compared", 0)

    # Difference in means, 2017-2019 vs the latest 12 months, Newey-West (HAC) standard errors:
    # monthly 12-month changes overlap by 11 months, so 12 lags.
    hac_rows = []
    for col in ("jobs_gap_dfw_us_pp", "jobs_dfw_yoy_pct"):
        pre_s, rec_s = d.loc["2017-01-01":"2019-12-01", col], d.loc[last12[0]:last12[1], col]
        y = pd.concat([pre_s, rec_s])
        X = sm.add_constant(pd.Series([0] * len(pre_s) + [1] * len(rec_s), index=y.index, name="recent"))
        fit = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 12})
        lo, hi = fit.conf_int().loc["recent"]
        hac_rows.append({"metric": col, "mean_2017_2019": fit.params["const"],
                         "mean_latest_12m": fit.params["const"] + fit.params["recent"],
                         "difference": fit.params["recent"], "hac_se": fit.bse["recent"], "ci95_low": lo,
                         "ci95_high": hi, "p_value": fit.pvalues["recent"], "n_months": int(fit.nobs),
                         "hac_lags": 12})
    hac = pd.DataFrame(hac_rows)
    hac.round(4).to_csv(RESULTS / "hac_difference_in_means.csv", index=False)
    g = hac.iloc[0]
    N.add("gap_mean_2017_2019", g["mean_2017_2019"], "pts", "2017-2019", "Average DFW minus U.S. gap", 2)
    N.add("gap_mean_latest_12m", g["mean_latest_12m"], "pts", p12, "Average gap, latest 12 months", 2)
    N.add("gap_difference", g["difference"], "pts", p12, "Change in the average gap (HAC regression)", 2)
    N.add("gap_ci95_low", g["ci95_low"], "pts", p12, "95% CI low (Newey-West, 12 lags)", 2)
    N.add("gap_ci95_high", g["ci95_high"], "pts", p12, "95% CI high (Newey-West, 12 lags)", 2)
    N.add("gap_p_value", g["p_value"], "", p12, "p-value for the change in the gap", 3)
    N.add("gap_test_n_months", g["n_months"], "months", p12, "Monthly observations in the HAC regression", 0)

    # ------------------------------------------------------- unemployment --
    t_ur, v = tf.last_valid(d["ur_dfw_nsa"])
    U = mlabel(t_ur)
    N.add("ur_dfw_nsa_latest", v, "%", U, "DFW unemployment rate, as published (NSA, preliminary)")
    N.add("ur_dfw_nsa_year_ago", d.at[t_ur - pd.DateOffset(months=12), "ur_dfw_nsa"], "%",
          mlabel(t_ur - pd.DateOffset(months=12)), "DFW unemployment rate a year earlier (NSA)")
    N.add("ur_dfw_nsa_12m_change", d.at[t_ur, "ur_dfw_nsa_12m_change_pp"], "pts", U,
          "Change in the DFW rate over 12 months")
    N.add("ur_dfw_sa_est_latest", d.at[t_ur, "ur_dfw_sa_est"], "%", U,
          "DFW unemployment rate, seasonally adjusted by this project")
    N.add("ur_tx_sa_same_month", d.at[t_ur, "ur_tx_sa"], "%", U, "Texas unemployment rate (SA), same month")
    N.add("ur_us_sa_same_month", d.at[t_ur, "ur_us_sa"], "%", U, "U.S. unemployment rate (SA), same month")
    chk = dash["notes"]["seasonal_adjustment_check_values"]
    N.add("sa_check_mae_tx", chk["tx"]["mae_pp"], "pts", "2016-2026",
          "Mean absolute gap: this project's SA method vs BLS official SA, Texas", 2)
    N.add("sa_check_mae_us", chk["us"]["mae_pp"], "pts", "2016-2026",
          "Mean absolute gap: this project's SA method vs BLS official SA, U.S.", 2)
    N.add("sa_check_max_tx", chk["tx"]["max_abs_pp"], "pts", "2016-2026",
          "Largest monthly gap vs BLS official SA, Texas", 2)
    N.add("sa_check_max_us", chk["us"]["max_abs_pp"], "pts", "2016-2026",
          "Largest monthly gap vs BLS official SA, U.S.", 2)
    pd.DataFrame([{"geography": g, **chk[g]} for g in ("tx", "us")]).to_csv(
        RESULTS / "seasonal_adjustment_check.csv", index=False)

    # Sahm-style gauge and its sensitivity to the seasonal adjustment method
    t_s, v = tf.last_valid(d["sahm_dfw"])
    N.add("sahm_dfw_latest", v, "pts", mlabel(t_s), "DFW Sahm-style gauge (heuristic)", 2)
    recent = d.loc[(t_s - pd.DateOffset(months=11)).strftime("%Y-%m-%d"):t_s.strftime("%Y-%m-%d"), "sahm_dfw"]
    N.add("sahm_dfw_max_12m", recent.max(), "pts", mlabel(recent.idxmax()),
          "Highest DFW gauge reading in the latest 12 months", 2)
    t_su, vu = tf.last_valid(d["sahm_us"])
    N.add("sahm_us_latest", vu, "pts", mlabel(t_su), "U.S. Sahm-style gauge from BLS SA rate", 2)
    nsa = d["ur_dfw_nsa"]
    variants = {
        "median factors (dashboard method)": tf.seasonal_adjust(nsa)[0],
        "mean factors": nsa - nsa.index.month.map(_mean_factors(nsa)).to_numpy(),
        "Texas official factors": nsa - (d["ur_tx_nsa"] - d["ur_tx_sa"]),
    }
    sens = []
    for name, sa in variants.items():
        g, _ = tf.sahm_gauge(sa)
        win = g.loc[recent.index.min():recent.index.max()]
        sens.append({"method": name, "latest": round(g.loc[t_s], 3), "max_latest_12m": round(win.max(), 3),
                     "max_month": mlabel(win.idxmax()), "months_at_or_above_0_5_since_2022":
                     int((g.loc["2022-01-01":] >= 0.5).sum())})
    sens = pd.DataFrame(sens)
    sens.to_csv(RESULTS / "sahm_sensitivity.csv", index=False)
    g_mean, _ = tf.sahm_gauge(variants["mean factors"])
    hits = g_mean.loc["2022-01-01":][g_mean.loc["2022-01-01":] >= 0.5]
    if len(hits):
        N.add("sahm_mean_variant_hits", len(hits), "months", ", ".join(mlabel(t) for t in hits.index),
              "Months the mean-factor variant reached 0.5 since 2022", 0)
        N.add("sahm_mean_variant_hit_job_growth", d.loc[hits.index, "jobs_dfw_yoy_pct"].mean(), "%",
              ", ".join(mlabel(t) for t in hits.index), "DFW 12-month job growth in those months (average)")
    N.add("sahm_dfw_max_12m_low", sens["max_latest_12m"].min(), "pts", "", "Lowest peak across 3 SA methods", 2)
    N.add("sahm_dfw_max_12m_high", sens["max_latest_12m"].max(), "pts", "", "Highest peak across 3 SA methods", 2)
    d[["ur_dfw_nsa", "ur_dfw_sa_est", "ur_dfw_3mma", "sahm_dfw", "ur_us_sa", "sahm_us"]].loc["2016-01-01":].round(3) \
        .to_csv(RESULTS / "sahm_gauge_monthly.csv")

    # -------------------------------------------------------- wages/prices --
    t_w, v = tf.last_valid(d["ahe_dfw_yoy_pct"])
    N.add("ahe_dfw_level_latest", d.at[t_w, "ahe_dfw"], "$/hour", mlabel(t_w),
          "DFW average hourly earnings, all private employees (NSA)", 2)
    N.add("ahe_dfw_yoy_latest", v, "%", mlabel(t_w), "DFW hourly earnings, 12-month % change")
    t_c, c = tf.last_valid(d["cpi_dfw_yoy_pct"])
    C = mlabel(t_c)
    N.add("ahe_dfw_yoy_cpi_month", d.at[t_c, "ahe_dfw_yoy_pct"], "%", C, "DFW hourly earnings growth, CPI month")
    N.add("cpi_dfw_yoy_latest", c, "%", C, "DFW CPI-U inflation, 12 months")
    N.add("cpi_dfw_yoy_year_ago", d.at[t_c - pd.DateOffset(months=12), "cpi_dfw_yoy_pct"], "%",
          mlabel(t_c - pd.DateOffset(months=12)), "DFW CPI-U inflation a year earlier")
    N.add("cpi_us_yoy_cpi_month", d.at[t_c, "cpi_us_yoy_pct"], "%", C, "U.S. CPI-U inflation, same month")
    N.add("real_wage_dfw_cpi_latest", d.at[t_c, "real_ahe_vs_dfw_cpi_pp"], "pts", C,
          "Real wage growth: wages minus DFW CPI")
    N.add("real_wage_us_cpi_latest", d.at[t_c, "real_ahe_vs_us_cpi_pp"], "pts", C,
          "Real wage growth: wages minus U.S. CPI")
    win = d.loc[(t_c - pd.DateOffset(months=11)).strftime("%Y-%m-%d"):t_c.strftime("%Y-%m-%d"), "real_ahe_vs_us_cpi_pp"]
    W = f"{mlabel(win.index.min())} to {C}"
    N.add("real_wage_us_cpi_avg_12m", win.mean(), "pts", W, "Average real wage growth vs U.S. CPI, 12 months")
    N.add("real_wage_us_cpi_months_used", win.notna().sum(), "months", W, "Months with data in that average", 0)
    N.add("real_wage_us_cpi_min_12m", win.min(), "pts", mlabel(win.idxmin()), "Lowest monthly reading in that window")
    N.add("real_wage_us_cpi_max_12m", win.max(), "pts", mlabel(win.idxmax()), "Highest monthly reading in that window")
    us_cpi = d["cpi_us_yoy_pct"].loc["2026-01-01":]
    N.add("cpi_us_yoy_2026_min", us_cpi.min(), "%", mlabel(us_cpi.idxmin()), "Lowest U.S. CPI inflation in 2026")
    N.add("cpi_us_yoy_2026_max", us_cpi.max(), "%", mlabel(us_cpi.idxmax()), "Highest U.S. CPI inflation in 2026")
    d[["ahe_dfw", "ahe_dfw_yoy_pct", "ahe_dfw_yoy_pct_3mma", "cpi_dfw_yoy_pct", "cpi_us_yoy_pct",
       "real_ahe_vs_dfw_cpi_pp", "real_ahe_vs_us_cpi_pp"]].loc["2017-01-01":].round(3) \
        .to_csv(RESULTS / "wages_vs_inflation_monthly.csv")

    # ------------------------------------------------------------ housing --
    # FHFA house prices are quarterly; they are read from the quarterly panel and never spread to months.
    hq = pd.read_csv(PROCESSED / "derived_quarterly.csv", parse_dates=["date"], index_col="date")
    t_h, v = tf.last_valid(hq["hpi_dallas_yoy_pct"])
    Q = tf.quarter_label(t_h)
    N.add("hpi_dallas_yoy_latest", v, "%", Q,
          "FHFA house price index, Dallas-Plano-Irving division: change vs the same quarter a year earlier")
    N.add("hpi_fortworth_yoy_latest", hq.at[t_h, "hpi_fortworth_yoy_pct"], "%", Q,
          "FHFA house price index, Fort Worth-Arlington-Grapevine division: change vs a year earlier")
    for key, col, name in [("hpi_dallas_yoy_peak", "hpi_dallas_yoy_pct", "Dallas-Plano-Irving"),
                           ("hpi_fortworth_yoy_peak", "hpi_fortworth_yoy_pct", "Fort Worth-Arlington-Grapevine")]:
        s_ = hq[col].dropna()
        N.add(key, s_.max(), "%", tf.quarter_label(s_.idxmax()), f"Largest 4-quarter gain since 2017, {name}")
    in_q = d.loc[t_h:t_h + pd.DateOffset(months=2), "cpi_dfw_yoy_pct"].dropna()
    if len(in_q):
        N.add("cpi_dfw_yoy_in_hpi_quarter", in_q.mean(), "%", ", ".join(mlabel(i) for i in in_q.index),
              f"DFW CPI-U inflation in the CPI month(s) of {Q} (DFW CPI is published every other month)")
    hq.round(3).to_csv(RESULTS / "house_prices_quarterly.csv")
    t_m, m = tf.last_valid(d["mortgage30_avg"])
    N.add("mortgage_latest", m, "%", mlabel(t_m), "30-year fixed rate, monthly average", 2)
    N.add("mortgage_yoy_pp", d.at[t_m, "mortgage30_yoy_pp"], "pts", mlabel(t_m), "Change vs a year earlier", 2)
    mm = d["mortgage30_avg"].dropna()
    higher = mm[(mm >= m) & (mm.index < t_m)]
    N.add("mortgage_highest_since_year", higher.index.max().year if len(higher) else float("nan"), "year",
          mlabel(higher.index.max()) if len(higher) else "", "Last earlier month with an average at least as high", 0)
    low26 = mm.loc["2026-01-01":]
    N.add("mortgage_2026_low", low26.min(), "%", mlabel(low26.idxmin()), "Lowest monthly average in 2026", 2)
    wk = obs[(obs["key"] == "mortgage30") & obs["value"].notna()].sort_values("date").iloc[-1]
    N.add("mortgage_latest_weekly", wk["value"], "%", tf.day_label(wk["date"]), "Latest weekly survey", 2)
    t_p, p = tf.last_valid(d["permits_dfw_12m"])
    N.add("permits_12m_latest", p, "units", mlabel(t_p), "Housing units authorized, 12-month total", 0)
    N.add("permits_12m_yoy", d.at[t_p, "permits_dfw_12m_yoy_pct"], "%", mlabel(t_p),
          "12-month total vs the 12 months before")
    pk = d["permits_dfw_12m"].dropna()
    N.add("permits_12m_vs_peak", (p / pk.max() - 1) * 100, "%", f"peak {mlabel(pk.idxmax())}",
          "12-month total vs its peak (2016 on)")
    d[["mortgage30_avg", "mortgage30_yoy_pp", "permits_dfw", "permits_dfw_12m",
       "permits_dfw_12m_yoy_pct"]].loc["2016-01-01":].round(3).to_csv(RESULTS / "housing_monthly.csv")

    # -------------------------------------------------------- supersectors --
    last = ss["date"].max()
    cur = ss[ss["date"] == last].dropna(subset=["yoy_k"]).sort_values("yoy_k", ascending=False)
    total_nsa = obs[(obs["key"] == "jobs_dfw_nsa")].set_index("date")["value"]
    total_change = total_nsa[last] - total_nsa[last - pd.DateOffset(months=12)]
    cur = cur.assign(share_of_net_change_pct=cur["yoy_k"] / total_change * 100)
    cur.assign(date=cur["date"].dt.strftime("%Y-%m-%d")).round(3).to_csv(
        RESULTS / "supersector_change_latest.csv", index=False)
    S = mlabel(last)
    N.add("ss_n_adding", (cur["yoy_k"] > 0).sum(), "of 10", S, "Supersectors with more jobs than a year earlier", 0)
    top = cur.iloc[0]
    N.add("ss_top_yoy_k", top["yoy_k"], "thousand jobs", S, f"Largest gain: {top['supersector']}")
    N.add("ss_top_yoy_pct", top["yoy_pct"], "%", S, f"Largest gain, % change: {top['supersector']}")
    N.add("ss_top_share_of_net", top["share_of_net_change_pct"], "%", S,
          f"{top['supersector']} share of the net 12-month change in total jobs (NSA)", 0)
    N.add("ss_total_nsa_yoy_k", total_change, "thousand jobs", S, "Net 12-month change, total jobs (NSA)")
    bottom = cur.iloc[-1]
    N.add("ss_bottom_yoy_k", bottom["yoy_k"], "thousand jobs", S, f"Largest decline: {bottom['supersector']}")
    second = cur.iloc[-2]
    N.add("ss_second_bottom_yoy_k", second["yoy_k"], "thousand jobs", S, f"Second-largest decline: {second['supersector']}")
    br = d["breadth_share_pos"].dropna()
    N.add("breadth_latest_of_10", br.iloc[-1] / 10, "of 10", mlabel(br.index[-1]),
          "Supersectors adding jobs over 12 months", 0)

    # ---------------------------------------------------- KPIs and quality --
    pd.DataFrame([{k: v for k, v in kpi.items() if k not in ("value",)} for kpi in dash["kpis"]]) \
        .to_csv(RESULTS / "kpi_cards.csv", index=False)
    dq = []
    for _, r in obs[obs["value"].isna()].iterrows():
        dq.append({"check": "missing value published by source", "series_id": r["series_id"],
                   "date": r["date"].strftime("%Y-%m-%d"), "detail": r["footnote"]})
    for k, dates in prelim.items():
        for t in sorted(dates):
            dq.append({"check": "preliminary value", "series_id": meta.loc[k, "series_id"],
                       "date": t.strftime("%Y-%m-%d"), "detail": "BLS footnote code P"})
    diff = d["ss_sum_minus_total_k"].dropna()
    dq.append({"check": "10 supersectors sum to total nonfarm (NSA)", "series_id": "SMU48191000000000001",
               "date": f"{diff.index.min():%Y-%m-%d} to {diff.index.max():%Y-%m-%d}",
               "detail": f"max absolute difference {diff.abs().max():.2f} thousand over {len(diff)} months"})
    pd.DataFrame(dq).to_csv(RESULTS / "data_quality.csv", index=False)
    N.add("ss_sum_check_max_abs_k", diff.abs().max(), "thousand jobs", "", "Max |sum of supersectors - total|", 2)
    N.add("n_missing_values", obs["value"].isna().sum(), "values", "Oct 2025",
          "Values published as unavailable", 0)

    nums = N.frame()
    nums.to_csv(RESULTS / "key_numbers.csv", index=False)
    charts(d, hq, N, prelim)
    print(f"Wrote {len(nums)} numbers to results/key_numbers.csv and {len(list(RESULTS.glob('*.csv')))} "
          f"CSV files in results/; charts in images/.")
    for r in nums.itertuples():
        print(f"  {r.key:<34} {r.display:>10} {r.unit:<20} {r.period}")
    return 0


def _mean_factors(s: pd.Series) -> pd.Series:
    """Variant for the sensitivity check: mean instead of median seasonal factors."""
    m12 = s.rolling(12).mean()
    dev = (s - 0.5 * (m12.shift(-5) + m12.shift(-6)))
    keep = ~((dev.index >= tf.PANDEMIC[0]) & (dev.index <= tf.PANDEMIC[1]))
    dev = dev[keep].dropna()
    f = dev.groupby(dev.index.month).mean()
    return f - f.mean()


# ------------------------------------------------------------------- charts --
MINUS = "−"


def sgn(x: float, nd: int = 1) -> str:
    """Signed number with a true minus sign and no negative zero (e.g. +0.7, 0.0, −0.8)."""
    r = round(float(x), nd) + 0.0
    if r == 0:
        return f"{0:.{nd}f}"
    return f"{'+' if r > 0 else MINUS}{abs(r):.{nd}f}"


def finish(fig, ax_list, note, path, year_step=1):
    import textwrap
    for ax in ax_list:
        ax.xaxis.set_major_locator(mdates.YearLocator(year_step))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        ax.tick_params(length=3)
        ax.set_axisbelow(True)
    fig.text(0.01, 0.012, textwrap.fill(note, 118), fontsize=8, color="#5b6676", ha="left", va="bottom",
             linespacing=1.3)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def share_phrase(r: float) -> str:
    """Words for a ratio, so chart titles stay true when the data change."""
    for limit, words in [(0.2, "less than a fifth of"), (0.3, "about a quarter of"), (0.4, "about a third of"),
                         (0.6, "about half of"), (0.85, "most of"), (1.15, "about the same as")]:
        if r < limit:
            return words
    return "faster than"


def end_label(ax, s, color, fmt="{:.1f}%"):
    s = s.dropna()
    ax.annotate(fmt.format(s.iloc[-1]), (s.index[-1], s.iloc[-1]), xytext=(4, 0), textcoords="offset points",
                color=color, fontsize=9.5, fontweight="bold", va="center")


def charts(d: pd.DataFrame, dq: pd.DataFrame, N: Numbers, prelim: dict) -> None:
    k = {r["key"]: r for r in N.rows}
    start = "2022-01-01"

    # 1. Job growth ---------------------------------------------------------
    fig, ax = plt.subplots(figsize=(8, 4.67))
    x = d.loc[start:]
    last = x["jobs_dfw_yoy_pct"].dropna().index[-1]
    ax.plot(x.index, x["jobs_dfw_yoy_pct"], color=NAVY, lw=2.6, label="DFW")
    ax.plot(x.index, x["jobs_tx_yoy_pct"], color=TEAL, lw=1.6, label="Texas")
    ax.plot(x.index, x["jobs_us_yoy_pct"], color=ORANGE, lw=1.6, label="U.S.")
    for key, color, name in [("jobs_dfw_yoy_avg_2017_2019", NAVY, "DFW 2017-19 average"),
                             ("jobs_us_yoy_avg_2017_2019", ORANGE, "U.S. 2017-19 average")]:
        ax.axhline(k[key]["value"], color=color, lw=1, ls="--", alpha=0.8)
        ax.text(pd.Timestamp("2022-02-01"), k[key]["value"] + 0.1, f"{name}: {k[key]['value']:.1f}%",
                color=color, fontsize=8.5)
    ax.axhline(0, color="#9aa5b4", lw=0.8)
    end_label(ax, x["jobs_dfw_yoy_pct"], NAVY)
    end_label(ax, x["jobs_us_yoy_pct"], ORANGE)
    ax.set_ylabel("12-month % change in payroll jobs (SA)")
    ax.legend(loc="upper right", ncols=3)
    a, b = k["jobs_dfw_yoy_avg_last12"]["value"], k["jobs_dfw_yoy_avg_2017_2019"]["value"]
    u = k["jobs_us_yoy_avg_last12"]["value"]
    ax.set_title(f"DFW job growth averaged {a:.1f}% over the past year, {share_phrase(a / b)}\n"
                 f"its 2017-19 pace ({b:.1f}%), {'but it still outgrew' if a > u else 'and it trailed'} "
                 f"the U.S. ({u:.2f}%)")
    ax.set_xlim(pd.Timestamp(start), last + pd.DateOffset(days=75))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.86, bottom=0.14)
    finish(fig, [ax], f"Source: BLS Current Employment Statistics via the BLS Public Data API. Payroll jobs, "
           f"seasonally adjusted. The latest month ({k['jobs_dfw_yoy_latest']['period']}) is preliminary.",
           IMAGES / "01_job_growth_dfw_vs_us.png")

    # 2. Unemployment ---------------------------------------------------------
    fig, ax = plt.subplots(figsize=(8, 4.67))
    x = d.loc[start:]
    ax.plot(x.index, x["ur_dfw_nsa"], color=GRAY, lw=1, ls=":", label="DFW, as published (NSA)")
    ax.plot(x.index, x["ur_dfw_sa_est"], color=NAVY, lw=2.6, label="DFW, seasonally adjusted (this project)")
    ax.plot(x.index, x["ur_tx_sa"], color=TEAL, lw=1.6, label="Texas (SA)")
    ax.plot(x.index, x["ur_us_sa"], color=ORANGE, lw=1.6, label="U.S. (SA)")
    ax.axvspan(pd.Timestamp("2025-09-16"), pd.Timestamp("2025-10-16"), color="#e6eaf0", lw=0)
    ax.set_ylim(x[["ur_dfw_nsa", "ur_dfw_sa_est", "ur_tx_sa", "ur_us_sa"]].min().min() - 0.25,
                x[["ur_dfw_nsa", "ur_dfw_sa_est", "ur_tx_sa", "ur_us_sa"]].max().max() + 0.35)
    ax.text(pd.Timestamp("2025-10-01"), ax.get_ylim()[0] + 0.04, "Oct 2025\nnot published", fontsize=7.5,
            color="#5b6676", ha="center", va="bottom")
    ax.set_ylabel("Unemployment rate (%)")
    ax.legend(loc="upper left", ncols=2, fontsize=9)
    dfw_sa, tx, us = (k["ur_dfw_sa_est_latest"]["value"], k["ur_tx_sa_same_month"]["value"],
                      k["ur_us_sa_same_month"]["value"])
    chg = k["ur_dfw_nsa_12m_change"]["value"]
    words = {"below": "below", "above": "above", "about the same as": "level with"}
    chg_text = ("unchanged from" if round(chg, 1) == 0 else
                f"{abs(chg):.1f} point {'lower' if chg < 0 else 'higher'} than")
    ax.set_title(f"DFW's unemployment rate ({dfw_sa:.1f}%, {k['ur_dfw_nsa_latest']['period']}) is "
                 f"{words[tf.compare(dfw_sa, tx)]} Texas ({tx:.1f}%)\nand {words[tf.compare(dfw_sa, us)]} "
                 f"the U.S. ({us:.1f}%), {chg_text} a year earlier")
    ax.set_xlim(pd.Timestamp(start), x["ur_us_sa"].dropna().index[-1] + pd.DateOffset(days=45))
    fig.subplots_adjust(left=0.08, right=0.98, top=0.86, bottom=0.15)
    finish(fig, [ax], "Source: BLS LAUS and CPS via the BLS Public Data API. BLS publishes the DFW rate only "
           "not seasonally adjusted (dotted); the solid DFW line and the title use this project's seasonal "
           "adjustment. Latest DFW month preliminary.", IMAGES / "02_unemployment_dfw_tx_us.png")

    # 3. Wages vs inflation ---------------------------------------------------
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(8, 4.67), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    x = d.loc["2021-01-01":]
    ax.plot(x.index, x["ahe_dfw_yoy_pct"], color=NAVY, lw=0.9, alpha=0.35)
    ax.plot(x.index, x["ahe_dfw_yoy_pct_3mma"], color=NAVY, lw=2.4, label="DFW hourly earnings (3-month avg.)")
    c = x["cpi_dfw_yoy_pct"].dropna()
    ax.plot(c.index, c, color=TEAL, lw=1.6, marker="o", ms=3, label="DFW CPI-U (every other month)")
    ax.plot(x.index, x["cpi_us_yoy_pct"], color=ORANGE, lw=1.4, ls="--", label="U.S. CPI-U")
    ax.set_ylabel("12-month % change")
    ax.legend(loc="upper right", fontsize=8.5)
    ax2.bar(x.index, x["real_ahe_vs_us_cpi_pp"], width=25, color=GRAY, alpha=0.6, label="vs U.S. CPI")
    r = x["real_ahe_vs_dfw_cpi_pp"].dropna()
    ax2.plot(r.index, r, color=NAVY, lw=1.4, marker="o", ms=3, label="vs DFW CPI")
    ax2.axhline(0, color="#5b6676", lw=0.8)
    ax2.set_ylabel("Real wage\ngrowth (pts)")
    ax2.legend(loc="lower right", ncols=2, fontsize=8.5)
    ax2.set_xlim(pd.Timestamp("2020-12-01"), x["ahe_dfw_yoy_pct"].dropna().index[-1] + pd.DateOffset(days=45))
    P = k["real_wage_dfw_cpi_latest"]["period"]
    ax.set_title(f"In {P}, DFW hourly pay grew {k['ahe_dfw_yoy_cpi_month']['value']:.1f}% vs "
                 f"{k['cpi_dfw_yoy_latest']['value']:.1f}% inflation (DFW CPI)\nand "
                 f"{k['cpi_us_yoy_cpi_month']['value']:.1f}% (U.S. CPI): real wage growth of "
                 f"{sgn(k['real_wage_dfw_cpi_latest']['value'])} and {sgn(k['real_wage_us_cpi_latest']['value'])} points")
    fig.subplots_adjust(left=0.1, right=0.98, top=0.86, bottom=0.14, hspace=0.16)
    finish(fig, [ax, ax2], "Source: BLS CES and CPI via the BLS Public Data API. Average hourly earnings of all "
           "private-sector employees, not seasonally adjusted (thin line: monthly). Real wage growth = wage growth "
           "minus CPI inflation.", IMAGES / "03_wages_vs_inflation.png")

    # 4. Housing ----------------------------------------------------------------
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(8, 4.67), gridspec_kw={"width_ratios": [1.15, 1]})
    q = dq.loc["2019-01-01":]
    for col, color, name in [("hpi_dallas_yoy_pct", NAVY, "Dallas division"),
                             ("hpi_fortworth_yoy_pct", TEAL, "Fort Worth division")]:
        s_ = q[col].dropna()
        ax.plot(s_.index, s_, color=color, lw=2, marker="o", ms=3, label=name)
    ax.axhline(0, color="#5b6676", lw=0.8)
    ax.set_ylabel("Home prices, % change vs a year earlier")
    ax.set_title("House prices, FHFA (quarterly)", fontsize=10.5, loc="left", fontweight="normal")
    ax.legend(loc="upper right", fontsize=8.5)
    x = d.loc["2019-01-01":]
    mo = x["mortgage30_avg"].dropna()
    ax2.plot(mo.index, mo, color=ORANGE, lw=2.2)
    end_label(ax2, mo, ORANGE, "{:.2f}%")
    ax2.set_ylabel("30-year fixed rate, monthly average (%)")
    ax2.set_title("30-year mortgage rate (U.S.)", fontsize=10.5, loc="left", fontweight="normal")
    ax2.set_xlim(mo.index[0] - pd.DateOffset(days=30), mo.index[-1] + pd.DateOffset(months=11))
    dv, fv = k["hpi_dallas_yoy_latest"]["value"], k["hpi_fortworth_yoy_latest"]["value"]
    Q = k["hpi_dallas_yoy_latest"]["period"]
    move = "rose" if k["mortgage_yoy_pp"]["value"] > 0 else "fell"
    if abs(dv) < 1.5 and abs(fv) < 1.5:
        line1 = f"Dallas-area home prices were flat over the year to {Q} ({sgn(dv)}% Dallas,"
    else:
        line1 = f"Dallas-area home prices changed over the year to {Q} ({sgn(dv)}% Dallas,"
    fig.suptitle(f"{line1}\n{sgn(fv)}% Fort Worth) while the 30-year mortgage rate {move} to "
                 f"{k['mortgage_latest']['value']:.2f}% ({k['mortgage_latest']['period']})",
                 x=0.01, ha="left", fontsize=12.5, fontweight="bold")
    fig.subplots_adjust(left=0.08, right=0.97, top=0.80, bottom=0.14, wspace=0.3)
    finish(fig, [ax, ax2], "Source: U.S. Federal Housing Finance Agency, All-Transactions House Price Index (quarterly, "
           "not seasonally adjusted, public domain) and Freddie Mac PMMS, both via FRED (St. Louis Fed).",
           IMAGES / "04_housing_prices_and_rates.png", year_step=2)


if __name__ == "__main__":
    raise SystemExit(main())
