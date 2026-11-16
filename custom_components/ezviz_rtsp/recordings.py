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
  many days to keep) removes the ones that have aged out. When the card has
  saving off, the clip goes to a temporary folder instead and is deleted after
  a couple of hours, so it never shows under Media.

Both go through the camera: a sleeping battery camera is woken for a replay, so
the card only ever fetches a clip when someone taps it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import tempfile
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiohttp import web
from homeassistant.const import CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView
from homeassistant.helpers.storage import Store

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
# The calendar marks a whole month by one search per day (the SD index has no
# summary of which days are busy). This bounds an explicit scan.
MONTH_SCAN_SECONDS = 40.0
# The camera channel recordings are searched and replayed on (0 is refused).
CHANNEL = 1
# A sleeping camera answers its first search with EZVIZ error 2009; retry once after this long.
WAKE_RETRY_SECONDS = 5.0

# Replay: the minimum time the relay gets to deliver a clip (it may first have
# to wake the camera). A longer recording needs a longer window, since the relay
# reads the SD card at a finite speed: the window grows with the clip's own
# length, and once no data came for IDLE seconds after the first packet the clip
# is taken as finished.
CLIP_DEADLINE = 90.0
CLIP_TRANSFER_RATE = 2.0
CLIP_TRANSFER_GRACE = 60.0
CLIP_IDLE = 10.0
# A clip shorter than this many seconds of the interval on the card is not the
# whole recording: fetch it once more before serving it.
CLIP_SHORTFALL = 1.5
CLIP_TRANSFER_ATTEMPTS = 2
# The socket's own timeout must outlast a camera waking up.
CLIP_SOCKET_TIMEOUT = 30.0
CLIP_MAX_BYTES = 96 * 1024 * 1024
# An MP4's metadata (moov) is small; cap what is read to find its duration.
_MOOV_MAX_BYTES = 8 * 1024 * 1024
# EZVIZ answers a playback request with 5405 while another viewer holds the
# camera: the app, a live view, or a session the relay is still releasing. The
# relay usually lets go a moment later, so a couple of attempts save the user a
# needless "try again".
BUSY_RESULT_CODE = 5405
PLAYBACK_ATTEMPTS = 3
PLAYBACK_RETRY_SECONDS = 2.0
# A recording longer than this is not a clip; refuse it rather than stream an hour.
MAX_CLIP_SECONDS = 15 * 60
# The longest transfer window a retried clip may ever get.
MAX_TRANSFER_SECONDS = MAX_CLIP_SECONDS * CLIP_TRANSFER_RATE + CLIP_TRANSFER_GRACE
_FFMPEG_TIMEOUT = 120
# A clip watched with "save watched recordings" off lives this long in the
# temporary folder before the sweep removes it (the signed address lives an hour).
TEMP_CLIP_SECONDS = 2 * 3600

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
    tz: tzinfo
    days: dict[tuple[str, date], _Day] = field(default_factory=dict)
    clips: OrderedDict[tuple[str, str, str, str], Path] = field(default_factory=OrderedDict)
    # Clips watched with saving off: files in the temp folder, never indexed in
    # ``clips`` and never listed as local.
    temp: dict[tuple[str, str, str, str], Path] = field(default_factory=dict)
    temp_pending: dict[tuple[str, str, str, str], asyncio.Future[Path]] = field(
        default_factory=dict
    )
    pending: dict[tuple[str, str, str, str], asyncio.Future[Path]] = field(default_factory=dict)
    # Clips the camera itself sends shorter than the index, even after a retry:
    # play them as they are instead of fetching them again on every play.
    short: set[tuple[str, str, str, str]] = field(default_factory=set)
    # Days whose SD list this run has seen with recordings, for the calendar.
    seen: set[tuple[str, date]] = field(default_factory=set)
    # One SD session per camera at a time: EZVIZ refuses parallel sessions.
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Reads of one day already on their way: a card that switches days quickly
    # (or a second card) joins the read instead of queueing a session of its own.
    day_tasks: dict[
        tuple[str, date], asyncio.Task[tuple[list[tuple[datetime, datetime]], bool]]
    ] = field(default_factory=dict)
    # Downloads in flight, for the card's progress bar: stage, video seconds
    # delivered and the interval's length. Written by the download thread.
    progress: dict[tuple[str, str, str, str], dict[str, Any]] = field(default_factory=dict)

    def lock(self, serial: str) -> asyncio.Lock:
        """The lock that serialises SD requests to one camera."""
        return self.locks.setdefault(serial, asyncio.Lock())


