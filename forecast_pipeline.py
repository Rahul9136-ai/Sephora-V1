"""
Sephora Contact-Volume Forecasting Pipeline
===========================================
End-to-end, re-runnable pipeline that:
  1. Ingests the raw Gladly export (Ss.xlsx) -> daily contact volume
     (total + per channel: VOICE / EMAIL / CHAT).
  2. Fills missing calendar days (e.g. Christmas gap).
  3. Normalizes outliers robustly, with EXTRA damping for the April
     promo surge (Sephora spring sale) so event spikes don't distort
     the learned baseline.
  4. Holds out the last N days as a TEST set, trains a weekly-seasonal
     SARIMAX model, and reports accuracy vs a seasonal-naive baseline.
  5. Refits on ALL cleaned data and FORECASTS forward H days beyond the
     last observed date, with confidence intervals.
  6. Writes every artifact to ./outputs so a scheduler can rerun this
     unattended each time new data lands ("auto-retrain").

Run:      python forecast_pipeline.py
Configure at the top of the file or via CLI flags (see `parse_args`).

Only depends on: pandas, numpy, statsmodels, matplotlib (optional plot).
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XLSX = r"C:\Users\lenovo\Desktop\Ss.xlsx"
OUT_DIR = os.path.join(HERE, "outputs")
DATA_DIR = os.path.join(HERE, "data")           # user-added actuals/orders/events
ACTUALS_CSV = os.path.join(DATA_DIR, "actuals.csv")   # date,segment,contacts
ORDERS_CSV = os.path.join(DATA_DIR, "orders.csv")     # date,orders
EVENTS_CSV = os.path.join(DATA_DIR, "events.csv")     # start,end,name,impact_pct
SOURCE_XLSX = os.path.join(DATA_DIR, "source.xlsx")   # uploaded raw Gladly export


def resolve_source(requested: str) -> str:
    """Prefer an uploaded raw export (data/source.xlsx) over the default path,
    unless a specific --xlsx was passed."""
    if requested == DEFAULT_XLSX and os.path.exists(SOURCE_XLSX):
        return SOURCE_XLSX
    return requested

# ---- forecasting config -------------------------------------------------
TEST_DAYS = 28          # holdout length for accuracy evaluation
HORIZON = 90            # days to forecast (3-month rolling forecast)
SEASON = 7              # weekly seasonality

# Forecast granularity = the 10 Country-Channel-Language segments ("LOBs"),
# e.g. US-PH-EN (US / Phone / English). Channel is abbreviated PH=Voice,
# CH=Chat, EM=Email. The country=ALL bucket is dropped. `total` (sum of the
# 10 segments) is also produced for reference. Columns are ordered by volume
# at ingest time; see SPARSE_MIN for how thin segments are handled.
CHANNEL_ABBR = {"VOICE": "PH", "CHAT": "CH", "EMAIL": "EM"}
SPARSE_MIN = 3          # if a segment averages < this many/day, use a simple
                        # robust baseline instead of STL (near-empty queues)

# ---- outlier / April config --------------------------------------------
Z_THRESH = 4.0          # robust z-score above which a point is an outlier
REL_THRESH = 0.40       # AND must deviate this fraction from expectation
APRIL_MONTHS = [4]      # months to treat as promo windows (April spring sale)
APRIL_CAP = 1.35        # in promo months, cap value at baseline * this
                        # (dampens the sustained surge, not just the peak)
# Known event windows whose surge should NOT inflate the baseline used for
# normalization (Sephora spring sale + Nov/Dec holiday peak + Christmas gap).
EVENT_WINDOWS = [("2026-04-01", "2026-04-30"),
                 ("2025-11-20", "2025-12-31")]


# =========================================================================
# 1. INGEST
# =========================================================================
def _segment_code(channel: str, country: str):
    """Map a raw (Channel, Country) pair to a Country-Channel-Language code.

    Country field already encodes language: voice uses 'US-EN','US-SP',
    'CA-EN','CA-FR'; email/chat use 'US','CA-EN','CA-FR' (US implies EN).
    The aggregate 'ALL' bucket is dropped (returns None).
    """
    if country is None:
        return None
    country = str(country).strip()
    if country.upper() == "ALL":
        return None
    ch = CHANNEL_ABBR.get(str(channel).strip().upper())
    if ch is None:
        return None
    if "-" in country:
        cc, lang = country.split("-", 1)
    else:
        cc, lang = country, "EN"
    return f"{cc.upper()}-{ch}-{lang.upper()}"


def load_daily(xlsx_path: str) -> pd.DataFrame:
    """Read the raw Voice + Email/Chat exports and aggregate to a daily series
    with one column per Country-Channel-Language segment (10 of them, e.g.
    US-PH-EN), plus a `total` = sum of those segments. country=ALL is removed.
    Columns are ordered `total` then segments by descending volume.
    """
    xl = pd.ExcelFile(xlsx_path)
    frames = []
    for sheet in ["Voice Export (Gladly)", "Email_Chat Export (Gladly)"]:
        df = pd.read_excel(xl, sheet, usecols=["Call_Date", "Channel", "Country", "Contacts"])
        frames.append(df)
    raw = pd.concat(frames, ignore_index=True)
    raw["Call_Date"] = pd.to_datetime(raw["Call_Date"])
    raw["Contacts"] = pd.to_numeric(raw["Contacts"], errors="coerce").fillna(0)
    raw["segment"] = [_segment_code(c, co)
                      for c, co in zip(raw["Channel"], raw["Country"])]
    raw = raw.dropna(subset=["segment"])            # drops ALL / unknown

    wide = (raw.groupby(["Call_Date", "segment"])["Contacts"].sum()
               .unstack(fill_value=0))
    wide = _merge_new_actuals(wide)                  # append user-added actuals
    order = wide.drop(columns=["total"], errors="ignore").sum() \
                .sort_values(ascending=False).index.tolist()
    wide = wide[order]
    wide.insert(0, "total", wide.sum(axis=1))
    wide.index.name = "date"
    return wide.asfreq("D")                          # exposes missing days


def _merge_new_actuals(wide: pd.DataFrame) -> pd.DataFrame:
    """Overlay user-added daily actuals (data/actuals.csv: date,segment,contacts)
    onto the Excel history, extending the date range as needed. `segment='total'`
    rows are ignored (total is recomputed from the segments)."""
    if not os.path.exists(ACTUALS_CSV):
        return wide
    add = pd.read_csv(ACTUALS_CSV)
    if add.empty:
        return wide
    add["date"] = pd.to_datetime(add["date"])
    add = add[add["segment"].astype(str).str.lower() != "total"]
    for _, r in add.iterrows():
        seg = str(r["segment"]).strip().upper()
        if seg not in wide.columns:
            wide[seg] = 0.0
        wide.loc[r["date"], seg] = float(r["contacts"])
    return wide.fillna(0.0).sort_index()


def load_orders(index: pd.DatetimeIndex):
    """Daily order counts (data/orders.csv: date,orders) reindexed to `index`.
    Returns a float Series (missing days interpolated) or None if no data."""
    if not os.path.exists(ORDERS_CSV):
        return None
    o = pd.read_csv(ORDERS_CSV)
    if o.empty:
        return None
    o["date"] = pd.to_datetime(o["date"])
    s = o.groupby("date")["orders"].sum().astype(float)
    full = pd.date_range(min(s.index.min(), index.min()),
                         max(s.index.max(), index.max()), freq="D")
    return s.reindex(full).interpolate().ffill().bfill()


def _extend_orders(orders: pd.Series, future_idx: pd.DatetimeIndex) -> pd.Series:
    """Return orders covering history + the forecast horizon. Future days the
    user already supplied are kept; the rest are projected from a weekly
    seasonal average so the exog driver is defined across the whole forecast."""
    if orders is None:
        return None
    hist = orders[orders.index < future_idx[0]]
    proj = {}
    for d in future_idx:
        if d in orders.index and not pd.isna(orders.loc[d]):
            proj[d] = orders.loc[d]
        else:
            same = hist[hist.index.dayofweek == d.dayofweek]
            proj[d] = float(same.iloc[-4:].mean()) if len(same) else float(hist.iloc[-SEASON:].mean() if len(hist) else 0)
    return pd.concat([hist, pd.Series(proj)]).sort_index()


def load_events() -> list:
    """Event calendar (data/events.csv: start,end,name,impact_pct). impact_pct is
    the expected % volume uplift during the window; used both as a note and to
    lift the future forecast for known promos."""
    if not os.path.exists(EVENTS_CSV):
        return []
    e = pd.read_csv(EVENTS_CSV)
    out = []
    for _, r in e.iterrows():
        try:
            out.append({"start": pd.to_datetime(r["start"]),
                        "end": pd.to_datetime(r["end"]),
                        "name": str(r["name"]),
                        "impact_pct": float(r.get("impact_pct", 0) or 0)})
        except Exception:
            continue
    return out


def event_factor(index: pd.DatetimeIndex, events: list) -> np.ndarray:
    """Multiplicative uplift per date from the event calendar (1.0 = no event)."""
    f = np.ones(len(index))
    for ev in events:
        m = np.asarray((index >= ev["start"]) & (index <= ev["end"]))
        f[m] *= (1 + ev["impact_pct"] / 100.0)
    return f


def segment_columns(daily: pd.DataFrame) -> list:
    """Ordered list of series to forecast: total + the 10 segments."""
    return list(daily.columns)


def trim_launch(s: pd.Series) -> pd.Series:
    """Drop a segment's dead pre-launch period (leading all-zero stretch).

    Several queues (e.g. US-EM-EN, CA-CH-FR) are 0 for months and then the
    channel goes live. Feeding that 0 -> N jump into a log-STL produces an
    exploding baseline, so we start the series at its activation date: the
    first day of the first 14-day window that is mostly active.
    """
    active = (s.fillna(0) > 0)
    roll = active.rolling(14).sum()
    if roll.max() is np.nan or roll.max() < 7:
        return s                                   # always thin -> keep all
    start = roll[roll >= 7].index.min() - pd.Timedelta(days=13)
    start = max(start, s.index.min())
    return s.loc[start:]


def fill_missing_days(s: pd.Series) -> pd.Series:
    """Fill gaps (e.g. Christmas) using the same weekday's local median so
    the imputation respects weekly seasonality rather than a flat line."""
    s = s.copy()
    missing = s.index[s.isna()]
    for d in missing:
        same_dow = s[s.index.dayofweek == d.dayofweek].dropna()
        window = same_dow[(same_dow.index >= d - pd.Timedelta(days=21)) &
                          (same_dow.index <= d + pd.Timedelta(days=21))]
        s.loc[d] = window.median() if len(window) else same_dow.median()
    return s.interpolate().bfill().ffill()


# =========================================================================
# 2. OUTLIER NORMALIZATION (April-aware)
# =========================================================================
def _event_mask(index: pd.DatetimeIndex) -> pd.Series:
    """Boolean mask of known promo/holiday windows."""
    m = pd.Series(False, index=index)
    for start, end in EVENT_WINDOWS:
        m |= (index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))
    return m


def _stl_expectation(log: pd.Series) -> pd.Series:
    """trend + seasonal from a robust weekly STL (in log space)."""
    from statsmodels.tsa.seasonal import STL
    stl = STL(log, period=SEASON, robust=True).fit()
    return stl.trend + stl.seasonal


def normalize_outliers(s: pd.Series):
    """Robustly normalize spikes/dips with an EVENT-AWARE baseline.

    Why event-aware: a 2-week sustained April surge would otherwise pull the
    STL trend upward, so the "expectation" it produces sits inside the surge
    and nothing gets capped. Instead we blank the known event windows, rebuild
    the baseline across them, and measure/cap against that clean baseline.

    Steps:
      1. Blank known event windows (April sale, Nov/Dec) -> interpolate ->
         robust STL -> `baseline` expectation NOT inflated by the surge.
      2. Statistical outliers = points whose robust z on (log - baseline)
         exceeds Z_THRESH AND that deviate >REL_THRESH from baseline.
         Replace them with the baseline expectation.
      3. April promo days: WINSORIZE anything above baseline * APRIL_CAP down
         to that cap -> smooths the entire sustained surge, not just the peak.

    Returns (clean_series, info_dataframe).
    """
    log = np.log(s.clip(lower=1))
    events = _event_mask(s.index)

    masked = log.copy()
    masked[events] = np.nan
    masked = masked.interpolate("time").bfill().ffill()
    # Thin queues (a few contacts/day) break STL; use a robust rolling median.
    if s.mean() < SPARSE_MIN or (s > 0).sum() < 2 * SEASON:
        baseline_log = masked.rolling(SEASON, center=True, min_periods=1).median()
    else:
        baseline_log = _stl_expectation(masked)
    baseline = np.exp(baseline_log)
    # Safety net: keep the baseline within locally observed magnitudes so a
    # structural break can never inflate it beyond real data (the 31k bug).
    loc_hi = s.rolling(31, center=True, min_periods=3).max() * 1.5
    baseline = baseline.clip(lower=0, upper=loc_hi).fillna(baseline)

    resid = log - baseline_log
    mad = 1.4826 * (resid - resid.median()).abs().median() or 1.0
    z = (resid - resid.median()) / mad
    rel = (s - baseline) / baseline.clip(lower=1)

    clean = s.copy().astype(float)

    # (2) statistical outliers outside event windows -> replace with baseline
    stat_out = (z.abs() > Z_THRESH) & (rel.abs() > REL_THRESH) & (~events)
    clean[stat_out] = baseline[stat_out]

    # (3) April sale window -> cap the sustained surge toward baseline
    april_mask = pd.Series(s.index.month.isin(APRIL_MONTHS), index=s.index)
    cap = baseline * APRIL_CAP
    april_capped = april_mask & (clean > cap)
    clean[april_capped] = cap[april_capped]

    # other event windows (Nov/Dec): only clip extreme spikes, keep the season
    other_event = events & (~april_mask)
    ecap = baseline * 2.2
    event_capped = other_event & (clean > ecap)
    clean[event_capped] = ecap[event_capped]

    info = pd.DataFrame({
        "raw": s.round(0),
        "baseline": baseline.round(0),
        "robust_z": z.round(2),
        "stat_outlier": stat_out,
        "april_capped": april_capped,
        "event_capped": event_capped,
        "clean": clean.round(0),
    })
    return clean, info


# =========================================================================
# 3/4. MODEL + EVALUATE
# =========================================================================
def metrics(actual, pred) -> dict:
    actual, pred = np.asarray(actual, float), np.asarray(pred, float)
    err = actual - pred
    denom = np.abs(actual).sum()
    # WAPE (volume-weighted abs % error) is robust for low-volume / intermittent
    # queues where classic MAPE explodes on near-zero days.
    wape = float(np.abs(err).sum() / denom * 100) if denom else float("inf")
    mask = np.abs(actual) >= 1
    mape = (float(np.mean(np.abs(err[mask]) / np.abs(actual[mask])) * 100)
            if mask.any() else None)
    return {"MAE": round(float(np.mean(np.abs(err))), 1),
            "RMSE": round(float(np.sqrt(np.mean(err ** 2))), 1),
            "WAPE_%": round(wape, 2),
            "MAPE_%": round(mape, 2) if mape is not None else None}


# ---- candidate models --------------------------------------------------
# Every model takes (train, steps, xtr=None, xfu=None); xtr/xfu are optional
# exogenous DataFrames (e.g. daily orders) aligned to the train / future dates.
# Simple models ignore exog; SARIMA and the regressions use it when present.
def _predict_seasonal_naive(train, steps, xtr=None, xfu=None):
    last = train.iloc[-SEASON:].values
    return np.array([last[i % SEASON] for i in range(steps)])


def _predict_snaive_drift(train, steps, xtr=None, xfu=None):
    """Seasonal naive + weekly drift (captures slow level trend)."""
    last = train.iloc[-SEASON:].values
    if len(train) >= 2 * SEASON:
        prev = train.iloc[-2 * SEASON:-SEASON].values
        drift = np.mean(last - prev) / SEASON
    else:
        drift = 0.0
    return np.array([last[i % SEASON] + drift * (i + 1) for i in range(steps)])


def _predict_sarimax(train, steps, xtr=None, xfu=None):
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    fit = SARIMAX(train, exog=xtr, order=(0, 1, 1),
                  seasonal_order=(0, 1, 1, SEASON),
                  enforce_stationarity=False,
                  enforce_invertibility=False).fit(disp=False)
    return np.asarray(fit.forecast(steps=steps, exog=xfu))


def _predict_holtwinters(train, steps, xtr=None, xfu=None):
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    fit = ExponentialSmoothing(train, trend="add", damped_trend=True,
                               seasonal="add", seasonal_periods=SEASON).fit()
    return np.asarray(fit.forecast(steps))


def _predict_moving_avg(train, steps, xtr=None, xfu=None):
    """Weekday moving average: each future day = mean of the last 4 same-weekday
    observations (a smoothed seasonal naive)."""
    out = []
    for i in range(steps):
        dow = (train.index[-1] + pd.Timedelta(days=i + 1)).dayofweek
        same = train[train.index.dayofweek == dow]
        out.append(same.iloc[-4:].mean() if len(same) else train.iloc[-SEASON:].mean())
    return np.asarray(out, float)


def _predict_theta(train, steps, xtr=None, xfu=None):
    from statsmodels.tsa.forecasting.theta import ThetaModel
    fit = ThetaModel(np.asarray(train, float), period=SEASON).fit()
    return np.asarray(fit.forecast(steps))


def _calendar_features(index, t0, exog=None):
    """Day-of-week one-hot + linear time trend (+ optional exog columns)."""
    dow = pd.get_dummies(index.dayofweek).reindex(columns=range(7), fill_value=0)
    trend = ((index - t0).days).to_numpy().reshape(-1, 1)
    feats = [dow.to_numpy(float), trend / 30.0]
    if exog is not None:
        feats.append(np.asarray(exog, float).reshape(len(index), -1))
    return np.hstack(feats)


def _predict_regression(estimator):
    def _fn(train, steps, xtr=None, xfu=None):
        t0 = train.index[0]
        X = _calendar_features(train.index, t0, xtr)
        estimator.fit(X, train.values)
        fut = pd.date_range(train.index[-1] + pd.Timedelta(days=1),
                            periods=steps, freq="D")
        return np.asarray(estimator.predict(_calendar_features(fut, t0, xfu)))
    return _fn


def _predict_linreg(train, steps, xtr=None, xfu=None):
    from sklearn.linear_model import LinearRegression
    return _predict_regression(LinearRegression())(train, steps, xtr, xfu)


def _predict_gbr(train, steps, xtr=None, xfu=None):
    from sklearn.ensemble import GradientBoostingRegressor
    est = GradientBoostingRegressor(n_estimators=200, max_depth=3,
                                    learning_rate=0.05, random_state=0)
    return _predict_regression(est)(train, steps, xtr, xfu)


def _predict_prophet(train, steps, xtr=None, xfu=None):
    """Facebook Prophet with weekly seasonality; orders (if provided) added as
    an extra regressor. Yearly/daily seasonality off (<1yr of daily data)."""
    import logging
    for lg in ("cmdstanpy", "prophet"):
        logging.getLogger(lg).setLevel(logging.ERROR)
    from prophet import Prophet
    df = pd.DataFrame({"ds": train.index, "y": np.asarray(train, float)})
    m = Prophet(weekly_seasonality=True, yearly_seasonality=False,
                daily_seasonality=False, interval_width=0.8)
    if xtr is not None:
        df["orders"] = np.asarray(xtr, float).reshape(-1)
        m.add_regressor("orders")
    m.fit(df)
    fut = pd.DataFrame({"ds": pd.date_range(train.index[-1] + pd.Timedelta(days=1),
                                            periods=steps, freq="D")})
    if xtr is not None:
        fut["orders"] = (np.asarray(xfu, float).reshape(-1) if xfu is not None
                         else float(np.asarray(xtr, float).reshape(-1)[-SEASON:].mean()))
    return np.asarray(m.predict(fut)["yhat"].values)


# 9 methods compared on the test set (labels shown in the UI)
MODELS = {
    "seasonal_naive": _predict_seasonal_naive,
    "snaive_drift": _predict_snaive_drift,
    "moving_avg_dow": _predict_moving_avg,
    "holt_winters": _predict_holtwinters,
    "sarimax": _predict_sarimax,
    "theta": _predict_theta,
    "linreg_calendar": _predict_linreg,
    "gbr_calendar": _predict_gbr,
    "prophet": _predict_prophet,
}

MODEL_LABELS = {
    "seasonal_naive": "Seasonal Naive",
    "snaive_drift": "Seasonal Naive + Drift",
    "moving_avg_dow": "Moving Avg (by weekday)",
    "holt_winters": "Holt-Winters (ETS)",
    "sarimax": "SARIMA",
    "theta": "Theta",
    "linreg_calendar": "Linear Regression",
    "gbr_calendar": "Gradient Boosting",
    "prophet": "Prophet",
}


def _exog_df(orders_full, index):
    """1-column exog DataFrame (orders) aligned to `index`, or None."""
    if orders_full is None:
        return None
    vals = orders_full.reindex(index).ffill().bfill()
    return pd.DataFrame({"orders": vals.values}, index=index)


def evaluate(clean: pd.Series, sparse: bool = False, orders_full=None):
    """Backtest ALL 8 methods on the last TEST_DAYS and return per-model metrics
    + predictions and the winner (lowest WAPE).

    All 8 are always scored so the leaderboard is fully populated; a method that
    can't fit (too few points on a launched queue) is recorded with an error.
    For the FORWARD forecast, however, thin queues are pinned to seasonal-naive
    (`safe_best`) so trend/drift models can't extrapolate absurd values.
    `orders_full` (optional) feeds SARIMA + the regressions as an exog driver."""
    # short (recently launched) series -> shrink the holdout
    test_days = TEST_DAYS
    if len(clean) < TEST_DAYS + 3 * SEASON:
        test_days = max(SEASON, len(clean) // 4)
        sparse = True
    train, test = clean.iloc[:-test_days], clean.iloc[-test_days:]
    xtr, xte = _exog_df(orders_full, train.index), _exog_df(orders_full, test.index)
    results, preds = {}, {}
    for name, fn in MODELS.items():                 # always try all 8
        try:
            pred = np.clip(fn(train, len(test), xtr, xte), 0, None)
            results[name] = metrics(test, pred)
            preds[name] = np.round(pred, 1).tolist()
        except Exception as e:
            results[name] = {"error": str(e)[:100], "WAPE_%": None}

    def _wape(k):
        w = results[k].get("WAPE_%")
        return w if isinstance(w, (int, float)) else float("inf")
    ranked_best = min(results, key=_wape)            # most accurate on the test
    # safety: pin thin queues to seasonal_naive for the forward forecast
    safe_best = "seasonal_naive" if sparse else ranked_best
    backtest = {"dates": [d.strftime("%Y-%m-%d") for d in test.index],
                "actual": np.round(test.values, 1).tolist(),
                "predictions": preds}
    return results, safe_best, ranked_best, backtest


# =========================================================================
# 5. FORECAST FUTURE (using the winning model, with an interval)
# =========================================================================
def forecast_future(clean: pd.Series, horizon: int, model_name: str,
                    orders_full=None, events=None) -> pd.DataFrame:
    """Refit the winning model on ALL cleaned data and project `horizon` days
    ahead. Orders feed the model as an exog driver; known future events lift the
    forecast within their windows. The interval comes from weekly residuals."""
    idx = pd.date_range(clean.index[-1] + pd.Timedelta(days=1),
                        periods=horizon, freq="D")
    xtr, xfu = _exog_df(orders_full, clean.index), _exog_df(orders_full, idx)
    fn = MODELS.get(model_name, _predict_seasonal_naive)
    try:
        mean = np.clip(fn(clean, horizon, xtr, xfu), 0, None)
    except Exception:
        mean = np.clip(_predict_seasonal_naive(clean, horizon), 0, None)

    # sanity cap: no forecast should exceed 1.5x the recent 8-week peak
    # (guards thin queues where drift/trend models can run away)
    recent_peak = float(np.nanmax(clean.iloc[-8 * SEASON:])) if len(clean) else None
    if recent_peak and recent_peak > 0:
        mean = np.clip(mean, 0, recent_peak * 1.5)

    # residual scale from a one-week-back in-sample fit -> +/- 1.28 sigma (80%)
    insample = _predict_seasonal_naive(clean.iloc[:-SEASON], SEASON) \
        if len(clean) > 2 * SEASON else clean.iloc[-SEASON:].values
    resid_std = float(np.std(clean.iloc[-SEASON:].values - insample)) or \
        float(clean.std() * 0.15)
    band = 1.2816 * resid_std * np.sqrt(1 + np.arange(horizon) / SEASON)

    # known future events lift the forecast within their windows
    ef = event_factor(idx, events or [])
    mean = mean * ef
    band = band * ef

    upper_cap = (recent_peak * 2.0 * ef) if recent_peak else None
    out = pd.DataFrame({
        "forecast": np.round(mean),
        "lower_80": np.clip(mean - band, 0, None).round(0),
        "upper_80": np.round(np.clip(mean + band, 0, upper_cap)),
        "event_factor": np.round(ef, 3),
    }, index=idx)
    out.index.name = "date"
    return out


# =========================================================================
# ORCHESTRATION
# =========================================================================
def run(xlsx_path: str, horizon: int, make_plot: bool = True) -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"[1/5] Ingesting {xlsx_path} ...")
    daily = load_daily(xlsx_path)
    daily.to_csv(os.path.join(OUT_DIR, "daily_contacts.csv"))
    print(f"      {len(daily)} days: {daily.index.min().date()} -> {daily.index.max().date()}")

    cols = segment_columns(daily)
    segs = [c for c in cols if c != "total"]
    print(f"      {len(segs)} segments: {', '.join(segs)}")

    # exogenous drivers: daily orders + known-event calendar
    future_idx = pd.date_range(daily.index[-1] + pd.Timedelta(days=1),
                               periods=horizon, freq="D")
    orders = load_orders(daily.index)
    orders_full = _extend_orders(orders, future_idx) if orders is not None else None
    events = load_events()
    upcoming = [e for e in events if e["end"] >= daily.index[-1]]
    print(f"      orders: {'yes' if orders is not None else 'none'} · "
          f"events: {len(events)} ({len(upcoming)} in/after horizon)")

    summary = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "source": xlsx_path,
               "history_days": len(daily),
               "history_start": str(daily.index.min().date()),
               "history_end": str(daily.index.max().date()),
               "horizon_days": horizon,
               "orders_provided": orders is not None,
               "events": [{"start": str(e["start"].date()), "end": str(e["end"].date()),
                           "name": e["name"], "impact_pct": e["impact_pct"]} for e in events],
               "model_labels": MODEL_LABELS,
               "segments": {}}
    ui = {"generated_at": summary["generated_at"], "model_labels": MODEL_LABELS,
          "horizon_days": horizon, "orders_provided": orders is not None,
          "events": summary["events"], "segments": {}}

    all_clean, all_forecast = {}, {}
    for col in cols:
        print(f"[2/5] {col}: filling gaps + normalizing outliers ...")
        filled = fill_missing_days(daily[col])
        if col != "total":
            filled = trim_launch(filled)           # drop dead pre-launch zeros
        clean, info = normalize_outliers(filled)
        info.to_csv(os.path.join(OUT_DIR, f"cleaning_{col}.csv"))
        all_clean[col] = clean

        sparse = float(daily[col].mean()) < 5      # micro-queue guard
        print(f"[3/5] {col}: back-testing 8 methods on the test set ...")
        ev, best, test_winner, backtest = evaluate(clean, sparse=sparse,
                                                   orders_full=orders_full)

        print(f"[4/5] {col}: forecasting next {horizon} days with '{best}' ...")
        fc = forecast_future(clean, horizon, best,
                             orders_full=orders_full, events=events)
        fc.to_csv(os.path.join(OUT_DIR, f"forecast_{col}.csv"))
        all_forecast[col] = fc

        n_out = int(info["stat_outlier"].sum())
        n_apr = int(info["april_capped"].sum())
        summary["segments"][col] = {
            "avg_daily_history": round(float(daily[col].mean()), 1),
            "outliers_normalized": n_out,
            "april_days_capped": n_apr,
            "forecast_model": best,
            "test_winner": test_winner,
            "test_winner_WAPE_%": ev[test_winner].get("WAPE_%"),
            "all_model_metrics": ev,
            "forecast_mean_daily": round(float(fc["forecast"].mean()), 0),
        }
        # rich per-segment payload for the UI (history tail + test + forecast)
        hist = clean.iloc[-90:]
        ui["segments"][col] = {
            "avg_daily_history": round(float(daily[col].mean()), 1),
            "forecast_model": best,
            "test_winner": test_winner,
            "metrics": ev,
            "history": {"dates": [d.strftime("%Y-%m-%d") for d in hist.index],
                        "values": np.round(hist.values, 1).tolist()},
            "backtest": backtest,
            "forecast": {"dates": [d.strftime("%Y-%m-%d") for d in fc.index],
                         "mean": fc["forecast"].tolist(),
                         "lower": fc["lower_80"].tolist(),
                         "upper": fc["upper_80"].tolist()},
        }
        print(f"      test winner={test_winner} (WAPE {ev[test_winner].get('WAPE_%')}%)  "
              f"forecast model={best}")

    with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(OUT_DIR, "results.json"), "w") as f:
        json.dump(ui, f)

    if make_plot:
        try:
            _plot(daily, all_clean, all_forecast, cols)
        except Exception as e:
            print("      (plot skipped:", str(e)[:80], ")")

    print(f"[5/5] Done. Artifacts in {OUT_DIR}")
    return summary


def _plot(daily, all_clean, all_forecast, cols):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(len(cols), 1, figsize=(13, 2.4 * len(cols)),
                             sharex=True)
    for ax, col in zip(np.atleast_1d(axes), cols):
        ax.plot(daily.index, daily[col], color="#bbb", lw=1, label="raw")
        ax.plot(all_clean[col].index, all_clean[col], color="#111", lw=1.2,
                label="cleaned")
        fc = all_forecast[col]
        ax.plot(fc.index, fc["forecast"], color="#d1006e", lw=1.8, label="forecast")
        ax.fill_between(fc.index, fc["lower_80"], fc["upper_80"], color="#d1006e",
                        alpha=0.15, label="80% CI")
        ax.axvspan(pd.Timestamp("2026-04-01"), pd.Timestamp("2026-04-30"),
                   color="#ffcc00", alpha=0.10)
        ax.set_title(f"Daily contacts — {col}", fontsize=10, loc="left")
        ax.legend(fontsize=7, ncol=4, loc="upper left")
        ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "forecast_plot.png"), dpi=110)
    plt.close(fig)


def parse_args():
    p = argparse.ArgumentParser(description="Sephora contact-volume forecaster")
    p.add_argument("--xlsx", default=DEFAULT_XLSX, help="path to Ss.xlsx export")
    p.add_argument("--horizon", type=int, default=HORIZON, help="days to forecast")
    p.add_argument("--no-plot", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    xlsx = resolve_source(args.xlsx)
    if not os.path.exists(xlsx):
        sys.exit(f"ERROR: data file not found: {xlsx}")
    print(f"[0/5] Source: {xlsx}")
    run(xlsx, args.horizon, make_plot=not args.no_plot)
