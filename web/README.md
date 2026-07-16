# web/ — Phase 2: WebUSB one-frame grab (Chrome for Android)

A **static, no-build** WebUSB app that connects to the **STEVAL-EVK-U0I**
(Cypress CX3 "spider" + ST VD56G3), replays the init-to-first-frame sequence,
pulls **one** frame over the video bulk-IN endpoint, renders it to a `<canvas>`,
and exports a JPEG via `canvas.toBlob("image/jpeg")`.

It mirrors the Phase-1 Python protocol **byte for byte** — the authoritative
spec is [`../PROTOCOL.md`](../PROTOCOL.md). No frameworks, no CDNs, no bundler:
three files (`index.html`, `app.js`, `protocol.js`) served over HTTPS.

## Files

| File | Role |
|---|---|
| `index.html` | Self-contained page: Connect / Capture buttons, canvas preview, log, endpoint + bpp inputs. |
| `protocol.js` | ES module. `Cx3Console` (ASCII console transport), `Vd56g3` (register/stream), patch loaders, `decodeFrame`. Pure protocol logic. |
| `app.js` | ES module. UI wiring: connect + endpoint discovery, cold init orchestration, single-frame read, canvas render, JPEG export. |

## Requirements

- **Chrome for Android** (or desktop Chrome/Edge). WebUSB is Chromium-only;
  Firefox/Safari do **not** implement it.
- **A secure context (HTTPS).** WebUSB refuses to run on plain HTTP. `localhost`
  is exempt for local testing, but on a phone you need HTTPS → **GitHub Pages**.
- A **SuperSpeed (5 Gbps) USB cable/OTG path** — the video bulk stream starves on
  USB 2 (PROTOCOL.md §1).

## Deploy on GitHub Pages (automated)

The [`deploy-pages`](../.github/workflows/pages.yml) workflow builds and deploys
this on every push to `main` that touches `web/` or `firmware/`. It assembles a
site root = `web/` contents **plus a `firmware/` subdir** (so the app can fetch
the captured init sequence at `firmware/vd56g3_cold_init.json`), then publishes it.

- One-time: **Settings → Pages → Build and deployment → Source = GitHub Actions**
  (the workflow will try to enable this automatically).
- App URL: **`https://<user>.github.io/<repo>/`** (served at the site root, not
  `/web/`). Open it in **Chrome for Android**.
- Tap **Connect**, choose the EVK (chooser filtered to VID `0x0553` / PID
  `0x040A`), then **Capture frame**.

> Pages is HTTPS by default, satisfying WebUSB's secure-context requirement. No
> server code runs — entirely static.

### Firmware fetch path

`replayColdInit` fetches `firmware/vd56g3_cold_init.json` (the hardware-captured
sequence — no patch needed). It tries `firmware/…` first (the deployed layout,
where the action copies `firmware/` next to `index.html`) and falls back to
`../firmware/…` (repo served at `/web/`), so the same code works in dev and
deployed. Same-origin fetch under Pages needs no CORS config.

## How it initializes (no firmware patch needed)

`replayColdInit(sensor, url)` in `protocol.js` plays the **hardware-captured**
cold-init sequence from `../firmware/vd56g3_cold_init.json` (PROTOCOL.md §9): the
exact ordered commands ST's GUI sent to a cold, **unpatched** VD56G3 that then
streamed — register writes (`I2CWRRD` rdlen=0), `CLKWR`/`CFG2WR`/`NRST`/`IOSET`/
`IOCFGWR`, ending at `CMD_START_STREAM (0x0201) <- 1` (the sensor is streaming
when the replay returns). A cold capture proved the sensor streams with **no FW
patch** (`FWPATCH_REVISION` = 0), so there's nothing to download. It streams
**RAW10 at 1120×1360**. The optional `loadMainPatch`/`loadVtPatch` helpers
remain for the *enhanced* firmware but are not called by default.

**2026-07-16 — this whole flow is hardware-validated** (via the identical
Python implementation on the reference PC, which captured a clean photo; the JS
decode is pixel-exact against it on a real captured frame). Three fixes from
that session are load-bearing (PROTOCOL.md §9.0/§5.0): command registers
self-clear and must be polled to 0 (`sendCommand`); `0x0201` starts / `0x0202`
stops streaming (ST's names suggest the opposite — the replay used to end with
the captured session's *Stop* click); and frames arrive as 16 KB header+footer
chunks read with ONE frame-sized transfer (`readFrame`) because the CX3 stalls
the pipe if the host pauses mid-frame.

## Configurable knobs

- **Endpoints** — auto-discovered (command-OUT `0x05` ↔ answer-IN `0x85` by number,
  video-IN `0x83`), matching the captured addresses; override via the three number
  inputs (blank = auto).
- **bpp** — the replay uses the captured format (RAW10); the dropdown affects only
  the (unused-by-default) synthetic path.
- **CSI config** — sent as the captured `CFG2WR` bytes (`CFG2WR_CAPTURED`),
  replayed verbatim.

## Verified against real hardware (PROTOCOL.md §5.0, §8–9)

Endpoint addresses, reply grammar (`OK <HH…>`), the `I2CWRRD`-rdlen0 write
encoding, `CFG2WR`, the full init sequence, the command handshake, the wire
chunk format, and the frame decode are all confirmed against the live board
(photo captured on the PC 2026-07-16; JS decode pixel-exact vs the Python
reference on a real frame). The only remaining unknown is Chrome-on-Android
itself: whether `claimInterface` succeeds on the Pixel (see below).

## Troubleshooting: "Unable to claim interface"

If Connect logs `claimInterface(...) failed: Unable to claim interface`, that is a
usbfs `EBUSY` from Chrome's Android backend — **not** a bug in this app (the
device's interfaces are vendor-class `0xFF`, so they aren't Chrome-blocked). The
app already: forces `selectConfiguration(1)`, retries the claim, selects alt 0,
releases stale handles on reconnect, and aborts with guidance rather than masking
the error. If it still fails:

1. **Read `chrome://device-log` on the phone** right after the failure — it prints
   `Failed to claim interface:` with the exact errno and, if a driver is bound,
   its name. `EBUSY` + a driver = a kernel driver holds it; `EBUSY` + no driver =
   another consumer (a stale tab/PWA, or a PC running the ST GUI on the same cable).
2. **Unplug/replug** the EVK and close other tabs/apps that opened it; retry.
3. **Enable `chrome://flags/#automatic-usb-detach`**, relaunch Chrome, retry.
   (Chrome only auto-detaches allowlisted drivers — `cdc_acm`/`usblp`/`ftdi_sio` —
   so this helps only if the bound driver is one of those.)
4. **If a non-allowlisted kernel driver is bound**, WebUSB cannot force-detach it
   from JavaScript. Use the **Phase-1 Termux/pyusb path** ([../docs/TERMUX_SETUP.md](../docs/TERMUX_SETUP.md)),
   which uses libusb — that *can* force-detach and claim. Same protocol, same
   captured init sequence.

## Safety / scope

The app does nothing beyond USB: no credentials, no network calls except the
same-origin `fetch()` of the firmware file. It reads exactly one frame per
Capture and always attempts to stop streaming afterward.
