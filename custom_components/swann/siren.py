"""Camera siren of Swann cameras (PreviewChannel/Floodlight2AudioAlarm)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.siren import (
    ATTR_VOLUME_LEVEL,
    SirenEntity,
    SirenEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SwannError
from .const import DOMAIN
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 1

# Real firmware reports audioAlarm_value_range {"min":1,"max":10}.
DEFAULT_RANGE = (1, 10)


def to_volume(value: int, rng: tuple[int, int]) -> float:
    """Map an NVR siren level (min..max) to HA volume 0..1."""
    low, high = rng
    if high <= low:
        return 1.0
    value = min(max(value, low), high)
    return round((value - low) / (high - low), 3)


def to_level(volume: float, rng: tuple[int, int]) -> int:
    """Map an HA volume (0..1) to the NVR siren level (min..max)."""
    low, high = rng
    volume = min(max(volume, 0.0), 1.0)
    return low + round(volume * (high - low))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sirens on cameras with the AudioAlarm ability."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SwannSiren(coordinator, ch.number)
        for ch in coordinator.paired_channels
        if "AudioAlarm" in ch.abilities
    )


class SwannSiren(SwannChannelEntity, SirenEntity):
    """Camera siren with level."""

    _attr_translation_key = "siren"
    _attr_supported_features = (
        SirenEntityFeature.TURN_ON
        | SirenEntityFeature.TURN_OFF
        | SirenEntityFeature.VOLUME_SET
    )

    def __init__(self, coordinator: SwannCoordinator, channel: int) -> None:
        """Initialize the siren."""
        # Not "siren": that was the 0.1.4 binary sensor's key, which the
        # stale-entity cleanup must remove.
        super().__init__(coordinator, channel, "alarm_siren")

    @property
    def _range(self) -> tuple[int, int]:
        return self.events.siren_range or DEFAULT_RANGE

    @property
    def is_on(self) -> bool | None:
        """Return whether the siren sounds."""
        return self.events.siren

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the current level as a volume (0..1)."""
        value = self.events.siren_value
        if value is None:
            return {}
        return {ATTR_VOLUME_LEVEL: to_volume(value, self._range)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Sound the siren."""
        level = None
        if kwargs.get(ATTR_VOLUME_LEVEL) is not None:
            level = to_level(kwargs[ATTR_VOLUME_LEVEL], self._range)
        await self._async_set(True, level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Silence the siren."""
        await self._async_set(False, None)

    async def _async_set(self, on: bool, level: int | None) -> None:
        try:
            await self.coordinator.client.async_set_siren(
                self.channel_number, on, level
            )
        except SwannError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_refresh_events()
