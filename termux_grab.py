#!/usr/bin/env python3
"""
termux_grab.py — capture ONE frame from the STEVAL-EVK-U0I on an Android phone,
using libusb directly (ctypes). No pyusb, no numpy, no Pillow.

Why this instead of the WebUSB app / evk grab.py: Chrome-for-Android's WebUSB
cannot force-detach a kernel driver, so `claimInterface` fails with EBUSY. libusb
CAN (`libusb_set_auto_detach_kernel_driver`), which is exactly what's needed here.
This script also avoids pyusb's fragile fd-adoption path — it drives libusb's C
API directly on the fd that `termux-usb` hands us.

The whole flow below is HARDWARE-VALIDATED (2026-07-16, on the Windows reference
PC via the same libusb calls through grab.py/evk): cold-init replay -> command
handshake -> streaming -> single-URB frame read -> de-chunk -> RAW10 decode
produced a clean photo. Key facts baked in (see PROTOCOL.md):

  * Command registers SELF-CLEAR: after writing 0x0200/0x0201/0x0202, poll the
    register until it reads 0 before the next command, or it gets dropped.
  * 0x0201 <- 01 STARTS streaming (SYSTEM_FSM 2->3); 0x0202 <- 01 STOPS it.
    (ST's constant names suggest the opposite — the capture + live sensor
    prove this mapping.) The captured cold-init JSON ends with the session's
    Stop click (0x0202 <- 01) which must NOT be replayed.
  * The CX3 delivers each frame as 16384-byte chunks: 16-byte header (magic
    10 01 02 00, u16 chunk idx from 1, u32 frame seq @8, u32 payload len @12)
    + up to 16352 payload bytes + 16-byte footer (absent on the final short
    chunk). 1120x1360 RAW10 -> payload 1,906,800 B -> 1,910,528 B on the wire.
  * The CX3 STALLS EP 0x83 when its DMA overflows (host too slow / not
    reading). clear_halt makes it resume at the next frame boundary — so read
    the WHOLE frame in ONE bulk transfer, retrying clear_halt+read on stall.

It writes:
  * frame.pgm  — 8-bit grayscale, viewable anywhere (Termux: `termux-open frame.pgm`)
  * frame.raw  — the exact wire bytes (chunked) for full-precision RAW10 later

USAGE (on the phone, in Termux):
  pkg install python libusb
  termux-usb -l                              # find the EVK, e.g. /dev/bus/usb/001/002
  termux-usb -r -e ./run_grab.sh /dev/bus/usb/001/002
(run_grab.sh just does: python termux_grab.py "$@" — termux-usb appends the fd.)

Or pass the fd yourself: python termux_grab.py --fd 3
"""
import ctypes
import ctypes.util
import json
import os
import sys
import time

# ---- device / protocol constants (PROTOCOL.md) ----------------------------
VID, PID = 0x0553, 0x040A
EP_CMD_OUT = 0x05      # console request  (interface 1)
EP_ANS_IN  = 0x85      # console reply    (interface 1)
EP_VIDEO_IN = 0x83     # video stream     (interface 0)
INTERFACES = (0, 1)    # claim both (video on if0, console on if1) — BOTH essential
I2C_ADDR = 0x20
REG_CMD_BOOT = 0x0200          # <-1 boot;      self-clears
REG_CMD_START_STREAM = 0x0201  # <-1 START stream (FSM->3); self-clears
REG_CMD_STOP_STREAM = 0x0202   # <-1 STOP stream  (FSM->2); self-clears
REG_SYSTEM_FSM = 0x0028        # 1 ready-to-boot, 2 standby, 3 streaming
LIBUSB_OPTION_NO_DEVICE_DISCOVERY = 2
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_PIPE = -9

# ---- CX3 wire frame chunking (hardware-confirmed) --------------------------
WIRE_MAGIC = b"\x10\x01\x02\x00"
WIRE_FRAME_START = WIRE_MAGIC + b"\x01\x00"  # header magic + u16 chunk idx == 1
WIRE_CHUNK_STRIDE = 16384
WIRE_CHUNK_PAYLOAD = 16352


