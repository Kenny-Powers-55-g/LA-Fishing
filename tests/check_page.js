// Syntax-checks every inline <script> in map.html so a typo can't reach the live site.
// Run with: node tests/check_page.js
const fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "map.html"), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error("No inline scripts found in map.html"); process.exit(1); }
let bad = 0;
scripts.forEach((code, i) => {
  try { new Function(code); }
  catch (e) { bad++; console.error(`Script ${i + 1}: ${e.message}`); }
});
for (const id of ["map", "detail", "ranks", "tabs", "outlook", "seastate", "status"]) {
  if (!html.includes(`id="${id}"`)) { bad++; console.error(`Missing element #${id}`); }
}
// Security policy: inline scripts only run if their fingerprints are listed. The deploy refreshes
// them, so a stale list here is only a warning (it must never block the hourly data update).
if (!/<meta http-equiv="Content-Security-Policy"/.test(html)) { bad++; console.error("Missing the Content-Security-Policy meta tag"); }
else {
  const r = require("child_process").spawnSync(process.execPath, [path.join(__dirname, "..", "scripts", "csp.js"), "--check", path.join(__dirname, "..", "map.html")], { encoding: "utf8" });
  if (r.status !== 0) console.warn("Warning: " + (r.stderr || r.stdout).trim());
}
// Data files must be loaded through dataPath() so the iPhone app can load them from the live site
const rawData = html.match(/fetch\(\s*["'`][a-z_]+\.json/g);
if (rawData) { bad++; console.error(`Data file loaded without dataPath(): ${rawData.join(", ")}`); }
if (!/fetch\(dataPath\(f\)/.test(html)) { bad++; console.error("Map-layer data isn't loaded through dataPath()"); }
// config.js dataUrl (used only by the app) must be an https address ending in "/", and allowed by the policy
{
  const cfg = fs.readFileSync(path.join(__dirname, "..", "config.js"), "utf8"), m = /dataUrl:\s*"([^"]*)"/.exec(cfg);
  if (!m || !/^https:\/\/[^\s"'<>]+\/$/.test(m[1])) { bad++; console.error(`config.js dataUrl must be an https address ending in "/" (found ${m ? m[1] : "none"})`); }
  else {
    const csp = (/<meta http-equiv="Content-Security-Policy" content="([^"]*)"/.exec(html) || [])[1] || "";
    const connect = (/connect-src([^;]*)/.exec(csp) || [])[1] || "", origin = new URL(m[1]).origin;
    if (!connect.split(/\s+/).includes(origin)) { bad++; console.error(`The security policy's connect-src doesn't allow config.js dataUrl (${origin})`); }
  }
}
for (const m of html.matchAll(/<script src="https:[^"]*"[^>]*>/g))
  if (!/integrity="sha(256|384|512)-/.test(m[0])) { bad++; console.error(`External script without an integrity hash: ${m[0]}`); }
console.log(bad ? `${bad} problem(s)` : `map.html OK (${scripts.length} script${scripts.length > 1 ? "s" : ""})`);
process.exit(bad ? 1 : 0);
