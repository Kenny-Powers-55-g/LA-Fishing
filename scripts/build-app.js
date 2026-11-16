// Copies the website's files into www/, the folder the iPhone app is built from.
// The website itself is unchanged and keeps deploying from the repository root.
// Data files are not copied: the app loads them from the live site (config.js dataUrl).
//
//   npm run build     copy the files into www/
//   npm run sync      copy, then update the iOS project (npx cap sync ios)
const fs = require("fs"), path = require("path");
const ROOT = path.join(__dirname, ".."), OUT = path.join(ROOT, "www");
const FILES = ["index.html", "config.js", "manifest.webmanifest",
  "favicon.svg", "icon-192.png", "icon-512.png", "apple-touch-icon.png",
  "logo.svg", "logo-dark.svg", "logo.png", "logo-dark.png"];

fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT, { recursive: true });
let bytes = 0;
for (const f of FILES) {
  const src = path.join(ROOT, f);
  if (!fs.existsSync(src)) { console.error(`Missing ${f}`); process.exit(1); }
  fs.copyFileSync(src, path.join(OUT, f));
  bytes += fs.statSync(src).size;
}
// Same check the website gets: the security policy's script fingerprints must match.
const r = require("child_process").spawnSync(process.execPath, [path.join(__dirname, "csp.js"), "--check", path.join(OUT, "index.html")], { encoding: "utf8" });
if (r.status !== 0) { console.error((r.stderr || r.stdout).trim() + "\nRun: node scripts/csp.js"); process.exit(1); }
console.log(`www/: ${FILES.length} files, ${(bytes / 1024).toFixed(0)} KB`);
