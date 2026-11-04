# Changelog

All notable changes to **Ezviz Cloud with Video** (`ezviz_rtsp`) are listed here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.2.1] — 2026-11-04

A housekeeping release that gets the repository ready for the HACS store.

### Added
- **Integration categories** in the manifest (`security_safety.cameras`,
  `security_safety.doorbells_intercoms`) so the integration is listed correctly
  in the HACS Marketplace.
- The repository is now validated by the **HACS Action** and **hassfest** on
  every push and pull request (and daily), so it keeps meeting the HACS
  requirements.

### Changed
- The `pyezvizapi` requirement is no longer pinned (`>=1.0.5.0`), so it can
  follow the version Home Assistant itself ships.

### Docs
- README corrections: the card is a corner play/stop chip that plays its own
  fast MJPEG stream, transcoding applies to Home Assistant's stream and other
  apps, and the other-apps addresses are opt-in. Plus a larger Ko-fi button.

## [1.2.0] — 2026-11-01

Two new Configure options put the streaming addresses and the post-view release
delay under your control, and a switch decides who may read the addresses.

### Fixed
- Turning the stream addresses for other apps **off** no longer breaks Home
  Assistant's own camera stream: the integration reads the stream with its own
  token, so the HLS path and the camera entity keep working while go2rtc,
  Frigate and friends are blocked.

### Added
- **Stream address for other apps** option (Configure → Stream address for other
  apps): lists each cloud camera's token-guarded MPEG-TS and MJPEG addresses,
  can hand out a **new token** (links shared before then stop working), and has
  an **Allow other apps to read the stream** switch — **off by default** — that
  turns those addresses on. The bundled card keeps working either way: it carries
  its own separate token.
- **Release the camera after** option (Configure → Release the camera after):
  how long the cloud stream is held open after the last viewer leaves, in seconds
  (default **10**; `0` releases the camera at once). A short hold keeps a quick
  re-play from waking a battery camera again.

## [1.1.0] — 2026-11-01

The live view is fast now, the card looks and behaves like a Mushroom card, and a
battery camera can never be left streaming.

### Added
- **Fast live view.** The integration encodes the cloud stream straight to JPEG
  and serves it as live MJPEG (`/api/ezviz_rtsp/mjpeg/<serial>`), so the card's
  picture appears a few seconds after the camera wakes instead of waiting for
  Home Assistant's HLS worker to build segments. HLS stays as the fallback.
- **Battery charge** of a battery camera shown in the card's header, opposite the
  camera name (red below 20 %).
- The card logs its version **and build hash** to the browser console
  (`[ezviz-video-card] loaded v…+hash`) so a running build is easy to confirm.

### Changed
- **The card is a Mushroom-style picture**: a small **play chip in the
  bottom-right corner** replaces the big round button over the middle; the
  **Stop** chip sits in the same corner while watching.
- `auto_play` now defaults to **off**: the card shows the camera's last alarm
  picture and waits for you to press play, so opening a dashboard never wakes a
  battery camera.
- **Leaving the card stops the stream for good** — switching to another dashboard
  view, hiding the tab, or scrolling away. Coming back shows the last picture
  with the play chip (or starts again when `auto_play` is on).
- While the stream wakes, the card keeps the **last picture** on screen with a
  small corner spinner instead of a dark overlay.
- Configure → **General**: the FFmpeg arguments field is relabelled and explained
  (it only applies to cameras added over local RTSP; cloud cameras ignore it).

### Fixed
- **Video actually plays** in Chromium browsers (Brave, Chrome): the card now
  prefers hls.js, like Home Assistant's own player, instead of the browser's
  native HLS path (which answered "maybe" and then failed to demux).
- The card **starts a new clip** after `max_seconds` runs out; previously a
  "stopped after N s" overlay swallowed the tap and a page reload was needed.
- Switching views no longer leaves a **"Stop" button with no picture**: the card
  is removed from the DOM by Lovelace, and it now stops cleanly on a real removal.

### Safety
- A battery camera is stopped when the card leaves the screen, **20 s** after the
  last reader leaves the cloud session (`LINGER`), and at the latest after
  **Maximum stream duration** (`Configure`, default 300 s) — and every open/close
  is written to the log.

## [1.0.9] — 2026-10-31
- The card prints its version in the browser console (read from the module URL,
  so it can never drift from the manifest).

## [1.0.8] — 2026-10-31
- New **Maximum stream duration** option (Configure): how long one battery-camera
  live stream may run before it is stopped, in seconds (default 300; `0` = no cap).

## [1.0.7] — 2026-10-31
- Battery safety: the card stops the stream reliably (leaving the view, a hidden
  tab), the integration closes the cloud session shortly after the last reader,
  and a battery camera is cut off after a hard session cap.

## [1.0.6] — 2026-10-31
- Fix the config-flow option labels that showed raw translation keys.

## [1.0.5] — 2026-10-31
- Cards and fixes on top of the first releases.

## [1.0.0] – [1.0.4] — 2026-10-31
- First releases: EZVIZ cloud (VTM) relay for battery cameras and peepholes that
  expose no local RTSP, a bundled Mushroom-styled Lovelace card, and a
  token-guarded MPEG-TS address for go2rtc, Frigate and friends.

[1.2.1]: https://github.com/tarasifua/ezviz_rtsp/releases/tag/v1.2.1
[1.2.0]: https://github.com/tarasifua/ezviz_rtsp/releases/tag/v1.2.0
[1.1.0]: https://github.com/tarasifua/ezviz_rtsp/releases/tag/v1.1.0
[1.0.9]: https://github.com/tarasifua/ezviz_rtsp/releases/tag/v1.0.9
[1.0.8]: https://github.com/tarasifua/ezviz_rtsp/releases/tag/v1.0.8
[1.0.7]: https://github.com/tarasifua/ezviz_rtsp/releases/tag/v1.0.7