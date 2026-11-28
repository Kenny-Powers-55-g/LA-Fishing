// Daily score snapshot for the "trend vs yesterday" numbers on the Today tab.
// Runs in the hourly workflow after latest.json is built. It scores every public spot for every fish
// with the site's own scoring code (from map.html, the same loader scripts/alerts.js uses) and writes
// scores-daily.json: { "YYYY-MM-DD": { "<spot id>|<fish>": score } }, keeping the last 3 days.
// Each run overwrites today's entry, so the value kept for a day is its latest one.
//
//   node scripts/snapshot.js        (reads and writes in the current folder, or SNAPSHOT_DIR)
const fs = require("fs"), path = require("path");
const DIR = process.env.SNAPSHOT_DIR || process.cwd();
const KEEP = 3;

const loadScoring = () => require("./scoring")(["SP", "score", "MODEL_UPDATED", "setCommunity", "bestWindowFor", "winTxt", "LV_TXT", "setFC"]);

const readJSON = (f, d) => { try { return JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")); } catch (e) { return d; } };
const dayKey = ms => new Date(ms).toISOString().slice(0, 10);

function main() {
  const latest = readJSON("latest.json", null);
  if (!latest || !Array.isArray(latest.spots) || !latest.spots.length) {
    console.log("snapshot: no latest.json, skipping"); return;
  }
  const api = loadScoring();
  api.setCommunity(readJSON("community.json", null));   // the same community tuning the page uses
  api.setFC(readJSON("forecast.json", null));           // for the card's best time and boat check
  const now = Date.now(), today = {};
  for (const spot of latest.spots) {
    if (spot.user) continue;
    for (const k of Object.keys(api.SP)) {
      let r;
      try { r = api.score(k, spot, now); } catch (e) { continue; }
      if (r && !r.gate && typeof r.total === "number") today[`${spot.id}|${k}`] = Math.round(r.total);
    }
  }
  // The landing page's example card: the best public spot for each of the fish most people come for,
  // best first. (Ranking every fish together put the same lightly-modelled fish on top every day.)
  // The card shows the visitor's own fish if they picked one in the app, otherwise the first entry.
  const FEATURED = ["redfish", "trout", "flounder", "sheepshead", "drum", "bass", "crappie"];
  const board = [];
  for (const k of FEATURED.filter(f => api.SP[f])) {
    let top = null;
    for (const spot of latest.spots) {
      if (spot.user) continue;
      let r; try { r = api.score(k, spot, now); } catch (e) { continue; }
      if (!r || r.gate || typeof r.total !== "number" || (r.coverage != null && r.coverage < 0.65)) continue;
      if (!top || r.total > top.score) top = { k, id: spot.id, name: spot.name, fish: api.SP[k].name, score: Math.round(r.total), reason: r.reason || "", spot };
    }
    if (top) {
      // The best 3-hour window in the next day and a half that's safe for a small boat, and the verdict for it
      let w = null; try { w = api.bestWindowFor(top.spot, k, 36); } catch (e) {}
      // The boat verdict has its own row on the card, so the time doesn't repeat it
      if (w) { top.best = api.winTxt(w).replace(/, [^,]* for your boat$/, ""); if (w.lv != null) top.boat = api.LV_TXT[w.lv]; }
      // Surf beaches are fished from the sand, so the card says so instead of giving a boat verdict
      if (/\(surf\)/i.test(top.name)) { top.shore = true; delete top.boat; }
      delete top.spot; board.push(top);
    }
  }
  board.sort((a, b) => b.score - a.score);
  const nSpots = latest.spots.filter(x => !x.user).length;
  fs.writeFileSync(path.join(DIR, "board.json"), JSON.stringify({ generated: new Date(now).toISOString(), counts: { spots: nSpots, fish: Object.keys(api.SP).length }, spots: board }));

  // Scores from an older version of the scoring rules aren't comparable: start the trend history fresh
  const old = readJSON("scores-daily.json", {}), model = api.MODEL_UPDATED || null;
  const out = { _model: model };
  if (old._model === model)
    for (const d of Object.keys(old).filter(k => /^\d{4}-\d\d-\d\d$/.test(k)).sort().slice(-(KEEP - 1)))
      if (old[d] && typeof old[d] === "object") out[d] = old[d];
  out[dayKey(now)] = today;
  fs.writeFileSync(path.join(DIR, "scores-daily.json"), JSON.stringify(out));
  console.log(`snapshot: ${Object.keys(today).length} scores for ${dayKey(now)}; days kept: ${Object.keys(out).filter(k => k !== "_model").join(", ")} (scoring rules ${model})`);
}
main();
process.exit(0);   // the page's startup code leaves timers behind; nothing else to wait for
