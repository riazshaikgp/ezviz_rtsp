"""Integration test: a battery camera streams through the cloud on a real hass."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.const import CONF_TYPE, CONF_URL, Platform

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ezviz_rtsp.const import (
    ATTR_TYPE_CLOUD,
    CONF_RFSESSION_ID,
    CONF_SESSION_ID,
    DATA_CARD,
    DATA_LIVE_VIEW,
    DOMAIN,
)

CAM_DATA = {
    "BK1": {
        "name": "Door",
        "mac_address": None,
        "device_sub_category": "DP2",
        "version": "1.0",
        "status": 1,
        "alarm_notify": False,
        "local_ip": "192.168.1.50",
        "local_rtsp_port": 554,
        "battery_level": 52,
        "battery_camera_work_mode": 5,
        "supported_channels": 1,
        "encrypted": False,
    }
}


class _FakeClient:
    """Stands in for pyezvizapi's EzvizClient: a logged-in client with one camera."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.login_calls = 0

    def login(self, sms_code=None) -> dict:
        self.login_calls += 1
        return {}

    def load_cameras(self, refresh: bool = True) -> dict:
        return CAM_DATA

    def export_token(self) -> dict:
        return {}

    def get_detection_sensibility(self, _serial) -> None:
        return None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):  # noqa: ANN001
    yield


async def _setup(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_TYPE: ATTR_TYPE_CLOUD,
            CONF_SESSION_ID: "session",
            CONF_RFSESSION_ID: "refresh",
            CONF_URL: "apiieu.ezvizlife.com",
        },
        options={"ffmpeg_arguments": "/Streaming/Channels/102", "timeout": 25},
        unique_id="owner@example.com",
    )
    entry.add_to_hass(hass)
    # Only the camera platform: the cloud stream is what this fork adds.
    with (
        patch("custom_components.ezviz_rtsp.EzvizClient", _FakeClient),
        patch.dict(
            "custom_components.ezviz_rtsp.PLATFORMS_BY_TYPE",
            {ATTR_TYPE_CLOUD: [Platform.CAMERA]},
        ),
        patch("custom_components.ezviz_rtsp.camera.get_ffmpeg_manager"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_battery_camera_sets_up_and_offers_a_cloud_stream(hass) -> None:
    entry = await _setup(hass)

    # The camera entity exists and advertises streaming.
    camera_ids = [s for s in hass.states.async_entity_ids("camera")]
    assert camera_ids, "no camera entity was created"
    state = hass.states.get(camera_ids[0])
    assert state is not None
    assert state.attributes["supported_features"] & CameraEntityFeature.STREAM

    # The cloud stream was built for the camera, and the view registered.
    assert set(entry.runtime_data.live_streams) == {"BK1"}
    assert hass.data[DATA_LIVE_VIEW] is True

    # A battery camera must not trigger the RTSP discovery flow.
    assert hass.config_entries.flow.async_progress() == []

    # The entity's stream source is the loopback MPEG-TS address of our view.
    component = hass.data["entity_components"][Platform.CAMERA]
    entity = component.get_entity(camera_ids[0])
    assert entity is not None
    source = await entity.stream_source()
    assert source is not None
    assert source.startswith("http://127.0.0.1:")
    assert f"/api/{DOMAIN}/live/BK1?token=" in source
    # The stream source carries the same token the entry persisted.
    assert entry.options["stream_token"] in source

    # The Lovelace card is served and registered for the frontend.
    assert hass.data[DATA_CARD] is True


async def test_card_devices_websocket_lists_the_camera(hass, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 7, "type": f"{DOMAIN}/devices"})
    msg = await client.receive_json()
    assert msg["success"]
    (device,) = msg["result"]
    assert device["serial"] == "BK1"
    assert device["name"] == "Door"
    assert device["battery"] is True
    assert device["entity_id"]
