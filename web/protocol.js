// protocol.js — STEVAL-EVK-U0I (CX3 "spider" + VD56G3) WebUSB protocol layer
// =============================================================================
// A dependency-free ES module that mirrors the Phase-1 Python protocol BYTE FOR
// BYTE. The authoritative spec is PROTOCOL.md in the repo root; section numbers
// (§2, §3.1, §4, §5, §6) below refer to it. Everything here was derived by
// static analysis of ST's STSW-IMG507 v2.1.0 binaries + shipped Python examples.
//
// Transport model (PROTOCOL.md §2):
//   The CX3 firmware exposes a request/response ASCII console over a BULK
//   endpoint pair. A request is one line:
//       <KEYWORD>( <HH>)* \r\n
//   i.e. the command mnemonic, then for EACH argument byte a single space
//   followed by TWO UPPERCASE hex digits (high nibble first), terminated by
//   CRLF (0x0D 0x0A). The reply arrives on the answer bulk-IN endpoint and is
//   NUL-terminated by firmware.
//
// Sensor register access (PROTOCOL.md §3.1) is I2C tunnelled through the console:
//   - register ADDRESS is 16-bit BIG-endian on the wire (addr_hi, addr_lo)
//   - register VALUE   is LITTLE-endian, LSB first (1/2/4 bytes)
//
// NOTHING here hardcodes endpoint numbers as the only option — they are
// auto-discovered from the interface descriptors and overridable (see app.js).
// =============================================================================

// ---------------------------------------------------------------------------
// Command index table (PROTOCOL.md §3). The *index* is 1-based table order; the
// firmware keys on the KEYWORD string on the wire, but we keep the index for
// documentation / parity with cx3_query(board, cmd_index, ...).
// ---------------------------------------------------------------------------
export const CMD = Object.freeze({
  ID:        1,
  VERSION:   2,
  I2CWR:     3,
  I2CRD:     4,
  IOSET:     5,
  IOGET:     6,
  SPIWRRD:   7,
  NRST:      8,
  CFGWR:     9,
  CFGRD:     10,
  CLKWR:     11,
  CLKRD:     12,
  I2CWRRD:   13,
  IOCFGWR:   14,
  IOCFGRD:   15,
  LOGLVLWR:  16,
  LOGLVLRD:  17,
  CFG2WR:    18,
  CFG2RD:    19,
  RESET:     20,
});

// Sensor I2C address as seen by the CX3 (8-bit base; 7-bit = 0x10). §1.
export const SENSOR_I2C_ADDR = 0x20;

// Status-line register addresses read back out of the frame (PROTOCOL.md §5,
// mirrors ST vdx6gx_constants.py).
export const STATUS_REG = Object.freeze({
  FRAME_COUNTER:   0x50, // u16
  CURRENT_CONTEXT: 0x56, // u8
  FORMAT_CTRL:     0x5B, // u8  -> bits per pixel (8 or 10)
  OUT_ROI_Y_SIZE:  0x94, // u16 -> y_size (image rows)
});

// Sensor command / config registers touched by the init sequence (PROTOCOL.md
// §6/§9.0, mirrors firmware/vd56g3_registers.py).
//
// ⚠ Command semantics (HARDWARE-CONFIRMED 2026-07-16, contra ST's constant
// names): command registers SELF-CLEAR to 0 when consumed (poll before the
// next command or it is silently dropped), 0x0201<-01 STARTS streaming
// (SYSTEM_FSM 2->3) and 0x0202<-01 STOPS it. See PROTOCOL.md §9.0.
export const REG = Object.freeze({
  CMD_BOOT:              0x0200, // u8  write 1 to boot the sensor FW; self-clears
  CMD_START_STREAM:      0x0201, // u8  write 1 to START streaming; self-clears
  CMD_STOP_STREAM:       0x0202, // u8  write 1 to STOP streaming; self-clears
  SYSTEM_FSM:            0x0028, // u8  1=ready-to-boot, 2=standby, 3=streaming
  CMD_DEBUG:             0x0203, // u8  1=enter patch mode, 2=exit (VT patch)
  STATICS_FORMAT_CTRL:   0x030A, // u16 bits per pixel (8 or 10)
  // STREAM_STATICS OUTPUT_CTRL. ST's vdx6gx_constants.py defines the symbol
  // twice (0x0096 then 0x0335); `import *` makes 0x0335 win, so the example
  // writes 0x0335. 0x0096 is the read-only status mirror. Do NOT change back.
  STREAM_OUTPUT_CTRL:    0x0335, // u8 (written via write16 to mirror ST exactly)
  CONTEXTS_READOUT_CTRL: 0x0474, // u8
  CTX0_OUT_ROI_X_START:  0x045E, // u16
  CTX0_OUT_ROI_X_END:    0x0460, // u16
  CTX0_OUT_ROI_Y_START:  0x0462, // u16
  CTX0_OUT_ROI_Y_END:    0x0464, // u16
});

// Known-good defaults (PROTOCOL.md §4/§6, firmware/vd56g3_registers.py DEFAULT).
// bpp defaults to 8 (RAW8) — "use RAW8 first on the phone".
export const DEFAULTS = Object.freeze({
  bpp: 8,
  // Sensor ROI (context 0) register values:
  x_start: 2, x_end: 1105,     // active width 1104
  y_start: 2, y_end: 1361,     // active height 1360
  // CX3 CSI receiver (CFGWR) params:
  csi_lanes: 2,
  csi_field_b: 0,              // VC / format selector (byte 1)
  csi_data_rate_mbps: 1500,
  csi_width: 1116,            // width on the wire (incl. ROI + status budget)
  csi_height: 1356,          // height on the wire
  csi_pixel_clock_hz: 160_800_000, // only consumed by the CFG2WR (v2) path
  csi_field11: 0,            // byte 11 reserved/format
});

// CFG2WR (v2 CSI config) payloads captured VERBATIM from real hardware
// (captures/steval-connect). The device uses CFG2WR, not the derived CFGWR
// struct — replay these 12 bytes for the matching bpp. Mirrors
// firmware/vd56g3_csi_cfg2wr.json and reg.CFG2WR_CAPTURED (Python).
export const CFG2WR_CAPTURED = Object.freeze({
  8:  [0x26, 0x02, 0x04, 0xBB, 0x8C, 0x40, 0x04, 0x00, 0x04, 0x00, 0x08, 0x64],
  10: [0x26, 0x02, 0x05, 0xEE, 0x3F, 0xE0, 0x04, 0x00, 0x04, 0x00, 0x0A, 0x64],
});

