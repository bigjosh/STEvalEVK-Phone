"""
VD56G3 ("S6G3") sensor driver — register I/O + CSI/stream config + cold-init.

Reimplements the register-access and stream-setup layer that ST provides in
``libst_brightsense_sdk_vdx6gx.so`` (``Comms::Write8/16/32``, ``ReadN``,
``WriteBurst``, ``configureCsiReceiver``, ``start_stream``). All sensor access
is *I²C tunnelled* through the CX3 console (PROTOCOL.md §2–3). This module owns
none of the USB details — it speaks only :class:`evk.cx3_console.Cx3Console`.

Endianness (PROTOCOL.md §3.1):

  * Register **ADDRESS** is 16-bit **big-endian** on the wire (addr_hi, addr_lo)
    — high-confidence, [needs-capture] to reach 100%.
  * Register **VALUE** is **little-endian**, LSB first (1/2/4 bytes) — confirmed
    (matches the LSB-first status-line readback in ST's decoder).

Register writes map to ``I2CWR``; reads map to ``I2CWRRD`` (repeated-start).
See PROTOCOL.md §3.1 for the exact byte layouts, restated inline below.
"""

from __future__ import annotations

import logging
from typing import Tuple

from firmware import vd56g3_registers as reg
from . import patch as patchmod
from .cx3_console import Cx3Console

logger = logging.getLogger("evk.vd56g3")

# Default per-write burst chunk for WriteBurst (ST's Python wrapper default).
DEFAULT_BURST_SIZE = 256


def _addr_bytes(register_address: int) -> bytes:
    """16-bit register ADDRESS, big-endian on the wire: [hi, lo] (PROTOCOL.md §3.1)."""
    if not 0 <= register_address <= 0xFFFF:
        raise ValueError(f"register address out of range: 0x{register_address:X}")
    return bytes(((register_address >> 8) & 0xFF, register_address & 0xFF))


def _value_bytes(value: int, nbytes: int) -> bytes:
    """N-byte register VALUE, little-endian / LSB first (PROTOCOL.md §3.1)."""
    if value < 0:
        raise ValueError("register value must be unsigned")
    if value >= (1 << (8 * nbytes)):
        raise ValueError(f"value 0x{value:X} does not fit in {nbytes} byte(s)")
    return value.to_bytes(nbytes, "little")


