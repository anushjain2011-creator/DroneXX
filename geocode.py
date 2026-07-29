"""
Address -> lat/lon geocoding using OpenStreetMap's free Nominatim API.
No API key required, but Nominatim's usage policy requires a descriptive
User-Agent and asks that you not hammer it (max ~1 req/sec).
"""
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "DroneX-AirQualityResponseDrone/1.0 (hobby project)"


class GeocodeError(Exception):
    pass


def geocode_address(address: str) -> dict:
    """
    Returns {"lat": float, "lon": float, "display_name": str}
    Raises GeocodeError if the address can't be resolved.
    """
    if not address or not address.strip():
        raise GeocodeError("Address is empty.")

    params = {
        "q": address,
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    }
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise GeocodeError(f"Geocoding request failed: {e}")

    results = resp.json()
    if not results:
        raise GeocodeError(f"Could not find a location for '{address}'.")

    top = results[0]
    return {
        "lat": float(top["lat"]),
        "lon": float(top["lon"]),
        "display_name": top.get("display_name", address),
    }
