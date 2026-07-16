"""
VD56G3 (S6G3) register addresses used by the phone capture path.

A trimmed, hand-authored subset of the register map — only the registers the
init-to-first-frame sequence touches (see PROTOCOL.md §6). Addresses match ST's
`vdx6gx_constants.py` in STSW-IMG507. Multi-byte register *values* are
little-endian (LSB first) on the wire; register *addresses* are 16-bit
big-endian (see PROTOCOL.md §3.1).
"""

# --- status / identity (UM2602 Rev 8 STATUS group, read-only, per-frame) ---
DEVICE_MODEL_ID       = 0x0000  # u16
DEVICE_REVISION       = 0x0002  # u16
ERROR_CODE            = 0x001C  # u16  nonzero when SYSTEM_FSM=0xFF (ERROR state)
FWPATCH_REVISION      = 0x001E  # u16  -> expect 5.x after main patch ("FW=5.0")
VTIMING_RD_REVISION   = 0x0020  # u32  -> expect 17 after VT patch ("VT=17")
SYSTEM_FSM            = 0x0028  # u8   0=HW_STBY 1=READY_TO_BOOT 2=SW_STBY 3=STREAMING 0xFF=ERROR
TEMPERATURE           = 0x004C  # u10  degrees C (was misread as AE status earlier)
FRAME_COUNTER         = 0x0050  # u16  (also surfaced in status line)
CURRENT_CONTEXT       = 0x0056  # u8
FORMAT_CTRL_STATUS    = 0x005B  # u8   (status-line copy of format ctrl / bpp)
APPLIED_COARSE_EXP    = 0x0064  # u16  lines — what the sensor is actually using
APPLIED_ANALOG_GAIN   = 0x0068  # u5   code; gain = 32/(32-code)
APPLIED_DIGITAL_GAIN  = 0x006A  # FP5.8
AE_MODE_STATUS        = 0x0072  # u2   applied exposure mode (0 auto/1 freeze/2 manual)
AE_STATUS             = 0x0073  # u1   1 = AE converged
AE_MEAN_ENERGY        = 0x0074  # FP8.8 mean image energy
APPLIED_LINE_LENGTH   = 0x0078  # u16
APPLIED_FRAME_LENGTH  = 0x007C  # u16

# --- commands ------------------------------------------------------------
# Command registers self-clear to 0 once the sensor consumes the command
# (capture + hardware confirmed 2026-07-16) — poll the register back to 0
# before issuing the next command (Vd56g3.send_command).
#
# ⚠ SEMANTICS (hardware-confirmed 2026-07-16, contra ST's constant names):
#   0x0201 <- 0x01  STARTS streaming  (SYSTEM_FSM 2 -> 3)
#   0x0202 <- 0x01  STOPS  streaming  (SYSTEM_FSM 3 -> 2)
# In captures/cold, 0x0201<-01 precedes all 242 streamed frames and 0x0202<-01
# is the GUI's Stop at session end; probing the live sensor reproduced both
# transitions. 0x0201<-0x04 is also seen around init/stop (mode unknown).
CMD_BOOT              = 0x0200  # u8   write 1 to boot the sensor FW
CMD_START_STREAM      = 0x0201  # u8   write 1 to START streaming (FSM -> 3)
CMD_STOP_STREAM       = 0x0202  # u8   write 1 to STOP streaming (FSM -> 2)
CMD_DEBUG             = 0x0203  # u8   1=enter patch mode, 2=exit  (VT patch)
# Legacy aliases (ST vdx6gx_constants.py names — misleading, kept for grep):
CMD_STBY              = CMD_START_STREAM
CMD_STREAMING         = CMD_STOP_STREAM

# --- static stream config (STATIC group: SW_STANDBY-only, latches at
# --- START_STREAM — UM2602 §19.4) ------------------------------------------
LINE_LENGTH           = 0x0300  # u16  pixel clocks/line; min 1236 (10-bit ADC),
                                #      no documented max. Long lines = ST-sanctioned
                                #      way to slow output for slow receivers
                                #      (our USB-2 slow mode multiplies this).
STATICS_FORMAT_CTRL   = 0x030A  # u16  bits/pixel (8 or 10)
OIF_CSI_BITRATE       = 0x0312  # u16  Mbps/lane (device runs 1010)

