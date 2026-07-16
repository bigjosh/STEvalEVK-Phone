#!/usr/bin/env python3
"""
termux_grab.py — capture ONE frame from the STEVAL-EVK-U0I on an Android phone,
using libusb directly (ctypes). No pyusb, no numpy, no Pillow.

Why this instead of the WebUSB app / evk grab.py: Chrome-for-Android's WebUSB
cannot force-detach a kernel driver, so `claimInterface` fails with EBUSY. libusb
CAN (`libusb_set_auto_detach_kernel_driver`), which is exactly what's needed here.
This script also avoids pyusb's fragile fd-adoption path — it drives libusb's C
API directly on the fd that `termux-usb` hands us.

It replays the hardware-captured cold-init sequence (firmware/vd56g3_cold_init.json,
PROTOCOL.md §9 — no firmware patch needed), reads one RAW10 frame off the video
bulk-IN (endpoint 0x83), and writes:
  * frame.pgm  — 8-bit grayscale, viewable anywhere (Termux: `termux-open frame.pgm`)
  * frame.raw  — the exact bytes off the wire (for full-precision RAW10 later)

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
INTERFACES = (0, 1)    # claim both (video on if0, console on if1)
I2C_ADDR = 0x20
LIBUSB_OPTION_NO_DEVICE_DISCOVERY = 2
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8

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

    def read_video(self, expected, tries=8, chunk=16384, timeout=1500):
        """Read frames off EP 0x83 (short packet delimits a frame). Return the
        completed frame whose size is closest to `expected` (skips a leading
        partial frame from starting mid-stream)."""
        buf = ctypes.create_string_buffer(chunk)
        frames = []
        cur = bytearray()
        deadline = time.time() + 8.0
        while time.time() < deadline and len(frames) < tries:
            rc, n = self._bulk(EP_VIDEO_IN, buf, chunk, timeout)
            if rc not in (0, LIBUSB_ERROR_TIMEOUT):
                if rc == LIBUSB_ERROR_OVERFLOW:
                    cur = bytearray(); continue
                raise RuntimeError("video read failed: %s" % err(self.lib, rc))
            if n > 0:
                cur += buf.raw[:n]
            if n < chunk:  # short packet -> end of a frame
                if len(cur) > expected * 0.5:
                    frames.append(bytes(cur))
                cur = bytearray()
        if not frames:
            return bytes(cur)
        frames.sort(key=lambda f: abs(len(f) - expected))
        return frames[0]


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
    writes = {}
    bpp = 10
    for step in doc["steps"]:
        if step["op"] == "write":
            reg, val = step["reg"], bytes(step["val"])
            # I2CWRRD rdlen=0: [0,0, i2c, reg_hi, reg_lo, value...]
            con.query("I2CWRRD", bytes((0, 0, I2C_ADDR, (reg >> 8) & 0xFF, reg & 0xFF)) + val)
            writes[reg] = val
            if reg == 0x0200:           # CMD_BOOT — let the sensor come up
                time.sleep(0.4)
            elif reg == 0x0202:         # CMD_STREAMING — let frames start
                time.sleep(0.2)
            else:
                time.sleep(0.01)
        else:
            args = bytes(step.get("args", []))
            if step["cmd"] == "CFG2WR" and len(args) >= 11:
                bpp = args[10]
            con.query(step["cmd"], args)
            time.sleep(0.01)
    u16 = lambda r: (writes[r][0] | (writes[r][1] << 8)) if r in writes and len(writes[r]) >= 2 else 0
    width = u16(0x0460) - u16(0x045E) + 1     # OUT_ROI_X_END - X_START + 1
    height = u16(0x0464) - u16(0x0462) + 1    # OUT_ROI_Y_END - Y_START + 1
    return width, height, bpp


# ---- frame decode (RAW10 -> 8-bit) + PGM ----------------------------------
def raw10_to_pgm8(frame, width, height):
    """Strip 2 status lines, drop every 5th byte (RAW10 low-bits) -> 8-bit rows.
    For RAW10, each 5 bytes = 4 pixels where bytes 0-3 are the high 8 bits; so the
    8-bit image is just those 4 bytes of every 5 (the 5th holds the low 2 bits)."""
    row_bytes = width * 10 // 8
    img_start = 2 * row_bytes
    out = bytearray()
    for r in range(height):
        base = img_start + r * row_bytes
        row = frame[base:base + row_bytes]
        if len(row) < row_bytes:
            row = row + b"\x00" * (row_bytes - len(row))
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
                print("claim_interface(%d) failed: %s" % (ifnum, err(lib, rc)), file=sys.stderr)
                if ifnum == 1:
                    return 5           # console interface is essential
            else:
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
        print("Streaming %dx%d bpp=%d (no patch)." % (width, height, bpp))

        row_bytes = width * bpp // 8
        expected = (2 + height) * row_bytes
        print("Reading one frame (~%d bytes off EP 0x%02X)..." % (expected, EP_VIDEO_IN))
        frame = con.read_video(expected)
        print("Got %d/%d bytes." % (len(frame), expected))

        with open(out_raw, "wb") as f:
            f.write(frame)
        if bpp == 10:
            pgm = raw10_to_pgm8(frame, width, height)
        else:  # RAW8 passthrough
            rb = width
            body = b"".join(frame[2 * rb + r * rb: 2 * rb + (r + 1) * rb] for r in range(height))
            pgm = ("P5\n%d %d\n255\n" % (width, height)).encode() + body
        with open(out_pgm, "wb") as f:
            f.write(pgm)
        print("Wrote %s (%dx%d) and %s. View: termux-open %s" % (out_pgm, width, height, out_raw, out_pgm))

        # stop streaming (best-effort)
        try:
            con.query("I2CWRRD", bytes((0, 0, I2C_ADDR, 0x02, 0x02, 0x00)))
        except Exception:
            pass
        return 0
    finally:
        for ifnum in claimed:
            lib.libusb_release_interface(handle, ifnum)
        lib.libusb_close(handle)
        lib.libusb_exit(ctx)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
