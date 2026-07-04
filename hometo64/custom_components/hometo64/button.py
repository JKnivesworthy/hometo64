"""
button.py — Button entities for HomeTo64.

Two buttons on the integration device page:

  "Run on C64"   — POST /v1/runners:run_prg → C64 resets and runs the PRG now.
                   Use this once to launch the dashboard.

  "Download PRG" — Writes the PRG to <config>/www/hometo64/<filename> (served
                   at /local/hometo64/<filename>), then writes a shim HTML page
                   that auto-clicks an <a download> element.  The notification
                   shows the shim URL as plain text — user opens it in a new
                   browser tab (Ctrl/Cmd+click the URL, or paste it) and the
                   OS Save dialog fires immediately.
                   
                   Why not just open it automatically?  There is no reliable
                   way for a HA Python integration to open a URL in a new
                   browser tab on the client machine without extra frontend
                   JS (browser_mod, custom card, etc.).  The /local/ static
                   path is the simplest zero-dependency approach.
"""

from __future__ import annotations

import logging
import os

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url, NoURLAvailableError

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_FILENAME,
    CONF_PASSWORD,
    CONF_PAGES,
    DEFAULT_FILENAME,
    DEFAULT_PASSWORD,
    WWW_SUBDIR,
    SCRATCH_BITMASK_ADDR,
)
from .ultimate_api import run_prg, write_mem, UltimateAPIError
from .c64_prg import NAV_ADDR

_LOGGER = logging.getLogger(__name__)


def _build_shim(filename: str, prg_local_path: str) -> str:
    """Return an HTML page that immediately triggers a Save dialog for the PRG."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>HomeTo64 \u2014 {filename}</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:"Courier New",monospace;background:#0d0d1a;color:#c8c8ff;
         min-height:100vh;display:flex;align-items:center;justify-content:center}}
    .card{{background:#1a1a2e;border:1px solid #3a3a6e;border-radius:8px;
           padding:2.5rem 3rem;text-align:center;max-width:440px;width:90%}}
    .logo{{font-size:2.5rem;margin-bottom:1rem}}
    h1{{font-size:1.1rem;color:#a0a0ff;margin-bottom:.4rem}}
    .fname{{color:#7fefbd;font-size:.95rem;margin-bottom:1.5rem}}
    #status{{color:#888;font-size:.85rem;margin-bottom:1.5rem;min-height:1.2em}}
    a.btn{{display:inline-block;color:#7fefbd;border:1px solid #7fefbd66;
           background:#0d2b1e;padding:.55rem 1.4rem;border-radius:4px;
           text-decoration:none;font-size:.9rem}}
    a.btn:hover{{background:#1a4a36}}
    .note{{margin-top:1.2rem;font-size:.75rem;color:#555}}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">\U0001f4be</div>
    <h1>HomeTo64</h1>
    <div class="fname">{filename}</div>
    <div id="status">Starting download\u2026</div>
    <a class="btn" id="dl" href="{prg_local_path}" download="{filename}">\u2b07 Save {filename}</a>
    <div class="note">Close this tab when done.</div>
  </div>
  <script>
    (function(){{
      var a=document.getElementById('dl');
      var s=document.getElementById('status');
      try{{
        a.click();
        s.textContent='\u2713 Download started \u2014 check your Downloads folder.';
        s.style.color='#7fefbd';
      }}catch(e){{
        s.textContent='Click the button below to save the file.';
      }}
    }})();
  </script>
</body>
</html>"""


def _write_files(www_dir: str, filename: str, prg_data: bytes, prg_local: str) -> None:
    os.makedirs(www_dir, exist_ok=True)
    with open(os.path.join(www_dir, filename), "wb") as fh:
        fh.write(prg_data)
    with open(os.path.join(www_dir, "download.html"), "w", encoding="utf-8") as fh:
        fh.write(_build_shim(filename, prg_local))


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"HomeTo64 ({entry.data.get(CONF_HOST, 'c64')})",
        manufacturer="Commodore",
        model="C64 Ultimate",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([
        HomeTo64RunButton(hass, entry),
        HomeTo64StopButton(hass, entry),
        HomeTo64NextPageButton(hass, entry),
    ])


# ---------------------------------------------------------------------------
# Button 1 — Run on C64
# ---------------------------------------------------------------------------

class HomeTo64RunButton(ButtonEntity):
    """Resets the C64 and immediately runs the current PRG."""

    entity_description = ButtonEntityDescription(
        key="run_on_c64",
        name="Run on C64",
        icon="mdi:play",
    )
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_run_on_c64"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        cfg      = {**self._entry.data, **self._entry.options}
        host     = cfg.get(CONF_HOST)
        password = cfg.get(CONF_PASSWORD, DEFAULT_PASSWORD)
        filename = cfg.get(CONF_FILENAME, DEFAULT_FILENAME)
        prg_data = self.hass.data[DOMAIN][self._entry.entry_id].build_prg()

        try:
            await self.hass.async_add_executor_job(
                run_prg, host, prg_data, filename, password,
            )
            _LOGGER.info("HomeTo64: run_prg sent to %s", host)
            # Enable writemem refresh now that a PRG is actually running on the C64
            self.hass.data[DOMAIN][self._entry.entry_id].notify_prg_running()
        except UltimateAPIError as exc:
            _LOGGER.error("HomeTo64: run_prg failed — %s", exc)


# ---------------------------------------------------------------------------
# Button 2 — Download PRG
# ---------------------------------------------------------------------------

