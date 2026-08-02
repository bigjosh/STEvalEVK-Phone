#!/usr/bin/env python3
"""Hands-free flasher for the Grove Vision AI V2 (WE2) bootloader.

Same XMODEM protocol as the SDK's xmodem_send.py, plus:
  - resets the board itself via a DTR toggle (no reset button needed)
  - ASCII-only progress output (the stock script crashes on cp1252 consoles)
  - after flashing, answers the reboot prompt and tails the new firmware's
    boot log for --boot-log seconds so you see it come up

Usage:  python himax/flash_we2.py --port COM11 --file path\to\output.img
"""

import argparse
import math
import os
import sys
import time

import serial
import xmodem


def log(msg):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM11")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--file", required=True)
    ap.add_argument("--boot-log", type=float, default=10.0,
                    help="seconds of post-flash boot log to show (0 = none)")
    ap.add_argument("--enter-timeout", type=float, default=20.0,
                    help="seconds to wait for the bootloader after reset")
    args = ap.parse_args()

    size = os.path.getsize(args.file)
    total_packets = math.ceil(size / 128)
    log(f"image: {args.file} ({size} bytes, {total_packets} xmodem packets)")

    ser = serial.Serial(args.port, args.baud, timeout=1)
    ser.flushInput()
    ser.flushOutput()

    log("resetting board via DTR...")
    ser.setDTR(False)
    time.sleep(0.05)
    ser.setDTR(True)

    # Catch the bootloader: it waits ~100 ms for a key on every boot.
    # Spam '1' at every line until it acknowledges xmodem mode.
    t0 = time.time()
    entered = False
    while time.time() - t0 < args.enter_timeout:
        line = ser.readline()
        if line:
            txt = line.decode("utf-8", "replace").strip()
            log("| " + txt)
            if "Send data using the xmodem protocol" in txt:
                entered = True
                break
        ser.write(b"1\r")
    if not entered:
        log("bootloader never offered xmodem mode — is this the right port?")
        sys.exit(1)

    time.sleep(1)
    ser.flushInput()
    ser.write(b"1\r")

    state = {"count": 0}

    def getc(sz, timeout=1):
        return ser.read(sz)

    def putc(data, timeout=1):
        return ser.write(data)

    def cb(total, success, errors):
        state["count"] = success
        if success % 200 == 0 or success >= total_packets:
            pct = 100.0 * success / total_packets
            log(f"  sent {success}/{total_packets} packets ({pct:.0f}%), errors={errors}")

    modem = xmodem.XMODEM(getc=getc, putc=putc, mode="xmodem")
    log("sending image...")
    with open(args.file, "rb") as stream:
        ok = modem.send(stream, callback=cb)
    if not ok:
        log("XMODEM SEND FAILED")
        sys.exit(1)
    log("xmodem transfer done")

    # Bootloader asks: end transmission and reboot? -> yes
    t0 = time.time()
    while time.time() - t0 < 15:
        line = ser.readline()
        if not line:
            continue
        txt = line.decode("utf-8", "replace").strip()
        log("| " + txt)
        if "reboot system" in txt:
            time.sleep(0.5)
            ser.flushInput()
            ser.write(b"y\r")
            log("answered 'y' — board restarting with new firmware")
            break

    if args.boot_log > 0:
        log(f"--- boot log ({args.boot_log:.0f}s) ---")
        t0 = time.time()
        while time.time() - t0 < args.boot_log:
            line = ser.readline()
            if line:
                log("| " + line.decode("utf-8", "replace").rstrip())

    ser.close()
    log("flash complete")


if __name__ == "__main__":
    main()
