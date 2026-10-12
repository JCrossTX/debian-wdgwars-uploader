#!/usr/bin/env bash
# DragonOS Pi64 (Raspberry Pi OS 64-bit, trixie) installer for the WDGWars uploader.
#
#   ./install-dragonos-pi64.sh            # decoder + tray libs + Python venv
#   ./install-dragonos-pi64.sh --service  # ...and enable start-at-login (systemd --user)
#
# What it does:
#  * builds readsb from upstream WITH RTL-SDR support and installs it as a .deb.
#    The readsb package in the Raspberry Pi OS / Debian archive lacks librtlsdr
#    and cannot open a dongle ("SDR type 'rtlsdr' not recognized");
#    dump1090-fa is not packaged at all.
#  * blacklists the DVB-T kernel driver so it stops claiming RTL2832U dongles
#  * points readsb at gpsd (gpsd_in) for the receiver position, if gpsd is installed
#  * installs python3-venv + the AppIndicator tray libs, builds .venv
#
# Re-running is safe: every step is skipped or refreshed as appropriate.
# Env: READSB_SRC (default ~/src/readsb) = where the readsb source is kept.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
READSB_SRC="${READSB_SRC:-$HOME/src/readsb}"
SERVICE=0
for a in "$@"; do
  case "$a" in
    --service) SERVICE=1 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

if [ ! -e /usr/share/rpd-wallpaper/dragonos.png ] && [ ! -e /usr/share/raspberrypi-artwork/dragonos.png ]; then
  echo "note: this script targets DragonOS Pi64 (Raspberry Pi OS 64-bit, trixie); running on" \
       "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-an unknown distro}") anyway" >&2
fi

echo "==> apt packages"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git build-essential debhelper help2man pkg-config libusb-1.0-0-dev librtlsdr-dev \
  libncurses-dev zlib1g-dev libzstd-dev rtl-sdr \
  python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1

echo "==> DVB-T kernel driver blacklist for RTL-SDR dongles"
BL=/etc/modprobe.d/blacklist-rtl-sdr.conf
if [ ! -f "$BL" ]; then
  sudo tee "$BL" >/dev/null <<'BLACKLIST'
# RTL2832U dongles on this box are software-defined radios, not TV tuners.
# Keep the DVB-T kernel drivers off them so librtlsdr-based tools (readsb,
# rtl_sdr, SDRTrunk, CubicSDR, Kismet) can claim the device cleanly.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
blacklist rtl2832_sdr
BLACKLIST
fi
# Unbind any dongle the DVB driver already holds, then unload (best effort).
for intf in /sys/bus/usb/drivers/dvb_usb_rtl28xxu/*:*; do
  [ -e "$intf" ] && basename "$intf" | sudo tee /sys/bus/usb/drivers/dvb_usb_rtl28xxu/unbind >/dev/null 2>&1 || true
done
for m in rtl2832_sdr rtl2832 dvb_usb_rtl28xxu; do sudo modprobe -r "$m" 2>/dev/null || true; done

echo "==> readsb with RTL-SDR support"
# The archive build answers: "SDR type 'rtlsdr' not recognized".
if command -v readsb >/dev/null && ! readsb --device-type rtlsdr --help 2>&1 | grep -q "not recognized"; then
  echo "    installed readsb already supports rtlsdr - skipping build"
else
  if [ -d "$READSB_SRC/.git" ]; then
    git -C "$READSB_SRC" pull --ff-only
  else
    mkdir -p "$(dirname "$READSB_SRC")"
    git clone --depth 1 --branch dev https://github.com/wiedehopf/readsb.git "$READSB_SRC"
  fi
  rm -f "$(dirname "$READSB_SRC")"/readsb_*.deb
  ( cd "$READSB_SRC" && DEB_BUILD_OPTIONS="noddebs parallel=$(nproc)" \
      dpkg-buildpackage -b -Prtlsdr -ui -uc -us )
  sudo dpkg -i "$(ls -t "$(dirname "$READSB_SRC")"/readsb_*.deb | head -1)"
  sudo apt-mark hold readsb >/dev/null   # keep apt from swapping back the no-RTL-SDR build
fi
sudo systemctl enable readsb >/dev/null 2>&1 || true
sudo systemctl restart readsb
echo "    readsb: $(systemctl is-active readsb)  (config: /etc/default/readsb, JSON: /run/readsb/aircraft.json)"

echo "==> receiver position from a USB GPS via gpsd"
if command -v gpsd >/dev/null; then
  if ! grep -q 'gpsd_in' /etc/default/readsb; then
    sudo sed -i 's/^NET_OPTIONS="\(.*\)"$/NET_OPTIONS="\1 --net-connector 127.0.0.1,2947,gpsd_in"/' /etc/default/readsb
    sudo systemctl restart readsb
  fi
  echo "    readsb takes its position from gpsd (127.0.0.1:2947); the uploader reads gpsd too"
else
  echo "    gpsd not installed: either 'sudo apt install gpsd' and re-run, or set --lat/--lon in /etc/default/readsb"
fi

echo "==> Python venv"
cd "$HERE"
[ -x .venv/bin/python ] || python3 -m venv --system-site-packages .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> detected radios"
.venv/bin/python run_app.py --detect || true

if [ "$SERVICE" = 1 ]; then
  echo "==> start-at-login (systemd user service)"
  .venv/bin/python - <<'PY'
from wdgwars_uploader import autostart
from wdgwars_uploader.config import Config
cfg = Config(); cfg.set("start_on_boot", True); cfg.save()
raise SystemExit(0 if autostart.enable() else 1)
PY
  systemctl --user restart wdgwars-uploader.service
  echo "    $(systemctl --user is-active wdgwars-uploader.service)  (journalctl --user -u wdgwars-uploader -f)"
fi

cat <<MSG

Done. Dashboard: http://127.0.0.1:8787/
  * paste your WDGWars API key in Settings
  * add your mesh radio there (port auto or /dev/serial/by-id/..., protocol MeshCore/Meshtastic)
  * receiver position: --lat/--lon in RECEIVER_OPTIONS of /etc/default/readsb,
    then: sudo systemctl restart readsb
  * run by hand:  .venv/bin/python run_app.py   (--detect / --once / --check)
MSG
