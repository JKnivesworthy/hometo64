# HomeTo64

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue)](https://www.home-assistant.io/)

Display your Home Assistant sensor data on a [**Commodore 64 Ultimate**](https://ultimate64.com/) using the REST API to load and control the program via HA.

![HomeTo64 Logo](custom_components/hometo64/brand/logo.png)

---

## Features
- 📺 Live sensor dashboard on your C64U
- 🔄 Automatic sensor value updates via direct RAM writes
- 📑 Up to **10 pages** of sensors, navigated with cursor keys or from HA
- ⌨️ Left/right cursor key navigation on the C64
- 🖱️ **Next Page** button in HA UI — navigate remotely or create your own timed automation!
- 🛑 **Stop RAM Writes** button — stop HA sending data when C64 is off or you change tasks
- ⏱️ Configurable RAM Write auto-stop timeout 
- 🔒 Optional network password support (Blank/None to match defaults)

---
## Requirements

- Home Assistant 2024.1 or later
- C64 Ultimate connected to the same local network as Home Assistant **wired or WiFi** network
---

## Installation

### Prerequisites

1. **Enable FTP services on C64U
   - On the C64, open the Ultimate menu and navigate to **Network Settings**
   - Enable the network interface (wired or WiFi)
   - Note the IP address shown — you will need it during setup
   - Enable FTP

2. **Install HomeTo64 in Home Assistant**

   **Via HACS (recommended)**
   - In HACS → Integrations → ⋮ → Custom repositories
   - Add `https://github.com/JKnivesworthy/hometo64` as an **Integration**
   - Install **HomeTo64** and restart Home Assistant

   **Manual**
   - Copy the `custom_components/hometo64` folder into your HA `custom_components` directory
   - Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for **HomeTo64**
2. Enter your Ultimate **IP address** and optional network password
3. Choose how many **pages** of sensors you want (1–10)
4. For each page, set a **heading** and select up to 10 sensors from your HA entities
5. Optionally customise each sensor's display name
6. Press **Run on C64** in the HomeTo64 device panel — your dashboard will appear immediately

That's it. Sensor values update automatically in the background at your configured interval. 
Navigate via HA integration or press Left/Right CRSR Key on the C64.

---

## How Data Works

> **Important:** Once you press **Run on C64**, Home Assistant will continuously write live sensor data directly into the C64's RAM via the Ultimate cartridge's DMA interface. This continues until one of the following stops it:
>
> - You press the **Stop RAM Writes** button in the device panel
> - Your configured **Auto-Stop timeout** is reached (default: 2 hours)
> - Home Assistant is restarted
>
> The C64 does not need to be on for HA to attempt writes — press **Stop RAM Writes** whenever you are done with the dashboard to avoid unnecessary network traffic to your Commodore.


## Usage

### Launching the dashboard

1. Press **Run on C64** in the HomeTo64 device panel
2. The C64 resets and the dashboard appears immediately
3. Sensor values update automatically at your configured interval

### Navigating pages

- **Right cursor key** — next page
- **Left cursor key** — previous page
- **Next Page** button in HA — advance page remotely (also automatable)

### Stopping updates

Press **Stop RAM Writes** when done. This prevents HA from writing to the C64's RAM while the C64 is off or in use for something else. Press **Run on C64** again to resume.

> **Note:** HomeTo64 assumes the C64 is normally off and HA is always running. Updates only start after you press **Run on C64** — HA never writes to the C64 unsolicited.

---

## Technical Notes

### How it works

1. **Run on C64** pushes a tokenised C64 BASIC PRG via the Ultimate's `run_prg` REST endpoint
2. The BASIC program draws the dashboard and enters a keyboard-polling spin loop
3. HA writes updated sensor values to scratch RAM at `$C000` via the `writemem` DMA endpoint
4. The BASIC program copies values from scratch RAM to screen RAM every ~3 seconds
5. Page navigation uses `$CFFF` (53247) as a shared page-index byte

### Ultimate API endpoints used

| Endpoint | Purpose |
|----------|---------|
| `GET /v1/info` | Connection test |
| `POST /v1/runners:run_prg` | Push and run the BASIC PRG |
| `PUT /v1/machine:writemem` | Update sensor values / navigate pages |

---

## Planned Features

- 🎵 SID music playback during dashboard display?
- 🖥️ Splash screen on launch

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Cannot connect during setup | Check IP address and network connectivity 
| Values not updating | Press **Run on C64** first; check HA logs for writemem errors 
| WiFi connection issues | Update Ultimate firmware to latest firmware and set refresh interval to 5s+ 
| Screen or Data scrambled | Press **Run on C64** to reinitialise 

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for details.
This software is free and open source. Any modifications must also be distributed under GPLv3.
