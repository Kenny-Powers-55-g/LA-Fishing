// Phone alerts. Runs in the hourly workflow after latest.json and forecast.json are built.
// For each rule in alerts.json it finds the best 3-hour daylight window in the next 48 hours
// using the site's own scoring code (from index.html), and sends one ntfy notification per
// rule, spot and day when the score reaches min_score.
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

async function main() {
  const cfg = readJSON("alerts.json", null) || readJSON(path.join("..", "alerts.json"), null)
    || JSON.parse(fs.readFileSync(path.join(ROOT, "alerts.json"), "utf8"));
  const rules = (cfg.rules || []).filter(r => r && r.enabled !== false);
  if (!rules.length) { console.log("No enabled alert rules in alerts.json."); return; }
  const latest = readJSON("latest.json", null), fc = readJSON("forecast.json", null);
  if (!latest || !fc) { console.log("latest.json or forecast.json missing; no alerts this run."); return; }
  const api = loadScoring(), now = Date.now();
  const state = readJSON("alerts_state.json", { sent: {} });
  for (const k of Object.keys(state.sent)) if (now - Date.parse(state.sent[k]) > 8 * 24 * H) delete state.sent[k];
  const topic = process.env.NTFY_TOPIC, site = (process.env.SITE_URL || "").replace(/\/$/, "");
  const recordDry = process.env.ALERTS_RECORD_DRY === "1";
  let sent = 0;
  for (const [ri, rule] of rules.entries()) {
    const sp = api.SP[rule.species];
    if (!sp) { console.log(`Rule ${ri + 1}: unknown species "${rule.species}"`); continue; }
    const ids = !rule.spots || rule.spots.includes("*") ? latest.spots.map(s => s.id) : rule.spots;
    for (const id of ids) {
      const spot = latest.spots.find(s => s.id === id), f = fc.spots && fc.spots[id];
      if (!spot || !f) { if (rule.spots && !rule.spots.includes("*")) console.log(`Rule ${ri + 1}: no data for spot "${id}"`); continue; }
      const w = bestWindow(api, rule.species, spot, f, now, rule.days);
      if (!w || w.v < (rule.min_score ?? 75)) continue;
      const key = `${rule.species}|${id}|${dayKey(w.from)}|${rule.min_score ?? 75}`;
      if (state.sent[key]) continue;
      const when = `${dayName(w.from)} ${hourTxt(w.from)}–${hourTxt(w.to)}`;
      const bits = [w.c.wind != null ? `wind ${w.c.wind} mph` : "", w.c.tide && w.c.tide !== "Slack" ? `${w.c.tide.toLowerCase()} tide` : "",
                    w.c.temp != null ? `water ${w.c.temp}°F` : ""].filter(Boolean).join(", ");
      const title = `${sp.name} at ${spot.name}: ${w.v}`;
      const body = `Best window ${when} (score about ${w.v}).${bits ? " " + bits[0].toUpperCase() + bits.slice(1) + "." : ""}`;
      const link = site ? `${site}/#${rule.species}/${id}` : "";
      if (!topic) {
        console.log(`[dry run] ${title} — ${body}`);
        if (recordDry) { state.sent[key] = new Date().toISOString(); sent++; }
        continue;
      }
      try {
        const res = await fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, { method: "POST", body,
          headers: { Title: title, Tags: "fish", ...(link ? { Click: link } : {}) } });
        if (!res.ok) throw new Error(`ntfy answered ${res.status}`);
        state.sent[key] = new Date().toISOString(); sent++;
        console.log(`Sent: ${title} — ${body}`);
      } catch (e) { console.log(`Couldn't send "${title}": ${e.message}`); }
    }
  }
  fs.writeFileSync(path.join(DIR, "alerts_state.json"), JSON.stringify(state));
  console.log(`${sent} alert(s) ${topic ? "sent" : "recorded (dry run)"}; ${Object.keys(state.sent).length} remembered so repeats are skipped.`);
}
main().catch(e => { console.error(e); process.exit(1); });
