"""Value mappings of the floodlight and siren entities."""

from __future__ import annotations

import pytest

from custom_components.swann import light, siren


@pytest.mark.parametrize(
    ("nvr", "ha"), [(1, 1), (50, 127), (100, 255), (0, 1), (150, 255)]
)
def test_to_ha_brightness(nvr: int, ha: int) -> None:
    assert light.to_ha_brightness(nvr, (1, 100)) == ha


@pytest.mark.parametrize(("ha", "nvr"), [(1, 1), (128, 51), (255, 100), (0, 1)])
def test_to_nvr_brightness(ha: int, nvr: int) -> None:
    assert light.to_nvr_brightness(ha, (1, 100)) == nvr


def test_brightness_round_trip() -> None:
    for value in range(1, 101):
        ha = light.to_ha_brightness(value, (1, 100))
        assert light.to_nvr_brightness(ha, (1, 100)) == value


@pytest.mark.parametrize(("level", "volume"), [(1, 0.0), (10, 1.0), (5, 0.444)])
def test_siren_volume(level: int, volume: float) -> None:
    assert siren.to_volume(level, (1, 10)) == volume
    assert siren.to_level(volume, (1, 10)) == level
