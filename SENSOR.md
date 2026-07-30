# SENSOR.md — VD56G3 bring-up handoff

Everything this project learned about initializing the **ST VD56G3** global-shutter
sensor and receiving image data, written as a handoff for a new project using the
same sensor (with or without the EVK's Cypress CX3 USB bridge).

**Provenance tags** — every claim is marked:
- **[hw]** verified by us against the live sensor (bench scripts, real frames)
- **[cap]** observed in USBPcap captures of ST's own GUI driving the sensor
- **[doc]** ST documentation: UM2602 Rev 8 (integration guide, the register
  bible) and DS12968 Rev 12 (datasheet), both in `datasheets/`
- **[evk]** EVK/CX3-bridge-specific — does not apply to a bare sensor design

Related repo files: `PROTOCOL.md` (CX3 USB protocol + §10 register digest),
`firmware/vd56g3_cold_init.json` (the exact hardware-proven init),
`firmware/vd56g3_registers.py` (register map with official names).

---

## 1. Device identity

- VD56G3: 1.5 MP **monochrome global shutter**, 1124×1364 active pixels
  (portrait 5:6), RAW8/RAW10, max 88 fps full-res, up to 2-lane MIPI CSI-2,
  on-chip AE, dark cal, defect correction, 4 contexts, test-pattern generator,
  temperature sensor, 6 GPIOs. **[doc]**
- `MODEL_ID (0x0000, u16)` reads **0x5603**. **[hw]**
- Color sibling: VD66GY (Bayer; same register map per UM2602). **[doc]**
- CAM-56G3 module variants add a fixed-focus f/2.0 lens (73°/84°/152° DFOV) —
  no focus actuator, nothing to drive. **[doc]**

## 2. Electrical / hookup (module or bare sensor)

- Rails: **VCORE 1.15 V, VDDIO 1.8 V, VANA 2.8 V**. **[doc]**
- **CLKIN: 6–27 MHz** external clock. Must be running **before** XSHUTDOWN is
  released. **[doc]** (EVK supplies 12 MHz. **[cap]**)
- **XSHUTDOWN**: low = power-down (HW_STANDBY), high (with rails + CLKIN) =
  device boots to READY_TO_BOOT. The only reset line. **[doc]**
- Power-up: rails on with XSHUTDOWN low → start CLKIN → XSHUTDOWN high.
  Power-down: STOP_STREAM → poll FSM=2 → CLKIN off + XSHUTDOWN low → rails
  down. **[doc]**
- Time from XSHUTDOWN release to READY_TO_BOOT depends on CLKIN frequency;
  **I²C does not respond before READY_TO_BOOT — a successful I²C read IS the
  ready signal** (poll it). **[doc]**

## 3. I²C / CCI interface

- Fast mode (400 kHz) or **Fast mode plus (1 MHz)** at 1.8 V; the default
  after MCU boot is **Fm+ with 20 mA sink** (reducible to 4 mA/fast-mode via a
  register). **[doc]**
- CCI protocol, **2-byte (16-bit) register subaddresses, big-endian on the
  wire**; multi-byte register **values are little-endian** (LSB first).
  **[doc]+[hw]**
- Default device address **0x20 including the R/W bit** (7-bit address 0x10);
  overridable (I2C_ADDR_UPDATE command / GPIO strap options). **[doc]**
  All our traffic used 0x20. **[hw]**
- Register access is plain CCI read/write; the sensor supports auto-increment
  writes (used for firmware patch upload). **[doc]**

## 4. State machine, commands, errors

`SYSTEM_FSM (0x0028, u8)`: **0**=HW_STANDBY, **1**=READY_TO_BOOT,
**2**=SW_STANDBY, **3**=STREAMING, **0xFF**=ERROR. **[doc]+[hw]**

**Command registers self-clear.** Write the command value, then **poll the same
register until it reads 0** (= acknowledged). A command issued while the
previous one is still nonzero is **silently ignored** — this cost us a day;
never skip the poll. Observed ack latency: ~1–25 ms. **[doc]+[hw]**

| Reg | Command values |
|---|---|
| `0x0200` BOOT | `01` BOOT, `02` PATCH_SETUP |
| `0x0201` SW_STANDBY | `01` **START_STREAM**, `02` NVM_READ, `03` NVM_PROG, `04` THSENS_READ, `05` I2C_ADDR_UPDATE |
| `0x0202` STREAMING | `01` **STOP_STREAM**, `02` VT_FSYNC_IN_I2C |
| `0x0203` VTPATCHING | `01` START_VTRAM_UPDATE, `02` END_VTRAM_UPDATE |

⚠ Beware misleading names in ST's SDK constants: 0x0201 ("CMD_STBY") is where
START_STREAM lives and 0x0202 ("CMD_STREAMING") is where STOP lives. **[hw]**

**Errors**: on failure the device enters ERROR (FSM=0xFF), streaming stops, and
`ERROR_CODE (0x001C, u16)` holds the reason (0xa00 LONG_COARSE_MAX, 0xa01
LONG_COARSE_MIN, 0xa02 BAD_FRAME_LENGTH, 0xa03 ISB_LONG_PIPE_OVERFLOW, 0xc00
CSI_LANE_DESYNC, …). Recovery requires a reset (XSHUTDOWN pulse). **[doc]**

## 5. Register groups and write rules

| Group | Range | Writable | Applied |
|---|---|---|---|
| STATUS | 0x0000–0x00BF | read-only | refreshed every frame |
| COMMAND | 0x0200–0x0203 | anytime | immediate, self-clearing |
| SENSOR_SETTINGS (clock/PLL) | 0x0220–0x0230 | **SW_STANDBY only** | at START_STREAM |
| STATIC (incl. LINE_LENGTH) | 0x0300–0x0341 | **SW_STANDBY only** | at START_STREAM |
| DYNAMIC (AE ctrl, GPH) | 0x0428–0x0448 | standby **or** streaming | per frame |
| CONTEXT 0 (ROI/exposure/FL) | 0x044C–0x048E | standby **or** streaming | at START_STREAM / per frame |
| MISC (limits, margins) | 0x0946–0x0980 | **SW_STANDBY only** | at START_STREAM |

(Contexts 1/2/3 replicate the CONTEXT block at +0x48 strides: 0x0494, 0x04DC,
0x0524.) **[doc]**

**GROUP_PARAM_HOLD (`0x0448`, bit0)** — atomic update latch for
EXP_MODE/MANUAL_*/AE_*/ROI: write 1 → update registers → write 0; firmware
applies the set on release, AE is frozen while held. **[doc]**
⚠ **[hw] Race we found (not in the doc):** the GPH release is processed
**asynchronously** — issuing START_STREAM within a few ms of `0x0448←0`
silently drops the held updates. For **pre-stream** config use bare CONTEXT
writes (they latch reliably at START_STREAM); reserve GPH for **live**
mid-stream changes, or wait ≥300 ms after release before starting.

## 6. Init: power-up → first frame (hardware-proven)

The complete working sequence, distilled from ST's GUI cold init (**[cap]**,
`firmware/vd56g3_cold_init.json`, 79 steps) and replayed successfully by our
own code on both PC and phone (**[hw]**). Register writes below are
sensor-I²C; board-level steps are tagged.

