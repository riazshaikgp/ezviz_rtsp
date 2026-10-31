// EZVIZ Cloud with Video — live view card, Mushroom-inspired.
// Served and registered by the ezviz_rtsp integration: there is nothing to add
// to your Lovelace resources.
//
//   type: custom:ezviz-video-card
//   serial: BC1234567      # device serial; the visual editor lists your cameras
//   entity: camera.front   # or point at the camera entity directly
//   title: Front door      # optional; defaults to the camera's name
//   icon: mdi:doorbell     # optional
//   auto_play: false       # optional; play while the card is on screen
//   max_seconds: 60        # optional; stop to save battery, 0 = never
//
// The card plays Home Assistant's own HLS of the camera (the integration relays
// the EZVIZ cloud stream for cameras that expose no local RTSP). It is styled
// after Mushroom cards — rounded card, an icon tile, a media area with a soft
// play button — but needs neither Mushroom nor any other frontend dependency.
//
// To keep a battery camera asleep, the card stops the stream whenever it is not
// on screen (another dashboard view, another browser tab, scrolled away) or
// after max_seconds, and starts it again on return when auto_play is on.

const HLS_VERSION = "1.5.20";
const HLS_URL = `https://cdn.jsdelivr.net/npm/hls.js@${HLS_VERSION}/dist/hls.min.js`;
// sha384 of that file; update it with the version.
const HLS_SRI = "sha384-V5ruNBgmYcC3SJRUQeNykAAAgde5gOFq/Hu0CZj7bygDP0yRIhkvX8+w0u/7mRvr";

// A battery camera takes a while to wake and send its first frame. max_seconds
// counts from the first frame; this is a separate cap on the wait for it.
const STARTUP_SECONDS = 45;

// hls.js is loaded once per page, only when a browser can't play HLS natively.
let hlsLoading = null;
const loadHls = () => {
  if (window.Hls) return Promise.resolve(window.Hls);
  hlsLoading ||= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = HLS_URL;
    script.integrity = HLS_SRI;
    script.crossOrigin = "anonymous";
    script.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error("hls.js")));
    script.onerror = () => reject(new Error("hls.js"));
    document.head.appendChild(script);
  });
  return hlsLoading;
};

const STRINGS = /* translations */ {
  "en": {
    "play": "Watch live",
    "stop": "Stop",
    "starting": "Waking the camera…",
    "loading": "Loading…",
    "live": "Live",
    "idle": "Tap to watch",
    "no_video": "Stopped after {s} s",
    "no_start": "The camera sent no video. It may still be waking up — try again.",
    "no_entity": "Camera not found in this account",
    "could_not_start": "Couldn't start the video: {msg}",
    "serial": "Camera",
    "title": "Title",
    "icon": "Icon",
    "auto_play": "Start automatically",
    "max_seconds": "Stop after (seconds, 0 = never)"
  },
  "uk": {
    "play": "Дивитися наживо",
    "stop": "Зупинити",
    "starting": "Прокидаємо камеру…",
    "loading": "Завантаження…",
    "live": "Наживо",
    "idle": "Торкніться, щоб дивитися",
    "no_video": "Зупинено через {s} с",
    "no_start": "Камера не надіслала відео. Можливо, ще прокидається — спробуйте ще раз.",
    "no_entity": "Камеру не знайдено в цьому акаунті",
    "could_not_start": "Не вдалося запустити відео: {msg}",
    "serial": "Камера",
    "title": "Заголовок",
    "icon": "Іконка",
    "auto_play": "Запускати автоматично",
    "max_seconds": "Зупиняти через (секунд, 0 = ніколи)"
  }
} /* end translations */;

const t = (lang, key, vars) => {
  const dict = STRINGS[lang] || STRINGS.en;
  let text = dict[key] || STRINGS.en[key] || key;
  if (vars) for (const [k, v] of Object.entries(vars)) text = text.replace(`{${k}}`, v);
  return text;
};

