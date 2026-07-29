import os
import json
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()  # must run before importing airquality, which reads AIRNOW_API_KEY at import time

from flask import Flask, request, jsonify, render_template, send_from_directory

from geocode import geocode_address, GeocodeError
from airquality import find_pollution_hotspot, historical_trend, aqi_category, AirQualityError
from mission import build_qgc_plan, save_plan, check_range, check_airspace

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Vercel's runtime filesystem is read-only except for /tmp, so route all
# writable state there when deployed. Locally we keep it in ./data.
if os.environ.get("VERCEL"):
    _RW_ROOT = "/tmp/dronex"
else:
    _RW_ROOT = os.path.join(BASE_DIR, "data")
DATA_DIR = _RW_ROOT
PLANS_DIR = os.path.join(DATA_DIR, "plans")
LOG_PATH = os.path.join(DATA_DIR, "flight_log.json")
LATEST_TELEMETRY_PATH = os.path.join(DATA_DIR, "latest_telemetry.json")

os.makedirs(PLANS_DIR, exist_ok=True)

DRONE_HOME = {
    "lat": float(os.environ.get("DRONE_HOME_LAT", 35.7796)),
    "lon": float(os.environ.get("DRONE_HOME_LON", -78.6382)),
}

# Mission targets are restricted to these two locations. The frontend sends
# the short key; the backend resolves it to the actual geocoding query, so
# a request straight to the API can't smuggle in an arbitrary address.
ALLOWED_LOCATIONS = {
    "virginia": {"label": "Virginia", "query": "Richmond, Virginia, USA"},
    "california": {"label": "California", "query": "Los Angeles, California, USA"},
}


# ---------- flight log helpers ----------

def _load_log():
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH) as f:
        return json.load(f)


def _save_log(log):
    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def _load_latest_telemetry():
    if not os.path.exists(LATEST_TELEMETRY_PATH):
        return None
    with open(LATEST_TELEMETRY_PATH) as f:
        return json.load(f)


# ---------- pages ----------

@app.route("/")
def index():
    return render_template("index.html", active="plan")


@app.route("/dashboard")
def dashboard():
    """Public read-only view: latest telemetry + most recent mission."""
    return render_template("dashboard.html", active="live")


@app.route("/history")
def history_page():
    return render_template("history.html", active="log")


# ---------- mission planning ----------

@app.route("/api/plan_mission", methods=["POST"])
def plan_mission():
    body = request.get_json(force=True) or {}
    location_key = (body.get("location") or "").strip().lower()
    max_round_trip = body.get("max_round_trip_miles")  # optional override

    if location_key not in ALLOWED_LOCATIONS:
        return jsonify({"error": "Choose Virginia or California."}), 400
    address = ALLOWED_LOCATIONS[location_key]["query"]

    # 1. Geocode
    try:
        loc = geocode_address(address)
    except GeocodeError as e:
        return jsonify({"error": str(e)}), 400

    home_lat, home_lon = DRONE_HOME["lat"], DRONE_HOME["lon"]

    # 2. Real AirNow search: sample candidates within one-way flight range
    #    (round trip / 2) so anything we pick is actually reachable.
    max_rt = float(max_round_trip) if max_round_trip else None
    one_way_budget = (max_rt or float(os.environ.get("DRONE_MAX_ROUND_TRIP_MILES", 3.0))) / 2

    try:
        result = find_pollution_hotspot(
            loc["lat"], loc["lon"], max_radius_miles=one_way_budget, num_candidates=5
        )
    except AirQualityError as e:
        return jsonify({"error": str(e)}), 502

    chosen = result["chosen"]
    for c in result["candidates"]:
        c["aqi_category"] = aqi_category(c["worst_aqi"])

    # 3. Battery-aware range check (home base -> chosen target, round trip)
    range_info = check_range(home_lat, home_lon, chosen["lat"], chosen["lon"], max_rt)

    # 4. Airspace / no-fly-zone check (best effort)
    airspace_info = check_airspace(chosen["lat"], chosen["lon"])

    # 5. Build QGC mission plan
    plan = build_qgc_plan(home_lat, home_lon, chosen["lat"], chosen["lon"])
    mission_id = str(uuid.uuid4())[:8]
    plan_path = os.path.join(PLANS_DIR, f"{mission_id}.plan")
    save_plan(plan, plan_path)

    # 6. Log it
    log = _load_log()
    entry = {
        "mission_id": mission_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "location": location_key,
        "location_label": ALLOWED_LOCATIONS[location_key]["label"],
        "address": address,
        "address_resolved": loc["display_name"],
        "target": {"lat": chosen["lat"], "lon": chosen["lon"]},
        "aqi_before": chosen["worst_aqi"],
        "aqi_param": chosen["worst_param"],
        "aqi_after": None,  # filled in later from telemetry, if you wire that up
        "range_info": range_info,
        "duration_seconds": None,
    }
    log.append(entry)
    _save_log(log)

    return jsonify(
        {
            "mission_id": mission_id,
            "home": DRONE_HOME,
            "resolved_address": loc,
            "candidates": result["candidates"],
            "chosen": chosen,
            "range": range_info,
            "airspace": airspace_info,
            "plan_download_url": f"/api/plan/{mission_id}",
        }
    )


@app.route("/api/plan/<mission_id>")
def download_plan(mission_id):
    filename = f"{mission_id}.plan"
    if not os.path.exists(os.path.join(PLANS_DIR, filename)):
        return jsonify({"error": "Mission not found"}), 404
    return send_from_directory(PLANS_DIR, filename, as_attachment=True)


@app.route("/api/trend")
def trend():
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    hours = request.args.get("hours", default=24, type=int)
    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400
    try:
        points = historical_trend(lat, lon, hours=min(hours, 48))
    except AirQualityError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"points": points})


# ---------- telemetry (ESP32 -> server) ----------

@app.route("/api/telemetry", methods=["POST"])
def receive_telemetry():
    """
    Called by the ESP32 bridge while the drone is in flight.
    Expected JSON body (extend freely as you add sensors, e.g. PMS5003
    pm25/pm10 fields -- the dashboard already renders any numeric field
    you add here):
      {
        "mission_id": "abc123",
        "temp_c": 24.1,
        "humidity_pct": 55.2,
        "fan_on": true,
        "lat": 35.78, "lon": -78.64,   # optional, for live map marker
        "pm25": null, "pm10": null      # populate once PMS5003 is wired in
      }
    """
    body = request.get_json(force=True) or {}
    body["received_at"] = datetime.now(timezone.utc).isoformat()
    with open(LATEST_TELEMETRY_PATH, "w") as f:
        json.dump(body, f, indent=2)
    return jsonify({"ok": True})


@app.route("/api/telemetry/latest")
def latest_telemetry():
    data = _load_latest_telemetry()
    return jsonify(data or {})


# ---------- flight log ----------

@app.route("/api/history")
def api_history():
    return jsonify(_load_log())


@app.route("/api/history/<mission_id>/close", methods=["POST"])
def close_mission(mission_id):
    """Call this when a mission completes to record AQI-after and duration."""
    body = request.get_json(force=True) or {}
    log = _load_log()
    for entry in log:
        if entry["mission_id"] == mission_id:
            entry["aqi_after"] = body.get("aqi_after")
            entry["duration_seconds"] = body.get("duration_seconds")
    _save_log(log)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
