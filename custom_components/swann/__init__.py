"""The Swann NVR integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from . import helpers
from .const import (
    CLOUD_STORE_VERSION,
    CONF_MANAGE_SERVERS,
    DEFAULT_MANAGE_SERVERS,
    DOMAIN,
)
from .coordinator import SwannConfigEntry, SwannCoordinator, SwannRuntimeData
from .entity import channel_identifier

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CAMERA, Platform.SWITCH]


def cloud_store(hass: HomeAssistant, entry_id: str) -> Store[dict]:
    """Return the store holding the NVR cloud settings saved by the cloud switch."""
    return Store(hass, CLOUD_STORE_VERSION, f"{DOMAIN}.{entry_id}.cloud")


async def async_setup_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> bool:
    """Set up Swann NVR from a config entry."""
    client = helpers.create_client(hass, entry.data)
    client.manage_servers = entry.options.get(
        CONF_MANAGE_SERVERS, DEFAULT_MANAGE_SERVERS
    )
    coordinator = SwannCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.async_close()
        raise
    # Register the NVR first so channel devices can always link to it.
    dev = coordinator.device
    registry = dr.async_get(hass)
    nvr_device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, dev.serial)},
        manufacturer="Swann",
        model=dev.model,
        name=dev.name,
        serial_number=dev.serial,
        sw_version=dev.firmware_version,
        hw_version=dev.hardware_version,
    )
    # Channel devices, linked to the NVR (entities only carry identifiers).
    for number, ch in coordinator.data.channels.items():
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, channel_identifier(dev.serial, number))},
            manufacturer="Swann",
            name=ch.name or f"Channel {number}",
            via_device_id=nvr_device.id,
        )
    entry.runtime_data = SwannRuntimeData(
        client=client,
        coordinator=coordinator,
        cloud_store=cloud_store(hass, entry.entry_id),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_events()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.async_close()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> None:
    """Delete saved cloud settings when the entry is removed."""
    await cloud_store(hass, entry.entry_id).async_remove()
