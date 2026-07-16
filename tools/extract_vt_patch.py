#!/usr/bin/env python3
"""
extract_vt_patch.py — pull the embedded VD56G3 "VT" (vertical-timing) firmware
patch out of ST's sensor library and emit it as a replayable register list.

Background
----------
STSW-IMG507's sensor library (libst_brightsense_sdk_vdx6gx.so / .dll) embeds the
VT patch as a C++ array:

    dvc::S6G3::vt_patch::vtram_patch17   (symbol _ZN3dvc4S6G38vt_patchL13vtram_patch17E)

The array is 3920 entries x 16 bytes. Each entry is:

    struct { uint64_t reg_address;  uint8_t value;  uint8_t pad[7]; }

S6G3::loadVtPatch() replays it as a sequence of Comms::Write8(reg_address, value)
after entering patch mode (write 0x0203 <- 1) and leaving it (write 0x0203 <- 2).
On the wire each Write8 is a single-register I2C write to the sensor through the
CX3 bridge (see PROTOCOL.md).

This script parses the ELF/PE by symbol and dumps the pairs to JSON so the phone
capture code (Termux pyusb / WebUSB) can replay the VT patch without ST's binary.

NOTE: This only covers the VT patch. The *main* FW patch is loaded by
S6G3::loadPatch() from an external file 'Resources/S6H..G3_patch..bin' that ST
did NOT ship in this SDK. See DECISIONS_QUEUE.md.

Usage:
    python extract_vt_patch.py <path-to-libst_brightsense_sdk_vdx6gx.so> [out.json]
"""
import json
import struct
import sys

SYMBOL = "_ZN3dvc4S6G38vt_patchL13vtram_patch17E"
ENTRY = 16          # bytes per entry
PATCH_MODE_REG = 0x0203   # VDx6Gx CMD_DEBUG


# --- minimal ELF (.symtab) locator -----------------------------------------
def _elf_symbol(data, name):
    if data[:4] != b"\x7fELF":
        return None
    (e_shoff,) = struct.unpack_from("<Q", data, 0x28)
    e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    secs = []
    for i in range(e_shnum):
        o = e_shoff + i * e_shentsize
        secs.append(struct.unpack_from("<IIQQQQIIQQ", data, o))  # name..entsize
    shstr_off = secs[e_shstrndx][4]

    def sname(nidx):
        b = shstr_off + nidx
        return data[b:data.index(b"\0", b)].decode()

    symtab = strtab = None
    for s in secs:
        nm = sname(s[0])
        if nm == ".symtab":
            symtab = s
        elif nm == ".strtab":
            strtab = s
    if not symtab:
        return None
    off, size = symtab[4], symtab[5]
    stroff = strtab[4]
    for i in range(size // 24):
        o = off + i * 24
        st_name, st_info, st_other, st_shndx, st_value, st_size = struct.unpack_from("<IBBHQQ", data, o)
        b = stroff + st_name
        if data[b:data.index(b"\0", b)].decode(errors="replace") == name:
            # map vaddr->file offset via section addr/offset
            for sec in secs:
                s_addr, s_off, s_size = sec[3], sec[4], sec[5]
                if sec[1] != 0 and s_addr and s_addr <= st_value < s_addr + s_size:
                    return s_off + (st_value - s_addr), st_size
    return None


# --- minimal PE (COFF symbol table) locator --------------------------------
def _pe_symbol(data, name):
    if data[:2] != b"MZ":
        return None
    (pe_off,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe_off:pe_off + 4] != b"PE\0\0":
        return None
    coff = pe_off + 4
    num_sections, = struct.unpack_from("<H", data, coff + 2)
    sym_ptr, num_syms = struct.unpack_from("<II", data, coff + 8)
    opt_size, = struct.unpack_from("<H", data, coff + 16)
    sec_tab = coff + 20 + opt_size
    image_base, = struct.unpack_from("<Q", data, coff + 20 + 24)  # PE32+ ImageBase
    sections = []
    for i in range(num_sections):
        o = sec_tab + i * 40
        vaddr, = struct.unpack_from("<I", data, o + 12)
        praw, = struct.unpack_from("<I", data, o + 20)
        vsize, = struct.unpack_from("<I", data, o + 8)
        sections.append((vaddr, praw, vsize))
    if not sym_ptr or not num_syms:
        return None
    strtab_off = sym_ptr + num_syms * 18
    def rva_to_off(rva):
        for vaddr, praw, vsize in sections:
            if vaddr <= rva < vaddr + max(vsize, 1) + 0x1000:
                return praw + (rva - vaddr)
        return None
    i = 0
    while i < num_syms:
        o = sym_ptr + i * 18
        raw = data[o:o + 8]
        if raw[:4] == b"\0\0\0\0":
            soff, = struct.unpack_from("<I", raw, 4)
            b = strtab_off + soff
            sym_name = data[b:data.index(b"\0", b)].decode(errors="replace")
        else:
            sym_name = raw.split(b"\0")[0].decode(errors="replace")
        value, = struct.unpack_from("<I", data, o + 8)
        sect, = struct.unpack_from("<h", data, o + 12)
        naux = data[o + 17]
        # PE symbol 'value' is section-relative when Section>0
        if sym_name == name or sym_name == "_" + name:
            if sect > 0:
                vaddr, praw, vsize = sections[sect - 1]
                return praw + value, None
        i += 1 + naux
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "vd56g3_vt_patch.json"
    with open(path, "rb") as f:
        data = f.read()

    loc = _elf_symbol(data, SYMBOL) or _pe_symbol(data, SYMBOL)
    if not loc:
        print(f"symbol {SYMBOL} not found in {path}", file=sys.stderr)
        sys.exit(2)
    off, size = loc
    if not size:  # PE: symbol table has no size; scan to end of a plausible run
        # walk until an all-zero 16-byte entry (address 0) terminates
        size = 0
        while data[off + size:off + size + 8] != b"\0" * 8:
            size += ENTRY
    n = size // ENTRY

    pairs = []
    for i in range(n):
        a, = struct.unpack_from("<Q", data, off + i * ENTRY)
        v = data[off + i * ENTRY + 8]
        pairs.append([a, v])

    doc = {
        "sensor": "VD56G3 (S6G3)",
        "patch": "VT / vertical-timing (vtram_patch17)",
        "source_symbol": SYMBOL,
        "enter_patch_mode": {"reg": PATCH_MODE_REG, "value": 1},
        "exit_patch_mode": {"reg": PATCH_MODE_REG, "value": 2},
        "count": len(pairs),
        "addr_min": min(a for a, _ in pairs),
        "addr_max": max(a for a, _ in pairs),
        "writes": [{"reg": a, "val": v} for a, v in pairs],
    }
    with open(out, "w") as f:
        json.dump(doc, f)
    print(f"extracted {len(pairs)} Write8 pairs -> {out}")
    print(f"addr range 0x{doc['addr_min']:x}..0x{doc['addr_max']:x}")


if __name__ == "__main__":
    main()