// Bulk transfer timeout in ms (PROTOCOL.md §2 — 150 ms).
export const BULK_TIMEOUT_MS = 150;

// =============================================================================
// Small byte helpers
// =============================================================================
const HEX = [];
for (let i = 0; i < 256; i++) HEX[i] = i.toString(16).toUpperCase().padStart(2, "0");

/**
 * Build the ASCII request line for the console (PROTOCOL.md §2).
 *   keyword + (" " + HH)* + CRLF
 * @param {string} keyword command mnemonic, e.g. "I2CWR"
 * @param {Uint8Array|number[]} args argument bytes
 * @returns {Uint8Array} bytes ready for transferOut (CRLF included, no NUL)
 */
export function buildRequest(keyword, args = []) {
  let line = keyword;
  for (const b of args) {
    // Each argument byte: one space + two uppercase hex digits, MSB nibble first.
    line += " " + HEX[b & 0xff];
  }
  line += "\r\n"; // CRLF terminator (0x0D 0x0A). NUL is not sent on the wire.
  // ASCII-only, so a plain char->byte map is exact.
  const out = new Uint8Array(line.length);
  for (let i = 0; i < line.length; i++) out[i] = line.charCodeAt(i) & 0xff;
  return out;
}

/**
 * Decode a NUL-terminated console reply into text (PROTOCOL.md §2).
 * @param {Uint8Array} bytes raw bytes read from the answer bulk-IN endpoint
 * @returns {string} the ASCII text up to (not including) the first NUL
 */
export function decodeReply(bytes) {
  let end = bytes.length;
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i] === 0x00) { end = i; break; }
  }
  let s = "";
  for (let i = 0; i < end; i++) s += String.fromCharCode(bytes[i]);
  return s;
}

/**
 * Parse the hex data bytes out of a console reply.
 * Grammar is [capture-confirmed] (PROTOCOL.md §8): "OK <HH> <HH> ...". We drop
 * the leading OK status token and parse the space-separated hex bytes. Returns
 * [] if nothing parses.
 * @param {string} reply
 * @returns {number[]} bytes
 */
export function parseHexBytes(reply) {
  const out = [];
  let toks = reply.replace(/,/g, " ").trim().split(/\s+/);
  if (toks.length && toks[0].toUpperCase() === "OK") toks = toks.slice(1);
  for (const t of toks) {
    const m = /^(0x)?([0-9a-fA-F]{1,2})$/.exec(t);
    if (m) out.push(parseInt(m[2], 16));
  }
  return out;
}

// =============================================================================
// Cx3Console — the ASCII request/response transport over a WebUSB USBDevice
// =============================================================================
export class Cx3Console {
  /**
   * @param {USBDevice} device an opened WebUSB device with its interface claimed
   * @param {object} eps endpoint numbers { cmdOut, ansIn, videoIn }
   * @param {(msg:string)=>void} [log] optional transaction logger
   */
  constructor(device, eps, log = () => {}) {
    this.device = device;
    this.cmdOut = eps.cmdOut;   // command OUT (console request)   — PROTOCOL.md §1
    this.ansIn = eps.ansIn;     // answer  IN (console reply)
    this.videoIn = eps.videoIn; // video   IN (streaming payload)
    this.log = log;
    // answer read buffer capacity (firmware reply is small ASCII; be generous)
    this.answerCap = 4096;
  }

  /**
   * One console transaction: write request, read the NUL-terminated reply.
   * Mirrors cx3_query (PROTOCOL.md §2): on a failed transfer, clear_halt the
   * answer-IN endpoint and retry ONCE.
   * @param {string} keyword
   * @param {Uint8Array|number[]} [args]
   * @returns {Promise<{text:string,bytes:Uint8Array}>}
   */
  async query(keyword, args = []) {
    const req = buildRequest(keyword, args);
    this.log(`> ${keyword}${args.length ? " " + Array.from(args, (b) => HEX[b & 0xff]).join(" ") : ""}`);

    let attempt = 0;
    // Retry loop: at most one retry (2 attempts total) per PROTOCOL.md §2.
    for (;;) {
      try {
        // ---- write request on the command bulk-OUT endpoint ----
        const wr = await this.device.transferOut(this.cmdOut, req);
        if (wr.status !== "ok") throw new Error(`transferOut status=${wr.status}`);

        // ---- read the reply on the answer bulk-IN endpoint ----
        const rd = await this._readAnswer();
        const text = decodeReply(rd);
        this.log(`< ${text.replace(/\r?\n/g, " ").trim() || "(empty)"}`);
        return { text, bytes: rd };
      } catch (err) {
        attempt++;
        if (attempt >= 2) {
          this.log(`! ${keyword} failed: ${err.message}`);
          throw err;
        }
        // Recovery: clear a stalled answer-IN endpoint, then retry once.
        this.log(`! ${keyword} transfer error (${err.message}); clearHalt + retry`);
        try { await this.device.clearHalt("in", this.ansIn); } catch (_) { /* ignore */ }
      }
    }
  }

  /** Read one NUL-terminated answer packet from the answer bulk-IN endpoint. */
  async _readAnswer() {
    const rd = await this.device.transferIn(this.ansIn, this.answerCap);
    if (rd.status !== "ok") throw new Error(`transferIn status=${rd.status}`);
    return new Uint8Array(rd.data.buffer, rd.data.byteOffset, rd.data.byteLength);
  }

  /**
   * Read up to `length` bytes off the VIDEO bulk-IN endpoint (a single transfer).
   * The higher-level frame assembler loops over this. clearHalt+retry once on stall.
   * @param {number} length bytes to request
   * @returns {Promise<Uint8Array>}
   */
  async readVideo(length) {
    let attempt = 0;
    for (;;) {
      try {
        const rd = await this.device.transferIn(this.videoIn, length);
        if (rd.status === "stall") throw new Error("video stall");
        // "babble" or "ok" both carry data; surface whatever we got.
        return new Uint8Array(rd.data.buffer, rd.data.byteOffset, rd.data.byteLength);
      } catch (err) {
        attempt++;
        if (attempt >= 2) throw err;
        try { await this.device.clearHalt("in", this.videoIn); } catch (_) { /* ignore */ }
      }
    }
  }

