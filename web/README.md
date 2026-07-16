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

## Deploy on GitHub Pages

1. Push this repo to GitHub.
2. Repo **Settings → Pages** → *Build and deployment* → **Deploy from a branch**,
   pick your branch and `/ (root)`.
3. The app is served at `https://<user>.github.io/<repo>/web/`. Open that URL in
   **Chrome for Android**.
4. Tap **Connect**, choose the EVK in the chooser (filtered to VID `0x0553`,
   PID `0x040A`), then **Capture frame**.

> GitHub Pages is HTTPS by default, which satisfies WebUSB's secure-context
> requirement. No server code runs — it is entirely static.

### VT patch path (fetch)

`protocol.js` fetches the VT patch from **`../firmware/vd56g3_vt_patch.json`**
(relative to `web/`), i.e. the repo's `firmware/` folder — so keep the whole repo
published, or **copy `firmware/vd56g3_vt_patch.json` next to the web app** and
change the URL passed to `loadVtPatch(sensor, url)` in `app.js`. Same-origin
fetch under Pages needs no CORS config.

## How it initializes (no firmware patch needed)

`replayColdInit(sensor, url)` in `protocol.js` plays the **hardware-captured**
cold-init sequence from `../firmware/vd56g3_cold_init.json` (PROTOCOL.md §9): the
exact ordered commands ST's GUI sent to a cold, **unpatched** VD56G3 that then
streamed — register writes (`I2CWRRD` rdlen=0), `CLKWR`/`CFG2WR`/`NRST`/`IOSET`/
`IOCFGWR`, ending at `CMD_STREAMING`. A cold capture proved the sensor streams
with **no FW patch** (`FWPATCH_REVISION` = 0), so there's nothing to download. It
streams **RAW10 at 1120×1360**. The optional `loadMainPatch`/`loadVtPatch` helpers
remain for the *enhanced* firmware but are not called by default.

## Configurable knobs

- **Endpoints** — auto-discovered (command-OUT `0x05` ↔ answer-IN `0x85` by number,
  video-IN `0x83`), matching the captured addresses; override via the three number
  inputs (blank = auto).
- **bpp** — the replay uses the captured format (RAW10); the dropdown affects only
  the (unused-by-default) synthetic path.
- **CSI config** — sent as the captured `CFG2WR` bytes (`CFG2WR_CAPTURED`),
  replayed verbatim.

## Verified against real hardware (PROTOCOL.md §8–9)

Endpoint addresses, reply grammar (`OK <HH…>`), the `I2CWRRD`-rdlen0 write
encoding, `CFG2WR`, and the full init sequence are all confirmed from USBPcap
captures. Remaining unknowns are on-device only: running it in Chrome on the
Pixel, and decoding a real frame (the captures' video is snaplen-truncated).

## Safety / scope

The app does nothing beyond USB: no credentials, no network calls except the
same-origin `fetch()` of the two firmware files. It reads exactly one frame per
Capture and always attempts to stop streaming afterward.
