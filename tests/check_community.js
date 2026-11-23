// Checks anonymous community reports and community tuning, so a slip can't reach the live site:
// the tuning maths in scripts/community.js, what a report sends (and never sends), and that the page's
// own scoring applies community.json the way "How scores work" says.
// Run with: node tests/check_community.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.join(__dirname, "..");
const { build, tuneCell, MIN_CAUGHT, MIN_SLOW, LO, HI } = require("../scripts/community.js");
let bad = 0;
const fail = m => { bad++; console.error("FAIL: " + m); };
const eq = (got, want, what) => { if (JSON.stringify(got) !== JSON.stringify(want)) fail(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); };

// ---- Tuning maths ----
{
  // [good-trip average, good trips, slow-trip average, slow trips]
  const f = { temp: [0.9, 60, 0.4, 40], tide: [0.5, 60, 0.7, 40], wind: [0.6, 60, 0.6, 40], moon: [0.9, 4, 0.1, 40] };
  const few = tuneCell({ caught: MIN_CAUGHT - 1, slow: 40, f });
  eq(few.tune, {}, "no tuning below the trip minimum");
  const t = tuneCell({ caught: 60, slow: 40, f }).tune;
  if (!(t.temp > 1 && t.temp <= HI)) fail(`temp graded higher on good trips should count more (got ${t.temp})`);
  if (!(t.tide < 1 && t.tide >= LO)) fail(`tide graded higher on slow trips should count less (got ${t.tide})`);
  if ("wind" in t) fail("a reading that grades the same on good and slow trips must be left alone");
  if ("moon" in t) fail("a reading with too few graded good trips must be left alone");
  const huge = tuneCell({ caught: 5000, slow: 5000, f: { temp: [1, 5000, 0, 5000], tide: [0, 5000, 1, 5000] } }).tune;
  eq([huge.temp, huge.tide], [HI, LO], "multipliers stay inside the limits");
  const small = tuneCell({ caught: 20, slow: 10, f: { temp: [0.8, 20, 0.65, 10] } }).tune.temp;
  const big = tuneCell({ caught: 200, slow: 100, f: { temp: [0.8, 200, 0.65, 100] } }).tune.temp;
  if (!(small < big)) fail(`fewer reports should move a weight less (${small} vs ${big})`);
  eq(tuneCell({ caught: 30, slow: 30, f: { temp: [1.7, 30, 0.1, 30], "x<y": [1, 30, 0, 30] } }).tune, {}, "out-of-range values and odd keys are ignored");
}
{
  const out = build({ total: 12, regions: { terr: 9, delta: 3, moon: 7 }, bands: { gd: [9, 6], po: [3, 0] },
    species: { redfish: { all: { caught: 9, slow: 3 }, terr: { caught: 9, slow: 3 }, "<b>": {} }, "Bad Key": { all: {} } } });
  eq(Object.keys(out.species), ["redfish"], "only plain fish keys are kept");
  eq(Object.keys(out.species.redfish).sort(), ["all", "terr"], "only the seven areas and \"all\" are kept");
  eq(out.regions, { terr: 9 }, "area counts under 5 and unknown areas are dropped");
  eq(out.bands, { gd: [9, 6] }, "score bands under 5 trips are dropped");
  let threw = false; try { build({ hello: 1 }); } catch (e) { threw = true; }
  if (!threw) fail("a wrong answer from Supabase must not become community.json");
}

