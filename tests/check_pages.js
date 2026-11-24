// Runs scripts/pages.js on a small made-up latest.json and checks the search pages it writes:
// one page per spot and fish, a directory, a sitemap, sensible titles and no unescaped text.
// Run with: node tests/check_pages.js
const fs = require("fs"), path = require("path"), os = require("os"), cp = require("child_process");
let bad = 0;
const fail = m => { bad++; console.error("FAIL: " + m); };
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-pages-"));
try {
  const cond = { temp: 70, wind: 8, tide: "Incoming", sal: 18, clar: "Stained", pres: "Steady", cloud: 30, trange: 1.2, dtemp: 0, front: 0, rain: 0 };
  fs.writeFileSync(path.join(dir, "latest.json"), JSON.stringify({ generated: new Date().toISOString(), spots: [
    { id: "barat", name: "Barataria Bay <Grand Isle>", lat: 29.27, lon: -89.95, zone: "inshore", conditions: cond, quality: {}, launch: { name: "Bridge Side & Marina", mi: 1.2, lat: 29.26, lon: -89.96, note: "fee" } },
    { id: "toledo", name: "Toledo Bend", lat: 31.4, lon: -93.7, zone: "inshore", conditions: { ...cond, sal: 0.1 }, quality: {} },
    { id: "../evil", name: "Bad id", lat: 29, lon: -90, zone: "inshore", conditions: cond, quality: {} }] }));
  const r = cp.spawnSync(process.execPath, [path.join(__dirname, "..", "scripts", "pages.js")], { env: { ...process.env, PAGES_DIR: dir }, encoding: "utf8" });
  if (r.status !== 0) fail("pages.js failed: " + (r.stderr || r.stdout));
  for (const f of ["spots/barat.html", "spots/toledo.html", "spots/index.html", "fish/redfish.html", "fish/bass.html", "sitemap.xml"])
    if (!fs.existsSync(path.join(dir, f))) fail(`missing ${f}`);
  if (fs.existsSync(path.join(dir, "evil.html")) || fs.readdirSync(path.join(dir, "spots")).some(f => /evil/.test(f))) fail("a spot id with odd characters must not become a file");
  const barat = fs.readFileSync(path.join(dir, "spots/barat.html"), "utf8");
  if (/<Grand Isle>/.test(barat) || !/&lt;Grand Isle&gt;/.test(barat)) fail("spot names must be escaped");
  if (!/Bridge Side &amp; Marina/.test(barat)) fail("the nearest launch should be listed (escaped)");
  if ((barat.match(/<h1/g) || []).length !== 1) fail("each page needs exactly one <h1>");
  if (!/<meta http-equiv="Content-Security-Policy" content="[^"]*script-src 'self'/.test(barat)) fail("pages need the site's security policy");
  if (!/href="\.\.\/map\.html#[a-z]+\/barat"/.test(barat)) fail("a spot page should link into the app at that spot");
  const sm = fs.readFileSync(path.join(dir, "sitemap.xml"), "utf8");
  if (!/spots\/barat\.html/.test(sm) || !/fish\/redfish\.html/.test(sm) || !/<loc>https:\/\/[^<]+\/<\/loc>/.test(sm)) fail("sitemap should list the home page and the new pages");
  const desc = (/name="description" content="([^"]*)"/.exec(barat) || [])[1] || "";
  if (desc.length < 50 || desc.length > 170) fail(`description length ${desc.length}`);
} finally {
  fs.rmSync(dir, { recursive: true, force: true });
}
console.log(bad ? `${bad} problem(s)` : "search pages OK");
process.exit(bad ? 1 : 0);