  /**
   * Read ONE complete on-wire frame in a SINGLE transfer (PROTOCOL.md §5.0,
   * hardware-confirmed strategy). The CX3 stalls EP 0x83 when its DMA
   * overflows — which it does within milliseconds whenever the host pauses
   * between chunked reads (~115 MB/s stream) or isn't reading at all.
   * clearHalt makes it resume at the NEXT FRAME BOUNDARY, so each attempt is:
   * clearHalt -> one frame-sized(+slack) transferIn (the end-of-frame short
   * packet terminates it at exactly the wire size) -> validate the first
   * chunk header. Chunked host reads tear frames; do not use them.
   * @param {number} wireTotal exact on-wire frame size (wireFrameSize(payload))
   * @param {number} [tries]
   * @returns {Promise<Uint8Array>} the wire frame, or throws after `tries`
   */
  async readFrame(wireTotal, tries = 8) {
    // Round the request up to a packet multiple (512 HS / 1024 SS): a
    // non-multiple buffer can end mid-packet -> "babble" on Android's usbfs.
    const request = Math.ceil((wireTotal + 65536) / 1024) * 1024;
    let lastErr = "";
    for (let attempt = 1; attempt <= tries; attempt++) {
      try { await this.device.clearHalt("in", this.videoIn); } catch (_) { /* ignore */ }
      let data;
      try {
        const rd = await this.device.transferIn(this.videoIn, request);
        if (rd.status === "stall") throw new Error("stall");
        data = new Uint8Array(rd.data.buffer, rd.data.byteOffset, rd.data.byteLength);
      } catch (err) {
        lastErr = err.message;
        this.log(`! video attempt ${attempt}/${tries}: ${err.message}`);
        continue;
      }
      if (data.length >= wireTotal && isFrameStart(data)) return data;
      this.log(`! video attempt ${attempt}/${tries}: ${data.length} bytes, frameStart=${isFrameStart(data)} — retrying`);
    }
    throw new Error(`no complete frame after ${tries} attempts${lastErr ? ` (last: ${lastErr})` : ""}`);
  }

  /**
   * Read ONE frame with the read QUEUED BEFORE the stream starts — the
   * strategy ST's own driver uses, and the robust one on Android where every
   * WebUSB call crosses an IPC hop and loses the clearHalt->read race against
   * the CX3's few-ms overflow window (symptom: every attempt reports "stall").
   *
   * Sequence per attempt: stop stream (self-clearing cmd, hardware-proven) ->
   * clearHalt -> queue the full-frame transferIn WITHOUT awaiting -> start
   * stream -> await the read. The first frame produced lands in the already-
   * pending transfer, so no host round-trip ever gaps the stream.
   *
   * @param {Vd56g3} sensor sensor helper (for stop/start commands)
   * @param {number} wireTotal exact on-wire frame size
   * @param {number} [tries]
   * @returns {Promise<Uint8Array>}
   */
  /**
   * Issue (or REUSE) the single outstanding video transfer. WebUSB transfers
   * cannot be cancelled: if a previous attempt timed out and we issued a NEW
   * transferIn each retry, abandoned multi-MB transfers stack up on the
   * endpoint and wedge Chrome's USB stack (observed on Android as a hang
   * after Capture). So at most ONE video transfer is ever in flight; a
   * timed-out attempt leaves it pending and the next attempt awaits the SAME
   * transfer.
   */
  _videoRead(request) {
    if (!this._pendingVideo) {
      const p = this.device.transferIn(this.videoIn, request);
      p.catch(() => {});                       // no unhandledrejection if it fails early
      p.finally(() => { if (this._pendingVideo === p) this._pendingVideo = null; });
      this._pendingVideo = p;
    }
    return this._pendingVideo;
  }

  async readFramePrequeued(sensor, wireTotal, tries = 4) {
    const request = Math.ceil((wireTotal + 65536) / 1024) * 1024;
    let stalls = 0;
    for (let attempt = 1; attempt <= tries; attempt++) {
      const reusing = !!this._pendingVideo;
      if (!reusing) {
        await sensor.stopStream();                  // idle the producer
        try { await this.device.clearHalt("in", this.videoIn); } catch (_) { /* ignore */ }
        this._videoRead(request);                   // queue FIRST (single outstanding)
        await sensor.startStream();                 // now produce frames into it
      }
      let data = null;
      try {
        // 10 s guard per attempt; on timeout the transfer stays pending and is
        // REUSED next attempt (never re-issued — see _videoRead).
        const rd = await Promise.race([
          this._videoRead(request),
          new Promise((_, rej) => setTimeout(() => rej(new Error("timeout (no video data yet)")), 10000)),
        ]);
        if (rd.status === "stall") { stalls++; throw new Error("stall"); }
        data = new Uint8Array(rd.data.buffer, rd.data.byteOffset, rd.data.byteLength);
      } catch (err) {
        this.log(`! prequeued video attempt ${attempt}/${tries}${reusing ? " (reused pending)" : ""}: ${err.message}`);
        continue;
      }
      if (data.length >= wireTotal && isFrameStart(data)) return data;
      this.log(`! prequeued video attempt ${attempt}/${tries}: ${data.length}/${wireTotal} bytes, ` +
               `frameStart=${isFrameStart(data)} — retrying`);
    }
    let msg = `no complete frame after ${tries} pre-queued attempts.`;
    if (stalls) {
      msg += ` ${stalls} attempt(s) ended in a stall even with the read queued before the stream ` +
             `started — the link cannot carry the ~115 MB/s video. This is the USB-2 cable ` +
             `signature: use a genuine 5 Gbps (USB 3) C-to-C cable.`;
    }
    throw new Error(msg);
  }

  /**
   * AE warm-up capture: get the first frame via readFramePrequeued, then keep
   * reading frames back-to-back for ~`warmupFrames` more and return the LAST
   * good one — by then the sensor's auto-exposure (active with the captured
   * init: CTX0_EXP_MODE 0x044C = 0) has converged on the scene. Requires the
   * link to keep up continuously (fine in slow mode / on SuperSpeed); a
   * mid-sequence stall is recovered with clearHalt and simply costs a frame.
   * @param {Vd56g3} sensor
   * @param {number} wireTotal
   * @param {number} [warmupFrames]
   * @returns {Promise<Uint8Array>}
   */
  async readFrameWarmup(sensor, wireTotal, warmupFrames = 15) {
    const first = await this.readFramePrequeued(sensor, wireTotal);
    const request = Math.ceil((wireTotal + 65536) / 1024) * 1024;
    let kept = first;
    let got = 1;
    const tEnd = performance.now() + 5000; // hard cap
    while (got < warmupFrames && performance.now() < tEnd) {
      try {
        // Single-outstanding-transfer discipline here too (see _videoRead),
        // with a short race so a quiet pipe can't hang the loop.
        const rd = await Promise.race([
          this._videoRead(request),
          new Promise((_, rej) => setTimeout(() => rej(new Error("frame timeout")), 2000)),
        ]);
        if (rd.status === "stall") throw new Error("stall");
        const d = new Uint8Array(rd.data.buffer, rd.data.byteOffset, rd.data.byteLength);
        if (d.length >= wireTotal && isFrameStart(d)) { kept = d; got++; }
      } catch (err) {
        if (err.message === "frame timeout") break;  // pipe quiet; keep what we have
        try { await this.device.clearHalt("in", this.videoIn); } catch (_2) { /* ignore */ }
      }
    }
    this.log(`AE warm-up: kept frame ${got}/${warmupFrames}.`);
    return kept;
  }
}

