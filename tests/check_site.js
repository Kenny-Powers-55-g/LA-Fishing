// Checks the static pages (landing, About, Contact, Terms, Privacy) so a slip can't reach the live site.
// Run with: node tests/check_site.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.join(__dirname, "..");
const PAGES = ["index.html", "about.html", "contact.html", "terms.html", "privacy.html"];
const read = f => fs.readFileSync(path.join(ROOT, f), "utf8");
let bad = 0;
const fail = m => { bad++; console.error("FAIL: " + m); };
const block = (h, re) => ((re.exec(h) || [])[0] || "").replace(/\s*aria-current="page"/g, "");
const heads = new Set(), foots = new Set();

for (const f of PAGES) {
  const h = read(f);
  if (!/<meta http-equiv="Content-Security-Policy" content="[^"]*script-src 'self'/.test(h)) fail(`${f}: missing or loose CSP meta`);
  if (/<script(?![^>]*\bsrc=)[^>]*>/i.test(h)) fail(`${f}: inline <script>`);
  if (/<style[\s>]/i.test(h)) fail(`${f}: inline <style>`);
  if (/\sstyle=/i.test(h)) fail(`${f}: style= attribute`);
  if (/\son[a-z]+=/i.test(h.replace(/<svg[\s\S]*?<\/svg>/g, ""))) fail(`${f}: inline event handler`);
  if ((h.match(/<h1[\s>]/g) || []).length !== 1) fail(`${f}: needs exactly one <h1>`);
  const title = (/<title>([^<]*)<\/title>/.exec(h) || [])[1] || "";
  if (!title.includes("OceanCast")) fail(`${f}: title lacks OceanCast`);
  if (/Ocean Cast|Oceancast|Ocean cast|OCEANcast/.test(h.replace(/OCEAN<\/span>/g, ""))) fail(`${f}: inconsistent brand spelling`);
  for (const m of h.replace(/<!--[\s\S]*?-->/g, "").matchAll(/(?:href|src)="([^"#:?]+)(?:[#?][^"]*)?"/g)) {
    if (!fs.existsSync(path.join(ROOT, m[1]))) fail(`${f}: broken link ${m[1]}`);
  }
  if (!h.includes("Scores are estimates, not guarantees. Not for navigation or safety decisions.")) fail(`${f}: footer disclaimer missing`);
  if (/terms|privacy/.test(f) && !/DRAFT &ndash; needs legal review|DRAFT – needs legal review/.test(h)) fail(`${f}: DRAFT banner missing`);
  if (!/<script src="site\.js">/.test(h)) fail(`${f}: site.js missing`);
  if (f === "index.html" && h.indexOf("forward.js") > h.indexOf("site.js")) fail("index.html: forward.js must load before site.js");
  // the landing page has its own header (handoff redesign); the other pages share one
  if (f !== "index.html") heads.add(block(h, /<header class="bar">[\s\S]*?<\/header>/));
  foots.add(block(h, /<footer class="foot">[\s\S]*?<\/footer>/));
}
if (heads.size !== 1) fail("headers differ between the About, Contact, Terms and Privacy pages");
if (!/id="themebtn"/.test(read("index.html"))) fail("landing page needs the theme button");
if (!/<script src="board\.js"/.test(read("index.html"))) fail("landing page needs board.js");
if (foots.size !== 1) fail("footers differ between pages");

// manifest and service worker
const man = JSON.parse(read("manifest.webmanifest"));
if (man.start_url !== "map.html") fail("manifest start_url should be map.html");
const sw = read("sw.js");
for (const f of [...PAGES, "map.html", "site.css", "site.js", "forward.js"]) if (!sw.includes(`"${f}"`)) fail(`sw.js ASSETS lacks ${f}`);
const wf = read(".github/workflows/update.yml");
for (const f of [...PAGES, "map.html", "site.css", "site.js", "forward.js", "favicon.svg", "apple-touch-icon.png"]) if (!wf.includes(f)) fail(`update.yml does not publish ${f}`);

// forward.js: which links go on to the app
function run(hash, search, o = {}) {
  let to = null;
  const ctx = { location: { hash, search, replace: u => { to = u; } }, document: { referrer: o.ref || "", documentElement: {}, addEventListener: () => {} },
    matchMedia: () => ({ matches: !!o.standalone }), navigator: {} };
  vm.runInNewContext(read("forward.js"), ctx);
  return to;
}
const cases = [
  ["#redfish/calc", "", {}, null], ["#tropical", "", {}, null],
  ["#access_token=x&type=recovery", "", {}, "map.html#access_token=x&type=recovery"],
  ["#error=access_denied&error_code=otp_expired", "", {}, "map.html#error=access_denied&error_code=otp_expired"],
  ["", "?code=abc", {}, "map.html?code=abc"], ["", "", { standalone: true }, "map.html"],
  ["#how", "", {}, null], ["", "", {}, null], ["", "", { standalone: true, ref: "https://x/LA-Fishing/map.html" }, null]];
for (const [h, s, o, want] of cases) { const got = run(h, s, o); if (got !== want) fail(`forward.js ${JSON.stringify([h, s, o])}: got ${got}, want ${want}`); }

// the one-tap banner for an old app link
{
  let ready = null, href = null; const p = { hidden: true, querySelector: () => ({ setAttribute: (k, v) => { href = v; } }) };
  vm.runInNewContext(read("forward.js"), { location: { hash: "#redfish/calc", search: "", replace: () => fail("forwarded #redfish/calc") },
    document: { referrer: "", documentElement: {}, addEventListener: (e, f) => { ready = f; }, getElementById: () => p }, matchMedia: () => ({ matches: false }), navigator: {} });
  if (ready) ready();
  if (href !== "map.html#redfish/calc" || p.hidden) fail(`resume banner: href ${href}, hidden ${p.hidden}`);
  if (!/id="resume" hidden/.test(read("index.html"))) fail("index.html: resume banner missing");
}

// Search and sharing: every page has a title, a description, and preview tags
const BASE = "https://kenny-powers-55-g.github.io/LA-Fishing/";
for (const f of [...PAGES, "map.html"]) {
  const h = read(f);
  const title = (/<title>([^<]*)<\/title>/.exec(h) || [])[1] || "";
  const desc = (/<meta name="description" content="([^"]*)"/.exec(h) || [])[1] || "";
  if (title.length < 20 || title.length > 75) fail(`${f}: title length ${title.length}`);
  if (desc.length < 50 || desc.length > 170) fail(`${f}: description length ${desc.length}`);
  for (const tag of ['property="og:title"', 'property="og:description"', 'property="og:image"', 'name="twitter:card"', 'name="twitter:image"', 'rel="canonical"'])
    if (!h.includes(tag)) fail(`${f}: missing ${tag}`);
}
const sm = read("sitemap.xml");
for (const p of ["", "map.html", ...PAGES.slice(1)]) if (!sm.includes(`<loc>${BASE}${p}</loc>`)) fail(`sitemap.xml lacks ${BASE}${p}`);
if (!/^Sitemap: https:\/\/.+\/sitemap\.xml$/m.test(read("robots.txt"))) fail("robots.txt needs a Sitemap line");
if (!fs.existsSync(path.join(ROOT, "og-image.png"))) fail("og-image.png missing");

console.log(bad ? `${bad} problem(s)` : `site pages OK (${PAGES.length} pages, forward.js ${cases.length} cases)`);
process.exit(bad ? 1 : 0);
