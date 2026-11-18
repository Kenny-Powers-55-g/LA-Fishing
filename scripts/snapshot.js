// Daily score snapshot for the "trend vs yesterday" numbers on the Today tab.
// Runs in the hourly workflow after latest.json is built. It scores every public spot for every fish
// with the site's own scoring code (from map.html, the same loader scripts/alerts.js uses) and writes
// scores-daily.json: { "YYYY-MM-DD": { "<spot id>|<fish>": score } }, keeping the last 3 days.
// Each run overwrites today's entry, so the value kept for a day is its latest one.
//
//   node scripts/snapshot.js        (reads and writes in the current folder, or SNAPSHOT_DIR)
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.join(__dirname, ".."), DIR = process.env.SNAPSHOT_DIR || process.cwd();
const KEEP = 3;

function loadScoring() {
  const html = fs.readFileSync(path.join(ROOT, "map.html"), "utf8");
  const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
  if (!code.trim()) throw new Error("No scripts found in map.html");
  const noop = () => [];
  const el = () => new Proxy({ style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} } },
    { get: (t, k) => (k in t ? t[k] : noop), set: (t, k, v) => ((t[k] = v), true) });
  const ctx = {
    document: { getElementById: el, querySelector: el, querySelectorAll: () => [], createElement: el,
                addEventListener() {}, body: el(), hidden: false },
    navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => Promise.reject(new Error("no network in snapshot")), setTimeout: () => 0, clearTimeout() {},
    setInterval: () => 0, console, location: { hash: "", href: "", hostname: "", pathname: "/" },
    history: { replaceState() {} }, addEventListener() {}, innerWidth: 1000, matchMedia: () => ({ matches: false }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(code + "\n;globalThis.__api={SP,score};", ctx, { filename: "map.html" });
  return ctx.__api;
}

const readJSON = (f, d) => { try { return JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")); } catch (e) { return d; } };
const dayKey = ms => new Date(ms).toISOString().slice(0, 10);

function main() {
  const latest = readJSON("latest.json", null);
  if (!latest || !Array.isArray(latest.spots) || !latest.spots.length) {
    console.log("snapshot: no latest.json, skipping"); return;
  }
  const api = loadScoring();
  const now = Date.now(), today = {};
  for (const spot of latest.spots) {
    if (spot.user) continue;
    for (const k of Object.keys(api.SP)) {
      let r;
      try { r = api.score(k, spot, now); } catch (e) { continue; }
      if (r && !r.gate && typeof r.total === "number") today[`${spot.id}|${k}`] = Math.round(r.total);
    }
  }
  // The landing page's "board": the six best public spots across all fish, from the same scores
  const best = new Map();
  for (const spot of latest.spots) {
    if (spot.user) continue;
    for (const k of Object.keys(api.SP)) {
      let r; try { r = api.score(k, spot, now); } catch (e) { continue; }
      if (!r || r.gate || typeof r.total !== "number" || (r.coverage != null && r.coverage < 0.65)) continue;
      const cur = best.get(spot.id);
      if (!cur || r.total > cur.score) best.set(spot.id, { id: spot.id, name: spot.name, fish: api.SP[k].name, score: Math.round(r.total), reason: r.reason || "" });
    }
  }
  const board = [...best.values()].sort((a, b) => b.score - a.score).slice(0, 6);
  const nSpots = latest.spots.filter(x => !x.user).length;
  fs.writeFileSync(path.join(DIR, "board.json"), JSON.stringify({ generated: new Date(now).toISOString(), counts: { spots: nSpots, fish: Object.keys(api.SP).length }, spots: board }));

  const old = readJSON("scores-daily.json", {});
  const out = {};
  for (const d of Object.keys(old).sort().slice(-(KEEP - 1))) if (old[d] && typeof old[d] === "object") out[d] = old[d];
  out[dayKey(now)] = today;
  fs.writeFileSync(path.join(DIR, "scores-daily.json"), JSON.stringify(out));
  console.log(`snapshot: ${Object.keys(today).length} scores for ${dayKey(now)}; days kept: ${Object.keys(out).join(", ")}`);
}
main();
process.exit(0);   // the page's startup code leaves timers behind; nothing else to wait for
