# PROTOCOL.md — STEVAL-EVK-U0I (CX3 "spider" + VD56G3) host↔device protocol

Derived by static analysis of ST's **STSW-IMG507 v2.1.0** SDK binaries
(`libcx3_spider_64.so`, `libst_brightsense_sdk_vdx6gx.so`, and the matching
Windows DLLs) plus the shipped Python examples. Everything marked
**[binary-confirmed]** was read out of the compiled code; everything marked
**[needs capture]** should be verified against a USBPcap trace before it is
trusted for the phone replay (see `DECISIONS_QUEUE.md`).

> TL;DR of the reverse-engineering: the EVK is **not** UVC and its host SDK is
> **not** pure Python. The USB layer is a native C library (libusb statically
> linked) driven over a **text command console on a bulk endpoint pair**. The
> sensor is driven entirely by tunnelling I²C register writes through that
> console. Reimplementing the host in pure Python (Termux) or JS (WebUSB) is
> straightforward *except* for one blocker: the main VD56G3 firmware patch is
> loaded from a `Resources/…bin` file ST did not ship. The **VT patch is
> embedded and has been extracted** (`firmware/vd56g3_vt_patch.json`).

---

## 0. Answers to the handoff's open questions

1. **Is STSW-IMG507's USB layer pure Python?** — **No. [binary-confirmed]**
   `image_sensor_python_sdk.py` is a thin `ctypes` wrapper. All USB work is in
   two native libraries:
   - `libcx3_spider_64.so` — the CX3 bridge driver. **libusb-1.0 is statically
     linked into it** (its `.dynsym` re-exports the whole `libusb_*` API and its
     `.rodata` contains the libusb + Linux-usbfs backend strings). It exposes a
     flat `cx3_*` C API.
   - `libst_brightsense_sdk_vdx6gx.so` — a 3.6 MB C++ sensor/board abstraction
     (`dvc::S6G3`, `evk::EvalKit`, `Comms*`, `Config*`, `Capture*`) that calls
     into the CX3 driver and exposes the flat C API the Python wrapper binds
     (`init_board`, `Write8/16/32`, `WriteBurst`, `configureCsiReceiver`,
     `start_stream`, `get_raw_frame`, …).
   - Only **x86-64 Linux `.so`** and **x64 Windows `.dll`** builds ship. **There
     is no aarch64 build**, so ST's binaries cannot run on the Pixel's ARM64
     Termux. Consequence: Phase 1 is a *reimplementation* of this protocol, not
     a port. That is fine — the protocol below is small.

2. **What does the CX3 need vs. what is sensor I²C tunnelled through it?** — The
   CX3 firmware (`cx3-spider`) speaks an ASCII command console (§2–3). Sensor
   register access is **I²C tunnelled** via the `I2CWR` / `I2CRD` / `I2CWRRD`
   commands (§3). CX3-local operations (CSI-2 receiver config, GPIO, sensor
   nRESET line, external clock, streaming) are their own commands. So a sensor
   register write is *host → `I2CWR` frame → CX3 → I²C bus → sensor*, and video
   is *sensor → MIPI CSI-2 → CX3 → bulk-IN → host*.

3. **Bulk stream framing** — Each delivered frame is the raw CSI-2 payload with
   **2 status lines prepended** (`OIF_ISL` info + the register snapshot the
   decoder reads back). Image rows follow, packed RAW8 (1 byte/px) or RAW10
   (ST's 5-byte-per-4-px MIPI packing). No per-line USB header. §5.

4. **termux-usb / libusb_wrap_sys_device** — the CX3 driver already uses
   `libusb_wrap_sys_device` internally **[binary-confirmed]** (it's exported and
   called), which is exactly the fd-adoption path Termux needs. We won't use
   ST's `.so`, but it confirms libusb-over-Android-fd is the intended shape.
   Whether SELinux on the Pixel 10 XL permits it is **[needs device]**.

5. **WebUSB claiming interfaces** — the console and the video stream live on
   bulk endpoints of the vendor interface(s); no kernel class driver binds them
   (libusbK on Windows, vendor class on Linux). Exact interface/endpoint numbers
   are **[needs capture]** (dump `lsusb -v` / descriptors on-device).

---

## 1. USB device identity & topology

| Item | Value | Source |
|---|---|---|
| VID:PID | `0x0553:0x040A` | handoff / observation |
| Manufacturer bridge | Cypress CX3 (MIPI-CSI2 → USB3), firmware string `cx3-spider` | handoff |
| Speed | USB 3.x SuperSpeed (5 Gbps cable **required** or the video bulk stream starves) | handoff |
| Class | Vendor-specific (libusbK / vendor, **not UVC**) | handoff + binary |
| Sensor | VD56G3 ("S6G3"), 1124×1364 mono global shutter, RAW8/RAW10 | handoff + binary |
| Sensor I²C addr | `0x20` (8-bit write addr; 7-bit `0x10`) — default in `image_sensor_python_sdk.py` | Python SDK |

**[capture-confirmed] from the EVK's own device+config descriptors** (device addr
49, VID 0x0553, bcdUSB **3.00**, bDeviceClass **0xEF** = Misc/IAD; 1 configuration,
**2 vendor-specific (0xFF) interfaces**). The three bulk endpoints are **split
across the two interfaces**:

| role | address | interface | type/mps |
|---|---|---|---|
| **video IN** (streaming payload)  | **`0x83`** | **interface 0** (1 EP) | bulk, 1024 |
| **command OUT** (console request) | **`0x05`** | **interface 1** (2 EP) | bulk, 1024 |
| **answer IN** (console reply)     | **`0x85`** | **interface 1**        | bulk, 1024 |

So a host must **claim both interfaces**: console (0x05/0x85) on interface 1,
video (0x83) on interface 0. The Python host does this (console interface +
cross-interface video discovery); the WebUSB app claims every interface the three
endpoints live on. Overrides remain available. (The earlier "firehose" endpoint
tallies in §8 were before filtering by device address — this table is the EVK's
real descriptor.)

---

## 2. The CX3 command console (transport)

The console is **request/response ASCII over bulk**. The driver's
`cx3_write_request` / `cx3_read_answer` **[binary-confirmed]** do exactly:

```
cx3_write_request:  libusb_bulk_transfer(handle, ep_cmd_OUT=struct[0x24],
                                          buf=struct[0x50], len=struct[0x4c],
                                          &transferred, timeout=150ms)
cx3_read_answer:    libusb_bulk_transfer(handle, ep_ans_IN =struct[0x25],
                                          buf=struct[0x1058], cap=struct[0x1050],
                                          &transferred, timeout=150ms)
                    // reply is NUL-terminated by the driver: buf[transferred]=0
```

Retry/recovery `[binary-confirmed]` in `cx3_query`: on failure it issues
`libusb_clear_halt` on the answer-IN endpoint and retries once.

### Request wire format (built by `cx3_query`) [binary-confirmed]

```
<KEYWORD> <HH> <HH> <HH> ... \r\n\0
```

- `KEYWORD` = ASCII command mnemonic (table in §3), looked up from an internal
  20-entry table (`keywords`, 20-byte stride, `{char name[16]; u32 index}`).
- Each argument **byte** is emitted as a space followed by **two uppercase hex
  digits** (`" %02X"`), MSB nibble first.
- Terminated with CRLF then NUL. (The `\0` is not sent on the wire — it bounds
  the buffer; `len` = struct[0x4c] is set to the strlen including CRLF.)

So `I2CWR` of bytes `20 0A 3C 05` is literally the ASCII string
`"I2CWR 20 0A 3C 05\r\n"`.

### Response wire format — **[capture-confirmed]** (§8)

ASCII: `OK[ <HH>]*\r\n` — the literal status token `OK`, then one space-separated
uppercase-hex byte per returned data byte, then CRLF. A pure ack (writes, `IOSET`,
`NRST`) is just `OK\r\n`; a read returns `OK <HH …>` with exactly `rdlen` data
bytes (reassemble LSB-first). Observed examples: `VERSION` → `OK 01 07 01`
(CX3 fw v1.7.1), `ID` → `OK 0B 14 00 00 27 00 00 00`, `IOGET 15` → `OK 01`,
a 32-bit read → `OK HH HH HH HH`. The host-side return-code convention in the
driver is `0` ok / `6` write-IO / `7` short-read / `3` bad-arg.

`cx3_query` C signature (recovered): **[binary-confirmed]**
```c
int cx3_query(board_t* b, int cmd_index,
              uint16_t in_len,  const uint8_t* in_buf,
              uint16_t out_len,       uint8_t* out_buf);
```

---

## 3. Command vocabulary [binary-confirmed]

From the `keywords` table (indices are 1-based, in table order):