def wire_frame_size(payload_size):
    """Total on-wire bytes for a frame with `payload_size` post-driver bytes."""
    chunks = (payload_size + WIRE_CHUNK_PAYLOAD - 1) // WIRE_CHUNK_PAYLOAD
    return payload_size + chunks * 16 + (chunks - 1) * 16


def strip_wire_chunks(raw):
    """[16B header][payload][16B footer] per 16384-B chunk -> (payload, frame_seq)."""
    if len(raw) < 16 or raw[:4] != WIRE_MAGIC:
        return bytes(raw), None
    out = bytearray()
    seq = None
    off = 0
    while off + 16 <= len(raw):
        if raw[off:off + 4] != WIRE_MAGIC:
            raise ValueError("wire chunk magic missing at offset %d" % off)
        if seq is None:
            seq = int.from_bytes(raw[off + 8:off + 12], "little")
        plen = int.from_bytes(raw[off + 12:off + 16], "little")
        if plen > WIRE_CHUNK_PAYLOAD:
            raise ValueError("chunk at %d declares payload %d" % (off, plen))
        out += raw[off + 16:off + 16 + plen]
        off += WIRE_CHUNK_STRIDE
    return bytes(out), seq


# ---- libusb loader --------------------------------------------------------
def load_libusb():
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    candidates = [
        os.path.join(prefix, "lib", "libusb-1.0.so"),
        "libusb-1.0.so", "libusb-1.0.so.0",
        ctypes.util.find_library("usb-1.0"),
    ]
    for c in candidates:
        if not c:
            continue
        try:
            return ctypes.CDLL(c)
        except OSError:
            continue
    raise RuntimeError("libusb-1.0 not found. On Termux: `pkg install libusb`.")


def setup_prototypes(lib):
    c_int, c_uint, c_void_p, POINTER = ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER
    lib.libusb_set_option.restype = c_int
    lib.libusb_init.argtypes = [POINTER(c_void_p)]; lib.libusb_init.restype = c_int
    lib.libusb_exit.argtypes = [c_void_p]
    lib.libusb_wrap_sys_device.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
    lib.libusb_wrap_sys_device.restype = c_int
    lib.libusb_set_auto_detach_kernel_driver.argtypes = [c_void_p, c_int]
    lib.libusb_set_auto_detach_kernel_driver.restype = c_int
    lib.libusb_kernel_driver_active.argtypes = [c_void_p, c_int]; lib.libusb_kernel_driver_active.restype = c_int
    lib.libusb_detach_kernel_driver.argtypes = [c_void_p, c_int]; lib.libusb_detach_kernel_driver.restype = c_int
    lib.libusb_claim_interface.argtypes = [c_void_p, c_int]; lib.libusb_claim_interface.restype = c_int
    lib.libusb_release_interface.argtypes = [c_void_p, c_int]; lib.libusb_release_interface.restype = c_int
    lib.libusb_clear_halt.argtypes = [c_void_p, ctypes.c_ubyte]; lib.libusb_clear_halt.restype = c_int
    lib.libusb_close.argtypes = [c_void_p]
    lib.libusb_bulk_transfer.argtypes = [c_void_p, ctypes.c_ubyte, ctypes.c_char_p, c_int, POINTER(c_int), c_uint]
    lib.libusb_bulk_transfer.restype = c_int
    lib.libusb_strerror.argtypes = [c_int]; lib.libusb_strerror.restype = ctypes.c_char_p


def err(lib, rc):
    try:
        return lib.libusb_strerror(rc).decode()
    except Exception:
        return "rc=%d" % rc