1. **Power + clock + reset** *(board level)*: rails up, CLKIN running (EVK:
   12 MHz), XSHUTDOWN released. ST's GUI pulses reset twice, spacing the
   toggles **76–494 ms** apart **[cap]**; we use ≥100 ms per edge. **[hw]**
2. **Wait for READY_TO_BOOT**: poll any register until I²C answers, or
   `SYSTEM_FSM`=1. **[doc]**
3. **(Optional) firmware patches** — **not needed to stream**: the sensor
   streams fine with `FWPATCH_REVISION (0x001E)` = 0. **[hw]** If wanted:
   main FW patch (~9–10 KB) is written to 0x2000 by I²C auto-increment during
   READY_TO_BOOT; VT patch via `0x0203` bracket commands. **[doc]**
4. **`0x0200 ← 01` (BOOT)**, poll to 0, then poll `SYSTEM_FSM` = 2
   (SW_STANDBY). GUI proceeded 14–131 ms after BOOT. **[cap]+[hw]**
5. **Clock/PLL config** (SW_STANDBY only) — the exact working set for a
   12 MHz CLKIN **[cap]+[hw]**, with the doc's constraints **[doc]**:

   | Reg | Name | Value | Rule |
   |---|---|---|---|
   | `0x0960` | (analog misc) | 0x1C | from capture, purpose unlisted |
   | `0x096A` | (analog misc) | 0x003C | from capture |
   | `0x0220` | EXT_CLOCK (u32, Hz) | 12,000,000 | must match CLKIN; 6–27 MHz |
   | `0x0224` | CLK_PLL_PREDIV | 2 | CLKIN/prediv ∈ [6,12] MHz; prediv ∈ {1,2,4} |
   | `0x0226` | CLK_SYS_PLL_MULT | 134 (0x86) | VCO = CLKIN·mult/prediv ∈ [500,1000] MHz |
   | `0x0225` | CLK_PLL_POSTDIV | 1 | PLL out ∈ [790,805] MHz, target ≈804 |
   | `0x0227` | VT_CLK_DIV | 5 | pixel clock = PLLout/div **must be ≈160.8 MHz** (10-bit ADC; 201 MHz for 9-bit, div 4) |

   ⚠ **The pixel clock is pinned.** Off-spec dividers (we tried 20 and 30)
   take the sensor's internal MCU down with them (it shares the PLL tree) —
   I²C dies, only a reset recovers. **Never slow the sensor via the clock
   tree; use LINE_LENGTH (§7).** **[hw]+[doc]**
