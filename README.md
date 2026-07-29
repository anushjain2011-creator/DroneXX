# DroneX -- Air Quality Response Drone

Enter an address, get a real-AirNow-backed pollution hotspot and a
QGroundControl mission file to fly there. Onboard telemetry
(temp/humidity, real PM2.5/PM10 from a PMS5003, and live GPS from the
flight controller's MAVLink stream) streams back live over WiFi, and the
filtration fan triggers automatically on arrival at the target.

## Setup

```bash
pip install -r requirements.txt

# Get a free AirNow API key: https://docs.airnowapi.org/account/request/
export AIRNOW_API_KEY="your-key-here"

# Optional: set your drone's actual home base and range
export DRONE_HOME_LAT=35.7796
export DRONE_HOME_LON=-78.6382
export DRONE_MAX_ROUND_TRIP_MILES=3.0

python app.py
```

Then open `http://localhost:5000`.

## Pages

- `/` -- mission planner. Enter an address, see the compared candidate
  readings, the chosen target, range/airspace checks, and download the
  `.plan` file for QGroundControl.
- `/dashboard` -- live telemetry view (polls every 3s). Public/read-only,
  safe to project on a screen during a demo.
- `/history` -- flight log of past missions (AQI before/after, distance,
  in-range status). Good for a portfolio link.

## How the "why this location" search works

AirNow is monitor-station based, not a smooth grid -- a single lat/long
query returns whatever real stations are within its search radius. To
give you several real candidates to compare (not just one black-box
pick), the app samples a small ring of points around the input address,
within your one-way flight budget, and queries AirNow's live
`observation/latLong/current` endpoint at each one. The worst AQI among
all candidates is flown to. This means:

- In dense urban areas, candidates may resolve to the same station.
- In rural areas, some candidate points may return no data (no monitor
  nearby) -- this is expected, not a bug.

## Hardware

The drone carries its own sensor/filtration electronics -- no separate
Arduino board. `firmware/drone_bridge/` is a single ESP32 sketch that:

- reads the DHT22 (temp/humidity) and the PMS5003 (real PM2.5/PM10,
  parsed from its UART frame with checksum validation, honoring the
  sensor's ~30s warm-up),
- taps the flight controller's MAVLink stream (`GLOBAL_POSITION_INT` for
  live GPS, `MISSION_ITEM_REACHED` for automatic fan-trigger when the
  drone reaches the target loiter waypoint -- no manual command needed
  in flight),
- drives the relay-controlled fan (auto-on at arrival, auto-off after
  `FAN_RUN_SECONDS`, plus a manual `FAN_ON`/`FAN_OFF` serial override for
  bench testing before a flight controller is wired up),
- POSTs all of it (`temp_c`, `humidity_pct`, `fan_on`, `pm25`, `pm10`,
  `lat`, `lon`) to `/api/telemetry` every 2s over WiFi.

Before flashing: fill in your WiFi credentials and server host at the
top of the sketch, install the "DHT sensor library" (Adafruit) via
Library Manager, and drop the `common/` MAVLink C headers from
https://github.com/mavlink/c_library_v2 into your Arduino `libraries/`
folder so `#include <common/mavlink.h>` resolves. Double-check your
flight controller's telemetry-port baud rate (57600 is ArduPilot's usual
TELEM2 default, but PX4 and custom setups vary) and wire per the pinout
comment at the top of the .ino.

`TARGET_WAYPOINT_SEQ` in the sketch (currently `4`) must match the
sequence number of the `MAV_CMD_NAV_LOITER_TIME` item that
`mission.py`'s `build_qgc_plan()` places at the target -- if you change
the item order or count in `mission.py`, update it there too.

## Extending

- **Closing the loop on AQI-after**: call `POST /api/history/<mission_id>/close`
  with `{"aqi_after": ..., "duration_seconds": ...}` once a mission lands,
  using the PMS5003's final reading, or a fresh AirNow query at RTL.

## Notes / limitations

- Geocoding (Nominatim) and airspace checks (FAA public ArcGIS layer) are
  free, no-key services with soft rate limits -- fine for personal/demo
  use, not for high-volume traffic.
- The AQI historical trend makes one API call per hour requested (AirNow's
  historical endpoint takes a single timestamp, not a range) -- keep
  `hours` modest.
- The airspace check is a coarse "airports nearby" flag, not a substitute
  for checking FAA's B4UFLY app before actually flying.
