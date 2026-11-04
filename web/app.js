/* Rezzie — mobile web front end. No build step, no dependencies. */
"use strict";

// ------------------------------------------------------------------ helpers
const $view = document.getElementById("view");
const $tabbar = document.getElementById("tabbar");
const $title = document.getElementById("title");
const $topRight = document.getElementById("topRight");

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "html") n.innerHTML = v; // only ever used with static strings
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

async function api(path, opts = {}) {
  const o = { method: "GET", headers: {}, credentials: "same-origin", ...opts };
  if (o.body && typeof o.body !== "string") {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (_) { /* empty */ }
  if (r.status === 401 && !path.startsWith("/api/login")) { state.authed = false; route(); throw new Error("Not logged in"); }
  if (!r.ok) {
    let msg = (data && (data.error || data.detail)) || `HTTP ${r.status}`;
    if (Array.isArray(msg)) msg = msg.map(d => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ");
    throw new Error(msg);
  }
  return data;
}

let toastTimer;
function toast(msg, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? 5000 : 2500);
}

async function busy(btn, fn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.replaceChildren(el("span", { class: "spinner" }));
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = label; }
}

const pad = n => String(n).padStart(2, "0");
const todayLocal = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
const addDays = (ymd, n) => { const d = new Date(ymd + "T12:00:00"); d.setDate(d.getDate() + n); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
function fmtDate(ymd) {
  const d = new Date(ymd + "T12:00:00");
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}
function fmtTime(hhmm) {
  if (!hhmm) return "";
  const [h, m] = hhmm.split(":").map(Number);
  const ap = h >= 12 ? "pm" : "am";
  return `${((h + 11) % 12) + 1}:${pad(m)}${ap}`;
}
function fmtWhen(iso, tz) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: tz || undefined, timeZoneName: "short" });
}
function ago(iso) {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function countdownText(iso) {
  let s = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000));
  const d = Math.floor(s / 86400); s %= 86400;
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  return d ? `${d}d ${h}h ${pad(m)}m` : `${pad(h)}:${pad(m)}:${pad(s)}`;
}
// Convert a UTC ISO time to "YYYY-MM-DDTHH:MM" in a timezone (for editing).
function isoToLocalInput(iso, tz) {
  if (!iso) return "";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso));
  const g = t => parts.find(p => p.type === t).value;
  return `${g("year")}-${g("month")}-${g("day")}T${g("hour")}:${g("minute")}`;
}

const PLATFORM_NAMES = { resy: "Resy", sevenrooms: "SevenRooms", opentable: "OpenTable" };
const MODE_NAMES = { snipe: "Snipe", monitor: "Monitor", reminder: "Reminder" };
const STATUS_NAMES = { scheduled: "Scheduled", monitoring: "Watching", attempting: "Sniping…", booked: "Booked", found: "Found", notified: "Reminded", failed: "Failed", cancelled: "Cancelled", expired: "Expired", paused: "Paused" };

const state = { authed: null, status: null, draft: null };

// ------------------------------------------------------------------ router
window.addEventListener("hashchange", route);
setInterval(() => {
  document.querySelectorAll("[data-countdown]").forEach(n => {
    const iso = n.dataset.countdown;
    n.textContent = countdownText(iso);
    n.classList.toggle("hot", new Date(iso).getTime() - Date.now() < 120000);
  });
}, 1000);
let refreshTimer;

