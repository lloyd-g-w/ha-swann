"""Camera, binary sensor and switch tests."""

from __future__ import annotations

from homeassistant.components.camera import async_get_stream_source
from homeassistant.components.camera.const import DATA_COMPONENT as CAMERA_DATA_KEY
from homeassistant.components.stream import (
    CONF_RTSP_TRANSPORT as STREAM_CONF_RTSP_TRANSPORT,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.swann.api import ChannelEvents
from custom_components.swann.const import CONF_RTSP_TRANSPORT, CONF_STREAM_TYPE

from .conftest import FakeSwannClient


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _switch(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        "switch", service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


@pytest.mark.parametrize(("stream", "subtype"), [("main", 0), ("sub", 1)])
async def test_camera_stream_source(
    hass: HomeAssistant,
    fake_client: FakeSwannClient,
    config_entry: MockConfigEntry,
    stream: str,
    subtype: int,
):
    entry = MockConfigEntry(
        domain=config_entry.domain,
        unique_id=config_entry.unique_id,
        data=dict(config_entry.data),
        options={CONF_STREAM_TYPE: stream},
    )
    await _setup(hass, entry)
    assert hass.states.get("camera.garden") is not None
    src = await async_get_stream_source(hass, "camera.front_door")
    assert src == (
        "rtsp://admin:p%40ss%3Aw%2Frd@192.0.2.10:554"
        f"/rtsp/streaming?channel=01&subtype={subtype}"
    )


async def test_binary_sensors(
    hass: HomeAssistant, fake_client: FakeSwannClient, config_entry: MockConfigEntry
):
    await _setup(hass, config_entry)
    assert hass.states.get("binary_sensor.front_door_motion").state == STATE_ON
    assert hass.states.get("binary_sensor.front_door_person").state == STATE_ON
    assert hass.states.get("binary_sensor.front_door_vehicle").state == STATE_OFF
    assert hass.states.get("binary_sensor.garden_motion").state == STATE_OFF

    fake_client.events[2] = ChannelEvents(motion=True, vehicle=True)
    fake_client.events[1] = ChannelEvents()
    await config_entry.runtime_data.coordinator.async_poll_events()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.garden_motion").state == STATE_ON
    assert hass.states.get("binary_sensor.garden_vehicle").state == STATE_ON
    assert hass.states.get("binary_sensor.front_door_person").state == STATE_OFF


async def test_motion_detection_switch(
    hass: HomeAssistant, fake_client: FakeSwannClient, config_entry: MockConfigEntry
):
    await _setup(hass, config_entry)
    ent = "switch.garden_motion_detection"
    assert hass.states.get(ent).state == STATE_OFF

    await _switch(hass, "turn_on", ent)
    assert fake_client.channels[2].motion_detection_enabled is True
    assert hass.states.get(ent).state == STATE_ON

    await _switch(hass, "turn_off", ent)
    assert fake_client.channels[2].motion_detection_enabled is False
    assert hass.states.get(ent).state == STATE_OFF


@pytest.mark.parametrize(
    ("options", "transport"),
    [({}, "tcp"), ({CONF_RTSP_TRANSPORT: "udp"}, "udp")],
)
async def test_camera_rtsp_transport(
    hass: HomeAssistant,
    fake_client: FakeSwannClient,
    config_entry: MockConfigEntry,
    options: dict,
    transport: str,
):
    entry = MockConfigEntry(
        domain=config_entry.domain,
        unique_id=config_entry.unique_id,
        data=dict(config_entry.data),
        options=options,
    )
    await _setup(hass, entry)
    camera = hass.data[CAMERA_DATA_KEY].get_entity("camera.front_door")
    # HA's stream worker passes this to ffmpeg as rtsp_transport.
    assert camera.stream_options == {STREAM_CONF_RTSP_TRANSPORT: transport}
