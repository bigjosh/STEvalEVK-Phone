// app.js — UI wiring for the STEVAL-EVK-U0I WebUSB capture app (Phase 2)
// =============================================================================
// Drives the page in index.html: Connect (enumerate + claim + endpoint
// discovery), Capture (cold init -> stream -> one frame -> canvas -> JPEG).
// All protocol logic lives in protocol.js; this file is UI + orchestration.
//
// See PROTOCOL.md §6 for the cold-init -> first-frame sequence this mirrors.
// WebUSB requires a secure context (HTTPS / GitHub Pages) and Chrome for Android.
// =============================================================================

import {
  Cx3Console, Vd56g3,
  replayColdInit, decodeFrame, wireFrameSize,
} from "./protocol.js";

// VID:PID of the EVK (PROTOCOL.md §1).
const VENDOR_ID = 0x0553;
const PRODUCT_ID = 0x040a;

// ---------------------------------------------------------------------------
// Tiny DOM + logging helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const logEl = () => $("log");

// Buffered logging: on phones, a DOM append + reflow per line adds real
// latency between USB transactions. Lines are queued and flushed in one DOM
// write every 100 ms; the log is trimmed to the last 500 lines.
const _logBuf = [];
let _logTimer = null;

function _flushLog() {
  _logTimer = null;
  if (!_logBuf.length) return;
  const el = logEl();
  el.textContent += _logBuf.join("\n") + "\n";
  _logBuf.length = 0;
  const lines = el.textContent.split("\n");
  if (lines.length > 500) el.textContent = lines.slice(-500).join("\n");
  el.scrollTop = el.scrollHeight;
}

function log(msg) {
  const ts = new Date().toISOString().slice(11, 23); // HH:MM:SS.mmm
  _logBuf.push(`[${ts}] ${msg}`);
  if (!_logTimer) _logTimer = setTimeout(_flushLog, 100);
}

function setStatus(msg) { $("status").textContent = msg; }

// Shared app state.
const state = {
  device: null,
  claimedInterfaces: [],
  console: null,
  sensor: null,
  eps: { cmdOut: null, ansIn: null, videoIn: null },
};

// ===========================================================================
// Endpoint auto-discovery (PROTOCOL.md §1)
// ---------------------------------------------------------------------------
// The console needs 3 bulk endpoints, discovered from the interface descriptors:
//   - first bulk-OUT           = command
//   - matching (first) bulk-IN = answer
//   - the larger / separate bulk-IN = video
// Never hardcoded — user can override via the number inputs on the page.
// ===========================================================================
function discoverEndpoints(configuration) {
  // The EVK is a composite USB-3 device (class 0xEF) that SPLITS its bulk
  // endpoints across two vendor interfaces (PROTOCOL.md §1, confirmed from the
  // device descriptor): interface 1 carries the console (OUT 0x05 / IN 0x85),
  // interface 0 carries the video (IN 0x83). So we scan ALL interfaces, not one.
  const bulkOut = [];
  const bulkIn = [];
  for (const iface of configuration.interfaces) {
    const alt = iface.alternate || iface.alternates[0];
    for (const ep of alt.endpoints) {
      if (ep.type !== "bulk") continue;
      const rec = { num: ep.endpointNumber, iface: iface.interfaceNumber, pkt: ep.packetSize || 1024 };
      (ep.direction === "out" ? bulkOut : bulkIn).push(rec);
    }
  }
  if (!bulkOut.length || !bulkIn.length) {
    throw new Error(`need >=1 bulk-OUT and >=1 bulk-IN across the config; ` +
      `found ${bulkOut.length} OUT / ${bulkIn.length} IN.`);
  }

  const cmd = bulkOut[0];                                    // command OUT (0x05, if1)
  // answer IN: prefer the bulk-IN on the SAME interface as the command OUT
  // (0x85 on if1); else same endpoint number; else first.
  const ans = bulkIn.find((e) => e.iface === cmd.iface)
           || bulkIn.find((e) => e.num === cmd.num)
           || bulkIn[0];
  // video IN: a bulk-IN on a DIFFERENT interface than the console (0x83 on if0);
  // else any other bulk-IN; else share the answer pipe.
  const video = bulkIn.find((e) => e.iface !== ans.iface)
             || bulkIn.find((e) => e !== ans)
             || ans;

  return {
    cmdOut: cmd.num, ansIn: ans.num, videoIn: video.num,
    videoPacketSize: video.pkt,
    // interfaces we must claim to use these endpoints:
    interfaces: [...new Set([cmd.iface, ans.iface, video.iface])],
  };
}