// =============================================================================
// Vd56g3 — sensor register / stream helpers layered on the console (PROTOCOL.md §3.1)
// =============================================================================
export class Vd56g3 {
  /**
   * @param {Cx3Console} console
   * @param {number} [i2cAddr] sensor I2C address (8-bit base 0x20)
   */
  constructor(console, i2cAddr = SENSOR_I2C_ADDR) {
    this.console = console;
    this.i2c = i2cAddr & 0xff;
  }

  // ---- register WRITE (PROTOCOL.md §3.1 / §8) ----------------------------
  // [capture-confirmed] writes go via I2CWRRD with read-length 0:
  //   [rdlen_hi=0, rdlen_lo=0, i2c, reg_hi, reg_lo, value_bytes(LE)...]
  //   register ADDRESS: 16-bit big-endian (reg_hi, reg_lo)
  //   register VALUE:   little-endian, LSB first; reply is a bare "OK"
  async _writeReg(reg, valueBytes) {
    const args = new Uint8Array(5 + valueBytes.length);
    args[0] = 0; args[1] = 0;    // rdlen = 0 -> write, read nothing
    args[2] = this.i2c;
    args[3] = (reg >> 8) & 0xff; // reg_hi (big-endian address)
    args[4] = reg & 0xff;        // reg_lo
    args.set(valueBytes, 5);
    return this.console.query("I2CWRRD", args);
  }

  /** Write raw (little-endian) value bytes to a register (used by the replay). */
  writeRegBytes(reg, valueBytes) {
    return this._writeReg(reg, valueBytes);
  }

  /** Write an 8-bit register value. */
  write8(reg, val) {
    return this._writeReg(reg, [val & 0xff]);
  }

  /** Write a 16-bit register value, LSB first. */
  write16(reg, val) {
    return this._writeReg(reg, [val & 0xff, (val >> 8) & 0xff]);
  }

  /** Write a 32-bit register value, LSB first. */
  write32(reg, val) {
    return this._writeReg(reg, [
      val & 0xff,
      (val >>> 8) & 0xff,
      (val >>> 16) & 0xff,
      (val >>> 24) & 0xff,
    ]);
  }

  // ---- register READ (PROTOCOL.md §3.1) ----------------------------------
  // I2CWRRD argument bytes = [rdlen_hi, rdlen_lo, i2c_addr, reg_hi, reg_lo]
  //   rdlen: number of bytes to read (16-bit big-endian)
  //   reply payload: rdlen bytes, reassembled little-endian.
  async _readReg(reg, n) {
    const args = new Uint8Array([
      (n >> 8) & 0xff, // rdlen_hi
      n & 0xff,        // rdlen_lo
      this.i2c,
      (reg >> 8) & 0xff, // reg_hi (big-endian address)
      reg & 0xff,        // reg_lo
    ]);
    const { text } = await this.console.query("I2CWRRD", args);
    // Reply grammar is [needs-capture]; parse a hex byte list, reassemble LE.
    const bytes = parseHexBytes(text);
    let value = 0;
    for (let i = 0; i < n && i < bytes.length; i++) value |= bytes[i] << (8 * i);
    return { value: value >>> 0, bytes };
  }

  async read8(reg)  { return (await this._readReg(reg, 1)).value; }
  async read16(reg) { return (await this._readReg(reg, 2)).value; }
  async read32(reg) { return (await this._readReg(reg, 4)).value >>> 0; }

  // ---- CSI receiver config (PROTOCOL.md §4) ------------------------------
  /**
   * Build + send the 12-byte CFGWR payload configuring the CX3 CSI-2 receiver.
   * Byte layout (PROTOCOL.md §4):
   *   [0]    lane_number (u8)
   *   [1]    field B     (u8)  VC/format selector
   *   [2..5] data_rate_mbps (u32 BIG-endian)
   *   [6..7] width  (u16 BIG-endian)
   *   [8..9] height (u16 BIG-endian)
   *   [10]   bit_per_pixel (u8)
   *   [11]   field (u8) reserved/format
   * @param {object} cfg { lanes, fieldB, dataRateMbps, width, height, bpp, field11 }
   * @param {boolean} [useV2] send CFG2WR (index 18) instead of CFGWR (index 9)
   * @param {boolean} [preferCaptured] default true: replay the exact CFG2WR bytes
   *   captured from real hardware for cfg.bpp (the device uses CFG2WR). Set false
   *   to use the computed path.
   */
  configureCsi(cfg, useV2 = false, preferCaptured = true) {
    // Preferred: replay the verbatim CFG2WR bytes ST's GUI sent on real hardware.
    if (preferCaptured && CFG2WR_CAPTURED[cfg.bpp]) {
      return this.console.query("CFG2WR", CFG2WR_CAPTURED[cfg.bpp]);
    }
    const p = new Uint8Array(12);
    p[0] = cfg.lanes & 0xff;
    p[1] = cfg.fieldB & 0xff;
    // data_rate_mbps as u32 big-endian
    p[2] = (cfg.dataRateMbps >>> 24) & 0xff;
    p[3] = (cfg.dataRateMbps >>> 16) & 0xff;
    p[4] = (cfg.dataRateMbps >>> 8) & 0xff;
    p[5] = cfg.dataRateMbps & 0xff;
    // width u16 big-endian
    p[6] = (cfg.width >> 8) & 0xff;
    p[7] = cfg.width & 0xff;
    // height u16 big-endian
    p[8] = (cfg.height >> 8) & 0xff;
    p[9] = cfg.height & 0xff;
    p[10] = cfg.bpp & 0xff;
    p[11] = cfg.field11 & 0xff;

    if (useV2) {
      // CFG2WR (v2) appends a derived timing word computed from pixel_clock:
      //   round(2*bpp*data_rate*1e6 / pixel_clock) - 1000000
      // The exact math is [needs-capture] (PROTOCOL.md §4) — this is a STUB that
      // sends the same 12 bytes plus a best-effort timing u32 (big-endian).
      // Default path stays CFGWR until a capture confirms the v2 word.
      const timing = this._cfg2Timing(cfg) >>> 0;
      const p2 = new Uint8Array(16);
      p2.set(p, 0);
      p2[12] = (timing >>> 24) & 0xff;
      p2[13] = (timing >>> 16) & 0xff;
      p2[14] = (timing >>> 8) & 0xff;
      p2[15] = timing & 0xff;
      return this.console.query("CFG2WR", p2);
    }
    return this.console.query("CFGWR", p);
  }