DATA_RECORDINGS = f"{DOMAIN}_recordings"
DATA_RECORDINGS_LOCK = f"{DOMAIN}_recordings_lock"
DATA_RECORDINGS_TIMER = f"{DOMAIN}_recordings_timer"
# The calendar's month scan survives restarts in Home Assistant's storage.
DATA_DAYS = f"{DOMAIN}_recording_days"
DATA_DAYS_LOCK = f"{DOMAIN}_recording_days_lock"
DATA_DAYS_STORE = f"{DOMAIN}_recording_days_store"
DAYS_STORAGE_KEY = f"{DOMAIN}.recording_days"
DAYS_STORAGE_VERSION = 1
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


def _clip_name(key: tuple[str, str, str, str]) -> str:
    """The MP4's file name: serial, interval and format."""
    serial, begin, end, fmt = key
    return f"{serial}_{begin}_{end}_{fmt}.mp4"


def _clip_folder(tz: tzinfo, begin: str) -> Path:
    """The folder of a recording: the year, month and local day it started on."""
    return Path(parse_stamp(begin).astimezone(tz).strftime("%Y/%m/%d"))


def _clip_path(store: RecordingStore, key: tuple[str, str, str, str]) -> Path:
    """Where a new copy of the recording goes: media/ezviz_rtsp/YYYY/MM/DD."""
    return store.root / _clip_folder(store.tz, key[1]) / _clip_name(key)


