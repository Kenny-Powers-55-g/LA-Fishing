// Syntax-checks every inline <script> in index.html so a typo can't reach the live site.
// Run with: node tests/check_page.js
const fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error("No inline scripts found in index.html"); process.exit(1); }
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
  const r = require("child_process").spawnSync(process.execPath, [path.join(__dirname, "..", "scripts", "csp.js"), "--check"], { encoding: "utf8" });
  if (r.status !== 0) console.warn("Warning: " + (r.stderr || r.stdout).trim());
}
for (const m of html.matchAll(/<script src="https:[^"]*"[^>]*>/g))
  if (!/integrity="sha(256|384|512)-/.test(m[0])) { bad++; console.error(`External script without an integrity hash: ${m[0]}`); }
console.log(bad ? `${bad} problem(s)` : `index.html OK (${scripts.length} script${scripts.length > 1 ? "s" : ""})`);
process.exit(bad ? 1 : 0);