  /** [needs-capture] best-effort CFG2WR timing word (PROTOCOL.md §4). */
  _cfg2Timing(cfg) {
    const pclk = cfg.pixelClockHz || DEFAULTS.csi_pixel_clock_hz;
    // Guarded so a zero pixel clock cannot divide-by-zero.
    if (!pclk) return 0;
    return Math.round((2 * cfg.bpp * cfg.dataRateMbps * 1e6) / pclk) - 1_000_000;
  }

  // ---- command handshake (PROTOCOL.md §9.0, hardware-confirmed) -----------
  /**
   * Write a self-clearing command register and poll it back to 0. Issuing the
   * next command while the previous one is still nonzero gets it silently
   * dropped (observed on hardware as SYSTEM_FSM stuck at 2 / no video).
   * @returns {Promise<boolean>} true once the register reads 0
   */
  async sendCommand(reg, value, timeoutMs = 2000) {
    await this.write8(reg, value);
    const deadline = performance.now() + timeoutMs;
    let last = -1;
    while (performance.now() < deadline) {
      try { last = await this.read8(reg); } catch (_) { last = -1; }
      if (last === 0) return true;
      await new Promise((r) => setTimeout(r, 10));
    }
    this.console.log(`! command 0x${reg.toString(16)} <- 0x${value.toString(16)} not consumed (reads ${last})`);
    return false;
  }

  /** Poll SYSTEM_FSM (0x0028) until it reaches `target` (1 ready / 2 standby / 3 streaming). */
  async waitFsm(target, timeoutMs = 3000, label = "") {
    const deadline = performance.now() + timeoutMs;
    let val = -1;
    while (performance.now() < deadline) {
      try { val = await this.read8(REG.SYSTEM_FSM); } catch (_) { val = -1; }
      if (val === target) return val;
      await new Promise((r) => setTimeout(r, 50));
    }
    this.console.log(`! SYSTEM_FSM=${val}, wanted ${target} ${label}`);
    return val;
  }

  // ---- streaming control (PROTOCOL.md §9.0 — hardware-confirmed semantics) --
  /** Start streaming: CMD_START_STREAM (0x0201) <- 1, then wait for FSM=3. */
  async startStream() {
    await this.sendCommand(REG.CMD_START_STREAM, 1);
    return this.waitFsm(3, 5000, "(post-start)");
  }
  /** Stop streaming: CMD_STOP_STREAM (0x0202) <- 1 (FSM -> 2). */
  stopStream()  { return this.sendCommand(REG.CMD_STOP_STREAM, 1); }
}

// =============================================================================
// Cold-init replay (PROTOCOL.md §9) — the hardware-proven path
// =============================================================================

/**
 * Replay the hardware-captured cold-init sequence verbatim from
 * firmware/vd56g3_cold_init.json. This is what ST's GUI actually sent to a cold,
 * UNPATCHED VD56G3 that then streamed: register writes (I2CWRRD rdlen=0) plus
 * CLKWR/CFG2WR/NRST/IOSET/IOCFGWR, ending at CMD_STREAMING<-1. No FW patch is
 * applied (the sensor streams without one). Returns {width, height, bpp} derived
 * from the replayed OUT_ROI writes and the final CFG2WR bpp field.
 * @param {Vd56g3} sensor
 * @param {string} [url]
 * @returns {Promise<{width:number,height:number,bpp:number}>}
 */
export async function fetchFirmwareJson(name) {
  // Resolve firmware/ whether it sits alongside the page (Pages deploy: the
  // action copies firmware/ next to index.html) or one level up (repo served at
  // /web/). Tries both so the same code works in dev and deployed.
  const tried = [];
  for (const url of [`firmware/${name}`, `../firmware/${name}`]) {
    tried.push(url);
    try {
      const r = await fetch(url);
      if (r.ok) return await r.json();
    } catch (_) { /* try next */ }
  }
  throw new Error(`could not fetch ${name} (tried ${tried.join(", ")})`);
}

