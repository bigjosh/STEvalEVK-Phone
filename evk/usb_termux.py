"""
USB device opening — Termux (Android fd adoption) and desktop-Linux fallback.

Under Android, stock pyusb/libusb **cannot enumerate** USB devices: an app has
no permission to walk ``/dev/bus/usb`` and there is no usbfs access without
root. Termux solves this with ``termux-usb``: it obtains an Android
``UsbDeviceConnection`` via the system permission dialog and hands your process
an **already-open file descriptor** for the device. libusb then adopts that fd
with ``libusb_wrap_sys_device`` (PROTOCOL.md open-Q #4 — ST's own CX3 driver
uses this exact call, [binary-confirmed]).

This module exposes:

  * :func:`open_from_fd` — the Termux path: wrap the fd libusb-side, return a
    pyusb ``Device``.
  * :func:`open_by_vid_pid` — desktop-Linux fallback for testing without a phone
    (normal pyusb enumeration).

Backend note: fd adoption requires **libusb 1.0.16+** and a pyusb libusb1
backend. On Termux: ``pkg install libusb`` then ``pip install pyusb``. We reach
into the libusb1 backend's ctypes handle to call ``libusb_wrap_sys_device``,
which stock pyusb does not surface directly.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Optional

import usb.core  # type: ignore
import usb.backend.libusb1 as libusb1  # type: ignore

logger = logging.getLogger("evk.usb")

CX3_VID = 0x0553
CX3_PID = 0x040A

# libusb option: don't scan /dev/bus/usb (denied on non-root Android). Must be
# set before the context is initialized. Value 2 in libusb 1.0.27+
# (older header name: LIBUSB_OPTION_WEAK_AUTHORITY).
LIBUSB_OPTION_NO_DEVICE_DISCOVERY = 2


def open_by_vid_pid(vid: int = CX3_VID, pid: int = CX3_PID) -> "usb.core.Device":
    """
    Desktop-Linux fallback: find + open the device by VID:PID via normal pyusb
    enumeration. Sets the first configuration. Not usable on Android (see
    module docstring) — use :func:`open_from_fd` there.
    """
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise RuntimeError(
            f"USB device {vid:04X}:{pid:04X} not found. On a phone use "
            f"open_from_fd() with the termux-usb fd; enumeration does not work "
            f"under Android."
        )
    _detach_and_configure(dev)
    logger.info("Opened %04X:%04X by enumeration (bus=%s addr=%s)", vid, pid, dev.bus, dev.address)
    return dev


def open_from_fd(fd: int, vid: int = CX3_VID, pid: int = CX3_PID) -> "usb.core.Device":
    """
    Termux path: adopt an already-open device *fd* (from ``termux-usb -e``) with
    ``libusb_wrap_sys_device`` and return a pyusb ``Device``.

    ``termux-usb -r -e '<wrapper>' /dev/bus/usb/BBB/DDD`` opens the device,
    requests the Android permission, then execs the wrapper appending the fd as
    the last argument. ``grab.py --fd N`` receives that N here.

    Mechanism:
      1. Get the libusb1 backend and its raw ``ctypes`` libusb handle.
      2. ``libusb_wrap_sys_device(ctx, fd, &dev_handle)`` -> a device handle for
         the adopted fd (no enumeration, no re-open).
      3. Build a pyusb ``Device`` around the underlying ``libusb_device`` so the
         rest of the stack (claim_interface, bulk read/write) works normally.

    Returns a pyusb ``Device`` ready to claim. Raises ``RuntimeError`` with an
    actionable message if the backend is too old or the wrap fails.

    ⚠ HIGHEST-RISK UNVERIFIED PATH. This reaches into pyusb's private libusb1
    internals to adopt an already-open handle, and it has not been run against a
    real Pixel yet (see STATE.md / PROTOCOL.md §0.4). pyusb's internal
    ``_Device``/``open_device`` shapes vary across versions; the code below is
    written against mainline pyusb 1.2.x and degrades to a clear error. If it
    fails on-device, the robust fallback is to drive the wrapped handle through
    the raw ``libusb1``/``usb1`` package (ctypes) instead of pyusb — see
    docs/TERMUX_SETUP.md.
    """
    backend = libusb1.get_backend()
    if backend is None:
        raise RuntimeError(
            "No libusb1 backend available. On Termux: `pkg install libusb` and "
            "`pip install pyusb`, then retry."
        )
    lib = backend.lib  # ctypes CDLL for libusb-1.0
    ctx = backend.ctx  # libusb_context*

    # Tell libusb not to enumerate /dev/bus/usb — denied on non-root Android and
    # the documented precondition for the fd-adoption path. Best-effort: it only
    # takes full effect if set before context init, but setting it on the default
    # (NULL) context here is harmless and covers builds that honor it late.
    if hasattr(lib, "libusb_set_option"):
        try:
            lib.libusb_set_option.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.libusb_set_option.restype = ctypes.c_int
            lib.libusb_set_option(None, LIBUSB_OPTION_NO_DEVICE_DISCOVERY)
        except Exception as err:  # noqa: BLE001 - purely best-effort
            logger.debug("libusb_set_option(NO_DEVICE_DISCOVERY) skipped: %s", err)

    # libusb_wrap_sys_device(libusb_context*, intptr_t sys_dev, libusb_device_handle**)
    if not hasattr(lib, "libusb_wrap_sys_device"):
        raise RuntimeError(
            "This libusb build lacks libusb_wrap_sys_device (need >= 1.0.16). "
            "Update libusb (`pkg install libusb`)."
        )
    lib.libusb_wrap_sys_device.argtypes = [
        ctypes.c_void_p,   # context
        ctypes.c_void_p,   # intptr_t sys_dev (the fd, widened to pointer size)
        ctypes.POINTER(ctypes.c_void_p),  # libusb_device_handle**
    ]
    lib.libusb_wrap_sys_device.restype = ctypes.c_int

    dev_handle = ctypes.c_void_p()
    rc = lib.libusb_wrap_sys_device(ctx, ctypes.c_void_p(fd), ctypes.byref(dev_handle))
    if rc != 0 or not dev_handle:
        raise RuntimeError(
            f"libusb_wrap_sys_device(fd={fd}) failed (rc={rc}). On the Pixel this "
            f"can be a SELinux/usbfs denial or a stale fd. Confirm the 5 Gbps "
            f"cable, that termux-usb granted permission, and that the fd is the "
            f"last CLI arg."
        )
    logger.info("Adopted Termux fd %d via libusb_wrap_sys_device.", fd)

    dev = _pyusb_device_from_handle(backend, lib, dev_handle)
    _detach_and_configure(dev, already_open=True)
    return dev


def _pyusb_device_from_handle(backend, lib, dev_handle: "ctypes.c_void_p") -> "usb.core.Device":
    """
    Wrap a raw ``libusb_device_handle*`` (from libusb_wrap_sys_device) into a
    pyusb ``Device`` that reuses this already-open handle.

    Key correctness points (the previous version crashed here):

      * ``usb.core.Device(dev, backend)`` expects *dev* to be the backend's own
        device object (a ``libusb1._Device`` whose ``.devid`` is the
        ``libusb_device*``) — NOT a raw pointer. The Device constructor
        immediately calls ``backend.get_device_descriptor(dev)`` which
        dereferences ``dev.devid``, so a raw pointer raises inside ``__init__``.
      * pyusb's ``managed_open`` passes that ``_Device`` object (not a pointer)
        to ``backend.open_device``; our override must key on the object's
        ``.devid`` and return a handle wrapper exposing ``.handle`` (the
        ``libusb_device_handle*`` used by every bulk/claim call) and ``.devid``.
      * ``libusb_get_device`` returns a *borrowed* reference; pyusb's
        ``_Device`` finalizer unrefs it, so we ref it once to balance.
    """
    lib.libusb_get_device.argtypes = [ctypes.c_void_p]
    lib.libusb_get_device.restype = ctypes.c_void_p
    dev_p = lib.libusb_get_device(dev_handle)
    if not dev_p:
        raise RuntimeError("libusb_get_device() returned NULL for the wrapped handle.")

    # Balance the refcount that pyusb's _Device finalizer will drop.
    if hasattr(lib, "libusb_ref_device"):
        try:
            lib.libusb_ref_device.argtypes = [ctypes.c_void_p]
            lib.libusb_ref_device.restype = ctypes.c_void_p
            lib.libusb_ref_device(dev_p)
        except Exception as err:  # noqa: BLE001
            logger.debug("libusb_ref_device skipped: %s", err)

    # Build pyusb's own backend device wrapper (constructor arity varies by
    # version: newer takes (devid, devs), older (devid)).
    _BackendDevice = getattr(libusb1, "_Device", None)
    if _BackendDevice is None:
        raise RuntimeError(
            "This pyusb build has no libusb1._Device; cannot adopt the fd handle. "
            "Use the raw libusb (usb1) fallback in docs/TERMUX_SETUP.md."
        )
    try:
        dev_obj = _BackendDevice(dev_p, None)
    except TypeError:
        dev_obj = _BackendDevice(dev_p)

    # A stand-in for pyusb's _DeviceHandle: only .handle and .devid are used by
    # the libusb1 backend's I/O methods.
    class _AdoptedHandle:
        def __init__(self, handle, devid):
            self.handle = handle    # libusb_device_handle*
            self.devid = devid      # libusb_device*

    adopted = _AdoptedHandle(dev_handle, dev_p)

    # Override backend.open_device so pyusb reuses our already-open handle
    # instead of calling libusb_open (which would re-open via the denied path).
    _orig_open = backend.open_device

    def _open_device(dev_wrapper):
        if getattr(dev_wrapper, "devid", None) == dev_p:
            return adopted
        return _orig_open(dev_wrapper)

    backend.open_device = _open_device  # type: ignore[assignment]

    dev = usb.core.Device(dev_obj, backend)
    logger.debug("Constructed pyusb Device over wrapped libusb handle (dev_p=%s).", dev_p)
    return dev


def _detach_and_configure(dev: "usb.core.Device", already_open: bool = False) -> None:
    """
    Common post-open steps: ensure a configuration is active (desktop path only)
    and log it. On the wrapped-fd (Android) path we must NOT call
    ``set_configuration``: the Android ``UsbDeviceConnection`` has already
    configured the device, and issuing SET_CONFIGURATION can reset the
    device/endpoints (and libusb-on-Android often can't do it at all).

    Parameters
    ----------
    already_open:
        True on the Termux fd path — suppresses the set_configuration fallback.
    """
    try:
        cfg = dev.get_active_configuration()
        logger.debug("Active configuration: bConfigurationValue=%s", cfg.bConfigurationValue)
        return
    except usb.core.USBError as err:
        if already_open:
            # fd path: leave the Android-set configuration untouched.
            logger.debug(
                "fd path: no active config read (%s); NOT forcing set_configuration "
                "(Android already configured the device).", err,
            )
            return
        try:
            dev.set_configuration()
            logger.debug("Set default configuration.")
        except usb.core.USBError as set_err:
            logger.warning("set_configuration failed (%s); continuing.", set_err)


def open_device(fd: Optional[int] = None, vid: int = CX3_VID, pid: int = CX3_PID) -> "usb.core.Device":
    """
    Convenience dispatcher: if *fd* is given use the Termux fd path, else fall
    back to VID:PID enumeration (desktop Linux).
    """
    if fd is not None:
        return open_from_fd(fd, vid, pid)
    return open_by_vid_pid(vid, pid)
