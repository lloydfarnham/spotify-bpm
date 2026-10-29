// Liked Songs BPM: list, sort and filter liked songs by BPM, and play them via Spotify.
// Data comes from data/tracks.json (exported by export_data.py). Spotify login is
// Authorization Code + PKCE in the browser, so no secret is involved.

const CLIENT_ID = "236a5db22f5a415bae0074616cebefa9";
const REDIRECT_URI = location.origin + location.pathname;
const SCOPES = [
  "user-library-read",
  "user-read-playback-state",
  "user-modify-playback-state",
  "streaming",
  "user-read-email",
  "user-read-private",
  "playlist-read-private",
  "playlist-read-collaborative",
  "playlist-modify-private",
  "playlist-modify-public",
].join(" ");
const API = "https://api.spotify.com/v1";
const QUEUE_SIZE = 50; // tracks sent to Spotify when you press play
const MIN_BPM = 90; // anything slower is shown doubled (half-time feel counted at full tempo)
const IS_MOBILE = /Android|iPhone|iPad|iPod|Mobi/i.test(navigator.userAgent);

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

let tracks = [];      // all songs
let view = [];        // songs after filter + sort
let sdkDeviceId = null;
let sdkPlayer = null;
let nowPlayingId = null;
let isPlaying = false;
let pos = { progress: 0, duration: 0, at: 0 }; // last known position, interpolated between polls
let dragging = false;
let holdUntil = 0; // after a seek/skip, Spotify briefly reports the old state; ignore it until then
const HOLD_MS = 2500;
let volume = null; // device volume 0–100, or null if the device doesn't allow remote volume
let volHoldUntil = 0;
let volPending = null;
let volTimer = null;
const VOLUME_STEP = 5;

// ---------- Spotify auth (PKCE) ----------

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function login() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(64)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  store.set("pkce", { verifier, state });
  const params = new URLSearchParams({
    client_id: CLIENT_ID, response_type: "code", redirect_uri: REDIRECT_URI, scope: SCOPES,
    state, code_challenge_method: "S256", code_challenge: challenge,
  });
  location.href = "https://accounts.spotify.com/authorize?" + params;
}

function logout() {
  store.del("token");
  if (sdkPlayer) sdkPlayer.disconnect();
  location.reload();
}

async function tokenRequest(body) {
  const resp = await fetch("https://accounts.spotify.com/api/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CLIENT_ID, ...body }),
  });
  if (!resp.ok) throw new Error("Spotify login failed (" + resp.status + ")");
  const j = await resp.json();
  const old = store.get("token") || {};
  const token = {
    access: j.access_token,
    refresh: j.refresh_token || old.refresh,
    expires: Date.now() + (j.expires_in - 60) * 1000,
    scope: j.scope || old.scope,
  };
  store.set("token", token);
  return token;
}

async function handleRedirect() {
  const q = new URLSearchParams(location.search);
  if (!q.has("code") && !q.has("error")) return;
  history.replaceState(null, "", REDIRECT_URI);
  const pkce = store.get("pkce");
  store.del("pkce");
  if (q.get("error")) return toast("Spotify login was cancelled.");
  if (!pkce || q.get("state") !== pkce.state) return toast("Login link expired; please try again.");
  try {
    await tokenRequest({ grant_type: "authorization_code", code: q.get("code"), redirect_uri: REDIRECT_URI, code_verifier: pkce.verifier });
  } catch (e) {
    toast(e.message);
  }
}

async function accessToken() {
  let token = store.get("token");
  if (!token) return null;
  if (Date.now() > token.expires) {
    try {
      token = await tokenRequest({ grant_type: "refresh_token", refresh_token: token.refresh });
    } catch {
      store.del("token");
      updateAuthUi();
      return null;
    }
  }
  return token.access;
}