export async function replayColdInit(sensor, url, mods = null) {
  // mods (all optional): {
  //   overrides: { reg: [le bytes] }   value overrides for steps already in the
  //                                    captured sequence,
  //   preStart:  [{ reg, val }]        extra register writes injected right
  //                                    before CMD_START_STREAM (for statics the
  //                                    GUI leaves at defaults, e.g. LINE_LENGTH),
  // }
  const overrides = (mods && mods.overrides) || null;
  const preStart = (mods && mods.preStart) || [];
  const doc = url
    ? await (async () => { const r = await fetch(url); if (!r.ok) throw new Error(`cold-init fetch failed: HTTP ${r.status} (${url})`); return r.json(); })()
    : await fetchFirmwareJson("vd56g3_cold_init.json");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // The captured session ends with the GUI's Stop click (0x0202 <- 01), which
  // older extractions kept as the "final init step" — replaying it stops the
  // stream right after starting it (PROTOCOL.md §9.0). Skip any trailing stop.
  const steps = doc.steps.slice();
  while (steps.length && steps[steps.length - 1].op === "write"
         && steps[steps.length - 1].reg === REG.CMD_STOP_STREAM) {
    steps.pop();
  }

  const cmdRegs = new Set([REG.CMD_BOOT, REG.CMD_START_STREAM, REG.CMD_STOP_STREAM]);
  const writes = {};
  let bpp = 8;
  let injected = false;
  for (const step of steps) {
    if (step.op === "write") {
      // Inject extra writes right before the stream ACTUALLY starts. NB: the
      // captured init writes 0x0201 THREE times — twice with value 0x04
      // (THSENS_READ, thermal) and finally with 0x01 (START_STREAM). Matching
      // on the register alone fired at the first thermal read, and the init's
      // own later 0x044C<-0 / 0x044E<-1000 writes then clobbered the manual
      // exposure block back to auto (the "slider does nothing" bug). Match the
      // START_STREAM value too.
      if (step.reg === REG.CMD_START_STREAM && step.val.length === 1 && step.val[0] === 1
          && preStart.length && !injected) {
        injected = true;
        for (const w of preStart) {
          sensor.console.log(`~ slow-mode inject: reg 0x${w.reg.toString(16).padStart(4, "0")} <- [${w.val.join(",")}]`);
          await sensor.writeRegBytes(w.reg, w.val);
          await sleep(10);
        }
      }
      // Optional value overrides for steps already in the captured sequence.
      let val = step.val;
      if (overrides && Object.prototype.hasOwnProperty.call(overrides, step.reg)) {
        val = overrides[step.reg];
        sensor.console.log(`~ slow-mode override: reg 0x${step.reg.toString(16).padStart(4, "0")} ` +
                           `[${step.val.join(",")}] -> [${val.join(",")}]`);
      }
      writes[step.reg] = val;
      // Command registers need the self-clear handshake (PROTOCOL.md §9.0) —
      // a back-to-back replay outruns the sensor and commands get dropped.
      if (cmdRegs.has(step.reg) && val.length === 1) {
        await sensor.sendCommand(step.reg, val[0]);
        if (step.reg === REG.CMD_BOOT) {
          await sensor.waitFsm(2, 2000, "(post-CMD_BOOT)");
        } else if (step.reg === REG.CMD_START_STREAM && val[0] === 1) {
          await sensor.waitFsm(3, 5000, "(post-CMD_START_STREAM)");
        }
      } else {
        await sensor.writeRegBytes(step.reg, val);
        await sleep(10);
      }
    } else {
      const args = step.args || [];
      if (step.cmd === "CFG2WR" && args.length >= 11) bpp = args[10];
      await sensor.console.query(step.cmd, args);
      // The GUI spaces the NRST reset toggles 76-494 ms apart.
      await sleep(step.cmd === "NRST" ? 100 : 10);
    }
  }
  const u16 = (r) => { const b = writes[r]; return b && b.length >= 2 ? (b[0] | (b[1] << 8)) : 0; };
  const width = u16(REG.CTX0_OUT_ROI_X_END) - u16(REG.CTX0_OUT_ROI_X_START) + 1;
  const height = u16(REG.CTX0_OUT_ROI_Y_END) - u16(REG.CTX0_OUT_ROI_Y_START) + 1;
  // The replay ends at CMD_START_STREAM <- 1: the sensor is STREAMING now.
  return { width, height, bpp };
}

// =============================================================================
// Patch helpers (OPTIONAL — the sensor streams unpatched; kept for completeness)
// =============================================================================

/**
 * Apply the VT (vertical-timing) patch (PROTOCOL.md §6 step 7).
 * Fetches firmware/vd56g3_vt_patch.json (configurable URL) and replays:
 *   Write8(enter_patch_mode.reg, enter_patch_mode.value)   // 0x0203 <- 1
 *   for each { reg, val } in writes: Write8(reg, val)      // 3920 frames
 *   Write8(exit_patch_mode.reg, exit_patch_mode.value)     // 0x0203 <- 2
 * NOTE: the JSON stores reg/val as DECIMAL integers (e.g. enter reg 515 = 0x0203).
 * Addresses are non-contiguous, hence per-register writes (not a burst).
 * @param {Vd56g3} sensor
 * @param {string} [url] path to the VT patch JSON
 * @param {(done:number,total:number)=>void} [onProgress]
 * @returns {Promise<number>} number of register writes applied
 */
export async function loadVtPatch(sensor, url = "../firmware/vd56g3_vt_patch.json", onProgress) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`VT patch fetch failed: HTTP ${resp.status} (${url})`);
  const patch = await resp.json();
  const enter = patch.enter_patch_mode;
  const exit = patch.exit_patch_mode;
  const writes = patch.writes || [];

  // enter patch mode
  await sensor.write8(enter.reg, enter.value);
  // replay every write (Write8 — values are single bytes in this patch)
  for (let i = 0; i < writes.length; i++) {
    await sensor.write8(writes[i].reg, writes[i].val);
    if (onProgress && (i % 200 === 0 || i === writes.length - 1)) {
      onProgress(i + 1, writes.length);
    }
  }
  // exit patch mode
  await sensor.write8(exit.reg, exit.value);
  return writes.length;
}

/**
 * Apply the MAIN firmware patch (PROTOCOL.md §6 step 6 — THE documented blocker).
 * If firmware/vd56g3_main_patch.bin exists: stream its bytes to the sensor, then
 * Write8(0x0200, 1) (CMD_BOOT). If ABSENT: print a clear warning and continue in
 * "warm-sensor mode" (assume the sensor is already patched, like ST init_board) —
 * do NOT crash. This is the clean, well-labelled plug-in seam.
 *
 * The exact patch-RAM write transaction is [needs-capture]; the bytes are streamed
 * as chunked I2CWR bursts to the patch region. `chunk` bounds each I2CWR payload.
 *
 * @param {Vd56g3} sensor
 * @param {string} [url] path to the main patch blob
 * @param {(msg:string)=>void} [log]
 * @param {object} [opts] { baseReg=0x2000, chunk=250 }
 * @returns {Promise<{applied:boolean, bytes:number}>}
 */
