# HIMAX.md — VD56G3 on Grove Vision AI V2 (HX6538 WE2): POC status

Goal: standalone MCU camera — capture one full-res frame from the VD56G3 (via
ST P-Board STEVAL-CAM-M0I, PCB4280D) on the Seeed Grove Vision AI Module V2
and pull it to the PC over USB serial. Companion docs: `../SENSOR.md` (sensor
bring-up bible), `../PROTOCOL.md` (EVK-era protocol).

## Status 2026-08-02

**Everything software works. The one blocker is physical: the MIPI pairs are
not connected through the current FFC adapter chain.**

Proven on hardware, in order:
1. Windows build chain: xpack ARM GCC 13.2.1 + xpack native make (devkitPro's
   MSYS make breaks gcc's temp dir — use the xpack one), image packaged by
   `we2_image_gen_local` (Windows exe), flashed via XMODEM at 921600.
2. **Hands-free flash + reset**: DTR toggle resets the board; the bootloader
   offers XMODEM on every boot. `flash_we2.py` does the whole cycle
   (reset → catch bootloader → send → answer reboot prompt → tail boot log).
   No buttons needed, ever.
3. **Sensor fully alive over the chain's I²C**: `MODEL_ID=0x5603` (50 ms after
   reset), FSM READY_TO_BOOT → BOOT → SW_STANDBY, full proven EVK init applied
   (12 MHz CLKIN confirmed — PLL values verbatim), THSENS skipped, manual
   exposure, LINE_LENGTH ×2, DT retagged RAW8, ISL off — then
   **START_STREAM → FSM=3 STREAMING, ERROR_CODE=0x0000**. The sensor is
   happily streaming into an unterminated link.
4. **Receiver-side proof of disconnection**: with the sensor guaranteed idle
   in SW_STANDBY (no HS possible), the WE2 D-PHY reports stop-state 0,0,0 for
   clk/l0/l1 — LP-11 (stop=1) is what wired-but-idle lanes must show. All
   three MIPI pairs float at the WE2. Additionally the sensor boots even
   without our XSHUTDOWN toggle (P-Board has a 100k pull-up + diode on XSDN),
   so NRST is likely not connected either. The pins that demonstrably pass
   through the chain: **3V3, GND, SCL, SDA — nothing else.**
5. The firmware self-probes a link matrix at every boot (bitrate 804/1010/500 ×
   OIF_CTRL lane topologies incl. 1-lane steering onto each physical pair ×
   polarity swaps × RX settle counts) and proceeds to capture + base64 dump on
   the first config that yields a frame. All permutations currently report
   info=0 err=0 phyerr=0 — consistent only with unwired pairs, not with any
   polarity/order/settle mismatch (those produce error IRQs).

## The cable problem (fix when back at the bench)

- P-Board J1 is a **24-pin 0.5 mm FFC** (Hirose FH12-24S class), NOT the
  22-pin RPi format. Pinout (from `datasheets/steval-cam-m0i-schematic.pdf`,
  geometric extraction): pin 5 GPIO1, 6 NRST, 10 SDA, 11 SCL, 14/15 CLK_N/P,
  17/18 D2_N/P, 20/21 D1_N/P, 23 P3V3, rest mostly GND.
- Grove V2 J2 is the **15-pin 1.0 mm classic RPi camera** connector:
  2/3 D0_N/P, 5/6 D1_N/P, 8/9 CLK_N/P, 11 cam-GPIO (net PA1), 13 SCL, 14 SDA,
  15 3V3.
- The current chain aligns only the power/I²C end. A correct chain must route
  all three MIPI pairs; the intended path is
  **P-Board →(ST's own cable that ships with STEVAL-CAM-M0I, 24-pin → RPi
  22-pin format)→ 22↔15 RPi "Standard–Mini" adapter → Grove V2**. Both hops
  follow RPi conventions at the 22-pin waypoint, so signals map by
  construction. Check contact-side orientation at every hop (contacts only
  count when facing the connector's contact side).
- Sanity check after recabling, before power: continuity from P-Board J1
  pins 20/21 (D1 pair) to Grove J2 pins 2/3 or 5/6.

When the cable is right, the already-flashed firmware will hit LINK UP in its
matrix (expected: 804 Mbps, OIF default polarity, hscnt 0x10) and immediately
dump the frame — just run the receiver.

## How to run everything

Build (PowerShell/Git Bash on this PC):

    cd D:/Github/Seeed_Grove_Vision_AI_Module_V2/EPII_CM55M_APP_S
    export PATH="/c/Users/passp/tools/xpack-windows-build-tools-4.4.1-3/bin:/c/Users/passp/tools/xpack-arm-none-eabi-gcc-13.2.1-1.1/bin:$PATH"
    export TMP='C:\Users\passp\AppData\Local\Temp' TEMP="$TMP"
    make -j8 APP_TYPE=vd56g3_poc
    cd ../we2_image_gen_local
    cp ../EPII_CM55M_APP_S/obj_epii_evb_icv30_bdv10/gnu_epii_evb_WLCSP65/EPII_CM55M_gnu_epii_evb_WLCSP65_s.elf input_case1_secboot/
    ./we2_local_image_gen.exe project_case1_blp_wlcsp.json

Flash (hands-free) and watch the boot log:

    python himax/flash_we2.py --port COM11 --file <sdk>/we2_image_gen_local/output_case1_sec_wlcsp/output.img --boot-log 60

Capture a frame to PNG/PGM/BIN (DTR-resets the board itself):

    python himax/receive_frame.py --port COM11

App sources: `himax/app/vd56g3_poc/` (authoritative copy; build happens in the
SDK clone at `D:/Github/Seeed_Grove_Vision_AI_Module_V2/EPII_CM55M_APP_S/app/
scenario_app/vd56g3_poc/` — keep both in sync when editing).

## WE2 specifics learned (beyond SENSOR.md)

- Raw INP→WDMA2 path stores **1 byte/pixel always** (disassembly-verified:
  wdma2_size = width×height, no depth term) → full 10-bit capture uses the
  packed-RAW10-as-RAW8 retag (`0x030F←0x2A`, width 1400 "pixels").
- Frame buffer: `.bss.NoInit` → SRAM01 region (1,904,000 B fits, 96.6% used;
  app code/data live in the two 256 KB TCMs).
- MIPI RX clock: use `SCU_HSCMIPICLKSRC_PLL` (200 MHz) like the stock IMX219
  driver, not RC96 (drain-rate + settle).
- `EPII_USECASE_SEL := drv_user_defined` requires the app dir to contain
  `drv_user_defined.mk` — without it every `IP_*` define vanishes and board
  files fail to compile with baffling SCU type errors.
- The framework `app/main.c` provides `main()` only for stock app defines;
  a new scenario app must define its own.
- OIF_CTRL (0x0308) power-on default reads 0x0000; the EVK stream worked
  without ever writing it. `sensordplib` event `-76` = EDM_WDT2_TIMEOUT (no
  line/frame activity reached the INP).
- Stock flasher `xmodem_send.py` crashes on cp1252 consoles (progress-bar
  glyph) — mid-transfer, silently. Use `flash_we2.py`.
