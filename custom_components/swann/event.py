"""Alert event entities for Swann NVR.

One "Alert" event entity per paired channel. It fires motion / person /
vehicle on the rising edge of the matching Event/Check state, at most once per
type per ``ALERT_COOLDOWN`` (see the coordinator). The same alert is also sent
on the bus as ``swann_alert``.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import (
    ALERT_KEY,
    ALERT_TYPES,
    SwannConfigEntry,
    SwannCoordinator,
    alert_signal,
)
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up alert event entities."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SwannAlertEvent(coordinator, ch.number) for ch in coordinator.paired_channels
    )


class SwannAlertEvent(SwannChannelEntity, EventEntity):
    """Motion / person / vehicle alerts of a channel."""

    _attr_translation_key = "alert"
    _attr_device_class = EventDeviceClass.MOTION

    def __init__(self, coordinator: SwannCoordinator, channel: int) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, channel, ALERT_KEY)
        self._attr_event_types = list(ALERT_TYPES)

    async def async_added_to_hass(self) -> None:
        """Listen for alerts of this entry."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                alert_signal(self.coordinator.config_entry.entry_id),
                self._async_handle_alert,
            )
        )

    @callback
    def _async_handle_alert(self, channel: int, kind: str) -> None:
        if channel != self.channel_number:
            return
        self._trigger_event(kind, {"channel": channel})
        self.async_write_ha_state()
