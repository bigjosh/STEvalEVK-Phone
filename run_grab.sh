#!/data/data/com.termux/files/usr/bin/sh
# Wrapper for termux-usb: it execs this with the device fd appended as the last
# argument. We forward everything to termux_grab.py.
#   termux-usb -r -e ./run_grab.sh /dev/bus/usb/BBB/DDD
exec python "$(dirname "$0")/termux_grab.py" "$@"
