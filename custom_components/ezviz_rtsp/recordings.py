"""A camera's SD-card recordings: list them and play one back.

Cameras with an SD card record events (or continuously) on the card, and the
EZVIZ cloud can do two things with that card the mobile app relies on:

* **search** it: ``/v3/streaming/v2/records`` returns the recorded intervals of
  a time window, at most 50 per answer, oldest first, in the camera's local
  time. ``async_list_day`` pages through one local day and caches it.
* **replay** an interval: the VTM relay that carries the live view also serves
  SD playback. ``_download_clip`` reads one interval as MPEG-PS (pyezvizapi),
  and ffmpeg turns it into an MP4 the browser can play: the original HEVC
  copied as is for browsers that play HEVC, or re-encoded to H.264 for the
  rest. Finished clips are kept as MP4 files under Home Assistant's media
  folder, so seeking (HTTP range requests) and a second play don't wake the
  camera again, and they survive a restart until the retention setting (how
  many days to keep) removes the ones that have aged out.

Both go through the camera: a sleeping battery camera is woken for a replay, so
the card only ever fetches a clip when someone taps it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import zlib
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiohttp import web
from homeassistant.const import CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import (
    ATTR_TYPE_CLOUD,
    CONF_CODES,
    CONF_RECORDINGS_KEEP_DAYS,
    DEFAULT_RECORDINGS_KEEP_DAYS,
    DOMAIN,
    SIGNAL_RECORDING_DOWNLOADED,
)

if TYPE_CHECKING:
    from .coordinator import EzvizConfigEntry, EzvizDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# The SD index answers at most this many records per request (asking for more
# changes nothing), oldest first.
PAGE_SIZE = 50
# A busy camera records a few hundred clips a day; this bounds one day's search.
MAX_PAGES = 40
# A past day's list is kept until the card asks for a refresh; today's for this long.
TODAY_TTL = 60.0
# The camera channel recordings are searched and replayed on (0 is refused).
CHANNEL = 1
# A sleeping camera answers its first search with EZVIZ error 2009; retry once after this long.
WAKE_RETRY_SECONDS = 5.0

# Replay: give up if the camera hasn't delivered the whole clip in this time
# (it may first have to wake up), and call a clip finished once no data came
# for IDLE seconds after the first packet.
CLIP_DEADLINE = 90.0
CLIP_IDLE = 4.0
# The socket's own timeout must outlast a camera waking up.
CLIP_SOCKET_TIMEOUT = 30.0
CLIP_MAX_BYTES = 96 * 1024 * 1024
# A recording longer than this is not a clip; refuse it rather than stream an hour.
MAX_CLIP_SECONDS = 15 * 60
_FFMPEG_TIMEOUT = 120

FORMATS = ("hevc", "h264")


class RecordingError(Exception):
    """Listing or replaying a recording failed; the message is shown on the card."""


@dataclass
class _Day:
    """One local day's recordings of one camera."""

    recordings: list[tuple[datetime, datetime]]
    fetched: float
    complete: bool


@dataclass
class RecordingStore:
    """Per Home Assistant: the day lists, the clip index and its locks."""

    root: Path
    days: dict[tuple[str, date], _Day] = field(default_factory=dict)
    clips: OrderedDict[tuple[str, str, str, str], Path] = field(default_factory=OrderedDict)
    pending: dict[tuple[str, str, str, str], asyncio.Future[Path]] = field(default_factory=dict)
    # One SD request per camera at a time: EZVIZ refuses parallel sessions.
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def lock(self, serial: str) -> asyncio.Lock:
        """The lock that serialises SD requests to one camera."""
        return self.locks.setdefault(serial, asyncio.Lock())


DATA_RECORDINGS = f"{DOMAIN}_recordings"
DATA_RECORDINGS_LOCK = f"{DOMAIN}_recordings_lock"
DATA_RECORDINGS_TIMER = f"{DOMAIN}_recordings_timer"
# How often the downloaded clips are swept on top of the start-up sweep.
PRUNE_INTERVAL = timedelta(days=1)