| # | Keyword    | Meaning                          | Argument bytes (host→dev) |
|---|------------|----------------------------------|---------------------------|
| 1 | `ID`       | read board id                    | none |
| 2 | `VERSION`  | read CX3 firmware version        | none |
| 3 | `I2CWR`    | I²C write                        | `[i2c_addr, data…]` |
| 4 | `I2CRD`    | I²C read                         | `[i2c_addr]`, reply = N bytes |
| 5 | `IOSET`    | GPIO set level                   | `[gpio_id, state]` |
| 6 | `IOGET`    | GPIO read level                  | `[gpio_id]` |
| 7 | `SPIWRRD`  | SPI write-then-read (CX3 flash)  | `[…]` |
| 8 | `NRST`     | drive sensor nRESET line         | `[assert(0/1)]` |
| 9 | `CFGWR`    | configure CSI-2 receiver (v1)    | 12-byte struct (§4) |
| 10| `CFGRD`    | read CSI-2 receiver config       | — |
| 11| `CLKWR`    | set sensor external clock        | 3 bytes `[clk…]` |
| 12| `CLKRD`    | read external clock              | — |
| 13| `I2CWRRD`  | I²C write-then-read (repeated start) | `[rdlen_hi, rdlen_lo, i2c_addr, write…]`, reply = rdlen bytes |
| 14| `IOCFGWR`  | GPIO direction/config write      | `[gpio_id, cfg]` |
| 15| `IOCFGRD`  | GPIO direction/config read       | `[gpio_id]` |
| 16| `LOGLVLWR` | set CX3 log level                | `[level]` |
| 17| `LOGLVLRD` | read CX3 log level               | — |
| 18| `CFG2WR`   | configure CSI-2 receiver (v2)    | 12-byte struct (§4) |
| 19| `CFG2RD`   | read CSI-2 receiver config (v2)  | — |
| 20| `RESET`    | reset the CX3 bridge             | none |

### 3.1 Register access mapping [binary-confirmed]

The sensor library builds register I/O on top of the I²C commands:

- **Write reg** — **[capture-confirmed]** the GUI writes every register via
  `I2CWRRD` with **read-length 0** (write the payload, read nothing back; reply is
  a bare `OK`):
  `I2CWRRD [rdlen_hi=0, rdlen_lo=0, i2c_addr, addr_hi, addr_lo, value…]`
  - Register **address is 16-bit big-endian** (`addr_hi, addr_lo`).
  - Register **value is little-endian (LSB first)** — e.g. cold-init wrote
    `EXT_CLOCK 0x0220 <- 00 1B B7 00` = `0x00B71B00` = 12 000 000 (12 MHz).
  - `I2CWR` (index 3) is the binary-documented write command but was **never**
    seen on the wire in either capture — the proven path is `I2CWRRD` rdlen=0, so
    that is what the code uses.
- **Read reg** `Comms::ReadN(addr)` →
  `I2CWRRD [rdlen_hi, rdlen_lo, i2c_addr, addr_hi, addr_lo]`, `rdlen=N`; reply =
  N bytes reassembled LE. **[capture-confirmed]** — exact framing matches 745
  real reads (§8).
- **Burst write** `WriteBurst(addr, data, n)` → chunked `I2CWR` of
  `[i2c_addr, addr_hi, addr_lo, data…]` (burst size default 256 in the Python
  wrapper).

I²C 7/8-bit note: the SDK passes `0x20`. The CX3 firmware sets the R/W bit itself
(`I2CWR` vs `I2CRD`/`I2CWRRD`), so pass the **8-bit base `0x20`** as the address
byte. **[capture-confirmed]** — every one of the 745 `I2CWRRD` reads in the warm
capture used i2c address `0x20` (§8).

---

## 4. CSI-2 receiver configuration (`CFGWR` / `CFG2WR`)

> **[capture-confirmed] This device uses `CFG2WR` (the v2 command), NOT `CFGWR`.**
> The warm capture (§8) shows six `CFG2WR` frames and zero `CFGWR`. The exact
> 12-byte payloads observed (replay these verbatim — stored in
> `firmware/vd56g3_csi_cfg2wr.json` / `reg.CFG2WR_CAPTURED`):
>
> | bpp | CFG2WR payload (12 bytes) |
> |----|----|
> | 8  | `26 02 04 BB 8C 40 04 00 04 00 08 64` |
> | 10 | `26 02 05 EE 3F E0 04 00 04 00 0A 64` |
>
> Partial field decode: `byte[1]=lanes` (2), `byte[10]=bits_per_pixel`,
> `byte[2..5]=` a bpp-scaled timing/clock word (u32 BE); `byte[0]=0x26`,
> `byte[6..9]=0x04000400`, `byte[11]=0x64` are constant across the samples.
> Note this layout does **not** match the derived `CFGWR` struct below — CFG2WR
> is its own format. The code sends the captured bytes verbatim.

The struct below is the **`CFGWR` (v1)** layout recovered from the binary, kept
for reference / non-captured parameters. `cx3_comm_cfg_write` packs a 12-byte
payload from a config struct. Newer CX3
firmware advertises v2 and the driver then sends `CFG2WR` (index 0x12) instead of
`CFGWR`; `cx3_comm_check_version` decides. Both carry the same 12 bytes plus, for
v2, a derived timing word.

