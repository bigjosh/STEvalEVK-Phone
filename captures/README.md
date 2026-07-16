# captures/

Raw USB captures used to reverse-engineer and then confirm the protocol. These
are **local-only** — the raw `.pcap`/`.pcapng`/USBPcap files are tens of MB of
binary bus traffic and are git-ignored (see `../.gitignore`). Everything we
*derived* from them is committed under `../firmware/` and documented in
`../PROTOCOL.md` (§8 warm capture, §9 cold capture).

Captures referenced by the docs (drop your own here with these names to
reproduce, or point the tools at any path):

| file | what it is |
|---|---|
| `steval-connect` | Warm reconnect to an already-streaming sensor (§8). Confirmed endpoints, reply grammar, `CFG2WR`. |
| `cold`           | Cold GUI launch (§9). Yielded the full init sequence and proved the sensor streams **unpatched**. |

Reproduce the derived artifacts:

```
python tools/parse_usb_capture.py captures/cold                 # endpoint summary
python tools/extract_cold_init.py  captures/cold  firmware/vd56g3_cold_init.json
python tools/decode_usbpcap.py     captures/cold  --json captures/cold_decoded.json
```

To get an **untruncated** capture for full-frame validation, raise the USBPcap
snaplen (Wireshark ▸ Capture ▸ Options ▸ Snaplen = 0) — see `../docs/CAPTURE_HOWTO.md`.
