"""The Swann NVR integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import helpers
from .const import CLOUD_STORE_VERSION, DOMAIN
from .coordinator import SwannConfigEntry, SwannCoordinator, SwannRuntimeData

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.SWITCH]


def cloud_store(hass: HomeAssistant, entry_id: str) -> Store[dict]:
    """Return the store holding the NVR cloud settings saved by the cloud switch."""
    return Store(hass, CLOUD_STORE_VERSION, f"{DOMAIN}.{entry_id}.cloud")


async def async_setup_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> bool:
    """Set up Swann NVR from a config entry."""
    client = helpers.create_client(hass, entry.data)
    coordinator = SwannCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.async_close()
        raise
    entry.runtime_data = SwannRuntimeData(
        client=client,
        coordinator=coordinator,
        cloud_store=cloud_store(hass, entry.entry_id),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.async_close()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> None:
    """Delete saved cloud settings when the entry is removed."""
    await cloud_store(hass, entry.entry_id).async_remove()
