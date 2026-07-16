# STEvalEVK-Phone

Capture one frame from an **STEVAL-EVK-U0I** (Cypress CX3 `cx3-spider` bridge +
**VD56G3** global-shutter sensor) directly on an Android phone — first via Termux
+ Python, then as a zero-install **WebUSB** web app. Full background in
[`handoff.md`](handoff.md).

## Status (2026-07-15)

Protocol discovery is done from static analysis of ST's **STSW-IMG507 v2.1.0**
SDK — see [`STATE.md`](STATE.md). One hard blocker remains (the main sensor
firmware patch, which ST didn't ship); see [`DECISIONS_QUEUE.md`](DECISIONS_QUEUE.md).

Key results:
- The EVK is **not UVC**; ST's host SDK is **native (libusb + C++), not pure
  Python**, and ships **no ARM64 build** → we reimplement the host, we don't port.
- The CX3 speaks an **ASCII command console over bulk USB**; the sensor is driven
  by **I²C register writes tunnelled through it**. Fully documented in
  [`PROTOCOL.md`](PROTOCOL.md).
- The **VT firmware patch is extracted** → [`firmware/vd56g3_vt_patch.json`](firmware/vd56g3_vt_patch.json).
- **Phase-1 (Termux pyusb) and Phase-2 (WebUSB) are written**, adversarially
  reviewed, and covered by an offline byte-level test (`python
  tests/test_protocol_offline.py`, 12/12).
- **The "FW patch" blocker is gone.** A cold USBPcap capture proved the VD56G3
  **streams unpatched**; the exact init-to-first-frame sequence
  (clock → `CMD_BOOT` → ROI → `CFG2WR` → `CMD_STREAMING`, RAW10 1120×1360) is
  captured to [`firmware/vd56g3_cold_init.json`](firmware/vd56g3_cold_init.json)
  and replayed verbatim. See [`PROTOCOL.md`](PROTOCOL.md) §9. Remaining: run it on
  the Pixel and decode a real frame.

## Layout

```
handoff.md            Original brief / goals / hardware facts
PROTOCOL.md           The recovered CX3 + VD56G3 protocol (the spec for both phases)
STATE.md              What works / what's blocked
DECISIONS_QUEUE.md    Choices needing your input (each has a recommendation)
docs/
  CAPTURE_HOWTO.md    USBPcap recipe to unblock the main-patch item
  TERMUX_SETUP.md     On-phone Phase-1 setup + run
evk/                  Phase-1 Python package (pyusb host)
  cx3_console.py      ASCII console transport over the bulk endpoint pair
  vd56g3.py           Sensor register I/O + CSI/stream config + cold_init
  patch.py            Optional VT/main patch helpers (NOT needed to stream)
  raw.py              Frame decode: status-line strip + RAW8/RAW10 unpack
  usb_termux.py       Termux fd adoption (libusb_wrap_sys_device) + desktop path
grab.py               Phase-1 CLI: init -> stream -> one frame -> JPG
web/                  Phase-2 WebUSB static app (index.html, app.js, protocol.js)
firmware/
  vd56g3_cold_init.json   Hardware-captured init-to-first-frame replay (the default path)
  vd56g3_csi_cfg2wr.json  Captured CFG2WR (CSI config) payloads, per bpp
  vd56g3_vt_patch.json    Extracted VT patch (optional; not needed to stream)
  vd56g3_registers.py     Register subset used by the init sequence
tools/
  extract_cold_init.py    Builds vd56g3_cold_init.json from a cold USBPcap
  extract_vt_patch.py     Reproduces vd56g3_vt_patch.json from ST's .so/.dll
  decode_usbpcap.py       Byte-scans any USB capture into a console transcript
  parse_usb_capture.py    URB-level USBPcap parser (endpoints, replies, frames)
tests/test_protocol_offline.py   Byte-level regression tests (no hardware)
.github/workflows/ci.yml         Compile + tests + JS syntax check
```

## Reproduce the VT-patch extraction

```
python tools/extract_vt_patch.py <path-to>/libst_brightsense_sdk_vdx6gx.so \
       firmware/vd56g3_vt_patch.json
```

## Next

Get the main FW patch (see `DECISIONS_QUEUE.md#2` — one USBPcap capture does it),
then write `grab.py` (Termux pyusb) to run the [`PROTOCOL.md`](PROTOCOL.md)
init-to-first-frame sequence and save a JPG.
