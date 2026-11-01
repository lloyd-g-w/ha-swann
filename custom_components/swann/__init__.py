"""The Swann NVR integration."""

from __future__ import annotations

import logging
import re

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from . import helpers
from .api import status_available
from .const import (
    CLOUD_STORE_VERSION,
    CONF_MANAGE_SERVERS,
    DEFAULT_MANAGE_SERVERS,
    DOMAIN,
)
from .coordinator import SwannConfigEntry, SwannCoordinator, SwannRuntimeData
from .entity import channel_identifier

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SIREN,
    Platform.SWITCH,
]


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
    # Channel devices for paired cameras, linked to the NVR (entities only
    # carry identifiers). Empty slots ("NotPaired") get no device.
    wanted = {nvr_device.id}
    for ch in coordinator.paired_channels:
        device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, channel_identifier(dev.serial, ch.number))},
            manufacturer="Swann",
            name=ch.name or f"Channel {ch.number}",
            via_device_id=nvr_device.id,
        )
        wanted.add(device.id)
    # Remove devices of channels that are no longer paired.
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if device.id not in wanted:
            registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )
    entry.runtime_data = SwannRuntimeData(
        client=client,
        coordinator=coordinator,
        cloud_store=cloud_store(hass, entry.entry_id),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_stale_entities(hass, entry, coordinator)
    coordinator.async_start_events()
    return True


_CHANNEL_UID_RE = re.compile(r"_ch(\d{2})_")


@callback
def _async_remove_stale_entities(
    hass: HomeAssistant, entry: SwannConfigEntry, coordinator: SwannCoordinator
) -> None:
    """Remove registry entries this setup no longer provides.

    Runs only after a successful first refresh (setup would have failed
    otherwise), so a temporarily unreachable NVR wipes nothing. Entities of a
    channel that is merely offline (or in an unknown state) are kept, since its
    capabilities may be incomplete; channels Online or asleep report them in
    full, and empty slots ("NotPaired") lose everything.
    """
    registry = er.async_get(hass)
    created = coordinator.created_unique_ids
    serial = coordinator.device.serial
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = reg_entry.unique_id
        if unique_id in created or not unique_id.startswith(f"{serial}_"):
            continue
        if match := _CHANNEL_UID_RE.match(unique_id, len(serial)):
            channel = coordinator.data.channels.get(int(match.group(1)))
            if (
                channel is not None
                and channel.paired
                and not status_available(channel.connect_status)
            ):
                continue  # offline: keep until it reports its capabilities
        _LOGGER.debug("Removing stale entity %s", reg_entry.entity_id)
        registry.async_remove(reg_entry.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.client.async_close()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: SwannConfigEntry) -> None:
    """Delete saved cloud settings when the entry is removed."""
    await cloud_store(hass, entry.entry_id).async_remove()