// ---- The page's own code ----
function loadPage() {
  const html = fs.readFileSync(path.join(ROOT, "map.html"), "utf8");
  const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
  const noop = () => [];
  const el = () => new Proxy({ style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} } },
    { get: (t, k) => (k in t ? t[k] : noop), set: (t, k, v) => ((t[k] = v), true) });
  const ctx = {
    document: { getElementById: el, querySelector: el, querySelectorAll: () => [], createElement: el, addEventListener() {}, body: el(), hidden: false },
    navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => Promise.reject(new Error("no network in tests")), setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, console,
    location: { hash: "", href: "", hostname: "", pathname: "/" }, history: { replaceState() {} }, addEventListener() {}, innerWidth: 1000,
    matchMedia: () => ({ matches: false }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(code + "\n;globalThis.__api={SP,scoreC,setCommunity,commCell,reportOf,areaOf,AREAS};", ctx, { filename: "map.html" });
  return { api: ctx.__api, html };
}
const { api, html } = loadPage();
{
  // A redfish day at a Terrebonne spot: warm water, slack tide
  const c = { temp: 75, tide: "Slack", wind: 6, clar: "Stained", sal: 15, zone: "inshore", mon: 10, area: "terr" };
  const plain = api.scoreC("redfish", c, {}, Date.parse("2026-10-15T15:00:00Z"));
  if (plain.gate) fail("test redfish day was gated: " + plain.gate);
  // Community says: water temp matters more, tide less, in Terrebonne
  api.setCommunity({ species: { redfish: { all: { caught: 40, slow: 20, tune: { tide: 0.9 } }, terr: { caught: 25, slow: 12, tune: { temp: 1.3, tide: 0.7 } } } } });
  const tuned = api.scoreC("redfish", c, {}, Date.parse("2026-10-15T15:00:00Z"));
  if (!(tuned.total > plain.total)) fail(`less weight on a slack tide should raise this score (${plain.total} -> ${tuned.total})`);
  if (!tuned.comm || tuned.comm.area !== "terr") fail("a tuned score should say it used the Terrebonne tuning");
  const sw = api.scoreC("redfish", { ...c, area: "sw" }, {}, Date.parse("2026-10-15T15:00:00Z"));
  if (!sw.comm || sw.comm.area !== "all") fail("an area without its own tuning should use the fish's all-areas tuning");
  const other = api.scoreC("trout", { ...c, sal: 15 }, {}, Date.parse("2026-10-15T15:00:00Z"));
  if (other.comm) fail("a fish with no community tuning must not be marked as tuned");
  api.setCommunity(null);
  const back = api.scoreC("redfish", c, {}, Date.parse("2026-10-15T15:00:00Z"));
  eq(back.total, plain.total, "without community.json the scores are the plain model");
}
{
  // What a report sends
  const r = api.reportOf({ sp: "redfish", region: "terr", result: "caught", score: 74,
    factors: { temp: 0.8349, tide: 1, depth: 0.9, light: 0.6, wind: 1.7, "a<b": 0.5 }, lat: 29.25, spot: "leeville", notes: "secret hole" });
  eq(Object.keys(r).sort(), ["factors", "region", "result", "score", "sp"], "a report holds only fish, area, result, score and grades");
  eq(r.factors, { temp: 0.8, tide: 1, wind: 1 }, "grades are rounded to 0.1, kept within 0-1, and depth and light are left out");
  eq(r.score, 70, "the score is rounded down to 5");
  // Every spot falls in one of the seven big areas
  const pts = [[29.25, -90.21, "terr"], [29.76, -93.35, "sw"], [30.15, -90.1, "nola"], [29.28, -89.35, "delta"], [32.7, -93.95, "north"], [29.72, -91.87, "acadiana"]];
  for (const [lat, lon, want] of pts) eq(api.areaOf({ lat, lon, zone: "inshore" }), want, `area for ${lat},${lon}`);
  eq(api.areaOf({ lat: 28.6, lon: -89.5, zone: "offshore" }), "offshore", "offshore spots are one area");
}
{
  // The form and the database must keep the promises made on screen
  if (!/id="logShare"/.test(html)) fail("map.html: the share tick box is missing");
  if (/<input type="checkbox" id="logShare" checked/.test(html)) fail("map.html: sharing must be off until someone ticks it");
  if (!/Never your spot, pins, depth, time, sizes, counts, bait, notes, photos or name/.test(html)) fail("map.html: the 'never sent' line is missing");
  if (!/credentials:"omit"/.test(html) || /Authorization/.test((html.match(/async function sendReport[\s\S]*?\n}/) || [""])[0]))
    fail("map.html: reports must be sent without the person's sign-in");
  const sql = fs.readFileSync(path.join(ROOT, "supabase", "setup.sql"), "utf8");
  const table = (/create table if not exists public\.oc_community_reports \(([\s\S]*?)\n\);/.exec(sql) || [])[1] || "";
  if (!table) fail("setup.sql: oc_community_reports table not found");
  for (const word of ["user_id", "uuid", "lat", "lon", "spot", "timestamptz", "notes", "ip"])
    if (new RegExp(`\\b${word}\\b`, "i").test(table.replace(/--.*$/gm, ""))) fail(`setup.sql: oc_community_reports must not have a ${word} column`);
  if (/auth\.uid\(\)/.test((/function public\.oc_report_catch[\s\S]*?\$\$;/.exec(sql) || [""])[0])) fail("setup.sql: oc_report_catch must not record who sent it");
  if (!/having count\(\*\) >= 5/.test(sql)) fail("setup.sql: community totals need the 5-report floor");
}

console.log(bad ? `${bad} problem(s)` : "community reports OK");
process.exit(bad ? 1 : 0);
