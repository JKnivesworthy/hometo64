"""HomeTo64 — push Home Assistant sensor data to a Commodore 64 Ultimate via REST API."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_HEADING,
    CONF_SENSORS,
    CONF_ENTITY_ID,
    CONF_DISPLAY_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SENSORS,
)
from .c64_prg import (
    build_sensor_prg, build_hello_world_prg, build_multipage_prg,
    NAV_ADDR, scratch_addr, VALUE_WIDTH, SENSORS_PER_PAGE,
)
from .ultimate_api import run_prg, write_mem, UltimateAPIError
from .const import (
    CONF_PASSWORD, DEFAULT_PASSWORD,
    CONF_AUTO_STOP_MIN, DEFAULT_AUTO_STOP_MIN,
    CONF_PAGES, CONF_PAGE_HEADING, CONF_NUM_PAGES,
    DEFAULT_HEADING,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["button"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = HomeTo64Coordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator
    coordinator.start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id).stop()
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


class HomeTo64Coordinator:
    """Builds the PRG and keeps screen values updated via DMA writemem."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._task: asyncio.Task | None = None
        self._prg_cache: bytes = b""
        self._prg_running: bool = False   # True only after user presses "Run on C64"
        self._prg_started_at: float = 0.0  # monotonic time when PRG was last run
        self._current_page: int = 0         # tracked page index for Next Page button

    def _cfg(self, key: str, default=None):
        return {**self.entry.data, **self.entry.options}.get(key, default)

    @staticmethod
    def _translate_state(state) -> str:
        """Translate raw on/off to human-readable labels using device_class."""
        raw = state.state
        if raw not in ("on", "off"):
            return raw
        dc = state.attributes.get("device_class", "")
        on_map = {
            "door": "Open", "window": "Open", "garage_door": "Open",
            "opening": "Open", "lock": "Unlocked", "motion": "Motion",
            "occupancy": "Occupied", "presence": "Present",
            "smoke": "Smoke", "carbon_monoxide": "CO Alert",
            "heat": "Heat", "fire": "Fire", "moisture": "Wet",
            "leak": "Leak", "battery": "Low Batt",
            "connectivity": "Connected", "running": "Running",
            "tamper": "Tampered", "vibration": "Vibration",
            "plug": "On", "light": "On", "power": "On",
        }
        off_map = {
            "door": "Closed", "window": "Closed", "garage_door": "Closed",
            "opening": "Closed", "lock": "Locked", "motion": "Clear",
            "occupancy": "Clear", "presence": "Away",
            "smoke": "Clear", "carbon_monoxide": "Clear",
            "heat": "Normal", "fire": "Normal", "moisture": "Dry",
            "leak": "Dry", "battery": "OK",
            "connectivity": "Disconnected", "running": "Idle",
            "tamper": "OK", "vibration": "Clear",
            "plug": "Off", "light": "Off", "power": "Off",
        }
        m = on_map if raw == "on" else off_map
        return m.get(dc, "On" if raw == "on" else "Off")

    def _sensor_rows_for(self, sensor_configs: list[dict]) -> list[tuple[str, str]]:
        """Build sensor rows from an explicit sensor config list."""
        rows: list[tuple[str, str]] = []
        for cfg in sensor_configs:
            entity_id    = cfg.get(CONF_ENTITY_ID, "")
            display_name = cfg.get(CONF_DISPLAY_NAME, "").strip()
            if not entity_id:
                continue
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if not display_name:
                display_name = entity_id.split(".")[-1].replace("_", " ").title()
            unit  = state.attributes.get("unit_of_measurement", "")
            value = f"{self._translate_state(state)} {unit}".strip() if unit else self._translate_state(state)
            rows.append((display_name, value))
        return rows

    def _get_sensor_rows(self) -> list[tuple[str, str]]:
        """Read current sensor states and return (display_name, value) rows.

        Sensors are stored as [{"entity_id": ..., "display_name": ...}].
        The display_name is whatever the user set in the config flow — it is
        used directly on the C64 screen, so the entity's friendly name is only
        ever used as a fallback default during initial setup, not at render time.
        """
        sensor_configs: list[dict] = self._cfg(CONF_SENSORS, DEFAULT_SENSORS) or []
        rows: list[tuple[str, str]] = []

        for cfg in sensor_configs:
            entity_id    = cfg.get(CONF_ENTITY_ID, "")
            display_name = cfg.get(CONF_DISPLAY_NAME, "").strip()

            if not entity_id:
                continue

            state = self.hass.states.get(entity_id)
            if state is None:
                _LOGGER.debug("HomeTo64: entity %s not found in state machine", entity_id)
                continue

            # Use the user-configured display name directly — no fallback needed
            # because the config flow always sets one (from friendly name or user edit)
            if not display_name:
                display_name = entity_id.split(".")[-1].replace("_", " ").title()

            unit  = state.attributes.get("unit_of_measurement", "")
            value = f"{self._translate_state(state)} {unit}".strip() if unit else self._translate_state(state)

            rows.append((display_name, value))

        return rows

    def notify_prg_stopped(self) -> None:
        """Called by the Stop button. Disables writemem updates immediately."""
        self._prg_running = False
        _LOGGER.info("HomeTo64: writemem updates stopped by user")

    def notify_prg_running(self) -> None:
        """Called by the Run button after a successful run_prg push.
        Enables the writemem refresh loop to start updating screen values.
        """
        import time as _time
        self._prg_running = True
        self._prg_started_at = _time.monotonic()
        self._current_page = 0
        _LOGGER.debug("HomeTo64: PRG is now running — writemem updates enabled")

    def build_prg(self) -> bytes:
        """Build the PRG from current sensor states."""
        pages = self._get_pages()
        if not pages:
            prg = build_hello_world_prg(self._cfg(CONF_HEADING, DEFAULT_HEADING))
        elif len(pages) == 1:
            prg = build_sensor_prg(*pages[0])
        else:
            prg = build_multipage_prg(pages)
        self._prg_cache = prg
        return prg

    def _get_pages(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Return (heading, sensor_rows) for each configured page.

        Supports both the new CONF_PAGES format and the old single-page
        CONF_SENSORS + CONF_HEADING format for backward compatibility.
        """
        # New multi-page format
        pages_cfg = self._cfg(CONF_PAGES, None)
        if pages_cfg:
            result = []
            for page in pages_cfg:
                heading  = page.get(CONF_PAGE_HEADING, DEFAULT_HEADING)
                sensors  = page.get(CONF_SENSORS, [])
                rows     = self._sensor_rows_for(sensors)
                if rows:
                    result.append((heading, rows))
            return result
        # Legacy single-page format
        heading = self._cfg(CONF_HEADING, DEFAULT_HEADING)
        sensors = self._get_sensor_rows()
        return [(heading, sensors)] if sensors else []

    def start(self) -> None:
        self._task = self.hass.loop.create_task(
            self._refresh_loop(), name=f"hometo64_refresh_{self.entry.entry_id}"
        )
        _LOGGER.info("HomeTo64: coordinator started for %s", self._cfg(CONF_HOST))

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        _LOGGER.info("HomeTo64: coordinator stopped")

    # C64 screen RAM constants
    # After run_prg, our PRG places:
    #   Row 0: heading (reverse video)
    #   Row 1: blank
    #   Row 2+: sensor rows — name cols 0-19, value cols 20-39
    _C64_COLS = 40  # screen width; scratch RAM layout in c64_prg.py

    @staticmethod
    def _to_screen_code(ch: str) -> int:
        """Convert a single character to C64 screen code."""
        c = ord(ch.upper())
        if 0x20 <= c <= 0x3F:
            return c        # space, digits, punctuation
        if 0x40 <= c <= 0x5F:
            return c - 0x40 # A-Z → 0x01-0x1A
        return 0x20         # fallback to space

    def _to_screen_bytes(self, text: str, width: int) -> bytes:
        """Encode text as C64 screen codes, left-justified to width."""
        padded = text.upper()[:width].ljust(width)
        return bytes(self._to_screen_code(ch) for ch in padded)

    def _build_scratch_update(
        self, pages: list[tuple[str, list[tuple[str, str]]]]
    ) -> list[tuple[int, bytes]]:
        """Build (address, data) pairs for the scratch RAM value table at $C000.

        Each sensor value is 20 screen-code bytes at scratch_addr(page, sensor).
        HA writes all pages — BASIC reads whichever page is active.
        """
        writes: list[tuple[int, bytes]] = []
        for page_idx, (_, sensors) in enumerate(pages):
            for sensor_idx, (_, value) in enumerate(sensors[:SENSORS_PER_PAGE]):
                addr = scratch_addr(page_idx, sensor_idx)
                data = self._to_screen_bytes(value, VALUE_WIDTH)
                writes.append((addr, data))
        return writes

    async def _refresh_loop(self) -> None:
        """Update sensor values in C64 screen RAM via DMA — no reset, no flash."""
        _LOGGER.debug("HomeTo64: refresh loop started")
        while True:
            try:
                interval = int(self._cfg(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
                await asyncio.sleep(interval)
                self.build_prg()   # keep PRG cache fresh for manual "Run on C64"
                if not self._prg_running:
                    _LOGGER.debug("HomeTo64: PRG not yet run — skipping writemem")
                    continue
                # Auto-stop check
                import time as _time
                auto_stop = int(self._cfg(CONF_AUTO_STOP_MIN, DEFAULT_AUTO_STOP_MIN))
                if auto_stop > 0:
                    elapsed_min = (_time.monotonic() - self._prg_started_at) / 60
                    if elapsed_min >= auto_stop:
                        self._prg_running = False
                        _LOGGER.info(
                            "HomeTo64: auto-stop after %d minutes — "
                            "press 'Run on C64' to resume updates",
                            auto_stop,
                        )
                        continue
                host     = self._cfg(CONF_HOST)
                password = self._cfg(CONF_PASSWORD, DEFAULT_PASSWORD)
                pages   = self._get_pages()
                writes  = self._build_scratch_update(pages)
                n_total = sum(len(d) for _, d in writes)
                for addr, data in writes:
                    await self.hass.async_add_executor_job(
                        write_mem, host, addr, data, password
                    )
                _LOGGER.info(
                    "HomeTo64: wrote %d values (%d bytes) to scratch RAM",
                    len(writes), n_total,
                )
            except UltimateAPIError as exc:
                _LOGGER.warning("HomeTo64: writemem failed (C64 offline?) — %s", exc)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("HomeTo64: unexpected error in refresh loop — %s", exc)
