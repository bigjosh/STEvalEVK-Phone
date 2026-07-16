# STATE.md — what works, what's blocked

_Updated 2026-07-16. Companion to `handoff.md`, `PROTOCOL.md`, `DECISIONS_QUEUE.md`._

## Where we are

**🎉 REAL PHOTO CAPTURED (2026-07-16), on the Windows PC, entirely through our
reimplemented stack:** `python grab.py --auto --out frame.jpg -v` → clean
1120×1360 RAW10 photo, exit 0, ~3 s. Every open protocol question is now
hardware-answered. The PC run surfaced and fixed four issues that would have
burned many phone debug cycles:

1. **Command registers self-clear** — must poll back to 0 after each command
   or the next one is dropped (PROTOCOL.md §9.0; `Vd56g3.send_command`).
2. **Start/stop were swapped**: `0x0201←01` STARTS streaming, `0x0202←01`
   STOPS. The extracted replay used to end with the captured session's Stop
   click, killing the stream right after starting it (§9.0; JSON regenerated).
3. **Wire chunk framing on EP 0x83**: 16 KB chunks with 16-byte header/footer
   (§5.0) — the old decode produced garbage even on a good read; de-chunking
   is implemented in `evk/raw.py` + `termux_grab.py`.
4. **The CX3 stalls EP 0x83 on DMA overflow**: read one whole frame per bulk
   transfer with `clear_halt` between attempts (§5.0); chunked reads tear.

All of this is ported into **`termux_grab.py`** (its de-chunk/decode logic is
validated against the real captured frame). What remains is only the on-phone
plumbing: `termux-usb` fd → `libusb_wrap_sys_device` → the same calls.

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
- **Offline regression test passes** (`tests/test_protocol_offline.py`, 13/13;
  no hardware/pyusb needed — now includes the wire-chunk format and the
  corrected command handshake) plus a CI workflow (`.github/workflows/ci.yml`)
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

- **✅ RESOLVED — the FW patch is not required** (cold capture; re-confirmed
  live: the sensor streamed a real photo with `FWPATCH_REVISION` = 0).
- **✅ RESOLVED — real-frame decode validated.** A live frame (1,910,528 wire
  bytes) de-chunks to exactly 1,906,800 payload bytes and decodes to a clean
  photo; status-line metadata (FORMAT_CTRL=10, OUT_ROI_Y_SIZE=1360, live
  FRAME_COUNTER) parses correctly.
- **Windows quirk (PC path only):** libusb's libusb0 fallback backend breaks on
  the video reads — `evk/usb_termux.py` now forces the libusb-1.0 backend via
  `libusb-package`. Under the old libusb0 backend the composite device also
  enumerates as TWO devices (one per interface); `evk/cx3_console.py` handles
  both presentations (cross-interface + cross-device video discovery).
- **Still phone-only unknowns** (all plumbing, no protocol): `termux-usb`
  permission/fd handoff, `libusb_wrap_sys_device` on the Pixel 10 XL,
  SuperSpeed negotiation + VBUS over the phone's C-to-C cable. Note the
  bundled Pixel cable is USB-2-only — console will work but video will starve;
  use a genuine 5 Gbps cable.
- Minor/unconfirmed: `CFG2WR` v2 field semantics (replayed verbatim — moot);
  `0x0201←04` mode-byte meaning (replayed verbatim); whether plain `I2CWR`
  also works (we use the proven `I2CWRRD` rdlen=0).

## Next actions (in order)

1. **Try the WebUSB page on the Pixel** (user preference: no CLI on the phone).
   `web/` now carries all the hardware-validated fixes (start/stop swap,
   self-clear handshake, §5.0 de-chunking + single-transfer `readFrame`; JS
   decode is pixel-exact vs Python on a real frame). Open
   https://bigjosh.github.io/STEvalEVK-Phone/ in Chrome for Android, plug the
   EVK with a **genuine 5 Gbps C-to-C cable**, Connect → Capture. The one
   unproven layer is Chrome-Android's `claimInterface` (past EBUSY was likely
   another claim-holder, possibly the parallel Termux tooling; the device's
   interfaces are plain vendor 0xFF — not Chrome-blocked). If it fails:
   replug, reboot, revoke other apps' USB access, check `chrome://device-log`
   (see web/README.md troubleshooting).
2. If WebUSB still can't claim: **Termux path** (`termux_grab.py`, fully
   updated; docs/TERMUX_SETUP.md) — libusb can force-detach where Chrome
   can't. Requires installing F-Droid Termux + Termux:API.
3. Robust fallback: **minimal Kotlin APK built by GitHub Actions**
   (UsbManager `claimInterface(force=true)`, `bulkTransfer`; ~8 files, no
   external libs; commit a debug.keystore for a stable CI signature). The
   protocol constants port 1:1 from `termux_grab.py`.
4. (Optional) If the enhanced firmware is ever wanted, sniff a GUI session that
   *does* patch and reconstruct the blob — but it is not needed to capture frames.