export async function loadMainPatch(sensor, url = "../firmware/vd56g3_main_patch.bin", log = () => {}, opts = {}) {
  const baseReg = opts.baseReg ?? 0x2000; // patch RAM base region (PROTOCOL.md §6)
  const chunk = opts.chunk ?? 250;         // I2CWR burst payload bound

  let resp;
  try {
    resp = await fetch(url);
  } catch (err) {
    resp = null;
  }

  if (!resp || !resp.ok) {
    // ---- WARM-SENSOR MODE (fallback) --------------------------------------
    log(
      "WARNING: main FW patch (" + url + ") not found — continuing in " +
      "WARM-SENSOR MODE. This assumes the sensor is already patched/booted " +
      "(as ST init_board does). A cold-powered sensor will NOT stream. See " +
      "PROTOCOL.md §6 step 6 / STATE.md blocker."
    );
    return { applied: false, bytes: 0 };
  }

  // ---- COLD PATCH PATH (plug-in point) ------------------------------------
  const buf = new Uint8Array(await resp.arrayBuffer());
  // Fail loudly rather than silently wrapping the 16-bit register address. NOTE:
  // ST's load_binary uses a 64-bit register address, so patch RAM may not live
  // in 16-bit I2C space at all — the addressing is NEEDS-CAPTURE.
  if (baseReg + buf.length > 0x10000) {
    throw new Error(
      `main patch (${buf.length} bytes) from base 0x${baseReg.toString(16)} overruns ` +
      `the 16-bit I2C register window (0x10000). Patch-RAM addressing is ` +
      `needs-capture — pin it from a USBPcap capture before enabling this path.`
    );
  }
  log(`Main FW patch found (${buf.length} bytes) — streaming to patch RAM @0x${baseReg.toString(16)}...`);
  // Stream as chunked I2CWR bursts. Exact addressing is [needs-capture]; we write
  // sequential 16-bit patch-RAM offsets from baseReg. Adjust here once a capture
  // pins the real transaction down.
  let off = 0;
  while (off < buf.length) {
    const n = Math.min(chunk, buf.length - off);
    const reg = (baseReg + off) & 0xffff;
    const args = new Uint8Array(3 + n);
    args[0] = sensor.i2c;
    args[1] = (reg >> 8) & 0xff; // reg_hi (big-endian address)
    args[2] = reg & 0xff;        // reg_lo
    args.set(buf.subarray(off, off + n), 3);
    await sensor.console.query("I2CWR", args);
    off += n;
  }
  // Boot the sensor after the patch load.
  await sensor.write8(REG.CMD_BOOT, 1); // CMD_BOOT 0x0200 <- 1
  log(`Main FW patch streamed (${buf.length} bytes) + CMD_BOOT issued.`);
  return { applied: true, bytes: buf.length };
}

// =============================================================================
// On-wire frame chunking (PROTOCOL.md §5.0 — hardware-confirmed 2026-07-16)
// =============================================================================
// The CX3 delivers each frame as 16384-byte chunks (final chunk short):
//   [16-byte header][payload_len bytes][16-byte footer (absent on last chunk)]
// header: magic 10 01 02 00 | u16 chunk idx (1-based) | u16 | u32 frame seq |
//         u32 payload_len (0x3FE0 = 16352 for full chunks).
// 1120x1360 RAW10 -> payload 1,906,800 B -> 117 chunks -> 1,910,528 B on wire.

export const WIRE_MAGIC = Object.freeze([0x10, 0x01, 0x02, 0x00]);
export const WIRE_CHUNK_STRIDE = 16384;
export const WIRE_CHUNK_PAYLOAD = 16352;

/** Total on-wire bytes for a frame whose post-driver payload is `payloadSize`. */
export function wireFrameSize(payloadSize) {
  const chunks = Math.ceil(payloadSize / WIRE_CHUNK_PAYLOAD);
  return payloadSize + chunks * 16 + (chunks - 1) * 16;
}

/** True if `data` begins with a frame's FIRST chunk header (magic + chunk idx 1). */
export function isFrameStart(data) {
  return data.length >= 6
    && data[0] === 0x10 && data[1] === 0x01 && data[2] === 0x02 && data[3] === 0x00
    && data[4] === 0x01 && data[5] === 0x00;
}

/**
 * Reassemble the post-driver frame payload from the CX3 wire chunking.
 * Pass-through (frameSeq=null) if `raw` does not start with the chunk magic.
 * @param {Uint8Array} raw
 * @returns {{payload: Uint8Array, frameSeq: (number|null)}}
 */
export function stripWireChunks(raw) {
  const isMagic = (o) => raw[o] === 0x10 && raw[o + 1] === 0x01 && raw[o + 2] === 0x02 && raw[o + 3] === 0x00;
  if (raw.length < 16 || !isMagic(0)) return { payload: raw, frameSeq: null };
  const out = new Uint8Array(raw.length); // upper bound; trimmed below
  let filled = 0;
  let frameSeq = null;
  for (let off = 0; off + 16 <= raw.length; off += WIRE_CHUNK_STRIDE) {
    if (!isMagic(off)) throw new Error(`wire chunk magic missing at offset ${off}`);
    if (frameSeq === null) {
      frameSeq = raw[off + 8] | (raw[off + 9] << 8) | (raw[off + 10] << 16) | (raw[off + 11] << 24);
    }
    const plen = raw[off + 12] | (raw[off + 13] << 8) | (raw[off + 14] << 16) | (raw[off + 15] << 24);
    if (plen > WIRE_CHUNK_PAYLOAD) throw new Error(`chunk at ${off} declares payload ${plen}`);
    out.set(raw.subarray(off + 16, off + 16 + plen), filled);
    filled += plen;
  }
  return { payload: out.subarray(0, filled), frameSeq };
}

// =============================================================================
// Grayscale PNG encoder (dependency-free) — full-bit-depth export
// =============================================================================
// PNG grayscale supports bit depths 8 and 16 (no native 10). RAW10 samples are
// stored LEFT-ALIGNED in 16-bit: v16 = (v<<6)|(v>>4), so 0->0, 1023->65535 and
// the original is recovered exactly with v = v16>>6. zlib layer comes from the
// browser-native CompressionStream("deflate") (the "deflate" format is
// zlib-wrapped per the Compression Streams spec, which is what IDAT needs).

const _crcTable = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();

