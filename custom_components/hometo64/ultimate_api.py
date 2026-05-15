"""
ultimate_api.py — REST API client for the Commodore 64 Ultimate (firmware 3.11+).

All blocking I/O must be called via hass.async_add_executor_job().
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .const import (
    ULTIMATE_API_BASE,
    ULTIMATE_HEADER_PASSWORD,
    API_INFO,
    API_RUN_PRG,
    API_LOAD_PRG,
)

_LOGGER = logging.getLogger(__name__)


class UltimateAPIError(Exception):
    """Raised when an Ultimate REST API call fails."""


def _base_url(host: str) -> str:
    return ULTIMATE_API_BASE.format(host=host)


def _post_prg(
    url: str,
    prg_data: bytes,
    filename: str,
    password: str,
    timeout: int,
) -> None:
    """POST a PRG binary to a runner endpoint."""
    headers: dict[str, str] = {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(prg_data)),
    }
    if password:
        headers[ULTIMATE_HEADER_PASSWORD] = password

    req = urllib.request.Request(url, data=prg_data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UltimateAPIError(
                "Access denied (HTTP 403) — check the Network Password on your Ultimate."
            ) from exc
        raise UltimateAPIError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise UltimateAPIError(f"Cannot reach Ultimate at {url}: {exc.reason}") from exc
    except OSError as exc:
        raise UltimateAPIError(f"Network error: {exc}") from exc

    try:
        result = json.loads(body)
        errors = result.get("errors", [])
        if errors:
            raise UltimateAPIError(f"Ultimate error(s): {'; '.join(errors)}")
    except (json.JSONDecodeError, AttributeError):
        pass   # empty body on success is fine


def test_connection(host: str, password: str = "", timeout: int = 10) -> dict[str, Any]:
    """GET /v1/info — verify the host is reachable and the password is correct."""
    url = _base_url(host) + API_INFO
    headers: dict[str, str] = {}
    if password:
        headers[ULTIMATE_HEADER_PASSWORD] = password

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise UltimateAPIError("Access denied (HTTP 403) — wrong Network Password.") from exc
        raise UltimateAPIError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise UltimateAPIError(f"Cannot reach Ultimate: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"errors": []}


def load_prg(
    host: str,
    prg_data: bytes,
    filename: str = "HOMETO64.PRG",
    password: str = "",
    timeout: int = 15,
) -> None:
    """POST /v1/runners:load_prg — reset C64 and load PRG via DMA. Does NOT run it.

    Use this for periodic background updates so the latest program is in
    memory without interrupting whatever the user is doing on the C64.
    The user (or a separate run_prg call) starts execution manually.
    """
    url = _base_url(host) + API_LOAD_PRG
    _LOGGER.debug("HomeTo64: load_prg → %s (%d bytes)", url, len(prg_data))
    _post_prg(url, prg_data, filename, password, timeout)
    _LOGGER.info("HomeTo64: PRG loaded into C64 memory via %s", host)


def run_prg(
    host: str,
    prg_data: bytes,
    filename: str = "HOMETO64.PRG",
    password: str = "",
    timeout: int = 15,
) -> None:
    """POST /v1/runners:run_prg — reset C64, load PRG via DMA, and RUN it immediately.

    Only call this when the user explicitly wants to start the program.
    Calling it on a timer would reset and relaunch the C64 every interval.
    """
    url = _base_url(host) + API_RUN_PRG
    _LOGGER.debug("HomeTo64: run_prg → %s (%d bytes)", url, len(prg_data))
    _post_prg(url, prg_data, filename, password, timeout)
    _LOGGER.info("HomeTo64: PRG sent and running on C64 via %s", host)


def write_mem(
    host: str,
    address: int,
    data: bytes,
    password: str = "",
    timeout: int = 15,
) -> None:
    """Write bytes directly into C64 RAM via DMA (PUT /v1/machine:writemem).

    Automatically chunks into 128-byte blocks if data exceeds the firmware limit.
    Uses urllib (same as the rest of this module) — no external dependencies.
    """
    chunk_size = 128
    offset = 0
    while offset < len(data):
        chunk = data[offset:offset + chunk_size]
        chunk_addr = address + offset
        hex_data = chunk.hex().upper()
        url = _base_url(host) + f"/machine:writemem?address={chunk_addr:04X}&data={hex_data}"
        req = urllib.request.Request(url, method="PUT")
        if password:
            req.add_header("X-Password", password)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            raise UltimateAPIError(
                f"writemem failed at {chunk_addr:#06x}: HTTP {exc.code} — {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise UltimateAPIError(
                f"writemem connection failed ({host}): {exc.reason}"
            ) from exc
        if status not in (200, 204):
            raise UltimateAPIError(
                f"writemem failed at {chunk_addr:#06x}: HTTP {status}"
            )
        offset += chunk_size
