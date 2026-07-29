/*
  DroneX -- onboard firmware (runs on the drone itself, no separate
  Arduino board -- an ESP32 has enough GPIO/UART to handle the sensors,
  relay, MAVLink tap, and WiFi uplink directly).

  Reads DHT22 temp/humidity + PMS5003 PM2.5/PM10, taps the flight
  controller's MAVLink stream for live GPS and automatic fan-trigger on
  arrival at the target waypoint, drives the relay-controlled filtration
  fan, and POSTs JSON telemetry to the Flask app over WiFi:

    { "mission_id", "temp_c", "humidity_pct", "fan_on",
      "pm25", "pm10", "lat", "lon" }

  Wiring on the drone:
    DHT22 data              -> GPIO4
    Relay IN                -> GPIO5   (drives the fan/filtration unit)
    PMS5003 TX -> ESP32 RX2 -> GPIO16
    PMS5003 RX -> ESP32 TX2 -> GPIO17  (only needed if you use active/
                                        passive mode commands; wiring it
                                        is cheap insurance either way)
    Flight controller TELEM2 (MAVLink, 57600 baud typical for ArduPilot)
      FC TX -> ESP32 RX1    -> GPIO18
      FC RX -> ESP32 TX1    -> GPIO19  (only needed if you want to send
                                        MAVLink requests back; not
                                        required for passive listening)

  Requires the MAVLink C headers (header-only, not a "sketch library" in
  the traditional sense): download the `common/` dialect from
  https://github.com/mavlink/c_library_v2 and drop the whole
  `c_library_v2` folder into your Arduino `libraries/` directory (or next
  to this .ino) so `#include <common/mavlink.h>` resolves.

  Also requires the "DHT sensor library" (Adafruit) from Library Manager.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <DHT.h>
#include <common/mavlink.h>

// ---- DHT22 ---------------------------------------------------------------
#define DHTPIN 4
#define DHTTYPE DHT22
DHT dht(DHTPIN, DHTTYPE);

// ---- Relay / fan -----------------------------------------------------------
#define RELAY_PIN 5
bool fanOn = false;
unsigned long fanOnAt = 0;
// How long to run the fan once triggered, in seconds. Matches the
// MAV_CMD_NAV_LOITER_TIME duration set in mission.py (loiter_seconds=30) --
// update both together if you change the loiter time.
const unsigned long FAN_RUN_SECONDS = 30;

// ---- PMS5003 (PM2.5/PM10), UART2 -------------------------------------------
HardwareSerial pmsSerial(2); // RX=16, TX=17
uint8_t pmsBuf[32];
int pm25 = -1, pm10 = -1;         // -1 == "no valid reading yet" -> reported as null
unsigned long bootMillis = 0;
const unsigned long PMS_WARMUP_MS = 30000; // fan needs ~30s to stabilize readings

// Reads one 32-byte PMS5003 frame if available. Frame layout (Plantower
// datasheet): 0x42 0x4D <len:2> <10 data fields:2 each> <reserved:2>
// <checksum:2>, all big-endian. We use the "atmospheric environment"
// PM2.5/PM10 fields (bytes 12-13 and 14-15) rather than the CF=1 factory
// fields, since atmospheric is the calibrated outdoor-air reading.
bool readPMS(int &outPm25, int &outPm10) {
  while (pmsSerial.available() >= 32) {
    if (pmsSerial.peek() != 0x42) { pmsSerial.read(); continue; }
    int n = pmsSerial.readBytes(pmsBuf, 32);
    if (n != 32) return false;
    if (pmsBuf[0] != 0x42 || pmsBuf[1] != 0x4D) continue;

    uint16_t checksum = 0;
    for (int i = 0; i < 30; i++) checksum += pmsBuf[i];
    uint16_t frameChecksum = (pmsBuf[30] << 8) | pmsBuf[31];
    if (checksum != frameChecksum) continue; // corrupt frame, drop it

    outPm25 = (pmsBuf[12] << 8) | pmsBuf[13];
    outPm10 = (pmsBuf[14] << 8) | pmsBuf[15];
    return true;
  }
  return false;
}

// ---- MAVLink GPS + arrival tap, UART1 --------------------------------------
HardwareSerial mavSerial(1); // RX=18, TX=19
mavlink_message_t mavMsg;
mavlink_status_t mavStatus;

float currentLat = 0, currentLon = 0;
bool haveGPS = false;

// seq of the MAV_CMD_NAV_LOITER_TIME item at the target in the QGC plan
// built by mission.py's build_qgc_plan(): item 0 (implicit home) = seq 0,
// takeoff = seq 1, transit waypoint = seq 2, descent waypoint = seq 3,
// loiter-at-target = seq 4. Update this if mission.py's item order changes.
const uint16_t TARGET_WAYPOINT_SEQ = 4;

void pollMAVLink() {
  while (mavSerial.available()) {
    uint8_t c = mavSerial.read();
    if (mavlink_parse_char(MAVLINK_COMM_0, c, &mavMsg, &mavStatus)) {
      switch (mavMsg.msgid) {
        case MAVLINK_MSG_ID_GLOBAL_POSITION_INT: {
          currentLat = mavlink_msg_global_position_int_get_lat(&mavMsg) / 1e7;
          currentLon = mavlink_msg_global_position_int_get_lon(&mavMsg) / 1e7;
          haveGPS = true;
          break;
        }
        case MAVLINK_MSG_ID_MISSION_ITEM_REACHED: {
          uint16_t seq = mavlink_msg_mission_item_reached_get_seq(&mavMsg);
          if (seq == TARGET_WAYPOINT_SEQ && !fanOn) {
            fanOn = true;
            fanOnAt = millis();
            digitalWrite(RELAY_PIN, HIGH);
          }
          break;
        }
        default:
          break;
      }
    }
  }
}

// ---- WiFi / server ----------------------------------------------------------
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL = "http://YOUR_SERVER_HOST:5000/api/telemetry";
const char* MISSION_ID = "CURRENT_MISSION_ID"; // set at mission launch

unsigned long lastSend = 0;
const unsigned long SEND_INTERVAL_MS = 2000;

void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
  }
}

void sendTelemetry(float temp, float hum) {
  if (WiFi.status() != WL_CONNECTED) { connectWiFi(); }

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");

  String body = "{";
  body += "\"mission_id\":\"" + String(MISSION_ID) + "\",";
  body += "\"temp_c\":" + String(temp, 1) + ",";
  body += "\"humidity_pct\":" + String(hum, 1) + ",";
  body += "\"fan_on\":" + String(fanOn ? "true" : "false") + ",";
  body += "\"pm25\":" + (pm25 < 0 ? String("null") : String(pm25)) + ",";
  body += "\"pm10\":" + (pm10 < 0 ? String("null") : String(pm10));
  if (haveGPS) {
    body += ",\"lat\":" + String(currentLat, 6) + ",\"lon\":" + String(currentLon, 6);
  }
  body += "}";

  http.POST(body);
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);
  dht.begin();
  pmsSerial.begin(9600, SERIAL_8N1, 16, 17);
  mavSerial.begin(57600, SERIAL_8N1, 18, 19); // 57600 is ArduPilot's typical TELEM2 default; check yours
  connectWiFi();
  bootMillis = millis();
}

void loop() {
  pollMAVLink();

  // Manual fan override for bench testing (before a flight controller is
  // wired up, or to force it off/on during ground tests). MAVLink arrival
  // above and the auto-shutoff below are the normal in-flight path.
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "FAN_ON") { fanOn = true; fanOnAt = millis(); digitalWrite(RELAY_PIN, HIGH); }
    if (cmd == "FAN_OFF") { fanOn = false; digitalWrite(RELAY_PIN, LOW); }
  }

  // Auto-shutoff FAN_RUN_SECONDS after an automatic (MAVLink) trigger, so
  // the fan doesn't stay on for the whole flight home.
  if (fanOn && (millis() - fanOnAt) >= FAN_RUN_SECONDS * 1000UL) {
    fanOn = false;
    digitalWrite(RELAY_PIN, LOW);
  }

  int newPm25, newPm10;
  if (readPMS(newPm25, newPm10) && (millis() - bootMillis) >= PMS_WARMUP_MS) {
    pm25 = newPm25;
    pm10 = newPm10;
  }

  unsigned long now = millis();
  if (now - lastSend >= SEND_INTERVAL_MS) {
    lastSend = now;
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    if (isnan(h) || isnan(t)) return;
    sendTelemetry(t, h);
  }
}
