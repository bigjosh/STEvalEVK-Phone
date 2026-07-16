# STATE.md — what works, what's blocked

_Updated 2026-07-15. Companion to `handoff.md`, `PROTOCOL.md`, `DECISIONS_QUEUE.md`._

## Where we are

Phase 0 (protocol discovery) is done. **Phase 1 (Termux pyusb) and Phase 2
(WebUSB) are written, reviewed, and offline-tested (12/12).** Two real captures
(warm + cold) have been fully decoded, and **the one hard blocker is RESOLVED**:
the cold capture proves the **VD56G3 streams with no firmware patch at all**
(`FWPATCH_REVISION` reads 0 throughout while ~16 MB of frames flow). The exact
hardware-proven cold-init sequence is captured to `firmware/vd56g3_cold_init.json`
and both impls now replay it verbatim. What remains is running it on the actual
Pixel (no on-device run yet) and validating a real decoded frame (both captures'
video is snaplen-truncated).

## Works / established (high confidence)

- **USB layer is native, not pure Python.** `image_sensor_python_sdk.py` is a
  `ctypes` shim over `libcx3_spider_64.so` (libusb statically linked) +
  `libst_brightsense_sdk_vdx6gx.so` (C++). → handoff open-Q #1 answered.
- **Only x86-64 Linux + x64 Windows binaries ship. No aarch64.** ST's SDK cannot
  run on the Pixel's ARM64 Termux → Phase 1 must *reimplement* the protocol, not
  port the SDK. (This changes the Phase-1 plan; see DECISIONS_QUEUE #1.)
- **Full CX3 console protocol recovered** and written up in `PROTOCOL.md`:
  ASCII request/response over a bulk endpoint pair; 20-command vocabulary;
  I²C-tunnelled register access with confirmed data endianness; `CFGWR`/`CFG2WR`
  CSI-receiver struct; frame status-line layout; RAW8/RAW10 packing.
- **VT firmware patch extracted.** `firmware/vd56g3_vt_patch.json` — 3920
  `Write8(addr,val)` pairs (addr 0xA000–0xD9F8), reproducible via
  `tools/extract_vt_patch.py`. Matches the handoff's "VT=17".
- **Register-level stream-init sequence known** from ST's `open_cv` example +
  `vdx6gx_constants.py` (mirrored minimally in `firmware/vd56g3_registers.py`).
- **Phase-1 Termux capture code written** (`evk/cx3_console.py`,
  `evk/vd56g3.py`, `evk/patch.py`, `evk/raw.py`, `evk/usb_termux.py`,
  `grab.py`): pure-pyusb console + sensor driver + frame decode + one-shot JPG
  grab. Setup in `docs/TERMUX_SETUP.md`.
- **Phase-2 WebUSB app written** (`web/`): dependency-free static app mirroring
  the same protocol byte-for-byte.
- **Adversarially verified + fixed.** A 10-agent build+verify pass surfaced 16
  findings; all applied. Notably it caught that ST's `vdx6gx_constants.py`
  shadow-defines `STREAM_STATICS_OUTPUT_CTRL` (0x0096 → **0x0335** wins under
  `import *`), so the stream-enable write goes to **0x0335** — fixed in the
  register file, both impls, and PROTOCOL.md.
- **Offline regression test passes** (`tests/test_protocol_offline.py`, 10/10;
  no hardware/pyusb needed) plus a CI workflow (`.github/workflows/ci.yml`)
  that compiles Python, runs the tests, and syntax-checks the WebUSB JS.
- **Two real captures URB-parsed** (`tools/parse_usb_capture.py`) and confirmed
  against hardware: **bulk endpoint addresses** (cmd-OUT `0x05`, answer-IN `0x85`,
  video-IN `0x83`); **reply grammar** `OK <HH…>` (fixed a read-parser bug); i²c
  `0x20`; big-endian register addresses; **register writes are `I2CWRRD` rdlen=0**
  (not `I2CWR` — corrected in both impls); the device uses **`CFG2WR`**; CX3 fw
  v1.7.1. See PROTOCOL.md §8.
- **The cold capture (`captures/cold`) yielded the full init-to-first-frame
  sequence** — 80 steps (clock/PLL, `CMD_BOOT`, ROI/exposure, `CFG2WR`,
  `CMD_STREAMING`) with **no patch** — extracted reproducibly
  (`tools/extract_cold_init.py`) to `firmware/vd56g3_cold_init.json` and replayed
  verbatim by `evk.vd56g3.replay_cold_init` / `web replayColdInit` (the default
  `cold_init` path). Streamed geometry: **1120×1360, RAW10**. See PROTOCOL.md §9.
- **Tooling in place:** ELF/PE symbol+disassembly scripts (scratchpad),
  `tools/extract_vt_patch.py`, `tools/decode_usbpcap.py` (container-agnostic
  console decoder that turns a future capture into a replay transcript).

## Blocked / open

- **✅ RESOLVED — the FW patch is not required.** The whole premise (patch each
  cold init) was wrong for basic streaming. The `captures/cold` session read
  `FWPATCH_REVISION`/`VTIMING` = 0 the entire time and still streamed. So we do
  **not** need ST's missing `Resources/…patch….bin`; the VT patch (already
  extracted) is likewise optional. The GUI patches only when explicitly enhancing
  the sensor; plain RAW capture doesn't need it. Main-patch and VT-patch code
  paths remain, gated off by default, for anyone who wants the enhanced firmware.
- **Not yet hardware-verified** (needs the device or an untruncated capture):
  running the replay on the actual Pixel; a full **real-frame decode** (both
  captures' video is snaplen-truncated at 64 KB/URB); whether `termux-usb` +
  `libusb_wrap_sys_device` clears SELinux on the Pixel 10 XL. Minor/unconfirmed:
  the `CFG2WR` v2 field semantics (bytes replayed verbatim, so it doesn't matter)
  and whether `I2CWR` also works (we use the proven `I2CWRRD` rdlen=0).
- **Highest-risk code path: `evk/usb_termux.py` fd adoption.** It reaches into
  pyusb's private libusb1 internals to reuse the Termux-provided fd; it is
  structurally correct against pyusb 1.2.x but unrun on a real Pixel. If it
  fails on-device, the fallback is the raw `libusb1`/`usb1` ctypes path noted in
  `docs/TERMUX_SETUP.md`. The rest of the stack (console/register/decode) is
  offline-tested and low-risk.

## Next actions (in order)

1. **Run `grab.py` on the Pixel** (`termux-usb -e … --fd`). This is now the
   critical path — the init sequence is hardware-proven, so the main unknowns are
   just the `usb_termux.py` fd adoption (fall back to the raw-`usb1` recipe in
   `docs/TERMUX_SETUP.md` if pyusb internals differ) and reading one frame off
   endpoint `0x83`. Iterate until a JPG lands.
2. **Validate the decoded frame.** Both captures' video is snaplen-truncated, so
   the decoder is only synthetic-tested. First real frame from the Pixel confirms
   the status-line strip + RAW10 unpack at 1120×1360; adjust width if the ROI
   differs from the captured 1120.
3. **Phase 2 is deployed** → https://bigjosh.github.io/STEvalEVK-Phone/ (via
   `.github/workflows/pages.yml`). Open in Chrome for Android and try Connect →
   Capture. The EVK splits endpoints across two interfaces (video `0x83` on if0,
   console `0x05`/`0x85` on if1 — confirmed from the device descriptor); the web
   app now claims both. WebUSB has not been run against the device yet.
4. (Optional) If the enhanced firmware is ever wanted, sniff a GUI session that
   *does* patch and reconstruct the blob — but it is not needed to capture frames.
