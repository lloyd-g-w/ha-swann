"""Binary sensors for Swann NVR: per-channel motion / person / vehicle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ChannelEvents
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SwannBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Swann event binary sensor."""

    value_fn: Callable[[ChannelEvents], bool]


SENSORS: tuple[SwannBinarySensorDescription, ...] = (
    SwannBinarySensorDescription(
        key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda ev: ev.motion,
    ),
    SwannBinarySensorDescription(
        key="person",
        translation_key="person",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        value_fn=lambda ev: ev.person,
    ),
    SwannBinarySensorDescription(
        key="vehicle",
        translation_key="vehicle",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda ev: ev.vehicle,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SwannEventBinarySensor(coordinator, ch, desc)
        for ch in coordinator.data.channels
        for desc in SENSORS
    )


class SwannEventBinarySensor(SwannChannelEntity, BinarySensorEntity):
    """Event state of a channel."""

    entity_description: SwannBinarySensorDescription

    def __init__(
        self,
        coordinator: SwannCoordinator,
        channel: int,
        description: SwannBinarySensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, channel, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return the event state."""
        events = self.coordinator.data.events.get(self.channel_number)
        if events is None:
            return None
        return self.entity_description.value_fn(events)