**12-byte `CFGWR` payload (byte offsets):**

| off | field | width/endian | notes |
|----|-------|--------------|-------|
| 0  | `lane_number`     | u8            | MIPI data lanes (2 for our mode) |
| 1  | field B           | u8            | virtual-channel / format selector |
| 2–5| `data_rate_mbps`  | u32 **big-endian** | per-lane Mbps (1500 for our mode) |
| 6–7| `width`           | u16 **big-endian** | pixels (1116) |
| 8–9| `height`          | u16 **big-endian** | lines incl. status? (1356) |
| 10 | `bit_per_pixel`   | u8            | 8 or 10 |
| 11 | field            | u8            | reserved/format |

The Python `reconfigure_csi_receiver(lane, data_rate, width, height, bpp,
pixel_clock)` maps onto this; `pixel_clock` is only consumed by the **v2**
(`CFG2WR`) path, where the driver computes a timing value
`round(2*bpp*data_rate*1e6 / (…pixel_clock…)) - 1000000` and appends it. Treat the
exact `CFG2WR` math as **[needs capture]** — capture the 12(+n) bytes the GUI
sends and replay verbatim.

**Known-good parameters** (from `vdx6gx_example_open_cv.py`):
`lanes=2, data_rate=1500 Mbps, width=1116, height=1356, bpp=10,
pixel_clock=160_800_000 Hz`.

---

## 5. Frame format on the video bulk-IN [binary-confirmed from decoder]

### 5.0 On-wire chunk framing ✅ [hardware-confirmed 2026-07-16]

Everything below §5.0 describes ST's **post-driver payload** — but the CX3
delivers each frame on EP `0x83` wrapped in a chunk framing that ST's native
driver strips before its decoder runs. Recovered by de-chunking a live frame
to a clean image (the framing was invisible in the USBPcap captures because
their video payloads were snaplen-truncated at 64 KB):

- The frame arrives as a train of **16384-byte chunks** (final chunk short):

  ```
  [16-byte header][payload_len bytes payload][16-byte footer]
  ```

  - header bytes 0-3: magic `10 01 02 00`
  - header bytes 4-5: u16 **chunk index within the frame, 1-based**
  - header bytes 6-7: u16 (counter high word; not needed for decode)
  - header bytes 8-11: u32 **frame sequence**, little-endian
  - header bytes 12-15: u32 **payload length** (`0x3FE0` = 16352 for full
    chunks; the actual remainder for the final chunk)
  - the 16-byte footer is absent on the final short chunk.

- Full chunks carry 16 + 16352 + 16 = 16384 bytes. For 1120×1360 RAW10 the
  post-driver payload is (2+1360)·1400 = **1,906,800 B** → 117 chunks →
  **1,910,528 B on the wire** — exactly the per-URB completion size in both
  captures. General formula: `wire = payload + 16·chunks + 16·(chunks-1)`,
  `chunks = ceil(payload / 16352)`.
- Concatenating the chunk payloads yields the §5 payload (2 status lines +
  image rows). Implementations: `evk/raw.py strip_wire_chunks`,
  `termux_grab.py strip_wire_chunks`.
- **Reading strategy (hardware-confirmed):** the CX3 **stalls EP 0x83 when
  its DMA overflows**, which happens within milliseconds whenever the host
  pauses mid-frame (the stream runs ~115 MB/s) or isn't reading at all.
  `clear_halt` makes it resume cleanly at the next frame boundary. Chunked
  host reads therefore tear frames; the reliable pattern is
  `clear_halt(0x83)` → **one bulk transfer sized ≥ the whole wire frame**
  (the end-of-frame short packet terminates it at exactly the wire size),
  retrying on a pipe error. ST's driver likewise submits frame-sized URBs.

From `vdx6gx_frame_decoding.py` + `image_sensor_python_sdk.py` (the
**post-driver payload**, i.e. after §5.0 de-chunking):

- The driver returns a contiguous payload (`get_raw_frame` gives an offset+size
  into a capture buffer sized `1024 + 2×max_frame_size`).
- Layout: **2 status lines**, then `y_size` image rows.
  - `x_size_in_bytes = bpp * width / 8`.
  - Image starts at byte offset `2 * x_size_in_bytes`.