6. **`0x0201 ← 04` (THSENS_READ)** — the GUI does this twice; it's just a
   thermal-sensor read, not required. **[cap]+[doc]**
7. **Context 0 configuration** (values = the proven 1120×1360 RAW10 setup)
   **[cap]+[hw]**:

   | Reg | Name | Value |
   |---|---|---|
   | `0x0302` | ORIENTATION | 2 |
   | `0x044C` | EXP_MODE | 0 (= auto; write **2** here for manual, §8) |
   | `0x045A/0x045C` | Y_START / Y_END | 0 / 1359 |
   | `0x0462/0x0464` | OUT_ROI_Y_START / _END | 0 / 1359 |
   | `0x0434/0x0438` | AE_ROI_START_V / _END_V | 0 / 1359 |
   | `0x045E/0x0460` | OUT_ROI_X_START / _END | 2 / 1121 (→ width 1120) |
   | `0x0458` | FRAME_LENGTH (lines) | 2168 (→ 60 fps at min line length) |
   | `0x044E` | (MANUAL_)COARSE_EXPOSURE | 1000 (start value; AE overrides in mode 0) |

   Width constraint: width·bpp must be a multiple of 32 (RAW10 → width ×16,
   RAW8 → ×4). **[doc]** Format (8 vs 10 bit) on the EVK comes from the CX3
   receiver config, not `0x030A`. **[cap]**
8. **`0x0201 ← 01` (START_STREAM)**, poll to 0, then `SYSTEM_FSM` = 3.
   First frame is on the wire immediately. **[hw]**
9. **Stop**: `0x0202 ← 01`, poll to 0 → FSM 2. The sensor finishes emitting
   the current frame first. **[doc]+[hw]**

Pacing: our replay inserts ~5–10 ms between plain writes (a capture-derived
safety margin, not a proven requirement); command registers use the
poll-to-zero handshake instead of delays. **[hw]**

## 7. Video timing and frame rate

- `LINE_LENGTH (0x0300, STATIC, u16)` in **pixel-clock cycles**; minimum
  **1236** (10-bit ADC) → 7.69 µs/line at 160.8 MHz; 1360 min for 9-bit.
  **No documented maximum** — ST explicitly endorses long lines for slow
  receivers. **[doc]** Readback of the applied value: STATUS `0x0078`. **[hw]**
