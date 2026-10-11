# WDGWars Mesh + ADS-B Uploader — Debian fork

Feeds live aircraft and LoRa mesh nodes to [WDGWars](https://wdgwars.pl), with a local dashboard and map.

This is a **Debian Linux port** of [RocketGod's wdgwars-uploader](https://github.com/RocketGod-git/wdgwars-uploader), which is a Windows tray app. This fork keeps the Windows code working and adds native Linux support — developed and tested on Debian 13 (trixie) on a ClockworkPi uConsole (Raspberry Pi CM4). All credit for the app itself goes to [RocketGod](https://github.com/RocketGod-git); this fork only adapts the platform layer.

## 🔀 What this fork changes

- **Config** lives in `~/.config/wdgwars-uploader/config.json` on Linux (`%APPDATA%\WDGWarsUploader\` on Windows, as upstream).
- **ADS-B from the distro decoder**: on Linux the app reads a running [dump1090-fa](https://github.com/flightaware/dump1090)/readsb's `aircraft.json` (default `/run/dump1090-fa/aircraft.json`) and never downloads or launches a decoder — the system service owns the SDR. Any aircraft.json URL or file path works via Settings.
- **Mesh over TCP**: a mesh device port of `tcp:HOST[:PORT]` (e.g. `tcp:localhost`) reads a local [meshtasticd](https://meshtastic.org/docs/software/linux-native/) through the Meshtastic TCP API — no USB radio needed. Serial (`/dev/ttyUSB0`, `/dev/ttyACM0`) and BLE work as upstream.
- **RTL-SDR detection** via sysfs USB IDs instead of Windows PnP; no Zadig/WinUSB step exists on Linux.
- **Tray via AppIndicator** (`python3-gi` + ayatana libs). Without a tray host the app runs headless — the dashboard at `http://127.0.0.1:8787/` does everything, including Quit, and SIGTERM still persists the un-uploaded queue.
- **Start at login** is a systemd *user* service (`~/.config/systemd/user/wdgwars-uploader.service`) with `Restart=on-failure`, which also replaces the upstream Windows watchdog sidecar.

## 📡 Description

It runs in the tray (or headless), watches whatever radios are plugged in, and uploads on a schedule. Everything it hears is kept in its own store, so nodes and aircraft that the radio has already forgotten still make it into the next upload.

- **ADS-B** from an [RTL-SDR](https://www.rtl-sdr.com/) decoded by dump1090 (dump1090-fa/readsb on Linux, the [gvanem build](https://github.com/gvanem/Dump1090) on Windows), or from a [PortaPack running Mayhem](https://github.com/portapack-mayhem/mayhem-firmware), which decodes on-device and hands the log back over its USB console.
- **Mesh** from [Meshtastic](https://meshtastic.org/) and [MeshCore](https://meshcore.co.uk/) radios at the same time — over serial, BLE, or (this fork) the Meshtastic TCP API — read from the live packet stream rather than from the node list.
- **Dashboard** on loopback: live feed, counters, a map, WDGWars stats and leaderboard, and the prerequisite checks.

## 🐧 Debian install

```sh
sudo apt install dump1090-fa python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1
git clone https://github.com/JCrossTX/debian-wdgwars-uploader.git
cd debian-wdgwars-uploader
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt pystray
.venv/bin/python run_app.py            # tray (or headless) + dashboard
.venv/bin/python run_app.py --detect   # list detected radios
```

Then open `http://127.0.0.1:8787/`, paste your WDGWars API key into Settings, and enable "Start at login" if you want the systemd service installed.

Running meshtasticd on the same box? Add a mesh device with port `tcp:localhost` in Settings. Leave the Meshtastic hop-limit setting at 0 for a TCP node unless you want the app writing LoRa config into your daemon (which reboots the radio).

## 🛠️ Notes

- On Windows the RTL-SDR has to be on the WinUSB driver before any decoder can open it — that is what [Zadig](https://zadig.akeo.ie/) is for. On Linux the packaged decoder handles the dongle (dump1090-fa blacklists the DVB-T kernel driver itself).
- A radio's firmware caps how many nodes it will remember (80 on a Meshtastic nRF52 build, around 350 on MeshCore) and evicts the rest. The capture store is unbounded and survives restarts, so the count keeps climbing past that.
- The bundled Windows dump1090 build takes the receiver position and tuner gain from its own `dump1090.cfg`, not the command line. Left alone it ships `homepos = -1,-1`, which puts every single-frame position off the coast of West Africa. (On Linux, set the location in your decoder's own config, e.g. `dpkg-reconfigure dump1090-fa`.)
- The map is [OpenFreeMap](https://openfreemap.org/) — no key, no tile account.
- Aircraft details come from [adsbdb](https://www.adsbdb.com/) and photos from [Planespotters](https://www.planespotters.net/).
- The API key lives in the config file; it is never returned by the dashboard API or written to the log.

## 📦 Requirements

Debian 13 (trixie) or Windows, Python 3.11+, and at least one radio (or a local meshtasticd / networked decoder). An API key from your [WDGWars profile](https://wdgwars.pl/profile) is what the uploads are credited to.

For Windows installs, use [the upstream repo](https://github.com/RocketGod-git/wdgwars-uploader) — it ships the exe installer and tracks Windows fixes first.

## 🤝 Community

This app comes from **The Pirates' Plunder** community on Discord (RocketGod's server):

➡️ **[https://discord.gg/thepirates](https://discord.gg/thepirates)**

## 📄 License

[LICENSE](LICENSE) — same license as upstream.

<img width="1500" height="1000" alt="rocketgod_logo_transparent" src="https://github.com/user-attachments/assets/b14191a5-b509-44c4-be17-ad51e3226ec8" />
