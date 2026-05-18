// bb-epaper firmware — XIAO ESP32-S3 + Seeed 7.5" 800x480 mono panel (UC8179).
//
// What it does:
//   1. On first boot (or held BOOT button), opens a captive-portal AP named
//      "bb-epaper-setup" so you can join WiFi and set the server URL.
//   2. Every refresh_seconds (default 300, server-controlled), it:
//        GET  http://<server>/epaper/frame.bin   -> 48000 bytes of 1bpp pixels
//        blits the buffer to the panel via GxEPD2
//        POST http://<server>/epaper/heartbeat   -> reports fw + RSSI + page
//      The server's response carries the next refresh_seconds, so changing
//      the cadence on the Pi web UI takes effect after one cycle.
//
// Why no deep sleep: USB-powered. Deep sleep would just complicate boot
// and the WiFi reconnect. delay() is fine here.
//
// Pin mapping below assumes the Seeed XIAO Expansion / ePaper Driver Board.
// If you wired the panel directly to bare XIAO pins, just edit the #defines.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiManager.h>      // tzapu/WiFiManager
#include <HTTPClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>      // bblanchon/ArduinoJson

// --- e-paper driver ---
// GxEPD2_750_GDEY075T7 LUT matches this panel (T7 LUT clears the screen
// instead of rendering). But GDEY075T7's stock full_refresh_time=1200ms is
// too short for a real full refresh; with BUSY=-1, GxEPD2 powers the panel
// off mid-scan and only the top ~8% commits. Fixed by patching the value
// in GxEPD2/src/gdey/GxEPD2_750_GDEY075T7.h to 5000 ms. If you reinstall
// GxEPD2, re-apply that patch (or wire BUSY correctly and revert it).
#include <GxEPD2_BW.h>

// --- Pin map (Seeed XIAO ESP32-S3 ↔ ePaper driver board) ---
// These are the XIAO "Dx" labels. Adjust if you wired the panel differently.
#define PIN_BUSY  D5
#define PIN_RST   D0
#define PIN_DC    D3
#define PIN_CS    D1
// SCK (D8) and MOSI (D10) are picked up by SPI.begin() defaults.

// BUSY=-1 because the BUSY pin is mis-wired for this carrier (EE04/EE05).
// With BUSY disconnected, GxEPD2 uses delay(full_refresh_time). We patched
// GDEY075T7.h to set that to 5000 ms; see the include block comment above.
GxEPD2_BW<GxEPD2_750_GDEY075T7, GxEPD2_750_GDEY075T7::HEIGHT>
  display(GxEPD2_750_GDEY075T7(/*CS=*/PIN_CS, /*DC=*/PIN_DC, /*RST=*/PIN_RST, /*BUSY=*/-1));

// --- Frame buffer (matches Pi-side renderer.image_to_packed_1bpp) ---
static constexpr uint16_t W = 800;
static constexpr uint16_t H = 480;
static constexpr size_t   FRAME_BYTES = (W * H) / 8;   // 48000

// 48 KB sitting in regular SRAM. Static avoids fragmentation.
static uint8_t frameBuf[FRAME_BYTES];

// --- Config (persisted in NVS) ---
Preferences prefs;
String serverUrl = "http://10.5.2.20:8081";    // editable in the captive portal
uint32_t refreshSeconds = 300;                  // server may override each cycle
String lastPage = "?";

// --- Identity reported in heartbeats ---
static constexpr const char* FW_VERSION = "bb-epaper-fw 0.1.0";

// Forward declaration so connectWiFi() can paint a status splash when forced
// into config-portal mode. Arduino-cli auto-generates these for .ino sketches,
// but spelling it out keeps us robust against the order of definitions below.
void splash(const char* line1, const char* line2);

// ---------------------------------------------------------------------------
// WiFi + first-time config
// ---------------------------------------------------------------------------
// XIAO ESP32-C3 BOOT button is wired to GPIO9 (active-low). GPIO9 is also the
// bootloader strap pin — held LOW during power-on puts the chip in USB
// download mode, so we can't read BOOT at boot the way you would on the S3.
// Instead, poll BOOT in loop(): a ≥2 s hold while the firmware is running
// wipes WiFi creds and reboots into the captive portal.
static constexpr uint8_t  PIN_BOOT      = 9;
static constexpr uint32_t BOOT_HOLD_MS  = 2000;

