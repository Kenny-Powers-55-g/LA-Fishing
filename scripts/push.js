// Phone notifications for everyone who turned them on (web push). Runs in the hourly workflow right after
// the deploy, so a tapped notification opens a site that already shows the same numbers.
//
// Each phone or computer that switched on notifications under My alerts has a row in Supabase
// (oc_push_devices, in supabase/setup.sql) holding a copy of its alert rules and the few settings that change
// their answers: starred spots, boat size, depth and tuning. For each device this script loads those settings
// into the page's own code and asks alertHits() in map.html what matches, exactly as the My alerts card does
// when the app is open. Then it sends whatever is new:
//   - at most one notification per alert, spot and day (an area alert: one a day for the whole area), and at
//     most one push per device per run (several new matches are listed together);
//   - fishing matches only for daylight windows the person's boat can go out in (alertHits already checks the
//     coming days; a "right now" match is skipped while it's a no-go for their boat, and offshore fishing
//     matches are skipped while a tropical storm threatens the coast);
//   - nothing between 9 PM and 6 AM Louisiana time except hurricane, tropical storm and storm surge warnings.
//     Anything held back goes out at the first run after 6 AM if it still holds;
//   - a one-time "Notifications are on" message on the first run after a device turns them on, so people
//     know it works.
//
//   node scripts/push.js
//
// Env: SUPABASE_SECRET_KEY  the Supabase project's secret key (sb_secret_...), a repository secret
//      VAPID_PRIVATE_KEY    from `node scripts/webpush.js keys`, a repository secret (its public half is pushKey in config.js)
//      SITE_URL             the live site, used as the contact address push services ask for
//      PUSH_DIR             folder with latest.json, forecast.json and community.json (default: current folder)
// Without the secrets or config.js pushKey it says what's missing and sends nothing.
process.env.TZ = "America/Chicago";      // the page code reads local time (weekend alerts, "today", hours)
const fs = require("fs"), path = require("path"), vm = require("vm");
const wp = require("./webpush");
const ROOT = path.join(__dirname, ".."), TZ = "America/Chicago", H = 36e5, DAY = 24 * H;
const KEEP_DAYS = 8;             // remember what was sent this long (longer than any alert looks ahead)
const MAX_FAILS = 10;            // refused sends in a row before a device is dropped
const PARALLEL = 8;              // notifications sent at once
const BUDGET_MS = 6 * 60e3;      // stop checking devices after this; the rest wait for the next run
const QUIET_FROM = 21, QUIET_TO = 6;
const NIGHT_OK = /Hurricane|Tropical Storm|Storm Surge|Extreme Wind/i;   // worth waking someone for
const URGENT = /Warning|Hurricane|Storm Surge|Extreme Wind/i;
const SEVERE = /Warning|Hurricane|Tropical Storm/i;
const NAMES = ["SP", "alertHits", "boatNow", "setCommunity", "setFC", "setDATA", "setTune", "setCommOn", "resetAlerts"];

const fmt = (ts, o) => new Intl.DateTimeFormat("en-US", { timeZone: TZ, ...o }).format(ts);
const dayKey = ts => new Intl.DateTimeFormat("en-CA", { timeZone: TZ }).format(ts);
const hourOf = ts => +new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", hourCycle: "h23" }).format(ts) % 24;
const hourTxt = ts => fmt(ts, { hour: "numeric" });
const dayWord = (ts, now) => dayKey(ts) === dayKey(now) ? "today" : dayKey(ts) === dayKey(now + DAY) ? "tomorrow" : fmt(ts, { weekday: "short" });
const isQuietHour = now => { const h = hourOf(now); return h >= QUIET_FROM || h < QUIET_TO; };
const events = spot => [...new Set((spot.alerts || []).map(a => a.event).filter(Boolean))];
const listTxt = a => a.length <= 2 ? a.join(" and ") : `${a.slice(0, -1).join(", ")} and ${a[a.length - 1]}`;

