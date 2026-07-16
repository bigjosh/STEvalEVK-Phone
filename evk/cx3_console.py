"""
CX3 command console — ASCII request/response over a bulk endpoint pair.

Reimplements ST's native ``cx3_write_request`` / ``cx3_read_answer`` /
``cx3_query`` (from ``libcx3_spider_64.so``) in pure Python on top of pyusb.
See PROTOCOL.md §2 ("The CX3 command console") and §3 ("Command vocabulary").

Wire format (PROTOCOL.md §2, [binary-confirmed]):

    <KEYWORD> <HH> <HH> ... \\r\\n

  * ``KEYWORD`` is the ASCII command mnemonic (table below / PROTOCOL.md §3).
  * Each *argument byte* is emitted as a single space followed by exactly two
    UPPERCASE hex digits, most-significant nibble first (printf ``" %02X"``).
  * The line is terminated with CRLF (0x0D 0x0A). On the wire the firmware
    additionally NUL-terminates its *reply*; we strip that.

Example: ``I2CWR`` of bytes ``[0x20, 0x03, 0x0A, 0x0A, 0x00]`` goes out as the
literal ASCII string ``"I2CWR 20 03 0A 0A 00\\r\\n"``.

Transport (PROTOCOL.md §2, [binary-confirmed]):

  * request  -> command bulk-OUT endpoint,
  * reply    <- answer  bulk-IN endpoint, NUL-terminated by firmware,
  * bulk transfer timeout = 150 ms,
  * on a failed transfer: ``clear_halt`` the answer-IN endpoint and retry once.

Endpoint addresses are **auto-discovered** from the claimed interface
descriptor (first bulk-OUT = command, its matching bulk-IN = answer, the
larger/separate bulk-IN = video) and may be explicitly overridden. Exact
endpoint addresses are high-confidence-but-[needs-capture]; never hardcode them
as the only option (PROTOCOL.md §1, §7).
"""

from __future__ import annotations

import logging
from typing import Optional

import usb.core  # type: ignore
import usb.util  # type: ignore

logger = logging.getLogger("evk.cx3")

# --- Command vocabulary (PROTOCOL.md §3, [binary-confirmed]) ----------------
# 1-based indices in the firmware's internal 20-entry ``keywords`` table. Only
# the mnemonic matters on the wire (the index is documented for capture/replay
# cross-referencing with ST's native cx3_query(cmd_index, ...)).
KEYWORDS: dict[str, int] = {
    "ID": 1,
    "VERSION": 2,
    "I2CWR": 3,
    "I2CRD": 4,
    "IOSET": 5,
    "IOGET": 6,
    "SPIWRRD": 7,
    "NRST": 8,
    "CFGWR": 9,
    "CFGRD": 10,
    "CLKWR": 11,
    "CLKRD": 12,
    "I2CWRRD": 13,
    "IOCFGWR": 14,
    "IOCFGRD": 15,
    "LOGLVLWR": 16,
    "LOGLVLRD": 17,
    "CFG2WR": 18,
    "CFG2RD": 19,
    "RESET": 20,
}

# Transport constants (PROTOCOL.md §2).
BULK_TIMEOUT_MS = 150
CRLF = b"\r\n"
# Reply read chunk — firmware answers are short ASCII lines; the video path
# uses a separate, much larger endpoint (see read_video()).
_ANSWER_BUF = 4096


class Cx3ConsoleError(RuntimeError):
    """Raised when the console does not answer or reports a transport failure."""