/** Read the manual endpoint override inputs; blank = auto. */
function readOverrides() {
  const num = (id) => {
    const v = $(id).value.trim();
    if (v === "") return null;
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : null;
  };
  return { cmdOut: num("epCmd"), ansIn: num("epAns"), videoIn: num("epVideo") };
}

function applyOverrides(auto) {
  const ov = readOverrides();
  return {
    cmdOut: ov.cmdOut ?? auto.cmdOut,
    ansIn: ov.ansIn ?? auto.ansIn,
    videoIn: ov.videoIn ?? auto.videoIn,
    videoPacketSize: auto.videoPacketSize || 1024,
    interfaces: auto.interfaces || [],
  };
}

// ===========================================================================
// Connect: request device -> open -> select config -> claim -> discover EPs
// ===========================================================================
async function onConnect() {
  try {
    if (!("usb" in navigator)) {
      setStatus("WebUSB unavailable — use Chrome for Android over HTTPS.");
      log("ERROR: navigator.usb is undefined. WebUSB needs a secure context (HTTPS) and a supporting browser.");
      return;
    }

    setStatus("Requesting device...");
    let device;
    try {
      device = await navigator.usb.requestDevice({
        filters: [{ vendorId: VENDOR_ID, productId: PRODUCT_ID }],
      });
    } catch (err) {
      // User dismissed the chooser, or no device matched.
      setStatus("No device selected.");
      log(`Device chooser cancelled or no match: ${err.message}`);
      return;
    }

    // Release any device we still hold from a previous attempt in this tab — a
    // lingering claim is a common Android "Unable to claim interface" (EBUSY)
    // source when the user retries Connect.
    if (state.device) {
      try {
        for (const i of state.claimedInterfaces) { try { await state.device.releaseInterface(i); } catch (_) {} }
        await state.device.close();
      } catch (_) { /* ignore */ }
    }
    state.device = device;
    state.claimedInterfaces = [];
    log(`Selected: ${device.productName || "device"} ` +
        `(VID 0x${device.vendorId.toString(16)}, PID 0x${device.productId.toString(16)})`);

    await device.open();
    log("Device opened.");

    // ALWAYS issue SET_CONFIGURATION, even when Chrome already reports config 1
    // active. On Android's usbfs backend, skipping it commonly leaves the handle
    // without proper interface ownership and claimInterface then fails.
    try {
      await device.selectConfiguration(1);
    } catch (err) {
      log(`selectConfiguration(1): ${err.message} (continuing)`);
    }
    log(`Configuration ${device.configuration ? device.configuration.configurationValue : "?"} active.`);

    // Discover the 3 bulk endpoints across ALL interfaces, then claim every
    // interface they live on (console on if1, video on if0 — see PROTOCOL.md §1).
    const auto = discoverEndpoints(device.configuration);
    state.eps = applyOverrides(auto);
    const claimed = [];
    for (const ifnum of auto.interfaces) {
      let ok = false;
      for (let attempt = 1; attempt <= 4 && !ok; attempt++) {
        try {
          await device.claimInterface(ifnum);
          // Put the interface's endpoints into the "selected alternate" state so
          // transfers are allowed (else: "endpoint is not part of a claimed and
          // selected alternate interface").
          try { await device.selectAlternateInterface(ifnum, 0); } catch (_) { /* alt 0 usually implicit */ }
          ok = true;
          claimed.push(ifnum);
          log(`Claimed interface ${ifnum}.`);
        } catch (err) {
          log(`claimInterface(${ifnum}) attempt ${attempt}/4: ${err.message}`);
          await new Promise((r) => setTimeout(r, 200));
        }
      }
      if (!ok) {
        for (const c of claimed) { try { await device.releaseInterface(c); } catch (_) {} }
        state.claimedInterfaces = [];
        setStatus(`Could not claim interface ${ifnum}.`);
        log(`ABORT: interface ${ifnum} could not be claimed ("Unable to claim interface" = usbfs EBUSY).`);
        log(`Diagnose on the phone: open chrome://device-log and find "Failed to claim interface" + its errno/driver.`);
        log(`Try: (1) unplug/replug the EVK, close other tabs/apps that opened it, retry;`);
        log(`     (2) enable chrome://flags/#automatic-usb-detach, relaunch Chrome, retry.`);
        log(`If a kernel driver is bound and won't detach, WebUSB can't force it — use the Phase-1`);
        log(`Termux/pyusb path (docs/TERMUX_SETUP.md), which CAN force-detach and claim.`);
        return;
      }
    }
    state.claimedInterfaces = claimed;
    // Reflect discovered values back into the (blank) inputs as placeholders.
    if ($("epCmd").value === "") $("epCmd").placeholder = String(auto.cmdOut);
    if ($("epAns").value === "") $("epAns").placeholder = String(auto.ansIn);
    if ($("epVideo").value === "") $("epVideo").placeholder = String(auto.videoIn);
    log(`Endpoints -> cmdOut #${state.eps.cmdOut} (OUT), ansIn #${state.eps.ansIn} (IN), ` +
        `videoIn #${state.eps.videoIn} (IN) on interfaces [${auto.interfaces.join(", ")}]`);

    // ---- Link-speed check (decisive for video) ---------------------------
    // At SuperSpeed the bulk endpoints report 1024-byte max packets; at USB-2
    // High Speed they report 512. The console works at any speed, but the
    // ~115 MB/s video stream is IMPOSSIBLE over USB 2 — the CX3 just
    // overflows and stalls EP 0x83 forever ("video attempt ... stall").
    const pkt = state.eps.videoPacketSize || 0;
    if (pkt === 512) {
      setStatus("USB 2 link — using slow mode.");
      log(`*** LINK IS USB 2 (HighSpeed): video max packet = 512. At full speed the`);
      log(`*** video overflows the CX3 and stalls. Slow mode retunes the sensor's VT`);
      log(`*** clock so full-res frames fit through USB 2 at a lower frame rate.`);
      if ($("slowmode").value === "1") {
        $("slowmode").value = "6";
        log(`Auto-selected "USB-2 slow mode: 6x" (~19 MB/s). Change it in Options if needed.`);
      }
    } else {
      log(`Link looks SuperSpeed (video max packet = ${pkt}). Good for full-rate video.`);
    }

    // Build the console + sensor helpers.
    state.console = new Cx3Console(device, state.eps, log);
    state.sensor = new Vd56g3(state.console);

    $("capture").disabled = false;
    setStatus("Connected. Ready to capture.");

    // Handle unexpected disconnects.
    navigator.usb.addEventListener("disconnect", onDisconnect);
  } catch (err) {
    setStatus("Connect failed.");
    log(`ERROR (connect): ${err.message}`);
  }
}