function readConfig() {
  const ctx = { window: {} };
  vm.runInNewContext(fs.readFileSync(path.join(ROOT, "config.js"), "utf8"), ctx);
  const c = ctx.window.OC_CONFIG || {};
  return { url: c.supabaseUrl ? new URL(c.supabaseUrl).origin : "", pushKey: String(c.pushKey || "").trim() };
}

// ---- What a device's saved copy may hold. It came from a browser, so every part is checked again here. ----
const ID_RE = /^[A-Za-z0-9_-]{1,40}$/, F_RE = /^[a-z]{2,10}$/, K_RE = /^[a-z]{2,20}$/;
function deviceSettings(watch, spotIds) {
  const w = watch && typeof watch === "object" && !Array.isArray(watch) ? watch : {};
  const m = new Map();
  m.set("oc-alerts", JSON.stringify(Array.isArray(w.rules) ? w.rules.slice(0, 20) : []));   // the page's cleanAlert() checks each rule
  m.set("la-fish-favs", JSON.stringify(Array.isArray(w.favs) ? w.favs.filter(x => typeof x === "string" && ID_RE.test(x) && spotIds.has(x)).slice(0, 200) : []));
  if (typeof w.boat === "string" && /^[a-z]{1,10}$/.test(w.boat)) m.set("boat", w.boat);           // the page's boat() checks the name
  if (typeof w.depth === "number" && isFinite(w.depth) && w.depth >= 0 && w.depth <= 1000) m.set("la-fish-depth", String(w.depth));
  return m;
}
function cleanTune(t) {
  if (!t || typeof t !== "object") return null;
  const out = { fish: {}, group: {} };
  for (const part of ["fish", "group"]) {
    const src = t[part];
    if (!src || typeof src !== "object") continue;
    for (const [k, v] of Object.entries(src).slice(0, 60)) {
      if (!K_RE.test(k) || !v || typeof v !== "object") continue;
      const o = {};                         // an empty {} is kept: it means "enough trips, nothing to change"
      for (const [f, x] of Object.entries(v).slice(0, 30)) if (F_RE.test(f) && typeof x === "number" && x >= 0.5 && x <= 1.5) o[f] = x;
      out[part][k] = o;
    }
  }
  return out;
}

// One alert match -> the key that stops it repeating. Fishing: per alert, spot and day of the window (an
// area alert counts the whole area as one). Warnings: per alert, spot, event and the warning's end, so an
// extended warning is sent again but one that just carries on isn't.
function hitKey(h) {
  const r = h.rule, where = r.where.startsWith("area:") ? "area" : h.spot.id;
  if (h.kind === "warn") {
    const ends = (h.spot.alerts || []).map(a => a.ends).filter(Boolean).sort().pop();
    return `${r.id}|w|${where}|${events(h.spot).sort().join("+")}|${ends ? String(ends).slice(0, 13) : dayKey(h.ts)}`;
  }
  return `${r.id}|s|${where}|${dayKey(h.ts)}`;
}

