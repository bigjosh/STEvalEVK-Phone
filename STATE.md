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

## 🏆 2026-07-16 (night): GOAL ACHIEVED ON THE PHONE

**Full-res frame captured on the Pixel 10 XL via the WebUSB page — through a
USB-2 cable.** Chrome-Android claimed both interfaces (the old EBUSY did not
recur), the full init replayed over WebUSB, and "slow mode" carried the video
across the USB-2 link:

- **USB-2 slow mode (line stretch)**: the link check (bulk mps 512 = HighSpeed)
  auto-enables it. It injects `LINE_LENGTH (0x0300)` = 1236×N just before
  `CMD_START_STREAM`, dividing the wire rate and fps by N with **zero clock
  changes**. **4× works on hardware** (~29 MB/s, ~15 fps, full 1120×1360).
  **12× faults the sensor** (SYSTEM_FSM reads 0xFF = I2C NAK; internal limit
  somewhere in (4944, 14832] line-clocks; 6× untested). Do NOT retune the PLL
  or `VT_CLK_DIV` — that faults the sensor the same way (tried first).
- **Manual exposure slider** (0.5–65 ms → line periods → `COARSE_EXPOSURE`
  override): needed because the pre-queued read captures the FIRST frame,
  before auto-exposure can adapt.
- Empirically confirmed registers: `0x0300` = LINE_LENGTH (readback 1236 =
  7.69 µs/line @ 160.8 MHz VT ✓ 60 fps × 2168 lines); `0x0312` reads 1010 =
  MIPI Mbps.

## 2026-07-16 (late): exposure control solved — datasheet + bench validated

User dropped the datasheets into `datasheets/` (UM2602 Rev 8 is the register
bible) and put the board back on the PC — which enumerated at **HighSpeed**,
i.e. phone-identical conditions, making the bench a perfect phone proxy. Full
matrix validated on hardware (see PROTOCOL.md §10 for the distilled spec):

- **Manual exposure**: `EXP_MODE (0x044C)=2` + `MANUAL_COARSE_EXPOSURE
  (0x044E)` via ST's `GROUP_PARAM_HOLD (0x0448)` latch. Brightness ladder
  120/480/1900 lines → mean 111/248/723. **Live-tunable while streaming.**
  Over-max clips safely. Applied values readable at 0x0064/0x0072.
- **Auto exposure**: the init's `0x044C←0` turns AEC on; it owns
  exposure+gains (why bare 0x044E writes did nothing). Warm-up capture (read
  ~15 frames, keep last) converges: 15/15 frames, 0 stalls, 1.02 s over HS 4×.
- **Phone "hang after capture" root cause (app-side)**: WebUSB transfers
  can't be cancelled; retries after a timeout stacked abandoned 2 MB
  transferIns and wedged the endpoint. Fixed: at most ONE video transfer in
  flight, reused across attempts. Plus ?v= cache-busting (stale Pages HTML).
- Phone 12× "FSM=255" is the **documented ERROR state** (UM2602 §9), not a
  dead sensor — `ERROR_CODE (0x001C)` names the reason and the app now reads
  it automatically. Bench 6×–12× init clean; clock-tree changes (VT_CLK_DIV)
  are the thing that genuinely kills it (pixel clock is pinned to 160.8 MHz).
- Mystery closed: the captured `0x0201←04` writes are THSENS_READ (thermal).

Web app: **build 2026-07-16i** deployed (GPH exposure latch, applied-mode
readback, ERROR_CODE diagnostics, single-outstanding-transfer fix).

## Next actions (optional polish)

1. Add analog/digital gain sliders if wanted: `0x044D` (32/(32−code), ×1–×8,
   raise `MAX_AG_CODED 0x0960` in standby for >×4) and `0x0450` (FP5.8) —
   documented + register file updated, not yet in the UI.
2. Tune exposure defaults / test 6× slow mode; a genuine 5 Gbps C-to-C cable
   enables full-rate (slow mode Off). PC's current port/cable is HS-only too.
3. Termux path (`termux_grab.py`) and the CI-built Kotlin APK remain as
   researched fallbacks — not needed now that WebUSB works end-to-end.
4. (Optional) If the enhanced firmware is ever wanted, sniff a GUI session that
   *does* patch and reconstruct the blob — but it is not needed to capture frames.
