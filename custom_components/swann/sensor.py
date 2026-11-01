"""Sensor platform for Swann NVR: the NVR's cloud connection state."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    async_add_entities([SwannCloudConnectionSensor(entry.runtime_data.coordinator)])


class SwannCloudConnectionSensor(SwannEntity, SensorEntity):
    """SystemInfo/Base agent_cloud_link_state, e.g. "Connected".

    Lets users check that the Swann cloud is really cut off. Refreshed together
    with the cloud settings (every few minutes, and after the cloud switch).
    """

    _attr_translation_key = "cloud_connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SwannCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "cloud_connection")

    @property
    def native_value(self) -> str | None:
        """Return the cloud link state as reported by the NVR."""
        return self.coordinator.data.cloud_link
