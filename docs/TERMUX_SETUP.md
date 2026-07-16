# TERMUX_SETUP.md — capture a frame on an Android phone

On-phone steps to drive the STEVAL-EVK-U0I (CX3 + VD56G3) from Termux and save
one frame. This is the Phase-1 target (ST ships **no aarch64 build** of its SDK).
Use this route when the WebUSB app can't claim the interface — libusb here can
**force-detach** the interface, which Chrome-for-Android's WebUSB cannot.

## Quickstart — the direct path (`termux_grab.py`, recommended)

`termux_grab.py` is self-contained: **libusb via ctypes, no pyusb/numpy/Pillow**.
It force-detaches + claims both interfaces, replays the captured init (no patch),
reads one RAW10 frame, and writes `frame.pgm` (viewable) + `frame.raw`.

```sh
pkg install python libusb git
git clone https://github.com/bigjosh/STEvalEVK-Phone && cd STEvalEVK-Phone
chmod +x run_grab.sh
termux-usb -l                                   # find the EVK, e.g. /dev/bus/usb/001/002
termux-usb -r -e ./run_grab.sh /dev/bus/usb/001/002
termux-open frame.pgm                           # view the captured image
```

`termux-usb -r` pops the Android permission dialog and hands the device fd to the
wrapper, which runs `python termux_grab.py <fd>`. Add `-v` (edit `run_grab.sh` or
run `python termux_grab.py --fd <N> -v`) to log every console transaction.

If the console answers `VERSION -> OK 01 07 01` but frames don't arrive, it's
almost always the **cable** (needs genuine 5 Gbps) — the bulk video starves on
USB 2 even though the console works.

The `evk/` + `grab.py` pyusb version below is the structured alternative; prefer
`termux_grab.py` for the first capture (fewer moving parts).

---

## 0. Hardware prerequisites

- A **5 Gbps (USB 3.x SuperSpeed) cable** between phone and EVK. The video bulk
  stream starves on a USB-2 cable — the console may still answer, but frames
  won't arrive (`PROTOCOL.md` §1). Use a known-good SS cable and, if needed, a
  USB-C-to-A adapter that carries the SS pairs.
- The phone must supply enough VBUS or use a powered hub/OTG-Y cable. Note the
  EVK is **power-cycled** each time you plug it into the phone, so **both**
  firmware patches (main FW + VT) must be re-applied every session — there is no
  "already warm" sensor across a physical replug (`STATE.md`).

## 1. Install packages

```sh
pkg update
pkg install python libusb termux-api
pip install -r requirements.txt
```

- `libusb` (the **native** library) is required and separate from the `pyusb`
  Python binding; it must be **>= 1.0.16** for `libusb_wrap_sys_device`, the fd
  adoption call used in `evk/usb_termux.py`.
- `termux-api` provides `termux-usb`. Also install the **Termux:API** app from
  F-Droid so the permission dialog can appear.

## 2. Find the device

```sh
termux-usb -l
```

Lists connected USB devices as `/dev/bus/usb/BBB/DDD` paths. Identify the EVK
(VID:PID `0553:040A`). If nothing lists, replug and check the SS cable.

## 3. Grant permission and run

`termux-usb` cannot hand a raw device node to a normal process — it opens the
device, shows the Android permission dialog, then **execs a wrapper command with
the granted file descriptor appended as the last argument**. So the `-e` wrapper
must end with the flag that receives the fd:

```sh
termux-usb -r -e 'python grab.py --bpp 8 --out frame.jpg -v --fd' /dev/bus/usb/BBB/DDD
```

- `-r` requests permission (shows the dialog the first time).
- `-e '<cmd>'` is the wrapper; Termux appends the fd, so the command effectively
  becomes `python grab.py --bpp 8 --out frame.jpg -v --fd <FD>`. That is why
  `--fd` is the **last token** inside the quotes.
- `grab.py` reads that fd via `libusb_wrap_sys_device` (no enumeration — stock
  pyusb enumeration does **not** work under Android; the fd path is mandatory).

On success it prints `OK: wrote frame.jpg (...)` and exits 0. Non-zero exit
codes each carry a specific message (console silent, incomplete frame, etc.).

## 4. No firmware patch needed (the default replay)

By default `grab.py` replays the **hardware-captured cold-init sequence**
(`firmware/vd56g3_cold_init.json`, see `PROTOCOL.md` §9): clock/PLL → `CMD_BOOT`
→ ROI/exposure → `CFG2WR` → `CMD_STREAMING`. A cold USBPcap capture proved the
VD56G3 **streams with no firmware patch** (it read `FWPATCH_REVISION` = 0 the whole
time), so there is nothing to download and no patch step. This streams **RAW10 at
1120×1360** (what ST's GUI used).

The optional patches remain for anyone wanting the *enhanced* firmware, gated off:

- `--synthetic` uses the legacy ST-example register path instead of the replay.
- `--synthetic --apply-vt-patch` applies the (embedded, extracted) VT patch.
- `--synthetic --apply-main-patch --main-patch <blob>` applies a main FW patch
  if you ever obtain one. Neither is required to capture frames.

## 5. Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| `libusb_wrap_sys_device(fd=…) failed` | libusb too old (`pkg install libusb`), stale fd, or SELinux/usbfs denial on this device. |
| `CX3 console did not answer VERSION` | Wrong endpoints — try `--ep-cmd-out/--ep-ans-in/--ep-video-in` and `--interface`; confirm the SS cable. |
| `Captured N/M bytes — incomplete frame` | Bulk video stream starving — usually a USB-2 cable (needs the genuine 5 Gbps C-to-C), or the video endpoint guessed wrong (`--ep-video-in 0x83`). |
| Nothing in `termux-usb -l` | Replug; verify Termux:API app installed and OTG works. |

## 6. Endpoint / addressing overrides

Endpoint addresses are auto-discovered from the interface descriptor (first
bulk-OUT = command, matching bulk-IN = answer, larger bulk-IN = video), but the
exact addresses are still **[needs-capture]** (`PROTOCOL.md` §7). Override any of
them without editing code:

```sh
... grab.py --interface 0 --ep-cmd-out 0x01 --ep-ans-in 0x81 --ep-video-in 0x82 --fd
```