function _crc32(...arrays) {
  let c = 0xFFFFFFFF;
  for (const a of arrays) {
    for (let i = 0; i < a.length; i++) c = _crcTable[(c ^ a[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xFFFFFFFF) >>> 0;
}

function _pngChunk(type, data) {
  const out = new Uint8Array(12 + data.length);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, data.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  dv.setUint32(8 + data.length, _crc32(out.subarray(4, 8), data));
  return out;
}

/**
 * Encode a grayscale PNG from row-major samples.
 * @param {Uint8Array|Uint16Array} pixels row-major samples, values 0..maxValue
 * @param {number} width
 * @param {number} height
 * @param {number} maxValue 255 -> 8-bit PNG; anything larger -> 16-bit PNG
 *   with samples scaled to full 16-bit range (invertible for 1023: v16>>6).
 * @returns {Promise<Blob>} image/png
 */
export async function encodeGrayPng(pixels, width, height, maxValue) {
  const depth16 = maxValue > 255;
  const stride = width * (depth16 ? 2 : 1);
  const raw = new Uint8Array(height * (1 + stride));
  let o = 0;
  for (let y = 0; y < height; y++) {
    raw[o++] = 0; // per-scanline filter byte: 0 = None
    const base = y * width;
    if (depth16) {
      for (let x = 0; x < width; x++) {
        const v = pixels[base + x];
        const v16 = maxValue === 1023 ? ((v << 6) | (v >> 4))
                                      : Math.round((v * 65535) / maxValue);
        raw[o++] = (v16 >> 8) & 0xff;  // PNG samples are big-endian
        raw[o++] = v16 & 0xff;
      }
    } else {
      for (let x = 0; x < width; x++) raw[o++] = pixels[base + x];
    }
  }
  const zstream = new Blob([raw]).stream().pipeThrough(new CompressionStream("deflate"));
  const idat = new Uint8Array(await new Response(zstream).arrayBuffer());

  const ihdr = new Uint8Array(13);
  const dv = new DataView(ihdr.buffer);
  dv.setUint32(0, width);
  dv.setUint32(4, height);
  ihdr[8] = depth16 ? 16 : 8; // bit depth
  ihdr[9] = 0;                // color type 0 = grayscale
  // bytes 10-12: compression 0, filter 0, interlace 0

  const sig = new Uint8Array([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);
  return new Blob(
    [sig, _pngChunk("IHDR", ihdr), _pngChunk("IDAT", idat), _pngChunk("IEND", new Uint8Array(0))],
    { type: "image/png" },
  );
}

// =============================================================================
// Frame decode (PROTOCOL.md §5, mirrors ST vdx6gx_frame_decoding.py exactly)
// =============================================================================

/**
 * Extract an 8-bit status-line register value from a raw frame (PROTOCOL.md §5).
 *   R <  0x7d : line 1, offset 2*R + 6
 *   R >= 0x7d : line 2, offset frame_width_bytes + 2*(R-0x7d) + 6
 * @param {Uint8Array} raw raw frame bytes
 * @param {number} reg status register address
 * @param {number} widthBytes frame width in bytes (bpp*width/8), needed for line 2
 */
export function statusValue8(raw, reg, widthBytes) {
  const off = reg < 0x7d
    ? 2 * reg + 6
    : widthBytes + 2 * (reg - 0x7d) + 6;
  return raw[off];
}

/** 16-bit status value (LSB-first), spanning consecutive register slots. */
export function statusValue16(raw, reg, widthBytes) {
  const lo = statusValue8(raw, reg, widthBytes);
  const hi = statusValue8(raw, reg + 1, widthBytes);
  return lo + 256 * hi;
}

/**
 * Unpack ST RAW10 packing (PROTOCOL.md §5, ST decode_raw_10): every 5 bytes
 * encode 4 pixels. bytes 0-3 are the high 8 bits of px0-3; byte 4 holds the
 * four pairs of low 2 bits: px_i = (byte_i << 2) | ((byte4 >> (2*i)) & 3).
 * Returns a Uint16Array of 10-bit pixels for `rows` rows of `widthBytes` bytes.
 * @param {Uint8Array} img image buffer (status lines already stripped)
 * @param {number} rows number of rows (y_size)
 * @param {number} widthBytes bytes per row (bpp*width/8, bpp=10)
 * @returns {Uint16Array}
 */
export function decodeRaw10(img, rows, widthBytes) {
  const pxPerRow = (widthBytes / 5) * 4; // 5 bytes -> 4 px
  const out = new Uint16Array(rows * pxPerRow);
  let o = 0;
  for (let r = 0; r < rows; r++) {
    const base = r * widthBytes;
    for (let b = 0; b + 4 < widthBytes; b += 5) {
      const i = base + b;
      const b0 = img[i], b1 = img[i + 1], b2 = img[i + 2], b3 = img[i + 3], b4 = img[i + 4];
      out[o++] = (b0 << 2) | ((b4 >> 0) & 0x03);
      out[o++] = (b1 << 2) | ((b4 >> 2) & 0x03);
      out[o++] = (b2 << 2) | ((b4 >> 4) & 0x03);
      out[o++] = (b3 << 2) | ((b4 >> 6) & 0x03);
    }
  }
  return out;
}

/**
 * Decode one raw frame (PROTOCOL.md §5, mirrors ST decode_frame).
 * Strips the 2 status lines, reads bpp from status reg 0x5B and y_size from
 * 0x94, then RAW8 = passthrough / RAW10 = 5->4 unpack.
 * @param {Uint8Array} raw the full raw frame payload from the video bulk-IN
 * @param {number} width image width in pixels (the CFG width used for framing)
 * @param {number} [bppHint] fallback bpp if the status line is unreadable
 * @returns {{
 *   width:number, height:number, bpp:number,
 *   frameCounter:number, currentContext:number,
 *   pixels:(Uint8Array|Uint16Array), // grayscale samples, row-major
 *   maxValue:number
 * }}
 */
export function decodeFrame(rawWire, width, bppHint = 8) {
  // De-chunk the CX3 wire framing first (PROTOCOL.md §5.0); pass-through for
  // post-driver payloads (no magic).
  const { payload: raw, frameSeq } = stripWireChunks(rawWire);

  // bits per pixel from the status line (must be 8 or 10). Fall back to hint if
  // the status line looks wrong (e.g. a truncated / not-yet-valid frame).
  let bpp = statusValue8(raw, STATUS_REG.FORMAT_CTRL, 0);
  if (bpp !== 8 && bpp !== 10) bpp = bppHint;

  const widthBytes = Math.floor((bpp * width) / 8);

  // CX3 4-byte packing constraint (PROTOCOL.md §5): width*bpp % 32 === 0.
  if ((width * bpp) % 32 !== 0) {
    throw new Error(
      `frame width ${width} invalid for ${bpp}bpp: width*bpp must be a ` +
      `multiple of 32 (CX3 4-byte packing constraint).`
    );
  }

  // y_size, frame counter, context from the status lines (LSB-first).
  const height = statusValue16(raw, STATUS_REG.OUT_ROI_Y_SIZE, widthBytes);
  const frameCounter = statusValue16(raw, STATUS_REG.FRAME_COUNTER, widthBytes);
  const currentContext = statusValue8(raw, STATUS_REG.CURRENT_CONTEXT, widthBytes);

  // Image data begins after the 2 status lines.
  const imgStart = 2 * widthBytes;
  const imgBytes = raw.subarray(imgStart, imgStart + height * widthBytes);

  let pixels, maxValue;
  if (bpp === 10) {
    pixels = decodeRaw10(imgBytes, height, widthBytes);
    maxValue = 1023;
  } else {
    // RAW8: 1 byte per pixel, passthrough (copy so callers own the buffer).
    pixels = new Uint8Array(imgBytes);
    maxValue = 255;
  }

  return { width, height, bpp, frameCounter, currentContext, pixels, maxValue, frameSeq };
}
