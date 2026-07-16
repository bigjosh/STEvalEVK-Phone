"""
Offline byte-level regression tests for the evk protocol layer.

Runs with NO hardware and NO pyusb/libusb backend (the ``usb`` package is
stubbed), so it works in CI / on-phone. It pins the wire bytes the code emits
for every load-bearing operation against PROTOCOL.md, which is the only
verification available until the EVK can be driven for real.

Run directly:      python tests/test_protocol_offline.py
Run under pytest:  pytest tests/test_protocol_offline.py
"""
import os
import sys
import types

import numpy as np

# --- repo root on sys.path (tests/ -> repo) --------------------------------
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)


# --- stub the `usb` package so evk.cx3_console imports without pyusb --------
def _install_usb_stub():
    if "usb" in sys.modules:
        return
    usb = types.ModuleType("usb")
    usb.core = types.ModuleType("usb.core")
    usb.util = types.ModuleType("usb.util")

    class USBError(Exception):
        pass

    usb.core.USBError = USBError
    usb.core.Device = object
    usb.core.find = lambda **k: None
    usb.util.ENDPOINT_TYPE_BULK = 2
    usb.util.ENDPOINT_OUT = 0
    usb.util.ENDPOINT_IN = 0x80
    usb.util.endpoint_type = lambda a: 2
    usb.util.endpoint_direction = lambda a: (0x80 if a & 0x80 else 0)
    usb.util.claim_interface = lambda *a: None
    usb.util.release_interface = lambda *a: None
    usb.util.dispose_resources = lambda *a: None
    sys.modules["usb"] = usb
    sys.modules["usb.core"] = usb.core
    sys.modules["usb.util"] = usb.util


_install_usb_stub()

from evk.cx3_console import Cx3Console          # noqa: E402
from evk.vd56g3 import Vd56g3                    # noqa: E402
from evk import patch as patchmod               # noqa: E402
from evk import raw as rawmod                    # noqa: E402
from firmware import vd56g3_registers as reg     # noqa: E402


class _FakeConsole:
    """Records (keyword, args) instead of touching USB."""

    def __init__(self):
        self.calls = []

    def query(self, kw, args=b""):
        self.calls.append((kw, bytes(args)))
        return b""


# ---------------------------------------------------------------- the tests
def test_console_request_framing():
    line = Cx3Console.build_request("I2CWR", bytes([0x20, 0x03, 0x0A, 0x0A, 0x00]))
    assert line == b"I2CWR 20 03 0A 0A 00\r\n"


def test_register_write_encoding():
    # [capture-confirmed] writes go via I2CWRRD rdlen=0:
    # [0, 0, i2c, reg_hi, reg_lo, value_LE...]; addr big-endian, value LE.
    fc = _FakeConsole()
    Vd56g3(fc).write16(0x030A, 10)
    assert fc.calls[-1] == ("I2CWRRD", bytes([0x00, 0x00, 0x20, 0x03, 0x0A, 0x0A, 0x00]))


def test_stream_output_ctrl_is_0x0335():
    # Regression: ST's constants shadow-define STREAM_STATICS_OUTPUT_CTRL; the
    # runtime value (and our register) must be 0x0335, NOT 0x0096. (Synthetic path.)
    assert reg.STREAM_OUTPUT_CTRL == 0x0335
    fc = _FakeConsole()
    w, h = Vd56g3(fc).apply_stream_registers(8)
    assert fc.calls[-1] == ("I2CWRRD", bytes([0x00, 0x00, 0x20, 0x03, 0x35, 0x01, 0x00]))
    assert (w, h) == (1104, 1360)


class _ReplayFake:
    """Answers reads like real hardware (hw-confirmed 2026-07-16): command
    registers self-clear to 0, SYSTEM_FSM walks 2 after CMD_BOOT and 3 after
    CMD_START_STREAM (0x0201) <- 1."""

    def __init__(self):
        self.calls = []
        self.fsm = 1

    def query(self, kw, args=b""):
        args = bytes(args)
        self.calls.append((kw, args))
        if kw == "I2CWRRD" and len(args) >= 5:
            rdlen = (args[0] << 8) | args[1]
            regad = (args[3] << 8) | args[4]
            if rdlen == 0:  # write
                if regad == 0x0200 and args[5:] == b"\x01":
                    self.fsm = 2
                elif regad == 0x0201 and args[5:] == b"\x01":
                    self.fsm = 3
                elif regad == 0x0202 and args[5:] == b"\x01":
                    self.fsm = 2
                return b"OK"
            if regad == 0x0028:  # SYSTEM_FSM
                return b"OK %02X" % self.fsm
            return b"OK" + b" 00" * rdlen
        return b"OK"


