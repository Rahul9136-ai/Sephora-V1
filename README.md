# Sephora Contact-Volume Forecaster

Forecasts daily customer-contact volume for the **10 Country-Channel-Language
segments** (e.g. `US-PH-EN` = US / Phone / English) from the Gladly export
`Ss.xlsx`, with robust outlier normalization (extra damping for the **April
spring-sale surge**) and automatic daily retraining.

## Forecast granularity — the 10 segments

Each raw `(Channel, Country)` pair is mapped to a `COUNTRY-CHANNEL-LANG` code
(`PH`=Voice, `CH`=Chat, `EM`=Email; the language comes from the country field,
e.g. `US-EN`, `US-SP`, `CA-FR`). The aggregate country=`ALL` chat bucket is
**dropped**. This yields exactly 10 segments, plus a `total` = their sum:

| Voice | Chat | Email |
|-------|------|-------|
| US-PH-EN, US-PH-SP, CA-PH-EN, CA-PH-FR | US-CH-EN, CA-CH-EN, CA-CH-FR | US-EM-EN, CA-EM-EN, CA-EM-FR |

## What it does

1. **Ingest** — reads the raw `Voice Export` and `Email_Chat Export` sheets,
   maps each row to its segment (dropping `ALL`), and aggregates to a daily
   series per segment.
2. **Trim launch + fill gaps** — drops each segment's dead pre-launch zero
   period (several queues, e.g. US-EM-EN and CA-CH-FR, went live mid-history),
   then imputes missing calendar days (e.g. the 2025-12-25 Christmas gap) using
   the same-weekday local median so weekly seasonality is preserved.
3. **Normalize outliers (April-aware)** — see below.
4. **Back-test & select model** — holds out the last 28 days, back-tests four
   candidate models, and keeps the most accurate one *per segment* (by WAPE).
5. **Forecast** — refits the winner on all cleaned data and projects the next
   30 days with an 80% confidence interval.
6. **Persist** — writes every artifact to `outputs/` so a scheduler can rerun
   it unattended.

Very thin queues (< 5 contacts/day) and recently-launched ones are restricted to
seasonal-naive, and all forecasts are capped at 1.5x the recent 8-week peak, so
trend/drift models can never extrapolate absurd values on sparse data.

## Outlier normalization (and April)

A 2-week sustained April surge would normally pull a trend estimate *up* into the
surge, so nothing looks anomalous. To avoid that the pipeline:

- Blanks the known event windows (April sale + Nov/Dec holiday), rebuilds a clean
  **baseline** (robust weekly STL) across them, so the baseline is *not* inflated
  by the surge.
- **Statistical outliers** (robust z > 4 *and* >40% off baseline, outside events)
  are replaced with the baseline expectation.
- **April promo days** are winsorized: anything above `baseline × 1.35` is capped
  down to that level — this damps the *entire* surge, not just the tallest day
  (e.g. 16,048 → ~9,458 on 2026-04-14 while the baseline stays ~7,000).
- Nov/Dec holiday spikes are only lightly clipped (`× 2.2`) so the genuine season
  is retained.

All decisions are logged per day in `outputs/cleaning_<channel>.csv`
(`raw`, `baseline`, `robust_z`, `stat_outlier`, `april_capped`, `clean`).

Tune in `forecast_pipeline.py`: `Z_THRESH`, `REL_THRESH`, `APRIL_CAP`,
`APRIL_MONTHS`, `EVENT_WINDOWS`.

## Models — 8 methods compared on the test set

Every segment is back-tested with **8 forecasting methods**; the one with the
lowest hold-out WAPE wins and is used for the future forecast (guaranteeing the
shipped forecast is never worse than the naive baseline):

1. **Seasonal Naive** — repeat last week
2. **Seasonal Naive + Drift** — plus weekly level trend
3. **Moving Avg (by weekday)** — mean of last 4 same-weekday values
4. **Holt-Winters (ETS)** — damped additive trend + weekly seasonal
5. **SARIMA** — airline (0,1,1)(0,1,1)₇
6. **Theta** — statsmodels ThetaModel
7. **Linear Regression** — day-of-week one-hot + linear trend
8. **Gradient Boosting** — same calendar features, boosted trees

## Web UI

A Flask dashboard to explore the results:

```bat
python app.py            REM -> http://127.0.0.1:5056
```

**Segment detail** tab — pick any segment to see: the 8-method **test-set
accuracy table** (WAPE / MAPE / MAE / RMSE, best highlighted), a **test-window
chart** of actual vs every method (toggle methods on/off), and the **history +
30-day forecast** with its 80% interval.

**Leaderboard** tab — a wins-per-method tally plus a full **WAPE % heat-map
matrix** (every segment × all 8 methods, best per row in green, worse cells
tinted red). All 8 methods are scored for *every* segment, including the thin
launched queues.

> Two "best" models are tracked per segment: the **test winner** (most accurate
> on the hold-out) and the **forecast model** actually used going forward. They
> match except on very thin/launched queues, where the forward forecast is
> pinned to seasonal-naive for safety (shown as a "forecast uses…" badge).

A "Retrain now" button reruns the whole pipeline on demand.

## Run it

```bat
REM one-off
python forecast_pipeline.py

REM custom source / horizon
python forecast_pipeline.py --xlsx "D:\path\Ss.xlsx" --horizon 45 --no-plot
```

## Automatic retraining

`run_forecast.bat` re-ingests, re-normalizes, retrains and re-forecasts, appending
to `outputs/run.log`. A Windows Scheduled Task named **"Sephora Contact Forecast"**
runs it **daily at 06:00**, so each time `Ss.xlsx` is refreshed with new days the
model retrains and the forecast rolls forward automatically.

```bat
schtasks /Query  /TN "Sephora Contact Forecast"     REM inspect
schtasks /Run    /TN "Sephora Contact Forecast"     REM run now
schtasks /Change /TN "Sephora Contact Forecast" /ST 07:30   REM retime
schtasks /Delete /TN "Sephora Contact Forecast" /F  REM remove
```

> The task points at `C:\Users\lenovo\Desktop\Ss.xlsx`. Keep overwriting that file
> with the latest export (same sheet names/columns) and the forecast stays current.

## Outputs (`outputs/`)

| File | Contents |
|------|----------|
| `daily_contacts.csv`      | daily `total` + per-segment history |
| `cleaning_<segment>.csv`  | per-day outlier decisions & cleaned values |
| `forecast_<segment>.csv`  | next 30 days: `forecast`, `lower_80`, `upper_80` |
| `forecast_plot.png`       | raw vs cleaned vs forecast, April window shaded |
| `summary.json`            | run metadata, per-segment best model & accuracy (WAPE) |
| `run.log`                 | scheduled-run history |

## Requirements

Python 3 with `pandas`, `numpy`, `statsmodels`, `matplotlib` (all already
installed on this machine).