# --- exposure control, context 0 (UM2602 §14.7; hardware-validated) --------
# CONTEXT + DYNAMIC groups are writable in SW_STANDBY or STREAMING. Wrap
# changes in GROUP_PARAM_HOLD: write 1 -> update -> write 0 (applied
# atomically on release; AE frozen while held).
GROUP_PARAM_HOLD      = 0x0448  # u1   GPH latch (DYNAMIC group)
CTX0_EXP_MODE         = 0x044C  # u2   0=Automatic AEC / 1=Freeze / 2=Manual
CTX0_MANUAL_AGAIN     = 0x044D  # u5   code 0..28; gain = 32/(32-code) (x1..x8,
                                #      clipped to x4 by default via 0x0960)
CTX0_MANUAL_COARSE    = 0x044E  # u16  lines; min 21, max FRAME_LENGTH-75;
                                #      out-of-range clips safely
CTX0_MANUAL_DGAIN_CH0 = 0x0450  # FP5.8 x1.0..x8.0 (CH0 suffices on mono)
CTX0_FRAME_LENGTH     = 0x0458  # u16  lines/frame (frame rate = 1/(line_t * FL))

# --- context 0 readout / ROI ---------------------------------------------
CONTEXTS_READOUT_CTRL = 0x0474  # u8
CTX0_OUT_ROI_X_START  = 0x045E  # u16
CTX0_OUT_ROI_X_END    = 0x0460  # u16
CTX0_OUT_ROI_Y_START  = 0x0462  # u16
CTX0_OUT_ROI_Y_END    = 0x0464  # u16
# STREAM_STATICS OUTPUT_CTRL. NOTE: ST's vdx6gx_constants.py DEFINES the symbol
# VDx6Gx_REG_STREAM_STATICS_OUTPUT_CTRL twice — 0x0096 (line 13) then 0x0335
# (line 212). The example does `from vdx6gx_constants import *`, so last-binding-
# wins makes the name resolve to 0x0335 at runtime, and Write16(...,1) hits
# 0x0335 (the real writable OUTPUT_CTRL). 0x0096 is the READ-ONLY status mirror
# (VDx6Gx_REG_STATUS_OUTPUT_CTRL). Do NOT "correct" this back to 0x0096.
STREAM_OUTPUT_CTRL    = 0x0335  # u8 (written via Write16 to mirror ST exactly)

# --- known-good full-res-ish window (from ST's open_cv example) ----------
DEFAULT = dict(
    bits_per_pixel=8,          # use RAW8 first on the phone
    x_start=2, x_end=1105,     # width 1104
    y_start=2, y_end=1361,     # height 1360
    # CX3 CSI receiver (CFGWR / CFG2WR):
    csi_lanes=2, csi_data_rate_mbps=1500,
    csi_width=1116, csi_height=1356, csi_pixel_clock_hz=160_800_000,
)

# I2C address of the sensor as seen by the CX3 (8-bit base; 7-bit = 0x10).
# CONFIRMED against real hardware (captures/steval-connect: all 745 I2CWRRD used 0x20).
SENSOR_I2C_ADDR = 0x20

# CFG2WR (CX3 CSI-2 receiver config, v2) payloads captured VERBATIM from ST's GUI
# on real hardware (captures/steval-connect). The device uses CFG2WR — NOT the
# CFGWR struct derived from the binary — so replay these 12 bytes for the matching
# bpp. Mirrors firmware/vd56g3_csi_cfg2wr.json (fetched by the web app). Partial
# decode: byte[1]=lanes, byte[10]=bpp, byte[2..5]=bpp-scaled timing (u32 BE);
# byte[0]=0x26, byte[6..9]=0x04000400, byte[11]=0x64 constant. Full semantics TBD.
CFG2WR_CAPTURED = {
    8:  bytes([0x26, 0x02, 0x04, 0xBB, 0x8C, 0x40, 0x04, 0x00, 0x04, 0x00, 0x08, 0x64]),
    10: bytes([0x26, 0x02, 0x05, 0xEE, 0x3F, 0xE0, 0x04, 0x00, 0x04, 0x00, 0x0A, 0x64]),
}