async function api(path, { method = "GET", body, query } = {}) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const token = await accessToken();
    if (!token) throw new Error("Not connected to Spotify");
    const url = (path.startsWith("http") ? path : API + path) + (query ? "?" + new URLSearchParams(query) : "");
    const resp = await fetch(url, {
      method,
      headers: { Authorization: "Bearer " + token, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (resp.status === 401) { const t = store.get("token"); if (t) { t.expires = 0; store.set("token", t); } continue; }
    if (resp.status === 429) { await sleep(1000 * (+resp.headers.get("Retry-After") || 2)); continue; }
    if (resp.status === 204 || resp.status === 202) return null;
    // Player commands can answer 200 with a non-JSON body, so only parse what parses.
    const text = await resp.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch {}
    if (!resp.ok) {
      const err = new Error(data?.error?.message || "Spotify error " + resp.status);
      err.status = resp.status;
      err.reason = data?.error?.reason;
      throw err;
    }
    return data;
  }
  throw new Error("Spotify request failed");
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- data ----------

async function loadData() {
  const resp = await fetch("data/tracks.json", { cache: "no-cache" });
  if (!resp.ok) throw new Error("Couldn't load data/tracks.json (" + resp.status + ")");
  const data = await resp.json();
  tracks = data.tracks.map(prepare);
  $("stats").dataset.generated = data.generated;
}

function liftBpm(bpm) {
  if (bpm == null || bpm <= 0) return bpm;
  while (bpm < MIN_BPM) bpm *= 2;
  return bpm;
}

function prepare(t) {
  t.rawBpm = t.bpm;
  t.bpm = liftBpm(t.bpm);
  if (t.alt != null) t.alt = liftBpm(t.alt);
  t.year = t.released && /^\d{4}/.test(t.released) ? +t.released.slice(0, 4) : null;
  t.artistText = t.artists.join(", ");
  t.haystack = (t.name + " " + t.artistText + " " + (t.album || "")).toLowerCase();
  return t;
}

// Liked songs newer than the export show up as "BPM pending" until the next update.
async function mergeNewLikes() {
  const known = new Set(tracks.map((t) => t.id));
  const fresh = [];
  let url = API + "/me/tracks?limit=50";
  for (let page = 0; url && page < 20; page++) {
    const j = await api(url);
    const items = (j.items || []).filter((i) => i.track && i.track.id);
    const unseen = items.filter((i) => !known.has(i.track.id));
    for (const i of unseen) {
      fresh.push(prepare({
        id: i.track.id, name: i.track.name, artists: i.track.artists.map((a) => a.name),
        album: i.track.album?.name, released: i.track.album?.release_date, added: i.added_at,
        bpm: null, source: "pending", isNew: true,
      }));
    }
    if (unseen.length < items.length) break; // reached songs we already have
    url = j.next;
  }
  if (fresh.length) {
    tracks = fresh.concat(tracks);
    render();
    toast(fresh.length + " newly liked song" + (fresh.length > 1 ? "s" : "") + " added. BPMs arrive with the next update.");
  }
}

// ---------- filter, sort, render ----------

const SORTS = {
  "bpm-asc": (a, b) => nullLast(a.bpm, b.bpm) || a.bpm - b.bpm,
  "bpm-desc": (a, b) => nullLast(a.bpm, b.bpm) || b.bpm - a.bpm,
  "added-desc": (a, b) => (b.added || "").localeCompare(a.added || ""),
  "added-asc": (a, b) => (a.added || "").localeCompare(b.added || ""),
  "name-asc": (a, b) => a.name.localeCompare(b.name),
  "name-desc": (a, b) => b.name.localeCompare(a.name),
  "artist-asc": (a, b) => a.artistText.localeCompare(b.artistText),
  "year-desc": (a, b) => nullLast(a.year, b.year) || b.year - a.year,
  "year-asc": (a, b) => nullLast(a.year, b.year) || a.year - b.year,
};
function nullLast(a, b) { return a == null ? (b == null ? 0 : 1) : b == null ? -1 : 0; }

function inRange(bpm, min, max, octave) {
  const ok = (v) => (min == null || v >= min) && (max == null || v <= max);
  return ok(bpm) || (octave && (ok(bpm * 2) || ok(bpm / 2)));
}

function applyFilters() {
  const q = $("search").value.trim().toLowerCase();
  const min = $("bpm-min").value === "" ? null : +$("bpm-min").value;
  const max = $("bpm-max").value === "" ? null : +$("bpm-max").value;
  const show = $("show").value;
  const octave = $("octave").checked;
  view = tracks.filter((t) => {
    if (q && !t.haystack.includes(q)) return false;
    if (show === "missing") return t.bpm == null;
    if (show === "disputed" && t.alt == null) return false;
    if (show !== "all" && t.bpm == null) return false;
    if (show === "sure" && t.source === "estimate") return false;
    if ((min != null || max != null) && (t.bpm == null || !inRange(t.bpm, min, max, octave))) return false;
    return true;
  });
  view.sort(SORTS[$("sort").value]);
}

const SOURCE_LABEL = {
  override: "set by you", reccobeats: "ReccoBeats", beatport: "Beatport",
  beatport_title: "Beatport (title)", estimate: "estimate", pending: "pending",
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function rowHtml(t, i) {
  const est = t.source === "estimate";
  const bpm = t.bpm == null
    ? `<div class="bpm none">—<small>${t.source === "pending" ? "pending" : "unknown"}</small></div>`
    : `<div class="bpm${est ? " est" : ""}${t.alt != null ? " disputed" : ""}"${t.alt != null
        ? ` title="Preview analysis suggests ≈${Math.round(t.alt)} BPM (${Math.round(t.altConf * 100)}% confident)"` : ""}>` +
      `${est ? "≈" : ""}${Math.round(t.bpm)}${t.alt != null ? `<span class="warn">⚠ ≈${Math.round(t.alt)}</span>` : ""}` +
      `<small>${SOURCE_LABEL[t.source] || ""}${est && t.conf != null ? " " + Math.round(t.conf * 100) + "%" : ""}` +
      `${t.rawBpm != null && t.rawBpm !== t.bpm ? ` <span title="Listed as ${Math.round(t.rawBpm)} BPM">×${Math.round(t.bpm / t.rawBpm)}</span>` : ""}</small></div>`;
  const bpmCell = canEdit
    ? `<button class="bpm-btn" data-edit="${i}" title="Click to correct this BPM">${bpm}</button>`
    : bpm;
  return `<li class="row${t.id === nowPlayingId ? " playing" : ""}" data-i="${i}">
    <button class="play" data-play="${i}" aria-label="Play ${esc(t.name)}">&#9654;</button>
    <div class="t"><div class="n">${esc(t.name)}${t.isNew ? '<span class="new-tag">NEW</span>' : ""}</div>
      <div class="a">${esc(t.artistText)}${t.album ? " · " + esc(t.album) : ""}</div></div>
    ${bpmCell}
    <div class="y">${t.year ?? ""}</div>
    <div class="d">${t.added ? t.added.slice(0, 10) : ""}</div>
    <button class="add" data-add="${i}" aria-label="Add ${esc(t.name)} to a playlist" title="Add to playlist"><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button>
    <a class="open" href="https://open.spotify.com/track/${t.id}" target="_blank" rel="noopener" aria-label="Open in Spotify">&#8599;</a>
  </li>`;
}

function render() {
  applyFilters();
  $("list").innerHTML = view.map(rowHtml).join("");
  $("empty").hidden = view.length > 0;
  const withBpm = tracks.filter((t) => t.bpm != null).length;
  const gen = $("stats").dataset.generated;
  $("stats").textContent =
    `${view.length.toLocaleString()} of ${tracks.length.toLocaleString()} songs shown · ` +
    `${withBpm.toLocaleString()} with BPM (${Math.round((100 * withBpm) / Math.max(tracks.length, 1))}%)` +
    (gen ? ` · data updated ${gen.slice(0, 10)}` : "");
  const key = $("sort").value.split("-")[0];
  document.querySelectorAll(".list-head button").forEach((b) => b.classList.toggle("on", b.dataset.sort === key));
  saveControls();
}

const CONTROL_IDS = ["search", "bpm-min", "bpm-max", "sort", "show", "octave"];
function saveControls() {
  store.set("controls", Object.fromEntries(CONTROL_IDS.map((id) => [id, $(id).type === "checkbox" ? $(id).checked : $(id).value])));
}
function restoreControls() {
  const c = store.get("controls");
  if (!c) return;
  for (const id of CONTROL_IDS) {
    if (!(id in c)) continue;
    if ($(id).type === "checkbox") $(id).checked = !!c[id];
    else $(id).value = c[id];
  }
}

// ---------- playback ----------

function loadWebPlayer() {
  if (IS_MOBILE) return; // Spotify's in-browser player doesn't support mobile browsers
  window.onSpotifyWebPlaybackSDKReady = () => {
    sdkPlayer = new Spotify.Player({
      name: "Liked Songs BPM (this browser)",
      getOAuthToken: (cb) => accessToken().then((t) => t && cb(t)),
      volume: 0.8,
    });
    sdkPlayer.addListener("ready", ({ device_id }) => { sdkDeviceId = device_id; refreshDevices(); });
    sdkPlayer.addListener("not_ready", () => { sdkDeviceId = null; refreshDevices(); });
    sdkPlayer.addListener("account_error", () => toast("In-browser playback needs Spotify Premium."));
    sdkPlayer.addListener("player_state_changed", () => pollNowPlaying());
    sdkPlayer.connect();
  };
  const s = document.createElement("script");
  s.src = "https://sdk.scdn.co/spotify-player.js";
  document.body.appendChild(s);
}

async function refreshDevices() {
  const sel = $("device");
  const prev = sel.value || store.get("device");
  let devices = [];
  try { devices = (await api("/me/player/devices")).devices || []; } catch {}
  if (sdkDeviceId && !devices.some((d) => d.id === sdkDeviceId)) {
    devices.unshift({ id: sdkDeviceId, name: "This browser", type: "Computer" });
  }
  sel.innerHTML = devices.length
    ? devices.map((d) => `<option value="${esc(d.id)}">${d.id === sdkDeviceId ? "This browser" : esc(d.name)}${d.is_active ? " (active)" : ""}</option>`).join("")
    : `<option value="">No devices: open Spotify</option>`;
  const pick = devices.find((d) => d.id === prev) || devices.find((d) => d.is_active) || devices[0];
  if (pick) sel.value = pick.id;
}

async function playFrom(i) {
  if (!store.get("token")) return login();
  const uris = view.slice(i, i + QUEUE_SIZE).map((t) => "spotify:track:" + t.id);
  let device = $("device").value;
  if (!device) { await refreshDevices(); device = $("device").value; }
  if (device === sdkDeviceId && sdkPlayer) sdkPlayer.activateElement();
  try {
    await api("/me/player/play", { method: "PUT", query: device ? { device_id: device } : undefined, body: { uris } });
    store.set("device", device);
    pos = { progress: 0, duration: 0, at: Date.now() };
    isPlaying = true;
    holdUntil = Date.now() + HOLD_MS;
    nowPlayingId = view[i].id;
    markPlaying();
    setTimeout(pollNowPlaying, 800);
  } catch (e) {
    if (e.status === 404) toast("No active Spotify device. Open Spotify on your phone or computer, then pick it in the device list.");
    else if (e.status === 403) toast("Spotify refused playback (" + (e.reason || e.message) + "). Playback needs Premium.");
    else toast(e.message);
    refreshDevices();
  }
}

async function pollNowPlaying() {
  if (!store.get("token") || document.hidden) return;
  let s = null;
  try { s = await api("/me/player"); } catch { return; }
  const item = s && s.item;
  const polled = { progress: s?.progress_ms || 0, duration: item?.duration_ms || 0, at: Date.now() };
  const polledPlaying = !!(s && s.is_playing);
  if (Date.now() > volHoldUntil) {
    const d = s && s.device;
    volume = d && d.supports_volume !== false && d.volume_percent != null ? d.volume_percent : null;
    updateVolumeUi();
  }
  const stale = Date.now() < holdUntil &&
    (Math.abs(polled.progress - currentPosition()) > 1500 || polledPlaying !== isPlaying);
  if (!stale) {
    pos = polled;
    isPlaying = polledPlaying;
    holdUntil = 0;
  }
  $("toggle").innerHTML = isPlaying ? "&#10074;&#10074;" : "&#9654;";
  updateProgress();
  if (stale) return;
  if (!item) { $("now-title").textContent = "Nothing playing"; $("now-sub").textContent = ""; return; }
  const t = tracks.find((x) => x.id === item.id);
  $("now-title").textContent = item.name;
  $("now-sub").textContent = item.artists.map((a) => a.name).join(", ") +
    (t && t.bpm != null ? ` · ${t.source === "estimate" ? "≈" : ""}${Math.round(t.bpm)} BPM` : "") +
    (s.device ? ` · on ${s.device.id === sdkDeviceId ? "this browser" : s.device.name}` : "");
  if (item.id !== nowPlayingId) { nowPlayingId = item.id; markPlaying(); }
}

function markPlaying() {
  document.querySelectorAll(".row.playing").forEach((r) => r.classList.remove("playing"));
  const i = view.findIndex((t) => t.id === nowPlayingId);
  if (i >= 0) document.querySelector(`.row[data-i="${i}"]`)?.classList.add("playing");
}

async function transport(action) {
  holdUntil = Date.now() + HOLD_MS;
  try {
    if (action === "toggle") {
      pos = { ...pos, progress: currentPosition(), at: Date.now() };
      isPlaying = !isPlaying; // optimistic, so the bar stops/starts straight away
      await api(isPlaying ? "/me/player/play" : "/me/player/pause", { method: "PUT" });
    } else {
      pos = { ...pos, progress: 0, at: Date.now() };
      await api("/me/player/" + action, { method: "POST" });
    }
  } catch (e) {
    toast(e.status === 404 ? "No active Spotify device." : e.message);
  }
  setTimeout(pollNowPlaying, 500);
}

function currentPosition() {
  const p = pos.progress + (isPlaying ? Date.now() - pos.at : 0);
  return Math.min(p, pos.duration || p);
}

function fmtTime(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function updateProgress() {
  if (dragging) return;
  const p = currentPosition();
  $("seek").value = pos.duration ? Math.round((1000 * p) / pos.duration) : 0;
  $("pos").textContent = fmtTime(p);
  $("dur").textContent = fmtTime(pos.duration);
}

async function seekTo(fraction) {
  if (!pos.duration) return;
  const ms = Math.round(Math.min(Math.max(fraction, 0), 0.999) * pos.duration);
  pos = { ...pos, progress: ms, at: Date.now() };
  holdUntil = Date.now() + HOLD_MS;
  updateProgress();
  try {
    await api("/me/player/seek", { method: "PUT", query: { position_ms: ms } });
  } catch (e) {
    toast(e.status === 404 ? "No active Spotify device." : e.message);
  }
}

function updateVolumeUi() {
  $("vol").disabled = volume == null;
  $("vol").value = volume ?? 0;
  $("vol-pct").textContent = volume == null ? "–" : volume + "%";
  $("vol").title = volume == null ? "This device doesn't allow volume control from here" : "";
}

function setVolume(v) {
  if (volume == null) return toast("Volume can't be changed on this device from here.");
  volume = Math.round(Math.min(100, Math.max(0, v)));
  volHoldUntil = Date.now() + HOLD_MS;
  updateVolumeUi();
  volPending = volume;
  if (!volTimer) flushVolume();
}

// Send at most one volume change every 250 ms (key repeat and dragging fire much faster).
async function flushVolume() {
  if (volPending == null) { volTimer = null; return; }
  const v = volPending;
  volPending = null;
  volTimer = setTimeout(flushVolume, 250);
  try {
    await api("/me/player/volume", { method: "PUT", query: { volume_percent: v } });
  } catch (e) {
    toast(e.status === 404 ? "No active Spotify device." : e.message);
  }
}

// ← / → skip, ↑ / ↓ volume, Space play/pause, 1 = start … 9 = 80%, 0 = 90%
function onKey(e) {
  if (!store.get("token") || e.ctrlKey || e.metaKey || e.altKey || $("pl-dialog").open || $("bpm-dialog").open) return;
  const el = e.target;
  const typing = el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "SELECT" ||
    (el.tagName === "INPUT" && el.type !== "range");
  if (typing) return;
  if (e.key === "ArrowRight") transport("next");
  else if (e.key === "ArrowLeft") transport("previous");
  else if (e.key === "ArrowUp") setVolume((volume ?? 0) + VOLUME_STEP);
  else if (e.key === "ArrowDown") setVolume((volume ?? 0) - VOLUME_STEP);
  else if (e.key === " ") transport("toggle");
  else if (/^[0-9]$/.test(e.key)) seekTo(e.key === "0" ? 0.9 : (+e.key - 1) / 10);
  else return;
  e.preventDefault();
}

// ---------- playlists ----------

let me = null;
let playlists = null;
let playlistsAt = 0;
let addIds = [];
let addPath = "items"; // Spotify's newer name for playlist tracks; falls back to "tracks"

function hasScope(scope) {
  return ((store.get("token") || {}).scope || "").split(" ").includes(scope);
}

function suggestedName() {
  const min = $("bpm-min").value, max = $("bpm-max").value;
  return min || max ? `${min || "0"}–${max || "∞"} BPM` : "";
}

async function openAddDialog(ids, label) {
  if (!store.get("token")) return login();
  if (!hasScope("playlist-modify-private")) {
    if (confirm("Adding to playlists needs permission to edit your Spotify playlists. Reconnect Spotify now?")) login();
    return;
  }
  if (!ids.length) return toast("No songs to add.");
  if (ids.length > 200 && !confirm(`Add all ${ids.length.toLocaleString()} songs shown to a playlist?`)) return;
  addIds = ids;
  $("pl-title").textContent = ids.length === 1 ? "Add to playlist" : `Add ${ids.length.toLocaleString()} songs to a playlist`;
  $("pl-sub").textContent = label;
  $("pl-filter").value = "";
  $("pl-new-name").value = ids.length > 1 ? suggestedName() : "";
  $("pl-dialog").showModal();
  renderPlaylists();
  try {
    await loadPlaylists();
  } catch (e) {
    $("pl-list").innerHTML = `<li class="msg">Couldn't load playlists: ${esc(e.message)}</li>`;
    return;
  }
  renderPlaylists();
}

async function loadPlaylists() {
  if (playlists && Date.now() - playlistsAt < 60000) return;
  me = me || (await api("/me"));
  const list = [];
  let url = API + "/me/playlists?limit=50";
  while (url) {
    const j = await api(url);
    // You can only add to playlists you own or collaborate on.
    list.push(...(j.items || []).filter((p) => p && (p.owner?.id === me.id || p.collaborative)));
    url = j.next;
  }
  playlists = list;
  playlistsAt = Date.now();
}

function renderPlaylists() {
  const el = $("pl-list");
  if (!playlists) { el.innerHTML = `<li class="msg">Loading your playlists…</li>`; return; }
  const q = $("pl-filter").value.trim().toLowerCase();
  const shown = playlists.filter((p) => p.name.toLowerCase().includes(q));
  el.innerHTML = shown.length
    ? shown.map((p) => {
        const n = (p.tracks || p.items || {}).total;
        return `<li><button type="button" data-pl="${esc(p.id)}"><span>${esc(p.name)}</span>` +
          `<span class="count">${n != null ? n.toLocaleString() + " songs" : ""}</span></button></li>`;
      }).join("")
    : `<li class="msg">${playlists.length ? "No playlists match." : "You don't have any playlists you can edit yet."}</li>`;
}

async function postTracks(pid, uris) {
  try {
    return await api(`/playlists/${pid}/${addPath}`, { method: "POST", body: { uris } });
  } catch (e) {
    if (e.status !== 404 || addPath === "tracks") throw e;
    addPath = "tracks";
    return api(`/playlists/${pid}/tracks`, { method: "POST", body: { uris } });
  }
}

async function addToPlaylist(pid, name) {
  const uris = addIds.map((id) => "spotify:track:" + id);
  $("pl-list").innerHTML = `<li class="msg">Adding to “${esc(name)}”…</li>`;
  try {
    for (let i = 0; i < uris.length; i += 100) await postTracks(pid, uris.slice(i, i + 100));
  } catch (e) {
    renderPlaylists();
    return toast(e.status === 403 ? "Spotify won't let you add to that playlist." : "Couldn't add: " + e.message);
  }
  $("pl-dialog").close();
  playlistsAt = 0; // song counts changed
  toast(`Added ${uris.length === 1 ? "1 song" : uris.length.toLocaleString() + " songs"} to “${name}”.`);
}

async function createAndAdd() {
  const name = $("pl-new-name").value.trim();
  if (!name) return $("pl-new-name").focus();
  const body = { name, public: false, description: "Made with Liked Songs BPM" };
  let p;
  try {
    me = me || (await api("/me"));
    try {
      p = await api("/me/playlists", { method: "POST", body });
    } catch (e) {
      if (e.status !== 404 && e.status !== 405) throw e;
      p = await api(`/users/${encodeURIComponent(me.id)}/playlists`, { method: "POST", body });
    }
  } catch (e) {
    return toast("Couldn't create playlist: " + e.message);
  }
  if (playlists) playlists.unshift(p);
  await addToPlaylist(p.id, p.name);
}

// ---------- manual BPM corrections (only when served by serve.py on this computer) ----------

let canEdit = false;
let editTrack = null;

function openEdit(t) {
  editTrack = t;
  const r = (v) => Math.round(v * 10) / 10;
  $("bpm-sub").textContent = `${t.name} · ${t.artistText}`;
  $("bpm-info").innerHTML = t.bpm == null
    ? `<span class="muted">No BPM found yet.</span>`
    : `Now <strong>${r(t.bpm)}</strong> <span class="muted">from ${esc(SOURCE_LABEL[t.source] || t.source)}` +
      `${t.rawBpm !== t.bpm ? `, listed as ${r(t.rawBpm)}` : ""}</span>` +
      (t.alt != null ? `<br><span class="muted">Preview analysis suggests ≈${r(t.alt)} (${Math.round(t.altConf * 100)}% confident)</span>` : "");
  const quick = [];
  if (t.bpm != null) {
    // Halving below the floor would just be doubled back, so only offer it when it sticks.
    if (t.bpm / 2 >= MIN_BPM) quick.push([`½ → ${r(t.bpm / 2)}`, r(t.bpm / 2)]);
    if (t.bpm * 2 <= 300) quick.push([`×2 → ${r(t.bpm * 2)}`, r(t.bpm * 2)]);
    if (t.alt != null) quick.unshift([`Use estimate ≈${r(t.alt)}`, r(t.alt)]);
  }
  $("bpm-quick").innerHTML = quick.map(([label, v]) => `<button class="btn ghost small" type="button" data-v="${v}">${label}</button>`).join("");
  $("bpm-input").value = t.bpm != null ? r(t.bpm) : "";
  $("bpm-reset").hidden = t.source !== "override";
  $("bpm-dialog").showModal();
  $("bpm-input").select();
}

async function saveBpm(bpm) {
  const t = editTrack;
  if (bpm != null && !(bpm >= 20 && bpm <= 300)) return toast("Enter a BPM between 20 and 300.");
  let fields;
  try {
    const resp = await fetch("api/bpm", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-BPM-App": "1" },
      body: JSON.stringify({ id: t.id, bpm }),
    });
    fields = await resp.json();
    if (!resp.ok) throw new Error(fields.error || "error " + resp.status);
  } catch (e) {
    return toast("Couldn't save: " + e.message);
  }
  for (const k of ["bpm", "source", "conf", "alt", "altConf"]) delete t[k];
  Object.assign(t, fields);
  prepare(t);
  $("bpm-dialog").close();
  render();
  toast(bpm == null
    ? `Back to the automatic BPM${t.bpm != null ? " (" + Math.round(t.bpm) + ")" : ""}.`
    : `Saved ${Math.round(t.bpm)} BPM for “${t.name}”.`);
}

// ---------- local update (only when served by serve.py on this computer) ----------

const IS_LOCAL = ["127.0.0.1", "localhost"].includes(location.hostname);
const STEP_ICON = { pending: "○", running: "●", done: "✓", failed: "✗", skipped: "–", cancelled: "–" };
let jobTimer = null;
let jobWasRunning = false;

async function jobApi(path, body) {
  const resp = await fetch("api/" + path, body === undefined ? { cache: "no-store" } : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-BPM-App": "1" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => null);
  if (!data || data.steps === undefined) throw new Error("Local update server not available");
  return data;
}

async function initLocalUpdate() {
  if (!IS_LOCAL) return;
  let s;
  try { s = await jobApi("status"); } catch { return; } // plain file server: no button
  $("update-btn").hidden = false;
  canEdit = true;
  render();
  showJob(s);
  if (s.running) { $("update-panel").hidden = false; pollJob(); }
}

function pollJob() {
  clearTimeout(jobTimer);
  jobTimer = setTimeout(async () => {
    try { showJob(await jobApi("status")); } catch {}
    if (jobWasRunning) pollJob();
  }, 1000);
}

function showJob(s) {
  const steps = s.steps || [];
  const i = steps.findIndex((x) => x.status === "running");
  const step = steps[i];
  $("update-btn").textContent = s.running ? "Updating…" : "Update BPMs";
  $("update-start").hidden = s.running;
  $("update-cancel").hidden = !s.running;
  $("opt-refresh").disabled = $("opt-publish").disabled = s.running;

  const bar = $("update-progress");
  bar.hidden = !s.running;
  if (step && step.total) { bar.max = step.total; bar.value = step.done; }
  else bar.removeAttribute("value"); // indeterminate while a step has no count yet

  $("update-step").textContent = s.running
    ? (step ? `Step ${i + 1} of ${steps.length}: ${step.name}` +
        (step.total ? ` (${step.done.toLocaleString()} / ${step.total.toLocaleString()})` : "") : "Starting…")
    : s.error ? "Stopped: " + s.error
    : s.finished ? "Finished at " + new Date(s.finished * 1000).toLocaleTimeString() : "";
  $("update-steps").innerHTML = steps.map((x) => `<li class="${x.status}">${STEP_ICON[x.status] || ""} ${esc(x.name)}</li>`).join("");
  const log = $("update-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
  log.textContent = (s.log || []).join("\n");
  if (atBottom) log.scrollTop = log.scrollHeight;

  if (jobWasRunning && !s.running) {
    loadData().then(render).catch(() => {});
    toast(s.error ? "Update stopped: " + s.error : "Update finished; BPMs reloaded.");
  }
  jobWasRunning = s.running;
}

async function startJob() {
  try {
    showJob(await jobApi("update", { refresh: $("opt-refresh").checked, publish: $("opt-publish").checked }));
    pollJob();
  } catch (e) {
    toast(e.message);
  }
}

async function cancelJob() {
  try { showJob(await jobApi("cancel", {})); } catch (e) { toast(e.message); }
}

// ---------- UI glue ----------

let toastTimer;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 5000);
}

function updateAuthUi() {
  const loggedIn = !!store.get("token");
  $("login").hidden = loggedIn;
  $("logout").hidden = !loggedIn;
  $("player").hidden = !loggedIn;
  $("add-shown").hidden = !loggedIn;
}

function wireUi() {
  let t;
  $("search").addEventListener("input", () => { clearTimeout(t); t = setTimeout(render, 150); });
  for (const id of ["bpm-min", "bpm-max"]) $(id).addEventListener("input", () => { clearTimeout(t); t = setTimeout(render, 250); });
  for (const id of ["sort", "show", "octave"]) $(id).addEventListener("change", render);
  document.querySelectorAll(".list-head button").forEach((b) => b.addEventListener("click", () => {
    const [key, dir] = $("sort").value.split("-");
    const defaults = { bpm: "asc", name: "asc", year: "desc", added: "desc" };
    const next = key === b.dataset.sort ? (dir === "asc" ? "desc" : "asc") : defaults[b.dataset.sort];
    $("sort").value = b.dataset.sort + "-" + next;
    if (!$("sort").value) $("sort").value = b.dataset.sort + "-" + defaults[b.dataset.sort];
    render();
  }));
  $("list").addEventListener("click", (e) => {
    const play = e.target.closest("[data-play]");
    if (play) return playFrom(+play.dataset.play);
    const edit = e.target.closest("[data-edit]");
    if (edit) return openEdit(view[+edit.dataset.edit]);
    const add = e.target.closest("[data-add]");
    if (add) {
      const t = view[+add.dataset.add];
      openAddDialog([t.id], `${t.name} · ${t.artistText}`);
    }
  });
  $("add-shown").addEventListener("click", () => {
    const range = suggestedName();
    openAddDialog(view.map((t) => t.id), `Everything currently shown${range ? " (" + range + ")" : ""}, in this order`);
  });
  $("pl-filter").addEventListener("input", renderPlaylists);
  $("pl-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-pl]");
    if (b) addToPlaylist(b.dataset.pl, b.querySelector("span").textContent);
  });
  $("pl-create").addEventListener("click", createAndAdd);
  $("pl-new-name").addEventListener("keydown", (e) => { if (e.key === "Enter") createAndAdd(); });
  $("pl-close").addEventListener("click", () => $("pl-dialog").close());
  $("bpm-close").addEventListener("click", () => $("bpm-dialog").close());
  $("bpm-save").addEventListener("click", () => saveBpm(parseFloat($("bpm-input").value)));
  $("bpm-input").addEventListener("keydown", (e) => { if (e.key === "Enter") saveBpm(parseFloat($("bpm-input").value)); });
  $("bpm-reset").addEventListener("click", () => saveBpm(null));
  $("bpm-quick").addEventListener("click", (e) => {
    const b = e.target.closest("[data-v]");
    if (b) saveBpm(+b.dataset.v);
  });
  $("update-btn").addEventListener("click", () => {
    $("update-panel").hidden = !$("update-panel").hidden;
    if (!$("update-panel").hidden) $("update-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("update-close").addEventListener("click", () => { $("update-panel").hidden = true; });
  $("update-start").addEventListener("click", startJob);
  $("update-cancel").addEventListener("click", cancelJob);
  $("login").addEventListener("click", login);
  $("logout").addEventListener("click", logout);
  $("toggle").addEventListener("click", () => transport("toggle"));
  $("next").addEventListener("click", () => transport("next"));
  $("prev").addEventListener("click", () => transport("previous"));
  $("seek").addEventListener("input", () => {
    dragging = true;
    $("pos").textContent = fmtTime((pos.duration * $("seek").value) / 1000);
  });
  $("seek").addEventListener("change", () => {
    dragging = false;
    seekTo($("seek").value / 1000);
  });
  $("vol").addEventListener("input", () => setVolume(+$("vol").value));
  document.addEventListener("keydown", onKey);
  setInterval(updateProgress, 250);
  $("device").addEventListener("focus", refreshDevices);
  $("device").addEventListener("change", () => store.set("device", $("device").value));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollNowPlaying(); });
}

async function main() {
  wireUi();
  restoreControls();
  await handleRedirect();
  updateAuthUi();
  try {
    await loadData();
  } catch (e) {
    $("stats").textContent = e.message;
    return;
  }
  render();
  initLocalUpdate();
  if (store.get("token")) {
    loadWebPlayer();
    refreshDevices();
    pollNowPlaying();
    setInterval(pollNowPlaying, 5000);
    mergeNewLikes().catch((e) => toast("Couldn't check for new likes: " + e.message));
  }
}

main();
