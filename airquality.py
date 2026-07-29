"""
Real AirNow integration.

AirNow (airnowapi.org) is monitor-station based, not a smooth grid -- a
single lat/long query returns observations from the nearest reporting
station(s) within a search radius, one row per pollutant. It does NOT
give you "5 nearby distinct locations" directly, so to build the
"why this location" comparison view we sample several points around the
user's address (a small ring of candidates within flight range) and
query AirNow's current-observation endpoint at each one. This is a
reasonable approximation given how AirNow's network is actually laid out.

Docs: https://docs.airnowapi.org/
"""
import os
import math
import time
import requests
from datetime import datetime, timedelta, timezone

AIRNOW_BASE = "https://www.airnowapi.org/aq/observation/latLong"
API_KEY = os.environ.get("AIRNOW_API_KEY", "")

# AQI category breakpoints (for labeling only)
AQI_CATEGORIES = [
    (0, 50, "Good"),
    (51, 100, "Moderate"),
    (101, 150, "Unhealthy for Sensitive Groups"),
    (151, 200, "Unhealthy"),
    (201, 300, "Very Unhealthy"),
    (301, 500, "Hazardous"),
]


class AirQualityError(Exception):
    pass


def _require_key():
    if not API_KEY:
        raise AirQualityError(
            "AIRNOW_API_KEY is not set. Get a free key at "
            "https://docs.airnowapi.org/account/request/ and set it as an "
            "environment variable before running the app."
        )


def aqi_category(aqi: int) -> str:
    for lo, hi, label in AQI_CATEGORIES:
        if lo <= aqi <= hi:
            return label
    return "Unknown"


def _destination_point(lat, lon, distance_miles, bearing_deg):
    """Project a point `distance_miles` from (lat, lon) along `bearing_deg`."""
    R = 3958.8  # earth radius, miles
    brng = math.radians(bearing_deg)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    d_r = distance_miles / R

    lat2 = math.asin(
        math.sin(lat1) * math.cos(d_r) + math.cos(lat1) * math.sin(d_r) * math.cos(brng)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brng) * math.sin(d_r) * math.cos(lat1),
        math.cos(d_r) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _query_point(lat, lon, radius_miles=25, distance_units_note=None):
    """Query AirNow current observations near a single point. Returns list of pollutant rows."""
    params = {
        "format": "application/json",
        "latitude": lat,
        "longitude": lon,
        "distance": radius_miles,
        "API_KEY": API_KEY,
    }
    resp = requests.get(f"{AIRNOW_BASE}/current/", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def find_pollution_hotspot(home_lat, home_lon, max_radius_miles=1.5, num_candidates=5):
    """
    Sample candidate points around the home location (within max_radius_miles,
    i.e. within one-way drone range) and query real AirNow data at each.
    Returns:
      {
        "candidates": [ {lat, lon, distance_miles, worst_aqi, worst_param, pollutants: [...] }, ... ],
        "chosen": <the candidate with the highest worst_aqi>
      }
    """
    _require_key()

    # Always include the home point itself as a candidate, plus a ring
    # around it so we're comparing several real readings, not just one.
    points = [(home_lat, home_lon, 0.0)]
    bearings = [i * (360 / max(1, num_candidates - 1)) for i in range(num_candidates - 1)]
    for b in bearings:
        plat, plon = _destination_point(home_lat, home_lon, max_radius_miles, b)
        points.append((plat, plon, max_radius_miles))

    candidates = []
    for lat, lon, dist in points:
        try:
            rows = _query_point(lat, lon)
        except requests.RequestException as e:
            continue  # skip unreachable point, don't fail the whole search
        if not rows:
            continue

        pollutants = [
            {
                "param": row.get("ParameterName"),
                "aqi": row.get("AQI"),
                "category": row.get("Category", {}).get("Name"),
                "reporting_area": row.get("ReportingArea"),
                "state": row.get("StateCode"),
            }
            for row in rows
            if row.get("AQI") is not None and row.get("AQI") >= 0
        ]
        if not pollutants:
            continue

        worst = max(pollutants, key=lambda p: p["aqi"])
        actual_dist = haversine_miles(home_lat, home_lon, lat, lon)
        candidates.append(
            {
                "lat": lat,
                "lon": lon,
                "distance_miles": round(actual_dist, 2),
                "worst_aqi": worst["aqi"],
                "worst_param": worst["param"],
                "reporting_area": worst.get("reporting_area"),
                "pollutants": pollutants,
            }
        )
        time.sleep(0.2)  # be polite to the API

    if not candidates:
        raise AirQualityError(
            "AirNow returned no observations near this address. Try a "
            "location closer to an urban area with active monitors."
        )

    chosen = max(candidates, key=lambda c: c["worst_aqi"])
    return {"candidates": candidates, "chosen": chosen}


def historical_trend(lat, lon, hours=24):
    """
    Build an AQI trend by making one historical call per hour, since AirNow's
    historical endpoint takes a single timestamp rather than a range.
    Returns a list of {time_iso, aqi, param} sorted oldest -> newest.
    Note: this makes `hours` API calls -- keep `hours` modest (<=48) to
    respect AirNow's rate limits.
    """
    _require_key()
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    points = []
    for h in range(hours, 0, -1):
        ts = now - timedelta(hours=h)
        date_param = ts.strftime("%Y-%m-%dT%H-0000")
        params = {
            "format": "application/json",
            "latitude": lat,
            "longitude": lon,
            "date": date_param,
            "distance": 25,
            "API_KEY": API_KEY,
        }
        try:
            resp = requests.get(f"{AIRNOW_BASE}/historical/", params=params, timeout=10)
            resp.raise_for_status()
            rows = resp.json()
        except requests.RequestException:
            rows = []

        if rows:
            worst = max(rows, key=lambda r: r.get("AQI", -1))
            points.append(
                {"time_iso": ts.isoformat(), "aqi": worst.get("AQI"), "param": worst.get("ParameterName")}
            )
        else:
            points.append({"time_iso": ts.isoformat(), "aqi": None, "param": None})

        time.sleep(0.1)

    return points