- `FRAME_LENGTH (0x0458, CONTEXT, u16)` in lines; min = ROI
  height/subsampling + FRAME_LENGTH_OFFSET (~69, itself shrinking as line
  length grows via READ_OFFSET = roundUp(18·1236/line_length)). **[doc]**
- **Frame rate = pixel_clock / (LINE_LENGTH × FRAME_LENGTH)**
  (160.8 MHz / (1236 × 2168) = 60.0 fps — matches capture). **[doc]+[hw]**
- **Bandwidth throttling ("line stretch")**: multiply LINE_LENGTH by N to
  divide the average output rate and fps by N at full resolution with zero
  clock changes. **4× (4944) fully validated incl. sustained streaming;
  6×–12× initialize and reach FSM=3** (12× once produced an ERROR state in a
  degraded USB session — if you push past 4×, read `ERROR_CODE` on failure).
  Remember exposure is in *line periods*: rescale it when stretching. **[hw]**

## 8. Exposure and gain (context 0)

- `EXP_MODE (0x044C)`: **0 = Automatic** (AEC owns coarse exposure + both
  gains — manual writes are overridden), **1 = Freeze** AE at current values,
  **2 = Manual**. **[doc]+[hw]**
- `MANUAL_COARSE_EXPOSURE (0x044E, u16, line periods)`: min **21** lines
  (10-bit ADC; ≥161 µs), max **FRAME_LENGTH − margin(68) − 7** (≈2093 for
  FL 2168). Out-of-range **clips safely** (no error). **[doc]+[hw]**
  Exposure time = value × line period (7.69 µs at min line length).
- `MANUAL_ANALOG_GAIN (0x044D, u5)`: gain = **32/(32−code)**, code 0–28 →
  ×1.0–×8.0; clipped to ×4 by default (`MAX_AG_CODED 0x0960`, MISC,
  standby-only, raise to 28 for ×8). **[doc]**
- `MANUAL_DIGITAL_GAIN_CH0 (0x0450, FP5.8)`: ×1.0–×8.0 (0x0100 = ×1.0);
  CH0 suffices on the mono VD56G3. **[doc]**
- Live change while streaming works (wrap in GPH): validated 488→1900 lines
  mid-stream. **[hw]**
- AE tuning (DYNAMIC): `AE_TARGET_PERCENTAGE 0x043C` (default 27% of
  saturation), `AE_COMPENSATION 0x043A` (EV, SFP8.8), coldstart values
  `0x0428–0x042E` (applied at START_STREAM). AE needs ~10–20 frames to
  converge — a one-shot capture in auto mode should discard warm-up frames
  and keep the last (validated: 15 frames ≈ 1 s at 15 fps). **[doc]+[hw]**
- **Applied-value readbacks** (STATUS, refreshed per frame): coarse `0x0064`,
  analog gain `0x0068`, digital gain `0x006A`, active mode `0x0072`,
  AE converged flag `0x0073`, mean energy `0x0074`. `0x004C` is the
  **temperature**, not an AE register. **[doc]+[hw]**
- Validated brightness ladder (fixed scene): 120/480/1900 lines → 10-bit mean
  111/248/723. **[hw]**

## 9. MIPI output and frame structure

- CSI-2, 1–2 data lanes; EVK setup runs **2 lanes** at
  `OIF_CSI_BITRATE (0x0312)` = **1010 Mbps/lane** (valid 250–1500; must
  satisfy one line of data fitting in one line time). **[doc]+[hw]**
- **Continuous MIPI clock by default** (clock lanes always HS); non-continuous
  (LP between frames) via `DPHYTX_CTRL` (MISC, default 0x1C, write 0x0C).
  **[doc]**