def test_replay_cold_init():
    # The hardware-captured replay: plays firmware/vd56g3_cold_init.json, needs
    # no patch, ends by STARTING the stream (CMD_START_STREAM 0x0201 <- 1 —
    # hardware-confirmed semantics), and must NOT replay the captured session's
    # trailing Stop (0x0202 <- 1).
    fc = _ReplayFake()
    w, h, bpp = Vd56g3(fc).replay_cold_init()
    writes = [a for kw, a in fc.calls if kw == "I2CWRRD" and a[:2] == b"\x00\x00"]
    assert writes[-1] == bytes([0x00, 0x00, 0x20, 0x02, 0x01, 0x01])       # start
    assert bytes([0x00, 0x00, 0x20, 0x02, 0x02, 0x01]) not in writes       # no stop
    assert fc.fsm == 3                                                     # streaming
    assert (w, h, bpp) == (1120, 1360, 10)
    # no VT-patch-sized burst; modest command count incl. handshake readbacks
    assert len(fc.calls) < 400


def test_cfgwr_payload():
    # The computed (derived-from-binary) CFGWR path, exercised with
    # prefer_captured=False (default replays the captured CFG2WR bytes instead).
    fc = _FakeConsole()
    Vd56g3(fc).configure_csi(lanes=2, data_rate_mbps=1500, width=1116, height=1356,
                             bpp=8, prefer_captured=False)
    kw, args = fc.calls[-1]
    assert kw == "CFGWR"
    assert args == bytes([0x02, 0x00, 0x00, 0x00, 0x05, 0xDC, 0x04, 0x5C, 0x05, 0x4C, 0x08, 0x00])


def test_cfg2wr_captured_replay():
    # The device uses CFG2WR; configure_csi replays the captured bytes verbatim.
    fc = _FakeConsole()
    Vd56g3(fc).configure_csi(bpp=8)
    assert fc.calls[-1] == ("CFG2WR", bytes([0x26, 0x02, 0x04, 0xBB, 0x8C, 0x40, 0x04, 0x00, 0x04, 0x00, 0x08, 0x64]))
    fc = _FakeConsole()
    Vd56g3(fc).configure_csi(bpp=10)
    assert fc.calls[-1] == ("CFG2WR", bytes([0x26, 0x02, 0x05, 0xEE, 0x3F, 0xE0, 0x04, 0x00, 0x04, 0x00, 0x0A, 0x64]))


def test_i2cwrrd_read_framing():
    fc = _FakeConsole()
    try:
        Vd56g3(fc).read16(0x0028)
    except Exception:
        pass
    # [rdlen_hi, rdlen_lo, i2c, reg_hi, reg_lo]
    assert fc.calls[0] == ("I2CWRRD", bytes([0x00, 0x02, 0x20, 0x00, 0x28]))


def test_vt_patch_replay():
    # Optional VT patch (not needed to stream). Writes via I2CWRRD rdlen=0.
    fc = _FakeConsole()
    n = patchmod.load_vt_patch(Vd56g3(fc))
    assert n == 3920
    assert fc.calls[0] == ("I2CWRRD", bytes([0x00, 0x00, 0x20, 0x02, 0x03, 0x01]))   # enter 0x0203<-1
    assert fc.calls[-1] == ("I2CWRRD", bytes([0x00, 0x00, 0x20, 0x02, 0x03, 0x02]))  # exit  0x0203<-2
    assert fc.calls[1] == ("I2CWRRD", bytes([0x00, 0x00, 0x20, 0xB8, 0x98, 0x11]))   # first 0xB898<-0x11
    assert len(fc.calls) == 3920 + 2


