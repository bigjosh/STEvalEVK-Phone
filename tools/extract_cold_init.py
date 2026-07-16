#!/usr/bin/env python3
"""
extract_cold_init.py — build firmware/vd56g3_cold_init.json from a cold USBPcap.

Parses a USBPcap capture of ST's GUI performing a *cold* init (unplug → capture →
GUI launch), extracts the ordered console command sequence on the CX3 console
(command bulk-OUT), drops the pure register reads / GPIO-config introspection,
and emits the essential replay up to and including CMD_START_STREAM (0x0201) <- 1.

⚠ Command semantics (hardware-confirmed 2026-07-16, contra ST's constant
names): 0x0201 <- 01 STARTS streaming, 0x0202 <- 01 STOPS it. The capture's
final 0x0202 <- 01 is the GUI's Stop click at session end and is EXCLUDED
from the replay (an earlier version kept it, which stopped the stream right
after starting it).

Register writes are recognized as I2CWRRD with read-length 0 (the GUI's write
encoding — see PROTOCOL.md §3.1/§8). Output steps are either:
    {"op": "write", "reg": <int>, "val": [<le bytes>]}
    {"op": "cmd",   "cmd": "<KEYWORD>", "args": [<bytes>]}   # CLKWR/CFG2WR/NRST/IOSET/IOCFGWR

Usage:
    python tools/extract_cold_init.py captures/cold [firmware/vd56g3_cold_init.json]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_usb_capture import iter_urbs, T_BULK, KEYWORDS  # noqa: E402

CMD_OUT_EP = 0x05
READ_KEYWORDS = ("IOCFGRD", "IOGET", "CLKRD", "CFGRD", "CFG2RD", "CLKRD")


def commands(data):
    kwset = tuple(k.encode() for k in KEYWORDS)
    for ep, tr, is_in, payload in iter_urbs(data):
        if tr == T_BULK and ep == CMD_OUT_EP and not is_in and payload:
            txt = payload.split(b"\r")[0].split(b"\n")[0]
            if txt[:8].strip().startswith(kwset):
                parts = txt.decode("ascii").split()
                yield parts[0], [int(x, 16) for x in parts[1:]]


def is_write(kw, a):
    return kw == "I2CWRRD" and len(a) >= 5 and ((a[0] << 8) | a[1]) == 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "firmware/vd56g3_cold_init.json"
    data = open(src, "rb").read()

    steps = []
    for kw, a in commands(data):
        if kw in READ_KEYWORDS:
            continue
        if kw == "I2CWRRD" and not is_write(kw, a):
            continue  # pure register read — grab.py polls FSM itself
        if is_write(kw, a):
            reg = (a[3] << 8) | a[4]
            val = a[5:]
            if reg == 0x0201 and val == [0x01]:
                steps.append({"op": "write", "reg": reg, "val": list(val)})
                break  # CMD_START_STREAM <- 1: the stream is running; anything
                       # after (incl. the session's 0x0202<-1 Stop) is excluded
            steps.append({"op": "write", "reg": reg, "val": list(val)})
        else:
            steps.append({"op": "cmd", "cmd": kw, "args": list(a)})

    doc = {
        "source": src,
        "note": "verbatim cold-init replay ending at CMD_START_STREAM (0x0201)<-1 "
                "(hw-confirmed: 0x0201 starts streaming, 0x0202 stops; command regs "
                "self-clear — poll to 0 before the next command). Sensor streams "
                "UNPATCHED (FWPATCH_REVISION read 0 throughout). Writes are I2CWRRD rdlen=0.",
        "steps": steps,
    }
    with open(out, "w") as f:
        json.dump(doc, f, indent=1)
    nwrite = sum(1 for s in steps if s["op"] == "write")
    print(f"wrote {len(steps)} steps ({nwrite} register writes) -> {out}")


if __name__ == "__main__":
    main()
