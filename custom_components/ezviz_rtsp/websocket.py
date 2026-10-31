"""Websocket commands the shipped Lovelace card uses.

The card can be configured with a ``serial`` instead of an entity id, so it
needs a way to map a camera serial to the camera entity of the loaded account.
"""

from __future__ import annotations

from typing import Any

import probatio as vol
from homeassistant.components.websocket_api import async_register_command
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import async_response, websocket_command
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_CODES, DATA_WS, DOMAIN


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the card's websocket command once."""
    if hass.data.get(DATA_WS):
        return
    hass.data[DATA_WS] = True
    async_register_command(hass, ws_get_devices)


def _loaded_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


@websocket_command({vol.Required("type"): f"{DOMAIN}/devices"})
@async_response
async def ws_get_devices(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """List the cloud cameras of every loaded account, for the card and its editor."""
    registry = er.async_get(hass)
    devices: list[dict[str, Any]] = []
    for entry in _loaded_entries(hass):
        codes = entry.options.get(CONF_CODES, {})
        data = getattr(getattr(entry, "runtime_data", None), "data", None) or {}
        for serial, value in data.items():
            devices.append(
                {
                    "entry_id": entry.entry_id,
                    "serial": serial,
                    "name": value.get("name") or serial,
                    "encrypted": bool(value.get("encrypted")),
                    "has_code": serial in codes,
                    "battery": value.get("battery_level") is not None,
                    "entity_id": registry.async_get_entity_id("camera", DOMAIN, serial),
                }
            )
    connection.send_result(msg["id"], devices)