// Returns true once the BOOT button has been held continuously for BOOT_HOLD_MS.
// Call frequently — it tracks state across invocations.
bool bootButtonHeldDuringRuntime() {
  static uint32_t pressedSince = 0;
  if (digitalRead(PIN_BOOT) == LOW) {
    if (pressedSince == 0) pressedSince = millis();
    if (millis() - pressedSince >= BOOT_HOLD_MS) return true;
  } else {
    pressedSince = 0;
  }
  return false;
}

void apModeCallback(WiFiManager* wm) {
  // Painted on the e-paper so a user looking at the panel knows exactly what to do.
  splash("setup mode", "join 'bb-epaper-setup' -> 192.168.4.1");
}

void connectWiFi(bool forceConfigPortal) {
  WiFiManager wm;
  WiFiManagerParameter pServer("server", "BB-Terminal server URL",
                               serverUrl.c_str(), 96);
  wm.addParameter(&pServer);

  // 3 min portal timeout — long enough to type creds, short enough to retry on hiccups.
  wm.setConfigPortalTimeout(180);
  // Paint a helpful splash whenever the AP comes up, regardless of cause.
  wm.setAPCallback(apModeCallback);

  if (forceConfigPortal) {
    Serial.println("[wifi] BOOT held — clearing saved creds and opening portal");
    wm.resetSettings();
  }

  bool ok = wm.autoConnect("bb-epaper-setup");
  if (!ok) {
    Serial.println("[wifi] portal timed out, rebooting");
    delay(1000);
    ESP.restart();
  }

  // If user typed something, store it.
  String typed = pServer.getValue();
  if (typed.length() > 0 && typed != serverUrl) {
    serverUrl = typed;
    prefs.putString("server", serverUrl);
    Serial.printf("[cfg] server URL set: %s\n", serverUrl.c_str());
  }

  Serial.printf("[wifi] connected, IP=%s RSSI=%d\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

// ---------------------------------------------------------------------------
// HTTP: fetch frame, post heartbeat
// ---------------------------------------------------------------------------
bool fetchFrame() {
  HTTPClient http;
  http.setTimeout(15000);
  http.setReuse(false);
  String url = serverUrl + "/epaper/frame.bin";
  if (!http.begin(url)) {
    Serial.println("[http] begin() failed");
    return false;
  }

  // Must register headers we care about BEFORE GET() — HTTPClient otherwise
  // discards response headers it doesn't recognize.
  static const char* kHeaders[] = { "X-Epaper-Page" };
  http.collectHeaders(kHeaders, sizeof(kHeaders) / sizeof(kHeaders[0]));

  int code = http.GET();
  if (code != 200) {
    Serial.printf("[http] frame.bin -> %d\n", code);
    http.end();
    return false;
  }

  // Capture which page the server says this is, so heartbeat reflects reality.
  if (http.hasHeader("X-Epaper-Page")) {
    lastPage = http.header("X-Epaper-Page");
  }

  // Stream into frameBuf. Reading in chunks keeps the TCP window flowing.
  WiFiClient* stream = http.getStreamPtr();
  size_t total = 0;
  uint32_t startMs = millis();
  while (http.connected() && total < FRAME_BYTES) {
    size_t avail = stream->available();
    if (avail) {
      int n = stream->readBytes(frameBuf + total,
                                min(avail, FRAME_BYTES - total));
      if (n <= 0) break;
      total += n;
    } else {
      if (millis() - startMs > 15000) break;
      delay(2);
    }
  }
  http.end();

  if (total != FRAME_BYTES) {
    Serial.printf("[http] short read: %u/%u\n",
                  (unsigned)total, (unsigned)FRAME_BYTES);
    return false;
  }
  return true;
}

void postHeartbeat() {
  HTTPClient http;
  http.setTimeout(5000);
  String url = serverUrl + "/epaper/heartbeat";
  if (!http.begin(url)) return;
  http.addHeader("Content-Type", "application/json");

  JsonDocument body;
  body["fw"]   = FW_VERSION;
  body["rssi"] = (int)WiFi.RSSI();
  body["page"] = lastPage;
  String payload;
  serializeJson(body, payload);

  int code = http.POST(payload);
  if (code == 200) {
    JsonDocument resp;
    DeserializationError err = deserializeJson(resp, http.getString());
    if (!err) {
      uint32_t srv = resp["refresh_seconds"] | refreshSeconds;
      // Clamp to sane bounds in case server config is corrupted.
      if (srv >= 60 && srv <= 3600) refreshSeconds = srv;
    }
  } else {
    Serial.printf("[http] heartbeat -> %d\n", code);
  }
  http.end();
}

// ---------------------------------------------------------------------------
// Panel drawing
// ---------------------------------------------------------------------------
void blitFrame() {
  // Split writeImage+refresh so we can FORCE full-refresh mode every cycle.
  // drawImage() defers to refresh(x,y,w,h) which only goes full-refresh on
  // the first post-boot call; subsequent calls use the partial-refresh LUT
  // (450 ms, no DC bias settle), which ghosts badly when content changes
  // wholesale — that's the mottled grey symptom.
  // invert=true bridges the Pi's UC8179-native bytes (1=white) to GxEPD2's
  // 1=black foreground convention.
  display.writeImage(frameBuf, 0, 0, W, H,
                     /*invert=*/true,
                     /*mirror_y=*/false,
                     /*pgm=*/false);
  display.refresh(false);  // false = full-refresh mode (forces full LUT every cycle)
  display.hibernate();
}

// Tiny status splash for boot / errors when we have no real frame yet.
void splash(const char* line1, const char* line2) {
  display.setFullWindow();
  display.firstPage();
  do {
    display.fillScreen(GxEPD_WHITE);
    display.setTextColor(GxEPD_BLACK);
    display.setCursor(20, 40);
    display.print("bb-epaper");
    display.setCursor(20, 80);
    display.print(line1);
    if (line2) {
      display.setCursor(20, 110);
      display.print(line2);
    }
  } while (display.nextPage());
  display.hibernate();
}

// ---------------------------------------------------------------------------
// Setup + loop
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println(FW_VERSION);

  prefs.begin("bb-epaper", /*readOnly=*/false);
  serverUrl = prefs.getString("server", serverUrl);

  // GPIO9 is the BOOT-strap pin on the C3: held LOW at reset = ROM download
  // mode, so we can't read it at boot. Pull it up here for runtime polling.
  pinMode(PIN_BOOT, INPUT_PULLUP);

  display.init(/*serial_diag_bitrate=*/0, /*initial=*/true, /*pulldown_rst_ms=*/10);
  splash("booting...", "");

  connectWiFi(/*forceConfigPortal=*/false);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] reconnecting...");
    WiFi.reconnect();
    delay(3000);
    if (WiFi.status() != WL_CONNECTED) {
      // Don't refresh the panel on every retry — paint once and keep trying.
      static bool painted = false;
      if (!painted) { splash("offline", "waiting for WiFi"); painted = true; }
      delay(5000);
      return;
    }
  }

  if (fetchFrame()) {
    blitFrame();
    postHeartbeat();
  } else {
    Serial.println("[loop] frame fetch failed, will retry");
    // Leave the existing panel content alone — last good frame stays visible.
  }

  // delay() honors refreshSeconds set by last heartbeat reply. We poll BOOT
  // each second so a 2 s hold anywhere in the sleep window triggers a reset
  // into captive-portal mode.
  Serial.printf("[loop] sleeping %u s\n", (unsigned)refreshSeconds);
  uint32_t end = millis() + (refreshSeconds * 1000UL);
  while ((int32_t)(end - millis()) > 0) {
    if (bootButtonHeldDuringRuntime()) {
      Serial.println("[boot] runtime BOOT-hold detected, clearing WiFi + reboot");
      splash("setup mode", "rebooting into portal...");
      WiFiManager wm;
      wm.resetSettings();
      delay(500);
      ESP.restart();
    }
    delay(200);
  }
}