async function route() {
  clearInterval(refreshTimer);
  if (state.authed === null) {
    try { state.authed = (await api("/api/me")).authed; } catch (_) { state.authed = false; }
  }
  if (!state.authed) return renderLogin();
  $tabbar.hidden = false;
  const h = location.hash.replace(/^#/, "") || "/";
  const tab = h.startsWith("/new") ? "new" : h.startsWith("/history") ? "history" : h.startsWith("/settings") ? "settings" : "requests";
  document.querySelectorAll(".tabbar a").forEach(a => a.classList.toggle("active", a.dataset.tab === tab));
  $topRight.replaceChildren();
  window.scrollTo(0, 0);
  try {
    if (h.startsWith("/r/")) return await renderDetail(h.slice(3));
    if (h.startsWith("/edit/")) return await renderNew(h.slice(6));
    if (h.startsWith("/new")) return await renderNew();
    if (h.startsWith("/history")) return await renderList("history");
    if (h.startsWith("/settings")) return await renderSettings();
    return await renderList("active");
  } catch (e) {
    $view.replaceChildren(el("div", { class: "notice bad" }, e.message));
  }
}

function setTitle(t) { $title.replaceChildren(t === "Rezzie" ? el("span", {}, "Rezzie", el("span", { class: "dot" }, ".")) : t); }

// ------------------------------------------------------------------- login
function renderLogin() {
  $tabbar.hidden = true;
  setTitle("Rezzie");
  const pw = el("input", { type: "password", placeholder: "Password", autocomplete: "current-password", autofocus: true });
  const btn = el("button", { class: "btn primary block", type: "submit" }, "Unlock");
  const form = el("form", { class: "login", onsubmit: e => {
    e.preventDefault();
    busy(btn, async () => {
      await api("/api/login", { method: "POST", body: { password: pw.value } });
      state.authed = true;
      location.hash = "#/";
      route();
    });
  } },
    el("h2", {}, "Rezzie", el("span", { class: "dot", style: "color:var(--accent)" }, ".")),
    el("p", { class: "muted" }, "Personal reservation sniper"),
    pw, el("div", { style: "height:12px" }), btn);
  $view.replaceChildren(form);
}

// -------------------------------------------------------------- list views
function requestCard(r) {
  const meta = [];
  meta.push(`${r.party_size} ppl`);
  meta.push(r.dates.length > 2 ? `${fmtDate(r.dates[0])} → ${fmtDate(r.dates[r.dates.length - 1])} (${r.dates.length})` : r.dates.map(fmtDate).join(", "));
  meta.push(`${fmtTime(r.time_start)}–${fmtTime(r.time_end)}`);
  let footer = null;
  if (r.status === "scheduled" && r.release_at) {
    footer = el("div", {},
      el("div", { class: "countdown mono", "data-countdown": r.release_at }, countdownText(r.release_at)),
      el("div", { class: "muted small" }, `Opens ${fmtWhen(r.release_at, r.venue_tz)}`));
  } else if (r.status === "monitoring") {
    footer = el("div", { class: "muted small" }, `Checked ${ago(r.last_checked)} · ${r.attempt_count} checks`);
  } else if (r.status === "booked" || r.status === "found") {
    footer = el("div", { class: "small", style: "color:var(--ok)" }, `${fmtDate(r.booked_date)} at ${fmtTime(r.booked_time)}${r.booked_seating ? " · " + r.booked_seating : ""}${r.confirmation ? " · #" + r.confirmation : ""}`);
  }
  return el("a", { class: "card link", href: `#/r/${r.id}` },
    el("div", { class: "row between" },
      el("h3", {}, r.venue_name),
      el("span", { class: `badge ${r.status}` }, STATUS_NAMES[r.status] || r.status)),
    el("div", { class: "row", style: "margin:4px 0 6px" },
      el("span", { class: `badge p-${r.platform}` }, PLATFORM_NAMES[r.platform]),
      el("span", { class: "badge" }, MODE_NAMES[r.mode])),
    el("div", { class: "muted" }, meta.join(" · ")),
    r.last_error && !["booked", "cancelled"].includes(r.status) ? el("div", { class: "small", style: "color:#ff8a8a;margin-top:6px" }, r.last_error) : null,
    footer);
}

async function renderList(scope) {
  setTitle(scope === "history" ? "History" : "Rezzie");
  const [items, st] = await Promise.all([api(`/api/requests?scope=${scope}`), api("/api/status").catch(() => null)]);
  state.status = st;
  const kids = [];
  if (st && scope === "active") {
    for (const [p, s] of Object.entries(st.platforms)) {
      if (s.paused_until) kids.push(el("div", { class: "notice bad" }, `${PLATFORM_NAMES[p]} is cooling off until ${new Date(s.paused_until * 1000).toLocaleTimeString()} — ${s.pause_reason}`));
    }
    if (!st.ntfy_topic) kids.push(el("a", { class: "notice", href: "#/settings", style: "display:block;text-decoration:none" }, "Set up push notifications in Settings so you hear about tables instantly →"));
    if (st.dry_run) kids.push(el("div", { class: "notice" }, "DRY RUN is on — bookings are simulated."));
  }
  if (!items.length) {
    kids.push(el("div", { class: "empty" },
      scope === "history" ? "Nothing here yet." : el("div", {}, el("p", {}, "No active requests."), el("a", { class: "btn primary", href: "#/new" }, "＋ New request"))));
  } else {
    items.forEach(r => kids.push(requestCard(r)));
  }
  $view.replaceChildren(...kids);
  if (scope === "active") refreshTimer = setInterval(async () => {
    if (location.hash && location.hash !== "#/") return;
    try {
      const fresh = await api("/api/requests?scope=active");
      if (JSON.stringify(fresh.map(r => [r.id, r.status, r.last_checked])) !== JSON.stringify(items.map(r => [r.id, r.status, r.last_checked]))) route();
    } catch (_) { /* ignore */ }
  }, 15000);
}

// ------------------------------------------------------------------ detail
async function renderDetail(id) {
  const r = await api(`/api/requests/${id}`);
  setTitle(r.venue_name);
  $topRight.replaceChildren(el("a", { class: "btn ghost", href: "#/", style: "min-height:36px;padding:6px 12px" }, "Back"));
  const rows = [
    ["Platform", PLATFORM_NAMES[r.platform]],
    ["Mode", MODE_NAMES[r.mode] + (r.platform !== "opentable" ? (r.auto_book ? " · auto-book" : " · notify only") : " · notify + link")],
    ["Party", `${r.party_size}`],
    ["Dates", r.dates.map(fmtDate).join(", ")],
    ["Time window", `${fmtTime(r.time_start)} – ${fmtTime(r.time_end)}${r.ideal_time ? ` (ideal ${fmtTime(r.ideal_time)})` : ""}`],
  ];
  if (r.seating_exclude?.length) rows.push(["Skip seating", r.seating_exclude.join(", ")]);
  if (r.release_at) rows.push(["Opens", fmtWhen(r.release_at, r.venue_tz)]);
  if (r.mode !== "reminder") rows.push(["Checks", `${r.attempt_count} · last ${ago(r.last_checked)}`]);
  if (r.status === "monitoring" && r.next_check_at) rows.push(["Next check", fmtWhen(r.next_check_at)]);
  if (r.monitor_until) rows.push(["Watch until", fmtWhen(r.monitor_until, r.venue_tz)]);
  if (r.notes) rows.push(["Notes", r.notes]);

  const result = [];
  if (r.status === "booked") result.push(el("div", { class: "notice ok" }, `Booked ${fmtDate(r.booked_date)} at ${fmtTime(r.booked_time)}${r.booked_seating ? ` (${r.booked_seating})` : ""} — confirmation ${r.confirmation}`));
  if (r.status === "booked" && r.platform === "sevenrooms") result.push(el("div", { class: "muted small" }, "To cancel or change it, use the link in the confirmation email from the restaurant (via SevenRooms)."));
  if (r.status === "found") result.push(el("div", { class: "notice ok" }, `Found ${fmtDate(r.booked_date)} at ${fmtTime(r.booked_time)}. Tap “Open booking page” to grab it.`));
  if (r.last_error && !["booked", "cancelled"].includes(r.status)) result.push(el("div", { class: "notice bad" }, r.last_error));

  const slotBox = el("div");
  const actions = [];
  const link = r.found_link || r.venue_url;
  if (link && /^https?:\/\//i.test(link)) actions.push(el("a", { class: "btn primary", href: link, target: "_blank", rel: "noopener" }, "Open booking page"));
  if (r.platform !== "opentable" && !["booked"].includes(r.status)) {
    const b = el("button", { class: "btn", onclick: () => busy(b, async () => {
      const res = await api(`/api/requests/${id}/check`, { method: "POST" });
      const lo = r.time_start, hi = r.time_end;
      slotBox.replaceChildren(el("div", { class: "section-title" }, `Open slots right now (${res.slots.length})`),
        res.slots.length ? el("div", { class: "slotlist" }, res.slots.map(s => el("span", { class: s.time >= lo && s.time <= hi ? "match" : "" }, `${fmtDate(s.date).split(",")[0]} ${fmtTime(s.time)}${s.seating ? " · " + s.seating : ""}`)))
          : el("div", { class: "muted" }, "Nothing open."));
    }) }, "Check now");
    actions.push(b);
  }
  if (["scheduled", "monitoring", "attempting", "paused"].includes(r.status)) {
    const b = el("button", { class: "btn danger", onclick: () => confirm("Stop this request?") && busy(b, async () => { await api(`/api/requests/${id}/cancel`, { method: "POST" }); route(); }) }, "Stop");
    actions.push(b);
  }
  if (["failed", "expired", "cancelled", "paused", "found", "notified"].includes(r.status) && r.platform !== "opentable") {
    const b = el("button", { class: "btn", onclick: () => busy(b, async () => { await api(`/api/requests/${id}/resume`, { method: "POST" }); route(); }) }, "Watch for cancellations");
    actions.push(b);
  }
  if (r.status === "booked" && r.platform === "resy" && r.booking_token) {
    const b = el("button", { class: "btn danger", onclick: () => confirm("Cancel this reservation on Resy?") && busy(b, async () => { await api(`/api/requests/${id}/cancel-booking`, { method: "POST" }); toast("Reservation cancelled"); route(); }) }, "Cancel reservation");
    actions.push(b);
  }
  if (r.status !== "attempting") actions.push(el("a", { class: "btn ghost", href: `#/edit/${id}` }, "Edit / copy"));
  const del = el("button", { class: "btn ghost", onclick: () => confirm("Delete this request and its log?") && busy(del, async () => { await api(`/api/requests/${id}`, { method: "DELETE" }); location.hash = "#/"; }) }, "Delete");
  actions.push(del);

  $view.replaceChildren(
    el("div", { class: "card" },
      el("div", { class: "row between" }, el("span", { class: `badge p-${r.platform}` }, PLATFORM_NAMES[r.platform]), el("span", { class: `badge ${r.status}` }, STATUS_NAMES[r.status] || r.status)),
      r.status === "scheduled" && r.release_at ? el("div", { class: "countdown mono", "data-countdown": r.release_at }, countdownText(r.release_at)) : null,
      ...result,
      el("div", { style: "margin-top:8px" }, rows.map(([k, v]) => el("div", { class: "row between", style: "padding:6px 0;border-bottom:1px solid var(--line)" }, el("span", { class: "muted" }, k), el("span", { style: "text-align:right" }, v))))),
    el("div", { class: "btns" }, actions),
    slotBox,
    el("div", { class: "section-title" }, "Activity"),
    el("div", { class: "card log" }, r.events.length ? r.events.map(e => el("div", { class: e.level }, el("span", { class: "ts" }, fmtWhen(e.ts)), e.message)) : el("div", { class: "muted" }, "No activity yet.")));
  if (["scheduled", "attempting", "monitoring"].includes(r.status)) {
    refreshTimer = setInterval(async () => {
      try {
        const fresh = await api(`/api/requests/${id}`);
        if (fresh.status !== r.status || fresh.events.length !== r.events.length) route();
      } catch (_) { /* ignore */ }
    }, r.status === "attempting" ? 2000 : 10000);
  }
}

// --------------------------------------------------------------- new / edit
function seg(options, value, onChange) {
  const wrap = el("div", { class: "seg" });
  const draw = v => {
    wrap.replaceChildren(...options.map(([val, label]) => el("button", { type: "button", class: val === v ? "on" : "", onclick: () => { onChange(val); draw(val); } }, label)));
  };
  draw(value);
  return wrap;
}
function toggle(checked, onChange) {
  const i = el("input", { type: "checkbox", onchange: e => onChange(e.target.checked) });
  i.checked = !!checked;
  return el("label", { class: "tog" }, i, el("span"));
}
function sw(title, sub, checked, onChange) {
  return el("div", { class: "switch" }, el("div", { class: "txt" }, title, sub ? el("span", { class: "muted" }, sub) : null), toggle(checked, onChange));
}

function blankDraft() {
  const d = addDays(todayLocal(), 14);
  return {
    platform: "resy", mode: "snipe", venue: null,
    party_size: 2, dates: [d], time_start: "18:30", time_end: "21:00", ideal_time: "19:30",
    seating_exclude: "", auto_book: true, release_local: "", snipe_window_seconds: 45,
    fallback_to_monitor: true, poll_interval_seconds: 120, monitor_until: "", notes: "",
  };
}

async function renderNew(editId) {
  setTitle(editId ? "Edit request" : "New request");
  if (!state.status) state.status = await api("/api/status").catch(() => null);
  let d;
  if (editId) {
    const r = await api(`/api/requests/${editId}`);
    // Finished requests are copied rather than overwritten (keeps the booking record).
    if (!["scheduled", "monitoring", "paused"].includes(r.status)) {
      editId = null;
      setTitle("New (copy)");
      if (r.release_at && new Date(r.release_at) < new Date()) r.release_at = null;
    }
    d = {
      ...blankDraft(), ...r,
      venue: { id: r.venue_id, name: r.venue_name, slug: r.venue_slug, city: r.venue_city, timezone: r.venue_tz, url: r.venue_url, platform: r.platform },
      seating_exclude: (r.seating_exclude || []).join(", "),
      release_local: r.release_at ? isoToLocalInput(r.release_at, r.venue_tz) : "",
      monitor_until: r.monitor_until ? isoToLocalInput(r.monitor_until, r.venue_tz) : "",
      mode: r.mode === "reminder" ? "snipe" : r.mode,
      poll_interval_seconds: r.poll_interval_seconds || 120,
      snipe_window_seconds: r.snipe_window_seconds || 45,
    };
    $topRight.replaceChildren(el("a", { class: "btn ghost", href: `#/r/${r.id}`, style: "min-height:36px;padding:6px 12px" }, "Back"));
  } else {
    d = state.draft || blankDraft();
  }
  state.draft = d;
  const body = el("div");
  const redraw = () => body.replaceChildren(...(d.venue ? configForm(d, editId) : venuePicker(d, redraw)));
  d._redraw = redraw;
  redraw();
  $view.replaceChildren(body);
}

function venuePicker(d, redraw) {
  const results = el("div");
  const q = el("input", { type: "search", placeholder: "", autocomplete: "off", enterkeyhint: "search" });
  const hint = el("div", { class: "muted small", style: "margin-top:8px" });
  const setHint = () => {
    q.placeholder = { resy: "Restaurant name, or paste a resy.com link", sevenrooms: "Paste sevenrooms.com link or venue slug", opentable: "Paste opentable.com/r/… link" }[d.platform];
    hint.textContent = {
      resy: "Resy: full auto-booking supported.",
      sevenrooms: "SevenRooms (also what DoorDash Reservations uses): auto-books tables that don't need a card, using your booking details from Settings. Card-required tables get a one-tap link.",
      opentable: "OpenTable: we never touch their servers (aggressive bot detection). You get a 60-second warning and a one-tap link at release time. Also turn on OpenTable's own “Notify me” in their app.",
    }[d.platform];
  };
  setHint();
  const searchBtn = el("button", { class: "btn primary block", type: "submit", style: "margin-top:10px" }, "Search");
  const form = el("form", { onsubmit: e => {
    e.preventDefault();
    if (!q.value.trim()) return;
    busy(searchBtn, async () => {
      let lat = 40.7128, lng = -74.006;
      const venues = await api(`/api/search?platform=${d.platform}&q=${encodeURIComponent(q.value)}&lat=${lat}&lng=${lng}`);
      if (!venues.length) { results.replaceChildren(el("div", { class: "empty" }, "No matches.")); return; }
      results.replaceChildren(el("div", { class: "card" }, venues.map(v => el("div", { class: "result", onclick: async () => {
        if (d.platform === "opentable") {
          const name = prompt("Restaurant name (for your list):", v.name);
          if (name) v.name = name;
        }
        d.venue = v;
        if (d.platform === "opentable") d.mode = "snipe";
        redraw();
      } },
        el("div", { style: "font-weight:600" }, v.name),
        el("div", { class: "muted small" }, [v.neighborhood, v.city, v.cuisine].filter(Boolean).join(" · ") || v.url)))));
    });
  } }, q, searchBtn);
  return [
    seg([["resy", "Resy"], ["sevenrooms", "SevenRooms"], ["opentable", "OpenTable"]], d.platform, v => { d.platform = v; setHint(); results.replaceChildren(); }),
    el("div", { style: "height:12px" }), form, hint, el("div", { style: "height:12px" }), results,
  ];
}

function configForm(d, editId) {
  const v = d.venue;
  const isOT = d.platform === "opentable";
  const isResy = d.platform === "resy";
  const out = [];
  out.push(el("div", { class: "card" },
    el("div", { class: "row between" },
      el("div", {}, el("h3", {}, v.name), el("div", { class: "muted small" }, [PLATFORM_NAMES[d.platform], v.neighborhood || v.city, v.timezone].filter(Boolean).join(" · "))),
      editId ? null : el("button", { class: "btn ghost", type: "button", style: "min-height:36px;padding:6px 12px", onclick: () => { d.venue = null; d._redraw(); } }, "Change"))));

  // Mode
  if (!isOT) {
    out.push(el("label", {}, "Mode"));
    out.push(seg([["snipe", "Snipe the drop"], ["monitor", "Watch cancellations"]], d.mode, m => { d.mode = m; d._redraw(); }));
    out.push(el("div", { class: "muted small", style: "margin-top:6px" }, d.mode === "snipe"
      ? "Fires the instant reservations are released, then (optionally) keeps watching."
      : "Checks politely every few minutes (with random jitter) and pounces on a cancellation."));
  }

  // Party
  const val = el("span", { class: "val" }, d.party_size);
  out.push(el("label", {}, "Party size"));
  out.push(el("div", { class: "stepper" },
    el("button", { type: "button", onclick: () => { d.party_size = Math.max(1, d.party_size - 1); val.textContent = d.party_size; } }, "−"), val,
    el("button", { type: "button", onclick: () => { d.party_size = Math.min(20, d.party_size + 1); val.textContent = d.party_size; } }, "+")));

  // Dates
  const chips = el("div", { class: "chips" });
  const drawChips = () => chips.replaceChildren(...d.dates.map(x => el("span", { class: "chip" }, fmtDate(x), el("button", { type: "button", "aria-label": "remove", onclick: () => { d.dates = d.dates.filter(y => y !== x); drawChips(); } }, "×"))));
  drawChips();
  const dateIn = el("input", { type: "date", min: todayLocal(), onchange: e => {
    const x = e.target.value;
    if (x && isOT) { d.dates = [x]; drawChips(); }
    else if (x && !d.dates.includes(x)) { d.dates = [...d.dates, x].sort(); drawChips(); if (d.mode === "snipe" && isResy && d.dates.length === 1) suggestRelease(); }
    e.target.value = "";
  } });
  out.push(el("label", {}, isOT ? "Date" : "Dates (add one or more)"), dateIn, chips);

  // Times
  const tIn = (key) => { const i = el("input", { type: "time", value: d[key] || "", step: 900, onchange: e => (d[key] = e.target.value) }); return i; };
  out.push(el("div", { class: "grid2" },
    el("div", {}, el("label", {}, "Earliest"), tIn("time_start")),
    el("div", {}, el("label", {}, "Latest"), tIn("time_end"))));
  out.push(el("label", {}, "Ideal time (closest wins)"), tIn("ideal_time"));

  if (!isOT) {
    out.push(el("label", {}, "Skip seating types containing (comma separated)"),
      el("input", { type: "text", value: d.seating_exclude, placeholder: "e.g. bar, counter, patio", oninput: e => (d.seating_exclude = e.target.value) }));
  }
  const isSR = d.platform === "sevenrooms";
  if (isResy) out.push(sw("Auto-book", "Off = just push me a link", d.auto_book, x => (d.auto_book = x)));
  if (isSR) {
    out.push(sw("Auto-book (no-card tables)", "Card-required tables always get a link instead", d.auto_book, x => (d.auto_book = x)));
    if (state.status && state.status.platforms && !state.status.platforms.sevenrooms.guest) {
      out.push(el("a", { class: "notice", href: "#/settings", style: "display:block;text-decoration:none" }, "Add your booking details (name, email, phone) in Settings so SevenRooms can auto-book →"));
    }
  }

  // Release time
  const relIn = el("input", { type: "datetime-local", value: d.release_local, oninput: e => (d.release_local = e.target.value) });
  const relHint = el("div", { class: "muted small", style: "margin-top:6px" });
  async function suggestRelease() {
    if (!isResy || !d.dates.length) return;
    try {
      const { lead_time_days } = await api(`/api/resy/venue/${v.id}/lead-time`);
      if (lead_time_days) {
        const rd = addDays(d.dates[0], -lead_time_days);
        const t = (d.release_local && d.release_local.slice(11)) || "10:00";
        d.release_local = `${rd}T${t}`;
        relIn.value = d.release_local;
        relHint.textContent = `Resy says this venue books ${lead_time_days} days out. Release time of day varies (often 9:00, 10:00 or midnight) — check the venue page and adjust.`;
      }
    } catch (e) { relHint.textContent = `Couldn't look up lead time: ${e.message}`; }
  }
  if (d.mode === "snipe" || isOT) {
    out.push(el("div", { class: "section-title" }, "When do reservations open?"));
    out.push(el("label", {}, `Release date & time (${v.timezone || "venue local time"})`), relIn, relHint);
    if (isResy) out.push(el("button", { class: "btn ghost block", type: "button", style: "margin-top:8px", onclick: suggestRelease }, "Suggest from Resy's booking window"));
    if (!isOT) {
      out.push(el("label", {}, "Keep trying after release for"),
        selectEl([[20, "20 seconds"], [45, "45 seconds"], [90, "90 seconds"], [180, "3 minutes"]], d.snipe_window_seconds, x => (d.snipe_window_seconds = +x)));
      out.push(sw("Then watch for cancellations", "If the drop is missed, keep monitoring", d.fallback_to_monitor, x => { d.fallback_to_monitor = x; }));
    }
  }
  if (!isOT && (d.mode === "monitor" || d.fallback_to_monitor)) {
    out.push(el("div", { class: "section-title" }, "Cancellation watching"));
    out.push(el("label", {}, "Check about every"),
      selectEl([[60, "1 minute (aggressive)"], [120, "2 minutes"], [300, "5 minutes (gentle)"], [600, "10 minutes"], [1800, "30 minutes"]], d.poll_interval_seconds, x => (d.poll_interval_seconds = +x)));
    out.push(el("div", { class: "muted small", style: "margin-top:6px" }, "Randomized ±25%, slowed 4× overnight, faster in the final 48 hours."));
    out.push(el("label", {}, "Stop watching at (optional)"), el("input", { type: "datetime-local", value: d.monitor_until, oninput: e => (d.monitor_until = e.target.value) }));
  }
  out.push(el("label", {}, "Notes"), el("input", { type: "text", value: d.notes || "", oninput: e => (d.notes = e.target.value) }));

  const save = el("button", { class: "btn primary block", type: "button", style: "margin-top:18px" }, editId ? "Save changes" : (isOT ? "Set reminder" : d.mode === "snipe" ? "Schedule snipe" : "Start watching"));
  save.addEventListener("click", () => busy(save, async () => {
    if (!d.dates.length) throw new Error("Add at least one date");
    const payload = {
      platform: d.platform, mode: isOT ? "reminder" : d.mode,
      venue_id: String(v.id), venue_name: v.name, venue_slug: v.slug || null, venue_city: v.city || null,
      venue_tz: v.timezone || null, venue_url: v.url || null,
      party_size: d.party_size, dates: d.dates, time_start: d.time_start, time_end: d.time_end, ideal_time: d.ideal_time || null,
      seating_exclude: (d.seating_exclude || "").split(",").map(s => s.trim()).filter(Boolean),
      auto_book: (isResy || isSR) && d.auto_book,
      release_local: (d.mode === "snipe" || isOT) ? (d.release_local || null) : null,
      snipe_window_seconds: d.snipe_window_seconds, fallback_to_monitor: !isOT && d.fallback_to_monitor,
      poll_interval_seconds: d.poll_interval_seconds, monitor_until: d.monitor_until || null, notes: d.notes || null,
    };
    const r = editId
      ? await api(`/api/requests/${editId}`, { method: "PUT", body: payload })
      : await api("/api/requests", { method: "POST", body: payload });
    state.draft = null;
    toast(editId ? "Saved" : "Created");
    location.hash = `#/r/${r.id}`;
  }));
  out.push(save);
  return out;
}

function selectEl(options, value, onChange) {
  const s = el("select", { onchange: e => onChange(e.target.value) }, options.map(([v, l]) => el("option", { value: v }, l)));
  s.value = String(value);
  return s;
}

// ---------------------------------------------------------------- settings
async function renderSettings() {
  setTitle("Settings");
  const st = await api("/api/status");
  state.status = st;
  const resy = st.platforms.resy;
  const out = [];

  // Resy account
  out.push(el("div", { class: "section-title" }, "Resy account"));
  const rc = el("div", { class: "card" });
  const auth = resy.auth;
  rc.append(el("div", { class: "muted" }, auth.logged_in
    ? `Signed in${auth.name ? " as " + auth.name : ""}. Token valid until ${auth.token_expires ? new Date(auth.token_expires * 1000).toLocaleDateString() : "?"}. ${auth.payment_method_id ? "Card on file ✓" : "No card found — some venues need one."}`
    : auth.has_credentials ? "Credentials saved; not signed in yet." : "Not connected."));
  if (st.resy_from_env) {
    rc.append(el("div", { class: "muted small", style: "margin-top:6px" }, "Credentials come from server secrets (RESY_EMAIL / RESY_PASSWORD)."));
  } else {
    const em = el("input", { type: "email", placeholder: "Resy email", autocomplete: "username" });
    const pw = el("input", { type: "password", placeholder: "Resy password", autocomplete: "current-password" });
    const b = el("button", { class: "btn primary block", style: "margin-top:10px" }, auth.has_credentials ? "Update & sign in" : "Connect Resy");
    b.onclick = () => busy(b, async () => { await api("/api/settings/resy", { method: "POST", body: { email: em.value, password: pw.value } }); toast("Resy connected"); route(); });
    rc.append(el("label", {}, "Email"), em, el("label", {}, "Password"), pw, b,
      el("div", { class: "muted small", style: "margin-top:8px" }, "Stored encrypted on your server only. We sign in once and reuse Resy's refresh token, so your password is rarely used."));
  }
  const tb = el("button", { class: "btn" }, "Test connection");
  tb.onclick = () => busy(tb, async () => { const r = await api("/api/settings/resy/test", { method: "POST" }); toast(`OK — ${r.user.name || "signed in"}${r.user.payment_method_id ? ", card on file" : ""}`); });
  const btns = [tb];
  if (auth.has_credentials && !st.resy_from_env) {
    const db = el("button", { class: "btn danger" }, "Disconnect");
    db.onclick = () => confirm("Remove Resy credentials?") && busy(db, async () => { await api("/api/settings/resy", { method: "DELETE" }); route(); });
    btns.push(db);
  }
  rc.append(el("div", { class: "btns" }, btns));
  out.push(rc);

  // Guest details for SevenRooms
  out.push(el("div", { class: "section-title" }, "Your booking details (SevenRooms)"));
  const gc = el("div", { class: "card" });
  const guest = st.platforms.sevenrooms.guest;
  const gf = el("input", { type: "text", placeholder: "First name", autocomplete: "given-name" });
  const gl = el("input", { type: "text", placeholder: "Last name", autocomplete: "family-name" });
  const ge = el("input", { type: "email", placeholder: "Email", autocomplete: "email" });
  const gp = el("input", { type: "tel", placeholder: "Mobile number", autocomplete: "tel-national" });
  const gs = el("button", { class: "btn primary block", style: "margin-top:10px" }, guest ? "Update details" : "Save details");
  gs.onclick = () => busy(gs, async () => { await api("/api/settings/guest", { method: "POST", body: { first_name: gf.value, last_name: gl.value, email: ge.value, phone: gp.value } }); toast("Saved"); route(); });
  gc.append(...[
    el("div", { class: "muted" }, guest ? `Saved: ${guest.name} · ${guest.email} · ${guest.phone}` : "Not set. SevenRooms tables will be sent to you as links until you add these."),
    el("div", { class: "muted small", style: "margin-top:6px" }, "SevenRooms has no login. Bookings are a guest checkout using these details, and the restaurant emails your confirmation. Stored encrypted on your server. US numbers only for now."),
    el("div", { class: "grid2" }, el("div", {}, el("label", {}, "First name"), gf), el("div", {}, el("label", {}, "Last name"), gl)),
    el("label", {}, "Email"), ge, el("label", {}, "Mobile"), gp, gs,
  ]);
  out.push(gc);

  // Notifications
  out.push(el("div", { class: "section-title" }, "Push notifications (ntfy)"));
  const nc = el("div", { class: "card" });
  const topic = el("input", { type: "text", value: st.ntfy_topic || "", placeholder: "secret topic name", disabled: st.ntfy_from_env || null });
  const gen = el("button", { class: "btn ghost", type: "button", onclick: () => { topic.value = "rezzie-" + crypto.getRandomValues(new Uint32Array(3)).join("").slice(0, 18); } }, "Generate");
  const sb = el("button", { class: "btn primary" }, "Save");
  sb.onclick = () => busy(sb, async () => { await api("/api/settings/ntfy", { method: "POST", body: { topic: topic.value } }); toast("Saved"); route(); });
  const tst = el("button", { class: "btn" }, "Send test");
  tst.onclick = () => busy(tst, async () => { await api("/api/settings/ntfy/test", { method: "POST" }); toast("Sent — check your phone"); });
  nc.append(...[
    el("div", { class: "muted small" }, "1) Install the free “ntfy” app from the App Store. 2) Pick a hard-to-guess topic below and save. 3) In ntfy, tap + and subscribe to the same topic. Anyone who knows the topic can read alerts, so keep it random."),
    el("label", {}, "Topic"), el("div", { class: "row", style: "flex-wrap:nowrap" }, topic, st.ntfy_from_env ? null : gen),
    el("div", { class: "btns" }, st.ntfy_from_env ? el("span", { class: "muted small" }, "Set by server secret") : sb, tst),
    st.ntfy_topic ? el("a", { class: "btn ghost block", style: "margin-top:10px", href: `${st.ntfy_server.replace(/\/$/, "")}/${st.ntfy_topic}`, target: "_blank", rel: "noopener" }, "Open topic in ntfy") : null].filter(Boolean));
  out.push(nc);

  // Safety
  out.push(el("div", { class: "section-title" }, "Account safety"));
  const sc = el("div", { class: "card" });
  for (const [p, s] of Object.entries(st.platforms)) {
    sc.append(el("div", { class: "row between", style: "padding:6px 0" },
      el("span", {}, PLATFORM_NAMES[p]),
      el("span", { class: "muted small mono" }, `${s.calls_today}/${s.budget} calls today`)));
    const mins = sec => sec < 90 ? `${Math.round(sec)} sec` : `${Math.round(sec / 60)} min`;
    sc.append(el("div", { class: "muted small" },
      s.budget_exhausted ? `Monitor budget used up for today (${s.monitor_budget}). Monitors resume after midnight.`
        : s.active_monitors ? `${s.active_monitors} monitor${s.active_monitors > 1 ? "s" : ""} running · each checks about every ${mins(Math.max(s.pace_seconds || 0, 120))} in the daytime (budget spread until midnight, slower overnight)`
        : `No monitors running. ${s.monitor_budget} calls/day available to monitors; ${s.budget - s.monitor_budget} held back for snipes.`));
    const bIn = el("input", { type: "number", inputmode: "numeric", min: st.budget_min, max: st.budget_max, step: 100, value: s.budget, style: "max-width:120px" });
    const bBtn = el("button", { class: "btn ghost" }, "Set daily budget");
    bBtn.onclick = () => busy(bBtn, async () => {
      const v = parseInt(bIn.value, 10);
      if (v > 1500 && !confirm(`${v} calls/day is above the 1,500 I'd suggest. Higher means more chance ${PLATFORM_NAMES[p]} notices. Continue?`)) return;
      await api(`/api/settings/budget/${p}`, { method: "POST", body: { daily_budget: v } });
      toast("Budget updated"); route();
    });
    sc.append(el("div", { class: "row", style: "flex-wrap:nowrap;margin:8px 0 12px" }, bIn, bBtn));
    if (s.paused_until) {
      const rb = el("button", { class: "btn danger block" }, `Resume ${PLATFORM_NAMES[p]} now (not recommended)`);
      rb.onclick = () => confirm("Cooling off protects your account. Resume anyway?") && busy(rb, async () => { await api(`/api/platform/${p}/resume`, { method: "POST" }); route(); });
      sc.append(el("div", { class: "notice bad" }, `Paused until ${new Date(s.paused_until * 1000).toLocaleString()} — ${s.pause_reason}`), rb);
    }
  }
  sc.append(el("div", { class: "muted small", style: "margin-top:8px" },
    `Active monitors: ${st.active_monitors}/${st.max_active_monitors} · min interval ${st.min_poll_seconds}s · ` +
    `outbound proxy: ${st.proxy ? "on" : "off"}${st.dry_run ? " · DRY RUN" : ""}`));
  out.push(sc);

  // Activity log
  out.push(el("div", { class: "section-title" }, "Recent activity"));
  const ev = await api("/api/events?limit=60");
  out.push(el("div", { class: "card log" }, ev.length ? ev.map(e => el("div", { class: e.level }, el("span", { class: "ts" }, fmtWhen(e.ts)), e.message)) : el("div", { class: "muted" }, "Nothing yet.")));

  const lo = el("button", { class: "btn ghost block", style: "margin-top:12px" }, "Log out");
  lo.onclick = () => busy(lo, async () => { await api("/api/logout", { method: "POST" }); state.authed = false; route(); });
  out.push(lo);
  $view.replaceChildren(...out);
}

// ------------------------------------------------------------------ boot
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
route();
