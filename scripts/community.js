// Community tuning. Runs in the hourly workflow before snapshot.js and alerts.js.
// People can share a catch anonymously from the catch log (see "Anonymous community catch reports"
// in supabase/setup.sql). This reads the totals from oc_community_stats() with the site's public key
// from config.js, works out how much each reading should count for each fish, and writes
// community.json. The page, the daily snapshot and the phone alerts all apply it to their scores.
//
// The idea is the same as personal tuning in the catch log: a reading that graded higher on trips
// that caught fish than on slow ones counts a bit more, and one that graded higher on slow trips
// counts a bit less. Community tuning is more careful, because anyone can send a report:
//   - a fish needs MIN_CAUGHT good and MIN_SLOW slow or empty trips before anything changes,
//     and each reading needs MIN_F_GOOD / MIN_F_SLOW of its own;
//   - the change shrinks toward "no change" while there are few reports (SHRINK);
//   - no reading ever counts more than HI or less than LO times its normal weight.
// One of the seven big areas gets its own tuning once it passes the same bars; until then the fish's
// all-areas tuning is used everywhere.
//
//   node scripts/community.js     (writes community.json in the current folder, or COMMUNITY_DIR)
//
// Env: COMMUNITY_STATS_FILE  read the totals from this file instead of Supabase (for tests)
//
// If Supabase can't be reached (or setup.sql hasn't been run yet), the previous community.json is
// kept as it is, so scores never jump back and forth with the network.
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.join(__dirname, ".."), DIR = process.env.COMMUNITY_DIR || process.cwd();
const OUT = "community.json";

const MIN_CAUGHT = 20, MIN_SLOW = 10;     // trips a fish (or a fish in one area) needs before tuning
const MIN_F_GOOD = 10, MIN_F_SLOW = 5;    // trips with this reading graded, on each side
const SHRINK = 30;                        // with this many trips on each side, half the measured change is used
const LO = 0.7, HI = 1.3;                 // weight multipliers stay inside this range
const MIN_CHANGE = 0.05;                  // smaller changes are left out
const AREAS = ["sw", "acadiana", "terr", "nola", "delta", "north", "offshore"];
const SP_RE = /^[a-z]{2,20}$/, F_RE = /^[a-z]{2,10}$/;

const int = v => (Number.isInteger(v) && v >= 0 ? v : 0);
const unit = v => (typeof v === "number" && isFinite(v) && v >= 0 && v <= 1 ? v : null);
function band(b) {
  const out = {};
  if (b && typeof b === "object")
    for (const k of ["ex", "gd", "fr", "po"])
      if (Array.isArray(b[k]) && int(b[k][0]) >= 5) out[k] = [int(b[k][0]), Math.min(int(b[k][1]), int(b[k][0]))];
  return out;
}

// One fish in one area (or "all"): its trip counts and, if it has enough of them, its multipliers
function tuneCell(cell) {
  const caught = int(cell && cell.caught), slow = int(cell && cell.slow), tune = {};
  if (caught >= MIN_CAUGHT && slow >= MIN_SLOW && cell.f && typeof cell.f === "object") {
    for (const [k, v] of Object.entries(cell.f)) {
      if (!F_RE.test(k) || !Array.isArray(v)) continue;
      const gm = unit(v[0]), gn = int(v[1]), sm = unit(v[2]), sn = int(v[3]);
      if (gm == null || sm == null || gn < MIN_F_GOOD || sn < MIN_F_SLOW) continue;
      const n = 2 / (1 / gn + 1 / sn);                 // trips on each side, as one number (harmonic mean)
      const mult = Math.max(LO, Math.min(HI, 1 + 2 * (gm - sm) * n / (n + SHRINK)));
      if (Math.abs(mult - 1) >= MIN_CHANGE) tune[k] = Math.round(mult * 100) / 100;
    }
  }
  return { caught, slow, tune, b: band(cell && cell.b) };
}

// The whole community.json from oc_community_stats()'s answer
function build(stats, now = Date.now()) {
  if (!stats || typeof stats !== "object" || typeof stats.total !== "number") throw new Error("not a community stats answer");
  const species = {};
  for (const [sp, cells] of Object.entries(stats.species || {})) {
    if (!SP_RE.test(sp) || !cells || typeof cells !== "object") continue;
    const out = {};
    for (const [area, cell] of Object.entries(cells))
      if (area === "all" || AREAS.includes(area)) out[area] = tuneCell(cell);
    if (Object.keys(out).length) species[sp] = out;
  }
  const regions = {};
  for (const a of AREAS) if (stats.regions && int(stats.regions[a]) >= 5) regions[a] = int(stats.regions[a]);
  return {
    generated: new Date(now).toISOString(),
    total: int(stats.total), regions, bands: band(stats.bands),
    min: { caught: MIN_CAUGHT, slow: MIN_SLOW, lo: LO, hi: HI },
    species,
  };
}

function config() {
  const ctx = { window: {} };
  vm.runInNewContext(fs.readFileSync(path.join(ROOT, "config.js"), "utf8"), ctx);
  const c = ctx.window.OC_CONFIG || {};
  if (!c.supabaseUrl || !c.supabaseKey) return null;
  return { url: new URL(c.supabaseUrl).origin, key: c.supabaseKey };
}

async function fetchStats() {
  if (process.env.COMMUNITY_STATS_FILE) return JSON.parse(fs.readFileSync(process.env.COMMUNITY_STATS_FILE, "utf8"));
  const c = config();
  if (!c) throw new Error("accounts aren't set up in config.js");
  const r = await fetch(c.url + "/rest/v1/rpc/oc_community_stats", {
    method: "POST", headers: { apikey: c.key, "Content-Type": "application/json" }, body: "{}",
    signal: AbortSignal.timeout(20000),
  });
  if (!r.ok) throw new Error(`Supabase answered ${r.status}${r.status === 404 ? " (run supabase/setup.sql again to add community reports)" : ""}`);
  return r.json();
}

async function main() {
  const file = path.join(DIR, OUT);
  let stats;
  try { stats = await fetchStats(); }
  catch (e) {
    console.log(`community: ${e.message}; ${fs.existsSync(file) ? "keeping the previous community.json" : "no community tuning yet"}`);
    return;
  }
  const out = build(stats);
  fs.writeFileSync(file, JSON.stringify(out));
  const tuned = Object.entries(out.species).filter(([, a]) => Object.values(a).some(c => Object.keys(c.tune).length)).map(([k]) => k);
  console.log(`community: ${out.total} reports; tuning for ${tuned.length ? tuned.join(", ") : "no fish yet"}`);
}

module.exports = { build, tuneCell, MIN_CAUGHT, MIN_SLOW, LO, HI };
if (require.main === module) main().then(() => process.exit(0), e => { console.log("community: " + e.message); process.exit(0); });
