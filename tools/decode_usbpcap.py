#!/usr/bin/env python3
"""
decode_usbpcap.py — turn a USB capture of the EVK console into a replay transcript.

The CX3 "spider" console is ASCII (see PROTOCOL.md §2): every host request is a
line "<KEYWORD> <HH> <HH> ...\r\n". Because it's plain text, we don't need to
perfectly parse USBPcap/usbmon encapsulation — we scan the raw capture bytes for
console request lines (and best-effort for replies). This is intentionally
container-agnostic: it works on .pcap, .pcapng, USBPcap or Linux usbmon dumps,
or even a raw bulk-endpoint dump.

What you get:
  * every console command in order, decoded into (keyword, arg-bytes)
  * I2CWR frames further decoded into sensor register writes
    [i2c_addr, reg_hi, reg_lo, data...] -> "reg 0xADDR = 0x.. .."
  * a flat list you can feed to the Termux/WebUSB replay.

Usage:
  python decode_usbpcap.py capture.pcapng            # human-readable transcript
  python decode_usbpcap.py capture.pcapng --json out.json
  python decode_usbpcap.py capture.pcapng --patch-bin main_patch.bin
      # heuristically reconstruct the main FW patch payload from the burst of
      # I2C writes to the patch-RAM region (see --patch-base)

Caveats: address byte-order for register writes is assumed big-endian (reg_hi
first) per PROTOCOL.md; pass --data-le/--data-be to reinterpret. Verify the first
few decoded writes against known values (e.g. FORMAT_CTRL) before trusting bulk
patch reconstruction.
"""
import argparse
import json
import re
import sys

KEYWORDS = [
    "I2CWRRD", "I2CWR", "I2CRD", "IOCFGWR", "IOCFGRD", "IOSET", "IOGET",
    "SPIWRRD", "NRST", "CFG2WR", "CFG2RD", "CFGWR", "CFGRD", "CLKWR", "CLKRD",
    "LOGLVLWR", "LOGLVLRD", "VERSION", "RESET", "ID",
]
# Longest-first so I2CWRRD wins over I2CWR, CFG2WR over CFGWR, etc.
_KW = "|".join(sorted(KEYWORDS, key=len, reverse=True))
# A console request: keyword, then space-separated 2-hex-digit bytes, CRLF (LF ok).
LINE_RE = re.compile(rb"(%s)((?: [0-9A-Fa-f]{2})*)\s*?\r?\n" % _KW.encode())


def scan(data):
    cmds = []
    for m in LINE_RE.finditer(data):
        kw = m.group(1).decode()
        args = bytes(int(h, 16) for h in m.group(2).split())
        cmds.append((m.start(), kw, args))
    cmds.sort(key=lambda t: t[0])
    return cmds


def decode_i2cwr(args, data_le=False):
    """args = [i2c_addr, reg_hi, reg_lo, data...] -> dict."""
    if len(args) < 3:
        return None
    i2c, reg = args[0], (args[1] << 8) | args[2]
    payload = args[3:]
    if data_le:
        val = int.from_bytes(payload, "little") if payload else None
    else:
        val = int.from_bytes(payload, "big") if payload else None
    return {"i2c": i2c, "reg": reg, "data": list(payload), "value": val}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--json")
    ap.add_argument("--patch-bin")
    ap.add_argument("--patch-base", type=lambda x: int(x, 0), default=0x2000,
                    help="register base of the patch RAM region (default 0x2000)")
    ap.add_argument("--data-le", action="store_true", help="interpret reg data little-endian")
    ap.add_argument("--limit", type=int, default=0, help="print only first N commands")
    args = ap.parse_args()

    with open(args.capture, "rb") as f:
        data = f.read()
    cmds = scan(data)
    if not cmds:
        print("No console commands found. Is this really an EVK console capture?",
              file=sys.stderr)
        print("Tip: capture the vendor interface's bulk OUT endpoint traffic.",
              file=sys.stderr)
        sys.exit(1)

    out = []
    patch_bytes = bytearray()
    n = 0
    for _off, kw, a in cmds:
        rec = {"cmd": kw, "args": list(a)}
        if kw == "I2CWR":
            d = decode_i2cwr(a, args.data_le)
            if d:
                rec["reg"] = d["reg"]
                rec["reg_data"] = d["data"]
                if args.patch_bin and args.patch_base <= d["reg"]:
                    patch_bytes += bytes(d["data"])
        out.append(rec)
        n += 1
        if args.limit and n >= args.limit:
            break

    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=1)
        print(f"wrote {len(out)} commands -> {args.json}")
    else:
        for r in out:
            if "reg" in r:
                dd = " ".join(f"{b:02X}" for b in r["reg_data"])
                print(f"{r['cmd']:8s} reg=0x{r['reg']:04X}  data=[{dd}]")
            else:
                aa = " ".join(f"{b:02X}" for b in r["args"])
                print(f"{r['cmd']:8s} {aa}")

    # command histogram
    from collections import Counter
    hist = Counter(r["cmd"] for r in out)
    print("\n# command counts:", dict(hist.most_common()), file=sys.stderr)

    if args.patch_bin:
        with open(args.patch_bin, "wb") as f:
            f.write(patch_bytes)
        print(f"# reconstructed {len(patch_bytes)} patch-region bytes -> {args.patch_bin}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
