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

app = Flask(__name__, template_folder=os.path.join(HERE, "templates"))
app.json.sort_keys = False        # preserve insertion order (total first, by volume)


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
                    "orders_rows": _count(ORDERS_CSV)})


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