const pickLang = (hass) => {
  const lang = hass?.locale?.language || hass?.language || "en";
  return STRINGS[lang] ? lang : STRINGS[lang.split("-")[0]] ? lang.split("-")[0] : "en";
};

const style = /* css */ `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .wrap { display: flex; flex-direction: column; gap: 12px; padding: 12px; }

  .header { display: flex; align-items: center; gap: 12px; }
  .tile {
    flex: 0 0 auto;
    width: 40px; height: 40px;
    border-radius: var(--mushroom-icon-shape, 12px);
    display: flex; align-items: center; justify-content: center;
    color: var(--primary-color);
    background: color-mix(in srgb, var(--primary-color) 16%, transparent);
    --mdc-icon-size: 22px;
  }
  .tile.error { color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 16%, transparent); }
  .info { min-width: 0; flex: 1 1 auto; }
  .primary {
    font-size: 14px; font-weight: 500; line-height: 20px;
    color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .secondary {
    font-size: 12px; line-height: 16px; margin-top: 2px;
    color: var(--secondary-text-color);
    display: flex; align-items: center; gap: 6px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--secondary-text-color); flex: 0 0 auto;
  }
  .dot.on { background: var(--success-color, #43a047); box-shadow: 0 0 0 0 currentColor; animation: pulse 2s infinite; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success-color, #43a047) 60%, transparent); }
    70% { box-shadow: 0 0 0 8px transparent; }
    100% { box-shadow: 0 0 0 0 transparent; }
  }

  .media {
    position: relative; aspect-ratio: 16 / 9; width: 100%;
    border-radius: var(--mushroom-media-radius, 12px);
    overflow: hidden;
    background: var(--secondary-background-color, #111);
  }
  .media video {
    position: absolute; inset: 0; width: 100%; height: 100%;
    object-fit: contain; background: #000; display: block;
  }
  .poster {
    position: absolute; inset: 0;
    background-size: cover; background-position: center;
    background-color: #000;
  }
  .cover {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    border: 0; margin: 0; padding: 0; cursor: pointer; background: transparent;
  }
  .cover:disabled { cursor: default; }
  .play {
    width: 56px; height: 56px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: #fff; --mdc-icon-size: 30px;
    background: color-mix(in srgb, #000 45%, transparent);
    backdrop-filter: blur(4px);
    transition: transform 120ms ease, background 120ms ease;
  }
  .cover:hover .play { transform: scale(1.06); background: color-mix(in srgb, #000 60%, transparent); }

  .message {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    text-align: center; padding: 0 14px; color: #eee; font-size: 13px;
    background: linear-gradient(0deg, rgba(0,0,0,.25), rgba(0,0,0,.25));
  }

  .chip {
    position: absolute; z-index: 2;
    font: inherit; font-size: 12px; line-height: 1;
    display: inline-flex; align-items: center; gap: 6px;
    padding: 7px 12px; border: 0; border-radius: 999px; cursor: pointer;
    background: color-mix(in srgb, #000 55%, transparent); color: #fff;
    backdrop-filter: blur(4px);
  }
  .chip.stop { right: 8px; bottom: 8px; }
  .chip:hover { background: color-mix(in srgb, #000 70%, transparent); }

  .spinner {
    width: 34px; height: 34px; border-radius: 50%;
    border: 3px solid color-mix(in srgb, #fff 25%, transparent);
    border-top-color: #fff;
    animation: spin 900ms linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
`;

class EzvizVideoCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._lang = "en";
    this._state = "idle"; // idle | loading | playing
    this._message = "";
    // Bumped on every stop so a stream that is still opening cannot finish
    // after the card was hidden or reloaded.
    this._gen = 0;
    // Whether the stream was stopped because the card went out of view, so it
    // can be resumed when the card is shown again (only if auto_play is on).
    this._stoppedByVisibility = false;
    // Whether the first frame has reached the screen (max_seconds counts from it).
    this._started = false;
    this._startupTimer = null;
    // A pause the user asked for (video controls) must not be auto-undone.
    this._userPaused = false;
  }

  static getStubConfig() {
    return { serial: "" };
  }

  static getConfigElement() {
    return document.createElement("ezviz-video-card-editor");
  }

  setConfig(config) {
    if (!config || (!config.serial && !config.entity)) {
      throw new Error("ezviz-video-card: 'serial' (or 'entity') is required");
    }
    this._config = { max_seconds: 60, auto_play: true, ...config };
    this._device = undefined;
    this._stop();
    this._state = "idle";
    this._message = "";
    this._render();
    this._maybeAutoPlay();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const lang = pickLang(hass);
    if (first && lang !== this._lang && !this._video) this._lang = lang;
    if (!this._devices) this._loadDevices();
    if (first || lang !== this._lang) {
      this._lang = lang;
      if (!this._video && this._state !== "loading") this._render();
    }
  }

  getCardSize() {
    return 6;
  }

  connectedCallback() {
    this._watchVisibility();
    this._maybeAutoPlay();
  }

  disconnectedCallback() {
    this._unwatchVisibility();
    this._stop();
  }

  // -- visibility ----------------------------------------------------------
  //
  // A battery camera must not be kept awake while nobody is looking, and a live
  // stream must be re-opened when the card returns. Lovelace keeps an inactive
  // view in the DOM, so IntersectionObserver is not reliable here; the card
  // polls its own geometry instead, and reacts at once to a hidden browser tab.

  _watchVisibility() {
    if (!this._visTimer) {
      this._visTimer = setInterval(() => this._syncVisibility(), 1000);
    }
    if (!this._io && "IntersectionObserver" in window) {
      // A second signal for the card scrolled off screen (Lovelace may keep an
      // inactive view laid out, just out of sight).
      this._io = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) this._intersecting = entry.isIntersecting;
          this._syncVisibility();
        },
        { threshold: 0 }
      );
      this._io.observe(this);
    }
    if (!this._onDocVisibility) {
      this._onDocVisibility = () => this._syncVisibility();
      document.addEventListener("visibilitychange", this._onDocVisibility);
    }
    this._syncVisibility();
  }

  _unwatchVisibility() {
    clearInterval(this._visTimer);
    this._visTimer = null;
    this._io?.disconnect();
    this._io = null;
    if (this._onDocVisibility) {
      document.removeEventListener("visibilitychange", this._onDocVisibility);
      this._onDocVisibility = null;
    }
  }

  _isVisible() {
    if (!this.isConnected || document.hidden) return false;
    if (this._intersecting === false) return false;
    const rect = this.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return false;
    const style = getComputedStyle(this);
    return style.display !== "none" && style.visibility !== "hidden";
  }

  _syncVisibility() {
    if (!this._config) return;
    const visible = this._isVisible();
    if (visible !== this._visible) {
      this._visible = visible;
      if (visible) this._onShown();
      else this._onHidden();
      return;
    }
    if (visible) this._ensurePlaying();
  }

  _onHidden() {
    if (this._state !== "playing" && this._state !== "loading") return;
    this._stoppedByVisibility = true;
    // A browser-induced pause must not be mistaken for the user pausing; only a
    // pause while the card is on screen counts as the user's.
    this._userPaused = false;
    this._stop(true, "");
  }

  _onShown() {
    this._userPaused = false;
    if (this._stoppedByVisibility) {
      this._stoppedByVisibility = false;
      if (this._config?.auto_play && this._state === "idle") this._play();
      return;
    }
    // The stream was not stopped (the view was never seen as hidden): a live
    // HLS that the browser paused while away is stale, so start it over.
    if (this._state === "playing" && this._started) this._restart();
  }

  _ensurePlaying() {
    if (this._userPaused || this._state !== "playing" || !this._started) return;
    const video = this._video;
    if (video && video.paused) video.play().catch(() => {});
  }

  _restart() {
    this._stop();
    this._state = "idle";
    this._play();
  }

  // -- data ----------------------------------------------------------------

  async _loadDevices() {
    if (!this._hass) return;
    try {
      this._devices = await this._hass.callWS({ type: "ezviz_rtsp/devices" });
    } catch {
      this._devices = [];
    }
    // Re-fill the header name/icon now that devices are known.
    if (this._state === "idle" && !this._video) this._render();
  }

  _findDevice() {
    if (this._device !== undefined) return this._device;
    if (!this._devices) return null;
    const { serial, entity } = this._config;
    this._device =
      this._devices.find((d) => (serial && d.serial === serial) || (entity && d.entity_id === entity)) ||
      null;
    return this._device;
  }

  _entityId() {
    return this._config.entity || this._findDevice()?.entity_id || null;
  }

  _name() {
    const id = this._entityId();
    const state = id ? this._hass?.states?.[id] : null;
    return (
      this._config.title ||
      state?.attributes?.friendly_name ||
      this._findDevice()?.name ||
      this._config.serial ||
      "EZVIZ"
    );
  }

  _posterUrl() {
    const id = this._entityId();
    if (!id) return "";
    const state = this._hass?.states?.[id];
    const pic = state?.attributes?.entity_picture;
    if (!pic) return "";
    return new URL(pic, window.location.origin).toString();
  }

  // -- rendering -----------------------------------------------------------

  _render() {
    if (!this._config) return;
    const root = this.shadowRoot;
    if (!root) return;

    const card = document.createElement("ha-card");
    const wrap = document.createElement("div");
    wrap.className = "wrap";

    // Header
    const header = document.createElement("div");
    header.className = "header";

    const tile = document.createElement("div");
    tile.className = "tile" + (this._state === "idle" && this._message ? " error" : "");
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", this._config.icon || "mdi:video");
    tile.appendChild(icon);

    const info = document.createElement("div");
    info.className = "info";
    const primary = document.createElement("div");
    primary.className = "primary";
    primary.textContent = this._name();
    const secondary = document.createElement("div");
    secondary.className = "secondary";
    const dot = document.createElement("span");
    dot.className = "dot" + (this._started ? " on" : "");
    const status = document.createElement("span");
    status.textContent = this._statusText();
    secondary.append(dot, status);
    info.append(primary, secondary);
    header.append(tile, info);

    // Media
    const media = document.createElement("div");
    media.className = "media";
    this._media = media;

    const poster = this._posterUrl();
    if (poster) {
      const layer = document.createElement("div");
      layer.className = "poster";
      layer.style.backgroundImage = `url("${poster}")`;
      media.appendChild(layer);
    }

    if (this._state === "playing") {
      media.appendChild(this._video); // re-attach the running element
      media.appendChild(this._stopChip());
    } else if (this._state === "loading") {
      const message = document.createElement("div");
      message.className = "message";
      const spinner = document.createElement("div");
      spinner.className = "spinner";
      message.appendChild(spinner);
      media.appendChild(message);
    } else {
      const cover = document.createElement("button");
      cover.className = "cover";
      cover.title = t(this._lang, this._message ? "play" : "play");
      const play = document.createElement("div");
      play.className = "play";
      const playIcon = document.createElement("ha-icon");
      playIcon.setAttribute("icon", "mdi:play");
      play.appendChild(playIcon);
      cover.appendChild(play);
      cover.addEventListener("click", () => this._play());
      media.appendChild(cover);
      if (this._message) {
        const message = document.createElement("div");
        message.className = "message";
        message.textContent = this._message;
        media.appendChild(message);
      }
    }

    wrap.append(header, media);
    card.appendChild(wrap);
    root.replaceChildren(createStyle(style), card);
    this._header = { tile, primary, secondary, dot, status };
  }

  _statusText() {
    if (this._started) return t(this._lang, "live");
    if (this._state === "playing" || this._state === "loading") {
      return t(this._lang, "starting");
    }
    if (this._message) return this._message;
    return t(this._lang, "idle");
  }

  _stopChip() {
    const stop = document.createElement("button");
    stop.className = "chip stop";
    const stopIcon = document.createElement("ha-icon");
    stopIcon.setAttribute("icon", "mdi:stop");
    stopIcon.style.setProperty("--mdc-icon-size", "14px");
    const label = document.createElement("span");
    label.textContent = t(this._lang, "stop");
    stop.append(stopIcon, label);
    stop.addEventListener("click", (event) => {
      event.stopPropagation();
      this._stop(true);
    });
    return stop;
  }

  // -- playback ------------------------------------------------------------

  _maybeAutoPlay() {
    if (
      this._config &&
      this._config.auto_play &&
      this.isConnected &&
      this._state === "idle" &&
      !this._video
    ) {
      this._play();
    }
  }

  async _play() {
    if (this._state === "loading" || this._state === "playing") return;
    const gen = ++this._gen;
    this._stoppedByVisibility = false;
    this._state = "loading";
    this._message = "";
    this._render();

    const lang = this._lang;
    let entity = this._entityId();
    if (!entity && this._config.serial) {
      if (!this._devices) await this._loadDevices();
      if (gen !== this._gen) return;
      entity = this._findDevice()?.entity_id || null;
    }
    if (!entity) {
      this._fail(t(lang, "no_entity"));
      return;
    }

    let stream;
    try {
      stream = await this._hass.callWS({
        type: "camera/stream",
        entity_id: entity,
        format: "hls",
      });
    } catch (err) {
      if (gen !== this._gen) return;
      this._fail(t(lang, "could_not_start", { msg: err.message || err.code || err }));
      return;
    }
    if (gen !== this._gen || !this.isConnected) return;

    const video = document.createElement("video");
    video.muted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.controls = true;
    // A pause while the card is on screen is the user's (video controls); a
    // pause while it is off screen is the browser's and must be undone.
    video.addEventListener("pause", () => {
      if (this._video === video && this._started && this._state === "playing" && this._isVisible()) {
        this._userPaused = true;
      }
    });
    video.addEventListener("play", () => {
      this._userPaused = false;
    });
    video.addEventListener("canplay", () => {
      if (this._video === video) video.play().catch(() => {});
    });
    this._video = video;
    this._state = "playing";
    this._render();

    const url = new URL(stream.url, window.location.origin).toString();
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
    } else {
      try {
        const Hls = await loadHls();
        if (gen !== this._gen) return;
        const hls = new Hls({ liveSyncDurationCount: 3 });
        let started = false;
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (data.fatal && !started && gen === this._gen) {
            this._fail(t(lang, "could_not_start", { msg: data.details }));
          }
        });
        hls.on(Hls.Events.FRAG_LOADED, () => {
          started = true;
        });
        hls.loadSource(url);
        hls.attachMedia(video);
        this._hls = hls;
      } catch (err) {
        if (gen !== this._gen) return;
        this._fail(t(lang, "could_not_start", { msg: err.message || err }));
        return;
      }
    }

    // The first frame can take a while on a battery camera; only then does the
    // max_seconds countdown start, so a short limit never cuts off the wake-up.
    const onStarted = () => this._onStarted(gen, lang);
    video.addEventListener("playing", onStarted);
    video.addEventListener("loadeddata", onStarted);
    video.play().catch(() => {});
    this._startupTimer = setTimeout(() => {
      if (gen !== this._gen || this._started) return;
      this._fail(t(lang, "no_start"));
    }, STARTUP_SECONDS * 1000);
  }

  _onStarted(gen, lang) {
    if (gen !== this._gen || this._started) return;
    this._started = true;
    clearTimeout(this._startupTimer);
    this._startupTimer = null;
    console.info("[ezviz-video-card] streaming", this._entityId() || this._config.serial);
    if (this._header) {
      this._header.dot.classList.add("on");
      this._header.status.textContent = t(lang, "live");
    }
    const max = Number(this._config.max_seconds) || 0;
    if (max > 0) {
      this._timer = setTimeout(
        () => this._stop(true, t(lang, "no_video", { s: max })),
        max * 1000
      );
    }
  }

  _fail(message) {
    console.warn("[ezviz-video-card]", message);
    this._stop();
    this._state = "idle";
    this._message = message;
    this._render();
  }

  _stop(render = false, message = "") {
    this._gen += 1; // abandon any open that is still in flight
    clearTimeout(this._timer);
    this._timer = null;
    clearTimeout(this._startupTimer);
    this._startupTimer = null;
    this._started = false;
    this._hls?.destroy();
    this._hls = null;
    if (this._video) {
      try {
        this._video.pause();
        this._video.removeAttribute("src");
        this._video.load?.();
      } catch {
        /* ignore */
      }
      this._video.remove();
      this._video = null;
    }
    if (render && this._config) {
      this._state = "idle";
      this._message = message;
      this._render();
    }
  }
}