- **Status lines** encode sensor registers: register `R` (`R < 0x7d`) is at byte
  `2*R + 6` in line 1; `R ≥ 0x7d` is at `frame_width_bytes + 2*(R-0x7d) + 6` in
  line 2. Multi-byte values are LSB-first. The decoder reads:
  - `FORMAT_CTRL` (0x5B) → bits/pixel (must be 8 or 10),
  - `OUT_ROI_Y_SIZE` (0x94) → `y_size`,
  - `FRAME_COUNTER` (0x50), `CURRENT_CONTEXT` (0x56).
- **CX3 4-byte packing constraint**: `width * bpp` must be a multiple of 32
  (`gcd(bpp,32)` check). For RAW8 width must be ×4; for RAW10 width must be ×16.
- **RAW10 unpacking** (`decode_raw_10`): 5 bytes → 4 pixels; bytes 0-3 are the
  high 8 bits of px0-3, byte 4 holds the 4 pairs of low 2 bits
  (`px_i = (byte_i<<2) | ((byte4>>(2*i)) & 3)`).
- **RAW8**: 1 byte per pixel, no unpacking. **Use RAW8 first on the phone.**

Full-res example uses `x:[2..1105] y:[2..1361]` → **width 1104, height 1360**
active (the example's CFG width 1116/height 1356 includes ROI + status-line
budget; reconcile exact numbers against a capture).

---

## 6. Cold-init → first-frame sequence (assembled, superseded by §9)

> **Read §9 first.** The cold capture (§9) gives the *actual* ordered sequence ST
> sends, and it proves **no firmware patch is needed to stream** (`FWPATCH_REVISION`
> reads 0 throughout). The assembled/derived steps below (from the binary + ST
> example) are kept for reference; where they disagree with §9, **§9 wins**. In
> particular, steps 6–7 (main + VT patch) are **optional** — the sensor streams
> unpatched.

This is a sequence the phone host *could* perform. Steps whose **exact bytes** are
locked are marked.

1. **Enumerate & claim** the vendor interface(s); locate the 3 bulk endpoints. *[needs-capture: EP addresses]*
2. **`VERSION`** — sanity check the console is answering. *[transport confirmed; reply grammar needs-capture]*
3. **External clock**: `CLKWR` to set the sensor input clock. *[needs-capture for exact bytes]*
4. **Sensor reset**: `NRST` assert→deassert to boot the sensor ROM. *[needs-capture]*
5. **Wait for boot**: poll `SYSTEM_FSM` (reg `0x0028`) via `I2CWRRD` until
   "ready to boot". *[register from constants]*
6. **Main FW patch** (`FW=5.0`): `S6G3::loadPatch()` streams
   `Resources/S6H…G3_patch….bin` to the sensor patch RAM (base region `0x2000`)
   then issues `CMD_BOOT` (`0x0200`). **⛔ This file is NOT in STSW-IMG507.**
   Options in `DECISIONS_QUEUE.md`. *[blocker]*
7. **VT patch** (`VT=17`): enter patch mode `Write8(0x0203,1)`, replay the
   **3920** `Write8(addr,val)` pairs (`addr` 0xA000–0xD9F8), exit
   `Write8(0x0203,2)`. **✅ Extracted → `firmware/vd56g3_vt_patch.json`.** *[binary-confirmed + extracted]*
8. **Stream/format registers** (from the example) *[binary-confirmed via constants]*:
   ```
   Write8 (0x0474 CONTEXTS_READOUT_CTRL, 0)
   Write16(0x030A STATICS_FORMAT_CTRL,  bpp)        # 8 or 10
   Write16(0x045E CTX0 OUT_ROI_X_START, 2)
   Write16(0x0460 CTX0 OUT_ROI_X_END,   1105)
   Write16(0x0462 CTX0 OUT_ROI_Y_START, 2)
   Write16(0x0464 CTX0 OUT_ROI_Y_END,   1361)
   Write16(0x0335 STREAM_STATICS_OUTPUT_CTRL, 1)
   ```
   > ⚠ `VDx6Gx_REG_STREAM_STATICS_OUTPUT_CTRL` is defined **twice** in ST's
   > `vdx6gx_constants.py` (0x0096 at line 13, **0x0335** at line 212). Because
   > the example does `from … import *`, the later binding wins and the write
   > actually lands on **0x0335** (the writable OUTPUT_CTRL). 0x0096 is the
   > read-only `STATUS_OUTPUT_CTRL` mirror — using it would poke a read-only
   > address and never enable stream output.
9. **Configure CX3 CSI receiver**: `CFGWR`/`CFG2WR` with the §4 params
   (2 lanes, 1500 Mbps, 1116×1356, bpp, 160.8 MHz). *[struct confirmed; v2 timing needs-capture]*
10. **Start streaming**: sensor `CMD_STREAMING` (`0x0202`←1) and start the CX3
    async bulk-IN transfers. *[needs-capture for exact ordering]*
