# HomeTo64

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/badge/version-1.3.0-green)](https://github.com/JKnivesworthy/hometo64/releases)

**Turn your Commodore 64 into a fully functional Home Assistant control interface.**

Not just a dashboard — HomeTo64 lets you control lights, switches, locks, fans, covers and more directly from the C64 keyboard, while displaying live sensor data from your smart home in real time.

![HomeTo64 Logo](custom_components/hometo64/brand/logo.png)

---

## What is HomeTo64?

HomeTo64 is a Home Assistant custom integration that brings your smart home to a **Commodore 64** (or C128) via the **Ultimate 64** FPGA board or **1541 Ultimate II+** cartridge REST API.

Using DMA writes directly into C64 screen RAM, sensor values update live with no screen flicker, no reset, and no typing required. But the real breakthrough in v1.1 is **bidirectional control** — the C64 keyboard becomes a remote control for Home Assistant. Navigate your sensor pages with cursor keys, select any controllable entity, and press Return to toggle it. The response is near-instant.

This is believed to be the first Home Assistant integration to provide full bidirectional control with a Commodore 64.

---

## Compatible Hardware

**Primary platform:**
- **Ultimate 64** — a full FPGA reimplementation of the Commodore 64. The 6510 CPU, SID, VIC-II and RAM are all implemented in FPGA silicon, with the Ultimate REST API built in. This is the majority platform for HomeTo64 users.

**Also supported:**
- **Original C64 / C64C / C128** with a **1541 Ultimate II+** cartridge in the expansion port. The cartridge adds its own ARM processor and WiFi/ethernet, exposing the same REST API.

Both platforms use identical API endpoints — HomeTo64 works the same on either.

---

## Features

### Control
- 🎮 **Toggle entities from the C64 keyboard** — lights, switches, locks, fans, covers, scenes, automations, groups
- ⚡ **Near-instant response** — optimistic display update in under 100ms
- 🔘 **● cursor indicator** on controllable rows — navigate with up/down cursor keys, press Return to toggle
- 🖱️ **Remote control from HA** — Next Page button in the HA device panel, fully automatable

### Display
- 📺 **Live sensor dashboard** — values update without resetting or flickering the C64 screen
- 📑 **Up to 10 pages** of sensors, each with a custom heading and up to 10 sensors
- 🎬 **Splash screen** on launch featuring HA logo ASCII art
- 🎨 **Alternating bone-white / light-blue** sensor rows for easy reading
- 🔄 **State translation** — binary sensors show Open/Closed, Motion/Clear, Locked/Unlocked etc.

### Navigation
- ⌨️ **Left/right cursor keys** cycle pages on the C64
- 🔁 **Auto-cycle pages** — automatically advance pages after configurable inactivity period

### Management
- 🛑 **Stop RAM Writes** button — stop HA sending data when C64 is off
- ⏱️ **Auto-stop timeout** — stops RAM writes after configurable idle period
- 🔒 Optional network password support

---

## Installation

### Prerequisites

1. **Enable network access on your Ultimate 64 or 1541 Ultimate II+**
   - Open the Ultimate menu → **Network Settings**
   - Enable wired or WiFi network
   - Note the IP address shown — you'll need it during setup

2. **Install HomeTo64 via HACS (recommended)**
   - In HACS → Integrations → ⋮ → Custom repositories
   - Add `https://github.com/JKnivesworthy/hometo64` as an **Integration**
   - Install **HomeTo64** and restart Home Assistant

   **Or install manually** by copying `custom_components/hometo64` into your HA `custom_components` directory and restarting.

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration** → search **HomeTo64**
2. Enter your Ultimate IP address and optional network password
3. Configure display settings:
   - **Refresh Interval** — how often to update values (minimum 1 second)
   - **Auto-Stop RAM Writes** — stop after N minutes idle (0 = never)
   - **Screen Saver / Auto-Cycle Pages** — auto-advance pages after N seconds of keyboard inactivity (0 = off, max 255s)
   - **Number of Pages** — 1 to 10 pages
4. For each page: set a heading, pick up to 10 sensors, optionally rename them
5. Press **Run on C64** — the splash screen appears, then your dashboard loads

---

## Using the Dashboard

### Navigating pages
| Input | Action |
|-------|--------|
| Right cursor key | Next page |
| Left cursor key | Previous page |
| Next Page button (HA) | Advance page remotely |
| Auto-cycle | Automatically advances after inactivity |

### Controlling entities

Pages containing controllable entities display a **●** indicator at column 19 of each controllable row.

| Input | Action |
|-------|--------|
| Down cursor key | Move ● to next controllable entity |
| Up cursor key | Move ● to previous controllable entity |
| Return / Enter | Toggle selected entity |

The value on screen updates optimistically within 100ms. If the toggle fails, the correct state returns on the next refresh cycle.

> **Note:** Up to **8 sensors per page** can be cursor-selected and toggled (byte-width bitmap limit). Sensors 9–10 display values but cannot be toggled. Split entities across pages if you need more than 8 toggleable per page.

### Stopping updates
Press **Stop RAM Writes** when you are done. HA stops sending data immediately. Press **Run on C64** again to resume.

> HomeTo64 assumes the C64 is normally off and HA is always running. HA never writes to the C64 unsolicited — updates only begin after you press **Run on C64**.

---

## Screen Layout

```
row  0: blank
row  1:  -- HOME ASSISTANT DASHBOARD --   (heading, reverse video)
row  2: blank
row  3:  LIVING ROOM TEMP   ●   21.5 C   (bone white — ● = controllable, cursor here)
row  4: blank
row  5:  HUMIDITY               54 %     (light blue)
row  6: blank
row  7:  CEILING LIGHT      ●   On       (bone white — controllable)
...
row 24:  HOME ASSISTANT TO C64           (footer, always row 24)
```

---

## Technical Notes

### How it works

1. **Run on C64** pushes a tokenised C64 BASIC PRG via `POST /v1/runners:run_prg`
2. The PRG draws the dashboard and enters a non-blocking keyboard poll loop (`GET K$`)
3. HA writes sensor values directly to screen RAM via `PUT /v1/machine:writemem` (DMA)
4. When Return is pressed, BASIC POKEs slot+1 to `$CFCC`; HA reads it via `GET /v1/machine:readmem`, calls the HA service, and writes the optimistic display value back — all within ~100ms
5. Page navigation uses `$CFFF` as a shared index byte; `$CFCD` carries the controllable bitmap; `$CFCB` carries the auto-cycle interval

### RAM memory map

| Address | Purpose |
|---------|---------|
| `$CFCB` | Auto-cycle interval in jiffies (HA writes) |
| `$CFCC` | Action byte — C64 writes slot+1 on Return, HA reads and clears |
| `$CFCD` | Controllable bitmap — HA writes per-page bitmask |
| `$CFFF` | Navigation byte — HA writes page index for remote nav |

### API endpoints used

| Endpoint | Purpose |
|----------|---------|
| `GET /v1/info` | Connection test |
| `POST /v1/runners:run_prg` | Push and run the BASIC PRG |
| `PUT /v1/machine:writemem` | Write sensor values, bitmap, nav byte |
| `GET /v1/machine:readmem` | Poll for toggle requests from C64 keyboard |

### Controllable entity domains
`light` · `switch` · `input_boolean` · `fan` · `cover` · `lock` · `scene` · `automation` · `group`

---

## Changelog

### 1.3.0 — The Optimization Release
- **BASIC hot-loop rewrite** — key comparisons no longer allocate strings every
  frame (was calling `CHR$()` up to 7x per idle loop), which prevents the
  garbage-collection stalls BASIC V2 is notorious for on long-running programs
- **Early-out on idle** — skips key-comparison work entirely when no key is pressed
- **Bug fix**: cursor indicator (●) could fail to reappear after using the
  Next Page button or auto-cycle on a controllable page; keyboard nav already
  reset the flag that HA-driven nav was missing
- **Action poll reduced to 1 HTTP call** (was 3) — action, activity, and page
  nav read in a single round trip since they share adjacent RAM addresses
- **Value diff cache** — HA only writes sensor values that actually changed,
  instead of rewriting all 10 slots every refresh tick
- **Targeted page-clear** — page transitions clear only the outgoing page's
  used rows instead of always clearing all 10
- Removed dead code left over from an earlier auto-cycle implementation
- Fixed a duplicate function definition in the Ultimate API client
- Faster offline recovery — read timeout reduced from 10s to 5s

### 1.2.0 — The Control Release
- **Bidirectional control** — toggle HA entities directly from the C64 keyboard
- **Near-instant toggle response** — action poll at 100ms, optimistic display update
- **● cursor indicator** on controllable sensor rows
- **Splash screen** with HA logo ASCII art on launch
- **Auto-cycle pages** — configurable automatic page advancement on inactivity
- **Up to 10 pages** (was 3 in 1.0)
- **Next Page** and **Stop RAM Writes** buttons in HA device panel
- **Multi-page config UI** — configure each page independently
- **State translation** — binary sensors show human-readable states
- **Group entity support** for light groups and switch groups
- **Auto-stop timeout** for RAM writes

### 1.0.0 — Initial Release (Dashboard)
- Live sensor dashboard on Commodore 64 via Ultimate 64 / 1541 Ultimate II+
- Sensor values update via DMA without resetting the C64

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Cannot connect | Check IP address and network connectivity |
| Values not updating | Press **Run on C64** first; check HA logs for writemem errors |
| WiFi issues | Update Ultimate firmware to 3.11+; try refresh interval 5s+ |
| Screen scrambled | Press **Run on C64** to reinitialise |
| ● cursor not appearing | Page has no controllable entities, or bitmap not yet written — wait one refresh interval |
| Toggle not responding | Check HA logs; confirm entity domain is supported |
| Sensor shows On/Off instead of Open/Closed | Confirm `device_class` is set on the entity in HA |

---

## Planned Features
- 🎵 SID music playback during dashboard display

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE)

## Contributing

Pull requests welcome. Open an issue first for significant changes.

[github.com/JKnivesworthy/hometo64](https://github.com/JKnivesworthy/hometo64)