def test_read_reply_grammar_ok_prefix():
    # Capture-confirmed reply grammar: "OK <HH> <HH> ..." reassembled LSB-first.
    from evk.vd56g3 import _parse_read_payload, reply_is_ok
    assert _parse_read_payload(b"OK 28 00", 2) == bytes([0x28, 0x00])
    assert _parse_read_payload(b"OK 01", 1) == bytes([0x01])
    assert reply_is_ok(b"OK 01 07 01\r\n") is True
    assert reply_is_ok(b"ERR 1") is False

    class _ReplyConsole:
        def __init__(self, reply):
            self.reply = reply
        def query(self, kw, args=b""):
            return self.reply

    # read32 of "OK 28 00 00 00" -> 0x00000028 = 40 (little-endian)
    assert Vd56g3(_ReplyConsole(b"OK 28 00 00 00")).read32(0x1234) == 0x28
    # read16 of "OK 34 12" -> 0x1234
    assert Vd56g3(_ReplyConsole(b"OK 34 12")).read16(0x0000) == 0x1234


def test_warm_sensor_fallback():
    # Absent main patch -> False, no crash.
    assert patchmod.load_main_patch(Vd56g3(_FakeConsole()), path="firmware/_does_not_exist.bin") is False


def _synth_frame(width, bpp, y_size, fill_base=100):
    xb = bpp * width // 8
    frame = bytearray((2 + y_size) * xb)

    def put8(r, v):
        off = (2 * r + 6) if r < 0x7D else (xb + 2 * (r - 0x7D) + 6)
        frame[off] = v

    put8(0x5B, bpp)                                   # FORMAT_CTRL
    put8(0x94, y_size & 0xFF); put8(0x95, (y_size >> 8) & 0xFF)  # OUT_ROI_Y_SIZE (line 2)
    put8(0x50, 7); put8(0x51, 0)                      # FRAME_COUNTER
    put8(0x56, 1)                                     # CURRENT_CONTEXT
    for r in range(y_size):
        for c in range(xb):
            frame[(2 + r) * xb + c] = (fill_base + r) & 0xFF
    return bytes(frame)


def test_decode_frame_raw8():
    meta, img = rawmod.decode_frame(_synth_frame(192, 8, 3), 192)
    assert meta["bits_per_pixels"] == 8
    assert meta["height"] == 3
    assert meta["frame_counter"] == 7
    assert meta["current_context"] == 1
    assert img.shape == (3, 192)
    assert int(img[0, 0]) == 100 and int(img[2, 0]) == 102


def test_decode_raw10_unpack():
    row = np.array([[0xFF, 0x00, 0xAA, 0x55, 0b11100100]], dtype=np.uint8)  # b4 = 0xE4
    dec = rawmod.decode_raw_10(row)
    assert [int(x) for x in dec[0]] == [1020, 1, 682, 343]


def test_wire_frame_chunking():
    # Hardware-confirmed CX3 wire framing: 16384-byte chunks of
    # [16 B header][<=16352 B payload][16 B footer], final chunk short with no
    # footer; header = magic + u16 chunk idx + u16 + u32 frame seq + u32 len.
    payload = _synth_frame(192, 8, 100)  # (2+100)*192 = 19584 B -> 2 chunks
    wire = bytearray()
    off, idx = 0, 1
    while off < len(payload):
        part = payload[off : off + rawmod.WIRE_CHUNK_PAYLOAD]
        wire += (rawmod.WIRE_FRAME_MAGIC + idx.to_bytes(2, "little") + b"\x00\x00"
                 + (5).to_bytes(4, "little") + len(part).to_bytes(4, "little"))
        wire += part
        off += len(part)
        idx += 1
        if off < len(payload):
            wire += b"\xEE" * 16  # inter-chunk footer
    assert rawmod.wire_frame_size(len(payload)) == len(wire)

    stripped, seq = rawmod.strip_wire_chunks(bytes(wire))
    assert stripped == payload and seq == 5

    meta, img = rawmod.decode_frame(bytes(wire), 192)
    assert meta["frame_seq"] == 5
    assert meta["height"] == 100
    assert img.shape == (100, 192)

    # post-driver payloads (no magic) pass through untouched
    passthrough, seq2 = rawmod.strip_wire_chunks(payload)
    assert passthrough == payload and seq2 is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as err:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {err}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