class Vd56g3:
    """
    VD56G3 sensor bound to a CX3 console.

    Parameters
    ----------
    console:
        A live :class:`evk.cx3_console.Cx3Console`.
    i2c_addr:
        8-bit base I²C address as seen by the CX3 (default ``0x20``; 7-bit
        ``0x10``). The CX3 firmware sets the R/W bit itself, so pass the 8-bit
        base for both reads and writes (PROTOCOL.md §3.1).
    """

    def __init__(self, console: Cx3Console, i2c_addr: int = reg.SENSOR_I2C_ADDR) -> None:
        self.console = console
        self.i2c_addr = i2c_addr

    # =========================================================== register I/O
    def _write(self, register_address: int, value: int, nbytes: int) -> None:
        """Write a register value (little-endian) — see :meth:`write_reg_bytes`."""
        self.write_reg_bytes(register_address, _value_bytes(value, nbytes))

    def write_reg_bytes(self, register_address: int, value_bytes: bytes) -> None:
        """
        Write raw (already little-endian) value bytes to a register.

        **[capture-confirmed]** encoding: the CX3 GUI performs every register write
        as an ``I2CWRRD`` with **read-length 0** — it writes the payload and reads
        nothing back (reply is a bare ``OK``). Wire bytes:

            I2CWRRD [rdlen_hi=0, rdlen_lo=0, i2c_addr, addr_hi, addr_lo, value...]

        Address is 16-bit big-endian; value bytes are little-endian (LSB first).
        (``I2CWR`` is the binary-documented alternative but was never observed on
        the wire — see PROTOCOL.md §3.1/§8.)
        """
        args = bytes((0x00, 0x00, self.i2c_addr)) + _addr_bytes(register_address) + bytes(value_bytes)
        self.console.query("I2CWRRD", args)

    def write8(self, register_address: int, value: int) -> None:
        """Write an 8-bit register value."""
        self._write(register_address, value, 1)

    def write16(self, register_address: int, value: int) -> None:
        """Write a 16-bit register value (LSB first on the wire)."""
        self._write(register_address, value, 2)

    def write32(self, register_address: int, value: int) -> None:
        """Write a 32-bit register value (LSB first on the wire)."""
        self._write(register_address, value, 4)

    def _read(self, register_address: int, nbytes: int) -> int:
        """
        I2CWRRD [rdlen_hi, rdlen_lo, i2c_addr, addr_hi, addr_lo] -> rdlen bytes
        reassembled little-endian (PROTOCOL.md §3.1).

        NOTE: the console *reply grammar* for read commands is [needs-capture]
        (PROTOCOL.md §7). This parser assumes the reply payload is the raw
        little-endian register bytes as ASCII-hex tokens or as raw bytes; it
        accepts either and reassembles LSB-first. Verify against a real
        ``I2CRD``/``I2CWRRD`` capture before trusting read values.
        """
        rdlen = nbytes
        args = bytes(((rdlen >> 8) & 0xFF, rdlen & 0xFF, self.i2c_addr)) + _addr_bytes(register_address)
        reply = self.console.query("I2CWRRD", args)
        payload = _parse_read_payload(reply, rdlen)
        return int.from_bytes(payload, "little")

    def read8(self, register_address: int) -> int:
        """Read an 8-bit register value."""
        return self._read(register_address, 1)

    def read16(self, register_address: int) -> int:
        """Read a 16-bit register value."""
        return self._read(register_address, 2)

    def read32(self, register_address: int) -> int:
        """Read a 32-bit register value."""
        return self._read(register_address, 4)

    def write_burst(self, register_address: int, data: bytes, burst_size: int = DEFAULT_BURST_SIZE) -> None:
        """
        Chunked burst write: repeated I2CWR of
        ``[i2c_addr, addr_hi, addr_lo, data_chunk...]`` (PROTOCOL.md §3.1).

        The sensor auto-increments its internal address across a burst, so each
        chunk restates the *chunk's* start address. Used by the main-FW patch
        upload path (see :mod:`evk.patch`).
        """
        if burst_size < 1:
            raise ValueError("burst_size must be >= 1")
        for off in range(0, len(data), burst_size):
            chunk = data[off : off + burst_size]
            # I2CWRRD rdlen=0 (the proven write encoding), address auto-incremented.
            self.write_reg_bytes(register_address + off, chunk)

    # ============================================================= CSI config
    def configure_csi(
        self,
        lanes: int = 2,
        field_b: int = 0,
        data_rate_mbps: int = 1500,
        width: int = 1116,
        height: int = 1356,
        bpp: int = 8,
        field11: int = 0,
        v2: bool = False,
        pixel_clock_hz: int = 160_800_000,
        prefer_captured: bool = True,
    ) -> None:
        """
        Configure the CX3 CSI-2 receiver (PROTOCOL.md §4).

        DEFAULT (``prefer_captured=True``): replay the exact ``CFG2WR`` 12-byte
        payload captured from ST's GUI on real hardware
        (``firmware/vd56g3_csi_cfg2wr.json`` / ``reg.CFG2WR_CAPTURED``) for the
        given ``bpp``. The device genuinely uses **CFG2WR**, and verbatim replay
        of known-good bytes is the safest path while the field semantics are only
        partially decoded. This ignores the width/height/rate args for bpp in
        {8, 10}. Set ``prefer_captured=False`` to use the computed path below.

        Computed path (``CFGWR`` / ``CFG2WR``) — from binary analysis, kept for
        parameters outside the captured set:

        12-byte payload (byte offsets):

          0    lane_number      u8
          1    field B          u8   (virtual-channel / format selector)
          2-5  data_rate_mbps   u32  BIG-endian
          6-7  width            u16  BIG-endian
          8-9  height           u16  BIG-endian
          10   bit_per_pixel    u8   (8 or 10)
          11   field            u8   (reserved/format)

        Known-good (ST open_cv example): lanes=2, data_rate=1500, width=1116,
        height=1356, bpp=10, pixel_clock=160.8 MHz.

        ``v2=True`` selects the newer ``CFG2WR`` (index 18) variant, which
        appends a derived timing word. The exact CFG2WR timing math is
        [needs-capture] (PROTOCOL.md §4) — the stub below documents ST's
        observed formula but defaults OFF. Prefer CFGWR until a capture pins it.
        """
        if bpp not in (8, 10):
            raise ValueError(f"bpp must be 8 or 10, got {bpp}")

        # Preferred: replay the exact CFG2WR bytes ST's GUI sent on real hardware.
        if prefer_captured and bpp in reg.CFG2WR_CAPTURED:
            captured = reg.CFG2WR_CAPTURED[bpp]
            logger.info("CFG2WR (captured verbatim, bpp=%d): %s",
                        bpp, " ".join("%02X" % b for b in captured))
            self.console.query("CFG2WR", captured)
            return

        payload = bytearray(12)
        payload[0] = lanes & 0xFF
        payload[1] = field_b & 0xFF
        payload[2:6] = int(data_rate_mbps).to_bytes(4, "big")  # u32 BIG-endian
        payload[6:8] = int(width).to_bytes(2, "big")           # u16 BIG-endian
        payload[8:10] = int(height).to_bytes(2, "big")         # u16 BIG-endian
        payload[10] = bpp & 0xFF
        payload[11] = field11 & 0xFF

        if not v2:
            self.console.query("CFGWR", bytes(payload))
            return

        # --- CFG2WR variant (index 18): NEEDS-CAPTURE ------------------------
        # PROTOCOL.md §4: the v2 path appends a derived timing word computed
        # (observed, unconfirmed) as:
        #     round(2 * bpp * data_rate_mbps * 1e6 / pixel_clock_hz) - 1_000_000
        # The exact width/endianness/placement of this word is unknown until a
        # USBPcap capture of the GUI's CFG2WR frame is decoded. We emit the base
        # 12 bytes plus a best-effort 4-byte BIG-endian timing word so the seam
        # is exercised, but callers should treat CFG2WR output as unverified.
        timing = round(2 * bpp * data_rate_mbps * 1e6 / pixel_clock_hz) - 1_000_000
        timing &= 0xFFFFFFFF
        logger.warning(
            "CFG2WR is NEEDS-CAPTURE: appending unverified timing word 0x%08X "
            "(derived from bpp=%d rate=%d clk=%d). Verify against a capture.",
            timing, bpp, data_rate_mbps, pixel_clock_hz,
        )
        self.console.query("CFG2WR", bytes(payload) + timing.to_bytes(4, "big"))

    # =============================================================== stream
    def start_stream(self) -> None:
        """Start sensor streaming: ``CMD_START_STREAM`` (0x0201) <- 1, then wait
        for ``SYSTEM_FSM`` = 3. Hardware-confirmed semantics (2026-07-16): ST's
        constant names had start/stop swapped — see firmware/vd56g3_registers.py."""
        logger.info("CMD_START_STREAM (0x0201) <- 1")
        self.send_command(reg.CMD_START_STREAM, 1)
        self.wait_system_fsm(3, timeout_s=5.0, label="post-start_stream")

    def stop_stream(self) -> None:
        """Stop sensor streaming: ``CMD_STOP_STREAM`` (0x0202) <- 1 (FSM -> 2)."""
        logger.info("CMD_STOP_STREAM (0x0202) <- 1")
        self.send_command(reg.CMD_STOP_STREAM, 1)

    def send_command(self, cmd_reg: int, value: int, timeout_s: float = 2.0) -> bool:
        """
        Write a VD56G3 command register with the **capture-confirmed handshake**:
        command registers (``CMD_BOOT`` 0x0200, 0x0201, ``CMD_STREAMING``
        0x0202) self-clear to 0 once the sensor has consumed the command. The
        GUI reads the register back after every command write (captures/cold:
        write 0x0202<-01 at #703 is bracketed by reads of 0x0202 at #702 and
        #704-706; 0x0201<-01 at #344 cleared after ~2 polls ~25 ms). Writing
        the *next* command while the previous one is still nonzero gets it
        silently dropped — observed on hardware as SYSTEM_FSM stuck at 2 after
        a back-to-back 0x0201<-01 / 0x0202<-01 replay.

        Returns True once the register reads 0, False on timeout (logged).
        """
        import time

        self.write8(cmd_reg, value)
        deadline = time.monotonic() + timeout_s
        last = -1
        while time.monotonic() < deadline:
            try:
                last = self.read8(cmd_reg)
            except Exception as err:  # noqa: BLE001 - keep the sequence alive
                logger.debug("command readback 0x%04X failed (%s)", cmd_reg, err)
                last = -1
            if last == 0:
                logger.info("command 0x%04X <- 0x%02X consumed", cmd_reg, value)
                return True
            time.sleep(0.01)
        logger.warning("command 0x%04X <- 0x%02X NOT consumed after %.1fs (reads back %d)",
                       cmd_reg, value, timeout_s, last)
        return False

    def wait_system_fsm(self, target: int, timeout_s: float = 3.0, label: str = "") -> int:
        """
        Poll ``SYSTEM_FSM`` (0x0028) until it reaches *target*.

        The cold capture shows ST's GUI polling 0x0028 dozens of times around
        the boot/standby/streaming commands (~4.5 s between ``CMD_STBY``<-1 and
        ``CMD_STREAMING``<-1) — a back-to-back replay outruns the sensor's
        state machine and the streaming command is silently ignored. FSM walks
        1 (ready-to-boot) -> 2 (standby) -> 3 (streaming) per PROTOCOL.md §9.

        Returns the last value read. Logs a warning (does not raise) on
        timeout so the verbatim replay continues; callers can inspect the
        returned state.
        """
        import time

        deadline = time.monotonic() + timeout_s
        val = -1
        while time.monotonic() < deadline:
            try:
                val = self.read8(reg.SYSTEM_FSM)
            except Exception as err:  # noqa: BLE001 - keep the replay alive
                logger.debug("SYSTEM_FSM read failed (%s)", err)
                val = -1
            if val == target:
                logger.info("SYSTEM_FSM=%d reached%s", val, f" ({label})" if label else "")
                return val
            time.sleep(0.05)
        logger.warning("SYSTEM_FSM=%d after %.1fs, wanted %d%s — continuing",
                       val, timeout_s, target, f" ({label})" if label else "")
        return val

    # =============================================================== cold init
    def apply_stream_registers(self, bpp: int, cfg: dict | None = None) -> Tuple[int, int]:
        """
        Write the static stream/format + CTX0 ROI registers (PROTOCOL.md §6.8).

        Mirrors ST's open_cv example exactly:

            Write8 (0x0474 CONTEXTS_READOUT_CTRL, 0)
            Write16(0x030A STATICS_FORMAT_CTRL,  bpp)
            Write16(0x045E CTX0 OUT_ROI_X_START, 2)
            Write16(0x0460 CTX0 OUT_ROI_X_END,   1105)
            Write16(0x0462 CTX0 OUT_ROI_Y_START, 2)
            Write16(0x0464 CTX0 OUT_ROI_Y_END,   1361)
            Write16(0x0335 STREAM_STATICS_OUTPUT_CTRL, 1)

        (0x0335 not 0x0096 — ST's constants define the symbol twice and
        ``import *`` makes 0x0335 win; see firmware/vd56g3_registers.py.)

        Returns the active (width, height) implied by the ROI window.
        """
        c = reg.DEFAULT if cfg is None else cfg
        x_start, x_end = c["x_start"], c["x_end"]
        y_start, y_end = c["y_start"], c["y_end"]

        self.write8(reg.CONTEXTS_READOUT_CTRL, 0)
        self.write16(reg.STATICS_FORMAT_CTRL, bpp)
        self.write16(reg.CTX0_OUT_ROI_X_START, x_start)
        self.write16(reg.CTX0_OUT_ROI_X_END, x_end)
        self.write16(reg.CTX0_OUT_ROI_Y_START, y_start)
        self.write16(reg.CTX0_OUT_ROI_Y_END, y_end)
        self.write16(reg.STREAM_OUTPUT_CTRL, 1)

        width = x_end - x_start + 1
        height = y_end - y_start + 1
        logger.info("Stream registers applied: active window %dx%d bpp=%d", width, height, bpp)
        return width, height

    def replay_cold_init(self, path: str = "firmware/vd56g3_cold_init.json") -> Tuple[int, int, int]:
        """
        Replay the **hardware-captured** cold-init sequence verbatim (PROTOCOL.md
        §9). This is the proven path: it plays ``firmware/vd56g3_cold_init.json``
        — the exact ordered console commands ST's GUI sent to a *cold, UNPATCHED*
        VD56G3 that then streamed (register writes via ``I2CWRRD`` rdlen=0, plus
        ``CLKWR``/``CFG2WR``/``NRST``/``IOSET``/``IOCFGWR``). **No firmware patch
        is applied — the sensor streams without one** (the cold capture read
        ``FWPATCH_REVISION`` = 0 throughout). Ends at ``CMD_STREAMING`` <- 1.

        Returns ``(width, height, bpp)`` derived from the replayed OUT_ROI writes
        and the final ``CFG2WR`` bpp field, for the frame reader.
        """
        import json
        from . import patch as _patchmod  # reuse the repo-relative resolver

        with open(_patchmod._repo_path(path), "r", encoding="utf-8") as f:
            doc = json.load(f)
        steps = doc["steps"]

        import time

        writes: dict[int, bytes] = {}
        bpp = 8
        # The captured session ends with the GUI's *Stop* click —
        # CMD_STOP_STREAM (0x0202) <- 01 — which older extractions kept as the
        # "final init step" (this silently stopped the stream right after
        # starting it). Skip any trailing stop.
        while steps and steps[-1].get("op") == "write" \
                and steps[-1]["reg"] == reg.CMD_STOP_STREAM:
            logger.info("skipping trailing CMD_STOP_STREAM step (the captured session's Stop)")
            steps = steps[:-1]

        _CMD_REGS = (reg.CMD_BOOT, reg.CMD_START_STREAM, reg.CMD_STOP_STREAM)
        for step in steps:
            if step["op"] == "write":
                regaddr = step["reg"]
                val = bytes(step["val"])
                writes[regaddr] = val
                # Command registers need the capture-confirmed self-clear
                # handshake (see send_command) — a back-to-back replay leaves
                # the sensor busy and the next command is silently dropped.
                if regaddr in _CMD_REGS and len(val) == 1:
                    self.send_command(regaddr, val[0])
                    if regaddr == reg.CMD_BOOT:
                        self.wait_system_fsm(2, timeout_s=2.0, label="post-CMD_BOOT")
                    elif regaddr == reg.CMD_START_STREAM and val[0] == 1:
                        self.wait_system_fsm(3, timeout_s=5.0, label="post-CMD_START_STREAM")
                else:
                    self.write_reg_bytes(regaddr, val)
                    time.sleep(0.005)
            else:
                args = bytes(step.get("args", []))
                if step["cmd"] == "CFG2WR" and len(args) >= 11:
                    bpp = args[10]  # byte[10] of the CFG2WR payload = bits/pixel
                self.console.query(step["cmd"], args)
                # GUI spaces the NRST reset toggles 76-494 ms apart.
                time.sleep(0.1 if step["cmd"] == "NRST" else 0.005)

        def _u16(reg: int, default: int = 0) -> int:
            b = writes.get(reg)
            return (b[0] | (b[1] << 8)) if b and len(b) >= 2 else default

        width = _u16(reg.CTX0_OUT_ROI_X_END) - _u16(reg.CTX0_OUT_ROI_X_START) + 1
        height = _u16(reg.CTX0_OUT_ROI_Y_END) - _u16(reg.CTX0_OUT_ROI_Y_START) + 1
        logger.info("replay_cold_init done: %d steps, streaming %dx%d bpp=%d (no patch)",
                    len(steps), width, height, bpp)
        return width, height, bpp

    def cold_init(
        self,
        bpp: int = 8,
        use_captured_sequence: bool = True,
        main_patch_path: str = "firmware/vd56g3_main_patch.bin",
        apply_main_patch: bool = False,
        apply_vt_patch: bool = False,
    ) -> Tuple[int, int, int]:
        """
        Init-to-first-frame. **Default** (``use_captured_sequence=True``) replays
        the hardware-proven captured sequence via :meth:`replay_cold_init` — this
        needs **no firmware patch** and is what actually streamed on real hardware.

        The legacy synthetic path below (``use_captured_sequence=False``) is the
        one derived from ST's ``open_cv`` example + register map, kept for
        experimentation. Patches are **off by default** now that we know the
        sensor streams unpatched; set ``apply_vt_patch``/``apply_main_patch`` to
        re-enable the (optional) patch steps.

        Returns the negotiated ``(width, height, bpp)`` for the frame reader.
        """
        # Default: the hardware-proven verbatim replay (needs no patch).
        if use_captured_sequence:
            return self.replay_cold_init()

        if bpp not in (8, 10):
            raise ValueError(f"bpp must be 8 or 10, got {bpp}")

        # --- legacy synthetic path (experimental) -----------------------------
        # Optional main FW patch (not required for streaming; off by default).
        if apply_main_patch:
            patchmod.load_main_patch(self, path=main_patch_path)
        # Optional VT patch (not required for streaming; off by default).
        if apply_vt_patch:
            patchmod.load_vt_patch(self)

        width, height = self.apply_stream_registers(bpp)
        c = reg.DEFAULT
        self.configure_csi(
            lanes=c["csi_lanes"],
            data_rate_mbps=c["csi_data_rate_mbps"],
            width=c["csi_width"],
            height=c["csi_height"],
            bpp=bpp,
            pixel_clock_hz=c["csi_pixel_clock_hz"],
        )
        logger.info("cold_init (synthetic) complete: width=%d height=%d bpp=%d", width, height, bpp)
        return width, height, bpp