class HomeTo64DownloadButton(ButtonEntity):
    """Writes the PRG to www/ and shows a direct download URL in a notification."""

    entity_description = ButtonEntityDescription(
        key="download_prg",
        name="Download PRG",
        icon="mdi:download",
    )
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_download_prg"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        entry_id = self._entry.entry_id
        cfg      = {**self._entry.data, **self._entry.options}
        filename = cfg.get(CONF_FILENAME, DEFAULT_FILENAME)
        prg_data = self.hass.data[DOMAIN][entry_id].build_prg()

        www_dir    = os.path.join(self.hass.config.config_dir, "www", WWW_SUBDIR)
        prg_local  = f"/local/{WWW_SUBDIR}/{filename}"
        shim_local = f"/local/{WWW_SUBDIR}/download.html"

        await self.hass.async_add_executor_job(
            _write_files, www_dir, filename, prg_data, prg_local,
        )

        try:
            base_url = get_url(self.hass, prefer_external=False)
        except NoURLAvailableError:
            base_url = "http://homeassistant.local:8123"

        shim_url = f"{base_url}{shim_local}"
        prg_url  = f"{base_url}{prg_local}"

        # Show both URLs — the shim auto-triggers the Save dialog when opened
        # in a new tab; the direct PRG URL works with wget/curl on the C64 side.
        # Ctrl+click (Mac) or middle-click either URL to open in a new tab.
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "HomeTo64 \u2014 PRG Ready",
                "message": (
                    f"**{filename}** is ready. Open one of these in a **new browser tab**:\n\n"
                    f"Auto-download page (recommended):\n`{shim_url}`\n\n"
                    f"Direct PRG file:\n`{prg_url}`"
                ),
                "notification_id": f"hometo64_download_{entry_id}",
            },
        )
        _LOGGER.info("HomeTo64: download URLs ready — shim: %s  prg: %s", shim_url, prg_url)


# ---------------------------------------------------------------------------
# Button 3 — Stop RAM Writes
# ---------------------------------------------------------------------------

class HomeTo64StopButton(ButtonEntity):
    """Stops HA from writing sensor values into C64 RAM.

    Press this when you are done using the HomeTo64 dashboard on the C64.
    While the C64 is off or you are not using HomeTo64, stopping RAM writes
    prevents Home Assistant from continuously sending data to the C64 Ultimate
    over the network. You can resume updates at any time by pressing
    'Run on C64' again.
    """

    entity_description = ButtonEntityDescription(
        key="stop_ram_writes",
        name="Stop RAM Writes",
        icon="mdi:stop-circle-outline",
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_stop_ram_writes"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Stop writemem updates immediately."""
        self.hass.data[DOMAIN][self._entry.entry_id].notify_prg_stopped()
        _LOGGER.info("HomeTo64: RAM writes stopped by user via Stop button")


# ---------------------------------------------------------------------------
# Button — Next Page
# ---------------------------------------------------------------------------

class HomeTo64NextPageButton(ButtonEntity):
    """Advance the C64 dashboard to the next page.

    Writes (current_page + 1) mod n_pages to the NAV_ADDR memory location
    ($CFFF / 53247) via writemem DMA. The running BASIC program polls this
    address on every loop iteration and switches pages when it changes.

    This lets you navigate pages remotely from the HA UI, or automate page
    changes with HA automations (e.g. cycle pages on a schedule, show an
    energy page during peak hours, etc.).

    The button has no effect if the PRG is not currently running on the C64
    (i.e. before "Run on C64" has been pressed).
    """

    entity_description = ButtonEntityDescription(
        key="next_page",
        name="Next Page",
        icon="mdi:page-next-outline",
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id  = f"{entry.entry_id}_next_page"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        """Advance to the next page on the C64 dashboard."""
        coordinator = self.hass.data[DOMAIN][self._entry.entry_id]
        if not coordinator._prg_running:
            _LOGGER.info("HomeTo64: Next Page pressed but PRG not running — ignored")
            return

        host     = coordinator._cfg(CONF_HOST)
        password = coordinator._cfg(CONF_PASSWORD, DEFAULT_PASSWORD)

        # Work out how many pages are configured
        pages_cfg = coordinator._cfg(CONF_PAGES, None)
        n_pages   = len(pages_cfg) if pages_cfg else 1
        if n_pages <= 1:
            _LOGGER.debug("HomeTo64: Next Page — only one page configured")
            return

        # Read current NAV_ADDR value from coordinator's tracked state
        # We store it on the coordinator so we don't need to read back from C64
        current = getattr(coordinator, '_current_page', 0)
        next_page = (current + 1) % n_pages
        coordinator._current_page = next_page

        try:
            # Clear bitmap first so stale bits don't trigger cursor init
            # on the new page before the correct bitmap arrives
            await self.hass.async_add_executor_job(
                write_mem, host, SCRATCH_BITMASK_ADDR, bytes([0]), password
            )
            await coordinator._clear_page_values(host, password)
            await self.hass.async_add_executor_job(
                write_mem, host, NAV_ADDR, bytes([next_page]), password
            )
            # Now write correct bitmap for new page
            await coordinator._write_bitmask(host, password, next_page)
            _LOGGER.info(
                "HomeTo64: Next Page → page %d of %d", next_page + 1, n_pages
            )
        except UltimateAPIError as exc:
            _LOGGER.error("HomeTo64: Next Page write failed — %s", exc)