# ---- console (ASCII command channel) --------------------------------------
class Console:
    def __init__(self, lib, handle, verbose=False):
        self.lib, self.h, self.v = lib, handle, verbose

    def _bulk(self, ep, data, length, timeout):
        transferred = ctypes.c_int(0)
        rc = self.lib.libusb_bulk_transfer(self.h, ctypes.c_ubyte(ep), data, length,
                                           ctypes.byref(transferred), timeout)
        return rc, transferred.value

    def query(self, keyword, args=b"", timeout=300):
        line = keyword.encode("ascii")
        for b in args:
            line += b" %02X" % b
        line += b"\r\n"
        buf = ctypes.create_string_buffer(line, len(line))
        rc, n = self._bulk(EP_CMD_OUT, buf, len(line), timeout)
        if rc != 0:
            raise RuntimeError("write %s failed: %s" % (keyword, err(self.lib, rc)))
        # read the ack/reply ("OK ..."); tolerate timeout (some cmds answer slowly)
        rbuf = ctypes.create_string_buffer(256)
        rc, n = self._bulk(EP_ANS_IN, rbuf, 256, timeout)
        reply = rbuf.raw[:n].split(b"\x00")[0].rstrip(b"\r\n \t") if n > 0 else b""
        if self.v:
            print("  > %-20s -> %s" % (line[:-2].decode("ascii", "replace"), reply.decode("ascii", "replace")))
        return reply

    # --- register access (I2CWRRD tunnelled I2C, PROTOCOL.md §3.1) ---------
    def write_reg(self, reg, val_bytes):
        """I2CWRRD rdlen=0: [0,0, i2c, reg_hi, reg_lo, value...] -> 'OK'."""
        self.query("I2CWRRD", bytes((0, 0, I2C_ADDR, (reg >> 8) & 0xFF, reg & 0xFF)) + val_bytes)

    def read_reg(self, reg, nbytes=1):
        """I2CWRRD rdlen=n -> 'OK <HH>...' little-endian value, or -1 on parse failure."""
        reply = self.query("I2CWRRD", bytes(((nbytes >> 8) & 0xFF, nbytes & 0xFF,
                                             I2C_ADDR, (reg >> 8) & 0xFF, reg & 0xFF)))
        toks = reply.split()
        if toks and toks[0].upper() == b"OK":
            toks = toks[1:]
        try:
            vals = [int(t, 16) for t in toks]
        except ValueError:
            return -1
        if len(vals) < nbytes:
            return -1
        return int.from_bytes(bytes(vals[:nbytes]), "little")

    def send_command(self, reg, value, timeout_s=2.0):
        """Write a self-clearing command register and poll it back to 0.

        Capture+hardware confirmed: issuing the next command while the previous
        one is still nonzero gets it silently dropped."""
        self.write_reg(reg, bytes((value,)))
        deadline = time.time() + timeout_s
        last = -1
        while time.time() < deadline:
            last = self.read_reg(reg, 1)
            if last == 0:
                return True
            time.sleep(0.01)
        print("WARNING: command 0x%04X <- 0x%02X not consumed (reads %d)" % (reg, value, last),
              file=sys.stderr)
        return False

    def wait_fsm(self, target, timeout_s=3.0, label=""):
        """Poll SYSTEM_FSM (0x0028): 1 ready-to-boot, 2 standby, 3 streaming."""
        deadline = time.time() + timeout_s
        val = -1
        while time.time() < deadline:
            val = self.read_reg(REG_SYSTEM_FSM, 1)
            if val == target:
                if self.v:
                    print("  SYSTEM_FSM=%d reached %s" % (val, label))
                return val
            time.sleep(0.05)
        print("WARNING: SYSTEM_FSM=%d after %.1fs, wanted %d %s" % (val, timeout_s, target, label),
              file=sys.stderr)
        return val

    # --- video ---------------------------------------------------------------
    def read_frame(self, wire_total, tries=8, timeout=3000):
        """Read ONE whole wire frame in a single bulk transfer.

        The CX3 stalls EP 0x83 on DMA overflow (host pauses at ~115 MB/s);
        clear_halt makes it resume at the next frame boundary. So each attempt
        is clear_halt -> one frame-sized (+slack) read; the end-of-frame short
        packet terminates the transfer at exactly the wire size."""
        request = wire_total + 65536
        buf = ctypes.create_string_buffer(request)
        for attempt in range(tries):
            self.lib.libusb_clear_halt(self.h, ctypes.c_ubyte(EP_VIDEO_IN))
            transferred = ctypes.c_int(0)
            rc = self.lib.libusb_bulk_transfer(self.h, ctypes.c_ubyte(EP_VIDEO_IN), buf, request,
                                               ctypes.byref(transferred), timeout)
            n = transferred.value
            data = buf.raw[:n]
            if n >= wire_total and data[:6] == WIRE_FRAME_START:
                return data
            print("  video attempt %d/%d: rc=%s n=%d frame-start=%s" %
                  (attempt + 1, tries, err(self.lib, rc) if rc else "ok", n,
                   data[:6] == WIRE_FRAME_START), file=sys.stderr)
        return b""


