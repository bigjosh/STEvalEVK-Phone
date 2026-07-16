# DECISIONS_QUEUE.md — choices that need your input

_Updated 2026-07-15. Each item has a recommendation so work can continue if you
just say "go with the rec."_

---

## #1 — Phase 1 approach: reimplement, don't port  ⟵ recommend confirm

**Finding.** ST ships only x86-64 Linux and x64 Windows binaries. There is **no
aarch64 build**, so STSW-IMG507's `.so` cannot run in the Pixel's Termux. The
original plan ("run/adapt ST's Python SDK on the phone") isn't possible as a port.

**Good news.** The protocol (`PROTOCOL.md`) is small and fully recovered, so a
pure-Python (`pyusb`/libusb-over-Termux-fd) reimplementation is very tractable
and doubles as the exact spec for the Phase-2 WebUSB app.

**Options.**
- **(A) Reimplement the host in pure Python for Termux. ✅ recommended.** No ST
  binary on-device. ~a few hundred lines against `PROTOCOL.md`.
- (B) Try to run the x86-64 `.so` under emulation (QEMU/box64) on Android.
  Fragile, slow, still needs the missing patch file — not worth it.

_Default if no answer: proceed with (A)._

---

## #2 — Main VD56G3 FW patch  ⟵ ✅ **RESOLVED: not needed**

> **Resolved (2026-07-16):** the cold capture `captures/cold` proves the
> **VD56G3 streams with no firmware patch** — `FWPATCH_REVISION` reads 0
> throughout while frames flow (PROTOCOL.md §9). So ST's missing
> `Resources/…patch….bin` is **not required** to capture frames, and neither is
> the (already-extracted) VT patch. The real cold-init is just clock/PLL →
> `CMD_BOOT` → ROI/exposure → `CFG2WR` → `CMD_STREAMING`, now captured to
> `firmware/vd56g3_cold_init.json` and replayed verbatim by the code. The
> patch-upload code paths remain, gated off, for anyone wanting the *enhanced*
> firmware later. **No action needed.**

**Finding.** `S6G3::boot()` loads two patches: the **VT patch** (embedded —
already extracted to `firmware/vd56g3_vt_patch.json`) and a **main FW patch**
read from `Resources/S6H…G3_patch….bin`. **That `Resources/` folder is not in the
STSW-IMG507 download** (verified: the whole tree is just `.py/.so/.dll/.h`). A
cold-powered sensor needs this patch, and the phone power-cycles VBUS every
session, so we can't avoid it.

**Options (do the top two in parallel).**
- **(A) USBPcap capture on the Windows reference PC. ✅ recommended, most
  reliable.** Capture one *cold* init (unplug → start capture → launch the
  STSW-IMG501 GUI → let it stream a few frames). The main patch will appear as a
  long run of `I2CWR` writes to the patch-RAM region. Then:
  `python tools/decode_usbpcap.py capture.pcapng --patch-bin firmware/vd56g3_main_patch.bin`
  This *also* nails the endpoint addresses, reply grammar, `CFG2WR` timing, and
  the reset/clock/boot byte sequences in one shot — i.e. it clears every other
  "needs-capture" item too. **Capture recipe in `docs/CAPTURE_HOWTO.md`.**
- **(B) Find the `Resources/` folder without a sniff.** Check the installed
  STSW-IMG501 GUI directory (it must ship these patch/regmap files), or
  re-download STSW-IMG507 in case the folder is created by an installer step. If
  found, drop it in `vendor/Resources/` and we read the `.bin` directly.
- (C) Reconstruct from the sensor datasheet/errata patch tables — slow, and the
  exact patch revision matters. Last resort.

**What I need from you:** confirm you can do (A) on the Windows box (you said it's
available for sniffing), and/or check (B). Either unblocks a real phone capture.

_Default if no answer: I'll finish everything that doesn't need the patch
(reimplementation scaffolding, WebUSB skeleton, decoder) and leave the patch load
as a documented plug-in point._

---

## #3 — Capture scope, if you do #2(A)

One cold-init capture covers **all** remaining unknowns. Please grab, in a single
session: unplug → **begin capture** → plug in → GUI launch → **Start** → ~10
frames → **Stop**. Save as `.pcapng`. If USB3/SuperSpeed makes USBPcap noisy,
capturing at HighSpeed is fine for the *control/console* traffic (the patch and
register writes) even though real streaming needs the 5 Gbps cable — we only need
the console bytes from the sniff, not the video payload.

_Default if no answer: assume you'll provide one `.pcapng`; `decode_usbpcap.py`
is ready for it._

---

## #4 — First-frame target format  ⟵ settled by the capture

The hardware-proven replay (`firmware/vd56g3_cold_init.json`) streams **RAW10 at
1120×1360** (that's what ST's GUI used), and the decoder already unpacks RAW10, so
the default path is RAW10. RAW8 would need a modified init (bpp=8 `CFG2WR` + regs)
— doable later via the `--synthetic --bpp 8` path once we've confirmed a RAW10
frame on-device. No action needed unless you specifically want RAW8 first.