- Each frame: **2 embedded status lines (ISL)** then the image rows. ISL is
  RAW8 SMIA-style 2-byte-tagged: status register `R` (< 0x7D) is at byte
  `2·R+6` of line 1; `R ≥ 0x7D` at `row_bytes + 2·(R−0x7D)+6` (line 2);
  multi-byte values LSB-first. Useful ISL fields: FORMAT_CTRL (0x5B → bpp),
  OUT_ROI_Y_SIZE (0x94), FRAME_COUNTER (0x50), and the applied exposure/gain
  set. **[doc]+[hw]**
- **RAW10 packing**: 5 bytes → 4 pixels; bytes 0–3 are the high 8 bits of
  px0–3, byte 4 holds the four low-2-bit pairs
  (`px_i = (byte_i<<2) | ((byte4>>(2i)) & 3)`). RAW8 is 1:1. **[doc]+[hw]**
- Row stride in bytes = width·bpp/8 (1400 B for 1120 @ RAW10); full payload =
  (2 + height) rows. **[hw]**
- ⚠ **[evk]** On the EVK, the CX3 bridge wraps each frame in its own 16 KB
  chunk framing (16-byte header + ≤16352 payload + 16-byte footer per chunk)
  and stalls its USB endpoint on overflow — that is bridge behavior, not the
  sensor. See `PROTOCOL.md` §5.0. A direct MIPI receiver sees the clean
  ISL+rows frame described above.

## 10. Gotchas checklist (each one cost us real debugging time)

1. **Poll commands to zero** — back-to-back command writes are silently
   dropped. **[hw]**
2. **ST's constant names swap start/stop** — START_STREAM is `0x0201←01`,
   STOP is `0x0202←01`. A blind replay of a capture will include the
   operator's final Stop click; don't replay it. **[hw]**
3. **AE owns exposure in mode 0** — writing `0x044E` does nothing visible
   until `EXP_MODE=2` (or Freeze). **[hw]**
4. **GPH release is async** — don't START_STREAM within ~ms of `0x0448←0`
   (updates silently lost). Bare writes pre-stream; GPH live. **[hw]**
5. **Never retune the clock tree to slow output** — pixel clock is pinned at
   160.8 MHz; use LINE_LENGTH. Off-spec dividers crash the sensor MCU
   (I²C dead until reset). **[hw]**
6. **FSM=0xFF is a documented ERROR state** — read `ERROR_CODE (0x001C)`
   before assuming a crash; reset (XSHUTDOWN) recovers either way. **[doc]+[hw]**
7. **`0x004C` is temperature** — don't mistake its drift for AE activity. **[doc]**
8. **No firmware patch is required to stream** — despite SDK code paths that
   imply otherwise. Patches only enable "enhanced" firmware features. **[hw]**
9. Reset pulses need real width — space XSHUTDOWN toggles ~100 ms (GUI used
   76–494 ms). **[cap]**
10. ST's `vdx6gx_constants.py` double-defines `STREAM_STATICS_OUTPUT_CTRL`
    (0x0096 read-only mirror vs **0x0335** writable) — `import *` order makes
    0x0335 win; 0x0096 writes go nowhere. **[hw]** (Replay path doesn't need
    0x0335 at all.)
11. Manual exposure means scene-dependent brightness — a fixed setting reads
    darker as the room dims; that's correct behavior, not a regression. **[hw]**

## 11. Reproduction pointers

- `firmware/vd56g3_cold_init.json` — the byte-exact 79-step init (regenerate:
  `python tools/extract_cold_init.py captures/cold`).
- `grab.py --auto` (PC) / `termux_grab.py` (Android/Termux) / `web/` (WebUSB)
  — three working implementations of everything above.
- `tests/test_protocol_offline.py` — 13 offline regression tests.
- UM2602 Rev 8 section index: §5 power-up→streaming, §8–9 commands+errors,
  §13 clocks/timing, §14 exposure/AE/GPH, §16 output interface, §19 register
  groups (Table 44 STATUS, 45 COMMAND, 46 SENSOR_SETTINGS, 47 STATIC,
  48 DYNAMIC, 49 CONTEXT, 51 MISC).