// The notification for one match: {title, body, url, tag, ttl (seconds), urgency, night}
function messageFor(h, api, watch, now) {
  const spot = h.spot, evs = events(spot);
  if (h.kind === "warn") {
    const ends = (spot.alerts || []).map(a => Date.parse(a.ends)).filter(isFinite).sort((a, b) => b - a)[0];
    const fish = watch && typeof watch.fish === "string" && api.SP[watch.fish] ? watch.fish : "redfish";
    return { title: `⚠️ ${listTxt(evs)}`, short: `⚠️ ${listTxt(evs)} at ${spot.name}`, url: `map.html#${fish}/${spot.id}`,
      body: `In effect at ${spot.name}${ends ? ` until ${dayWord(ends, now)} ${hourTxt(ends)}` : ""}. Check the NWS marine forecast before you go.`,
      ttl: ends ? Math.min(DAY, Math.max(H, ends - now)) / 1000 : 6 * 3600, urgency: URGENT.test(evs.join(" ")) ? "high" : "normal",
      night: NIGHT_OK.test(evs.join(" ")) };
  }
  const fish = api.SP[h.rule.sp].name, url = `map.html#${h.rule.sp}/${spot.id}`;
  const sev = evs.filter(e => SEVERE.test(e)), tr = (api.__data && api.__data.tropical) || null;
  if (tr && tr.rank >= 5 && !sev.length) sev.push(`a tropical ${tr.level}`);
  const caveat = sev.length ? ` But ${listTxt(sev)} in effect: check before going.` : "";
  if (h.kind === "now") {
    const c = spot.conditions || {};
    const bits = [c.wind != null ? (c.wind < 1 ? "calm wind" : `wind ${Math.round(c.wind)} mph`) : "", c.tide && c.tide !== "Slack" ? `${String(c.tide).toLowerCase()} tide` : "",
                  c.temp != null ? `water ${Math.round(c.temp)}°F` : ""].filter(Boolean).join(", ");
    return { title: `${fish} at ${spot.name}: ${h.v} now`, short: `${fish} at ${spot.name}: ${h.v} now`, url, ttl: 2 * 3600, urgency: "normal",
      body: `${bits ? bits[0].toUpperCase() + bits.slice(1) + "." : "Good right now."}${h.lv === 1 ? " Use caution in your boat." : ""}${caveat}` };
  }
  return { title: `${fish} at ${spot.name}: about ${h.v}`, short: `${fish} at ${spot.name}: about ${h.v}, ${dayWord(h.ts, now)} ${hourTxt(h.ts)}`, url, urgency: "normal",
    body: `Best window ${dayWord(h.ts, now)} ${hourTxt(h.ts)}–${hourTxt(h.ts + 3 * H)}.${caveat}`,
    ttl: Math.min(2 * DAY, Math.max(H, h.ts + 3 * H - now)) / 1000 };
}

// Everything new for one device this run: [{key, msg}], plus whether something was held for quiet hours
function newFor(api, store, dev, spotIds, now) {
  const w = dev.watch && typeof dev.watch === "object" ? dev.watch : {};
  store.clear();
  for (const [k, v] of deviceSettings(w, spotIds)) store.set(k, v);
  api.setTune(cleanTune(w.tune));
  api.setCommOn(w.comm !== false);
  api.resetAlerts();
  const sent = dev.sent || {}, quiet = isQuietHour(now), tr = api.__data && api.__data.tropical, out = [];
  let held = false;
  for (const h of api.alertHits()) {
    if (!h || !h.rule || !h.spot || !h.ts) continue;
    if (h.kind !== "warn") {
      if (!api.SP[h.rule.sp]) continue;
      if (h.spot.zone === "offshore" && tr && tr.rank >= 4) continue;            // a fishing alert during a storm threat would be bad advice
      if (h.kind === "now") {
        const b = api.boatNow(h.spot, { ...h.spot.conditions, zone: h.spot.zone, alerts: events(h.spot).join("|") });
        if (b && b.lv === 0) continue;                                         // no-go for their boat right now
        h.lv = b ? b.lv : null;
      }
    }
    const key = hitKey(h);
    if (sent[key] || out.some(x => x.key === key)) continue;
    const msg = messageFor(h, api, w, now);
    if (quiet && !(h.kind === "warn" ? msg.night : h.rule.quiet === false)) { held = true; continue; }
    out.push({ key, msg });
  }
  return { items: out, held };
}

// One push per device: a single match as it is, several listed together
function combine(items, first) {
  if (!items.length) return first ? { title: "Notifications are on", url: "map.html", tag: "oc-hello", ttl: 12 * 3600, urgency: "normal",
    body: "OceanCast will tell you here when one of your alerts matches. Change them any time under My alerts." } : null;
  if (items.length === 1) return { ...items[0].msg, tag: "oc-" + items[0].key.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40) };
  const lines = items.slice(0, 4).map(x => x.msg.short || x.msg.title);
  return { title: `${items.length} OceanCast alerts`, url: "map.html", tag: "oc-alerts",
    body: lines.join("\n") + (items.length > 4 ? `\nand ${items.length - 4} more` : ""),
    ttl: Math.min(...items.map(x => x.msg.ttl)), urgency: items.some(x => x.msg.urgency === "high") ? "high" : "normal" };
}

