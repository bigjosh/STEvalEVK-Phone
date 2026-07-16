#!/usr/bin/env python3
"""
parse_usb_capture.py — URB-level parser for a USBPcap capture of the EVK.

Unlike tools/decode_usbpcap.py (which byte-scans for the ASCII console lines and
is container-agnostic), this understands the USBPcap pcap format (DLT 249) and
splits traffic by endpoint + transfer type. That lets it separate the three bulk
channels the CX3 console uses — command OUT, answer IN, video IN — from the video
firehose, recover the actual endpoint addresses and the read reply grammar, and
reassemble real frames.

Usage:
  python tools/parse_usb_capture.py captures/steval-connect                # summary
  python tools/parse_usb_capture.py captures/steval-connect --console      # console transcript (cmd + reply)
  python tools/parse_usb_capture.py captures/steval-connect --frames out/  # dump reassembled video frames
"""
import argparse
import struct
import sys

# USBPcap transfer types
T_ISO, T_INTR, T_CTRL, T_BULK = 0, 1, 2, 3
TNAME = {0: "ISO", 1: "INTR", 2: "CTRL", 3: "BULK"}

# Console command keywords (to recognize request lines in bulk-OUT payloads)
KEYWORDS = ("I2CWRRD", "I2CWR", "I2CRD", "IOCFGWR", "IOCFGRD", "IOSET", "IOGET",
            "SPIWRRD", "NRST", "CFG2WR", "CFG2RD", "CFGWR", "CFGRD", "CLKWR",
            "CLKRD", "LOGLVLWR", "LOGLVLRD", "VERSION", "RESET", "ID")


def _read_global_header(d):
    magic, = struct.unpack_from("<I", d, 0)
    if magic not in (0xA1B2C3D4, 0xA1B23C4D):
        raise ValueError("not a little-endian classic pcap (magic 0x%08x)" % magic)
    network, = struct.unpack_from("<I", d, 20)
    if network != 249:
        raise ValueError("not USBPcap (DLT=%d, expected 249)" % network)
    return 24  # global header size


def iter_urbs(d):
    """Yield (endpoint, transfer, direction_in, data_bytes) per URB record."""
    off = _read_global_header(d)
    n = len(d)
    while off + 16 <= n:
        _ts_s, _ts_us, incl, _orig = struct.unpack_from("<IIII", d, off)
        off += 16
        rec = d[off:off + incl]
        off += incl
        if len(rec) < 27:
            continue
        header_len, = struct.unpack_from("<H", rec, 0)
        endpoint = rec[21]
        transfer = rec[22]
        data_len, = struct.unpack_from("<I", rec, 23)
        data = rec[header_len:header_len + data_len] if header_len <= len(rec) else b""
        yield endpoint, transfer, bool(endpoint & 0x80), data


def summarize(d):
    from collections import defaultdict
    stats = defaultdict(lambda: {"count": 0, "bytes": 0, "transfer": None})
    for ep, transfer, _in, data in iter_urbs(d):
        s = stats[ep]
        s["count"] += 1
        s["bytes"] += len(data)
        s["transfer"] = transfer
    print("endpoint  dir  type  packets     bytes")
    for ep in sorted(stats):
        s = stats[ep]
        d_ = "IN " if ep & 0x80 else "OUT"
        print("  0x%02X    %s  %-4s  %7d  %10d" % (ep, d_, TNAME.get(s["transfer"], "?"),
                                                   s["count"], s["bytes"]))
    return stats


def console_transcript(d, limit=0):
    """
    Walk URBs in order; for BULK OUT that look like console requests, print them,
    and pair each with the next BULK IN on a *small* endpoint (the answer pipe).
    Identifies the command-OUT / answer-IN / video-IN endpoints heuristically:
    the bulk-IN carrying huge payloads is video; the other bulk-IN is the answer.
    """
    # First pass: classify bulk-IN endpoints by mean payload size.
    from collections import defaultdict
    in_bytes = defaultdict(int)
    in_count = defaultdict(int)
    out_eps = set()
    for ep, transfer, is_in, data in iter_urbs(d):
        if transfer != T_BULK:
            continue
        if is_in:
            in_bytes[ep] += len(data)
            in_count[ep] += 1
        else:
            out_eps.add(ep)
    if not in_count:
        print("no bulk-IN endpoints found", file=sys.stderr)
        return
    means = {ep: in_bytes[ep] / max(in_count[ep], 1) for ep in in_count}
    video_ep = max(means, key=means.get)
    answer_eps = [ep for ep in in_count if ep != video_ep]
    print("# bulk-OUT (command): %s" % ", ".join("0x%02X" % e for e in sorted(out_eps)), file=sys.stderr)
    print("# bulk-IN  (answer):  %s" % ", ".join("0x%02X" % e for e in sorted(answer_eps)), file=sys.stderr)
    print("# bulk-IN  (video):   0x%02X (mean %d B/xfer)" % (video_ep, means[video_ep]), file=sys.stderr)

    # Second pass: emit command lines and the immediately-following answer.
    kwset = tuple(k.encode() for k in KEYWORDS)
    pending = None
    n = 0
    for ep, transfer, is_in, data in iter_urbs(d):
        if transfer != T_BULK:
            continue
        if not is_in:
            txt = data.split(b"\r")[0].split(b"\n")[0]
            if txt[:8].strip().startswith(kwset):
                if pending is not None:
                    print("%-40s" % pending.decode("ascii", "replace"))
                pending = txt
                n += 1
                if limit and n > limit:
                    break
        elif ep in answer_eps and pending is not None:
            reply = data.split(b"\x00")[0].rstrip(b"\r\n \t")
            print("%-40s -> %r" % (pending.decode("ascii", "replace"), reply.decode("ascii", "replace")))
            pending = None
    if pending is not None:
        print("%s -> (no reply captured)" % pending.decode("ascii", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--console", action="store_true", help="print console cmd/reply transcript")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    d = open(args.capture, "rb").read()
    if args.console:
        console_transcript(d, args.limit)
    else:
        summarize(d)


if __name__ == "__main__":
    main()
