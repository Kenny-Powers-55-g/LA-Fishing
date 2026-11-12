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
console.log(bad ? `${bad} problem(s)` : `index.html OK (${scripts.length} script${scripts.length > 1 ? "s" : ""})`);
process.exit(bad ? 1 : 0);
