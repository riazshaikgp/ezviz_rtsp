"""The integration's local brand images must be served to the frontend.

Home Assistant serves a custom integration's brand images from
``custom_components/<domain>/brand/`` through the ``brands`` component. If the
``brand`` folder is not recognized, the frontend shows "icon not available"
next to the integration, so this guards against that regression.
"""

from __future__ import annotations

import pytest

from custom_components.ezviz_rtsp.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def test_brand_icon_is_served_from_the_component(hass, hass_client) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "brands", {})

    # The brands view is public, but it still expects the request to carry the
    # frontend's short-lived token (or an authenticated request).
    client = await hass_client()
    for image in ("icon.png", "logo.png", "icon@2x.png", "logo@2x.png"):
        resp = await client.get(f"/api/brands/integration/{DOMAIN}/{image}")
        assert resp.status == 200, f"{image}: HTTP {resp.status}"
        body = await resp.read()
        assert body[:8] == b"\x89PNG\r\n\x1a\n", f"{image} is not a PNG"
