#!/usr/bin/env python3
"""Receive one VD56G3 frame from the Grove Vision AI V2 (vd56g3_poc firmware).

Opens the board's COM port, resets it via DTR, echoes the console, then
captures the base64 frame dump between the BEGIN/END markers, CRC-checks it,
and writes:
  vd56g3_YYYYmmdd_HHMMSS-<ms>.bin   packed RAW10 (1400 B x 1360 rows), as captured
  vd56g3_YYYYmmdd_HHMMSS-<ms>.pgm   unpacked 16-bit grayscale (10-bit data << 6)
  vd56g3_YYYYmmdd_HHMMSS-<ms>.png   same, via Pillow if installed

Usage:  python himax/receive_frame.py --port COM11
"""

import argparse
import base64
import binascii
import datetime
import re
import sys
import time

import serial  # pyserial

BEGIN_RE = re.compile(
    rb"===VD56G3_FRAME_BEGIN len=(\d+) w=(\d+) h=(\d+) bpp=8 crc32=([0-9a-fA-F]{8}) coarse=(\d+)==="
)
END_RE = re.compile(rb"===VD56G3_FRAME_END crc32=([0-9a-fA-F]{8})===")

# Sensor timing for the POC build (LINE_LENGTH=2472 @ 160.8 MHz pixel clock)
LINE_PERIOD_S = 2472 / 160.8e6


def unpack_raw10(packed: bytes, row_bytes: int, rows: int):
    """5 bytes -> 4 pixels; returns list of rows, each a list of 10-bit ints."""
    try:
        import numpy as np

        a = np.frombuffer(packed, dtype=np.uint8).reshape(rows, row_bytes)
        g = a.reshape(rows, row_bytes // 5, 5).astype(np.uint16)
        px = np.empty((rows, (row_bytes // 5) * 4), dtype=np.uint16)
        for i in range(4):
            px[:, i::4] = (g[:, :, i] << 2) | ((g[:, :, 4] >> (2 * i)) & 3)
        return px  # numpy array rows x width
    except ImportError:
        out = []
        for r in range(rows):
            row = packed[r * row_bytes:(r + 1) * row_bytes]
            px = []
            for gi in range(0, row_bytes, 5):
                b0, b1, b2, b3, b4 = row[gi:gi + 5]
                px.append((b0 << 2) | (b4 & 3))
                px.append((b1 << 2) | ((b4 >> 2) & 3))
                px.append((b2 << 2) | ((b4 >> 4) & 3))
                px.append((b3 << 2) | ((b4 >> 6) & 3))
            out.append(px)
        return out


def write_pgm16(path, pixels, width, height):
    """16-bit binary PGM, big-endian samples; v16 = (v<<6)|(v>>4) like the web app."""
    with open(path, "wb") as f:
        f.write(f"P5\n{width} {height}\n65535\n".encode())
        try:
            import numpy as np

            v = pixels.astype(np.uint16)
            v16 = ((v << 6) | (v >> 4)).astype(">u2")
            f.write(v16.tobytes())
        except (ImportError, AttributeError):
            import struct

            for row in pixels:
                f.write(b"".join(struct.pack(">H", ((v << 6) | (v >> 4)) & 0xFFFF) for v in row))


def try_write_png(path, pixels, width, height):
    try:
        import numpy as np
        from PIL import Image

        v = pixels.astype(np.uint16)
        v16 = ((v << 6) | (v >> 4)).astype(np.uint16)
        Image.fromarray(v16, mode="I;16").save(path)
        return True
    except Exception as e:  # PIL or numpy missing, or save failed
        print(f"(png skipped: {e})")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM11")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--timeout", type=float, default=180.0, help="overall capture timeout, s")
    ap.add_argument("--no-reset", action="store_true", help="don't toggle DTR to reset the board")
    ap.add_argument("--prefix", default="vd56g3")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=1)
    if not args.no_reset:
        print(f"[{args.port}] resetting board via DTR...")
        ser.setDTR(False)
        time.sleep(0.05)
        ser.setDTR(True)

    print(f"[{args.port}] listening at {args.baud}...")
    t0 = time.time()
    meta = None
    b64_chunks = []
    end_crc = None

    while time.time() - t0 < args.timeout:
        line = ser.readline()
        if not line:
            continue
        if meta is None:
            m = BEGIN_RE.search(line)
            if m:
                meta = {
                    "len": int(m.group(1)),
                    "w": int(m.group(2)),
                    "h": int(m.group(3)),
                    "crc": int(m.group(4), 16),
                    "coarse": int(m.group(5)),
                }
                print(f"--> frame announced: {meta}")
            else:
                sys.stdout.write("| " + line.decode("utf-8", "replace"))
                sys.stdout.flush()
        else:
            m = END_RE.search(line)
            if m:
                end_crc = int(m.group(1), 16)
                break
            b64_chunks.append(line.strip())

    ser.close()

    if meta is None:
        print("No frame marker seen — is vd56g3_poc flashed? (reset the board?)")
        sys.exit(1)
    if end_crc is None:
        print("BEGIN seen but END never arrived (timeout).")
        sys.exit(1)

    data = base64.b64decode(b"".join(b64_chunks))
    print(f"decoded {len(data)} bytes (expect {meta['len']})")
    if len(data) != meta["len"]:
        print("LENGTH MISMATCH")
        sys.exit(1)
    crc = binascii.crc32(data) & 0xFFFFFFFF
    if crc != meta["crc"]:
        print(f"CRC MISMATCH: got {crc:08x}, expect {meta['crc']:08x}")
        sys.exit(1)
    print(f"CRC ok ({crc:08x})")

    exp_ms = meta["coarse"] * LINE_PERIOD_S * 1000.0
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{args.prefix}_{stamp}-{int(exp_ms)}"

    with open(base + ".bin", "wb") as f:
        f.write(data)
    print(f"wrote {base}.bin (exposure ~{exp_ms:.1f} ms)")

    w_px = meta["w"] * 4 // 5
    pixels = unpack_raw10(data, meta["w"], meta["h"])
    write_pgm16(base + ".pgm", pixels, w_px, meta["h"])
    print(f"wrote {base}.pgm ({w_px}x{meta['h']} 16-bit)")
    if try_write_png(base + ".png", pixels, w_px, meta["h"]):
        print(f"wrote {base}.png")


if __name__ == "__main__":
    main()
