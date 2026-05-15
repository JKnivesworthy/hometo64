"""Config flow for HomeTo64 — multi-page sensor dashboard for Commodore 64.

Data structure
──────────────
  config_entry.data:
    host, password

  config_entry.options:
    scan_interval, auto_stop_minutes,
    num_pages,
    pages: [
      {"page_heading": "Home Assistant", "sensors": [{"entity_id":..., "display_name":...}]},
      {"page_heading": "Energy",         "sensors": [...]},
      ...
    ]

Backward compatibility
──────────────────────
  Old configs have a top-level "sensors" list and "heading" key instead of "pages".
  _get_pages() in __init__.py handles the migration transparently at runtime.

Options flow steps
──────────────────
  init       — scan_interval, auto_stop, num_pages
  page_N     — heading for page N  (repeated for each page)
  sensors_N  — entity picker for page N
  names_N    — display name overrides for page N
  (pages iterated via _page_index state variable)
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er, selector

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_HEADING,
    CONF_SCAN_INTERVAL,
    CONF_AUTO_STOP_MIN,
    CONF_SENSORS,
    CONF_ENTITY_ID,
    CONF_DISPLAY_NAME,
    CONF_PAGES,
    CONF_PAGE_HEADING,
    CONF_NUM_PAGES,
    DEFAULT_PASSWORD,
    DEFAULT_HEADING,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_AUTO_STOP_MIN,
    DEFAULT_SENSORS,
    DEFAULT_NUM_PAGES,
    MAX_PAGES,
)
from .ultimate_api import test_connection, UltimateAPIError

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _friendly_name(hass, entity_id: str) -> str:
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if entry and entry.name:
        return entry.name
    state = hass.states.get(entity_id)
    if state:
        fn = state.attributes.get("friendly_name")
        if fn:
            return fn
    return entity_id.split(".")[-1].replace("_", " ").title()


def _sensor_list_to_entity_ids(sensors: list[dict]) -> list[str]:
    return [s[CONF_ENTITY_ID] for s in sensors if CONF_ENTITY_ID in s]


def _build_sensor_list(hass, entity_ids: list[str], existing: list[dict] | None = None) -> list[dict]:
    existing_map = {
        s[CONF_ENTITY_ID]: s[CONF_DISPLAY_NAME]
        for s in (existing or [])
        if CONF_ENTITY_ID in s and CONF_DISPLAY_NAME in s
    }
    return [
        {
            CONF_ENTITY_ID:    eid,
            CONF_DISPLAY_NAME: existing_map.get(eid) or _friendly_name(hass, eid),
        }
        for eid in entity_ids
    ]


def _get_existing_pages(entry: config_entries.ConfigEntry) -> list[dict]:
    """Return existing pages list, migrating old single-page format if needed."""
    merged = {**entry.data, **entry.options}

    # New format: pages list
    if CONF_PAGES in merged and merged[CONF_PAGES]:
        return merged[CONF_PAGES]

    # Old format: top-level sensors + heading → migrate to single page
    if CONF_SENSORS in merged:
        return [{
            CONF_PAGE_HEADING: merged.get(CONF_HEADING, DEFAULT_HEADING),
            CONF_SENSORS:      merged[CONF_SENSORS],
        }]

    return []


# ---------------------------------------------------------------------------
# Schema builders
# ---------------------------------------------------------------------------

def _connection_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): selector.TextSelector(),
        vol.Optional(
            CONF_PASSWORD, default=d.get(CONF_PASSWORD, DEFAULT_PASSWORD)
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    })


def _global_settings_schema(defaults: dict | None = None) -> vol.Schema:
    """Scan interval, auto-stop, and number of pages."""
    d = defaults or {}
    return vol.Schema({
        vol.Optional(
            CONF_SCAN_INTERVAL,
            default=d.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=3600, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(
            CONF_AUTO_STOP_MIN,
            default=d.get(CONF_AUTO_STOP_MIN, DEFAULT_AUTO_STOP_MIN),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=1440, mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Optional(
            CONF_NUM_PAGES,
            default=d.get(CONF_NUM_PAGES, DEFAULT_NUM_PAGES),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=MAX_PAGES, mode=selector.NumberSelectorMode.BOX)
        ),
    })


def _page_heading_schema(page_index: int, default_heading: str) -> vol.Schema:
    return vol.Schema({
        vol.Optional(
            CONF_PAGE_HEADING,
            default=default_heading,
        ): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
    })


def _sensor_pick_schema(current_entity_ids: list[str]) -> vol.Schema:
    return vol.Schema({
        vol.Optional(
            CONF_SENSORS, default=current_entity_ids
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(multiple=True)
        ),
    })


def _names_schema(sensors: list[dict]) -> vol.Schema:
    fields: dict = {}
    for s in sensors:
        eid = s[CONF_ENTITY_ID]
        fields[vol.Optional(eid, default=s[CONF_DISPLAY_NAME])] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )
    return vol.Schema(fields)


# ---------------------------------------------------------------------------
# Initial config flow
# ---------------------------------------------------------------------------

class HomeTo64ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Setup: connect → global settings → page 1 heading → page 1 sensors → names → (repeat for more pages)."""

    VERSION = 1

    def __init__(self) -> None:
        self._host:          str  = ""
        self._password:      str  = ""
        self._global:        dict = {}
        self._num_pages:     int  = 1
        self._page_index:    int  = 0          # which page we're currently configuring
        self._pages:         list = []         # completed page configs
        self._pending_heading:  str       = DEFAULT_HEADING
        self._pending_sensors:  list[dict] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    test_connection,
                    user_input[CONF_HOST],
                    user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD),
                )
            except UltimateAPIError as exc:
                errors["base"] = "invalid_auth" if "403" in str(exc) else "cannot_connect"
            except Exception:
                _LOGGER.exception("HomeTo64 config flow error")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                self._host     = user_input[CONF_HOST]
                self._password = user_input.get(CONF_PASSWORD, DEFAULT_PASSWORD)
                return await self.async_step_global()

        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(user_input), errors=errors
        )

    async def async_step_global(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._global    = user_input
            self._num_pages = int(user_input.get(CONF_NUM_PAGES, 1))
            self._page_index = 0
            self._pages      = []
            return await self.async_step_page_heading()

        return self.async_show_form(
            step_id="global", data_schema=_global_settings_schema()
        )

    async def async_step_page_heading(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._pending_heading = user_input.get(CONF_PAGE_HEADING, DEFAULT_HEADING)
            return await self.async_step_page_sensors()

        default_heading = DEFAULT_HEADING if self._page_index == 0 else f"PAGE {self._page_index + 1}"
        return self.async_show_form(
            step_id="page_heading",
            data_schema=_page_heading_schema(self._page_index, default_heading),
            description_placeholders={"page_num": str(self._page_index + 1)},
        )

    async def async_step_page_sensors(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            entity_ids = user_input.get(CONF_SENSORS) or []
            self._pending_sensors = _build_sensor_list(self.hass, entity_ids)
            if self._pending_sensors:
                return await self.async_step_page_names()
            return await self._finish_page()

        return self.async_show_form(
            step_id="page_sensors",
            data_schema=_sensor_pick_schema([]),
            description_placeholders={"page_num": str(self._page_index + 1)},
        )

    async def async_step_page_names(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            for sensor in self._pending_sensors:
                eid = sensor[CONF_ENTITY_ID]
                if eid in user_input and user_input[eid].strip():
                    sensor[CONF_DISPLAY_NAME] = user_input[eid].strip()
            return await self._finish_page()

        return self.async_show_form(
            step_id="page_names",
            data_schema=_names_schema(self._pending_sensors),
            description_placeholders={"page_num": str(self._page_index + 1)},
        )

    async def _finish_page(self) -> FlowResult:
        self._pages.append({
            CONF_PAGE_HEADING: self._pending_heading,
            CONF_SENSORS:      self._pending_sensors,
        })
        self._page_index += 1
        self._pending_heading = DEFAULT_HEADING
        self._pending_sensors = []

        if self._page_index < self._num_pages:
            return await self.async_step_page_heading()

        return self._create_entry()

    def _create_entry(self) -> FlowResult:
        data = {
            CONF_HOST:     self._host,
            CONF_PASSWORD: self._password,
            **self._global,
            CONF_PAGES:    self._pages,
        }
        return self.async_create_entry(title=f"HomeTo64 ({self._host})", data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return HomeTo64OptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow (Configure button)
# ---------------------------------------------------------------------------

class HomeTo64OptionsFlow(config_entries.OptionsFlow):
    """Re-configure: global settings → per-page heading/sensors/names (repeated)."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry          = config_entry
        self._global:        dict = {}
        self._num_pages:     int  = 1
        self._page_index:    int  = 0
        self._pages:         list = []
        self._existing_pages: list = []
        self._pending_heading:  str       = DEFAULT_HEADING
        self._pending_sensors:  list[dict] = []

    def _current(self, key: str, default=None):
        return {**self._entry.data, **self._entry.options}.get(key, default)

    # Step 1 — global settings + page count
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._global         = user_input
            self._num_pages      = int(user_input.get(CONF_NUM_PAGES, 1))
            self._page_index     = 0
            self._pages          = []
            self._existing_pages = _get_existing_pages(self._entry)
            return await self.async_step_page_heading()

        current_num = len(_get_existing_pages(self._entry)) or DEFAULT_NUM_PAGES
        return self.async_show_form(
            step_id="init",
            data_schema=_global_settings_schema({
                CONF_SCAN_INTERVAL: self._current(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                CONF_AUTO_STOP_MIN: self._current(CONF_AUTO_STOP_MIN, DEFAULT_AUTO_STOP_MIN),
                CONF_NUM_PAGES:     current_num,
            }),
        )

    # Step 2a — heading for current page
    async def async_step_page_heading(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._pending_heading = user_input.get(CONF_PAGE_HEADING, DEFAULT_HEADING)
            return await self.async_step_page_sensors()

        # Pre-fill from existing config if available
        if self._page_index < len(self._existing_pages):
            existing_page  = self._existing_pages[self._page_index]
            default_heading = existing_page.get(CONF_PAGE_HEADING,
                              existing_page.get(CONF_HEADING, DEFAULT_HEADING))
        else:
            default_heading = DEFAULT_HEADING if self._page_index == 0 else f"PAGE {self._page_index + 1}"

        return self.async_show_form(
            step_id="page_heading",
            data_schema=_page_heading_schema(self._page_index, default_heading),
            description_placeholders={"page_num": str(self._page_index + 1)},
        )

    # Step 2b — sensor picker for current page
    async def async_step_page_sensors(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            entity_ids = user_input.get(CONF_SENSORS) or []
            existing_sensors = []
            if self._page_index < len(self._existing_pages):
                existing_sensors = self._existing_pages[self._page_index].get(CONF_SENSORS, [])
            self._pending_sensors = _build_sensor_list(
                self.hass, entity_ids, existing=existing_sensors
            )
            if self._pending_sensors:
                return await self.async_step_page_names()
            return await self._finish_page()

        current_ids: list[str] = []
        if self._page_index < len(self._existing_pages):
            current_ids = _sensor_list_to_entity_ids(
                self._existing_pages[self._page_index].get(CONF_SENSORS, [])
            )

        return self.async_show_form(
            step_id="page_sensors",
            data_schema=_sensor_pick_schema(current_ids),
            description_placeholders={"page_num": str(self._page_index + 1)},
        )

    # Step 2c — display name overrides for current page
    async def async_step_page_names(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            for sensor in self._pending_sensors:
                eid = sensor[CONF_ENTITY_ID]
                if eid in user_input and user_input[eid].strip():
                    sensor[CONF_DISPLAY_NAME] = user_input[eid].strip()
            return await self._finish_page()

        return self.async_show_form(
            step_id="page_names",
            data_schema=_names_schema(self._pending_sensors),
            description_placeholders={"page_num": str(self._page_index + 1)},
        )

    async def _finish_page(self) -> FlowResult:
        self._pages.append({
            CONF_PAGE_HEADING: self._pending_heading,
            CONF_SENSORS:      self._pending_sensors,
        })
        self._page_index     += 1
        self._pending_heading = DEFAULT_HEADING
        self._pending_sensors = []

        if self._page_index < self._num_pages:
            return await self.async_step_page_heading()

        return self._save()

    def _save(self) -> FlowResult:
        return self.async_create_entry(
            title="",
            data={
                **self._global,
                CONF_PAGES: self._pages,
            },
        )
