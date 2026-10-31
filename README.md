# Ezviz Cloud with Video for Home Assistant

A local fork of Home Assistant's built-in **EZVIZ** integration
([`homeassistant/components/ezviz`](https://github.com/home-assistant/core/tree/dev/homeassistant/components/ezviz))
that adds **live view for cameras with no local RTSP** — battery cameras and
smart peepholes such as the DP2, HP2, CP2 and CP4.

The built-in integration plays video over the camera's local `rtsp://` address.
Battery cameras sleep and expose no RTSP server, so they show nothing. This fork
keeps every feature of the built-in integration and, for a camera without RTSP,
falls back to the **EZVIZ cloud relay (VTM)** — the same transport the EZVIZ
mobile app uses when you are away from home. The cloud wakes the camera; Home
Assistant reads the MPEG-PS, decrypts it when video encryption is on, and serves
it as the camera entity's stream that any card, notification or bridge can play.

## What was added

- **Cloud live view** for cameras without a local RTSP config entry. The camera
  entity advertises `STREAM` and serves the cloud stream on demand; it is only
  open while someone watches (plus a short linger).
- **Verification codes per camera** (`Configure → Verification codes`) for
  cameras with video encryption on. The code decrypts the stream inside Home
  Assistant and never reaches a browser.
- **Optional transcoding**, **on by default** (`Configure → Video transcoding`):
  re-encode the cloud HEVC stream to H.264 so it plays in every browser. It uses
  the Home Assistant host's CPU only while someone watches; turn it off if your
  browser or phone already plays H.265.
- **A still on the dashboard without waking the camera**: a cloud camera's
  thumbnail is its last alarm picture, so drawing it costs no battery.
- **MFA support** at setup and reauthentication: when your EZVIZ account has
  two-step verification on, EZVIZ sends a code by e-mail or SMS and the flow
  asks for it, instead of telling you to disable 2FA.
- **Stable stream address** guarded by a random token, kept in the config entry
  across restarts, for apps that can't log in to Home Assistant (go2rtc,
  Frigate).

Everything else (alarm panel, sensors, switches, siren, PTZ, image, update,
services, the RTSP camera path) is the upstream integration, unchanged.

## Requirements

- Home Assistant 2026.10 or newer (the component copy matches that release).
- An EZVIZ account with the camera added in the EZVIZ app.
- The camera's **verification code** (six capital letters on the label) only if
  video encryption is turned on for that camera.
- Internet on both the camera and Home Assistant: cloud video goes through EZVIZ.

## Installation

This is not published in HACS. Copy the folder into your HA config:

1. Copy `custom_components/ezviz/` into `config/custom_components/ezviz/` on the
   Home Assistant machine (Samba / SSH / *File editor* add-on on HAOS).
2. Restart Home Assistant.

A custom component with the same domain as a built-in integration **overrides**
it, so the built-in EZVIZ integration is replaced by this fork. To go back,
delete the folder and restart.

## Configuration

1. **Settings → Devices & services → Add integration → EZVIZ**, then sign in with
   the e-mail/phone and password you use in the EZVIZ app and pick your region
   (`Europe`, `Russia`, or a custom URL).
2. If EZVIZ sends a verification code, type it in the next step.
3. Battery cameras show up as a camera entity. The dashboard shows their last
   alarm picture; press it to open the live view (transcoding is on by default,
   so it plays in any browser).

### Verification codes

Only cameras with video encryption turned on need a code. Open the integration's
**Configure → Verification codes**, pick the camera and type the code from its
label (leave it empty to remove a stored code).

### Other apps (go2rtc, Frigate)

Each camera entity's stream is also served as MPEG-TS at

```
<HOME_ASSISTANT_URL>/api/ezviz/live/<serial>?token=<stream_token>
```

where `stream_token` is kept in the integration options. This address wakes the
camera while it is read, so keep it for short sessions.

## How it works

1. The account session is the upstream one (`pyezvizapi`, the private app API).
2. When a stream starts, `cloud_stream.LiveSource` opens a VTM cloud session for
   the camera and reads its MPEG-PS in a worker thread.
3. `PackSplitter` cuts the stream into frames and skips the group of pictures the
   cloud sends first (it doesn't decode). With a verification code, the NAL
   bodies are decrypted first.
4. `LiveStream` shares one open stream per camera between HA's stream worker and
   go2rtc, and closes it `LINGER` seconds after the last reader leaves.
5. `LiveStreamView` re-packages it to MPEG-TS for HA's stream worker, optionally
   transcoding to H.264.

## Files added or changed vs. upstream

- `cloud_stream.py` (new): VTM source, packet bridge, splitter, decrypt, ffmpeg.
- `live.py` (new): shared `LiveStream`, `LiveStreamView`, setup helpers.
- `camera.py`: cloud fallback for cameras without RTSP; no thumbnail wake for
  battery cameras.
- `config_flow.py`: an MFA step; an options menu with **General**,
  **Verification codes** and **Video transcoding**.
- `const.py`, `coordinator.py`, `__init__.py`, `strings.json`,
  `translations/en.json`, `manifest.json`: wiring, options and texts.