def _parse_read_payload(reply: bytes, rdlen: int) -> bytes:
    """
    Parse an I2CWRRD/I2CRD reply into ``rdlen`` little-endian payload bytes.

    Reply grammar is **[capture-confirmed]** (PROTOCOL.md §8): the console answers
    ``OK <HH> <HH> ...\\r\\n`` — the literal "OK", then one space-separated,
    uppercase-hex byte per read byte (an ack with no data is just ``OK``). We drop
    the leading ``OK`` and parse the hex tokens, reassembling LSB-first. Falls
    back to raw bytes for any other shape, zero-padding short replies (with a
    warning) so offline scaffolding keeps running.
    """
    text = reply.strip()
    tokens = text.split()
    # Confirmed grammar: leading "OK" status token, then hex data bytes.
    if tokens and tokens[0].upper() == b"OK":
        tokens = tokens[1:]
    if tokens and all(len(t) <= 2 and _is_hex(t) for t in tokens):
        try:
            vals = bytes(int(t, 16) for t in tokens)
            return _fit(vals, rdlen)
        except ValueError:
            pass
    # Fallback: raw bytes (e.g. an unexpected/binary reply shape).
    return _fit(bytes(reply), rdlen)


def reply_is_ok(reply: bytes) -> bool:
    """True if a console reply starts with the ``OK`` status token (PROTOCOL.md §8)."""
    return reply.strip()[:2].upper() == b"OK"


def _is_hex(token: bytes) -> bool:
    try:
        int(token, 16)
        return True
    except ValueError:
        return False


def _fit(data: bytes, rdlen: int) -> bytes:
    if len(data) >= rdlen:
        return data[:rdlen]
    logger.warning("read payload shorter than rdlen (%d < %d); zero-padding", len(data), rdlen)
    return data + b"\x00" * (rdlen - len(data))