# ---- cold-init replay -----------------------------------------------------
def find_init_json():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "firmware", "vd56g3_cold_init.json"),
              os.path.join(here, "vd56g3_cold_init.json")):
        if os.path.exists(p):
            return p
    raise RuntimeError("firmware/vd56g3_cold_init.json not found next to this script.")


def replay_cold_init(con, path, verbose=False):
    with open(path) as f:
        doc = json.load(f)
    steps = doc["steps"]

    # The captured session ends with the GUI's Stop click (0x0202 <- 01) —
    # replaying it would stop the stream we just started. Drop it.
    while steps and steps[-1].get("op") == "write" and steps[-1]["reg"] == REG_CMD_STOP_STREAM:
        steps = steps[:-1]

    cmd_regs = (REG_CMD_BOOT, REG_CMD_START_STREAM, REG_CMD_STOP_STREAM)
    writes = {}
    bpp = 10
    for step in steps:
        if step["op"] == "write":
            reg, val = step["reg"], bytes(step["val"])
            writes[reg] = val
            if reg in cmd_regs and len(val) == 1:
                con.send_command(reg, val[0])
                if reg == REG_CMD_BOOT:
                    con.wait_fsm(2, label="(post-CMD_BOOT)")
                elif reg == REG_CMD_START_STREAM and val[0] == 1:
                    con.wait_fsm(3, timeout_s=5.0, label="(post-CMD_START_STREAM)")
            else:
                con.write_reg(reg, val)
                time.sleep(0.01)
        else:
            args = bytes(step.get("args", []))
            if step["cmd"] == "CFG2WR" and len(args) >= 11:
                bpp = args[10]
            con.query(step["cmd"], args)
            # the GUI spaces NRST reset toggles 76-494 ms apart
            time.sleep(0.1 if step["cmd"] == "NRST" else 0.01)
    u16 = lambda r: (writes[r][0] | (writes[r][1] << 8)) if r in writes and len(writes[r]) >= 2 else 0
    width = u16(0x0460) - u16(0x045E) + 1     # OUT_ROI_X_END - X_START + 1
    height = u16(0x0464) - u16(0x0462) + 1    # OUT_ROI_Y_END - Y_START + 1
    return width, height, bpp


# ---- frame decode (RAW10 -> 8-bit) + PGM ----------------------------------
def raw10_to_pgm8(payload, width, height):
    """payload = de-chunked post-driver bytes: 2 status lines then image rows.
    RAW10: each 5 bytes = 4 pixels, bytes 0-3 the high 8 bits (byte 4 = low
    2-bit pairs) — so the 8-bit preview drops every 5th byte."""
    row_bytes = width * 10 // 8
    img_start = 2 * row_bytes
    out = bytearray()
    for r in range(height):
        base = img_start + r * row_bytes
        row = payload[base:base + row_bytes]
        out += bytes(row[i] for i in range(len(row)) if i % 5 != 4)
    header = ("P5\n%d %d\n255\n" % (width, height)).encode()
    return header + bytes(out)