11. **Read one frame** off the video bulk-IN, strip the 2 status lines, unpack
    (RAW8 = passthrough), save JPG.
12. **Stop**: `CMD_STREAMING`←0, stop transfers, release.

> The register addresses in steps 5/7/8 come from `vdx6gx_constants.py` (checked
> into the SDK) — see `firmware/vd56g3_registers.py` for a trimmed copy.

---

## 7. What is verified vs. what still needs a capture

**Verified from binaries (safe to build on):** console transport & framing;
full command vocabulary; I²C/register tunnelling and data endianness; VT-patch
contents (extracted); frame status-line layout; RAW8/RAW10 packing; the fact that
only x86-64/x64 binaries ship and that `init_board` does *not* re-patch.

**Now capture-confirmed (§8, `captures/steval-connect`):** the three bulk
**endpoint addresses** (cmd-OUT `0x05`, answer-IN `0x85`, video-IN `0x83`); the
**reply grammar** (`OK <HH…>`); i²c address `0x20`; register **address
big-endian**; `I2CWRRD` read framing; `CFG2WR` is the CSI command actually used
(exact bytes captured); `NRST`/`CLKWR`/`IOSET`/`IOCFG*` formats; `VERSION`/`ID`
take no args; CX3 firmware **v1.7.1**.

**Now cold-capture-confirmed (§9, `captures/cold`):** the **full init-to-first-
frame register-write sequence**; that **no FW patch is needed** to stream
(`FWPATCH_REVISION` = 0 throughout); that register **writes use `I2CWRRD` rdlen=0**;
the clock/PLL, ROI/exposure, and `CMD_BOOT`/`CMD_STREAMING` values.

**Still open (needs the device or an untruncated capture):** running the replay on
the Pixel; a **full real-frame decode** (both captures' video is snaplen-truncated
at 64 KB/URB); the `termux-usb`+`libusb_wrap_sys_device` SELinux question. The
`CFG2WR` v2 field math is moot (bytes replayed verbatim).

---

## 8. Capture evidence — `captures/steval-connect` (warm sensor)

A 35 MB USBPcap capture (DLT 249) of the EVK was parsed at the URB level with
`tools/parse_usb_capture.py` (and byte-scanned with `tools/decode_usbpcap.py`).
It is a **reconnect to an already-configured, already-streaming** sensor
(unpatched, like §9), so it contains **no register writes** — but it validates a
lot of the above against real hardware:

- **Bulk endpoints:** command-OUT `0x05` ↔ answer-IN `0x85` (paired 1700/1700
  URBs = 850 commands × submit+complete), video-IN `0x83` (~20.5 MB of frames).
  (The capture's ISO `0x01` / INTR `0x81` endpoints are *other* devices on the
  bus — it's a whole-bus firehose.)
- **Reply grammar** `OK <HH…>\r\n` (see §2): `VERSION` → `OK 01 07 01`,
  `ID` → `OK 0B 14 00 00 27 00 00 00`, reads → `OK` + rdlen hex bytes, acks → `OK`.

- **850 console commands**, histogram:
  `I2CWRRD ×745, IOCFGRD ×32, IOGET ×32, IOSET ×13, IOCFGWR ×13, CFG2WR ×6,
  NRST ×5, ID/VERSION/CLKWR/CLKRD ×1`. **Zero `I2CWR`** and **zero `CFGWR`**.
- All 745 `I2CWRRD` parse cleanly as `[rdlen_hi, rdlen_lo, 0x20, reg_hi, reg_lo]`
  → confirms read framing, i²c `0x20`, and big-endian register addresses.
  Registers read include `0x0000` (MODEL_ID), `0x0028` (SYSTEM_FSM), `0x001E`
  (FWPATCH_REV) and `0x0020` (VTIMING_RD_REV) — the GUI is *checking* the patch
  revisions, consistent with a warm/patched sensor.
- `CFG2WR` payloads captured (see §4). `CLKWR` = `02 00 00`; `NRST` toggles
  `01`/`00`; `IOSET` = `[gpio, state]` (e.g. `2C 00`/`2C 01`).
- The bulk-IN video payload (raw 8/10-bit pixels) dominates the file, i.e. the
  stream was live — but the *setup* writes that started it are not in-frame.

---

## 9. Cold-init sequence — `captures/cold` (the real init-to-first-frame)

A 25 MB USBPcap of a **cold** GUI launch (unplug → capture → GUI). Same channels
as §8 (cmd-OUT `0x05`, answer-IN `0x85`, video-IN `0x83`, ~16.5 MB of frames).

> ### ⚠ 9.0 Command semantics — ✅ hardware-confirmed 2026-07-16
>
> Driving the live sensor exposed two things the write-only extraction missed
> (each cost a silent no-video failure until found):
>
> 1. **Command registers self-clear.** `0x0200`/`0x0201`/`0x0202` read back 0
>    once the sensor has consumed the command. The GUI polls the register
>    after every command write (e.g. `0x0201`←01 cleared after ~2 polls
>    ≈ 25 ms); **issuing the next command while the previous is nonzero gets
>    it silently dropped.** Implementations must write → poll-to-0
>    (`Vd56g3.send_command` / `termux_grab.Console.send_command`).
> 2. **ST's constant names have start/stop swapped.**
>    `0x0201 ← 01` **STARTS** streaming (`SYSTEM_FSM` 2→3);
>    `0x0202 ← 01` **STOPS** it (3→2). In the capture, `0x0201`←01 precedes
>    all 242 streamed frames and the final `0x0202`←01 is the GUI's **Stop
>    click at session end** — early extractions replayed it as the "last init
>    step", stopping the stream immediately after starting it. The replay now
>    ends at `0x0201`←01. (`0x0201 ← 04` also appears around init/stop; mode
>    byte semantics unknown, replayed verbatim.)
>
> `SYSTEM_FSM` (0x0028): 1 = ready-to-boot, 2 = standby, 3 = streaming.

The decisive results:

- **No firmware patch.** Reads of `FWPATCH_REVISION` (0x1E) and `VTIMING_RD_REV`
  (0x20) return **0 throughout**, yet the sensor streams. **The VD56G3 streams
  unpatched** — the "patch each cold init" premise was wrong for plain RAW capture.
  (`MODEL_ID` reads `0x5603`; `SYSTEM_FSM` walks 1 → 2 → 3.)
- **Register writes = `I2CWRRD` with rdlen 0** (write payload, read nothing;
  reply `OK`). There is no `I2CWR` in the capture. Reads and writes share the
  `I2CWRRD` command, distinguished by `rdlen` (>0 read, 0 write).
- The **essential ordered sequence** (reads/polls elided), extracted verbatim to
  `firmware/vd56g3_cold_init.json` by `tools/extract_cold_init.py` and replayed by
  `evk.vd56g3.replay_cold_init` / web `replayColdInit`:

  1. `ID`, `VERSION`.
  2. `IOSET 2C 00/01` ×5 (GPIO strobe), GPIO-config probes, `CLKWR 02 00 00`.
  3. `CFG2WR` (bpp8 then bpp10), `NRST 01`, `IOCFGWR 11..39` (GPIO dirs),
     `IOSET 18 00 / 2C 01 / 2D 00`, `NRST 00/01` ×2 (sensor reset pulse).
  4. **`CMD_BOOT (0x0200) <- 01`**.
  5. Analog/clock: `0x0960<-1C`, `0x096A<-3C00`, **`EXT_CLOCK 0x0220 <- 12 MHz`**,
     `CLK_PLL_PREDIV 0x0224<-02`, `CLK_SYS_PLL_MULT 0x0226<-86`,
     `CLK_PLL_POSTDIV 0x0225<-01`, `VT_CLK_DIV 0x0227<-05`.
  6. `0x0201<-04` (mode byte, see §9.0); `ORIENTATION 0x0302<-02`; `CTX0_EXP_MODE 0x044C<-00`.
  7. ROI/exposure (context 0): `Y_START 0x045A=0`, `Y_END 0x045C=1359`,
     `OUT_ROI_Y 0x0462=0 / 0x0464=1359`, `AE_ROI_V 0x0434=0 / 0x0438=1359`,
     `OUT_ROI_X 0x045E=2 / 0x0460=1121`, `FRAME_LENGTH 0x0458=2168`,
     `COARSE_EXPOSURE 0x044E=1000` (repeated a few times).
  8. `CFG2WR` (bpp10) again; **`CMD_START_STREAM 0x0201<-01`** → streaming
     (FSM=3; all 242 captured frames follow). The capture's later
     `0x0202<-01` is the session's **Stop** (§9.0) — excluded from the replay.

- **Streamed geometry:** OUT_ROI X`[2..1121]` → **width 1120**, Y`[0..1359]` →
  **height 1360**, **RAW10** (final `CFG2WR` bpp = 10). Note the format register
  `0x030A` is *not* written — format comes from `CFG2WR`. (Video is snaplen-
  truncated at 64 KB/URB, so a full real-frame decode still needs on-device data.)

---

## 10. Sensor control — UM2602-verified + hardware-validated (2026-07-16)

With ST's UM2602 Rev 8 in `datasheets/` the guesswork is over. Everything below
is documented AND was verified against the live board (bench scripts in the
session scratchpad; results in STATE.md).

### 10.1 Commands (0x0200–0x0203) and states

Write the value, then **poll the same register until it reads 0** (= CMD_ACK).
A command issued before the previous ACK is IGNORED (§9.0 confirmed by doc).

| Reg | Values |
|---|---|
| `0x0200` BOOT | `01`=BOOT, `02`=PATCH_SETUP |
| `0x0201` SW_STANDBY | `01`=START_STREAM, `02`=NVM_READ, `03`=NVM_PROG, `04`=THSENS_READ, `05`=I2C_ADDR_UPDATE |
| `0x0202` STREAMING | `01`=STOP_STREAM, `02`=VT_FSYNC_IN_I2C |
| `0x0203` VTPATCHING | `01`=START_VTRAM_UPDATE, `02`=END_VTRAM_UPDATE |

(The captured init's `0x0201←04` writes are **thermal-sensor reads**, nothing
to do with standby/exposure.)

`SYSTEM_FSM (0x0028)`: 0=HW_STANDBY, 1=READY_TO_BOOT, 2=SW_STANDBY,
3=STREAMING, **0xFF=ERROR**. In ERROR the reason is readable at
**`ERROR_CODE (0x001C, u16)`** (e.g. 0xa00 LONG_COARSE_MAX, 0xa03
ISB_LONG_PIPE_OVERFLOW, 0xc00 CSI_LANE_DESYNC) and the device needs a reset
(our NRST pulses at init recover it).

### 10.2 Exposure (context 0) — hardware-validated

- `0x044C` **EXP_MODE**: 0=Automatic (AEC owns exposure+gains and overrides
  manual writes), 1=Freeze AE, **2=Manual**. The captured init writes 0 (AE on).
- `0x044E` **MANUAL_COARSE_EXPOSURE** (u16, line periods): min 21 lines
  (ADC10), max `FRAME_LENGTH − EXP_COARSE_INTG_MARGIN(68) − 7`; out-of-range
  values **clip safely** (bench-verified, no ERROR).
- `0x044D` **MANUAL_ANALOG_GAIN**: code 0–28, gain = 32/(32−code) → ×1..×8,
  clipped to ×4 by default (`MAX_AG_CODED 0x0960`, Misc, SW_STANDBY-only).
- `0x0450` **MANUAL_DIGITAL_GAIN_CH0** (FP5.8, `0x0100`=×1.0): ×1..×8.
- **Latch protocol (GROUP_PARAM_HOLD `0x0448`)**: write 1 → update the
  EXP_MODE/MANUAL_*/AE_*/ROI registers → write 0; firmware applies atomically
  on release (AE frozen while held). Works in SW_STANDBY **and live during
  STREAMING** (bench: live exposure change mid-stream works).
- Applied-value readbacks (STATUS, per frame): `0x0064` coarse, `0x0068`
  analog gain, `0x006A` digital gain, `0x0072` AE_MODE, `0x0073` AE_STATUS
  (1=converged), `0x0074` AE_MEAN_ENERGY. (`0x004C` is the TEMPERATURE.)
- AEC tuning (DYNAMIC): `0x043C` AE_TARGET_PERCENTAGE (default 27%),
  `0x043A` AE_COMPENSATION, coldstart regs `0x0428–0x042E`.

### 10.3 Timing groups and the slow-mode rationale

- `0x0300` **LINE_LENGTH** (STATIC, u16, pixel clocks): min 1236 (10-bit ADC),
  **no documented max**; ST: long lines "may allow interoperability with low
  frequency MIPI receivers" — exactly our USB-2 slow mode. STATIC +
  SENSOR_SETTINGS (0x0220 clock tree) + Misc groups are **SW_STANDBY-only**
  (latch at next START_STREAM); CONTEXT + DYNAMIC groups are writable in
  standby or streaming.
- `0x0458` FRAME_LENGTH (u16, lines): min = ROI height/subsampling + ~69.
  Frame rate = 1/(line_time × FRAME_LENGTH).
- **Clock tree is pinned**: pixel clock must be ≈160.8 MHz (10-bit; 201 MHz
  9-bit), PLL out 790–805 MHz, VCO 0.5–1 GHz → `VT_CLK_DIV (0x0227)`
  effectively must stay 5 (4 in 9-bit mode). Off-spec dividers take down the
  sensor's internal MCU (shares the PLL tree) — do NOT retune clocks; stretch
  `LINE_LENGTH` instead.
- Applied LINE_LENGTH/FRAME_LENGTH readbacks: STATUS `0x0078`/`0x007C`.
