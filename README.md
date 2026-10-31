# Ezviz Cloud with Video

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.10%2B-41BDF5.svg)](https://www.home-assistant.io)

Watch the live video of your EZVIZ cameras on a Home Assistant dashboard,
**including battery cameras and smart peepholes that have no RTSP** (DP2, HP2,
CP2, CP4, …) and cameras with **video encryption turned on**.

The built-in EZVIZ integration plays video over the camera's local `rtsp://`
address. Battery cameras sleep and expose no RTSP server, so they show nothing.
This integration keeps every feature of the built-in one and, for a camera
without RTSP, falls back to the **EZVIZ cloud relay (VTM)** — the same transport
the EZVIZ mobile app uses when you are away from home. The cloud wakes the
camera; Home Assistant reads the MPEG-PS, decrypts it when video encryption is
on, and serves it as the camera entity's stream that any card, notification or
bridge can play.

> This is an **unofficial** project, not affiliated with or endorsed by EZVIZ.

## Domain and coexistence with the built-in integration

The integration uses the domain **`ezviz_rtsp`**, so it does **not** replace or
conflict with the built-in `ezviz` integration. You can run both side by side.
It appears in Home Assistant as **"Ezviz Cloud with Video"**.

## Features

- **Cloud live view** for cameras with no local RTSP: the camera entity
  advertises streaming and serves the EZVIZ cloud stream on demand, only while
  someone watches (plus a short linger). No local RTSP, no open ports.
- **Everything the built-in integration has**: alarm panel, sensors, switches,
  siren, PTZ, image, update, services and the local RTSP camera path.
- **A still on the dashboard without waking the camera**: a cloud camera's
  thumbnail is its last alarm picture, so drawing it costs no battery.
- **Video that just plays**: the cloud stream is HEVC (H.265), so it is
  re-encoded to H.264 by default (`Configure → Video transcoding`). Turn that
  off if your browser or phone already plays H.265.
- **Verification codes per camera** (`Configure → Verification codes`) for
  cameras with video encryption on. The code decrypts the stream inside Home
  Assistant and never reaches a browser or a dashboard.
- **Two-step verification (MFA)**: when your EZVIZ account has 2FA on, EZVIZ
  sends a code by e-mail or SMS and the setup flow asks for it.
- **A stable stream address** guarded by a random token, kept in the config
  entry across restarts, for apps that cannot log in to Home Assistant
  (go2rtc, Frigate).

## Requirements

- Home Assistant 2026.10 or newer.
- An EZVIZ account with the camera added in the EZVIZ app.
- The camera's **verification code** (six capital letters on the label) only if
  video encryption is turned on for that camera.
- Internet on both the camera and Home Assistant: cloud video goes through EZVIZ.

## Installation

### HACS (custom repository)

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/tarasifua/ezviz_rtsp` with category
   **Integration**.
3. Install **Ezviz Cloud with Video** and restart Home Assistant.

### Manual

Copy `custom_components/ezviz_rtsp/` into `config/custom_components/ezviz_rtsp/`
on the Home Assistant machine (Samba, SSH or the *File editor* add-on on HAOS),
then restart Home Assistant.

## Configuration

1. **Settings → Devices & services → Add integration → Ezviz Cloud with Video.**
2. Sign in with the e-mail/phone and password you use in the EZVIZ app and pick
   your region (`Europe`, `Russia`, or a custom URL).
3. If EZVIZ sends a verification code, type it in the next step.
4. Battery cameras show up as a camera entity. The dashboard shows their last
   alarm picture; press it to open the live view.

### Verification codes

Only cameras with video encryption turned on need a code. Open **Configure →
Verification codes**, pick the camera and type the code from its label (leave it
empty to remove a stored code).

### Video transcoding

On by default. Turn it off in **Configure → Video transcoding** if your browser
or phone already plays H.265, to save the Home Assistant host's CPU.

### Other apps (go2rtc, Frigate)

Each camera entity's stream is also served as MPEG-TS at

```
<HOME_ASSISTANT_URL>/api/ezviz_rtsp/live/<serial>?token=<stream_token>
```

where `stream_token` is kept in the integration options. This address wakes the
camera while it is read, so keep sessions short.

## How it works

1. The account session uses [`pyezvizapi`](https://pypi.org/project/pyezvizapi/),
   the private API of the EZVIZ mobile app.
2. When a stream starts, `cloud_stream.LiveSource` opens a VTM cloud session and
   reads the camera's MPEG-PS in a worker thread.
3. `PackSplitter` cuts the stream into frames and skips the group of pictures
   the cloud sends first (it does not decode). With a verification code, the NAL
   bodies are decrypted first.
4. `LiveStream` shares one open stream per camera between Home Assistant's
   stream worker and go2rtc, and closes it a few seconds after the last reader
   leaves.
5. `LiveStreamView` re-packages it to MPEG-TS for the stream worker, transcoding
   to H.264 when asked.

## Troubleshooting

| Symptom | What to check |
|---|---|
| The live view stays black | Transcoding is on by default; if you turned it off, turn it back on, or use a browser/phone that plays H.265. |
| The camera card has no picture but live view works | The still is the camera's last alarm picture; if the camera never recorded one, the card is empty until you open the live view. |
| `Live view of <serial> could not start: …` in the log | The camera did not answer the EZVIZ cloud: check it is online in the app and try again. Some battery cameras refuse a new stream for a few minutes after the last one. |
| A stored code makes the picture garbled | The code is only applied to cameras that encrypt video. Check the code under Configure → Verification codes. |
| `"EZVIZ rejected the account or password"` | Check the e-mail/phone, the password and the **region**. |

## Credits

This project stands on two upstream works. Full license notices are in
[`NOTICE`](NOTICE).

- **Home Assistant Core — EZVIZ integration**
  ([home-assistant/core](https://github.com/home-assistant/core/tree/dev/homeassistant/components/ezviz)),
  licensed under the Apache License 2.0. The integration is derived from it.
  Original author / codeowner: **RenierM26 ([@RenierM26](https://github.com/RenierM26))**.
- **ezviz-cloud-hass**
  ([bvis/ezviz-cloud-hass](https://github.com/bvis/ezviz-cloud-hass)),
  licensed under the MIT License. The cloud (VTM) live-view code is adapted
  from it. Author: **Basilio Vera ([@bvis](https://github.com/bvis))**.
- The brand icon is the EZVIZ brand image published in
  [home-assistant/brands](https://github.com/home-assistant/brands) with an
  added "video" badge.

## License

[Apache License 2.0](LICENSE), with the MIT-licensed parts attributed in
[`NOTICE`](NOTICE).

EZVIZ is a trademark of its owner. This project is not affiliated with,
endorsed by or supported by EZVIZ.