def _prepare_store(root: Path, tz: tzinfo) -> RecordingStore:
    """Make the media folder and index the MP4s already in it. Blocking.

    Clips live in year/month/day folders; a file saved flat by an older version
    is moved into its day's folder once, then indexed like the rest. Runs in the
    executor: the folder can hold thousands of clips after a long retention, so
    neither the mkdir nor the walk may run in the event loop.
    """
    root.mkdir(parents=True, exist_ok=True)
    store = RecordingStore(root, tz)
    try:
        files = sorted(root.rglob("*.mp4"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return store
    for path in files:
        key = _key_from_name(path.name)
        if key is None:
            continue
        if path.parent == root:
            target = root / _clip_folder(tz, key[1]) / path.name
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                path.rename(target)
                path = target
            except OSError:
                pass
        store.clips[key] = path
    return store


def _saved_clip(store: RecordingStore, key: tuple[str, str, str, str]) -> Path | None:
    """A copy of the recording already in the media folder. Blocking.

    The requested format wins. An H.264 copy also answers an HEVC request:
    H.264 plays in every browser, so fetching or converting the same recording
    again would only wake the camera and read its SD card for nothing. The
    reverse is not done — an HEVC file does not play everywhere. The old flat
    layout is checked too, so clips saved before the day folders still count.
    """
    serial, begin, end, fmt = key
    names = [_clip_name(key)]
    if fmt == "hevc":
        names.append(f"{serial}_{begin}_{end}_h264.mp4")
    folder = store.root / _clip_folder(store.tz, begin)
    for base in (folder, store.root):
        for name in names:
            path = base / name
            if path.is_file():
                return path
    return None


def temp_root(hass: HomeAssistant) -> Path:
    """The folder that holds clips watched with "save watched recordings" off."""
    return Path(tempfile.gettempdir()) / DOMAIN


def _sweep_temp(root: Path) -> None:
    """Remove temp clips older than their short life. Blocking; never fails."""
    cutoff = time.time() - TEMP_CLIP_SECONDS
    try:
        files = list(root.glob("*.mp4"))
    except OSError:
        return
    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _prepare_temp_root(root: Path) -> Path:
    """Make the temp folder, drop what has aged out, and return it. Blocking."""
    root.mkdir(parents=True, exist_ok=True)
    _sweep_temp(root)
    return root


def _saved_temp(root: Path, key: tuple[str, str, str, str]) -> Path | None:
    """A temp copy of the recording from a play in the last couple of hours."""
    serial, begin, end, fmt = key
    path = root / f"{serial}_{begin}_{end}_{fmt}.mp4"
    if path.is_file():
        return path
    if fmt == "hevc":
        fallback = root / f"{serial}_{begin}_{end}_h264.mp4"
        if fallback.is_file():
            return fallback
    return None


def _mp4_seconds(path: Path) -> float | None:
    """The movie duration recorded in an MP4's ``mvhd`` box, in seconds.

    A clip saved by a version whose fixed transfer window cut long recordings
    would otherwise be served from media as if it were whole. The movie header
    sits near the start of files written with ``+faststart``; the rest of the
    file is skipped by box size and never read.
    """
    try:
        with path.open("rb") as file:
            end = path.stat().st_size
            pos = 0
            while pos + 8 <= end:
                file.seek(pos)
                head = file.read(16)
                if len(head) < 8:
                    return None
                size = int.from_bytes(head[0:4], "big")
                kind = head[4:8]
                header = 8
                if size == 1:
                    if len(head) < 16:
                        return None
                    size = int.from_bytes(head[8:16], "big")
                    header = 16
                elif size == 0:
                    size = end - pos
                if size < header or pos + size > end:
                    return None
                if kind == b"moov":
                    file.seek(pos + header)
                    return _mvhd_seconds(file.read(min(size - header, _MOOV_MAX_BYTES)))
                pos += size
    except OSError:
        return None
    return None


def _mvhd_seconds(payload: bytes) -> float | None:
    """The duration in an MP4 ``moov`` payload, from its ``mvhd`` child."""
    pos = 0
    while pos + 8 <= len(payload):
        size = int.from_bytes(payload[pos : pos + 4], "big")
        kind = payload[pos + 4 : pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > len(payload):
                return None
            size = int.from_bytes(payload[pos + 8 : pos + 16], "big")
            header = 16
        elif size == 0:
            size = len(payload) - pos
        if size < header or pos + size > len(payload):
            return None
        if kind == b"mvhd":
            body = payload[pos + header : pos + size]
            if len(body) < 20:
                return None
            if body[0] == 1:
                if len(body) < 32:
                    return None
                timescale = int.from_bytes(body[20:24], "big")
                duration = int.from_bytes(body[24:32], "big")
            else:
                timescale = int.from_bytes(body[12:16], "big")
                duration = int.from_bytes(body[16:20], "big")
            return duration / timescale if timescale else None
        pos += size
    return None


def _clip_is_short(path: Path, key: tuple[str, str, str, str]) -> bool:
    """Whether a saved MP4 is clearly shorter than the interval it stands for."""
    seconds = _mp4_seconds(path)
    if seconds is None:
        return False
    wanted = (parse_stamp(key[2]) - parse_stamp(key[1])).total_seconds()
    return _short_by(seconds, wanted) > 0


def _remove_quietly(path: Path) -> None:
    """Delete a file, tolerating one that is already gone."""
    with contextlib.suppress(OSError):
        path.unlink()


def _replace_clip(part: Path, target: Path) -> None:
    """Put a finished MP4 in place, so no reader ever sees a partial file."""
    try:
        part.replace(target)
    except OSError:
        _remove_quietly(part)
        raise


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
        store = await hass.async_add_executor_job(
            _prepare_store, media_root(hass), ZoneInfo(hass.config.time_zone)
        )
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
    for path in root.rglob("*.mp4"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path.name)
                # Leave no empty year/month/day folders behind.
                _drop_empty_dirs(path.parent, root)
        except OSError:
            pass
    return removed


def _drop_empty_dirs(folder: Path, root: Path) -> None:
    """Remove the folders a deleted clip left empty, up to the media folder."""
    while folder != root and folder != folder.parent:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _ensure_parent(path: Path) -> None:
    """Make the clip's folder (a day folder, or the temp folder). Blocking."""
    path.parent.mkdir(parents=True, exist_ok=True)


async def async_prune_recordings(hass: HomeAssistant) -> int:
    """Drop the downloaded clips past their retention; returns how many went."""
    store = await async_get_store(hass)
    removed = await hass.async_add_executor_job(_prune, store.root, _keep_days(hass))
    for name in removed:
        key = _key_from_name(name)
        if key is not None:
            store.clips.pop(key, None)
    # Clips watched with saving off are only kept for a couple of hours.
    await hass.async_add_executor_job(_sweep_temp, temp_root(hass))
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
            # The index answers 2004 when there are no recordings at all in the
            # window — a new day before the first event, or a quiet stretch
            # after the last one. That is an empty result, not a failure.
            if "2004" in str(err):
                return sorted(found), True
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
            if cached.recordings:
                store.seen.add((camera.serial, day))
            return cached.recordings, cached.complete
    if day > now.date():
        return [], True
    # Switching days quickly used to queue one SD session per tap behind the
    # camera lock. Everyone waiting for the same day shares the read that is
    # already on its way.
    task = store.day_tasks.get(key)
    if task is not None and task.done():
        # It finished before its callback cleared the map: do not hand a stale
        # result (or a stale error) to the caller, start a fresh read.
        store.day_tasks.pop(key, None)
        task = None
    if task is None:
        task = hass.async_create_task(
            _read_day(hass, store, camera, day, tz, now),
            f"ezviz_rtsp read {camera.serial} {day}",
        )
        store.day_tasks[key] = task

        def _forget(finished: asyncio.Task, key: tuple[str, date] = key) -> None:
            if store.day_tasks.get(key) is finished:
                store.day_tasks.pop(key, None)
            # No one is left to take the error when every waiter went away.
            with contextlib.suppress(BaseException):
                finished.exception()

        task.add_done_callback(_forget)
    return await asyncio.shield(task)


async def _read_day(
    hass: HomeAssistant,
    store: RecordingStore,
    camera: Camera,
    day: date,
    tz: ZoneInfo,
    now: datetime,
) -> tuple[list[tuple[datetime, datetime]], bool]:
    """Read one local day from the SD card and cache it. Runs as a shared task."""
    async with store.lock(camera.serial):
        recordings, complete = await hass.async_add_executor_job(
            search_day, camera.coordinator.ezviz_client, camera.serial, day, tz, now
        )
    store.days[(camera.serial, day)] = _Day(recordings, time.monotonic(), complete)
    if recordings:
        store.seen.add((camera.serial, day))
    return recordings, complete


# -- the calendar's days -----------------------------------------------------------


def _local_days(
    store: RecordingStore, serial: str, year: int, month: int, tz: ZoneInfo
) -> set[date]:
    """Days of a month that already have a saved MP4, in the camera's local time."""
    days: set[date] = set()
    for (clip_serial, begin, _end, _fmt) in store.clips:
        if clip_serial != serial:
            continue
        moment = parse_stamp(begin).astimezone(tz)
        if moment.year == year and moment.month == month:
            days.add(moment.date())
    return days


def day_has_records(
    client: Any, serial: str, day: date, tz: ZoneInfo, now: datetime
) -> bool:
    """Whether the SD index has a recording on ``day``. Blocking.

    One record is enough for a calendar dot, so this asks for a single result
    instead of paging a whole day. A sleeping camera's first search is retried
    like the day list's.
    """
    from pyezvizapi.exceptions import PyEzvizError

    start = datetime.combine(day, datetime.min.time(), tz)
    end = min(start + timedelta(days=1), now.astimezone(tz).replace(microsecond=0))
    if start >= end:
        return False
    retried = False
    while True:
        try:
            answer = client.search_records_v2(
                serial,
                CHANNEL,
                start.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"),
                size=1,
            )
        except PyEzvizError as err:
            # 2004 here means the window holds no recordings at all.
            if "2004" in str(err):
                return False
            if "2009" not in str(err) or retried:
                raise RecordingError(_explain(err)) from err
            retried = True
            time.sleep(WAKE_RETRY_SECONDS)
            continue
        records, _ = decode_records(answer)
        for record in records:
            if not isinstance(record, Mapping):
                continue
            begin = _local(record.get("B"), tz)
            if begin is not None and begin.date() == day:
                return True
        return False


def scan_month_days(
    client: Any,
    serial: str,
    first: date,
    last: date,
    tz: ZoneInfo,
    now: datetime,
    skip: set[date],
) -> tuple[set[date], bool]:
    """Which days of a month have SD recordings; one search per day. Blocking.

    ``skip`` holds days already known from media (their dot is green anyway), so
    the camera is not asked about them. The scan stops at its time budget and
    reports itself incomplete rather than making a long battery-camera session.
    """
    found: set[date] = set()
    deadline = time.monotonic() + MONTH_SCAN_SECONDS
    day = first
    while day <= last:
        if day not in skip:
            if time.monotonic() >= deadline:
                return found, False
            if day_has_records(client, serial, day, tz, now):
                found.add(day)
        day += timedelta(days=1)
    return found, True


async def _days_cache(hass: HomeAssistant) -> tuple[dict[str, Any], Store]:
    """The month-scan cache, loaded from Home Assistant's storage once."""
    if (data := hass.data.get(DATA_DAYS)) is not None:
        return data, hass.data[DATA_DAYS_STORE]
    lock: asyncio.Lock = hass.data.setdefault(DATA_DAYS_LOCK, asyncio.Lock())
    async with lock:
        if (data := hass.data.get(DATA_DAYS)) is not None:
            return data, hass.data[DATA_DAYS_STORE]
        storage: Store = Store(hass, DAYS_STORAGE_VERSION, DAYS_STORAGE_KEY)
        data = await storage.async_load() or {}
        hass.data[DATA_DAYS] = data
        hass.data[DATA_DAYS_STORE] = storage
        return data, storage


async def async_month_days(
    hass: HomeAssistant, camera: Camera, year: int, month: int, *, scan: bool
) -> dict[str, Any]:
    """The days of one month that have local clips and SD recordings.

    Local days come from the media folder and are free; SD days come from the
    persistent cache and from days already looked at this run. ``scan`` asks the
    camera about the month and saves the answer, so it survives a restart.
    """
    store = await async_get_store(hass)
    tz = ZoneInfo(hass.config.time_zone)
    now = datetime.now(tz)
    local = _local_days(store, camera.serial, year, month, tz)
    seen = {
        day
        for (clip_serial, day) in store.seen
        if clip_serial == camera.serial and day.year == year and day.month == month
    }
    month_key = f"{year:04d}-{month:02d}"
    data, storage = await _days_cache(hass)
    cached = (data.get(camera.serial) or {}).get(month_key)
    if scan:
        first = date(year, month, 1)
        after = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        last = min(after - timedelta(days=1), now.date())
        if first <= last:
            from .live import find_stream

            async with store.lock(camera.serial):
                live = find_stream(hass, camera.serial)
                if live is not None and live.active:
                    raise RecordingError(
                        "The camera is streaming live; stop the live view first"
                    )
                found, complete = await hass.async_add_executor_job(
                    scan_month_days,
                    camera.coordinator.ezviz_client,
                    camera.serial,
                    first,
                    last,
                    tz,
                    now,
                    local,
                )
        else:
            found, complete = set(), True
        # An incomplete scan keeps the days found by earlier ones.
        old: set[date] = set()
        for text in (cached or {}).get("days", []):
            with contextlib.suppress(ValueError):
                old.add(date.fromisoformat(text))
        days = found if complete else old | found
        entry = {
            "days": sorted(day.isoformat() for day in days),
            "scanned": now.isoformat(),
            "complete": complete,
        }
        async with hass.data[DATA_DAYS_LOCK]:
            data.setdefault(camera.serial, {})[month_key] = entry
            await storage.async_save(data)
        cached = entry
    card = set(seen)
    for text in (cached or {}).get("days", []):
        with contextlib.suppress(ValueError):
            card.add(date.fromisoformat(text))
    return {
        "local": sorted(day.isoformat() for day in local),
        "card": sorted(day.isoformat() for day in card),
        "scanned": cached.get("scanned") if cached else None,
        "complete": bool(cached.get("complete")) if cached else False,
    }


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


def _open_playback(
    client: Any, serial: str, begin: str, end: str, timezone: str, deadline: float
) -> Any:
    """Open the SD playback stream, waiting out a camera that is busy (5405).

    EZVIZ answers a request with 5405 while another viewer holds the camera, and
    the relay takes a moment to let go after that session ends — most often the
    live view the card just stopped. Retrying a couple of times turns that into
    a short wait instead of an error the user has to act on.
    """
    from pyezvizapi.exceptions import EzvizPlaybackNegotiationError
    from pyezvizapi.streaming.cloud.playback import open_cloud_playback_stream

    last: EzvizPlaybackNegotiationError | None = None
    for attempt in range(1, PLAYBACK_ATTEMPTS + 1):
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
        except BaseException:
            with contextlib.suppress(OSError):
                stream.close()
            raise
        if info.result in (None, 0):
            return stream
        last = EzvizPlaybackNegotiationError(info.result)
        with contextlib.suppress(OSError):
            stream.close()
        if info.result != BUSY_RESULT_CODE or attempt >= PLAYBACK_ATTEMPTS:
            break
        _LOGGER.debug(
            "Camera %s is busy with another viewer (code %s); retrying in %.0f s",
            serial,
            info.result,
            PLAYBACK_RETRY_SECONDS,
        )
        time.sleep(PLAYBACK_RETRY_SECONDS)
    assert last is not None
    raise last


def _transfer_budget(seconds: float) -> float:
    """How long the relay may take to deliver a recording of ``seconds``.

    The relay reads the camera's SD card at a finite speed, so the old fixed
    window cut long recordings: a clip that took longer than the window arrived
    half-downloaded. The window now grows with the interval, with a minimum that
    covers a camera waking up and a cap for the longest clip allowed.
    """
    wanted = seconds * CLIP_TRANSFER_RATE + CLIP_TRANSFER_GRACE
    return min(max(CLIP_DEADLINE, wanted), MAX_TRANSFER_SECONDS)


_PES_TAIL = 17  # a start code split across packets, plus its 14-byte header


def _pes_pts(header: bytes) -> int | None:
    """The 33-bit PTS of a video PES header, or None when it carries none."""
    if len(header) < 14 or header[:3] != b"\x00\x00\x01" or not 0xE0 <= header[3] <= 0xEF:
        return None
    if not header[7] & 0x80:  # PTS_DTS_flags: 10 or 11 (a PTS is present)
        return None
    return (
        ((header[9] & 0x0E) << 29)
        | (header[10] << 22)
        | ((header[11] & 0xFE) << 14)
        | (header[12] << 7)
        | ((header[13] & 0xFE) >> 1)
    )


class _VideoPts:
    """Follow the video presentation time of a PS stream as packets arrive.

    The relay sends MPEG-PS in chunks that do not line up with its PES packets,
    so a small tail is carried over to complete a start code and its header. The
    timestamps stay clear even when the video bodies are encrypted.
    """

    def __init__(self) -> None:
        self.first: int | None = None
        self.last: int | None = None
        self._tail = b""

    def feed(self, body: bytes) -> None:
        data = self._tail + body
        start = 0
        while True:
            index = data.find(b"\x00\x00\x01", start)
            if index < 0 or index + 14 > len(data):
                break
            pts = _pes_pts(data[index : index + 14])
            if pts is not None:
                if self.first is None:
                    self.first = pts
                if self.last is not None and pts + (1 << 32) < self.last:
                    pts += 1 << 33  # the 33-bit clock wrapped
                if self.last is None or pts > self.last:
                    self.last = pts
            start = index + 4
        self._tail = data[-_PES_TAIL:]

    @property
    def seconds(self) -> float | None:
        """Video seconds covered so far, or None before two timestamps."""
        if self.first is None or self.last is None or self.last < self.first:
            return None
        return (self.last - self.first) / 90_000


def _read_playback(
    stream: Any, deadline: float, on_progress: Callable[[float | None], None] | None = None
) -> bytearray:
    """Read one playback stream to its end (or until ``deadline``). Blocking."""
    from pyezvizapi.streaming.cloud.protocol import VtmStreamEOF, VtmStreamTimeout

    data = bytearray()
    pts = _VideoPts()

    def take(**limits: Any) -> int:
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
            if on_progress is not None:
                pts.feed(packet.body)
                on_progress(pts.seconds)
            if len(data) > CLIP_MAX_BYTES:
                raise RecordingError("The recording is too large to play here")
        return taken

    try:
        # A sleeping camera takes a while to send its first packet: wait for it
        # against the whole deadline. pyezvizapi's idle timer runs from the start
        # of iteration, so it only applies afterwards, when IDLE seconds without
        # data mean the clip is over.
        if take(first_packet_deadline=deadline, max_packets=1):
            take(stream_packet_timeout_seconds=CLIP_IDLE)
    except VtmStreamEOF:
        pass
    except VtmStreamTimeout:
        if not data:
            raise
    return data


def _delivered_seconds(data: bytes) -> float | None:
    """The video PTS span of a captured MPEG-PS clip, when it can be measured."""
    from pyezvizapi.streaming.media.mpegps import mpeg_ps_video_pts_span_seconds

    return mpeg_ps_video_pts_span_seconds(data, max_gap_seconds=2.0)


def _short_by(span: float, wanted: float) -> float:
    """How much of the interval the camera did not send, within a tolerance.

    The SD index and the file it names can differ by a fraction of a second (the
    camera often closes the file a little early), so only a real shortfall is
    worth another fetch: at least ``CLIP_SHORTFALL`` seconds or 5% of the clip.
    """
    missing = wanted - span
    allowed = max(CLIP_SHORTFALL, wanted * 0.05)
    return missing if missing > allowed else 0.0


def _download_clip(
    client: Any,
    serial: str,
    begin: str,
    end: str,
    timezone: str,
    code: str | None,
    progress: Callable[[float | None, float, int], None] | None = None,
) -> bytes:
    """One SD interval as MPEG-PS through the cloud relay. Blocking.

    pyezvizapi's own ``save_clip`` refuses a clip whose video falls a fraction
    of a second short of the requested range (the camera often closes the file
    early), so this reads the relay directly and keeps whatever arrived. A clip
    that arrives clearly shorter than the interval is fetched once more, because
    a short transfer usually means the relay stalled rather than a short file.
    """
    from pyezvizapi.exceptions import EzvizPlaybackNegotiationError, PyEzvizError
    from pyezvizapi.streaming.cloud.protocol import decrypt_hikvision_ps_video

    wanted = (parse_stamp(end) - parse_stamp(begin)).total_seconds()
    budget = _transfer_budget(wanted)
    data: bytearray | None = None
    span: float | None = None
    try:
        for attempt in range(1, CLIP_TRANSFER_ATTEMPTS + 1):
            started = time.monotonic()
            deadline = started + budget
            stream = _open_playback(client, serial, begin, end, timezone, deadline)

            def report(
                delivered: float | None, attempt: int = attempt, wanted: float = wanted
            ) -> None:
                if progress is not None:
                    progress(delivered, wanted, attempt)

            try:
                data = _read_playback(stream, deadline, report)
            finally:
                with contextlib.suppress(OSError):
                    stream.close()
            span = _delivered_seconds(bytes(data))
            report(span)
            missing = 0.0 if span is None else _short_by(span, wanted)
            _LOGGER.debug(
                "Recording %s %s-%s: %.1f of %.1f s from the camera in %.1f s (attempt %d)",
                serial,
                begin,
                end,
                span or 0.0,
                wanted,
                time.monotonic() - started,
                attempt,
            )
            if not missing:
                break
            if attempt >= CLIP_TRANSFER_ATTEMPTS:
                _LOGGER.warning(
                    "Recording %s %s-%s: the camera sent %.1f s of the %.1f s in the list",
                    serial,
                    begin,
                    end,
                    span or 0.0,
                    wanted,
                )
                break
            budget = min(budget * 2, MAX_TRANSFER_SECONDS)
            _LOGGER.debug(
                "Recording %s %s-%s: %.1f s short; fetching again with a longer window",
                serial,
                begin,
                end,
                missing,
            )
    except EzvizPlaybackNegotiationError as err:
        raise RecordingError(
            f"The camera refused to play the recording (code {err.result_code}); "
            "it is busy with another viewer. Close the EZVIZ app or the live view "
            "elsewhere, then try again."
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


def _validate_clip(begin: datetime, end: datetime, fmt: str) -> None:
    """Refuse what cannot or should not be played, before waking the camera."""
    if fmt not in FORMATS:
        raise RecordingError(f"Unknown format {fmt!r}")
    if end <= begin:
        raise RecordingError("A recording must end after it begins")
    if (end - begin).total_seconds() > MAX_CLIP_SECONDS:
        raise RecordingError("The recording is too long to play here")


async def _lookup_clip(
    hass: HomeAssistant, store: RecordingStore, key: tuple[str, str, str, str]
) -> Path | None:
    """The clip from the in-memory index or the media folder, if it is there."""
    if (path := store.clips.get(key)) is not None:
        if not await hass.async_add_executor_job(path.is_file):
            # Removed outside the integration (by hand, or by another tool).
            store.clips.pop(key, None)
        elif key in store.short or not await hass.async_add_executor_job(
            _clip_is_short, path, key
        ):
            store.clips.move_to_end(key)
            return path
        else:
            # Saved by a version whose fixed transfer window cut long clips:
            # this file is shorter than the interval, so fetch it again.
            _LOGGER.debug("Clip %s is shorter than its interval; fetching again", path.name)
            await hass.async_add_executor_job(_remove_quietly, path)
            store.clips.pop(key, None)
    # The media folder is the source of truth: a clip saved earlier is served
    # again without touching the camera, even when the in-memory index missed it
    # (a restored backup, a copied file, or an index built before it was written).
    if (path := await hass.async_add_executor_job(_saved_clip, store, key)) is not None:
        if key not in store.short and await hass.async_add_executor_job(
            _clip_is_short, path, key
        ):
            _LOGGER.debug("Clip %s is shorter than its interval; fetching again", path.name)
            await hass.async_add_executor_job(_remove_quietly, path)
            return None
        store.clips[key] = path
        store.clips.move_to_end(key)
        return path
    return None


async def async_clip_states(
    hass: HomeAssistant, serial: str, recordings: list[tuple[datetime, datetime]]
) -> list[tuple[bool, bool]]:
    """For each interval: whether a copy is saved, and whether it is short.

    ``incomplete`` is true when the saved MP4 is clearly shorter than the
    interval the camera's index lists for it — a copy cut off by an interrupted
    download. The card uses it to say that the recording is being fetched again.
    """
    store = await async_get_store(hass)

    def states() -> list[tuple[bool, bool]]:
        result: list[tuple[bool, bool]] = []
        for begin, end in recordings:
            begin_stamp, end_stamp = utc_stamp(begin), utc_stamp(end)
            local = False
            incomplete = False
            for fmt in FORMATS:
                key = (serial, begin_stamp, end_stamp, fmt)
                path = store.clips.get(key)
                if path is not None and path.is_file():
                    local = True
                    if key not in store.short and _clip_is_short(path, key):
                        incomplete = True
            result.append((local, incomplete))
        return result

    return await hass.async_add_executor_job(states)


async def async_get_clip(
    hass: HomeAssistant, camera: Camera, begin: datetime, end: datetime, fmt: str
) -> Path:
    """The MP4 of one recording, from the cache or fetched from the camera now."""
    _validate_clip(begin, end, fmt)
    store = await async_get_store(hass)
    key = (camera.serial, utc_stamp(begin), utc_stamp(end), fmt)
    # A fetch already in flight must win: its target file is not there yet (and
    # a partial one must never be taken for a saved clip).
    if (pending := store.pending.get(key)) is not None:
        return await asyncio.shield(pending)
    if (path := await _lookup_clip(hass, store, key)) is not None:
        return path
    # The check again: another caller may have started the fetch while the
    # (possibly slow) duration check above was running.
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


async def async_get_temp_clip(
    hass: HomeAssistant, camera: Camera, begin: datetime, end: datetime, fmt: str
) -> tuple[Path, bool]:
    """One recording to watch, without keeping it in the media folder.

    Returns ``(path, saved)``. ``saved`` is true when a copy was already in the
    media folder (from an earlier play with saving on, or the download service):
    that copy is served and nothing new is stored. Otherwise the clip is fetched
    into the temp folder, left there for a couple of hours and never listed as
    local or shown under Media.
    """
    _validate_clip(begin, end, fmt)
    store = await async_get_store(hass)
    key = (camera.serial, utc_stamp(begin), utc_stamp(end), fmt)
    if (path := await _lookup_clip(hass, store, key)) is not None:
        return path, True
    root = await hass.async_add_executor_job(_prepare_temp_root, temp_root(hass))
    if (path := await hass.async_add_executor_job(_saved_temp, root, key)) is not None:
        store.temp[key] = path
        return path, False
    if (pending := store.temp_pending.get(key)) is not None:
        return await asyncio.shield(pending), False
    future: asyncio.Future[Path] = asyncio.get_running_loop().create_future()
    store.temp_pending[key] = future
    try:
        path = await _async_fetch(hass, store, camera, key, fmt, keep=False, root=root)
    except BaseException as err:
        future.set_exception(err if isinstance(err, Exception) else RecordingError("Cancelled"))
        future.exception()  # mark retrieved: nobody else may be waiting
        raise
    finally:
        store.temp_pending.pop(key, None)
    future.set_result(path)
    return path, False


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
    *,
    keep: bool = True,
    root: Path | None = None,
) -> Path:
    from .live import ffmpeg_binary, find_stream

    serial, begin, end, _ = key
    live = find_stream(hass, serial)
    if live is not None and live.active:
        raise RecordingError("The camera is streaming live; stop the live view first")
    if keep:
        # Filed by the recording's local day: media/ezviz_rtsp/YYYY/MM/DD.
        target = _clip_path(store, key)
    else:
        target_dir = root or await hass.async_add_executor_job(
            _prepare_temp_root, temp_root(hass)
        )
        target = target_dir / _clip_name(key)
    started = time.monotonic()
    wanted = (parse_stamp(end) - parse_stamp(begin)).total_seconds()
    # The card polls this while the clip is on its way: the camera sends the
    # recording as a PS stream, and the video timestamps it carries say how much
    # of the interval has arrived.
    state: dict[str, Any] = {
        "stage": "queued",
        "delivered": 0.0,
        "total": wanted,
        "started": started,
        "attempt": 1,
    }
    store.progress[key] = state

    def report(delivered: float | None, total: float, attempt: int) -> None:
        current = store.progress.get(key)
        if current is None:
            return
        if delivered is not None:
            current["delivered"] = min(delivered, total)
        current["total"] = total
        current["attempt"] = attempt

    try:
        async with store.lock(serial):
            # A queued request can wait here for another clip, and a live view may
            # have started meanwhile; the camera cannot stream and read its SD card
            # at the same time.
            live = find_stream(hass, serial)
            if live is not None and live.active:
                raise RecordingError("The camera is streaming live; stop the live view first")
            state["stage"] = "downloading"
            ps = await hass.async_add_executor_job(
                _download_clip,
                camera.coordinator.ezviz_client,
                serial,
                begin,
                end,
                hass.config.time_zone,
                camera.code,
                report,
            )
        fetched = time.monotonic()
        state["stage"] = "converting"
        state["delivered"] = wanted
        await hass.async_add_executor_job(_ensure_parent, target)
        # Write beside the target and move it into place when ffmpeg is done, so a
        # reader never sees a half-written MP4.
        part = target.with_name(target.name + ".part")
        try:
            await _async_to_mp4(ffmpeg_binary(hass), ps, fmt, part)
            state["stage"] = "saving"
            await hass.async_add_executor_job(_replace_clip, part, target)
        except BaseException:
            await hass.async_add_executor_job(_remove_quietly, part)
            raise
        _LOGGER.debug(
            "Recording %s %s-%s: %d bytes from the camera in %.1f s, %s MP4 in %.1f s",
            serial, begin, end, len(ps), fetched - started, fmt, time.monotonic() - fetched,
        )
        if keep:
            store.clips[key] = target
            store.clips.move_to_end(key)
            # The camera sometimes sends a little less than the index promised. One
            # real attempt was enough: keep what came back instead of fetching it on
            # every play.
            if await hass.async_add_executor_job(_clip_is_short, target, key):
                store.short.add(key)
                _LOGGER.debug(
                    "Recording %s %s-%s is still shorter than its interval; keeping it",
                    serial,
                    begin,
                    end,
                )
            else:
                store.short.discard(key)
            # Clips are kept until the retention setting ages them out; a download is
            # a good moment to sweep, since the folder is on disk anyway.
            await async_prune_recordings(hass)
        else:
            store.temp[key] = target
        return target
    finally:
        store.progress.pop(key, None)


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
        key = (serial, begin, end, fmt)
        path = store.clips.get(key) or store.temp.get(key)
        if path is None or not path.is_file():
            raise web.HTTPNotFound
        return web.FileResponse(
            path, headers={"Content-Type": "video/mp4", "Cache-Control": "private, max-age=3600"}
        )
