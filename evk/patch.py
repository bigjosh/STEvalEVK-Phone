"""
VD56G3 firmware patches — **OPTIONAL**. The VD56G3 streams with **no** firmware
patch (proven by ``captures/cold``: ``FWPATCH_REVISION`` reads 0 while frames
flow — see PROTOCOL.md §9). The default init path
(:meth:`evk.vd56g3.Vd56g3.replay_cold_init`) applies neither patch. These helpers
remain only for anyone who wants the *enhanced* firmware later.

  * **VT patch** ("VT=17", vertical-timing): embedded in ST's binaries and
    extracted to ``firmware/vd56g3_vt_patch.json`` (3920 register writes). Applied
    only via ``cold_init(..., apply_vt_patch=True)``. See :func:`load_vt_patch`.

  * **Main FW patch** ("FW=5.0"): ST loads it from a ``Resources/…patch….bin``
    file NOT shipped in STSW-IMG507. Not needed to stream; :func:`load_main_patch`
    applies it only if the blob is present (the exact upload mechanism is
    [needs-capture] and clearly stubbed below), else it logs a note and returns
    ``False``.

Writes go via ``I2CWRRD`` rdlen=0 (the proven encoding — see
:meth:`evk.vd56g3.Vd56g3.write_reg_bytes`).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle (vd56g3 imports patch)
    from .vd56g3 import Vd56g3

logger = logging.getLogger("evk.patch")

# Repo-relative default locations (resolved against the repo root at call time).
VT_PATCH_JSON = "firmware/vd56g3_vt_patch.json"
MAIN_PATCH_BIN = "firmware/vd56g3_main_patch.bin"

# CMD registers (mirrors firmware/vd56g3_registers.py; restated to keep this
# module importable without a hard dependency ordering).
REG_CMD_BOOT = 0x0200   # write 1 to boot after main patch load
REG_CMD_DEBUG = 0x0203  # 1 = enter patch mode, 2 = exit (VT patch)

# Main-patch RAM base region per PROTOCOL.md §6 step 6 ("base region 0x2000").
# [needs-capture] — the true target/stride is defined by the decoded USBPcap
# transcript, not confirmed here.
MAIN_PATCH_RAM_BASE = 0x2000


def _repo_path(path: str) -> str:
    """
    Resolve *path* relative to the repo root (two levels up from this file:
    ``<repo>/evk/patch.py`` -> ``<repo>``). Absolute paths pass through.
    """
    if os.path.isabs(path):
        return path
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, path)


def load_vt_patch(sensor: "Vd56g3", json_path: str = VT_PATCH_JSON) -> int:
    """
    Apply the VT patch to *sensor* (PROTOCOL.md §6 step 7, [binary-confirmed]).

    Procedure (from ``firmware/vd56g3_vt_patch.json``):

      1. ``Write8(enter_patch_mode.reg=0x0203, 1)``   — enter patch mode
      2. for each ``{reg, val}`` in ``writes``: ``Write8(reg, val)``
      3. ``Write8(exit_patch_mode.reg=0x0203, 2)``    — exit patch mode

    The JSON holds 3920 non-contiguous ``Write8`` pairs (addr 0xA000-0xD9F8), so
    these are individual I2CWR frames, not a burst. Returns the number of
    register writes replayed.
    """
    path = _repo_path(json_path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    enter = data["enter_patch_mode"]
    exit_ = data["exit_patch_mode"]
    writes = data["writes"]
    count = data.get("count", len(writes))

    if len(writes) != count:
        logger.warning("VT patch: 'count'=%d but 'writes' has %d entries", count, len(writes))

    logger.info(
        "VT patch: enter (reg=0x%04X <- %d), %d writes [0x%04X..0x%04X], exit (reg=0x%04X <- %d)",
        enter["reg"], enter["value"], len(writes),
        data.get("addr_min", 0), data.get("addr_max", 0),
        exit_["reg"], exit_["value"],
    )

    sensor.write8(enter["reg"], enter["value"])
    for i, entry in enumerate(writes):
        sensor.write8(entry["reg"], entry["val"])
        if (i + 1) % 500 == 0:
            logger.debug("VT patch: %d/%d writes", i + 1, len(writes))
    sensor.write8(exit_["reg"], exit_["value"])

    logger.info("VT patch applied: %d register writes.", len(writes))
    return len(writes)


def load_main_patch(sensor: "Vd56g3", path: str = MAIN_PATCH_BIN) -> bool:
    """
    Apply the main FW patch if ``firmware/vd56g3_main_patch.bin`` exists;
    otherwise fall back to warm-sensor mode (PROTOCOL.md §6 step 6 — the
    blocker).

    When the file is PRESENT:
        stream its bytes into sensor patch RAM, then ``Write8(0x0200, 1)``
        (CMD_BOOT). Returns ``True``.

    When the file is ABSENT:
        log a prominent warning and return ``False`` — the caller continues
        assuming the sensor is already patched (valid only if a prior host/GUI
        run patched it this power cycle).

    ⚠ NEEDS-CAPTURE — the exact upload mechanism is not yet known. ST's
    ``S6G3::loadPatch()`` streams ``Resources/S6H…G3_patch….bin`` to a patch-RAM
    region (PROTOCOL.md names base 0x2000) and then boots. Whether the transfer
    is a single burst to a fixed window, an address-prefixed stream, or a
    dedicated console command is undetermined until a USBPcap capture of one
    cold init is decoded (``tools/decode_usbpcap.py --patch-bin``). The burst
    below is the documented *seam*: it is structurally plausible and exercises
    the path, but MUST be validated against a capture before it is trusted.
    """
    resolved = _repo_path(path)
    if not os.path.exists(resolved):
        logger.warning(
            "=" * 72 + "\n"
            "MAIN FW PATCH MISSING: %s not found.\n"
            "Continuing in WARM-SENSOR MODE (assuming the VD56G3 is already\n"
            "patched from a prior host/GUI run this power cycle). A cold-powered\n"
            "sensor will NOT stream without it. To resolve: capture one cold init\n"
            "on the reference PC and reconstruct the blob with\n"
            "  tools/decode_usbpcap.py --patch-bin\n"
            "(see PROTOCOL.md §6 step 6 / DECISIONS_QUEUE.md).\n" + "=" * 72,
            resolved,
        )
        return False

    with open(resolved, "rb") as f:
        blob = f.read()

    # Fail loudly (not via a bare address-range ValueError deep in write_burst)
    # if the blob can't fit the assumed 16-bit I2C register window. NOTE: ST's
    # load_binary(path, start_register, burst_size) takes a *64-bit* register
    # address, so the real patch RAM may not live in 16-bit I2C space at all —
    # this whole addressing is NEEDS-CAPTURE.
    if MAIN_PATCH_RAM_BASE + len(blob) > 0x10000:
        raise ValueError(
            f"main patch ({len(blob)} bytes) from base 0x{MAIN_PATCH_RAM_BASE:04X} "
            f"overruns the 16-bit I2C register window (0x10000). The patch-RAM "
            f"addressing is NEEDS-CAPTURE (ST uses a 64-bit register address); pin "
            f"the real upload mechanism from a USBPcap capture before enabling this."
        )

    logger.info(
        "MAIN FW PATCH: streaming %d bytes to patch RAM base 0x%04X, then CMD_BOOT "
        "(mechanism is NEEDS-CAPTURE — validate against a USBPcap transcript).",
        len(blob), MAIN_PATCH_RAM_BASE,
    )

    # --- NEEDS-CAPTURE upload seam --------------------------------------------
    # Plausible structural default: address-prefixed burst into the patch-RAM
    # window. Replace with the exact transcript once decoded. Kept as a single
    # clearly-labelled call so the real mechanism drops in here verbatim.
    sensor.write_burst(MAIN_PATCH_RAM_BASE, blob)

    # Boot the freshly-patched firmware (PROTOCOL.md §6 step 6).
    sensor.write8(REG_CMD_BOOT, 1)
    logger.info("MAIN FW PATCH applied and CMD_BOOT issued.")
    return True
