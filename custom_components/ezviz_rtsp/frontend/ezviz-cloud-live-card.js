// EZVIZ Cloud with Video — live view card. Served and registered by the
// ezviz_rtsp integration, so there is nothing to add to Lovelace resources.
//
//   type: custom:ezviz-cloud-live-card
//   serial: BC1234567     # device serial; the visual editor lists the account's cameras
//   title: Front door     # optional
//   max_seconds: 60       # optional; stop the stream to save battery, 0 = never
//
// For a cloud camera (battery camera or peephole) the integration relays the
// EZVIZ cloud stream and Home Assistant serves it as HLS. This card plays that
// HLS with the standard camera stream API: no EZVIZ player, no iframe, and the
// stream is stopped again as soon as you want, so a battery camera is awake for
// as short as possible.

const HLS_VERSION = "1.5.20";
const HLS_URL = `https://cdn.jsdelivr.net/npm/hls.js@${HLS_VERSION}/dist/hls.min.js`;
// sha384 of that file; update it with the version.
const HLS_SRI = "sha384-V5ruNBgmYcC3SJRUQeNykAAAgde5gOFq/Hu0CZj7bygDP0yRIhkvX8+w0u/7mRvr";

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
    "starting": "Starting video…",
    "loading": "Loading…",
    "no_video": "No video after {s} s",
    "no_entity": "Camera not found in this account",
    "could_not_start": "Couldn't start the video: {msg}",
    "serial": "Camera",
    "title": "Title",
    "max_seconds": "Stop after (seconds, 0 = never)"
  },
  "uk": {
    "play": "Дивитися наживо",
    "stop": "Зупинити",
    "starting": "Запуск відео…",
    "loading": "Завантаження…",
    "no_video": "Немає відео після {s} с",
    "no_entity": "Камеру не знайдено в цьому акаунті",
    "could_not_start": "Не вдалося запустити відео: {msg}",
    "serial": "Камера",
    "title": "Заголовок",
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

class EzvizCloudLiveCard extends HTMLElement {
  static getStubConfig() {
    return { serial: "" };
  }

  static getConfigElement() {
    return document.createElement("ezviz-cloud-live-card-editor");
  }

  setConfig(config) {
    if (!config.serial && !config.entity) throw new Error("serial is required");
    this._config = { max_seconds: 60, ...config };
    this._stop();
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    // setConfig renders before hass arrives; redraw once the language is known,
    // unless a stream is already on screen.
    const lang = pickLang(hass);
    if (lang !== this._lang && this._config && !this._video) {
      this._render(this._message);
    }
  }

  getCardSize() {
    return 6;
  }

  disconnectedCallback() {
    this._stop();
  }

  _render(message = "") {
    this._lang = this._hass ? pickLang(this._hass) : this._lang || "en";
    this._message = message;
    this._video = null;
    this._hls = null;
    if (!this._config) return;

    const card = document.createElement("ha-card");
    card.style.overflow = "hidden";
    if (this._config.title) {
      const head = document.createElement("div");
      head.style.cssText = "padding:8px 12px 0;font-weight:500";
      head.textContent = this._config.title;
      card.appendChild(head);
    }

    const box = document.createElement("div");
    box.style.cssText =
      "position:relative;aspect-ratio:16/9;background:#000;display:flex;align-items:center;justify-content:center;color:#ddd";
    const label = document.createElement("div");
    label.style.cssText = "display:flex;flex-direction:column;gap:10px;align-items:center";
    const text = document.createElement("div");
    text.style.cssText = "font-size:14px;opacity:.85;text-align:center;padding:0 12px";
    text.textContent = message || t(this._lang, "loading");
    label.appendChild(text);
    if (message) {
      const again = document.createElement("button");
      again.textContent = t(this._lang, "play");
      again.style.cssText = "font:inherit;padding:6px 14px;border:0;border-radius:16px;cursor:pointer;background:var(--primary-color,#03a9f4);color:#fff";
      again.addEventListener("click", () => this._play());
      label.appendChild(again);
    }
    box.appendChild(label);
    card.appendChild(box);
    this._box = box;
    this.innerHTML = "";
    this.appendChild(card);

    if (!message && this.isConnected) this._play();
  }

  _show(key) {
    const box = this._box;
    if (!box) return;
    box.textContent = "";
    const wrap = document.createElement("div");
    wrap.style.cssText = "display:flex;flex-direction:column;gap:10px;align-items:center";
    const text = document.createElement("div");
    text.textContent = t(this._lang, key);
    text.style.cssText = "font-size:14px;opacity:.85";
    const bar = document.createElement("div");
    bar.style.cssText = "width:120px;height:4px;border-radius:2px;background:#333;overflow:hidden";
    const fill = document.createElement("div");
    fill.style.cssText = "height:100%;width:35%;background:var(--primary-color,#03a9f4)";
    bar.appendChild(fill);
    wrap.append(text, bar);
    box.appendChild(wrap);
  }

  _fail(message) {
    this._stop();
    this._render(message);
  }

  async _play() {
    this._show("starting");
    const lang = this._lang;
    let entity = this._config.entity;
    if (!entity) {
      try {
        const devices = await this._hass.callWS({ type: "ezviz_rtsp/devices" });
        entity = devices.find((d) => d.serial === this._config.serial)?.entity_id;
      } catch (err) {
        this._fail(t(lang, "could_not_start", { msg: err.message || err.code || err }));
        return;
      }
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
      this._fail(t(lang, "could_not_start", { msg: err.message || err.code || err }));
      return;
    }
    if (!this.isConnected || !this._box) return;

    const video = document.createElement("video");
    video.muted = true;
    video.autoplay = true;
    video.playsInline = true;
    video.controls = true;
    video.style.cssText =
      "width:100%;height:100%;position:absolute;inset:0;object-fit:contain;background:#000";
    this._video = video;
    this._box.textContent = "";
    this._box.appendChild(video);

    const stop = document.createElement("button");
    stop.textContent = `■ ${t(lang, "stop")}`;
    stop.style.cssText =
      "position:absolute;right:8px;bottom:8px;z-index:1;font:inherit;font-size:12px;padding:4px 10px;border:0;border-radius:12px;cursor:pointer;background:rgba(0,0,0,.6);color:#fff";
    stop.addEventListener("click", () => this._stop(true));
    this._box.appendChild(stop);

    const url = new URL(stream.url, window.location.origin).toString();
    let started = false;
    const ready = () => {
      started = true;
      stop.style.opacity = "1";
    };
    video.addEventListener("playing", ready);
    video.addEventListener("loadeddata", ready);

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
    } else {
      try {
        const Hls = await loadHls();
        const hls = new Hls({ liveSyncDurationCount: 3 });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (data.fatal && !started) {
            this._fail(t(lang, "could_not_start", { msg: data.details }));
          }
        });
        hls.loadSource(url);
        hls.attachMedia(video);
        this._hls = hls;
      } catch (err) {
        this._fail(t(lang, "could_not_start", { msg: err.message || err }));
        return;
      }
    }
    video.play().catch(() => {});

    const max = Number(this._config.max_seconds) || 0;
    if (max > 0) {
      this._timer = setTimeout(() => this._stop(true, t(lang, "no_video", { s: max })), max * 1000);
    }
  }

  _stop(render = false, message = "") {
    clearTimeout(this._timer);
    this._timer = null;
    this._hls?.destroy();
    this._hls = null;
    if (this._video) {
      this._video.pause();
      this._video.removeAttribute("src");
      this._video.load?.();
      this._video.remove();
      this._video = null;
    }
    if (render && this._config) this._render(message);
  }
}

class EzvizCloudLiveCardEditor extends HTMLElement {
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
    } catch (err) {
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
      { name: "serial", required: true, selector: { select: { options, mode: "dropdown" } } },
      { name: "title", selector: { text: {} } },
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
        }),
      );
    });
    this.innerHTML = "";
    this.appendChild(form);
  }
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "ezviz-cloud-live-card",
  name: "EZVIZ Cloud live",
  description: "Live view of an EZVIZ camera through the EZVIZ cloud",
});

// The integration loads this file from the page head, so it often runs before
// the frontend swaps window.customElements for its scoped-registry polyfill.
// Define on the current registry now and again on any registry that replaces it
// while the page boots; the frontend re-renders the card once it is defined.
const define = () => {
  if (!window.customElements.get("ezviz-cloud-live-card")) {
    window.customElements.define("ezviz-cloud-live-card", EzvizCloudLiveCard);
  }
  if (!window.customElements.get("ezviz-cloud-live-card-editor")) {
    window.customElements.define("ezviz-cloud-live-card-editor", EzvizCloudLiveCardEditor);
  }
};
define();
const watchRegistry = setInterval(define, 100);
setTimeout(() => clearInterval(watchRegistry), 30000);
