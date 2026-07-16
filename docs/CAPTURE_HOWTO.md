# CAPTURE_HOWTO.md — sniffing a cold-init on the Windows reference PC

> **Status: mostly done.** The `captures/cold` session already gave us the full
> init sequence and proved **no FW patch is needed** (PROTOCOL.md §9). You only
> need a *new* capture for one thing now: a **frame decode reference**. The
> current captures were snaplen-truncated at 64 KB/URB, so the ~1.9 MB video
> transfers are cut off. To get whole frames, raise the capture's packet-size
> limit: in Wireshark **Capture ▸ Options ▸ (your USBPcap iface) ▸ set "Snaplen
> (B)" to 0 / unlimited** (or ≥ 2 MB) before recording. The patch-extraction goal
> is obsolete.

Goal (original): a `.pcapng` of a cold sensor init to confirm endpoints, reply
grammar, `CFG2WR`, and the reset/clock/boot sequences — all now confirmed. The
one remaining use is an **untruncated** capture for full-frame validation.

## You need
- The Windows 11 reference PC that already runs **STSW-IMG501 (EVK GS GUI)**.
- **Wireshark** with the **USBPcap** capture driver (bundled in the Wireshark
  installer — tick "Install USBPcap" during setup, then reboot).

## Steps
1. **Unplug the EVK** from USB. (We must capture a *cold* sensor so the main patch
   upload is in the trace — a warm sensor is already patched and won't re-upload.)
2. Open Wireshark. In the capture-interface list pick the **USBPcap** interface
   that corresponds to the root hub / port the EVK will use. If unsure, start
   capture on all USBPcap interfaces.
3. **Start capture.**
4. **Plug in the EVK** (use the genuine 5 Gbps C-to-C cable so it enumerates as it
   normally would; for the console/patch bytes even HighSpeed is acceptable).
5. Launch the **STSW-IMG501 GUI**, let the sensor **stream ~10 frames**, then
   **Stop** and close.
6. **Stop capture.** File → Save As → `capture.pcapng`. Drop it in this repo
   (e.g. `captures/cold_init.pcapng`).

## Trim (optional but helpful)
In Wireshark, filter to the EVK only:
```
usb.idVendor == 0x0553 && usb.idProduct == 0x040a
```
or filter by device address (`usb.device_address == N`). File →
Export Specified Packets to shrink it.

## Decode it
```
python tools/decode_usbpcap.py captures/cold_init.pcapng            # transcript
python tools/decode_usbpcap.py captures/cold_init.pcapng --json captures/init.json
python tools/decode_usbpcap.py captures/cold_init.pcapng \
       --patch-bin firmware/vd56g3_main_patch.bin                   # reconstruct patch
```
The decoder is container-agnostic (it scans for the ASCII console lines), so it
also works on a raw usbmon dump or a bulk-endpoint export.

## Sanity checks after decoding
- Early commands should include `VERSION`, a `CLKWR`, an `NRST`, then a large run
  of `I2CWR` writes (the main patch), then the VT-patch writes (addresses
  0xA000–0xD9F8 — compare against `firmware/vd56g3_vt_patch.json`), then the
  stream-config `Write*`/`CFGWR`/`CFG2WR`, then `CMD_STREAMING`.
- One decoded `I2CWR reg=0x030A` (FORMAT_CTRL) with value 8 or 10 confirms the
  register address byte-order assumption; if it looks swapped, re-run with
  `--data-le` / check `PROTOCOL.md §3.1`.
- Note the **endpoint addresses** Wireshark shows for the bulk OUT (console
  requests) / bulk IN (replies) / bulk IN (video) — record them in `PROTOCOL.md
  §1`.
