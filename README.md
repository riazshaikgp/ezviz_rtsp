# Ezviz Cloud with Video

*Live picture from EZVIZ cameras that don't want to give you one — battery
doorbells, peepholes and other sleepy devices.*

![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-2f6f4e)
![Home Assistant 2026.10+](https://img.shields.io/badge/Home_Assistant-2026.10%2B-3d7ff2)

Licensed under **[Apache-2.0](LICENSE)**. Requires **Home Assistant 2026.10 or newer**.

---

## The short version

If you have an EZVIZ battery camera or peephole, you already know the routine:
it works in the EZVIZ app, but Home Assistant shows you nothing. That's not a
bug you can fix with a setting — those devices sleep and never open an RTSP
port, so the local-address approach every Home Assistant integration uses has
nothing to talk to.

This integration talks to them the way the phone app does when you're on
mobile data. It asks EZVIZ's cloud to wake the camera, pulls the stream down,
and hands it to Home Assistant as a normal camera. You get a live view, a
built-in Lovelace card, and the camera stays awake only while you're actually
looking at it.

It is a fork of the **built-in** EZVIZ integration, so everything that one
does still works here. It lives under its own domain, `ezviz_rtsp`, which means
it sits **next to** the built-in one instead of replacing it.

---

## Put it in HACS

[![Open HACS repository in Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=tarasifua&repository=ezviz_rtsp&category=integration)

Or add it by hand:

1. Open **HACS**.
2. Top-right menu (**⋮**) → **Custom repositories**.
3. Repository: `https://github.com/tarasifua/ezviz_rtsp` — Category: **Integration**.
4. Find **Ezviz Cloud with Video** in the list, install it, restart Home Assistant.

<details>
<summary>Bleeding edge (the <code>main</code> branch)</summary>

Releases are the stable channel. To always run the latest committed changes,
open the integration in HACS → **⋮ → Redownload** and pick **`main`** (the
default branch) instead of a release. HACS then offers an update every time the
branch moves — no release needed. Switch back to a release the same way.

</details>

<details>
<summary>No HACS? Copy the folder</summary>

Drop `custom_components/ezviz_rtsp/` into `config/custom_components/ezviz_rtsp/`
on your Home Assistant machine (Samba, SSH, or the *File editor* add-on on
HAOS), then restart.

</details>

---

## First run

Add the integration and sign in with the EZVIZ account from your phone:

[![Start setting up Ezviz Cloud with Video](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ezviz_rtsp)

Pick your region, type the e-mail/phone and password, and — if your account has
two-step verification on — the code EZVIZ sends you. That's it. Your battery
cameras appear as normal camera entities and start showing video.

The account session is stored in the config entry and renewed quietly in the
background, so a restart doesn't make you log in again.

---

## The card that comes with it

The integration serves its own Lovelace card, so there's no resource to add:
just pick **EZVIZ Video** in the card picker or paste the YAML. It is styled
after **Mushroom cards** — a rounded card, an icon tile, a status line and a
soft play button over the picture — but it needs neither Mushroom nor any other
frontend add-on. The card finds a camera by its serial and plays Home
Assistant's own stream.

```yaml
type: custom:ezviz-video-card
serial: BC1234567
title: Front door      # optional; defaults to the camera's name
icon: mdi:doorbell     # optional
auto_play: false       # optional; off by default — the card waits for a tap
max_seconds: 60        # optional; stop the stream to protect the battery (0 = never)
```

| Option | Default | What it does |
|---|---|---|
| `serial` | — | The device serial (on the label or in the EZVIZ app). The visual editor lists your cameras. |
| `entity` | — | Use a camera entity directly instead of looking one up by serial. |
| `title` | camera name | The heading in the card. |
| `icon` | `mdi:video` | The icon in the tile. |
| `auto_play` | `false` | Start a clip as soon as the card is on screen. Off (the default) shows the camera's last picture and waits for a tap, so opening a dashboard never wakes a battery camera. A clip you started keeps running (and resumes) when you change views until its `max_seconds` are up. |
| `max_seconds` | `60` | How long a clip runs before it is stopped automatically, counted **from the first frame** (a battery camera can take a while to wake). `0` never stops it automatically. |

By default the card does nothing until you tap: it shows the camera's **last
alarm picture** with a play button over it. Tapping starts the live view, which
runs for `max_seconds`; a **Stop** chip ends it early. When `max_seconds` runs
out the card quietly returns to the last picture — tap the card again to watch
another clip; nothing has to be reloaded.

**A running clip keeps running.** If you switch to another dashboard view (or
another browser tab) while the picture is playing, the card only pauses the
picture off screen — the stream and its countdown carry on, and it resumes when
you come back, as long as `max_seconds` has not passed. With `auto_play: true`
the card also starts a clip by itself when it first appears; with the default
`false` it waits for a tap. An idle card never wakes the camera.

A **battery camera's charge** is shown in the top-right corner, opposite the
camera name, when the integration exposes it.

The card waits up to 45 seconds for the camera to wake and send its first frame
(a battery camera over the cloud relay is not instant); `max_seconds` only starts
counting once the picture is on screen.

**A battery camera can never be left streaming forever.** Any one of these ends
it:

1. The **card** stops the stream when `max_seconds` runs out, and releases the
   HLS reader.
2. The **integration** closes the cloud session **20 seconds** after the last
   reader leaves (`LINGER` in `live.py`).
3. Even if a client misbehaves entirely, a battery camera is cut off after
   **5 minutes** of one continuous stream (changeable under **Configure →
   Maximum stream duration**); powered cameras are not capped.

Every open and close is written to the Home Assistant log
(`Live view of <serial> is streaming` / `… stopped` / `… hit its N s battery
limit`), so you can confirm in the log that the camera actually went back to
sleep.

There's also a visual editor: the serial becomes a dropdown of your cameras.

---

## The settings worth knowing

Everything below lives under **Configure** on the integration:

- **Video transcoding** — *on by default.* EZVIZ sends H.265, which most
  desktop browsers refuse to decode; this re-encodes the live picture to H.264
  so it just plays. If your browser or phone already handles H.265, turn it off
  to save CPU.
- **Verification codes** — only matters for cameras with *video encryption*
  enabled. Type the six-letter code from the camera's label; it decrypts the
  stream inside Home Assistant and is never sent to a browser.
- **Maximum stream duration** — how long one battery-camera live stream may run
  before the integration stops it, in seconds (default **300**; `0` removes the
  cap). This is the safety net that keeps a battery camera from being left
  streaming, whatever a client does.
- **General** — request timeout and the ffmpeg arguments for cameras you added
  over local RTSP.
- **Stream address for other apps** — see below.

---

## Feeding other apps (Frigate, go2rtc, …)

Every camera can be read as plain MPEG-TS from a local, token-guarded address:

```
http://<your-ha>:8123/api/ezviz_rtsp/live/<serial>?token=<stream token>
```

The token is generated once and kept across restarts, and you can rotate it in
**Configure → Stream address for other apps**. Reading the address wakes the
camera, so treat it like a doorbell: switch it on for a moment, then let it
close.

---

## Under the hood

A quick sketch, for the curious:

```
 camera ──(sleeps)──┐
                    │  EZVIZ cloud wakes it on demand
                    ▼
        EZVIZ VTM relay ──► LiveSource (worker thread, pyezvizapi)
                                    │
                    MPEG-PS frames  │  pack-split, optional H.265/AES decrypt
                                    ▼
                              LiveStream (one per camera, shared)
                                    │
                ┌───────────────────┴────────────────────┐
     LiveStreamView (MPEG-TS,           Home Assistant camera entity
      token-guarded) ──► HA stream       (for any card / bridge /
      worker ──► HLS (┌─ H.264)           mobile notification)
                └────────────────────────┘
                          │
                  the Lovelace card / any camera card
```

The whole stream is opened lazily — the first thing that asks for it starts it,
and it closes a few seconds after the last reader leaves. Nothing polls a
sleeping camera in the background.

---

## Devices

There is no per-model promise. This uses the same cloud path the EZVIZ app
uses, so **it should work with any camera the app can show** — battery cameras,
peepholes, and cameras with video encryption on. A camera that plays in the app
is expected to play here, but only testing will tell.

Verified while building this: **DP2** (battery video doorbell), 2048×1296 H.265
+ AAC through the cloud relay.

If you try another model, an issue with the result is welcome.

---

## If something doesn't play

<details>
<summary>The card / camera stays black</summary>

Transcoding is on by default — if you turned it off, switch it back on, or use
a browser/phone that decodes H.265. Check the log for a line like
`Live view of <serial> could not start: …`; that means the camera didn't answer
the cloud (most often it's asleep or was just streaming).

</details>

<details>
<summary>The dashboard shows a still, not video</summary>

A cloud camera's thumbnail is its **last alarm picture**, on purpose — drawing a
camera on a dashboard must not drain its battery. Tap it to open the live view.

</details>

<details>
<summary>“EZVIZ rejected the account or password”</summary>

Double-check the e-mail/phone, the password, and the **region** your account
belongs to.

</details>

<details>
<summary>A stored code garbles the picture</summary>

A verification code is only applied to cameras that actually encrypt video.
Re-check it under **Configure → Verification codes**.

</details>

---

## Credits

This project would not exist without two upstream works; their notices are in
[`NOTICE`](NOTICE).

- **The EZVIZ integration in Home Assistant Core** — the foundation this is
  forked from. Written and maintained by
  **[RenierM26](https://github.com/RenierM26)** and the Home Assistant
  contributors, under Apache-2.0.
- **[ezviz-cloud-hass](https://github.com/bvis/ezviz-cloud-hass)** — the
  cloud (VTM) live-view idea and code that this draws on. By
  **[Basilio Vera (@bvis)](https://github.com/bvis)**, under MIT.
- Brand icon: the EZVIZ brand image from
  [home-assistant/brands](https://github.com/home-assistant/brands), with a
  small "video" badge.

Licensed under **[Apache-2.0](LICENSE)**, with the MIT-licensed parts attributed
in [`NOTICE`](NOTICE).

EZVIZ is a trademark of its owner. This is an independent, unofficial project
with no affiliation to or endorsement from EZVIZ.

---

## Support

If this project saved you some time, you can buy me a coffee — it keeps the
battery cameras streaming.

[![Buy Me a Coffee at ko-fi.com](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/tarasifua)

[ko-fi.com/tarasifua](https://ko-fi.com/tarasifua)
