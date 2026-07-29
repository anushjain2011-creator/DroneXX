"""
Builds a QGroundControl-compatible .plan file (JSON) for a
home -> target -> loiter/trigger -> home mission, plus a battery-aware
range check and a basic FAA-airspace proximity check.
"""
import json
import math
import os
import requests
from airquality import haversine_miles

DEFAULT_ALT_M = 40  # meters AGL for transit
LOITER_ALT_M = 20   # meters AGL over the target, lower for sensing/filtration
DRONE_MAX_ROUND_TRIP_MILES = float(os.environ.get("DRONE_MAX_ROUND_TRIP_MILES", 3.0))

# FAA UAS Facility Map / airport proximity data requires an account for the
# full dataset. As a free, no-key stand-in we use the public FAA/NASR
# airports list mirrored via the FAA's open ArcGIS layer for a coarse
# "is there an airport within N miles" check. If that fetch fails (e.g. no
# network), we skip the check rather than block mission generation.
FAA_AIRPORTS_ARCGIS_URL = (
    "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/"
    "Airports/FeatureServer/0/query"
)


class RangeError(Exception):
    pass


def check_range(home_lat, home_lon, target_lat, target_lon, max_round_trip_miles=None):
    """
    Returns {"one_way_miles": x, "round_trip_miles": x, "max_round_trip_miles": x, "in_range": bool}
    Does NOT raise -- caller decides whether to warn or block.
    """
    max_rt = max_round_trip_miles or DRONE_MAX_ROUND_TRIP_MILES
    one_way = haversine_miles(home_lat, home_lon, target_lat, target_lon)
    round_trip = one_way * 2
    return {
        "one_way_miles": round(one_way, 2),
        "round_trip_miles": round(round_trip, 2),
        "max_round_trip_miles": max_rt,
        "in_range": round_trip <= max_rt,
    }


def check_airspace(lat, lon, radius_miles=5):
    """
    Best-effort check for nearby airports using FAA's public airports layer.
    Returns {"checked": bool, "nearby_airports": [...], "warning": str or None}
    Fails soft (checked=False) if the service is unreachable -- this should
    never block mission generation, only add a caution note.
    """
    try:
        # bounding box query around the point
        deg_pad = radius_miles / 69.0  # ~69 miles per degree latitude
        params = {
            "geometry": f"{lon - deg_pad},{lat - deg_pad},{lon + deg_pad},{lat + deg_pad}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "false",
            "f": "json",
        }
        resp = requests.get(FAA_AIRPORTS_ARCGIS_URL, params=params, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        names = []
        for f in features[:5]:
            attrs = f.get("attributes", {})
            name = attrs.get("NAME") or attrs.get("IDENT") or "Unnamed airport"
            names.append(name)
        warning = None
        if names:
            warning = (
                f"{len(names)} airport(s) within ~{radius_miles} miles: {', '.join(names)}. "
                "Check FAA B4UFLY / LAANC before flying here."
            )
        return {"checked": True, "nearby_airports": names, "warning": warning}
    except requests.RequestException:
        return {
            "checked": False,
            "nearby_airports": [],
            "warning": "Could not reach FAA airspace data -- verify manually with B4UFLY before flying.",
        }


def build_qgc_plan(home_lat, home_lon, target_lat, target_lon, alt_m=DEFAULT_ALT_M,
                    loiter_alt_m=LOITER_ALT_M, loiter_seconds=30):
    """
    Returns a dict in QGroundControl .plan JSON format:
    home -> waypoint at target -> loiter (trigger fan) -> RTL.
    """
    items = [
        # 1. Takeoff
        {
            "AMSLAltAboveTerrain": None,
            "Altitude": alt_m,
            "AltitudeMode": 1,
            "autoContinue": True,
            "command": 22,  # MAV_CMD_NAV_TAKEOFF
            "doJumpId": 1,
            "frame": 3,
            "params": [0, 0, 0, None, home_lat, home_lon, alt_m],
            "type": "SimpleItem",
        },
        # 2. Waypoint: fly to target
        {
            "AMSLAltAboveTerrain": None,
            "Altitude": alt_m,
            "AltitudeMode": 1,
            "autoContinue": True,
            "command": 16,  # MAV_CMD_NAV_WAYPOINT
            "doJumpId": 2,
            "frame": 3,
            "params": [0, 0, 0, None, target_lat, target_lon, alt_m],
            "type": "SimpleItem",
        },
        # 3. Descend to loiter altitude over target for sensing/filtration
        {
            "AMSLAltAboveTerrain": None,
            "Altitude": loiter_alt_m,
            "AltitudeMode": 1,
            "autoContinue": True,
            "command": 16,  # MAV_CMD_NAV_WAYPOINT (same point, lower alt)
            "doJumpId": 3,
            "frame": 3,
            "params": [0, 0, 0, None, target_lat, target_lon, loiter_alt_m],
            "type": "SimpleItem",
        },
        # 4. Loiter for a fixed time -- this is the window where the onboard
        #    controller triggers the fan/filtration relay on GPS arrival.
        {
            "AMSLAltAboveTerrain": None,
            "Altitude": loiter_alt_m,
            "AltitudeMode": 1,
            "autoContinue": True,
            "command": 19,  # MAV_CMD_NAV_LOITER_TIME
            "doJumpId": 4,
            "frame": 3,
            "params": [loiter_seconds, 0, 0, None, target_lat, target_lon, loiter_alt_m],
            "type": "SimpleItem",
        },
        # 5. Return to launch
        {
            "autoContinue": True,
            "command": 20,  # MAV_CMD_NAV_RETURN_TO_LAUNCH
            "doJumpId": 5,
            "frame": 2,
            "params": [0, 0, 0, 0, 0, 0, 0],
            "type": "SimpleItem",
        },
    ]

    plan = {
        "fileType": "Plan",
        "version": 1,
        "groundStation": "QGroundControl",
        "mission": {
            "cruiseSpeed": 10,
            "hoverSpeed": 5,
            "firmwareType": 12,  # ArduPilot; use 3 for PX4
            "vehicleType": 2,  # Multirotor
            "version": 2,
            "globalPlanAltitudeMode": 1,
            "plannedHomePosition": [home_lat, home_lon, alt_m],
            "items": items,
        },
        "rallyPoints": {"points": [], "version": 2},
        "geoFence": {"circles": [], "polygons": [], "version": 2},
    }
    return plan


def save_plan(plan: dict, path: str):
    with open(path, "w") as f:
        json.dump(plan, f, indent=2)
