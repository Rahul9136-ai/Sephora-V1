"""
Sephora Forecast — web UI
=========================
Flask dashboard to explore the 8-method test results and the 30-day forecast
per Country-Channel-Language segment.

    python app.py            # then open http://127.0.0.1:5056

Reads the artifacts written by forecast_pipeline.py (outputs/results.json,
outputs/summary.json). Run the pipeline first if outputs/ is empty.
"""
import csv
import json
import os
import subprocess
import sys

from flask import Flask, jsonify, render_template, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "outputs")
DATA_DIR = os.path.join(HERE, "data")
ACTUALS_CSV = os.path.join(DATA_DIR, "actuals.csv")
ORDERS_CSV = os.path.join(DATA_DIR, "orders.csv")
EVENTS_CSV = os.path.join(DATA_DIR, "events.csv")
SOURCE_XLSX = os.path.join(DATA_DIR, "source.xlsx")
RAW_SHEETS = {"Voice Export (Gladly)", "Email_Chat Export (Gladly)"}

app = Flask(__name__, template_folder=os.path.join(HERE, "templates"))
app.json.sort_keys = False        # preserve insertion order (total first, by volume)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024   # allow large raw exports


def _load(name):
    path = os.path.join(OUT_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/results")
def api_results():
    results = _load("results.json")
    if results is None:
        return jsonify({"error": "No results yet. Run forecast_pipeline.py first."}), 404
    return jsonify(results)


def _append_row(path, header, row):
    os.makedirs(DATA_DIR, exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


@app.route("/api/data")
def api_data():
    """Current state of the user-editable inputs (events + row counts)."""
    def _count(p):
        if not os.path.exists(p):
            return 0
        with open(p, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    events = []
    if os.path.exists(EVENTS_CSV):
        with open(EVENTS_CSV, encoding="utf-8") as f:
            events = list(csv.DictReader(f))
    return jsonify({"events": events,
                    "actuals_rows": _count(ACTUALS_CSV),
                    "orders_rows": _count(ORDERS_CSV),
                    "custom_source": os.path.exists(SOURCE_XLSX)})


@app.route("/api/actual", methods=["POST"])
def api_actual():
    d = request.get_json(force=True)
    try:
        _append_row(ACTUALS_CSV, ["date", "segment", "contacts"],
                    [d["date"], str(d["segment"]).strip(), float(d["contacts"])])
        if d.get("orders") not in (None, ""):       # optional same-day orders
            _append_row(ORDERS_CSV, ["date", "orders"], [d["date"], float(d["orders"])])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 400


@app.route("/api/order", methods=["POST"])
def api_order():
    d = request.get_json(force=True)
    try:
        _append_row(ORDERS_CSV, ["date", "orders"], [d["date"], float(d["orders"])])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 400


@app.route("/api/event", methods=["POST"])
def api_event():
    d = request.get_json(force=True)
    try:
        _append_row(EVENTS_CSV, ["start", "end", "name", "impact_pct"],
                    [d["start"], d["end"], str(d["name"]).strip(),
                     float(d.get("impact_pct", 0) or 0)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 400


@app.route("/api/event/delete", methods=["POST"])
def api_event_delete():
    """Remove an event by name (exact match)."""
    name = request.get_json(force=True).get("name")
    if not os.path.exists(EVENTS_CSV):
        return jsonify({"ok": True})
    with open(EVENTS_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    kept = [r for r in rows if r.get("name") != name]
    with open(EVENTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["start", "end", "name", "impact_pct"])
        w.writeheader()
        w.writerows(kept)
    return jsonify({"ok": True, "removed": len(rows) - len(kept)})


def _import_events(df):
    """Append rows from a DataFrame that has start/end/name(/impact_pct)."""
    cols = {c.lower().strip(): c for c in df.columns}
    need = ("start", "end", "name")
    if not all(k in cols for k in need):
        return None
    n = 0
    for _, r in df.iterrows():
        try:
            imp = r[cols["impact_pct"]] if "impact_pct" in cols else 0
            _append_row(EVENTS_CSV, ["start", "end", "name", "impact_pct"],
                        [str(r[cols["start"]])[:10], str(r[cols["end"]])[:10],
                         str(r[cols["name"]]).strip(), float(imp or 0)])
            n += 1
        except Exception:
            continue
    return n


def _import_tabular(df):
    """Route a generic table to orders / actuals by its columns."""
    cols = {c.lower().strip(): c for c in df.columns}
    if "date" in cols and "orders" in cols:
        n = 0
        for _, r in df.iterrows():
            try:
                _append_row(ORDERS_CSV, ["date", "orders"],
                            [str(r[cols["date"]])[:10], float(r[cols["orders"]])]); n += 1
            except Exception:
                continue
        return ("orders", n)
    if "date" in cols and "segment" in cols and "contacts" in cols:
        n = 0
        for _, r in df.iterrows():
            try:
                _append_row(ACTUALS_CSV, ["date", "segment", "contacts"],
                            [str(r[cols["date"]])[:10], str(r[cols["segment"]]).strip(),
                             float(r[cols["contacts"]])]); n += 1
            except Exception:
                continue
        return ("actuals", n)
    return (None, 0)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Import an uploaded file, auto-detecting its kind:
      * raw Gladly export (xlsx with the Voice/Email-Chat sheets) -> becomes the
        pipeline source (data/source.xlsx)
      * events file (start,end,name[,impact_pct]) -> merged into the calendar
      * orders (date,orders) / actuals (date,segment,contacts) -> appended
    Does not retrain automatically — the user clicks Retrain afterwards.
    """
    import pandas as pd
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "no file"}), 400
    os.makedirs(DATA_DIR, exist_ok=True)
    name = f.filename
    lower = name.lower()
    try:
        if lower.endswith((".xlsx", ".xls", ".xlsm")):
            tmp = os.path.join(DATA_DIR, "_upload.xlsx")
            f.save(tmp)
            xl = pd.ExcelFile(tmp)
            sheets = list(xl.sheet_names)
            if RAW_SHEETS & set(sheets):                # raw Gladly export
                xl.close()                              # release the handle first
                os.replace(tmp, SOURCE_XLSX)
                return jsonify({"ok": True, "kind": "source",
                                "message": f"'{name}' set as the forecast source "
                                           f"({', '.join(s for s in sheets if s in RAW_SHEETS)}). "
                                           "Click Retrain to rebuild on it."})
            df = pd.read_excel(xl, sheets[0])
            xl.close()
            os.remove(tmp)
        elif lower.endswith((".csv", ".txt")):
            df = pd.read_csv(f)
        else:
            return jsonify({"ok": False, "error": "unsupported file type"}), 400

        ev = _import_events(df)
        if ev is not None:
            return jsonify({"ok": True, "kind": "events",
                            "message": f"Imported {ev} event(s) from '{name}'."})
        kind, n = _import_tabular(df)
        if kind:
            return jsonify({"ok": True, "kind": kind,
                            "message": f"Imported {n} {kind} row(s) from '{name}'. Click Retrain."})
        return jsonify({"ok": False, "error":
                        "Could not detect columns. Expected a raw Gladly export, "
                        "or columns [start,end,name], [date,orders], or "
                        "[date,segment,contacts]. Got: " + ", ".join(map(str, df.columns[:8]))}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 400


@app.route("/api/source/reset", methods=["POST"])
def api_source_reset():
    """Revert to the default Ss.xlsx source (remove the uploaded one)."""
    if os.path.exists(SOURCE_XLSX):
        os.remove(SOURCE_XLSX)
    return jsonify({"ok": True})


@app.route("/api/retrain", methods=["POST"])
def api_retrain():
    """Re-run the pipeline on demand (same code the daily scheduler uses),
    picking up any newly added actuals / orders / events."""
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "forecast_pipeline.py")],
                       cwd=HERE, check=True, timeout=1200)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/outputs/<path:fname>")
def outputs(fname):
    return send_from_directory(OUT_DIR, fname)


if __name__ == "__main__":
    print("Sephora Forecast UI -> http://127.0.0.1:5056")
    app.run(host="127.0.0.1", port=5056, debug=False)
