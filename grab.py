#!/usr/bin/env python3
"""
grab.py — STEVAL-EVK-U0I init-to-first-frame, save ONE frame as JPG.

Phase-1 CLI for the pure-Python (pyusb/libusb) host. Opens the CX3 bridge
(Termux fd or desktop VID:PID), runs the VD56G3 cold-init sequence
(PROTOCOL.md §6), starts streaming, reads exactly one frame off the video
bulk-IN, decodes it (mirroring ST's ``vdx6gx_frame_decoding.py``), and writes a
grayscale JPEG.

Typical Termux invocation (the ``-e`` wrapper appends the fd as the last arg):

    termux-usb -r -e 'python grab.py --bpp 8 --out frame.jpg --fd' /dev/bus/usb/001/002

Desktop-Linux test (normal enumeration):

    python grab.py --auto --bpp 8 --out frame.jpg -v

Exit codes: 0 on success; non-zero (with a clear message) if the console does
not answer or a frame cannot be captured. See docs/TERMUX_SETUP.md.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

logger = logging.getLogger("evk.grab")


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="STEVAL-EVK-U0I: init to first frame, save one JPG.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument(
        "--fd", type=int, default=None,
        help="Termux USB file descriptor (from `termux-usb -e ... --fd`).",
    )
    src.add_argument(
        "--auto", action="store_true",
        help="Desktop-Linux fallback: open by VID:PID (0553:040A) via enumeration.",
    )
    p.add_argument("--bpp", type=int, choices=(8, 10), default=8,
                   help="Pixel format for the SYNTHETIC path only (the captured "
                        "replay uses the format ST's GUI used).")
    p.add_argument("--out", default="frame.jpg", help="Output JPEG path.")
    p.add_argument("--synthetic", action="store_true",
                   help="Use the legacy synthetic init (ST-example registers) "
                        "instead of the hardware-captured replay (default).")
    p.add_argument("--apply-vt-patch", action="store_true",
                   help="[synthetic only] apply the (optional) VT patch — NOT needed to stream.")
    p.add_argument("--main-patch", default="firmware/vd56g3_main_patch.bin",
                   help="[synthetic only] path to a main FW patch blob (optional; not needed to stream).")
    p.add_argument("--apply-main-patch", action="store_true",
                   help="[synthetic only] apply the main FW patch if present (optional).")
    # Endpoint / addressing overrides (all optional; auto-discovered otherwise).
    p.add_argument("--ep-cmd-out", type=lambda x: int(x, 0), default=None, help="Override command bulk-OUT endpoint address.")
    p.add_argument("--ep-ans-in", type=lambda x: int(x, 0), default=None, help="Override answer bulk-IN endpoint address.")
    p.add_argument("--ep-video-in", type=lambda x: int(x, 0), default=None, help="Override video bulk-IN endpoint address.")
    p.add_argument("--interface", type=int, default=None, help="Vendor interface number to claim.")
    p.add_argument("--i2c-addr", type=lambda x: int(x, 0), default=0x20, help="Sensor I2C address (8-bit base).")
    p.add_argument("--vid", type=lambda x: int(x, 0), default=0x0553, help="USB vendor id (for --auto).")
    p.add_argument("--pid", type=lambda x: int(x, 0), default=0x040A, help="USB product id (for --auto).")
    p.add_argument("--video-timeout-ms", type=int, default=2000, help="Per-transfer video read timeout.")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v = INFO, -vv = DEBUG.")
    return p.parse_args(argv)


def setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)-12s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def read_one_frame(console, frame_size: int, chunk: int, timeout_ms: int) -> bytes:
    """
    Reassemble exactly one frame (``frame_size`` bytes) off the video bulk-IN.

    Reads in ``chunk``-sized transfers until we have ``frame_size`` bytes
    (PROTOCOL.md §5/§6 step 11). Extra bytes from the final transfer are kept —
    the decoder tolerates a payload at least as large as required.
    """
    buf = bytearray()
    while len(buf) < frame_size:
        want = min(chunk, frame_size - len(buf) + chunk)  # allow a full trailing transfer
        data = console.read_video(want, timeout_ms=timeout_ms)
        if not data:
            logger.warning("empty video transfer (%d/%d bytes so far)", len(buf), frame_size)
            break
        buf.extend(data)
        logger.debug("video: %d/%d bytes", len(buf), frame_size)
    return bytes(buf)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    if args.fd is None and not args.auto:
        logger.error("Specify a device source: --fd N (Termux) or --auto (desktop VID:PID).")
        return 2

    # Imports here so --help works even if pyusb/numpy/pillow aren't installed.
    from evk.usb_termux import open_device
    from evk.cx3_console import Cx3Console, Cx3ConsoleError
    from evk.vd56g3 import Vd56g3
    from evk import raw as rawmod
    from firmware import vd56g3_registers as reg

    # 1. Open the USB device.
    try:
        device = open_device(fd=args.fd, vid=args.vid, pid=args.pid)
    except Exception as err:  # noqa: BLE001 - surface any open failure clearly
        logger.error("Failed to open device: %s", err)
        return 3

    console = None
    sensor = None
    try:
        # 2. Console + VERSION sanity check.
        console = Cx3Console(
            device,
            interface_number=args.interface,
            ep_cmd_out=args.ep_cmd_out,
            ep_ans_in=args.ep_ans_in,
            ep_video_in=args.ep_video_in,
        )
        try:
            version = console.version()
        except Cx3ConsoleError as err:
            logger.error("CX3 console did not answer VERSION: %s", err)
            logger.error("Check endpoints (try --ep-* overrides), cable (5 Gbps), and permissions.")
            return 4
        logger.info("CX3 VERSION: %s", version.decode("ascii", "replace") or "<empty>")
        if not version:
            logger.error("CX3 VERSION reply was empty; console not usable. Aborting.")
            return 4

        sensor = Vd56g3(console, i2c_addr=args.i2c_addr)

        # 3. Cold init. Default = replay the hardware-captured sequence (no patch
        #    needed — the sensor streams unpatched, per captures/cold).
        width, height, bpp = sensor.cold_init(
            bpp=args.bpp,
            use_captured_sequence=not args.synthetic,
            main_patch_path=args.main_patch,
            apply_main_patch=args.apply_main_patch,
            apply_vt_patch=args.apply_vt_patch,
        )

        # 4. Start streaming and read one frame.
        x_size_in_bytes = int(bpp * width / 8)
        # Full payload = 2 status lines + `height` image rows (PROTOCOL.md §5).
        frame_size = (2 + height) * x_size_in_bytes
        logger.info("Expecting frame_size=%d bytes (%dx%d bpp=%d, row=%d B)",
                    frame_size, width, height, bpp, x_size_in_bytes)

        sensor.start_stream()
        try:
            raw_frame = read_one_frame(
                console, frame_size, chunk=x_size_in_bytes * 16, timeout_ms=args.video_timeout_ms
            )
        finally:
            sensor.stop_stream()

        if len(raw_frame) < frame_size:
            logger.error("Captured %d/%d bytes — incomplete frame. Not saving.",
                         len(raw_frame), frame_size)
            logger.error("Bulk video stream likely starving: confirm the genuine 5 Gbps "
                         "C-to-C cable, and that the video endpoint is right "
                         "(--ep-video-in 0x83). See PROTOCOL.md §9.")
            return 5

        # 5. Decode + save.
        metadata, image = rawmod.decode_frame(raw_frame, width)
        rawmod.to_jpeg(image, args.out)
        logger.info("Saved %s: %s", args.out, metadata)
        print(f"OK: wrote {args.out} ({metadata['width']}x{metadata['height']} "
              f"bpp={metadata['bits_per_pixels']} frame#{metadata['frame_counter']})")
        return 0

    except Exception as err:  # noqa: BLE001
        logger.exception("Capture failed: %s", err)
        return 1
    finally:
        try:
            if console is not None:
                console.close()
        except Exception as err:  # noqa: BLE001
            logger.debug("console.close() error: %s", err)


if __name__ == "__main__":
    sys.exit(main())
