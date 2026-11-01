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


@pytest.fixture
async def setup_blueprint(hass: HomeAssistant, tmp_path: Path):
    """Install the blueprint in a temp config dir and return a setup helper."""
    hass.config.config_dir = str(tmp_path)
    target = tmp_path / "blueprints" / "automation" / "swann"
    target.mkdir(parents=True)
    shutil.copy(BLUEPRINT, target / "notify_alert.yaml")

    phone_entry = MockConfigEntry(domain="mobile_app")
    phone_entry.add_to_hass(hass)
    phone = dr.async_get(hass).async_get_or_create(
        config_entry_id=phone_entry.entry_id,
        identifiers={("mobile_app", "pixel")},
        name="Pixel 9",
    )
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

    return setup


def _alert(hass: HomeAssistant, when: str, kind: str) -> None:
    hass.states.async_set(ALERT, when, {"event_type": kind})


async def test_notifies_person_with_snapshot(
    hass: HomeAssistant, setup_blueprint
) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_pixel_9")
    await setup_blueprint(cooldown={"seconds": 0})
    _alert(hass, "2026-11-01T10:00:00+00:00", "person")
    await hass.async_block_till_done()
    assert len(calls) == 1
    data = calls[0].data
    assert data["title"] == "Person detected"
    assert data["message"] == "Someone is at the Syd Driveway"
    assert data["data"]["image"] == f"/api/camera_proxy/{CAMERA}"
    assert data["data"]["clickAction"] == f"entityId:{CAMERA}"
    assert data["data"]["push"]["interruption-level"] == "active"


async def test_motion_filtered_by_default(hass: HomeAssistant, setup_blueprint) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_pixel_9")
    await setup_blueprint(cooldown={"seconds": 0})
    _alert(hass, "2026-11-01T10:00:00+00:00", "motion")
    await hass.async_block_till_done()
    assert calls == []


async def test_doorbell_is_urgent_and_chimes(
    hass: HomeAssistant, setup_blueprint
) -> None:
    calls = async_mock_service(hass, "notify", "mobile_app_pixel_9")
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
    calls = async_mock_service(hass, "notify", "mobile_app_pixel_9")
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
    calls = async_mock_service(hass, "notify", "mobile_app_pixel_9")
    await setup_blueprint(cooldown={"seconds": 0})
    hass.states.async_set(ALERT, "unavailable")
    await hass.async_block_till_done()
    _alert(hass, "2026-11-01T10:00:00+00:00", "person")
    await hass.async_block_till_done()
    assert calls == []


async def test_person_speaks_message(hass: HomeAssistant, setup_blueprint) -> None:
    async_mock_service(hass, "notify", "mobile_app_pixel_9")
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
