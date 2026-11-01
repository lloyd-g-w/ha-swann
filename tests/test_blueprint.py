"""The notification blueprint (blueprints/automation/swann/notify_alert.yaml)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import shutil

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

BLUEPRINT = (
    Path(__file__).resolve().parent.parent
    / "blueprints"
    / "automation"
    / "swann"
    / "notify_alert.yaml"
)
ALERT = "event.syd_driveway_alert"
CAMERA = "camera.syd_driveway"
WEBHOOK_ID = "pixel9webhook"


def _mock_notify(hass: HomeAssistant):
    """Replace the phone's notify service (registered by mobile_app)."""
    return async_mock_service(hass, "notify", "mobile_app_pixel_9")


@pytest.fixture
async def setup_blueprint(hass: HomeAssistant, tmp_path: Path, hass_admin_user):
    """Install the blueprint in a temp config dir and return a setup helper."""
    hass.config.config_dir = str(tmp_path)
    target = tmp_path / "blueprints" / "automation" / "swann"
    target.mkdir(parents=True)
    shutil.copy(BLUEPRINT, target / "notify_alert.yaml")

    # A push-capable companion app registration, as HA's mobile_app tests make
    # it: the integration creates the device and the notify target from it.
    phone_entry = MockConfigEntry(
        domain="mobile_app",
        data={
            "webhook_id": WEBHOOK_ID,
            "device_id": "pixel-9-device",
            "device_name": "Pixel 9",
            "manufacturer": "Google",
            "model": "Pixel 9",
            "os_name": "Android",
            "os_version": "16",
            "app_id": "io.homeassistant.companion.android",
            "app_name": "Home Assistant",
            "app_version": "2026.10",
            "app_data": {
                "push_token": "PUSH_TOKEN",
                "push_url": "https://mobile-push.home-assistant.dev/push",
            },
            "supports_encryption": False,
            "secret": None,
            "user_id": hass_admin_user.id,
        },
    )
    phone_entry.add_to_hass(hass)
    assert await async_setup_component(hass, "mobile_app", {})
    await hass.async_block_till_done()
    phone = dr.async_get(hass).async_get_device_by_identifier(
        ("mobile_app", "pixel-9-device"), phone_entry.entry_id
    )
    assert phone is not None
    hass.states.async_set(CAMERA, "idle", {"friendly_name": "Syd Driveway"})
    hass.states.async_set(ALERT, "unknown", {"event_type": None})
    await hass.async_block_till_done()

    async def setup(**inputs) -> None:
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "use_blueprint": {
                        "path": "swann/notify_alert.yaml",
                        "input": {
                            "alert_entities": [ALERT],
                            "camera": CAMERA,
                            "notify_device": phone.id,
                            **inputs,
                        },
                    }
                }
            },
        )
        await hass.async_block_till_done()

    setup.phone = phone
    return setup


def _alert(hass: HomeAssistant, when: str, kind: str) -> None:
    hass.states.async_set(ALERT, when, {"event_type": kind})


async def test_notifies_person_with_snapshot(
    hass: HomeAssistant, setup_blueprint
) -> None:
    calls = _mock_notify(hass)
    await setup_blueprint(cooldown={"seconds": 0})
    _alert(hass, "2026-11-01T10:00:00+00:00", "person")
    await hass.async_block_till_done()
    assert len(calls) == 1
    data = calls[0].data
    assert data["target"] == WEBHOOK_ID
    assert data["title"] == "Person detected"
    assert data["message"] == "Someone is at the Syd Driveway"
    assert data["data"]["image"] == f"/api/camera_proxy/{CAMERA}"
    assert data["data"]["clickAction"] == f"entityId:{CAMERA}"
    assert data["data"]["url"] == f"entityId:{CAMERA}"
    assert data["data"]["push"]["interruption-level"] == "active"


async def test_delivered_after_phone_renamed(
    hass: HomeAssistant, setup_blueprint
) -> None:
    """Renaming the phone in HA must not break delivery (no slug guessing)."""
    calls = _mock_notify(hass)
    registry = dr.async_get(hass)
    registry.async_update_device(setup_blueprint.phone.id, name_by_user="Lloyd's phone")
    await setup_blueprint(cooldown={"seconds": 0})
    _alert(hass, "2026-11-01T10:00:00+00:00", "vehicle")
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data["target"] == WEBHOOK_ID
    assert calls[0].data["title"] == "Vehicle detected"


async def test_motion_filtered_by_default(hass: HomeAssistant, setup_blueprint) -> None:
    calls = _mock_notify(hass)
    await setup_blueprint(cooldown={"seconds": 0})
    _alert(hass, "2026-11-01T10:00:00+00:00", "motion")
    await hass.async_block_till_done()
    assert calls == []


async def test_doorbell_is_urgent_and_chimes(
    hass: HomeAssistant, setup_blueprint
) -> None:
    calls = _mock_notify(hass)
    plays = async_mock_service(hass, "media_player", "play_media")
    await setup_blueprint(
        cooldown={"seconds": 0},
        media_player=["media_player.kitchen"],
        chime_url="media-source://media_source/local/chime.mp3",
    )
    _alert(hass, "2026-11-01T10:00:00+00:00", "doorbell")
    await hass.async_block_till_done()
    data = calls[0].data["data"]
    assert data["push"]["interruption-level"] == "time-sensitive"
    assert data["priority"] == "high"
    assert data["ttl"] == 0
    assert plays[0].data["media_content_id"].endswith("chime.mp3")


async def test_cooldown(
    hass: HomeAssistant, setup_blueprint, freezer: FrozenDateTimeFactory
) -> None:
    calls = _mock_notify(hass)
    await setup_blueprint()  # default 2 minutes
    _alert(hass, "2026-11-01T10:00:00+00:00", "person")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=60))
    _alert(hass, "2026-11-01T10:01:00+00:00", "vehicle")
    await hass.async_block_till_done()
    assert len(calls) == 1
    freezer.tick(timedelta(seconds=61))
    _alert(hass, "2026-11-01T10:02:01+00:00", "vehicle")
    await hass.async_block_till_done()
    assert len(calls) == 2


async def test_restore_after_restart_ignored(
    hass: HomeAssistant, setup_blueprint
) -> None:
    calls = _mock_notify(hass)
    await setup_blueprint(cooldown={"seconds": 0})
    hass.states.async_set(ALERT, "unavailable")
    await hass.async_block_till_done()
    _alert(hass, "2026-11-01T10:00:00+00:00", "person")
    await hass.async_block_till_done()
    assert calls == []


async def test_person_speaks_message(hass: HomeAssistant, setup_blueprint) -> None:
    _mock_notify(hass)
    speaks = async_mock_service(hass, "tts", "speak")
    await setup_blueprint(
        cooldown={"seconds": 0},
        media_player=["media_player.kitchen"],
        tts_entity="tts.google_en_com",
    )
    _alert(hass, "2026-11-01T10:00:00+00:00", "person")
    await hass.async_block_till_done()
    assert speaks[0].data["message"] == "Person at the Syd Driveway"
    assert speaks[0].data["media_player_entity_id"] == ["media_player.kitchen"]
