"""Tests for the SD-card recordings: decoding the index, paging a day, clip helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import zlib
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util
from pyezvizapi.exceptions import PyEzvizError
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ezviz_rtsp import recordings as rec_module
from custom_components.ezviz_rtsp.const import DOMAIN, SIGNAL_RECORDING_DOWNLOADED
from custom_components.ezviz_rtsp.recordings import (
    MAX_PAGES,
    PAGE_SIZE,
    RecordingError,
    decode_records,
    ffmpeg_args,
    parse_stamp,
    search_day,
    utc_stamp,
)

TZ = ZoneInfo("Africa/Johannesburg")


def _packed(records: list[dict], pad: int = 0) -> str:
    return base64.b64encode(zlib.compress(json.dumps(records).encode()) + b"\0" * pad).decode()


def _mp4_bytes(seconds: float, timescale: int = 1000) -> bytes:
    """A minimal MP4 with just the boxes needed to read its duration."""
    body = (
        b"\x00\x00\x00\x00"  # version 0, flags
        + b"\x00" * 8  # creation and modification
        + timescale.to_bytes(4, "big")
        + int(seconds * timescale).to_bytes(4, "big")
        + b"\x00" * 8
    )
    mvhd = (8 + len(body)).to_bytes(4, "big") + b"mvhd" + body
    moov = (8 + len(mvhd)).to_bytes(4, "big") + b"moov" + mvhd
    ftyp = (16).to_bytes(4, "big") + b"ftyp" + b"isom" + b"\x00" * 4
    return ftyp + moov


def _rec(begin: datetime, seconds: int = 8) -> dict:
    return {
        "B": begin.strftime("%Y-%m-%dT%H:%M:%S"),
        "E": (begin + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S"),
        "Type": 0,
    }


def test_decode_records_plain_compressed_and_padded() -> None:
    records = [{"B": "2026-11-10T10:00:00", "E": "2026-11-10T10:00:08"}]
    assert decode_records({"records": records, "isFinished": 1}) == (records, False)
    assert decode_records({"records": _packed(records), "isFinished": 0}) == (records, True)
    # The app's payloads can carry zero padding after the compressed stream.
    assert decode_records({"records": _packed(records, pad=31), "isFinished": 1}) == (records, False)
    assert decode_records({"records": None, "isFinished": None}) == ([], False)
    assert decode_records({"records": ""}) == ([], False)


def test_decode_records_rejects_garbage() -> None:
    with pytest.raises(RecordingError):
        decode_records({"records": "not base64!"})


class FakeIndex:
    """Answers like /v3/streaming/v2/records: 50 records from the window start, oldest first."""

    def __init__(self, starts: list[datetime], fail: Exception | None = None) -> None:
        self.records = [_rec(start) for start in sorted(starts)]
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def search_records_v2(self, serial, channel, start, stop, *, size):
        assert channel == 1
        self.calls.append((start, stop))
        if self.fail:
            raise self.fail
        lo = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        hi = datetime.strptime(stop, "%Y-%m-%d %H:%M:%S")
        inside = [r for r in self.records if lo <= datetime.fromisoformat(r["B"]) < hi]
        page = inside[: min(size, PAGE_SIZE)]
        return {"records": _packed(page), "isFinished": 0 if len(inside) > len(page) else 1}


def test_search_day_pages_through_a_busy_day() -> None:
    day = date(2026, 11, 9)
    base = datetime(2026, 11, 9, 0, 5)
    starts = [base + timedelta(minutes=5 * i) for i in range(260)]  # ~21 h of clips
    index = FakeIndex(starts)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
    found, complete = search_day(index, "BB1", day, TZ, now)
    assert complete
    assert [b.replace(tzinfo=None) for b, _ in found] == starts
    assert all(b.tzinfo == TZ for b, _ in found)
    # 260 clips at 50 per answer: six searches, each starting after the last clip seen.
    assert len(index.calls) == 6
    assert index.calls[0][0] == "2026-11-09 00:00:00"
    assert index.calls[1][0] == (starts[49] + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")


def test_search_day_today_stops_at_now_and_dedupes() -> None:
    now = datetime(2026, 11, 10, 12, 0, 30, tzinfo=TZ)
    starts = [datetime(2026, 11, 10, 11, 0) + timedelta(minutes=i) for i in range(55)]
    index = FakeIndex(starts + starts[:3])  # duplicates in the index are dropped
    found, complete = search_day(index, "BB1", now.date(), TZ, now)
    assert complete
    assert len(found) == 55
    assert index.calls[0][1] == "2026-11-10 12:00:30"


def test_search_day_reports_a_cut_short_list() -> None:
    starts = [datetime(2026, 11, 9, 0, 0) + timedelta(seconds=20 * i) for i in range(4000)]
    found, complete = search_day(
        FakeIndex(starts), "BB1", date(2026, 11, 9), TZ, datetime(2026, 11, 10, tzinfo=TZ)
    )
    assert not complete
    assert len(found) == MAX_PAGES * PAGE_SIZE


@pytest.mark.parametrize(
    ("code", "text"),
    [("2009", "did not answer in time"), ("2004", "SD card"), ("9999", "Could not read")],
)
def test_search_day_explains_camera_errors(code: str, text: str, monkeypatch) -> None:
    monkeypatch.setattr(rec_module, "WAKE_RETRY_SECONDS", 0)
    index = FakeIndex([], fail=PyEzvizError(f"Could not search v2 records: response code={code}"))
    with pytest.raises(RecordingError, match=text):
        search_day(index, "BB1", date(2026, 11, 9), TZ, datetime(2026, 11, 10, tzinfo=TZ))
    # Only the "camera asleep" answer is retried, and only once.
    assert len(index.calls) == (2 if code == "2009" else 1)


def test_search_day_retries_once_while_the_camera_wakes(monkeypatch) -> None:
    monkeypatch.setattr(rec_module, "WAKE_RETRY_SECONDS", 0)
    index = FakeIndex([datetime(2026, 11, 9, 8, 0)])
    real = index.search_records_v2

    def waking(*args, **kwargs):
        if not index.calls:
            index.calls.append(("asleep", ""))
            raise PyEzvizError("Could not search v2 records: response code=2009, fields=4")
        return real(*args, **kwargs)

    index.search_records_v2 = waking
    found, complete = search_day(
        index, "BB1", date(2026, 11, 9), TZ, datetime(2026, 11, 10, tzinfo=TZ)
    )
    assert complete
    assert len(found) == 1


def test_utc_stamps_round_trip() -> None:
    local = datetime(2026, 11, 10, 10, 37, 2, tzinfo=TZ)
    assert utc_stamp(local) == "20261110T083702Z"
    assert parse_stamp("20261110T083702Z") == datetime(2026, 11, 10, 8, 37, 2, tzinfo=UTC)
    with pytest.raises(RecordingError):
        parse_stamp("2026-11-10 08:37:02")


def test_ffmpeg_args_copy_hevc_or_encode_h264() -> None:
    hevc = ffmpeg_args("hevc")
    assert hevc[hevc.index("-c:v") + 1] == "copy"
    assert "hvc1" in hevc
    h264 = ffmpeg_args("h264")
    assert h264[h264.index("-c:v") + 1] == "libx264"
    for args in (hevc, h264):
        assert "+faststart" in args
        assert args[-2:] == ("-f", "mp4")


def test_media_root_uses_the_media_dirs_setting() -> None:
    config = SimpleNamespace(
        media_dirs={"local": "/media"}, path=lambda *parts: "/config/" + "/".join(parts)
    )
    hass = SimpleNamespace(config=config)
    assert rec_module.media_root(hass) == Path("/media") / DOMAIN
    # With no media dir configured it falls back to <config>/media.
    config.media_dirs = {}
    assert rec_module.media_root(hass) == Path("/config/media") / DOMAIN


# -- on a real hass (needs the HA test harness, so Linux) -------------------------



@pytest.fixture
def _cloud(enable_custom_integrations, hass, tmp_path):
    # Downloaded clips live under the media folder; point it at a per-test
    # directory so runs don't see each other's files (the HA test config dir is
    # shared).
    hass.config.media_dirs = {"local": str(tmp_path)}
    yield


async def _setup_with_index(hass, starts: list[datetime]):
    from tests.test_integration import _FakeClient, _setup

    index = FakeIndex(starts)
    _FakeClient.search_records_v2 = lambda self, *a, **kw: index.search_records_v2(*a, **kw)
    try:
        entry = await _setup(hass)
    finally:
        del _FakeClient.search_records_v2
    entry.runtime_data.ezviz_client.search_records_v2 = index.search_records_v2
    return entry, index


async def test_websocket_lists_a_day_and_hands_out_a_clip(
    hass, hass_ws_client, hass_client, _cloud, tmp_path
) -> None:
    await hass.config.async_set_time_zone("Africa/Johannesburg")
    tz = ZoneInfo("Africa/Johannesburg")
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    starts = [datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=h) for h in (8, 9, 10)]
    entry, index = await _setup_with_index(hass, starts)
    ws = await hass_ws_client(hass)

    await ws.send_json(
        {"id": 1, "type": "ezviz_rtsp/recordings", "serial": "BK1", "date": yesterday.isoformat()}
    )
    msg = await ws.receive_json()
    assert msg["success"], msg
    result = msg["result"]
    assert result["complete"] is True
    assert [r["start"][11:19] for r in result["recordings"]] == ["10:00:00", "09:00:00", "08:00:00"]
    assert result["recordings"][0]["seconds"] == 8
    # Nothing has been saved to the media folder yet.
    assert [r["local"] for r in result["recordings"]] == [False, False, False]
    calls = len(index.calls)

    # A past day is answered from the cache until a refresh is asked for.
    await ws.send_json(
        {"id": 2, "type": "ezviz_rtsp/recordings", "serial": "BK1", "date": yesterday.isoformat()}
    )
    assert (await ws.receive_json())["success"]
    assert len(index.calls) == calls

    # Fetching a clip: the camera is read once, converted, cached and served with ranges.
    newest = result["recordings"][0]

    async def fake_mp4(_bin, ps, fmt, target):
        target.write_bytes(b"\0\0\0\x18ftypmp42" + ps + fmt.encode())

    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA") as download,
        patch.object(rec_module, "_async_to_mp4", side_effect=fake_mp4),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        for msg_id in (3, 4):
            await ws.send_json(
                {
                    "id": msg_id,
                    "type": "ezviz_rtsp/recording/clip",
                    "serial": "BK1",
                    "begin": newest["begin"],
                    "end": newest["end"],
                    "format": "hevc",
                }
            )
            clip = await ws.receive_json()
            assert clip["success"], clip
            assert clip["result"]["saved"] is True
        assert download.call_count == 1
        args = download.call_args.args
        assert args[1:5] == ("BK1", newest["begin"], newest["end"], "Africa/Johannesburg")

    url = clip["result"]["url"]
    assert url.startswith(f"/api/{DOMAIN}/recording/BK1/{newest['begin']}/{newest['end']}/hevc.mp4?authSig=")
    client = await hass_client()
    response = await client.get(url, headers={"Range": "bytes=0-3"})
    assert response.status == 206
    assert await response.read() == b"\0\0\0\x18"

    # The downloaded clip is now marked local; the others still are not.
    await ws.send_json(
        {"id": 7, "type": "ezviz_rtsp/recordings", "serial": "BK1", "date": yesterday.isoformat()}
    )
    msg = await ws.receive_json()
    assert msg["success"], msg
    assert [r["local"] for r in msg["result"]["recordings"]] == [True, False, False]

    # Unknown cameras and bad input are refused with errors, not exceptions.
    await ws.send_json({"id": 8, "type": "ezviz_rtsp/recordings", "serial": "NOPE"})
    assert (await ws.receive_json())["error"]["code"] == "not_found"
    await ws.send_json(
        {"id": 9, "type": "ezviz_rtsp/recording/clip", "serial": "BK1", "begin": "x", "end": "y"}
    )
    assert (await ws.receive_json())["error"]["code"] == "recording_failed"

    # The clip lives under the media folder, in the folder of its local day, and
    # survives an unload.
    root = (await rec_module.async_get_store(hass)).root
    clip_file = (
        root
        / yesterday.strftime("%Y/%m/%d")
        / f"BK1_{newest['begin']}_{newest['end']}_hevc.mp4"
    )
    assert root == rec_module.media_root(hass)
    assert clip_file.exists()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert clip_file.exists()
    assert rec_module.DATA_RECORDINGS not in hass.data


async def test_list_marks_a_clip_saved_in_either_format_as_local(
    hass, hass_ws_client, _cloud
) -> None:
    await hass.config.async_set_time_zone("Africa/Johannesburg")
    tz = ZoneInfo("Africa/Johannesburg")
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    starts = [
        datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=h)
        for h in (8, 9)
    ]
    await _setup_with_index(hass, starts)
    ws = await hass_ws_client(hass)
    listing = {"id": 1, "type": "ezviz_rtsp/recordings", "serial": "BK1", "date": yesterday.isoformat()}
    await ws.send_json(listing)
    msg = await ws.receive_json()
    assert msg["success"], msg
    newer, older = msg["result"]["recordings"]
    assert [newer["local"], older["local"]] == [False, False]

    # Save only an H.264 copy of the older recording (as the download service
    # does), then let the store index the folder again, as a restart would.
    store = await rec_module.async_get_store(hass)
    saved = rec_module._clip_path(store, ("BK1", older["begin"], older["end"], "h264"))
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"mp4")
    await rec_module.async_close_store(hass)
    await rec_module.async_get_store(hass)

    listing["id"] = 2
    await ws.send_json(listing)
    msg = await ws.receive_json()
    assert msg["success"], msg
    flags = {r["begin"]: r["local"] for r in msg["result"]["recordings"]}
    assert flags[older["begin"]] is True
    assert flags[newer["begin"]] is False


async def test_websocket_keeps_a_watched_clip_out_of_media_when_asked(
    hass, hass_ws_client, hass_client, _cloud, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(rec_module, "temp_root", lambda _hass: tmp_path / "temp")
    await hass.config.async_set_time_zone("Africa/Johannesburg")
    tz = ZoneInfo("Africa/Johannesburg")
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    starts = [
        datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=h)
        for h in (8, 9)
    ]
    await _setup_with_index(hass, starts)
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "ezviz_rtsp/recordings", "serial": "BK1", "date": yesterday.isoformat()}
    )
    newest = (await ws.receive_json())["result"]["recordings"][0]

    async def fake_mp4(_bin, ps, fmt, target):
        target.write_bytes(b"\0\0\0\x18ftypmp42" + ps + fmt.encode())

    message = {
        "type": "ezviz_rtsp/recording/clip",
        "serial": "BK1",
        "begin": newest["begin"],
        "end": newest["end"],
        "format": "hevc",
        "keep": False,
    }
    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA") as download,
        patch.object(rec_module, "_async_to_mp4", side_effect=fake_mp4),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        await ws.send_json({"id": 2, **message})
        clip = await ws.receive_json()
        assert clip["success"], clip
        assert clip["result"]["saved"] is False

        # Nothing was stored under Media; the temporary copy is what plays.
        store = await rec_module.async_get_store(hass)
        assert not list(store.root.glob("*.mp4"))
        temp_file = tmp_path / "temp" / f"BK1_{newest['begin']}_{newest['end']}_hevc.mp4"
        assert temp_file.exists()
        client = await hass_client()
        response = await client.get(clip["result"]["url"])
        assert response.status == 200
        assert (await response.read()).startswith(b"\0\0\0\x18ftypmp42")

        # Playing it again reuses the temporary copy instead of waking the camera.
        await ws.send_json({"id": 3, **message})
        again = await ws.receive_json()
        assert again["success"], again
        assert again["result"]["saved"] is False
        assert download.call_count == 1


async def test_a_clip_already_saved_is_kept_with_saving_off(
    hass, hass_ws_client, _cloud, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(rec_module, "temp_root", lambda _hass: tmp_path / "temp")
    await hass.config.async_set_time_zone("Africa/Johannesburg")
    tz = ZoneInfo("Africa/Johannesburg")
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    starts = [
        datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=h)
        for h in (8, 9)
    ]
    await _setup_with_index(hass, starts)
    ws = await hass_ws_client(hass)
    await ws.send_json(
        {"id": 1, "type": "ezviz_rtsp/recordings", "serial": "BK1", "date": yesterday.isoformat()}
    )
    newest = (await ws.receive_json())["result"]["recordings"][0]

    async def fake_mp4(_bin, ps, fmt, target):
        target.write_bytes(b"\0\0\0\x18ftypmp42" + ps + fmt.encode())

    message = {
        "type": "ezviz_rtsp/recording/clip",
        "serial": "BK1",
        "begin": newest["begin"],
        "end": newest["end"],
        "format": "hevc",
    }
    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA") as download,
        patch.object(rec_module, "_async_to_mp4", side_effect=fake_mp4),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        # Saved the normal way first...
        await ws.send_json({"id": 2, "keep": True, **message})
        first = await ws.receive_json()
        assert first["success"], first
        assert first["result"]["saved"] is True

        # ...then watched with saving off: the media copy answers, nothing new.
        await ws.send_json({"id": 3, "keep": False, **message})
        second = await ws.receive_json()
        assert second["success"], second
        assert second["result"]["saved"] is True
        assert download.call_count == 1

    temp = tmp_path / "temp"
    assert not temp.exists() or not list(temp.glob("*.mp4"))


def test_temp_clips_are_swept_after_their_short_life(tmp_path) -> None:
    fresh = tmp_path / "BK1_20261110T080000Z_20261110T080008Z_h264.mp4"
    stale = tmp_path / "BK1_20261110T090000Z_20261110T090008Z_h264.mp4"
    fresh.write_bytes(b"mp4")
    stale.write_bytes(b"mp4")
    old = time.time() - rec_module.TEMP_CLIP_SECONDS - 60
    os.utime(stale, (old, old))

    rec_module._sweep_temp(tmp_path)

    assert fresh.exists()
    assert not stale.exists()


async def test_clip_refused_while_the_camera_streams_live(hass, _cloud) -> None:
    entry, _ = await _setup_with_index(hass, [])
    camera = rec_module.find_camera(hass, "BK1")
    assert camera is not None
    stream = entry.runtime_data.live_streams["BK1"]
    with patch.object(type(stream), "active", new=True), patch.object(
        rec_module, "_download_clip", new=AsyncMock()
    ) as download:
        begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
        with pytest.raises(rec_module.RecordingError, match="streaming live"):
            await rec_module.async_get_clip(hass, camera, begin, begin + timedelta(seconds=8), "h264")
        download.assert_not_called()


async def test_websocket_releases_the_live_stream_at_once(
    hass, hass_ws_client, _cloud
) -> None:
    entry, _ = await _setup_with_index(hass, [])
    stream = entry.runtime_data.live_streams["BK1"]
    ws = await hass_ws_client(hass)

    # Nothing is open: the answer says so, and nothing breaks.
    await ws.send_json({"id": 1, "type": "ezviz_rtsp/live/stop", "serial": "BK1"})
    msg = await ws.receive_json()
    assert msg["success"], msg
    assert msg["result"] == {"stopped": False}

    # With a live task open, the card's request releases it now, not after the
    # release delay; otherwise the first SD playback would find the camera busy.
    async def forever() -> None:
        await asyncio.sleep(3600)

    stream._task = asyncio.create_task(forever())
    assert stream.active
    await ws.send_json({"id": 2, "type": "ezviz_rtsp/live/stop", "serial": "BK1"})
    msg = await ws.receive_json()
    assert msg["success"], msg
    assert msg["result"] == {"stopped": True}
    assert not stream.active


async def test_a_clip_saved_in_media_is_served_without_the_camera(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    camera = rec_module.find_camera(hass, "BK1")
    begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    end = begin + timedelta(seconds=8)
    saved = rec_module._clip_path(
        store, ("BK1", "20261110T080000Z", "20261110T080008Z", "h264")
    )
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"mp4")
    # The index can miss a file that is on disk: a restored backup, a copied
    # clip, or a store built before the file was written.
    store.clips.clear()

    with patch.object(rec_module, "_download_clip", new=AsyncMock()) as download:
        path = await rec_module.async_get_clip(hass, camera, begin, end, "h264")

    assert path == saved
    download.assert_not_called()
    assert store.clips[("BK1", "20261110T080000Z", "20261110T080008Z", "h264")] == saved


async def test_an_h264_copy_answers_an_hevc_request(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    camera = rec_module.find_camera(hass, "BK1")
    begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    end = begin + timedelta(seconds=8)
    # "Download latest recording" and the browser asking for HEVC must not store
    # the same recording twice: H.264 plays everywhere.
    saved = rec_module._clip_path(
        store, ("BK1", "20261110T080000Z", "20261110T080008Z", "h264")
    )
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"mp4")
    store.clips.clear()

    with patch.object(rec_module, "_download_clip", new=AsyncMock()) as download:
        path = await rec_module.async_get_clip(hass, camera, begin, end, "hevc")

    assert path == saved
    download.assert_not_called()


async def test_an_hevc_only_copy_is_not_used_for_h264(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    camera = rec_module.find_camera(hass, "BK1")
    begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    end = begin + timedelta(seconds=8)
    hevc = rec_module._clip_path(
        store, ("BK1", "20261110T080000Z", "20261110T080008Z", "hevc")
    )
    hevc.parent.mkdir(parents=True)
    hevc.write_bytes(b"mp4")
    store.clips.clear()

    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA") as download,
        patch.object(
            rec_module,
            "_async_to_mp4",
            side_effect=lambda _bin, _ps, _fmt, target: target.write_bytes(b"mp4"),
        ),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        path = await rec_module.async_get_clip(hass, camera, begin, end, "h264")

    # HEVC does not play everywhere, so an H.264 request still needs its copy.
    download.assert_called_once()
    assert path.name.endswith("_h264.mp4")
    assert path.exists() and path != hevc


async def test_a_clip_removed_from_media_is_fetched_again(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    camera = rec_module.find_camera(hass, "BK1")
    begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    end = begin + timedelta(seconds=8)
    key = ("BK1", "20261110T080000Z", "20261110T080008Z", "h264")
    gone = rec_module._clip_path(store, key)
    store.clips[key] = gone  # indexed, but the file was deleted by hand

    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA") as download,
        patch.object(
            rec_module,
            "_async_to_mp4",
            side_effect=lambda _bin, _ps, _fmt, target: target.write_bytes(b"mp4"),
        ),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        path = await rec_module.async_get_clip(hass, camera, begin, end, "h264")

    download.assert_called_once()
    assert path == gone and gone.exists()


def _playback_stream(*results) -> MagicMock:
    stream = MagicMock()
    stream.start.side_effect = [SimpleNamespace(result=result) for result in results]
    return stream


def _patch_playback(monkeypatch, stream: MagicMock) -> MagicMock:
    opener = MagicMock(return_value=stream)
    monkeypatch.setattr(
        "pyezvizapi.streaming.cloud.playback.open_cloud_playback_stream", opener
    )
    return opener


def test_playback_negotiation_retries_while_the_camera_is_busy(monkeypatch) -> None:
    stream = _playback_stream(
        rec_module.BUSY_RESULT_CODE, rec_module.BUSY_RESULT_CODE, 0
    )
    opener = _patch_playback(monkeypatch, stream)
    sleeps: list[float] = []
    monkeypatch.setattr(rec_module.time, "sleep", sleeps.append)

    got = rec_module._open_playback(
        MagicMock(),
        "BK1",
        "20261110T080000Z",
        "20261110T080008Z",
        "Africa/Johannesburg",
        time.monotonic() + 60,
    )

    assert got is stream
    assert opener.call_count == 3
    assert sleeps == [rec_module.PLAYBACK_RETRY_SECONDS] * 2
    assert stream.close.call_count == 2


def test_playback_negotiation_does_not_retry_another_refusal(monkeypatch) -> None:
    from pyezvizapi.exceptions import EzvizPlaybackNegotiationError

    stream = _playback_stream(4001)
    opener = _patch_playback(monkeypatch, stream)
    sleeps: list[float] = []
    monkeypatch.setattr(rec_module.time, "sleep", sleeps.append)

    with pytest.raises(EzvizPlaybackNegotiationError):
        rec_module._open_playback(
            MagicMock(),
            "BK1",
            "20261110T080000Z",
            "20261110T080008Z",
            "Africa/Johannesburg",
            time.monotonic() + 60,
        )

    assert opener.call_count == 1
    assert sleeps == []


def test_playback_negotiation_gives_up_when_the_camera_stays_busy(monkeypatch) -> None:
    from pyezvizapi.exceptions import EzvizPlaybackNegotiationError

    stream = _playback_stream(*[rec_module.BUSY_RESULT_CODE] * rec_module.PLAYBACK_ATTEMPTS)
    opener = _patch_playback(monkeypatch, stream)
    sleeps: list[float] = []
    monkeypatch.setattr(rec_module.time, "sleep", sleeps.append)

    with pytest.raises(EzvizPlaybackNegotiationError) as raised:
        rec_module._open_playback(
            MagicMock(),
            "BK1",
            "20261110T080000Z",
            "20261110T080008Z",
            "Africa/Johannesburg",
            time.monotonic() + 60,
        )

    assert raised.value.result_code == rec_module.BUSY_RESULT_CODE
    assert opener.call_count == rec_module.PLAYBACK_ATTEMPTS
    assert len(sleeps) == rec_module.PLAYBACK_ATTEMPTS - 1


def test_transfer_window_grows_with_a_long_recording() -> None:
    # A short clip keeps the minimum, enough for the camera to wake up.
    assert rec_module._transfer_budget(10) == rec_module.CLIP_DEADLINE
    # A minute needs more than the old fixed 90 s window, which used to cut it.
    assert rec_module._transfer_budget(60) > rec_module.CLIP_DEADLINE
    # The longest allowed clip still gets a bounded window.
    assert (
        rec_module._transfer_budget(rec_module.MAX_CLIP_SECONDS)
        == rec_module.MAX_TRANSFER_SECONDS
    )


def test_a_fraction_short_is_not_a_shortfall() -> None:
    # The camera often closes the file a fraction early; that is not a retry.
    assert rec_module._short_by(11.4, 12) == 0
    assert rec_module._short_by(118, 120) == 0
    # A real gap is.
    assert rec_module._short_by(9.0, 12) > 0
    assert rec_module._short_by(100, 120) > 0


def test_mp4_duration_is_read_from_the_movie_header(tmp_path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(_mp4_bytes(12.5))
    assert rec_module._mp4_seconds(clip) == 12.5

    (tmp_path / "bad.mp4").write_bytes(b"not an mp4 at all")
    assert rec_module._mp4_seconds(tmp_path / "bad.mp4") is None


async def test_a_cached_clip_shorter_than_its_interval_is_fetched_again(
    hass, _cloud
) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    camera = rec_module.find_camera(hass, "BK1")
    begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    end = begin + timedelta(seconds=8)
    key = ("BK1", "20261110T080000Z", "20261110T080008Z", "h264")
    saved = rec_module._clip_path(store, key)
    saved.parent.mkdir(parents=True)
    # What the fixed 90 s window used to leave behind: a file much shorter than
    # the interval in its name.
    saved.write_bytes(_mp4_bytes(2))
    store.clips.clear()

    async def full_mp4(_bin, _ps, _fmt, target):
        target.write_bytes(_mp4_bytes(8))

    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA") as download,
        patch.object(rec_module, "_async_to_mp4", side_effect=full_mp4),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        path = await rec_module.async_get_clip(hass, camera, begin, end, "h264")

    download.assert_called_once()
    assert path == saved
    assert rec_module._mp4_seconds(path) == 8.0
    assert key not in store.short


async def test_a_recording_the_camera_sends_short_is_kept_and_served(
    hass, _cloud
) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    camera = rec_module.find_camera(hass, "BK1")
    begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    end = begin + timedelta(seconds=8)
    key = ("BK1", "20261110T080000Z", "20261110T080008Z", "h264")
    saved = rec_module._clip_path(store, key)

    async def short_mp4(_bin, _ps, _fmt, target):
        target.write_bytes(_mp4_bytes(6))  # the camera itself sends 6 of 8 s

    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA"),
        patch.object(rec_module, "_async_to_mp4", side_effect=short_mp4),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        path = await rec_module.async_get_clip(hass, camera, begin, end, "h264")

    # Kept, and marked: a second play must not wake the camera again.
    assert path == saved and path.exists()
    assert key in store.short
    with patch.object(rec_module, "_download_clip", new=AsyncMock()) as download:
        again = await rec_module.async_get_clip(hass, camera, begin, end, "h264")
    download.assert_not_called()
    assert again == path


async def test_a_cached_clip_of_the_right_length_is_not_fetched_again(
    hass, _cloud
) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    camera = rec_module.find_camera(hass, "BK1")
    begin = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
    end = begin + timedelta(seconds=8)
    key = ("BK1", "20261110T080000Z", "20261110T080008Z", "h264")
    saved = rec_module._clip_path(store, key)
    saved.parent.mkdir(parents=True)
    saved.write_bytes(_mp4_bytes(8))
    store.clips.clear()

    with patch.object(rec_module, "_download_clip", new=AsyncMock()) as download:
        path = await rec_module.async_get_clip(hass, camera, begin, end, "h264")

    assert path == saved
    download.assert_not_called()


def test_a_short_download_is_fetched_once_more(monkeypatch) -> None:
    opener = MagicMock(side_effect=[MagicMock(), MagicMock()])
    monkeypatch.setattr(rec_module, "_open_playback", opener)
    monkeypatch.setattr(
        rec_module,
        "_read_playback",
        MagicMock(side_effect=[bytearray(b"PS1"), bytearray(b"PS2")]),
    )
    spans = MagicMock(side_effect=[5.0, 12.0])
    monkeypatch.setattr(rec_module, "_delivered_seconds", spans)

    clip = rec_module._download_clip(
        MagicMock(),
        "BK1",
        "20261110T080000Z",
        "20261110T080012Z",
        "Africa/Johannesburg",
        None,
    )

    assert clip == b"PS2"
    assert opener.call_count == 2
    assert spans.call_count == 2
    first_deadline = opener.call_args_list[0].args[-1]
    second_deadline = opener.call_args_list[1].args[-1]
    assert second_deadline > first_deadline


def test_a_whole_download_is_not_fetched_twice(monkeypatch) -> None:
    opener = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(rec_module, "_open_playback", opener)
    monkeypatch.setattr(
        rec_module, "_read_playback", MagicMock(return_value=bytearray(b"PS"))
    )
    monkeypatch.setattr(rec_module, "_delivered_seconds", MagicMock(return_value=12.0))

    clip = rec_module._download_clip(
        MagicMock(),
        "BK1",
        "20261110T080000Z",
        "20261110T080012Z",
        "Africa/Johannesburg",
        None,
    )

    assert clip == b"PS"
    assert opener.call_count == 1


async def test_flat_clips_are_migrated_into_day_folders(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    # A clip saved flat by a version without the year/month/day folders.
    clip = store.root / "BK1_20261110T080000Z_20261110T080008Z_h264.mp4"
    clip.write_bytes(b"mp4")

    # A restart forgets the store; the next use rebuilds the index from the
    # folder and files the old clip away under its day.
    key = ("BK1", "20261110T080000Z", "20261110T080008Z", "h264")
    await rec_module.async_close_store(hass)
    assert rec_module.DATA_RECORDINGS not in hass.data
    again = await rec_module.async_get_store(hass)
    moved = rec_module._clip_path(again, key)
    assert not clip.exists()
    assert moved.exists()
    assert again.clips[key] == moved


async def test_downloaded_clips_are_pruned_by_retention(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    fresh = store.root / "BK1_20261110T080000Z_20261110T080008Z_h264.mp4"
    stale = store.root / "BK1_20261101T080000Z_20261101T080008Z_h264.mp4"
    fresh.write_bytes(b"fresh")
    stale.write_bytes(b"stale")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(stale, (ten_days_ago, ten_days_ago))

    # The default keeps a week: the ten-day-old clip is swept, the new one stays.
    assert await rec_module.async_prune_recordings(hass) == 1
    assert fresh.exists() and not stale.exists()

    # 0 keeps them until they are removed by hand.
    stale.write_bytes(b"stale")
    os.utime(stale, (ten_days_ago, ten_days_ago))
    with patch.object(rec_module, "_keep_days", return_value=0):
        assert await rec_module.async_prune_recordings(hass) == 0
    assert stale.exists()


async def test_a_downloaded_clip_is_filed_under_its_local_day(hass, _cloud) -> None:
    await hass.config.async_set_time_zone("Africa/Johannesburg")
    await _setup_with_index(hass, [])
    camera = rec_module.find_camera(hass, "BK1")
    # 22:30 UTC is already the next day in Johannesburg (UTC+2).
    begin = datetime(2026, 11, 10, 22, 30, tzinfo=UTC)
    end = begin + timedelta(seconds=8)

    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA"),
        patch.object(
            rec_module,
            "_async_to_mp4",
            side_effect=lambda _bin, _ps, _fmt, target: target.write_bytes(b"mp4"),
        ),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        path = await rec_module.async_get_clip(hass, camera, begin, end, "h264")

    assert path == (
        rec_module.media_root(hass)
        / "2026/11/11"
        / "BK1_20261110T223000Z_20261110T223008Z_h264.mp4"
    )
    assert path.exists()


async def test_retention_removes_empty_day_folders(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    folder = store.root / "2026/10/01"
    folder.mkdir(parents=True)
    stale = folder / "BK1_20261001T080000Z_20261001T080008Z_h264.mp4"
    stale.write_bytes(b"stale")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(stale, (ten_days_ago, ten_days_ago))

    assert await rec_module.async_prune_recordings(hass) == 1
    assert not stale.exists()
    # The empty year/month/day folders go with it.
    assert not (store.root / "2026").exists()


async def test_recordings_sweep_is_scheduled_and_stopped(hass, _cloud) -> None:
    entry, _ = await _setup_with_index(hass, [])
    assert rec_module.DATA_RECORDINGS_TIMER in hass.data
    # The last account unloading stops the daily sweep.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert rec_module.DATA_RECORDINGS_TIMER not in hass.data


async def test_daily_sweep_prunes_old_clips(hass, _cloud) -> None:
    await _setup_with_index(hass, [])
    store = await rec_module.async_get_store(hass)
    stale = store.root / "BK1_20261101T080000Z_20261101T080008Z_h264.mp4"
    stale.write_bytes(b"stale")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(stale, (ten_days_ago, ten_days_ago))

    # A day later the timer fires and sweeps the clip that has aged out.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(days=1))
    await hass.async_block_till_done()

    assert not stale.exists()


# -- the latest recording: service, button and sensor ----------------------------


@contextmanager
def _clip_available():
    """Enter the patchers that stand in for the camera read and the ffmpeg conversion."""
    with (
        patch.object(rec_module, "_download_clip", return_value=b"PSDATA"),
        patch.object(
            rec_module,
            "_async_to_mp4",
            side_effect=lambda _bin, ps, fmt, target: target.write_bytes(b"mp4"),
        ),
        patch("custom_components.ezviz_rtsp.live.ffmpeg_binary", return_value="ffmpeg"),
    ):
        yield


def _bare_camera() -> rec_module.Camera:
    return rec_module.Camera(coordinator=MagicMock(), serial="BK1", code=None)


def _bare_hass() -> MagicMock:
    hass = MagicMock()
    hass.config.time_zone = "Africa/Johannesburg"
    return hass


async def test_latest_recording_prefers_today() -> None:
    tz = ZoneInfo("Africa/Johannesburg")
    today = datetime.now(tz).date()
    newest = (
        datetime(2026, 11, 10, 9, 0, tzinfo=tz),
        datetime(2026, 11, 10, 9, 0, 8, tzinfo=tz),
    )
    seen: list[date] = []

    async def fake_list(_hass, _camera, day, *, refresh=False):
        seen.append(day)
        return ([newest], True) if day == today else ([], True)

    with (
        patch.object(rec_module, "async_list_day", fake_list),
        patch.object(rec_module, "async_get_clip", return_value=Path("/media/today.mp4")),
    ):
        begin, end, path = await rec_module.async_latest_recording(_bare_hass(), _bare_camera())

    assert seen == [today]
    assert (begin, end) == newest
    assert path == Path("/media/today.mp4")


async def test_latest_recording_falls_back_to_yesterday() -> None:
    tz = ZoneInfo("Africa/Johannesburg")
    today = datetime.now(tz).date()
    yesterday = today - timedelta(days=1)
    older = (
        datetime(2026, 11, 9, 9, 0, tzinfo=tz),
        datetime(2026, 11, 9, 9, 0, 8, tzinfo=tz),
    )
    seen: list[date] = []

    async def fake_list(_hass, _camera, day, *, refresh=False):
        seen.append(day)
        return ([older], True) if day == yesterday else ([], True)

    with (
        patch.object(rec_module, "async_list_day", fake_list),
        patch.object(rec_module, "async_get_clip", return_value=Path("/media/yesterday.mp4")),
    ):
        begin, _end, path = await rec_module.async_latest_recording(_bare_hass(), _bare_camera())

    assert seen == [today, yesterday]
    assert begin == older[0]
    assert path == Path("/media/yesterday.mp4")


async def test_latest_recording_raises_when_there_are_none() -> None:
    async def fake_list(*_args, **_kwargs):
        return [], True

    with (
        patch.object(rec_module, "async_list_day", fake_list),
        pytest.raises(RecordingError, match="No recordings"),
    ):
        await rec_module.async_latest_recording(_bare_hass(), _bare_camera())


async def test_download_latest_announces_the_recording() -> None:
    tz = ZoneInfo("Africa/Johannesburg")
    begin = datetime(2026, 11, 10, 9, 0, tzinfo=tz)
    end = begin + timedelta(seconds=8)

    with (
        patch.object(
            rec_module,
            "async_latest_recording",
            return_value=(begin, end, Path("/media/BK1_x.mp4")),
        ),
        patch.object(rec_module, "async_dispatcher_send") as send,
    ):
        result = await rec_module.async_download_latest(_bare_hass(), _bare_camera())

    assert result["serial"] == "BK1"
    assert result["format"] == "h264"
    assert result["seconds"] == 8
    assert result["path"] == "/media/BK1_x.mp4"
    send.assert_called_once()
    assert send.call_args.args[1] == SIGNAL_RECORDING_DOWNLOADED.format("BK1")
    assert send.call_args.args[2] == result


async def _yesterday_camera(hass) -> None:
    tz = ZoneInfo("Africa/Johannesburg")
    await hass.config.async_set_time_zone("Africa/Johannesburg")
    yesterday = datetime.now(tz).date() - timedelta(days=1)
    starts = [
        datetime.combine(yesterday, datetime.min.time()) + timedelta(hours=h) for h in (8, 9)
    ]
    await _setup_with_index(hass, starts)


async def test_download_recording_service_updates_the_sensor(hass, _cloud) -> None:
    await _yesterday_camera(hass)
    sensor_id = next(
        entity
        for entity in hass.states.async_entity_ids("sensor")
        if entity.endswith("_latest_recording")
    )
    assert hass.states.get(sensor_id).state == "unknown"
    camera_id = next(iter(hass.states.async_entity_ids("camera")))

    with _clip_available():
        response = await hass.services.async_call(
            DOMAIN,
            "download_recording",
            {"entity_id": camera_id},
            blocking=True,
            return_response=True,
        )

    recording = response[camera_id]
    assert recording["serial"] == "BK1"
    assert recording["format"] == "h264"
    assert recording["seconds"] == 8
    state = hass.states.get(sensor_id)
    assert state.state != "unknown"
    assert state.attributes["path"].endswith(".mp4")
    assert state.attributes["format"] == "h264"


async def test_download_recording_button_updates_the_sensor(hass, _cloud) -> None:
    await _yesterday_camera(hass)
    sensor_id = next(
        entity
        for entity in hass.states.async_entity_ids("sensor")
        if entity.endswith("_latest_recording")
    )
    button_id = next(
        entity
        for entity in hass.states.async_entity_ids("button")
        if entity.endswith("_download_latest_recording")
    )

    with _clip_available():
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )
        await hass.async_block_till_done()

    assert hass.states.get(sensor_id).state != "unknown"
