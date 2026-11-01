"""Binary sensors for Swann NVR.

Per channel: motion / person / vehicle events, whether a battery camera is
awake, and (read-only) floodlight and siren state where the camera has them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import STATUS_ONLINE, Channel, ChannelEvents
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SwannBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Swann channel binary sensor."""

    # (events, live connect_status) -> state
    value_fn: Callable[[ChannelEvents, str | None], bool | None]
    exists_fn: Callable[[Channel], bool] = lambda ch: True


SENSORS: tuple[SwannBinarySensorDescription, ...] = (
    SwannBinarySensorDescription(
        key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda ev, _: ev.motion,
    ),
    SwannBinarySensorDescription(
        key="person",
        translation_key="person",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        value_fn=lambda ev, _: ev.person,
    ),
    SwannBinarySensorDescription(
        key="vehicle",
        translation_key="vehicle",
        device_class=BinarySensorDeviceClass.MOTION,
        value_fn=lambda ev, _: ev.vehicle,
    ),
    SwannBinarySensorDescription(
        key="awake",
        translation_key="awake",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda _, status: (
            None if status is None else status.lower() == STATUS_ONLINE
        ),
    ),
    SwannBinarySensorDescription(
        key="floodlight",
        translation_key="floodlight",
        device_class=BinarySensorDeviceClass.LIGHT,
        value_fn=lambda ev, _: ev.floodlight,
        exists_fn=lambda ch: "Floodlight" in ch.abilities,
    ),
    SwannBinarySensorDescription(
        key="siren",
        translation_key="siren",
        device_class=BinarySensorDeviceClass.SOUND,
        value_fn=lambda ev, _: ev.siren,
        exists_fn=lambda ch: "AudioAlarm" in ch.abilities,
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
        SwannChannelBinarySensor(coordinator, ch.number, desc)
        for ch in coordinator.paired_channels
        for desc in SENSORS
        if desc.exists_fn(ch)
    )


class SwannChannelBinarySensor(SwannChannelEntity, BinarySensorEntity):
    """Binary state of a channel."""

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
        """Return the state."""
        return self.entity_description.value_fn(self.events, self.connect_status)