function createStyle(css) {
  const el = document.createElement("style");
  el.textContent = css;
  return el;
}

class EzvizVideoCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._loadDevices();
  }

  async _loadDevices() {
    if (this._devices || !this._hass) return;
    try {
      this._devices = await this._hass.callWS({ type: "ezviz_rtsp/devices" });
    } catch {
      this._devices = [];
    }
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    const lang = pickLang(this._hass);
    const options = (this._devices || []).map((d) => ({
      value: d.serial,
      label: `${d.name} (${d.serial})${d.battery ? " 🔋" : ""}`,
    }));
    const schema = [
      { name: "serial", selector: { select: { options, mode: "dropdown" } } },
      { name: "title", selector: { text: {} } },
      { name: "icon", selector: { icon: {} } },
      { name: "auto_play", selector: { boolean: {} } },
      {
        name: "max_seconds",
        selector: { number: { min: 0, max: 600, mode: "box", unit_of_measurement: "s" } },
      },
    ];
    const form = document.createElement("ha-form");
    form.hass = this._hass;
    form.data = this._config;
    form.schema = schema;
    form.computeLabel = (field) => t(lang, field.name);
    form.addEventListener("value-changed", (event) => {
      this._config = event.detail.value;
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: this._config },
          bubbles: true,
          composed: true,
        })
      );
    });
    this.replaceChildren(form);
  }
}

// Announce the card to the picker. The frontend may replace window.customCards
// while it boots, so register defensively and re-register if it goes missing.
const CARD_INFO = {
  type: "ezviz-video-card",
  name: "EZVIZ Video",
  description: "Live view of an EZVIZ camera through the EZVIZ cloud",
  preview: true,
};

const registerCard = () => {
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c && c.type === CARD_INFO.type)) {
    window.customCards.push(CARD_INFO);
  }
};

// The integration loads this file from the page head, so it often runs before
// the frontend swaps window.customElements for its scoped-registry polyfill.
// Define on the current registry now and again on any registry that replaces it
// while the page boots; the frontend re-renders the card once it is defined.
const define = () => {
  registerCard();
  if (!window.customElements.get("ezviz-video-card")) {
    window.customElements.define("ezviz-video-card", EzvizVideoCard);
  }
  if (!window.customElements.get("ezviz-video-card-editor")) {
    window.customElements.define("ezviz-video-card-editor", EzvizVideoCardEditor);
  }
};
define();
console.info("[ezviz-video-card] loaded (custom:ezviz-video-card)");
const watchRegistry = setInterval(define, 100);
setTimeout(() => clearInterval(watchRegistry), 30000);
