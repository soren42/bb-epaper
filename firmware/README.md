# bb-epaper firmware

Thin-client firmware for the Seeed XIAO ESP32-S3 + 7.5" 800×480 mono ePaper
panel (UC8179). All rendering lives on the Pi (`/usr/local/bb-epaper`); the
device just pulls a 48 KB packed bitmap and blits it.

## Hardware

- **MCU:** Seeed Studio XIAO ESP32-S3
- **Panel:** Seeed 7.5" 800×480 mono ePaper (UC8179 controller)
- **Carrier:** Seeed XIAO ePaper Driver Board (recommended — handles SPI + RST/BUSY/DC wiring)
- **Power:** USB-C (continuous; no battery / deep sleep path)

Pin mapping in `bb-epaper.ino` is for the Seeed driver board:

| Signal | XIAO pin |
|--------|---------|
| BUSY   | D5      |
| RST    | D0      |
| DC     | D3      |
| CS     | D1      |
| SCK    | D8      (SPI default) |
| MOSI   | D10     (SPI default) |

Edit the `PIN_*` macros if you wired the panel differently.

## Build (Arduino IDE)

1. **Board manager:** install **esp32 by Espressif Systems** (≥ 3.0).
2. **Board:** *XIAO_ESP32C3* (Tools → Board → ESP32 Arduino → XIAO_ESP32C3).
   Note: the unit reports as C3 despite some product pages saying S3.
3. **Libraries** (Tools → Manage Libraries):
   - `GxEPD2` by Jean-Marc Zingg — **then apply [the patch below](#library-patch)**
   - `WiFiManager` by tzapu
   - `ArduinoJson` by Benoît Blanchon
4. Open `firmware/bb-epaper/bb-epaper.ino`, plug the XIAO in (BOOT-held first
   flash if needed), select the serial port, hit **Upload**.

CLI flow (what we actually use):

```
~/bin/arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C3 firmware/bb-epaper
~/bin/arduino-cli upload  -p /dev/ttyACM0 --fqbn esp32:esp32:XIAO_ESP32C3 firmware/bb-epaper
```

## Library patch

The BUSY pin on the Seeed XIAO ePaper Display Board EE04/EE05 carrier is not
on D5 (where the older standalone ePaper Driver Board put it). Until we map
the correct GPIO, the firmware constructs the panel with `BUSY=-1`, which
tells GxEPD2 to substitute fixed `delay()`s for BUSY polling.

That makes one driver constant load-bearing: `GxEPD2_750_GDEY075T7::full_refresh_time`.
Stock value is 1200 ms (a fast-LUT figure); the real UC8179 7.5" full refresh
takes ~4.1 s. With 1200 ms, `powerOff()` halts the scan after ~8% — you see
only the header, body never commits.

Apply [`patches/GxEPD2_750_GDEY075T7-full_refresh_time.patch`](patches/GxEPD2_750_GDEY075T7-full_refresh_time.patch)
to your local GxEPD2 install:

```
cd ~/Arduino/libraries/GxEPD2
patch -p1 < /usr/local/bb-epaper/firmware/patches/GxEPD2_750_GDEY075T7-full_refresh_time.patch
```

If you ever reinstall/upgrade GxEPD2, re-apply this patch. Once the real BUSY
GPIO is identified and wired in `bb-epaper.ino`, you can revert the patch
(BUSY polling will pace the wait properly, and the constant stops mattering).

## First-time setup

On first boot the device has no WiFi creds, so it opens an AP named
**`bb-epaper-setup`**. From your phone/laptop:

1. Join `bb-epaper-setup` (open network).
2. The captive portal should pop up; if not, browse to `http://192.168.4.1`.
3. **Configure WiFi** → pick your SSID, enter password.
4. Set **BB-Terminal server URL** to `http://<pi-ip>:8081` (currently
   `http://10.5.2.20:8081`).
5. **Save**. The device reboots, joins WiFi, pulls a frame, and paints it.

**To re-enter setup later:** hold the **BOOT** button (the smaller of the two
buttons on the XIAO) for **≥ 2 seconds while the device is running normally**
(NOT during reset — on the ESP32-C3 that puts the chip in download mode).
The firmware detects the hold during its sleep loop, wipes saved WiFi creds,
and reboots into the `bb-epaper-setup` AP. Use this if you change WiFi
networks or want to point the device at a different Pi.

## How the loop runs

```
boot
 └→ WiFiManager.autoConnect()
     └→ HTTP GET  /epaper/frame.bin      (48000 bytes, 1 bpp packed)
        ↳ GxEPD2.drawImage(invert=true)  (panel update, ~3 s for full refresh)
     └→ HTTP POST /epaper/heartbeat       (fw, rssi, page)
        ↳ server replies with refresh_seconds; firmware honors it
     └→ delay(refresh_seconds)            (default 300)
     └→ loop()
```

## Iterating on layout

You **do not need to reflash** to change anything visual. Edit
`/usr/local/bb-epaper/renderer.py` on the Pi, then:

```
sudo systemctl restart bb-epaper
```

Within one cycle (≤ refresh_seconds), the new layout appears.

## Iterating on cadence / pages

The Pi web UI at `http://<pi-ip>:8081/` has a config form. The device
picks up changes on the next heartbeat round-trip.

## Troubleshooting

- **Blank panel after boot:** check serial monitor at 115200 baud. WiFi or
  HTTP error will print there.
- **`bb-epaper-setup` AP doesn't appear:** held BOOT during reset? Try a
  clean power cycle. WiFiManager keeps trying for 30 s before opening the AP.
- **Short read on frame.bin:** Pi service may be cold-fetching live data
  (8 s on a fresh cache); HTTPClient timeout in firmware is 15 s — fine, but
  if your link is flaky bump it.
- **Ghosting / faded image:** UC8179 panels need a *full* refresh occasionally.
  We always use `setFullWindow()` so this shouldn't accumulate. If it does,
  power-cycle the panel.