// ---- Supabase, with the secret key: the device list and what was sent ----
function sbHeaders(key) {
  const h = { apikey: key, "Content-Type": "application/json" };
  if (/^eyJ/.test(key)) h.Authorization = `Bearer ${key}`;   // an older "service_role" key is a JWT and goes here too
  return h;
}
async function listDevices(url, key, fetchFn) {
  const out = [];
  let last = null;
  for (let page = 0; page < 500; page++) {
    const q = new URLSearchParams({ select: "device_id,endpoint,p256dh,auth,watch,sent,fails", order: "device_id.asc", limit: "1000" });
    if (last) q.set("device_id", `gt.${last}`);
    const r = await fetchFn(`${url}/rest/v1/oc_push_devices?${q}`, { headers: sbHeaders(key), signal: AbortSignal.timeout(30000) });
    if (!r.ok) throw new Error(`Supabase answered ${r.status} for the device list${r.status === 404 ? " (run supabase/setup.sql again to add notifications)" : r.status === 401 || r.status === 403 ? " (check the SUPABASE_SECRET_KEY secret)" : ""}`);
    const rows = await r.json();
    if (!Array.isArray(rows)) throw new Error("Supabase sent something other than a list of devices");
    out.push(...rows);
    if (rows.length < 1000) break;
    last = rows[rows.length - 1].device_id;
  }
  return out;
}
async function mark(url, key, sent, gone, fetchFn) {
  for (let i = 0; i < Math.max(sent.length, gone.length, 1); i += 500) {
    const r = await fetchFn(`${url}/rest/v1/rpc/oc_push_mark`, { method: "POST", headers: sbHeaders(key),
      body: JSON.stringify({ p_sent: sent.slice(i, i + 500), p_gone: gone.slice(i, i + 500) }), signal: AbortSignal.timeout(30000) });
    if (!r.ok) throw new Error(`Supabase answered ${r.status} when saving what was sent`);
  }
}

async function pool(items, n, fn) {
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => { while (i < items.length) { const x = items[i++]; await fn(x); } }));
}

