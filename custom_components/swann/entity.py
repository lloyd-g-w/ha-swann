"""Base entities for Swann NVR."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Channel, ChannelEvents, status_available
from .const import DOMAIN
from .coordinator import SwannCoordinator


class SwannEntity(CoordinatorEntity[SwannCoordinator]):
    """Entity attached to the NVR device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SwannCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        dev = coordinator.device
        self._attr_unique_id = f"{dev.serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, dev.serial)},
            manufacturer="Swann",
            model=dev.model,
            name=dev.name,
            serial_number=dev.serial,
            sw_version=dev.firmware_version,
            hw_version=dev.hardware_version,
        )


def channel_identifier(serial: str, channel: int) -> str:
    """Return the device identifier of a channel."""
    return f"{serial}_ch{channel:02d}"


class SwannChannelEntity(CoordinatorEntity[SwannCoordinator]):
    """Entity attached to a camera channel device (via the NVR)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SwannCoordinator, channel: int, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.channel_number = channel
        serial = coordinator.device.serial
        self._attr_unique_id = f"{serial}_ch{channel:02d}_{key}"
        # The channel device itself (with its link to the NVR via
        # via_device_id) is registered in async_setup_entry; entities only
        # reference it, since via_device in DeviceInfo is deprecated.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, channel_identifier(serial, channel))}
        )

    @property
    def channel(self) -> Channel | None:
        """Return the latest channel data."""
        return self.coordinator.data.channels.get(self.channel_number)

    @property
    def events(self) -> ChannelEvents:
        """Return the latest event state of the channel."""
        return self.coordinator.data.events.get(self.channel_number, ChannelEvents())

    @property
    def connect_status(self) -> str | None:
        """Live connect_status (Event/Check), else the polled one."""
        status = self.events.connect_status
        if status:
            return status
        ch = self.channel
        return ch.connect_status if ch is not None else None

    @property
    def available(self) -> bool:
        """Available when the NVR is reachable and the camera online or asleep."""
        status = self.connect_status
        return (
            super().available
            and self.channel is not None
            and status is not None
            and status_available(status)
        )
