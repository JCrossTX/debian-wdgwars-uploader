# WDGWars Mesh + ADS-B Uploader

Tray app (Windows + Debian Linux) that feeds live aircraft and LoRa mesh nodes to [WDGWars](https://wdgwars.pl), with a local dashboard and map.

## 📡 Description

It runs in the tray, watches whatever radios are plugged in, and uploads on a schedule. Everything it hears is kept in its own store, so nodes and aircraft that the radio has already forgotten still make it into the next upload.

- **ADS-B** from an [RTL-SDR](https://www.rtl-sdr.com/) decoded on the PC by [dump1090](https://github.com/gvanem/Dump1090), or from a [PortaPack running Mayhem](https://github.com/portapack-mayhem/mayhem-firmware), which decodes on-device and hands the log back over its USB console.
- **Mesh** from [Meshtastic](https://meshtastic.org/) and [MeshCore](https://meshcore.co.uk/) radios at the same time, over serial or BLE, read from the live packet stream rather than from the node list.
- **Dashboard** on loopback: live feed, counters, a map, WDGWars stats and leaderboard, and the prerequisite checks.

## 🛠️ Notes

- The RTL-SDR has to be on the WinUSB driver before any decoder can open it — that is what [Zadig](https://zadig.akeo.ie/) is for.
- A radio's firmware caps how many nodes it will remember (80 on a Meshtastic nRF52 build, around 350 on MeshCore) and evicts the rest. The capture store is unbounded and survives restarts, so the count keeps climbing past that.
- The bundled dump1090 build takes the receiver position and tuner gain from its own `dump1090.cfg`, not the command line. Left alone it ships `homepos = -1,-1`, which puts every single-frame position off the coast of West Africa.
- The map is [OpenFreeMap](https://openfreemap.org/) — no key, no tile account.
- Aircraft details come from [adsbdb](https://www.adsbdb.com/) and photos from [Planespotters](https://www.planespotters.net/).
- The API key lives in `%APPDATA%\WDGWarsUploader\config.json`; it is never returned by the dashboard API or written to the log.

## 📦 Requirements

Windows or Debian 13 (trixie), Python 3.11+, and at least one radio. An API key from your [WDGWars profile](https://wdgwars.pl/profile) is what the uploads are credited to.

## 🐧 Debian / Raspberry Pi

This fork runs natively on Debian trixie (tested on a uConsole CM4). Differences from Windows:

- **Config** lives in `~/.config/wdgwars-uploader/config.json` (not `%APPDATA%`).
- **ADS-B** comes from the distro decoder, never a bundled one: `sudo apt install dump1090-fa` (or readsb) and the app reads `/run/dump1090-fa/aircraft.json` (the Linux default). Any aircraft.json URL/file works via Settings. The app never launches a decoder on Linux — the system service owns the SDR.
- **Mesh over TCP**: set a mesh device's port to `tcp:localhost` (or `tcp:host:4403`) to read a local [meshtasticd](https://meshtastic.org/docs/software/linux-native/) through the Meshtastic TCP API — no USB radio needed. Serial (`/dev/ttyUSB0`, `/dev/ttyACM0`) and BLE work as on Windows.
- **Tray** uses pystray's AppIndicator backend (`sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1`). Without a tray host the app runs headless and the dashboard at `http://127.0.0.1:8787/` does everything, including Quit.
- **Start at login** is a systemd user service (`~/.config/systemd/user/wdgwars-uploader.service`), toggled from Settings/tray like on Windows; it also restarts the app on crashes.

Install:

```sh
sudo apt install python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt pystray
.venv/bin/python run_app.py            # tray (or headless) + dashboard
.venv/bin/python run_app.py --detect   # list detected radios
```

## 🤝 Community

Join **The Pirates' Plunder** on Discord:

➡️ **[https://discord.gg/thepirates](https://discord.gg/thepirates)**

## 📄 License

[LICENSE](LICENSE)

<img width="1500" height="1000" alt="rocketgod_logo_transparent" src="https://github.com/user-attachments/assets/b14191a5-b509-44c4-be17-ad51e3226ec8" />