async function run({ env = process.env, dir = env.PUSH_DIR || process.cwd(), fetchFn = fetch, now = Date.now(), log = console.log, config = readConfig() } = {}) {
  const secret = (env.SUPABASE_SECRET_KEY || "").trim(), priv = (env.VAPID_PRIVATE_KEY || "").trim();
  const missing = [!config.url && "supabaseUrl in config.js", !config.pushKey && "pushKey in config.js",
    !secret && "the SUPABASE_SECRET_KEY secret", !priv && "the VAPID_PRIVATE_KEY secret"].filter(Boolean);
  if (missing.length) { log(`push: notifications aren't set up yet (missing ${missing.join(", ")}); see README "Phone notifications".`); return { sent: 0, skipped: true }; }
  const site = (env.SITE_URL || "").replace(/\/$/, "");
  const signer = wp.vapid(priv, /^https:\/\//.test(site) ? site : "https://github.com/Kenny-Powers-55-g/LA-Fishing");
  if (signer.publicKey !== config.pushKey.replace(/=+$/, "")) throw new Error("VAPID_PRIVATE_KEY doesn't belong to pushKey in config.js; use the pair `node scripts/webpush.js keys` printed together");
  const read = f => { try { return JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")); } catch (e) { return null; } };
  const latest = read("latest.json"), fc = read("forecast.json");
  if (!latest || !Array.isArray(latest.spots) || !fc) { log("push: latest.json or forecast.json missing; nothing checked this run."); return { sent: 0 }; }

  const devices = await listDevices(config.url, secret, fetchFn);
  if (!devices.length) { log("push: no devices have notifications on yet."); return { sent: 0 }; }
  const store = new Map(), api = require("./scoring")(NAMES, { storage: store });
  // The page starts its own load() when its code runs; in the sandbox that fails at once (no network) and stops
  // because no data is loaded. Let it finish before handing over the data, or it would go on to draw the page.
  await new Promise(r => setTimeout(r, 20));
  api.setCommunity(read("community.json"));     // the same community tuning the page uses
  api.setFC(fc);
  api.setDATA(latest);
  api.__data = latest;
  const spotIds = new Set(latest.spots.filter(s => !s.user).map(s => s.id));

  const t0 = Date.now(), today = dayKey(now), todo = [], marks = [], gone = [];
  let checked = 0, held = 0, broken = 0, bad = 0;
  for (const dev of devices.sort(() => Math.random() - 0.5)) {    // a random order, so a cut-short run doesn't always miss the same ones
    if (Date.now() - t0 > BUDGET_MS) break;
    if (!wp.endpointOk(dev.endpoint)) { bad++; gone.push({ device_id: dev.device_id, endpoint: dev.endpoint }); continue; }
    const sent = {};
    for (const [k, v] of Object.entries(dev.sent && typeof dev.sent === "object" ? dev.sent : {}))
      if (typeof v === "string" && Date.parse(v) > now - KEEP_DAYS * DAY) sent[k] = v;
    const pruned = Object.keys(sent).length !== Object.keys(dev.sent || {}).length;
    let r;
    try { r = newFor(api, store, { ...dev, sent }, spotIds, now); } catch (e) { broken++; continue; }
    checked++;
    if (r.held) held++;
    const msg = combine(r.items, !sent.hello);
    if (msg) todo.push({ dev, sent, msg, keys: [...r.items.map(x => x.key), "hello"] });
    else if (pruned) marks.push({ device_id: dev.device_id, sent, fails: dev.fails || 0, ok: false });
  }

  let ok = 0, failed = 0;
  const why = new Map();
  await pool(todo, PARALLEL, async t => {
    const { tag, ttl, urgency, title, body, url } = t.msg;
    const res = await wp.send(t.dev, { title, body, url, tag }, signer, { ttl, urgency, fetchFn });
    if (res.ok) {
      ok++;
      for (const k of t.keys) if (!t.sent[k]) t.sent[k] = today;
      marks.push({ device_id: t.dev.device_id, sent: t.sent, fails: 0, ok: true });
    } else if (res.gone) gone.push({ device_id: t.dev.device_id, endpoint: t.dev.endpoint });
    else {
      failed++;
      if (!why.has(res.status)) why.set(res.status, res.why || "");
      const fails = res.retry ? t.dev.fails || 0 : (t.dev.fails || 0) + 1;
      if (fails >= MAX_FAILS) gone.push({ device_id: t.dev.device_id, endpoint: t.dev.endpoint });
      else marks.push({ device_id: t.dev.device_id, sent: t.sent, fails, ok: false });
    }
  });
  if (marks.length || gone.length) await mark(config.url, secret, marks, gone, fetchFn);
  const left = devices.length - checked - bad - broken;
  log(`push: ${devices.length} device(s), ${checked} checked${left > 0 ? ` (${left} left for the next run)` : ""}; ` +
      `${ok} notification(s) sent, ${failed} failed, ${gone.length} device(s) removed${held ? `, ${held} held for quiet hours` : ""}${broken ? `, ${broken} couldn't be checked` : ""}.`);
  for (const [s, w] of why) log(`push: the push service answered ${s || "nothing"}${w ? `: ${w}` : ""}`);
  // A run where every send was refused points at a key problem, not at one phone: make it visible
  if (failed && !ok && failed >= 3) throw new Error(`every notification was refused (${[...why.keys()].join(", ")}); check the VAPID keys`);
  return { sent: ok, failed, gone: gone.length, checked, held };
}

module.exports = { run, hitKey, messageFor, combine, deviceSettings, cleanTune, isQuietHour, newFor };
if (require.main === module) run().then(() => process.exit(0), e => { console.error("push: " + e.message); process.exit(1); });
