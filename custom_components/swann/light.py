"""Floodlight of Swann cameras (PreviewChannel/Floodlight2AudioAlarm)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SwannError
from .const import DOMAIN
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 1

# Real firmware reports floodlight_value_range {"min":1,"max":100}.
DEFAULT_RANGE = (1, 100)


def to_ha_brightness(value: int, rng: tuple[int, int]) -> int:
    """Map an NVR brightness (min..max) to HA 1..255."""
    low, high = rng
    if high <= low:
        return 255
    value = min(max(value, low), high)
    return max(1, round((value - low) * 254 / (high - low)) + 1)


def to_nvr_brightness(brightness: int, rng: tuple[int, int]) -> int:
    """Map an HA brightness (1..255) to the NVR range (min..max)."""
    low, high = rng
    brightness = min(max(brightness, 1), 255)
    return low + round((brightness - 1) * (high - low) / 254)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up floodlights on cameras with the Floodlight ability."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SwannFloodlight(coordinator, ch.number)
        for ch in coordinator.paired_channels
        if "Floodlight" in ch.abilities
    )


class SwannFloodlight(SwannChannelEntity, LightEntity):
    """Camera floodlight with brightness."""

    _attr_translation_key = "floodlight"
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, coordinator: SwannCoordinator, channel: int) -> None:
        """Initialize the light."""
        # Not "floodlight": that was the 0.1.4 binary sensor's key, which the
        # stale-entity cleanup must remove.
        super().__init__(coordinator, channel, "light")
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    @property
    def _range(self) -> tuple[int, int]:
        return self.events.floodlight_range or DEFAULT_RANGE

    @property
    def is_on(self) -> bool | None:
        """Return whether the floodlight is on."""
        return self.events.floodlight

    @property
    def brightness(self) -> int | None:
        """Return the brightness (1..255)."""
        value = self.events.floodlight_value
        return None if value is None else to_ha_brightness(value, self._range)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the floodlight on."""
        level = None
        if ATTR_BRIGHTNESS in kwargs:
            level = to_nvr_brightness(kwargs[ATTR_BRIGHTNESS], self._range)
        await self._async_set(True, level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the floodlight off."""
        await self._async_set(False, None)

    async def _async_set(self, on: bool, level: int | None) -> None:
        try:
            await self.coordinator.client.async_set_floodlight(
                self.channel_number, on, level
            )
        except SwannError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_refresh_events()
