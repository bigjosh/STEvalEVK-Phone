"""
Frame decoding — mirrors ST's ``vdx6gx_frame_decoding.py`` exactly.

The video bulk-IN payload is: **2 status lines** followed by ``y_size`` image
rows (PROTOCOL.md §5, [binary-confirmed from ST's decoder]). Status lines encode
sensor registers; the decoder reads back bits-per-pixel, Y size, frame counter
and current context from them, strips the 2 status lines, and unpacks RAW8
(passthrough) or RAW10 (ST's 5-byte→4-pixel MIPI packing).

Status-line register extraction offsets (ST ``extract_*_status_line_value8``):

  * register ``R`` with ``R < 0x7d``:  byte at ``2*R + 6``            (line 1)
  * register ``R`` with ``R >= 0x7d``: byte at ``W + 2*(R-0x7d) + 6`` (line 2)
    where ``W = frame_width_in_bytes = bpp*width/8``.

Multi-byte status values are LSB-first. Registers read (PROTOCOL.md §5):

  * FORMAT_CTRL   (0x5B, u8)  -> bits_per_pixel (must be 8 or 10)
  * OUT_ROI_Y_SIZE(0x94, u16) -> y_size
  * FRAME_COUNTER (0x50, u16)
  * CURRENT_CONTEXT(0x56, u8)

CX3 4-byte packing constraint: ``width * bpp`` must be a multiple of 32
(RAW8 -> width ×4; RAW10 -> width ×16).
"""

from __future__ import annotations

import logging
from math import gcd
from typing import Dict, Tuple

import numpy as np

logger = logging.getLogger("evk.raw")

# Status-line register addresses (ST vdx6gx_constants.py names, restated).
REG_STATUS_FRAME_COUNTER = 0x50    # u16
REG_STATUS_CURRENT_CONTEXT = 0x56  # u8
REG_STATUS_FORMAT_CTRL = 0x5B      # u8  -> bits per pixel
REG_STATUS_OUT_ROI_Y_SIZE = 0x94   # u16 -> y_size

_STATUS_LINE_COUNT = 2
_SECOND_LINE_THRESHOLD = 0x7D


# --------------------------------------------------------------- status lines
def _extract8(reg: int, raw: np.ndarray, width: int, bpp: int) -> int:
    """One status-line byte for register *reg* (ST extract_status_line_value8)."""
    if reg < _SECOND_LINE_THRESHOLD:
        return int(raw[2 * reg + 6])
    frame_width_in_bytes = int(width * bpp / 8)
    return int(raw[frame_width_in_bytes + 2 * (reg - _SECOND_LINE_THRESHOLD) + 6])


def _extract16(reg: int, raw: np.ndarray, width: int, bpp: int) -> int:
    """Two status-line bytes, LSB-first (ST extract_status_line_value16)."""
    lo = _extract8(reg, raw, width, bpp)
    hi = _extract8(reg + 1, raw, width, bpp)
    return lo + 256 * hi