function onDisconnect(event) {
  if (state.device && event.device === state.device) {
    log("Device disconnected.");
    setStatus("Device disconnected.");
    $("capture").disabled = true;
    state.device = null;
    state.console = null;
    state.sensor = null;
  }
}

// ===========================================================================
// Capture: VERSION -> cold init -> start stream -> read ONE frame -> render
// ===========================================================================
async function onCapture() {
  if (!state.sensor) { setStatus("Not connected."); return; }
  $("capture").disabled = true;
  try {
    let bpp = parseInt($("bpp").value, 10) === 10 ? 10 : 8; // replay overrides this
    const sensor = state.sensor;

    // ---- 2. VERSION sanity check (PROTOCOL.md §6 step 2) -----------------
    setStatus("VERSION check...");
    try {
      const { text } = await state.console.query("VERSION");
      log(`VERSION reply: ${text.trim() || "(empty)"} [reply grammar needs-capture]`);
    } catch (err) {
      log(`VERSION failed (continuing): ${err.message}`);
    }

    // ---- USB-2 slow mode: LINE STRETCH (experimental) ---------------------
    // Full-res over a USB-2 High-Speed link by slowing pixel OUTPUT, without
    // touching the clock tree (a VT_CLK_DIV change wedged the sensor —
    // SYSTEM_FSM read 0xFF, i.e. dead I2C). LINE_LENGTH (0x0300, statics page,
    // hardware readback = 1236 VT clocks = 7.69 us/line = 60 fps at 2168
    // lines/frame) sets the time between line readouts: multiplying it by N
    // stretches line blanking, dividing the wire rate and fps by N while the
    // PLL/MIPI stay at ST's exact captured values. Exposure is counted in
    // LINES (0x044E=1000) and each line is now N x longer, so scale it 1/N.
    const slowFactor = parseInt($("slowmode").value, 10) || 1;
    const LINE_LENGTH_DEFAULT = 1236;
    let mods = null;
    if (slowFactor > 1) {
      const lineLen = LINE_LENGTH_DEFAULT * slowFactor;   // 4944 / 7416 / 14832 (u16 ok)
      const exp = Math.max(1, Math.round(1000 / slowFactor));
      mods = {
        overrides: { 0x044E: [exp & 0xff, (exp >> 8) & 0xff] },
        preStart: [{ reg: 0x0300, val: [lineLen & 0xff, (lineLen >> 8) & 0xff] }],
      };
      log(`USB-2 slow mode ${slowFactor}x (line stretch): LINE_LENGTH 0x0300 ` +
          `${LINE_LENGTH_DEFAULT} -> ${lineLen}, COARSE_EXPOSURE 1000 -> ${exp} ` +
          `(~${(114.6 / slowFactor).toFixed(1)} MB/s, ~${(60 / slowFactor).toFixed(0)} fps). ` +
          `No clock changes. EXPERIMENTAL.`);
    }

    // ---- Cold-init: replay the hardware-captured sequence (PROTOCOL.md §9) --
    // No FW patch — the VD56G3 streams unpatched. Plays the exact ordered
    // commands ST's GUI sent, with the §9.0 self-clear command handshake, and
    // ends at CMD_START_STREAM<-1: the sensor is STREAMING when this returns.
    setStatus("Replaying captured cold-init...");
    const geo = await replayColdInit(sensor, undefined, mods);
    const { width, height } = geo;
    bpp = geo.bpp;
    log(`Cold-init replayed: streaming ${width}x${height} bpp=${bpp} (no patch).`);

    // Slow-mode readback: did the sensor ACCEPT the stretched line length?
    // (If it clamps back to ~1236, full-rate video will stall on USB 2 and we
    // know to try a different register rather than a different value.)
    if (mods) {
      try {
        const ll = await sensor.read16(0x0300);
        const fsm = await sensor.read8(0x0028);
        log(`Slow-mode readback: LINE_LENGTH(0x0300)=${ll} ` +
            `(wanted ${LINE_LENGTH_DEFAULT * slowFactor}), SYSTEM_FSM=${fsm}.`);
      } catch (err) {
        log(`Slow-mode readback failed: ${err.message}`);
      }
    }

    // ---- Read ONE frame off the video bulk-IN (PROTOCOL.md §5.0) ---------
    // PRE-QUEUED single transfer: stop the stream, queue the frame-sized read,
    // then start the stream so the first frame lands in the already-pending
    // transfer. This removes the clearHalt->read race that Android's WebUSB
    // IPC latency loses against the CX3's few-ms overflow window.
    setStatus("Reading one frame...");
    const widthBytes = Math.floor((bpp * width) / 8);
    const payloadSize = (2 + height) * widthBytes;   // 2 status lines + rows
    const wireTotal = wireFrameSize(payloadSize);    // + 16 B header/footer per 16 KB chunk
    log(`Expecting ${wireTotal} wire bytes (payload ${payloadSize}).`);
    const t0 = performance.now();
    const raw = await state.console.readFramePrequeued(sensor, wireTotal);
    const dt = (performance.now() - t0) / 1000;
    log(`Read ${raw.length} wire bytes in ${dt.toFixed(2)} s ` +
        `(~${(raw.length / dt / 1e6).toFixed(1)} MB/s incl. stream start).`);

    // ---- Stop streaming (0x0202 <- 1, self-clearing — PROTOCOL.md §9.0) ---
    try { await sensor.stopStream(); log("Streaming stopped (CMD_STOP_STREAM 0x0202 <- 1)."); }
    catch (err) { log(`stopStream warning: ${err.message}`); }

    // ---- Decode + render (PROTOCOL.md §5: de-chunk, strip status lines) ---
    setStatus("Decoding frame...");
    const frame = decodeFrame(raw, width, bpp);
    log(`Decoded: ${frame.width}x${frame.height}, ${frame.bpp}bpp, ` +
        `frame#${frame.frameCounter}, seq=${frame.frameSeq}, ctx=${frame.currentContext}.`);
    renderFrame(frame);

    setStatus(`Frame captured: ${frame.width}x${frame.height} ${frame.bpp}bpp.`);
  } catch (err) {
    setStatus("Capture failed.");
    log(`ERROR (capture): ${err.message}`);
    // Best-effort stop so the sensor isn't left streaming.
    try { await state.sensor.stopStream(); } catch (_) { /* ignore */ }
  } finally {
    $("capture").disabled = false;
  }
}

