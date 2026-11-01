"""Select platform for Swann NVR: motion detection mode per channel.

Firmware such as the NVW-MR4K's has no boolean motion ``switch``; instead
AlarmConfig/Motion has ``multiple_switch`` (e.g. "PIR_and_Person_or_Vehicle").
The allowed values come from AlarmConfig/Motion/Range. If the NVR lists none,
the select only shows the current value and refuses changes: no "off" value
is guessed.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import SwannError
from .const import DOMAIN
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up motion mode selects."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        SwannMotionModeSelect(coordinator, ch.number)
        for ch in coordinator.paired_channels
        if ch.motion_mode is not None
    )


class SwannMotionModeSelect(SwannChannelEntity, SelectEntity):
    """Motion detection mode (``multiple_switch``) of a channel."""

    _attr_translation_key = "motion_mode"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SwannCoordinator, channel: int) -> None:
        """Initialize the select."""
        super().__init__(coordinator, channel, "motion_mode")

    @property
    def _allowed(self) -> tuple[str, ...]:
        modes = self.coordinator.data.motion_modes
        return modes.get(self.channel_number) or modes.get(0) or ()

    @property
    def options(self) -> list[str]:
        """Allowed modes (plus the current one if the NVR did not list it)."""
        options = list(self._allowed)
        current = self.current_option
        if current is not None and current not in options:
            options.append(current)
        return options

    @property
    def current_option(self) -> str | None:
        """Return the current mode."""
        ch = self.channel
        return ch.motion_mode if ch else None

    async def async_select_option(self, option: str) -> None:
        """Change the mode (read-modify-write of AlarmConfig/Motion)."""
        if option not in self._allowed:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="motion_mode_read_only",
                translation_placeholders={"option": option},
            )
        try:
            await self.coordinator.client.async_set_motion_mode(
                self.channel_number, option
            )
        except SwannError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_refresh()
