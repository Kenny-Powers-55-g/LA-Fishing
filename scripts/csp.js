// Keeps the Content Security Policy in index.html in step with its inline <script> blocks.
// The policy only lets inline scripts run if their exact SHA-256 hash is listed, so any edit to
// a script changes its hash. The deploy runs this on the published copy, so the live site is
// always right; run it locally after editing index.html to keep the repository copy right too.
//
//   node scripts/csp.js [file]           rewrite the hashes in file (default: index.html)
//   node scripts/csp.js --check [file]   exit 1 if the hashes are out of date
const fs = require("fs"), path = require("path"), crypto = require("crypto");
const args = process.argv.slice(2), check = args.includes("--check");
const file = args.find(a => !a.startsWith("--")) || path.join(__dirname, "..", "map.html");

function hashes(html) {
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(m => `'sha256-${crypto.createHash("sha256").update(m[1], "utf8").digest("base64")}'`);
}
function withHashes(html) {
  const meta = /(<meta http-equiv="Content-Security-Policy" content="[^"]*?script-src 'self')((?: 'sha256-[A-Za-z0-9+/=]+')*)/;
  if (!meta.test(html)) throw new Error("No Content-Security-Policy meta tag with script-src 'self' found");
  return html.replace(meta, (_, head) => head + " " + hashes(html).join(" "));
}

const html = fs.readFileSync(file, "utf8"), out = withHashes(html);
if (check) {
  if (out !== html) { console.error(`${file}: CSP script hashes are out of date. Run: node scripts/csp.js`); process.exit(1); }
  console.log(`${file}: CSP script hashes OK (${hashes(html).length})`);
} else if (out !== html) {
  fs.writeFileSync(file, out);
  console.log(`${file}: updated CSP script hashes (${hashes(out).length})`);
} else console.log(`${file}: CSP script hashes already up to date`);