// (Frame reading lives in Cx3Console.readFrame — one single frame-sized
// transfer per attempt, per PROTOCOL.md §5.0. The old chunked reassembly loop
// was removed: the CX3 stalls EP 0x83 whenever the host pauses mid-frame, so
// chunked reads can only ever produce torn frames.)

// ===========================================================================
// Render a decoded grayscale frame to the canvas + wire up the JPEG download.
// ===========================================================================
function renderFrame(frame) {
  const canvas = $("preview");
  canvas.width = frame.width;
  canvas.height = frame.height;
  const ctx = canvas.getContext("2d");
  const imgData = ctx.createImageData(frame.width, frame.height);
  const px = frame.pixels;
  const scale = 255 / frame.maxValue; // normalize 10-bit -> 8-bit for display
  const dst = imgData.data;
  const count = Math.min(px.length, frame.width * frame.height);
  for (let i = 0; i < count; i++) {
    const g = frame.maxValue === 255 ? px[i] : Math.min(255, (px[i] * scale) | 0);
    const j = i * 4;
    dst[j] = g; dst[j + 1] = g; dst[j + 2] = g; dst[j + 3] = 255;
  }
  ctx.putImageData(imgData, 0, 0);

  // Export a JPEG via canvas.toBlob and enable the download link.
  canvas.toBlob((blob) => {
    if (!blob) { log("toBlob returned null — JPEG export unavailable."); return; }
    const link = $("download");
    if (link.href) URL.revokeObjectURL(link.href);
    link.href = URL.createObjectURL(blob);
    link.download = `evk_frame_${frame.frameCounter}.jpg`;
    link.classList.remove("disabled");
    link.textContent = `Download JPEG (${(blob.size / 1024).toFixed(1)} KB)`;
    log(`JPEG ready (${blob.size} bytes).`);
  }, "image/jpeg", 0.92);
}

// ---------------------------------------------------------------------------
// Wire up buttons on load.
// ---------------------------------------------------------------------------
const APP_BUILD = "2026-07-16e (slow mode via LINE_LENGTH stretch — no clock changes; buffered log)";

window.addEventListener("DOMContentLoaded", () => {
  log(`App build: ${APP_BUILD}`);
  $("connect").addEventListener("click", onConnect);
  $("capture").addEventListener("click", onCapture);
  if (!("usb" in navigator)) {
    setStatus("WebUSB not supported in this browser.");
    log("WebUSB not available. Requires Chrome for Android (or desktop Chrome) over HTTPS.");
  } else {
    setStatus("Ready. Click Connect.");
    log("WebUSB available. Click Connect to choose the EVK.");
  }
});
