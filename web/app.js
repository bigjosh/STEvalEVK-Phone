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
  replayColdInit, decodeFrame,
} from "./protocol.js";

// VID:PID of the EVK (PROTOCOL.md §1).
const VENDOR_ID = 0x0553;
const PRODUCT_ID = 0x040a;

// ---------------------------------------------------------------------------
// Tiny DOM + logging helpers
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const logEl = () => $("log");

function log(msg) {
  const el = logEl();
  const ts = new Date().toISOString().slice(11, 23); // HH:MM:SS.mmm
  el.textContent += `[${ts}] ${msg}\n`;
  el.scrollTop = el.scrollHeight;
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

    // ---- Cold-init: replay the hardware-captured sequence (PROTOCOL.md §9) --
    // No FW patch — the VD56G3 streams unpatched. This plays the exact ordered
    // commands ST's GUI sent (register writes via I2CWRRD rdlen=0, CLKWR/CFG2WR/
    // NRST/IOSET/IOCFGWR), ending at CMD_STREAMING<-1.
    setStatus("Replaying captured cold-init...");
    const geo = await replayColdInit(sensor);  // resolves firmware/ in dev or deployed
    const { width, height } = geo;
    bpp = geo.bpp;
    log(`Cold-init replayed: streaming ${width}x${height} bpp=${bpp} (no patch).`);

    // ---- Read ONE frame off the video bulk-IN ----------------------------
    setStatus("Reading one frame...");
    const widthBytes = Math.floor((bpp * width) / 8);
    // Total payload = (2 status lines + height rows) * widthBytes.
    const target = (2 + height) * widthBytes;
    const raw = await readOneFrame(target);
    log(`Read ${raw.length}/${target} bytes from video bulk-IN.`);

    // ---- 12. Stop streaming ----------------------------------------------
    try { await sensor.stopStream(); log("Streaming stopped (CMD_STREAMING <- 0)."); }
    catch (err) { log(`stopStream warning: ${err.message}`); }

    // ---- Decode + render (PROTOCOL.md §5) --------------------------------
    setStatus("Decoding frame...");
    const frame = decodeFrame(raw, width, bpp);
    log(`Decoded: ${frame.width}x${frame.height}, ${frame.bpp}bpp, ` +
        `frame#${frame.frameCounter}, ctx=${frame.currentContext}.`);
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

/**
 * Read one full frame by looping transferIn on the video endpoint until we've
 * reassembled `target` bytes (or a transfer returns nothing). clearHalt+retry is
 * handled inside Cx3Console.readVideo. A short read count guard prevents an
 * infinite loop if the pipe goes quiet.
 */
async function readOneFrame(target) {
  const buf = new Uint8Array(target);
  let filled = 0;
  let emptyReads = 0;
  // Request in reasonably large chunks; the device caps to its packet size.
  const chunk = 512 * 1024;
  // Bulk-IN reads must request a whole multiple of the endpoint's max packet
  // size, else Chrome can complete with status "babble" (overflow). Round up.
  const pkt = state.eps.videoPacketSize || 1024;
  while (filled < target) {
    let want = Math.min(chunk, target - filled);
    want = Math.ceil(want / pkt) * pkt; // multiple of packet size (may overshoot; clamped on copy)
    const part = await state.console.readVideo(want);
    if (part.length === 0) {
      if (++emptyReads > 8) {
        log("Video pipe returned no data repeatedly — stopping frame read.");
        break;
      }
      continue;
    }
    emptyReads = 0;
    const n = Math.min(part.length, target - filled);
    buf.set(part.subarray(0, n), filled);
    filled += n;
  }
  return filled === target ? buf : buf.subarray(0, filled);
}

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
window.addEventListener("DOMContentLoaded", () => {
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
