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


# First 6 bytes of a frame's FIRST wire chunk: magic + u16 chunk index 1
# (hardware-confirmed; see evk/raw.py wire-chunk layout).
WIRE_FRAME_START = b"\x10\x01\x02\x00\x01\x00"


def read_one_frame(console, wire_total: int, timeout_ms: int, max_reads: int = 10) -> bytes:
    """
    Read one complete on-wire frame (``wire_total`` bytes) off the video
    bulk-IN with a SINGLE large URB per attempt.

    Why single-URB (hardware-confirmed): the CX3 stalls EP 0x83 when its DMA
    buffers overflow — which they do within milliseconds whenever the host
    pauses between chunked reads at the stream's ~115 MB/s. One URB for the
    whole frame drains at line rate with no host round-trips (ST's driver
    submits exactly frame-sized URBs). After a stall, ``clear_halt`` makes the
    CX3 resume cleanly at the next frame boundary, so each attempt is:
    clear_halt -> read (frame + slack; the end-of-frame short packet
    terminates the URB) -> validate the frame-start chunk header.
    """
    import usb.core  # local import: module must stay importable without pyusb

    request = wire_total + 65536
    for attempt in range(max_reads):
        try:
            console.video_device.clear_halt(console.ep_video_in)
        except Exception as err:  # noqa: BLE001
            logger.debug("clear_halt(video) failed: %s", err)
        try:
            data = console.read_video(request, timeout_ms=timeout_ms)
        except usb.core.USBError as err:
            logger.warning("video read failed (%s); retry %d/%d", err, attempt + 1, max_reads)
            continue
        if data[:6] == WIRE_FRAME_START and len(data) >= wire_total:
            return bytes(data)
        logger.warning("discarding non-frame read: %d bytes, frame-start=%s (retry %d/%d)",
                       len(data), data[:6] == WIRE_FRAME_START, attempt + 1, max_reads)
    return b""


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
        # Post-driver payload = 2 status lines + `height` rows; the wire adds
        # 16 B header + 16 B footer per 16 KB chunk (see evk/raw.py).
        payload_size = (2 + height) * x_size_in_bytes
        wire_total = rawmod.wire_frame_size(payload_size)
        logger.info("Expecting %d wire bytes (payload %d = (2+%d) rows x %d B; %dx%d bpp=%d)",
                    wire_total, payload_size, height, x_size_in_bytes, width, height, bpp)

        # The captured replay ends with CMD_START_STREAM<-1 (sensor already
        # streaming, FSM=3); only the legacy synthetic path needs an explicit
        # start here.
        if args.synthetic:
            sensor.start_stream()
        try:
            raw_frame = read_one_frame(console, wire_total, timeout_ms=args.video_timeout_ms)
        finally:
            sensor.stop_stream()

        if len(raw_frame) < wire_total:
            logger.error("Captured %d/%d bytes — incomplete frame. Not saving.",
                         len(raw_frame), wire_total)
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
