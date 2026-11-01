"""Switches for Swann NVR: per-channel motion detection and the cloud switch."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.storage import Store

from .api import CloudSettings, SwannError
from .const import DOMAIN
from .coordinator import SwannConfigEntry, SwannCoordinator
from .entity import SwannChannelEntity, SwannEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

STORE_KEY_SAVED = "saved"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SwannConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    entities: list[SwitchEntity] = [
        SwannMotionDetectionSwitch(coordinator, ch) for ch in coordinator.data.channels
    ]
    entities.append(SwannCloudSwitch(coordinator, runtime.cloud_store))
    async_add_entities(entities)


class SwannMotionDetectionSwitch(SwannChannelEntity, SwitchEntity):
    """Enable/disable motion detection on a channel."""

    _attr_translation_key = "motion_detection"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SwannCoordinator, channel: int) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, channel, "motion_detection")

    @property
    def is_on(self) -> bool | None:
        """Return whether motion detection is enabled."""
        ch = self.channel
        return ch.motion_detection_enabled if ch else None

    async def _async_set(self, enabled: bool) -> None:
        try:
            await self.coordinator.client.async_set_motion_detection(
                self.channel_number, enabled
            )
        except SwannError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable motion detection."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable motion detection."""
        await self._async_set(False)


class SwannCloudSwitch(SwannEntity, SwitchEntity):
    """On = the NVR's cloud features are allowed.

    Off reads the current cloud settings, stores them, and applies all-off.
    On restores the stored settings. This only changes NVR settings; it cannot
    guarantee that the firmware makes no other outbound connections.
    """

    _attr_translation_key = "cloud"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SwannCoordinator, store: Store[dict]) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, "cloud")
        self._store = store

    @property
    def is_on(self) -> bool | None:
        """Return True if any cloud feature is enabled on the NVR."""
        cloud = self.coordinator.data.cloud
        return cloud.any_enabled if cloud is not None else None

    async def _async_saved(self) -> CloudSettings | None:
        data = await self._store.async_load()
        if not data or STORE_KEY_SAVED not in data:
            return None
        return CloudSettings.from_dict(data[STORE_KEY_SAVED])

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Save current cloud settings and disable all cloud features."""
        client = self.coordinator.client
        try:
            current = await client.async_get_cloud_settings()
            # Don't overwrite a previous snapshot with an already all-off state.
            if current.any_enabled or await self._async_saved() is None:
                await self._store.async_save({STORE_KEY_SAVED: current.as_dict()})
            await client.async_set_cloud_settings(current.all_off())
        except SwannError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Restore the cloud settings saved when the switch was turned off."""
        saved = await self._async_saved()
        if saved is None:
            cloud = self.coordinator.data.cloud
            if cloud is not None and cloud.any_enabled:
                return  # already on, nothing to restore
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_saved_cloud_settings"
            )
        try:
            await self.coordinator.client.async_set_cloud_settings(saved)
        except SwannError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self._store.async_remove()
        await self.coordinator.async_refresh()