def media_root(hass: HomeAssistant) -> Path:
    """The folder under Home Assistant's media directory that keeps the clips."""
    dirs = getattr(hass.config, "media_dirs", None) or {}
    base = dirs.get("local") or hass.config.path("media")
    return Path(base) / DOMAIN


def _key_from_name(name: str) -> tuple[str, str, str, str] | None:
    """The clip key of an MP4 file name, or None when it is not one of ours."""
    if not name.endswith(".mp4"):
        return None
    parts = name[:-4].rsplit("_", 3)
    if len(parts) != 4 or parts[3] not in FORMATS:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _prepare_store(root: Path) -> RecordingStore:
    """Make the media folder and index the MP4s already in it. Blocking.

    Runs in the executor: the folder can hold thousands of clips after a long
    retention, so neither the mkdir nor the glob may run in the event loop.
    """
    root.mkdir(parents=True, exist_ok=True)
    store = RecordingStore(root)
    try:
        files = sorted(root.glob("*.mp4"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return store
    for path in files:
        key = _key_from_name(path.name)
        if key is not None:
            store.clips[key] = path
    return store


def _saved_clip(store: RecordingStore, key: tuple[str, str, str, str]) -> Path | None:
    """A copy of the recording already in the media folder. Blocking.

    The requested format wins. An H.264 copy also answers an HEVC request:
    H.264 plays in every browser, so fetching or converting the same recording
    again would only wake the camera and read its SD card for nothing. The
    reverse is not done — an HEVC file does not play everywhere.
    """
    serial, begin, end, fmt = key
    path = store.root / f"{serial}_{begin}_{end}_{fmt}.mp4"
    if path.is_file():
        return path
    if fmt == "hevc":
        fallback = store.root / f"{serial}_{begin}_{end}_h264.mp4"
        if fallback.is_file():
            return fallback
    return None


async def async_get_store(hass: HomeAssistant) -> RecordingStore:
    """The recording store, made on first use over Home Assistant's media folder.

    The folder is created and indexed in the executor, once; concurrent callers
    share the one store through a lock.
    """
    if (store := hass.data.get(DATA_RECORDINGS)) is not None:
        return store
    lock: asyncio.Lock = hass.data.setdefault(DATA_RECORDINGS_LOCK, asyncio.Lock())
    async with lock:
        if (store := hass.data.get(DATA_RECORDINGS)) is not None:
            return store
        store = await hass.async_add_executor_job(_prepare_store, media_root(hass))
        hass.data[DATA_RECORDINGS] = store
        return store


def _keep_days(hass: HomeAssistant) -> int:
    """How many days downloaded clips are kept; 0 keeps them forever."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_TYPE) == ATTR_TYPE_CLOUD:
            try:
                return int(
                    entry.options.get(
                        CONF_RECORDINGS_KEEP_DAYS, DEFAULT_RECORDINGS_KEEP_DAYS
                    )
                )
            except (TypeError, ValueError):
                break
    return DEFAULT_RECORDINGS_KEEP_DAYS


def _prune(root: Path, keep_days: int) -> list[str]:
    """Delete the MP4s downloaded more than ``keep_days`` ago. Blocking."""
    if keep_days <= 0:
        return []
    cutoff = time.time() - keep_days * 86400
    removed: list[str] = []
    for path in root.glob("*.mp4"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.name)
        except OSError:
            pass
    return removed


async def async_prune_recordings(hass: HomeAssistant) -> int:
    """Drop the downloaded clips past their retention; returns how many went."""
    store = await async_get_store(hass)
    removed = await hass.async_add_executor_job(_prune, store.root, _keep_days(hass))
    for name in removed:
        key = _key_from_name(name)
        if key is not None:
            store.clips.pop(key, None)
    return len(removed)


async def async_setup_recordings(hass: HomeAssistant) -> None:
    """Sweep the clips now, then once a day for as long as Home Assistant runs.

    Without the timer, retention would only be applied at start-up and when a new
    clip is downloaded, so a long-running instance could hold old files forever.
    """
    await async_prune_recordings(hass)
    if DATA_RECORDINGS_TIMER in hass.data:
        return

    async def _sweep(_now: datetime) -> None:
        if removed := await async_prune_recordings(hass):
            _LOGGER.debug("Pruned %d recording(s) past their retention", removed)

    hass.data[DATA_RECORDINGS_TIMER] = async_track_time_interval(
        hass, _sweep, PRUNE_INTERVAL
    )


async def async_close_store(hass: HomeAssistant) -> None:
    """Forget the in-memory lists and stop the sweep; the clips stay in the media folder."""
    hass.data.pop(DATA_RECORDINGS, None)
    hass.data.pop(DATA_RECORDINGS_LOCK, None)
    if (unsub := hass.data.pop(DATA_RECORDINGS_TIMER, None)) is not None:
        unsub()


# -- finding a camera ------------------------------------------------------------


@dataclass(frozen=True)
class Camera:
    """The account client and settings a camera's recordings are read with."""

    coordinator: EzvizDataUpdateCoordinator
    serial: str
    code: str | None


def find_camera(hass: HomeAssistant, serial: str) -> Camera | None:
    """The cloud camera of any loaded entry, with its verification code if encrypted."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coordinator: EzvizDataUpdateCoordinator | None = getattr(entry, "runtime_data", None)
        data = getattr(coordinator, "data", None) or {}
        if serial in data and coordinator is not None:
            return Camera(coordinator, serial, _code(entry, data[serial], serial))
    return None


def _code(entry: EzvizConfigEntry, value: Mapping[str, Any], serial: str) -> str | None:
    # A stored code only means something while the camera encrypts its video.
    if not value.get("encrypted"):
        return None
    return entry.options.get(CONF_CODES, {}).get(serial)


# -- listing ---------------------------------------------------------------------


def decode_records(payload: Any) -> tuple[list[Any], bool]:
    """The records of a search answer, and whether the index says there are more.

    The list arrives either plain or as base64 of zlib-compressed JSON (the app's
    format, sometimes zero-padded after the compressed stream).
    """
    if not isinstance(payload, Mapping):
        return [], False
    more = payload.get("isFinished") in (0, False)
    value = payload.get("records")
    if value is None or value == "":
        return [], more
    if isinstance(value, list):
        return value, more
    if isinstance(value, str):
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(base64.b64decode(value, validate=True), 8 * 1024 * 1024)
            records = json.loads(raw)
        except (ValueError, zlib.error) as err:
            raise RecordingError("The camera's recording list could not be read") from err
        if isinstance(records, list):
            return records, more
    raise RecordingError("The camera's recording list could not be read")


def _local(value: Any, tz: ZoneInfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def search_day(
    client: Any, serial: str, day: date, tz: ZoneInfo, now: datetime
) -> tuple[list[tuple[datetime, datetime]], bool]:
    """All recordings that start on ``day`` (local), oldest first. Blocking.

    The index returns at most 50 records from the window's start, so the next
    request starts one second after the last record seen. The second value says
    whether the whole day was read (False when the page budget ran out).
    """
    from pyezvizapi.exceptions import PyEzvizError

    start = datetime.combine(day, datetime.min.time(), tz)
    end = min(start + timedelta(days=1), now.astimezone(tz).replace(microsecond=0))
    found: dict[tuple[datetime, datetime], None] = {}
    cursor = start
    retried = False
    for _ in range(MAX_PAGES):
        if cursor >= end:
            return list(found), True
        try:
            answer = client.search_records_v2(
                serial,
                CHANNEL,
                cursor.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"),
                size=PAGE_SIZE,
            )
        except PyEzvizError as err:
            # A sleeping camera answers the first search with 2009 while it wakes
            # (like capture_picture); ask once more after a moment.
            if "2009" not in str(err) or retried:
                raise RecordingError(_explain(err)) from err
            retried = True
            time.sleep(WAKE_RETRY_SECONDS)
            continue
        records, more = decode_records(answer)
        latest = cursor
        for record in records:
            if not isinstance(record, Mapping):
                continue
            begin = _local(record.get("B"), tz)
            stop = _local(record.get("E"), tz)
            if begin is None or stop is None or stop <= begin:
                continue
            found[(begin, stop)] = None
            latest = max(latest, begin)
        if not more or not records:
            return sorted(found), True
        # Resume one second after the newest start seen (always moving forward);
        # a record seen twice is deduplicated above.
        cursor = max(latest, cursor) + timedelta(seconds=1)
    return sorted(found), False


def _explain(err: Exception) -> str:
    text = str(err)
    if "2009" in text:
        return "The camera did not answer in time; it may be asleep or offline. Try again."
    if "2004" in text:
        return "The camera reported a problem reading its SD card."
    return f"Could not read the camera's recordings: {text}"


async def async_list_day(
    hass: HomeAssistant, camera: Camera, day: date, *, refresh: bool = False
) -> tuple[list[tuple[datetime, datetime]], bool]:
    """A local day's recordings, from the cache unless stale or ``refresh``."""
    store = await async_get_store(hass)
    tz = ZoneInfo(hass.config.time_zone)
    now = datetime.now(tz)
    key = (camera.serial, day)
    cached = store.days.get(key)
    loop_now = time.monotonic()
    if cached is not None and not refresh:
        past = day < now.date()
        if past or loop_now - cached.fetched < TODAY_TTL:
            return cached.recordings, cached.complete
    if day > now.date():
        return [], True
    async with store.lock(camera.serial):
        recordings, complete = await hass.async_add_executor_job(
            search_day, camera.coordinator.ezviz_client, camera.serial, day, tz, now
        )
    store.days[key] = _Day(recordings, time.monotonic(), complete)
    return recordings, complete


# -- replay ----------------------------------------------------------------------


def utc_stamp(moment: datetime) -> str:
    """A UTC instant in the form pyezvizapi's playback takes (20261110T083702Z)."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_stamp(value: str) -> datetime:
    """Read a UTC stamp back; raises RecordingError when malformed."""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError) as err:
        raise RecordingError(f"Not a recording time: {value!r}") from err


def _download_clip(
    client: Any, serial: str, begin: str, end: str, timezone: str, code: str | None
) -> bytes:
    """One SD interval as MPEG-PS through the cloud relay. Blocking.

    pyezvizapi's own ``save_clip`` refuses a clip whose video falls a fraction
    of a second short of the requested range (the camera often closes the file
    early), so this reads the relay directly and keeps whatever arrived.
    """
    from pyezvizapi.exceptions import EzvizPlaybackNegotiationError, PyEzvizError
    from pyezvizapi.streaming.cloud.playback import open_cloud_playback_stream
    from pyezvizapi.streaming.cloud.protocol import (
        VtmStreamEOF,
        VtmStreamTimeout,
        decrypt_hikvision_ps_video,
    )

    deadline = time.monotonic() + CLIP_DEADLINE
    data = bytearray()
    try:
        stream = open_cloud_playback_stream(
            client,
            serial,
            begin,
            end,
            channel=CHANNEL,
            timezone=timezone,
            timeout=CLIP_SOCKET_TIMEOUT,
            capture_deadline=deadline,
        )
        try:
            info = stream.start(deadline=deadline)
            if info.result not in (None, 0):
                raise EzvizPlaybackNegotiationError(info.result)

            def _take(**limits: Any) -> int:
                taken = 0
                for packet in stream.iter_packets(
                    duration_seconds=max(deadline - time.monotonic(), 1.0),
                    duration_from_start=True,
                    **limits,
                ):
                    if packet.encrypted:
                        raise RecordingError("The camera sent the clip on an encrypted channel")
                    data.extend(packet.body)
                    taken += 1
                    if len(data) > CLIP_MAX_BYTES:
                        raise RecordingError("The recording is too large to play here")
                return taken

            try:
                # A sleeping camera takes a while to send its first packet: wait
                # for it against the whole deadline. pyezvizapi's idle timer runs
                # from the start of iteration, so it only applies afterwards, when
                # IDLE seconds without data mean the clip is over.
                if _take(first_packet_deadline=deadline, max_packets=1):
                    _take(stream_packet_timeout_seconds=CLIP_IDLE)
            except VtmStreamEOF:
                pass
            except VtmStreamTimeout:
                if not data:
                    raise
        finally:
            with contextlib.suppress(OSError):
                stream.close()
    except EzvizPlaybackNegotiationError as err:
        raise RecordingError(
            f"The camera refused to play the recording (code {err.result_code}); "
            "it may be busy with another viewer. Try again."
        ) from err
    except (PyEzvizError, OSError) as err:
        if isinstance(err, RecordingError):
            raise
        raise RecordingError(f"The recording could not be played: {err}") from err
    if not data:
        raise RecordingError("The camera sent no video for this recording")
    clip = bytes(data)
    if code:
        clip = decrypt_hikvision_ps_video(clip, code, nalu_header_size=None)
    return clip


def ffmpeg_args(fmt: str) -> tuple[str, ...]:
    """ffmpeg output options for an MP4 of the chosen video format."""
    if fmt == "hevc":
        # Copy the camera's HEVC; Safari needs the hvc1 tag to play it.
        video = ("-c:v", "copy", "-tag:v", "hvc1")
    else:
        video = (
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", "-profile:v", "high",
        )
    return (
        "-map", "0:v:0", *video,
        "-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart", "-f", "mp4",
    )


async def _async_to_mp4(ffmpeg_bin: str, ps: bytes, fmt: str, target: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin,
        "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts", "-f", "mpeg", "-i", "pipe:0",
        *ffmpeg_args(fmt), str(target),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(_FFMPEG_TIMEOUT):
            _, err = await proc.communicate(ps)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RecordingError("Converting the recording took too long") from exc
    if proc.returncode != 0:
        message = err.decode(errors="replace").strip().splitlines()
        raise RecordingError(
            f"The recording could not be converted: {message[-1] if message else proc.returncode}"
        )


async def async_get_clip(
    hass: HomeAssistant, camera: Camera, begin: datetime, end: datetime, fmt: str
) -> Path:
    """The MP4 of one recording, from the cache or fetched from the camera now."""
    if fmt not in FORMATS:
        raise RecordingError(f"Unknown format {fmt!r}")
    if end <= begin:
        raise RecordingError("A recording must end after it begins")
    if (end - begin).total_seconds() > MAX_CLIP_SECONDS:
        raise RecordingError("The recording is too long to play here")
    store = await async_get_store(hass)
    key = (camera.serial, utc_stamp(begin), utc_stamp(end), fmt)
    if (path := store.clips.get(key)) is not None:
        if await hass.async_add_executor_job(path.is_file):
            store.clips.move_to_end(key)
            return path
        # Removed outside the integration (by hand, or by another tool).
        store.clips.pop(key, None)
    # The media folder is the source of truth: a clip saved earlier is served
    # again without touching the camera, even when the in-memory index missed it
    # (a restored backup, a copied file, or an index built before it was written).
    if (path := await hass.async_add_executor_job(_saved_clip, store, key)) is not None:
        store.clips[key] = path
        store.clips.move_to_end(key)
        return path
    if (pending := store.pending.get(key)) is not None:
        return await asyncio.shield(pending)
    future: asyncio.Future[Path] = asyncio.get_running_loop().create_future()
    store.pending[key] = future
    try:
        path = await _async_fetch(hass, store, camera, key, fmt)
    except BaseException as err:
        future.set_exception(err if isinstance(err, Exception) else RecordingError("Cancelled"))
        future.exception()  # mark retrieved: nobody else may be waiting
        raise
    finally:
        store.pending.pop(key, None)
    future.set_result(path)
    return path


async def async_latest_recording(
    hass: HomeAssistant, camera: Camera, *, fmt: str = "h264"
) -> tuple[datetime, datetime, Path]:
    """The camera's newest SD-card recording, downloaded now.

    Looks at today first and falls back to yesterday, so a camera that has not
    recorded anything yet today still answers with its last recording.
    """
    tz = ZoneInfo(hass.config.time_zone)
    today = datetime.now(tz).date()
    for day in (today, today - timedelta(days=1)):
        recordings, _ = await async_list_day(hass, camera, day)
        if recordings:
            begin, end = recordings[-1]
            path = await async_get_clip(hass, camera, begin, end, fmt)
            return begin, end, path
    raise RecordingError("No recordings were found for today or yesterday")


async def async_download_latest(
    hass: HomeAssistant, camera: Camera, *, fmt: str = "h264"
) -> dict[str, Any]:
    """Download the newest recording and announce it to the latest-recording sensors."""
    begin, end, path = await async_latest_recording(hass, camera, fmt=fmt)
    recording: dict[str, Any] = {
        "serial": camera.serial,
        "begin": utc_stamp(begin),
        "end": utc_stamp(end),
        "start": begin.isoformat(),
        "seconds": int((end - begin).total_seconds()),
        "format": fmt,
        "path": str(path),
    }
    async_dispatcher_send(
        hass, SIGNAL_RECORDING_DOWNLOADED.format(camera.serial), recording
    )
    return recording


async def _async_fetch(
    hass: HomeAssistant,
    store: RecordingStore,
    camera: Camera,
    key: tuple[str, str, str, str],
    fmt: str,
) -> Path:
    from .live import ffmpeg_binary, find_stream

    serial, begin, end, _ = key
    live = find_stream(hass, serial)
    if live is not None and live.active:
        raise RecordingError("The camera is streaming live; stop the live view first")
    started = time.monotonic()
    async with store.lock(serial):
        ps = await hass.async_add_executor_job(
            _download_clip,
            camera.coordinator.ezviz_client,
            serial,
            begin,
            end,
            hass.config.time_zone,
            camera.code,
        )
    fetched = time.monotonic()
    target = store.root / f"{serial}_{begin}_{end}_{fmt}.mp4"
    await _async_to_mp4(ffmpeg_binary(hass), ps, fmt, target)
    _LOGGER.debug(
        "Recording %s %s-%s: %d bytes from the camera in %.1f s, %s MP4 in %.1f s",
        serial, begin, end, len(ps), fetched - started, fmt, time.monotonic() - fetched,
    )
    store.clips[key] = target
    store.clips.move_to_end(key)
    # Clips are kept until the retention setting ages them out; a download is a
    # good moment to sweep, since the folder is on disk anyway.
    await async_prune_recordings(hass)
    return target


def clip_path(serial: str, begin: str, end: str, fmt: str) -> str:
    """The clip view's path, before it is signed for the card."""
    return f"/api/{DOMAIN}/recording/{serial}/{begin}/{end}/{fmt}.mp4"


class RecordingClipView(HomeAssistantView):
    """Serves a cached recording as MP4, with range requests for seeking.

    Behind Home Assistant authentication; the card reaches it through a short
    signed path that the ``recording/clip`` websocket command hands out once
    the clip is ready, so a request here never waits on the camera.
    """

    url = f"/api/{DOMAIN}/recording/{{serial}}/{{begin}}/{{end}}/{{fmt}}.mp4"
    name = f"api:{DOMAIN}:recording"
    requires_auth = True

    async def get(
        self, request: web.Request, serial: str, begin: str, end: str, fmt: str
    ) -> web.StreamResponse:
        """The clip's file, or 404 once retention has removed it."""
        hass = request.app[KEY_HASS]
        store = await async_get_store(hass)
        path = store.clips.get((serial, begin, end, fmt))
        if path is None or not path.is_file():
            raise web.HTTPNotFound
        return web.FileResponse(
            path, headers={"Content-Type": "video/mp4", "Cache-Control": "private, max-age=3600"}
        )