# ---- main -----------------------------------------------------------------
def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    fd = None
    if "--fd" in argv:
        fd = int(argv[argv.index("--fd") + 1])
    elif len(argv) >= 2 and argv[-1].isdigit():
        fd = int(argv[-1])          # termux-usb appends the fd as the last arg
    if fd is None:
        print("No fd. Run via: termux-usb -r -e ./run_grab.sh /dev/bus/usb/BBB/DDD", file=sys.stderr)
        return 2
    out_pgm = "frame.pgm"
    out_raw = "frame.raw"

    lib = load_libusb()
    setup_prototypes(lib)

    # NO_DEVICE_DISCOVERY must be set before init on Android (no usbfs scan).
    try:
        lib.libusb_set_option(None, ctypes.c_int(LIBUSB_OPTION_NO_DEVICE_DISCOVERY))
    except Exception as e:
        print("set_option(NO_DEVICE_DISCOVERY) skipped:", e, file=sys.stderr)

    ctx = ctypes.c_void_p()
    rc = lib.libusb_init(ctypes.byref(ctx))
    if rc != 0:
        print("libusb_init failed:", err(lib, rc), file=sys.stderr); return 3

    handle = ctypes.c_void_p()
    rc = lib.libusb_wrap_sys_device(ctx, ctypes.c_void_p(fd), ctypes.byref(handle))
    if rc != 0 or not handle:
        print("libusb_wrap_sys_device(fd=%d) failed: %s" % (fd, err(lib, rc)), file=sys.stderr)
        print("Check: libusb >= 1.0.23, the fd is the last CLI arg, termux-usb granted permission.", file=sys.stderr)
        return 4
    print("Adopted fd %d." % fd)

    # THE key step WebUSB can't do: force-detach any bound kernel driver, then claim.
    lib.libusb_set_auto_detach_kernel_driver(handle, 1)
    claimed = []
    try:
        for ifnum in INTERFACES:
            if lib.libusb_kernel_driver_active(handle, ifnum) == 1:
                lib.libusb_detach_kernel_driver(handle, ifnum)
            rc = lib.libusb_claim_interface(handle, ifnum)
            if rc != 0:
                # BOTH interfaces are essential: video EP 0x83 is on if0, the
                # console on if1 — a failed claim means no capture.
                print("claim_interface(%d) failed: %s" % (ifnum, err(lib, rc)), file=sys.stderr)
                return 5
            claimed.append(ifnum)
            print("Claimed interface %d." % ifnum)

        con = Console(lib, handle, verbose=verbose)
        ver = con.query("VERSION")
        print("VERSION -> %r" % ver.decode("ascii", "replace"))
        if not ver:
            print("Console silent — endpoints or cable issue. Aborting.", file=sys.stderr)
            return 6

        init_path = find_init_json()
        print("Replaying cold-init from %s ..." % os.path.basename(init_path))
        width, height, bpp = replay_cold_init(con, init_path, verbose=verbose)
        fsm = con.read_reg(REG_SYSTEM_FSM, 1)
        print("Init done: %dx%d bpp=%d, SYSTEM_FSM=%d (3=streaming, no patch needed)."
              % (width, height, bpp, fsm))

        row_bytes = width * bpp // 8
        payload_size = (2 + height) * row_bytes
        wire_total = wire_frame_size(payload_size)
        print("Reading one frame (%d wire bytes off EP 0x%02X)..." % (wire_total, EP_VIDEO_IN))
        frame = con.read_frame(wire_total)
        if len(frame) < wire_total:
            print("FAILED: no complete frame (%d/%d bytes). Usual causes: USB-2-only "
                  "cable (needs a real 5 Gbps C-to-C — console works but video "
                  "starves), or the sensor never reached FSM=3." % (len(frame), wire_total),
                  file=sys.stderr)
            return 7
        print("Got %d wire bytes." % len(frame))

        with open(out_raw, "wb") as f:
            f.write(frame)
        payload, seq = strip_wire_chunks(frame)
        if len(payload) < payload_size:
            print("FAILED: de-chunked payload %d < %d expected." % (len(payload), payload_size),
                  file=sys.stderr)
            return 8
        if bpp == 10:
            pgm = raw10_to_pgm8(payload, width, height)
        else:  # RAW8 passthrough
            body = b"".join(payload[2 * row_bytes + r * row_bytes: 2 * row_bytes + (r + 1) * row_bytes]
                            for r in range(height))
            pgm = ("P5\n%d %d\n255\n" % (width, height)).encode() + body
        with open(out_pgm, "wb") as f:
            f.write(pgm)
        print("Wrote %s (%dx%d, frame seq %s) and %s. View: termux-open %s"
              % (out_pgm, width, height, seq, out_raw, out_pgm))

        # stop streaming (0x0202 <- 1 is STOP, self-clearing)
        con.send_command(REG_CMD_STOP_STREAM, 1)
        return 0
    finally:
        for ifnum in claimed:
            lib.libusb_release_interface(handle, ifnum)
        lib.libusb_close(handle)
        lib.libusb_exit(ctx)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