class Cx3Console:
    """
    Request/response console wrapper around a pyusb ``usb.core.Device``.

    Parameters
    ----------
    device:
        An already-open (and, ideally, configuration-set) ``usb.core.Device``.
        On desktop Linux this comes from :func:`evk.usb_termux.open_by_vid_pid`;
        on Android it comes from :func:`evk.usb_termux.open_from_fd`.
    interface_number:
        Vendor interface to claim. ``None`` -> auto-pick the first interface
        that exposes a bulk-OUT + bulk-IN pair.
    ep_cmd_out / ep_ans_in / ep_video_in:
        Optional explicit endpoint *addresses* (e.g. ``0x01``, ``0x81``,
        ``0x82``). Any left ``None`` is auto-discovered from the interface
        descriptor. Overrides exist because exact addresses are
        [needs-capture] (PROTOCOL.md §1/§7) — do not assume the defaults.
    """

    def __init__(
        self,
        device: "usb.core.Device",
        interface_number: Optional[int] = None,
        ep_cmd_out: Optional[int] = None,
        ep_ans_in: Optional[int] = None,
        ep_video_in: Optional[int] = None,
    ) -> None:
        self.device = device
        self._claimed_interfaces: list[int] = []

        intf = self._select_interface(interface_number)
        self.interface_number = intf.bInterfaceNumber

        # Auto-discover the three bulk endpoints from the descriptor unless the
        # caller pinned them. PROTOCOL.md §1: command OUT, answer IN, video IN.
        d_cmd, d_ans, d_video = self._discover_endpoints(intf)
        self.ep_cmd_out = ep_cmd_out if ep_cmd_out is not None else d_cmd
        self.ep_ans_in = ep_ans_in if ep_ans_in is not None else d_ans
        self.ep_video_in = ep_video_in if ep_video_in is not None else d_video

        if self.ep_cmd_out is None or self.ep_ans_in is None:
            raise Cx3ConsoleError(
                "Could not resolve command/answer bulk endpoints from interface "
                f"{self.interface_number}; pass ep_cmd_out/ep_ans_in explicitly."
            )

        logger.info(
            "CX3 console on interface %d: cmd_out=0x%02X ans_in=0x%02X video_in=%s",
            self.interface_number,
            self.ep_cmd_out,
            self.ep_ans_in,
            "0x%02X" % self.ep_video_in if self.ep_video_in is not None else "None",
        )

        self._claim(self.interface_number)

        # Composite device (handoff observed MI_00 = "Stream"): if the console
        # interface has no separate video bulk-IN, look for one on another
        # interface and claim it too (PROTOCOL.md §1). Overrides skip this.
        if self.ep_video_in is None:
            self.ep_video_in = self._discover_video_cross_interface()
            if self.ep_video_in is not None:
                logger.info("video bulk-IN resolved on a separate interface: 0x%02X", self.ep_video_in)

    # ------------------------------------------------------------------ setup
    def _select_interface(self, interface_number: Optional[int]):
        """Return the ``usb.core.Interface`` to drive (active configuration)."""
        cfg = self.device.get_active_configuration()
        if interface_number is not None:
            for intf in cfg:
                if intf.bInterfaceNumber == interface_number:
                    return intf
            raise Cx3ConsoleError(f"Interface {interface_number} not found.")

        # Auto: first interface that has at least one bulk-OUT and one bulk-IN.
        for intf in cfg:
            outs, ins = self._bulk_endpoints(intf)
            if outs and ins:
                return intf
        # Fall back to the first interface so the caller gets a clearer later
        # error about endpoints rather than "no interface".
        return next(iter(cfg))

    @staticmethod
    def _bulk_endpoints(intf):
        """Return (bulk_out_eps, bulk_in_eps) for an interface, in descriptor order."""
        outs, ins = [], []
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                outs.append(ep)
            else:
                ins.append(ep)
        return outs, ins

    def _discover_endpoints(self, intf):
        """
        Heuristic endpoint discovery (PROTOCOL.md §1):

          * command OUT = first bulk-OUT.
          * answer  IN  = the bulk-IN whose endpoint *number* matches the
            command bulk-OUT (e.g. OUT 0x01 <-> IN 0x81) — the console
            request/answer pair.
          * video   IN  = the *other* bulk-IN (the streaming payload).

        We deliberately do NOT rank by ``wMaxPacketSize``: this device runs at
        SuperSpeed (PROTOCOL.md §1 requires the 5 Gbps cable), where every bulk
        endpoint reports the same 1024-byte wMaxPacketSize and burst capacity
        lives in the SuperSpeed *companion* descriptor (bMaxBurst), so packet
        size cannot distinguish the console pipe from the video pipe. Endpoint
        pairing by number is stable; when it can't disambiguate we fall back to
        descriptor order and warn. Exact addresses are [needs-capture] — pass
        ep_* overrides to be sure.

        Returns (ep_cmd_out, ep_ans_in, ep_video_in) as integer addresses (or
        ``None`` where not resolvable).
        """
        outs, ins = self._bulk_endpoints(intf)
        cmd_out = outs[0].bEndpointAddress if outs else None

        ans_in = None
        video_in = None
        if len(ins) == 1:
            ans_in = ins[0].bEndpointAddress
        elif len(ins) >= 2:
            out_num = (cmd_out & 0x0F) if cmd_out is not None else None
            paired = next((e for e in ins if (e.bEndpointAddress & 0x0F) == out_num), None)
            if paired is not None:
                ans_in = paired.bEndpointAddress
                others = [e for e in ins if e.bEndpointAddress != ans_in]
                video_in = others[0].bEndpointAddress if others else None
            else:
                # No number match: guess by descriptor order and make it loud.
                ans_in = ins[0].bEndpointAddress
                video_in = ins[1].bEndpointAddress
                logger.warning(
                    "endpoint pairing ambiguous (no bulk-IN matches cmd-OUT number "
                    "0x%02X); guessing ans_in=0x%02X video_in=0x%02X by descriptor "
                    "order — override with ep_ans_in/ep_video_in if VERSION fails.",
                    cmd_out or 0, ans_in, video_in,
                )
        return cmd_out, ans_in, video_in

    def _claim(self, interface_number: int) -> None:
        """Detach any kernel driver and claim the vendor interface."""
        try:
            if self.device.is_kernel_driver_active(interface_number):
                logger.debug("Detaching kernel driver from interface %d", interface_number)
                self.device.detach_kernel_driver(interface_number)
        except (NotImplementedError, usb.core.USBError):
            # Windows/libusbK and Android usbfs typically report "not implemented".
            pass
        usb.util.claim_interface(self.device, interface_number)
        if interface_number not in self._claimed_interfaces:
            self._claimed_interfaces.append(interface_number)
        logger.debug("Claimed interface %d", interface_number)

    def _discover_video_cross_interface(self) -> Optional[int]:
        """
        Find (and claim) a video bulk-IN on a *different* interface than the
        console's, for composite layouts where streaming lives on its own
        interface (handoff: MI_00 = "Stream"). Returns the endpoint address or
        ``None``. Pass ``ep_video_in`` (+ maybe ``interface_number``) explicitly
        for cross-interface layouts if this heuristic guesses wrong.
        """
        cfg = self.device.get_active_configuration()
        for intf in cfg:
            if intf.bInterfaceNumber == self.interface_number:
                continue
            _outs, ins = self._bulk_endpoints(intf)
            if not ins:
                continue
            try:
                self._claim(intf.bInterfaceNumber)
            except usb.core.USBError as err:
                logger.warning("could not claim candidate video interface %d: %s",
                               intf.bInterfaceNumber, err)
                continue
            return ins[0].bEndpointAddress
        return None

    # ------------------------------------------------------------- low level
    @staticmethod
    def build_request(keyword: str, args: bytes = b"") -> bytes:
        """
        Build the raw request line for *keyword* + *args* (PROTOCOL.md §2).

        Each arg byte -> ``" %02X"`` (uppercase, MSB nibble first); line ends
        with CRLF. The firmware NUL-terminates its reply, not the request.
        """
        if keyword not in KEYWORDS:
            raise Cx3ConsoleError(f"Unknown keyword {keyword!r} (not in KEYWORDS).")
        line = keyword.encode("ascii")
        for b in args:
            line += b" %02X" % b  # space + two UPPERCASE hex digits, high nibble first
        line += CRLF
        return line

    def _write_request(self, keyword: str, args: bytes = b"") -> bytes:
        """Send one request line to the command bulk-OUT endpoint."""
        line = self.build_request(keyword, args)
        logger.info("-> %s", line[:-2].decode("ascii", "replace"))  # log without CRLF
        self.device.write(self.ep_cmd_out, line, timeout=BULK_TIMEOUT_MS)
        return line

    def _read_answer(self) -> bytes:
        """
        Read one NUL-terminated reply from the answer bulk-IN endpoint.

        Firmware NUL-terminates (PROTOCOL.md §2); we also strip trailing
        whitespace/CRLF. Returned bytes exclude the NUL.
        """
        data = self.device.read(self.ep_ans_in, _ANSWER_BUF, timeout=BULK_TIMEOUT_MS)
        raw = bytes(data)
        nul = raw.find(b"\x00")
        if nul >= 0:
            raw = raw[:nul]
        raw = raw.rstrip(b"\r\n \t")
        logger.info("<- %s", raw.decode("ascii", "replace"))
        return raw

    # ---------------------------------------------------------------- public
    def query(self, keyword: str, args: bytes = b"") -> bytes:
        """
        Execute one console transaction: write request, read reply.

        Mirrors native ``cx3_query`` recovery (PROTOCOL.md §2): on a
        ``usb.core.USBError`` (timeout/stall) issue ``clear_halt`` on the
        answer-IN endpoint and retry exactly once. The log *is* the spec — every
        request/reply is emitted at INFO level.

        NOTE: the retry re-transmits the *whole* request (write + read), matching
        the native cx3_query loop which re-issues the command after clear_halt.
        This is safe here because every command we send is an idempotent set-write
        (register writes with fixed reg=value, CMD_STREAMING<-1/0), so a duplicate
        transmission has no side effect. If a non-idempotent command is ever added,
        revisit this to re-read only on a read-side failure.

        Returns the NUL/whitespace-stripped reply bytes.
        """
        try:
            self._write_request(keyword, args)
            return self._read_answer()
        except usb.core.USBError as first_err:
            logger.warning(
                "%s transfer failed (%s); clear_halt(0x%02X) + retry once",
                keyword,
                first_err,
                self.ep_ans_in,
            )
            try:
                self.device.clear_halt(self.ep_ans_in)
            except usb.core.USBError as halt_err:
                logger.debug("clear_halt failed: %s", halt_err)
            try:
                self._write_request(keyword, args)
                return self._read_answer()
            except usb.core.USBError as second_err:
                raise Cx3ConsoleError(
                    f"{keyword} failed twice: {first_err} / {second_err}"
                ) from second_err

    def read_video(self, length: int, timeout_ms: int = 1000) -> bytes:
        """
        Read up to *length* bytes from the video bulk-IN endpoint.

        Used by the frame grabber (PROTOCOL.md §5/§6 step 11). The video path
        needs a longer timeout than the 150 ms console default and a discovered
        (or overridden) video endpoint. Returns however many bytes arrived in
        this transfer (callers reassemble to the full frame size).
        """
        if self.ep_video_in is None:
            raise Cx3ConsoleError(
                "No video bulk-IN endpoint resolved; pass ep_video_in explicitly."
            )
        data = self.device.read(self.ep_video_in, length, timeout=timeout_ms)
        return bytes(data)

    # -------------------------------------------------------- convenience API
    def version(self) -> bytes:
        """``VERSION`` — CX3 firmware version string (console liveness check)."""
        return self.query("VERSION")

    def board_id(self) -> bytes:
        """``ID`` — board identifier."""
        return self.query("ID")

    def reset_bridge(self) -> bytes:
        """``RESET`` — reset the CX3 bridge."""
        return self.query("RESET")

    def close(self) -> None:
        """Release all claimed interfaces and dispose of pyusb resources."""
        for ifnum in self._claimed_interfaces:
            try:
                usb.util.release_interface(self.device, ifnum)
            except usb.core.USBError as err:
                logger.debug("release_interface(%d) failed: %s", ifnum, err)
        self._claimed_interfaces = []
        usb.util.dispose_resources(self.device)

    def __enter__(self) -> "Cx3Console":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