# ------------------------------------------------------------------- RAW10
def decode_raw_10(image_buffer: np.ndarray) -> np.ndarray:
    """
    Unpack MIPI RAW10 (ST ``SensorDll.decode_raw_10``, PROTOCOL.md §5).

    Every 5 bytes encode 4 pixels: bytes 0-3 are the high 8 bits of px0-3,
    byte 4 holds the four low-2-bit pairs::

        px_i = (byte_i << 2) | ((byte4 >> (2*i)) & 0x03)

    Input is a 2-D ``(y_size, x_size_in_bytes)`` uint8 array; output is
    ``(y_size, width)`` uint16 (0..1023). Requires each row length to be a
    multiple of 5 (guaranteed when ``width*10`` is a multiple of 32 -> width is
    a multiple of 16 -> row bytes multiple of 20).
    """
    buf = np.asarray(image_buffer, dtype=np.uint8)
    if buf.ndim != 2:
        raise ValueError("decode_raw_10 expects a 2-D (rows, row_bytes) array")
    rows, row_bytes = buf.shape
    if row_bytes % 5 != 0:
        raise ValueError(f"RAW10 row byte count {row_bytes} is not a multiple of 5")

    flat = buf.reshape(rows, row_bytes // 5, 5).astype(np.uint16)
    b0, b1, b2, b3, b4 = (flat[:, :, i] for i in range(5))
    px0 = (b0 << 2) | ((b4 >> 0) & 0x03)
    px1 = (b1 << 2) | ((b4 >> 2) & 0x03)
    px2 = (b2 << 2) | ((b4 >> 4) & 0x03)
    px3 = (b3 << 2) | ((b4 >> 6) & 0x03)
    # Interleave px0..px3 back into pixel order along the row.
    out = np.empty((rows, (row_bytes // 5) * 4), dtype=np.uint16)
    out[:, 0::4] = px0
    out[:, 1::4] = px1
    out[:, 2::4] = px2
    out[:, 3::4] = px3
    return out


# ------------------------------------------------------------------- decode
def decode_frame(raw, width: int) -> Tuple[Dict, np.ndarray]:
    """
    Decode one raw video-bulk payload into (metadata, image).

    Mirrors ST ``vdx6gx_frame_decoding.decode_frame`` (PROTOCOL.md §5):

      1. Read bits_per_pixel from status reg 0x5B; assert in {8, 10}.
      2. Assert ``width*bpp`` is a multiple of 32 (CX3 4-byte packing).
      3. Read y_size (0x94), frame_counter (0x50), current_context (0x56).
      4. ``x_size_in_bytes = bpp*width/8``; image starts after 2 status lines.
      5. Reshape image to (y_size, x_size_in_bytes); RAW8 passthrough, RAW10
         unpack via :func:`decode_raw_10`.

    Parameters
    ----------
    raw:
        ``bytes`` or 1-D ``np.uint8`` array — the full payload (status lines +
        image rows) as delivered on the video bulk-IN.
    width:
        Active image width in pixels (from the ROI, e.g. 1104 or the CFG width).

    Returns
    -------
    (metadata, image):
        ``metadata`` = dict(width, height, bits_per_pixels, frame_counter,
        current_context); ``image`` = 2-D uint8 (RAW8) or uint16 (RAW10).
    """
    raw_frame = np.frombuffer(raw, dtype=np.uint8) if isinstance(raw, (bytes, bytearray)) else np.asarray(raw, dtype=np.uint8)

    # 1. bits per pixel (status reg 0x5B).
    bits_per_pixel = _extract8(REG_STATUS_FORMAT_CTRL, raw_frame, width, bpp=8)
    if bits_per_pixel not in (8, 10):
        raise ValueError(
            f"Unsupported bits_per_pixel={bits_per_pixel} from status line 0x5B "
            f"(expected 8 or 10). Frame likely misaligned or sensor not streaming."
        )

    # 2. CX3 4-byte packing constraint (ST: gcd check).
    cx3_limits = gcd(bits_per_pixel, 32)
    if (width * bits_per_pixel) % 32 != 0:
        raise ValueError(
            f"frame width must be a multiple of {32 // cx3_limits} when working "
            f"with {bits_per_pixel} bits due to CX3 limitations (width={width})"
        )

    # 3. y_size / frame_counter / current_context.
    y_size = _extract16(REG_STATUS_OUT_ROI_Y_SIZE, raw_frame, width, bits_per_pixel)
    frame_counter = _extract16(REG_STATUS_FRAME_COUNTER, raw_frame, width, bits_per_pixel)
    current_context = _extract8(REG_STATUS_CURRENT_CONTEXT, raw_frame, width, bits_per_pixel)

    # 4. Strip the 2 status lines; reshape image rows.
    x_size_in_bytes = int(bits_per_pixel * width / 8)
    image_start = _STATUS_LINE_COUNT * x_size_in_bytes
    expected = image_start + y_size * x_size_in_bytes
    if raw_frame.size < expected:
        raise ValueError(
            f"payload too short: have {raw_frame.size} bytes, need {expected} "
            f"(2 status lines + {y_size} rows x {x_size_in_bytes} B)"
        )
    image_buffer = raw_frame[image_start : image_start + y_size * x_size_in_bytes]
    image_buffer = image_buffer.reshape((y_size, x_size_in_bytes))

    # 5. Unpack.
    if bits_per_pixel == 10:
        decoded_image = decode_raw_10(image_buffer)
    else:
        decoded_image = image_buffer  # RAW8 passthrough (uint8)

    metadata = {
        "width": width,
        "height": y_size,
        "bits_per_pixels": bits_per_pixel,
        "frame_counter": frame_counter,
        "current_context": current_context,
    }
    logger.info(
        "decoded frame: %dx%d bpp=%d frame_counter=%d ctx=%d",
        width, y_size, bits_per_pixel, frame_counter, current_context,
    )
    return metadata, decoded_image


# ------------------------------------------------------------------- to JPEG
def to_jpeg(image: np.ndarray, path: str, quality: int = 92) -> None:
    """
    Save a decoded frame as a JPEG (grayscale). RAW10 (10-bit, 0..1023) is
    scaled down to 8-bit by a right-shift of 2; RAW8 is written as-is.

    Uses Pillow. The VD56G3 is a monochrome global-shutter sensor, so the frame
    is written as a single-channel 'L' image.
    """
    from PIL import Image  # local import so numpy-only users don't need Pillow

    arr = np.asarray(image)
    if arr.dtype == np.uint16:
        arr8 = (arr >> 2).astype(np.uint8)  # 10-bit -> 8-bit
    elif arr.dtype == np.uint8:
        arr8 = arr
    else:
        # Generic fallback: normalize to 0..255.
        a = arr.astype(np.float64)
        span = max(a.max() - a.min(), 1.0)
        arr8 = ((a - a.min()) / span * 255.0).astype(np.uint8)

    Image.fromarray(arr8, mode="L").save(path, format="JPEG", quality=quality)
    logger.info("wrote JPEG %s (%dx%d)", path, arr8.shape[1], arr8.shape[0])
