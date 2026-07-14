"""
Sephora Forecast — web UI
=========================
Flask dashboard to explore the 8-method test results and the 30-day forecast
per Country-Channel-Language segment.

    python app.py            # then open http://127.0.0.1:5056

Reads the artifacts written by forecast_pipeline.py (outputs/results.json,
outputs/summary.json). Run the pipeline first if outputs/ is empty.
"""
import json
import os
import subprocess
import sys

from flask import Flask, jsonify, render_template, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "outputs")

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


@app.route("/api/retrain", methods=["POST"])
def api_retrain():
    """Re-run the pipeline on demand (same code the daily scheduler uses)."""
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "forecast_pipeline.py")],
                       cwd=HERE, check=True, timeout=900)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/outputs/<path:fname>")
def outputs(fname):
    return send_from_directory(OUT_DIR, fname)


if __name__ == "__main__":
    print("Sephora Forecast UI -> http://127.0.0.1:5056")
    app.run(host="127.0.0.1", port=5056, debug=False)
