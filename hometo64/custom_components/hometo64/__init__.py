"""HomeTo64 — push Home Assistant sensor data to a Commodore 64 Ultimate via REST API."""

from __future__ import annotations

import asyncio
import time
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
from .ultimate_api import run_prg, write_mem, read_mem, UltimateAPIError
from .const import (
    SCRATCH_ACTION_ADDR, SCRATCH_BITMASK_ADDR,
    TOGGLEABLE_DOMAINS, OPTIMISTIC_TOGGLE,
)
from .const import (
    CONF_PASSWORD, DEFAULT_PASSWORD,
    CONF_AUTO_STOP_MIN, DEFAULT_AUTO_STOP_MIN,
    CONF_PAGES, CONF_PAGE_HEADING, CONF_NUM_PAGES,
    DEFAULT_HEADING,
    CONF_AUTO_CYCLE, DEFAULT_AUTO_CYCLE,
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
        self._prg_settled: bool = False     # True after settle delay post-run_prg
        self._task_action: asyncio.Task | None = None
        self._last_activity_val: int = 0    # last seen activity toggle byte
        # Diff cache: skip writemem for values unchanged since last write.
        self._value_cache: dict[int, bytes] = {}   # slot -> last written bytes
        self._value_cache_page: int = -1           # page the cache belongs to
        self._refresh_tick: int = 0                # for periodic full rewrite

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
        self._prg_running = True
        self._prg_settled = False   # will be set True after settle delay
        self._prg_started_at = time.monotonic()
        self._current_page = 0
        self._last_activity_val = 0
        self._last_interaction = time.monotonic()
        self._invalidate_value_cache()
        _LOGGER.debug("HomeTo64: PRG is now running — writemem updates enabled")
        async def _init_bitmask():
            try:
                h = self._cfg(CONF_HOST)
                p = self._cfg(CONF_PASSWORD, DEFAULT_PASSWORD)
                await self._write_bitmask(h, p, 0)
            except Exception:
                pass
        self.hass.loop.create_task(_init_bitmask())

    def _get_page_entities(self, page_index: int) -> list[dict]:
        """Return raw entity config list for a specific page."""
        pages_cfg = self._cfg(CONF_PAGES, None)
        if pages_cfg and page_index < len(pages_cfg):
            return pages_cfg[page_index].get(CONF_SENSORS, [])
        if page_index == 0:
            return self._cfg(CONF_SENSORS, [])
        return []

    def _controllable_bitmask(self, page_index: int) -> int:
        """Return bitmask of which slots are toggleable (bit N = slot N).

        Capped at 8 bits (slots 0-7) since the bitmap is a single byte.
        Slots 8-9 are displayed but not cursor-selectable.
        """
        entities = self._get_page_entities(page_index)
        mask = 0
        for i, cfg in enumerate(entities[:8]):   # only bits 0-7 fit in a byte
            eid = cfg.get(CONF_ENTITY_ID, "")
            domain = eid.split(".")[0] if "." in eid else ""
            if domain in TOGGLEABLE_DOMAINS:
                mask |= (1 << i)
        return mask & 0xFF   # guarantee single byte

    async def _clear_page_values(
        self, host: str, password: str, old_page: int | None = None
    ) -> None:
        """Write spaces to the sensor value columns of the outgoing page.

        If old_page is given, only that page's used rows are cleared
        (e.g. 4 writes instead of 10 — each is a full HTTP round trip).
        """
        rows = 10
        if old_page is not None:
            pages = self._get_pages()
            if 0 <= old_page < len(pages):
                rows = min(10, max(1, len(pages[old_page][1])))
        blank = bytes([0x20] * 20)   # 20 spaces in screen codes
        for i in range(rows):
            row  = 3 + i * 2
            addr = 1024 + row * 40 + 20
            await self.hass.async_add_executor_job(
                write_mem, host, addr, blank, password
            )
        self._invalidate_value_cache()

    def _invalidate_value_cache(self) -> None:
        """Force the next refresh tick to rewrite every value."""
        self._value_cache.clear()
        self._value_cache_page = -1

    async def _write_bitmask(self, host: str, password: str, page_index: int) -> None:
        """Write controllable bitmask for page to scratch RAM at $CFFD."""
        mask = self._controllable_bitmask(page_index)
        await self.hass.async_add_executor_job(
            write_mem, host, SCRATCH_BITMASK_ADDR, bytes([mask]), password
        )

    async def _action_poll_loop(self) -> None:
        """Poll $CFFC every 0.5s for C64 Return-key action requests.

        BASIC POKEs slot+1 to $CFFC when user presses Return on a
        controllable entity. We read it, call the HA service, write 0
        back to acknowledge, and write the optimistic state to scratch RAM.
        """
        _LOGGER.debug("HomeTo64: action poll loop started")
        while True:
            try:
                await asyncio.sleep(0.1)
                if not self._prg_running:
                    continue
                host     = self._cfg(CONF_HOST)
                password = self._cfg(CONF_PASSWORD, DEFAULT_PASSWORD)
                # ONE read covers all four shared bytes (contiguous at
                # $CFFC-$CFFF): [0]=action [1]=bitmask [2]=activity [3]=nav.
                # Was 3 separate HTTP round trips per 0.1s tick (30 req/s);
                # now 1 (10 req/s) — faster detection, 1/3 the traffic.
                data = await self.hass.async_add_executor_job(
                    read_mem, host, SCRATCH_ACTION_ADDR, 4, password
                )
                if not data or len(data) < 4:
                    continue
                if data[0] == 0:
                    # Cursor activity = toggle byte changed since last look
                    if data[2] != self._last_activity_val:
                        self._last_activity_val = data[2]
                        self._last_interaction = time.monotonic()
                        _LOGGER.debug("HomeTo64: cursor activity — screensaver reset")
                    # Page changed via keyboard navigation?
                    nav0 = data[3]
                    if nav0 != self._current_page:
                        old_page = self._current_page
                        self._current_page = nav0
                        self._last_interaction = time.monotonic()  # reset screensaver
                        # Clear bitmap immediately so stale page's bitmap
                        # doesn't trigger cursor init on the new page
                        await self.hass.async_add_executor_job(
                            write_mem, host, SCRATCH_BITMASK_ADDR, bytes([0]), password
                        )
                        await self._clear_page_values(host, password, old_page)
                        # Then write correct bitmap for new page
                        await self._write_bitmask(host, password, self._current_page)
                        _LOGGER.debug("HomeTo64: page changed to %d via keyboard",
                                      self._current_page)
                    continue

                # Action byte: 1-10 = slot 0-9 (Return key = toggle)
                raw_val = data[0]
                if not (1 <= raw_val <= 10):
                    continue
                slot = raw_val - 1
                self._last_interaction = time.monotonic()  # reset screensaver
                forced_state = None
                _LOGGER.info("HomeTo64: action slot=%d page=%d",
                             slot, self._current_page)

                # Clear action byte immediately
                await self.hass.async_add_executor_job(
                    write_mem, host, SCRATCH_ACTION_ADDR, bytes([0]), password
                )

                entities = self._get_page_entities(self._current_page)
                if slot >= len(entities):
                    continue
                entity_id = entities[slot].get(CONF_ENTITY_ID, "")
                domain    = entity_id.split(".")[0] if "." in entity_id else ""
                if domain not in TOGGLEABLE_DOMAINS:
                    continue

                state = self.hass.states.get(entity_id)
                if state is None:
                    continue

                # Optimistic display: flip current state
                effective_state = state.state
                opt = OPTIMISTIC_TOGGLE.get(effective_state, effective_state)
                opt_bytes = self._to_screen_bytes(opt, VALUE_WIDTH)
                if self._value_cache_page == self._current_page:
                    self._value_cache[slot] = opt_bytes
                await self.hass.async_add_executor_job(
                    write_mem, host,
                    scratch_addr(self._current_page, slot),
                    opt_bytes,
                    password
                )

                await self._toggle_entity(domain, entity_id, state.state)

            except UltimateAPIError as exc:
                _LOGGER.debug("HomeTo64: action poll — offline: %s", exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("HomeTo64: action poll error — %s", exc)

    async def _toggle_entity(self, domain: str, entity_id: str, state: str) -> None:
        """Call the appropriate HA service to toggle an entity."""
        svc_domain = domain
        if domain in ("light", "switch", "input_boolean", "fan", "automation"):
            service = "turn_off" if state == "on" else "turn_on"
        elif domain == "lock":
            service = "unlock" if state == "locked" else "lock"
        elif domain == "cover":
            service = "close_cover" if state == "open" else "open_cover"
        elif domain == "scene":
            service = "turn_on"
        elif domain == "group":
            # Groups use homeassistant domain for turn_on/off
            svc_domain = "homeassistant"
            service = "turn_off" if state == "on" else "turn_on"
        else:
            return
        await self.hass.services.async_call(
            svc_domain, service, {"entity_id": entity_id}, blocking=False
        )
        _LOGGER.info("HomeTo64: %s.%s(%s)", svc_domain, service, entity_id)

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
        self._task_action = self.hass.loop.create_task(
            self._action_poll_loop(), name=f"hometo64_action_{self.entry.entry_id}"
        )
        self._task_cycle = self.hass.loop.create_task(
            self._auto_cycle_loop(), name=f"hometo64_cycle_{self.entry.entry_id}"
        )
        _LOGGER.info("HomeTo64: coordinator started for %s", self._cfg(CONF_HOST))

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if self._task_action:
            self._task_action.cancel()
        if hasattr(self, '_task_cycle') and self._task_cycle:
            self._task_cycle.cancel()
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

    # Screen RAM layout for direct value write
    _SCREEN_BASE     = 1024   # $0400
    _SENSOR_ROW      = 3      # first sensor row (row 0=blank,1=heading,2=blank,3+)
    _SENSOR_ROW_STEP = 2      # double-spaced rows
    _VALUE_COL       = 20     # value starts at col 20
    _VALUE_WIDTH     = 20     # 20 screen-code bytes per value

    def _screen_value_addr(self, sensor_index: int) -> int:
        """Screen RAM address of the value column for sensor N on the current page."""
        row = self._SENSOR_ROW + sensor_index * self._SENSOR_ROW_STEP
        return self._SCREEN_BASE + row * 40 + self._VALUE_COL

    def _build_screen_update(
        self, sensors: list[tuple[str, str]]
    ) -> list[tuple[int, bytes]]:
        """Build (address, data) pairs writing sensor values directly to screen RAM.

        Writes all sensors (up to 10) — all get value updates.
        Only slots 0-7 are cursor-selectable (bitmap byte limit).
        Slots 8-9 display values but cannot be toggled via cursor.
        """
        writes: list[tuple[int, bytes]] = []
        for i, (_, value) in enumerate(sensors[:SENSORS_PER_PAGE]):   # all 10
            addr = self._screen_value_addr(i)
            data = self._to_screen_bytes(value, self._VALUE_WIDTH)
            writes.append((addr, data))
        return writes

    async def _auto_cycle_loop(self) -> None:
        """Advance to next page after ac_secs seconds of C64 keyboard inactivity.

        Uses a timestamp to track last keyboard interaction (detected via readmem
        in the action poll loop). Fires the same logic as the Next Page button.
        """
        _LOGGER.debug("HomeTo64: auto-cycle loop started")
        if not hasattr(self, '_last_interaction'):
            self._last_interaction = time.monotonic()
        while True:
            await asyncio.sleep(1)
            if not self._prg_running or not self._prg_settled:
                self._last_interaction = time.monotonic()  # reset while inactive
                continue
            ac_secs = int(self._cfg(CONF_AUTO_CYCLE, DEFAULT_AUTO_CYCLE))
            if ac_secs <= 0:
                self._last_interaction = time.monotonic()  # reset while disabled
                continue
            idle = time.monotonic() - self._last_interaction
            _LOGGER.debug("HomeTo64: auto-cycle idle=%.1fs threshold=%ds", idle, ac_secs)
            if idle < ac_secs:
                continue
            # Inactivity threshold reached — advance page
            self._last_interaction = time.monotonic()
            pages_cfg = self._cfg(CONF_PAGES, None)
            n_pages   = len(pages_cfg) if pages_cfg else 1
            if n_pages <= 1:
                continue
            host     = self._cfg(CONF_HOST)
            password = self._cfg(CONF_PASSWORD, DEFAULT_PASSWORD)
            old_page  = self._current_page
            next_page = (old_page + 1) % n_pages
            self._current_page = next_page
            try:
                await self.hass.async_add_executor_job(
                    write_mem, host, SCRATCH_BITMASK_ADDR, bytes([0]), password
                )
                await self._clear_page_values(host, password, old_page)
                await self.hass.async_add_executor_job(
                    write_mem, host, NAV_ADDR, bytes([next_page]), password
                )
                await self._write_bitmask(host, password, next_page)
                _LOGGER.info(
                    "HomeTo64: auto-cycle → page %d of %d (idle %.1fs)",
                    next_page + 1, n_pages, idle
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("HomeTo64: auto-cycle write failed — %s", exc)

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
                if not self._prg_settled:
                    # Give the PRG time to fully draw before first write.
                    # Without this, HA writes to screen RAM while BASIC PRINT
                    # is still drawing sensor rows, causing garbled display.
                    _LOGGER.debug("HomeTo64: waiting for PRG to settle...")
                    await asyncio.sleep(6)
                    self._prg_settled = True
                    _LOGGER.info("HomeTo64: PRG settled — starting screen RAM updates")
                # Auto-stop check
                auto_stop = int(self._cfg(CONF_AUTO_STOP_MIN, DEFAULT_AUTO_STOP_MIN))
                if auto_stop > 0:
                    elapsed_min = (time.monotonic() - self._prg_started_at) / 60
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
                pages    = self._get_pages()
                # Read live page index from C64 on every tick to stay in sync
                try:
                    nav_raw = await self.hass.async_add_executor_job(
                        read_mem, host, NAV_ADDR, 1, password
                    )
                    if nav_raw and nav_raw[0] < len(pages) and nav_raw[0] != self._current_page:
                        self._current_page = nav_raw[0]
                        self._last_interaction = time.monotonic()
                        _LOGGER.debug("HomeTo64: refresh loop synced to page %d", self._current_page)
                except UltimateAPIError:
                    pass
                page_idx = self._current_page
                if page_idx < len(pages):
                    _, sensors = pages[page_idx]
                    writes = self._build_screen_update(sensors)
                    # Diff against last-written values: most ticks nothing
                    # changed, so most writemem calls were redundant traffic.
                    # Cache resets on page change; every 30 ticks a full
                    # rewrite runs as a corruption safety net.
                    self._refresh_tick += 1
                    if (page_idx != self._value_cache_page
                            or self._refresh_tick % 30 == 0):
                        self._value_cache.clear()
                        self._value_cache_page = page_idx
                    written = 0
                    for slot, (addr, data) in enumerate(writes):
                        if self._value_cache.get(slot) == data:
                            continue
                        await self.hass.async_add_executor_job(
                            write_mem, host, addr, data, password
                        )
                        self._value_cache[slot] = data
                        written += 1
                    # Keep bitmask current for active page
                    await self._write_bitmask(host, password, page_idx)
                    if written:
                        _LOGGER.info(
                            "HomeTo64: wrote %d changed values to screen RAM (page %d)",
                            written, page_idx,
                        )
            except UltimateAPIError as exc:
                self._invalidate_value_cache()   # full re-sync on reconnect
                _LOGGER.warning("HomeTo64: writemem failed (C64 offline?) — %s", exc)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("HomeTo64: unexpected error in refresh loop — %s", exc)
