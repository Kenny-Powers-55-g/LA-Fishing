// Checks scoring rules that must stay right: closed seasons, the time-of-year factor and
// per-fish personal tuning. Loads the page's own scoring code the way scripts/snapshot.js does.
// Run with: node tests/check_scoring.js
const fs = require("fs"), path = require("path"), vm = require("vm");
let bad = 0;
const fail = m => { bad++; console.error("FAIL: " + m); };

function load(store = {}) {
  const html = fs.readFileSync(path.join(__dirname, "..", "map.html"), "utf8");
  const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
  const noop = () => [];
  const el = () => new Proxy({ style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} } },
    { get: (t, k) => (k in t ? t[k] : noop), set: (t, k, v) => ((t[k] = v), true) });
  const ctx = {
    document: { getElementById: el, querySelector: el, querySelectorAll: () => [], createElement: el, addEventListener() {}, body: el(), hidden: false },
    navigator: { onLine: true }, localStorage: { getItem: k => (k in store ? store[k] : null), setItem() {}, removeItem() {} },
    fetch: () => Promise.reject(new Error("no network in tests")), setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, console,
    location: { hash: "", href: "", hostname: "", pathname: "/" }, history: { replaceState() {} }, addEventListener() {}, innerWidth: 1000,
    matchMedia: () => ({ matches: false }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(code + "\n;globalThis.__api={SP,scoreC,closedNow,computeTune,tuneFor,SEASON,SEASONS_CHECKED,REGS,REGS_CHECKED,regFor,keepCheck};", ctx, { filename: "map.html" });
  return ctx.__api;
}
const at = d => Date.parse(d + "T18:00:00Z");

// ---- Closed seasons ----
{
  const api = load();
  const cases = [["flounder", "2026-10-20", true], ["flounder", "2026-12-05", false],
    ["trigger", "2027-01-15", true], ["trigger", "2027-03-15", false], ["trigger", "2027-06-15", true], ["trigger", "2027-08-15", false],
    ["amberjack", "2026-10-13", false], ["amberjack", "2026-10-14", true], ["amberjack", "2027-08-31", true], ["amberjack", "2027-09-01", false],
    ["snapper", "2026-11-23", true]];
  for (const [k, d, want] of cases) if (!!api.closedNow(api.SP[k], at(d)) !== want) fail(`${k} on ${d} should be ${want ? "closed" : "open"}`);
  // A closed fish is never scored, however good the water
  const c = { zone: "offshore", temp: 76, wind: 4, wave: 1, clar: "Clear", mon: 11, area: "offshore" };
  if (!api.scoreC("amberjack", c, {}, at("2026-11-23")).gate) fail("amberjack must show as closed in November 2026");
  // Seasons set one year at a time must be checked again before they run out
  const age = (Date.now() - Date.parse(api.SEASONS_CHECKED)) / 864e5;
  if (age > 300) console.warn(`Warning: the closed-season list (SEASON_RULES in map.html) was last checked ${Math.round(age)} days ago. Check LDWF and NOAA Fisheries.`);
}

// ---- Time of year counts in the score ----
{
  const api = load();
  for (const k of Object.keys(api.SEASON))
    if (!api.SP[k].factors.some(F => F.k === "mon")) fail(`${k} has a season guide but no time-of-year factor in its score`);
  const c = { zone: "offshore", temp: 76, wind: 4, wave: 1, clar: "Clear", vis: 20, area: "offshore" };
  const july = api.scoreC("cobia", { ...c, mon: 4 }, {}, at("2027-04-15")).total, nov = api.scoreC("cobia", { ...c, mon: 11 }, {}, at("2026-11-23")).total;
  if (!(nov < 60 && july - nov >= 20)) fail(`cobia should score well below "Good" in November, when most have left (April ${july}, November ${nov})`);
}

// ---- Personal tuning is per fish ----
{
  // 6 good and 4 slow bass trips where warm water matched the good days
  const logs = [];
  for (let i = 0; i < 10; i++) logs.push({ id: i + 1, sp: "bass", spot: "toledo", date: "2026-11-01", result: i < 6 ? "caught" : "slow", factors: { temp: i < 6 ? 0.9 : 0.3, wind: 0.6 } });
  let api = load({ "la-fish-catches-v1": JSON.stringify(logs) });
  api.computeTune();
  if (!api.tuneFor("bass")) fail("bass should be tuned from its own 10 trips");
  if (!api.tuneFor("crappie") || api.tuneFor("crappie").scope !== "group") fail("crappie (freshwater, no trips) should borrow the freshwater tuning");
  if (api.tuneFor("redfish")) fail("bass trips must not change how redfish are scored");
  const c = { zone: "inshore", temp: 75, tide: "Incoming", wind: 6, clar: "Stained", sal: 15, mon: 10, area: "terr" };
  const r = api.scoreC("redfish", c, {}, at("2026-10-15"));
  if (r.tuned) fail("a redfish score must not say 'Tuned to your catches' from bass trips");
  // Add 5 good and 3 slow redfish trips: redfish now has its own tuning
  for (let i = 0; i < 8; i++) logs.push({ id: 100 + i, sp: "redfish", spot: "barat", date: "2026-11-02", result: i < 5 ? "caught" : "none", factors: { tide: i < 5 ? 1 : 0.2, temp: 0.7 } });
  api = load({ "la-fish-catches-v1": JSON.stringify(logs) });
  api.computeTune();
  const T = api.tuneFor("redfish");
  if (!T || T.scope !== "fish" || !(T.t.tide > 1) || "wind" in T.t) fail(`redfish should be tuned from its own trips only (got ${JSON.stringify(T)})`);
  api = load({ "la-fish-catches-v1": JSON.stringify(logs), "la-fish-tune": "off" });
  api.computeTune();
  if (api.tuneFor("redfish")) fail("switching tuning off must stop it");
}

// ---- Size and bag limits (LDWF 2026) ----
{
  const api = load();
  for (const [k, R] of Object.entries(api.REGS)) {
    if (!api.SP[k]) fail(`REGS has "${k}", which isn't a fish in the app`);
    if (!R.bag) fail(`REGS.${k} has no daily limit`);
    for (const f of ["min", "max", "over"]) if (R[f] != null && !(Number.isInteger(R[f]) && R[f] >= 0)) fail(`REGS.${k}.${f} must be a whole number`);
    if (R.min && R.max && R.min >= R.max) fail(`REGS.${k}: minimum must be below maximum`);
    if ((R.min || R.max) && !R.len) fail(`REGS.${k}: say how it's measured (total length, fork length…)`);
  }
  const keep = (k, inches, spot) => (api.keepCheck(k, inches, spot) || {}).ok;
  const cases = [["redfish", 17, false], ["redfish", 18, true], ["redfish", 27, true], ["redfish", 28, false], ["trout", 12, false], ["trout", 22, true],
    ["drum", 30, true], ["cobia", 35, false], ["cobia", 40, true], ["bass", 11, true]];
  for (const [k, n, want] of cases) if (keep(k, n) !== want) fail(`${k} at ${n} in should be ${want ? "a keeper" : "released"}`);
  if (keep("bass", 11, { id: "verret" }) !== false) fail("bass under 12 in must be released at Lake Verret (from Aug. 1, 2026)");
  if (!api.regFor("bass", { id: "caddo" }).local) fail("Caddo Lake has its own bass rule");
  const age = (Date.now() - Date.parse(api.REGS_CHECKED)) / 864e5;
  if (age > 300) console.warn(`Warning: the size and bag limits (REGS in map.html) were last checked ${Math.round(age)} days ago. Check LDWF's current regulations.`);
}

console.log(bad ? `${bad} problem(s)` : "scoring rules OK");
process.exit(bad ? 1 : 0);
