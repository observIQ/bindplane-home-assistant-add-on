"""Brand images are served from the integration's brand folder."""

from pathlib import Path

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

BRAND = Path(__file__).parent.parent / "custom_components" / "bindplane" / "brand"
IMAGES = [
    "icon.png",
    "icon@2x.png",
    "logo.png",
    "logo@2x.png",
    "dark_icon.png",
    "dark_icon@2x.png",
    "dark_logo.png",
    "dark_logo@2x.png",
]


@pytest.mark.parametrize("image", IMAGES)
async def test_brand_image_served(hass: HomeAssistant, hass_client, image):
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()
    resp = await client.get(f"/api/brands/integration/bindplane/{image}?placeholder=no")
    assert resp.status == 200
    assert await resp.read() == (BRAND / image).read_bytes()
