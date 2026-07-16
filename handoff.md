# HANDOFF: ST EVK camera → Android capture app

## Goal

Build a minimal capture pipeline that runs on an Android phone (Pixel 10 XL), talks
to an STMicroelectronics EVK Main board (STEVAL-EVK-U0I) with a VD56G3 sensor over
USB, initializes it with default parameters, grabs one frame, and saves it as a JPG.

Two-phase plan:

1. **Phase 1 (validation): Termux + Python.** Run/adapt ST's Python SDK
   (STSW-IMG507) on the phone using `termux-usb` to pass a raw USB fd to
   pyusb/libusb. Goal: prove init + single-frame capture works on-device with
   minimal new code.
2. **Phase 2 (product): WebUSB web app.** Static HTML/JS page served over HTTPS
   (GitHub Pages) that claims the vendor interface via WebUSB in Chrome for
   Android, replays the init sequence, pulls one frame over bulk-in, renders to
   canvas, exports JPEG via `canvas.toBlob('image/jpeg')`.

Phase 2 only proceeds once Phase 1 has produced a documented, replayable protocol
transcript.

## Hardware / identifiers (confirmed by direct observation)

- Board: STEVAL-EVK-U0I ("EVK Main"), Cypress CX3 MIPI-to-USB bridge.
  Firmware product string: `cx3-spider`. Self-powered, 500 mA demanded.
- Sensor: VD56G3, 1.5 MP monochrome global shutter, native 1124×1364
  (EVK GUI rounds to 1120×1360). RAW10 or RAW8 output. Max 88 fps full-res.
- USB IDs: **VID 0x0553** (ST Imaging Division / VLSI Vision), **PID 0x040A**.
  Composite device; MI_00 is the "Stream" interface (bulk video). There is a
  separate control path (EVK GUI shows live status/I2C while video is down).
- Windows driver used by ST tooling: libusbK (vendor class, not UVC). This is
  good news for WebUSB — no kernel class driver will claim it on Android.
- Device is SuperSpeed-capable. **Requires a genuine 5 Gbps cable** — with a
  USB2-only cable it enumerates and control traffic works but the video bulk
  stream silently starves (we lost hours to this; verified via USB Tree Viewer
  "connected at HighSpeed"). Use a C-to-C 5 Gbps cable on the phone.
- Sensor requires a firmware patch upload at every cold init (Status pane shows
  e.g. "FW=5.0, VT=17" after patching; a freshly powered sensor has no patch).
  The host tooling performs this — the patch blob must be extracted from the SDK
  or captured from a sniff. Phone will power-cycle VBUS between sessions, so
  full init runs every time.

## Known-good reference setup

Windows 11 PC running STSW-IMG501 v1.1.x ("EVK GS" GUI) streams the sensor
successfully at 1120×1360 RAW10, 60 fps, 2 lanes × 1010 Mbps. This machine is
available for USB sniffing (Wireshark + USBPcap) if the SDK route falls short.

## Primary source to mine: STSW-IMG507

`STSW-IMG507_56G3` (v2.x) — ST's Python SDK for this sensor family on the EVK.
Contains `vdx6gx_example_open_cv.py` which does init + streaming.

**FIRST TASK: obtain the zip (free download from st.com, may require login —
user can download and drop it in the repo), unzip, and answer: is the USB layer
pure Python (pyusb/libusbK bindings) or a native DLL wrapper?**

- If pure Python → transcribe: enumerate every control transfer (bmRequestType,
  bRequest, wValue, wIndex, payload), the sensor patch upload, the I2C register
  write batches, stream-start command, and the bulk data framing (headers per
  frame/line? alignment? RAW10 packing?). Produce `PROTOCOL.md` documenting the
  minimal init-to-first-frame sequence.
- If native wrapper → fall back to USBPcap capture on the Windows box:
  plug-in → GUI start → first few frames. Same deliverable: `PROTOCOL.md`.

## Phase 1 details (Termux)

- `pkg install python libusb; pip install pyusb`
- `termux-usb -l` to list, `termux-usb -r -e ./grab.py /dev/bus/usb/XXX/YYY` to
  get the fd. pyusb needs the fd-based open path (libusb `libusb_wrap_sys_device`)
  — handle this; stock pyusb device discovery won't work under Android's USB
  permission model.
- Target: `grab.py` — init sensor (default params, RAW8, full res 1120×1360,
  modest fps), capture ONE frame, save `frame.png`/`frame.jpg` (Pillow), exit.
- Keep every USB transaction logged — this log IS the spec for Phase 2.

## Phase 2 details (WebUSB)

- Static site, no build step preferred (user deploys via GitHub Pages; dev
  happens on mobile via GitHub Actions — no dev machine).
- `navigator.usb.requestDevice({filters:[{vendorId:0x0553, productId:0x040A}]})`
- Claim the vendor interface(s); replay init from PROTOCOL.md; `transferIn`
  loop on the bulk endpoint (~1.5 MB per RAW8 frame — chunked reads, reassemble,
  strip framing).
- RAW8 first (skip 10-bit unpacking). Grayscale → ImageData → canvas → JPEG blob
  → download link.
- UI: one Connect button, one Capture button, image preview. Nothing else.

## Constraints & conventions

- User has no desktop dev machine for the Android side: development happens
  on-phone (Termux) and via GitHub Actions CI. Structure the repo so everything
  is testable/buildable from mobile.
- User is expert-level in embedded/USB/MIPI — do not oversimplify; do surface
  register-level detail.
- Follow the existing handoff conventions: maintain `STATE.md` (what works,
  what's blocked) and `DECISIONS_QUEUE.md` (choices needing user input) alongside
  this file.

## Open questions (resolve in order)

1. Is STSW-IMG507's USB layer pure Python? (Determines everything downstream.)
2. What exactly does the CX3 need from the host vs. what is sensor I2C traffic
   tunneled through it? (Affects how much must be replayed verbatim.)
3. Bulk stream framing: per-frame or per-line headers? Any embedded status
   lines (ISL) prepended/appended to the image?
4. Does `termux-usb` + libusb_wrap_sys_device work cleanly on the Pixel 10 XL's
   Android build, or does SELinux get in the way?
5. WebUSB on Android: confirm Chrome can claim BOTH interfaces if the control
   and stream interfaces are separate (composite device, MI_00 observed).

## Success criteria

- Phase 1: JPG of a real scene captured entirely from the phone, no PC involved.
- Phase 2: same result from a URL in Chrome with zero installs.
