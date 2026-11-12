// Phone alerts. Runs in the hourly workflow after latest.json and forecast.json are built.
// For each rule in alerts.json it finds the best 3-hour daylight window in the next 48 hours
// using the site's own scoring code (from index.html), and sends one ntfy notification per
// rule, spot and day when the score reaches min_score. It also sends safety alerts (NWS
// warnings at your spots, forecast wind/seas over your limits, tropical storms) set under
// "safety" in alerts.json.
//
//   node scripts/alerts.js          (needs NTFY_TOPIC to send; without it, prints a dry run)
//
// Env: NTFY_TOPIC  ntfy topic to publish to (a repository secret)
//      SITE_URL    link opened when the notification is tapped
//      ALERTS_DIR  folder with latest.json, forecast.json, alerts.json (default: current folder)
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.join(__dirname, ".."), DIR = process.env.ALERTS_DIR || process.cwd();
const TZ = "America/Chicago", H = 36e5;

function loadScoring() {
  const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
  const noop = () => [];
  const el = () => new Proxy({ style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} } },
    { get: (t, k) => (k in t ? t[k] : noop), set: (t, k, v) => ((t[k] = v), true) });
  const ctx = {
    document: { getElementById: el, querySelector: el, querySelectorAll: () => [], createElement: el,
                addEventListener() {}, body: el(), hidden: false },
    navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => Promise.reject(new Error("no network in alerts")), setTimeout, clearTimeout,
    setInterval: () => 0, console, location: { hash: "", href: "", hostname: "", pathname: "/" },
    history: { replaceState() {} }, addEventListener() {}, innerWidth: 1000, matchMedia: () => ({ matches: false }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  // the page's top-level consts aren't visible from outside, so hand the pieces we need back
  vm.runInContext(code + "\n;globalThis.__api={SP,score,scoreC,fcConditions,lightNow};", ctx, { filename: "index.html" });
  return ctx.__api;
}

const readJSON = (f, d) => { try { return JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")); } catch (e) { return d; } };
const dayName = ts => new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short" }).format(ts);
const dayKey = ts => new Intl.DateTimeFormat("en-CA", { timeZone: TZ }).format(ts);
const hourTxt = ts => new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric" }).format(ts);

function bestWindow(api, spId, spot, f, now, days) {
  const t0 = Date.parse(f.start), hrs = [];
  for (let i = 0; i < f.temp.length; i++) {
    const ts = t0 + i * H;
    if (ts < now - H / 2) continue;
    if (ts > now + 48 * H) break;
    const c = api.fcConditions(spot, f, i, ts, null);
    const r = c.light === "Night" ? null : api.scoreC(spId, c, spot.quality || {}, ts);
    hrs.push({ ts, v: r && !r.gate ? r.total : null, c });
  }
  let best = null;
  for (let i = 0; i + 2 < hrs.length; i++) {
    const w = hrs.slice(i, i + 3);
    if (w.some(x => x.v == null) || w[2].ts - w[0].ts !== 2 * H) continue;
    if (days && days.length && !days.includes(dayName(w[0].ts))) continue;
    const v = Math.round((w[0].v + w[1].v + w[2].v) / 3);
    if (!best || v > best.v) best = { v, from: w[0].ts, to: w[0].ts + 3 * H, c: w[1].c };
  }
  return best;
}

// ---- Safety alerts: NWS warnings at your spots, forecast wind and seas, tropical storms ----
const SAFETY_EVENTS = /Small Craft|Gale|Storm|Hurricane|Tropical|Special Marine|Hazardous Seas|Marine Weather Statement|Waterspout|Tornado|Severe Thunderstorm|Dense Fog|Coastal Flood|Storm Surge|Rip Current|High Surf|Flash Flood|Extreme Wind/i;
const URGENT = /Warning|Hurricane|Storm Surge|Extreme Wind/i;
const TROP_LEVELS = ["none", "far", "gulf", "near", "cone", "watch", "warning"];
const untilTxt = t => t ? ` until ${dayName(Date.parse(t))} ${hourTxt(Date.parse(t))}` : "";
const list = names => names.length <= 4 ? names.join(", ") : `${names.slice(0, 3).join(", ")} and ${names.length - 3} more`;

// Returns the notifications to send, each {key, title, body, priority, tags, link}.
function safetyMessages(cfg, latest, fc, rules, now, site) {
  const s = cfg.safety;
  if (!s || s.enabled === false) return [];
  const all = latest.spots.map(x => x.id);
  let ids = s.spots && s.spots.length ? (s.spots.includes("*") ? all : s.spots)
    : [...new Set(rules.flatMap(r => !r.spots || r.spots.includes("*") ? all : r.spots))];
  if (!ids.length) ids = all;
  const spots = ids.map(id => latest.spots.find(x => x.id === id)).filter(Boolean);
  const out = [], day = dayKey(now), link = site ? `${site}/` : "";
  // 1. NWS alerts in effect at those spots, one message per event type
  if (s.nws !== false) {
    const re = s.events && s.events.length ? new RegExp(s.events.join("|"), "i") : SAFETY_EVENTS, by = {};
    for (const sp of spots) for (const a of sp.alerts || []) {
      if (!a.event || !re.test(a.event)) continue;
      (by[a.event] = by[a.event] || { spots: [], ends: a.ends }).spots.push(sp);
      if (a.ends && (!by[a.event].ends || a.ends > by[a.event].ends)) by[a.event].ends = a.ends;
    }
    for (const [ev, g] of Object.entries(by)) {
      out.push({ keys: g.spots.map(sp => `nws|${ev}|${sp.id}|${(g.ends || day).slice(0, 10)}`), title: `⚠️ ${ev}`,
        body: `${ev} in effect for ${list(g.spots.map(x => x.name))}${untilTxt(g.ends)}. Check the NWS marine forecast before you go.`,
        priority: URGENT.test(ev) ? "high" : "default", tags: "warning", link });
    }
  }
  // 2. Forecast wind or seas over your limits in the next 48 hours
  const windLim = s.wind_mph, waveLim = s.wave_ft;
  if (windLim || waveLim) {
    for (const [k, lim, unit, label] of [["wind", windLim, "mph", "Wind"], ["wave", waveLim, "ft", "Seas"]]) {
      if (!lim) continue;
      const hits = [];
      for (const sp of spots) {
        const f = fc.spots && fc.spots[sp.id];
        if (!f || !f[k]) continue;
        const t0 = Date.parse(f.start);
        let first = null, max = null;
        for (let i = 0; i < f[k].length; i++) {
          const ts = t0 + i * H, v = f[k][i];
          if (ts < now - H / 2 || v == null) continue;
          if (ts > now + 48 * H) break;
          if (v >= lim) { if (first == null) first = ts; if (max == null || v > max) max = v; }
        }
        if (first != null) hits.push({ sp, first, max });
      }
      if (!hits.length) continue;
      hits.sort((a, b) => a.first - b.first);
      const first = hits[0].first, max = Math.max(...hits.map(h => h.max));
      out.push({ keys: hits.map(h => `${k}|${h.sp.id}|${dayKey(h.first)}|${lim}`), title: `${label} over ${lim} ${unit} coming`,
        body: `${label} forecast to reach ${Math.round(max)} ${unit} from ${dayName(first)} ${hourTxt(first)} at ${list(hits.map(h => h.sp.name))}. Plan around it.`,
        priority: "default", tags: k === "wind" ? "dash" : "ocean", link });
    }
  }
  // 3. Tropical storms threatening the Louisiana coast
  const tr = latest.tropical, min = TROP_LEVELS.indexOf(s.tropical || "near");
  if (tr && min > 0 && tr.rank >= min) {
    const names = (tr.storms || []).map(x => x.name).join("+") || "outlook";
    out.push({ keys: [`trop|${names}|${tr.level}|${day}`], title: `🌀 Tropical: ${tr.level === "gulf" ? "storm in the Gulf" : tr.level}`,
      body: tr.text, priority: tr.rank >= 4 ? "high" : "default", tags: "cyclone", link: site ? `${site}/#tropical` : "" });
  }
  return out;
}

async function send(topic, m) {
  const res = await fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, { method: "POST", body: m.body,
    headers: { Title: m.title, Tags: m.tags || "fish", Priority: m.priority || "default", ...(m.link ? { Click: m.link } : {}) } });
  if (!res.ok) throw new Error(`ntfy answered ${res.status}`);
}

async function main() {
  const cfg = readJSON("alerts.json", null) || readJSON(path.join("..", "alerts.json"), null)
    || JSON.parse(fs.readFileSync(path.join(ROOT, "alerts.json"), "utf8"));
  const rules = (cfg.rules || []).filter(r => r && r.enabled !== false);
  const safetyOn = cfg.safety && cfg.safety.enabled !== false;
  if (!rules.length && !safetyOn) { console.log("No enabled alert rules or safety alerts in alerts.json."); return; }
  const latest = readJSON("latest.json", null), fc = readJSON("forecast.json", null);
  if (!latest || !fc) { console.log("latest.json or forecast.json missing; no alerts this run."); return; }
  const now = Date.now();
  const state = readJSON("alerts_state.json", { sent: {} });
  for (const k of Object.keys(state.sent)) if (now - Date.parse(state.sent[k]) > 8 * 24 * H) delete state.sent[k];
  const topic = process.env.NTFY_TOPIC, site = (process.env.SITE_URL || "").replace(/\/$/, "");
  const recordDry = process.env.ALERTS_RECORD_DRY === "1";
  let sent = 0;
  const deliver = async (keys, m) => {
    if (!topic) {
      console.log(`[dry run] ${m.title} — ${m.body}`);
      if (recordDry) { for (const k of keys) state.sent[k] = new Date().toISOString(); sent++; }
      return;
    }
    try { await send(topic, m); for (const k of keys) state.sent[k] = new Date().toISOString(); sent++; console.log(`Sent: ${m.title} — ${m.body}`); }
    catch (e) { console.log(`Couldn't send "${m.title}": ${e.message}`); }
  };
  // Safety first: these go out even if no fishing rule is set
  for (const m of safetyMessages(cfg, latest, fc, rules, now, site)) {
    const fresh = m.keys.filter(k => !state.sent[k]);
    if (fresh.length) await deliver(m.keys, m);
  }
  const api = rules.length ? loadScoring() : null;
  for (const [ri, rule] of rules.entries()) {
    const sp = api.SP[rule.species];
    if (!sp) { console.log(`Rule ${ri + 1}: unknown species "${rule.species}"`); continue; }
    const ids = !rule.spots || rule.spots.includes("*") ? latest.spots.map(s => s.id) : rule.spots;
    for (const id of ids) {
      const spot = latest.spots.find(s => s.id === id), f = fc.spots && fc.spots[id];
      if (!spot || !f) { if (rule.spots && !rule.spots.includes("*")) console.log(`Rule ${ri + 1}: no data for spot "${id}"`); continue; }
      const tr = latest.tropical;
      if (spot.zone === "offshore" && tr && tr.rank >= 4) { console.log(`Rule ${ri + 1}: ${spot.name} skipped, tropical threat (${tr.level})`); continue; }
      const w = bestWindow(api, rule.species, spot, f, now, rule.days);
      if (!w || w.v < (rule.min_score ?? 75)) continue;
      const key = `${rule.species}|${id}|${dayKey(w.from)}|${rule.min_score ?? 75}`;
      if (state.sent[key]) continue;
      const when = `${dayName(w.from)} ${hourTxt(w.from)}–${hourTxt(w.to)}`;
      const bits = [w.c.wind != null ? `wind ${w.c.wind} mph` : "", w.c.tide && w.c.tide !== "Slack" ? `${w.c.tide.toLowerCase()} tide` : "",
                    w.c.temp != null ? `water ${w.c.temp}°F` : ""].filter(Boolean).join(", ");
      const title = `${sp.name} at ${spot.name}: ${w.v}`;
      const body = `Best window ${when} (score about ${w.v}).${bits ? " " + bits[0].toUpperCase() + bits.slice(1) + "." : ""}`;
      // a fishing alert during a severe marine warning would be bad advice
      const sev = (spot.alerts || []).filter(a => /Warning|Hurricane|Tropical Storm/i.test(a.event || "")).map(a => a.event);
      if (tr && tr.rank >= 5 && !sev.length) sev.push(`a tropical ${tr.level}`);
      await deliver([key], { title, body: body + (sev.length ? ` But ${sev.join(", ")} in effect: check before going.` : ""),
        tags: "fish", link: site ? `${site}/#${rule.species}/${id}` : "" });
    }
  }
  fs.writeFileSync(path.join(DIR, "alerts_state.json"), JSON.stringify(state));
  console.log(`${sent} alert(s) ${topic ? "sent" : "recorded (dry run)"}; ${Object.keys(state.sent).length} remembered so repeats are skipped.`);
}
main().catch(e => { console.error(e); process.exit(1); });
