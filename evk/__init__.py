"""
evk — pure-Python (pyusb/libusb) host for the STEVAL-EVK-U0I.

A clean-room reimplementation of ST's STSW-IMG507 host protocol (the CX3
"spider" bridge + VD56G3 "S6G3" sensor), targeting Termux/pyusb on an Android
phone. ST ships only x86-64 Linux and x64 Windows binaries — there is **no**
aarch64 build of its SDK — so this package does *not* load any ST `.so`/`.dll`.
Everything here is driven directly from the protocol documented in
``PROTOCOL.md`` at the repo root, cross-checked against ST's shipped Python
example/decoder (``vdx6gx_frame_decoding.py``, ``vdx6gx_example_open_cv.py``,
``vdx6gx_constants.py``, ``image_sensor_python_sdk.py``).

Public surface:

- :class:`evk.cx3_console.Cx3Console` — the ASCII request/response console over
  the CX3 bulk endpoint pair (PROTOCOL.md §2–3).
- :class:`evk.vd56g3.Vd56g3` — VD56G3 register access + CSI/stream config +
  ``cold_init`` init-to-first-frame sequence (PROTOCOL.md §3.1, §4, §6).
- :mod:`evk.patch` — VT patch replay (embedded, extracted) and the main-FW
  patch seam (needs-capture blocker; PROTOCOL.md §6 step 6).
- :mod:`evk.raw` — frame decoder mirroring ST's ``vdx6gx_frame_decoding.py``.
- :mod:`evk.usb_termux` — Termux fd adoption via ``libusb_wrap_sys_device``.
"""

from __future__ import annotations

from .cx3_console import Cx3Console
from .vd56g3 import Vd56g3

__all__ = ["Cx3Console", "Vd56g3"]

__version__ = "0.1.0"
