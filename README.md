# WDGWars Mesh + ADS-B Uploader — Debian fork

Feeds live aircraft and LoRa mesh nodes to [WDGWars](https://wdgwars.pl), with a local dashboard and map.

This is a **Debian Linux port** of [RocketGod's wdgwars-uploader](https://github.com/RocketGod-git/wdgwars-uploader), which is a Windows tray app. This fork keeps the Windows code working and adds native Linux support — developed and tested on Debian 13 (trixie) on a ClockworkPi uConsole (Raspberry Pi CM4). All credit for the app itself goes to [RocketGod](https://github.com/RocketGod-git); this fork only adapts the platform layer.

**This branch (`dragonos-pi64`) is the [DragonOS Pi64](https://sourceforge.net/projects/dragonos-pi64/) version.** DragonOS Pi64 is the SDR-focused distribution built on Raspberry Pi OS 64-bit (Debian 13 trixie base); this branch was built and tested on it on a Raspberry Pi 5 with an RTL-SDR, a Meshtastic radio and a USB GPS. `main` is the plain Debian port.

## 🔀 What this fork changes

- **Config** lives in `~/.config/wdgwars-uploader/config.json` on Linux (`%APPDATA%\WDGWarsUploader\` on Windows, as upstream).
- **ADS-B from the system decoder**: on Linux the app reads a running [readsb](https://github.com/wiedehopf/readsb)/[dump1090-fa](https://github.com/flightaware/dump1090)'s `aircraft.json` (`/run/readsb/aircraft.json` or `/run/dump1090-fa/aircraft.json`, whichever is installed) and never downloads or launches a decoder — the system service owns the SDR. Any aircraft.json URL or file path works via Settings.
- **Mesh over TCP**: a mesh device port of `tcp:HOST[:PORT]` (e.g. `tcp:localhost`) reads a local [meshtasticd](https://meshtastic.org/docs/software/linux-native/) through the Meshtastic TCP API — no USB radio needed. Serial (`/dev/ttyUSB0`, `/dev/ttyACM0`) and BLE work as upstream.
- **RTL-SDR detection** via sysfs USB IDs instead of Windows PnP; no Zadig/WinUSB step exists on Linux.
- **Tray via AppIndicator** (`python3-gi` + ayatana libs). Without a tray host the app runs headless — the dashboard at `http://127.0.0.1:8787/` does everything, including Quit, and SIGTERM still persists the un-uploaded queue.
- **Receiver location from gpsd**: a USB GPS owned by gpsd feeds both the decoder (readsb `gpsd_in`) and the uploader's own gpsd client, so nothing has to be typed in. Manual lat/lon still works.
- **Start at login** is a systemd *user* service (`~/.config/systemd/user/wdgwars-uploader.service`) with `Restart=on-failure`, which also replaces the upstream Windows watchdog sidecar.

## 📡 Description

It runs in the tray (or headless), watches whatever radios are plugged in, and uploads on a schedule. Everything it hears is kept in its own store, so nodes and aircraft that the radio has already forgotten still make it into the next upload.

- **ADS-B** from an [RTL-SDR](https://www.rtl-sdr.com/) decoded by dump1090 (dump1090-fa/readsb on Linux, the [gvanem build](https://github.com/gvanem/Dump1090) on Windows), or from a [PortaPack running Mayhem](https://github.com/portapack-mayhem/mayhem-firmware), which decodes on-device and hands the log back over its USB console.
- **Mesh** from [Meshtastic](https://meshtastic.org/) and [MeshCore](https://meshcore.co.uk/) radios at the same time — over serial, BLE, or (this fork) the Meshtastic TCP API — read from the live packet stream rather than from the node list.
- **Dashboard** on loopback: live feed, counters, a map, WDGWars stats and leaderboard, and the prerequisite checks.

## 🐉 DragonOS Pi64 install

```sh
git clone -b dragonos-pi64 https://github.com/JCrossTX/debian-wdgwars-uploader.git
cd debian-wdgwars-uploader
./install-dragonos-pi64.sh --service   # decoder + venv + start-at-login service
.venv/bin/python run_app.py --detect   # list detected radios
```

`install-dragonos-pi64.sh` does everything the app needs on DragonOS Pi64 (trixie):

- **readsb with RTL-SDR support**, built from [upstream](https://github.com/wiedehopf/readsb) and installed as a `.deb` (then put on `apt-mark hold`). The `readsb` package in the Raspberry Pi OS / Debian archive is built *without* librtlsdr — it only accepts `modesbeast`/`gnshulc`/`ifile`/`none` and crash-loops on `--device-type rtlsdr` — and `dump1090-fa` is not in the Debian or Raspberry Pi archives at all. Its config is `/etc/default/readsb`; it writes `/run/readsb/aircraft.json`.
- **DVB-T kernel driver blacklist** (`/etc/modprobe.d/blacklist-rtl-sdr.conf`) so `dvb_usb_rtl28xxu` stops claiming the dongle at plug-in and librtlsdr tools (readsb, rtl_sdr, SDRTrunk, CubicSDR, Kismet) can open it.
- `python3-venv`, `python3-gi` + `gir1.2-ayatanaappindicator3-0.1` (tray on labwc/wf-panel-pi, via StatusNotifier), and the Python venv with `requirements.txt`.
- With `--service`: enables the systemd *user* unit so the uploader starts at login (runs headless; the dashboard still does everything).

Without the script, the manual equivalent is `python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt` plus a decoder that can open your SDR.

Then open `http://127.0.0.1:8787/`, paste your WDGWars API key into Settings, add your mesh radio (port `auto` or a stable `/dev/serial/by-id/...` path, protocol MeshCore or Meshtastic) and save.

Receiver position: plug in a USB GPS (see the next section) or add `--lat <deg> --lon <deg>` to `RECEIVER_OPTIONS` in `/etc/default/readsb`, `sudo systemctl restart readsb`, and enter the same location under Settings → Receiver location.

## 📍 Receiver location from a USB GPS

Plug in a USB GPS receiver (tested with a u-blox 7) and gpsd picks it up through udev. Two things read it:

- **readsb** takes its receiver position from gpsd via `--net-connector 127.0.0.1,2947,gpsd_in` in `/etc/default/readsb`; `install-dragonos-pi64.sh` adds that when gpsd is installed. That is what lets single-frame (local CPR) aircraft positions decode and puts the location in `receiver.json`.
- **The uploader** runs its own small gpsd client (`wdgwars_uploader/gps.py`, JSON over TCP 2947, no extra packages). The fix is the receiver reference for the aircraft and mesh outlier filters, shows on the dashboard's GPS card and as a marker on the map, and the last good fix is persisted so a restart does not wait for re-acquisition. Settings → *Receiver location*: **Auto** (GPS when it has a fix, else the manual lat/lon), **GPS only**, or **Manual**.

`sudo apt install gpsd` if it is missing. One caveat: gpsd's udev rules also treat common USB-serial chips (CP210x, PL2303, FTDI) as possible GPS receivers, so a mesh radio on a CP2102 (Heltec V3, T-Beam) can be grabbed by gpsd at plug-in. If that happens, set `USBAUTO="false"` in `/etc/default/gpsd` and name the real GPS in `DEVICES="/dev/serial/by-id/..."`.

Running meshtasticd on the same box? Add a mesh device with port `tcp:localhost` in Settings. Leave the Meshtastic hop-limit setting at 0 for a TCP node unless you want the app writing LoRa config into your daemon (which reboots the radio).

## 🛠️ Notes

- On Windows the RTL-SDR has to be on the WinUSB driver before any decoder can open it — that is what [Zadig](https://zadig.akeo.ie/) is for. On Linux the equivalent gotcha is the `dvb_usb_rtl28xxu` DVB-T kernel driver grabbing the dongle; `install-dragonos-pi64.sh` blacklists it (librtlsdr can also detach it on the fly, but a blacklist is cleaner).
- ESP32-S3 boards on native USB all show up as `303a:1001`, but two USB stacks hide behind that ID. The hardware USB-Serial-JTAG unit resets the board when DTR/RTS toggle, so the app holds those lines low for it; an Arduino/TinyUSB software CDC (what Meshtastic uses on native-USB ESP32-S3 boards) instead stays silent until the host asserts DTR. The app tells them apart by the USB interface string ("TinyUSB CDC") and asserts DTR for the latter — without that the Meshtastic connect times out while `meshtastic --info` works fine.
- A radio's firmware caps how many nodes it will remember (80 on a Meshtastic nRF52 build, around 350 on MeshCore) and evicts the rest. The capture store is unbounded and survives restarts, so the count keeps climbing past that.
- The bundled Windows dump1090 build takes the receiver position and tuner gain from its own `dump1090.cfg`, not the command line. Left alone it ships `homepos = -1,-1`, which puts every single-frame position off the coast of West Africa. (On Linux, set the location in your decoder's own config: `--lat/--lon` in `/etc/default/readsb`, or `dpkg-reconfigure dump1090-fa`.)
- The map is [OpenFreeMap](https://openfreemap.org/) — no key, no tile account.
- Aircraft details come from [adsbdb](https://www.adsbdb.com/) and photos from [Planespotters](https://www.planespotters.net/).
- The API key lives in the config file; it is never returned by the dashboard API or written to the log.

## 📦 Requirements

DragonOS Pi64 (trixie) on this branch (plain Debian 13 or Windows otherwise), Python 3.11+, and at least one radio (or a local meshtasticd / networked decoder). An API key from your [WDGWars profile](https://wdgwars.pl/profile) is what the uploads are credited to.

For Windows installs, use [the upstream repo](https://github.com/RocketGod-git/wdgwars-uploader) — it ships the exe installer and tracks Windows fixes first.

## 🤝 Community

This app comes from **The Pirates' Plunder** community on Discord (RocketGod's server):

➡️ **[https://discord.gg/thepirates](https://discord.gg/thepirates)**

## 📄 License

[LICENSE](LICENSE) — same license as upstream.

<img width="1500" height="1000" alt="rocketgod_logo_transparent" src="https://github.com/user-attachments/assets/b14191a5-b509-44c4-be17-ad51e3226ec8" />
