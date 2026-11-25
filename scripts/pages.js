// Search pages. Runs in the hourly workflow after snapshot.js. The app keeps every spot and fish behind
// a #link, which search engines can't see, so this writes a small plain page for each one:
//   spots/<id>.html   one spot: today's scores for every fish, the readings, the nearest boat launch
//   fish/<id>.html    one fish: its best spots right now, its season and its Louisiana limits
//   spots/index.html  every spot and fish, by area
// and rewrites sitemap.xml to list them. Scores come from the site's own scoring code (map.html), the
// same way snapshot.js and alerts.js load it. Header, footer and security policy come from about.html,
// so these pages look and behave like the rest of the site.
//
//   node scripts/pages.js        (reads latest.json and writes in the current folder, or PAGES_DIR)
const fs = require("fs"), path = require("path"), vm = require("vm");
const ROOT = path.join(__dirname, ".."), DIR = process.env.PAGES_DIR || process.cwd();

function loadScoring() {
  const html = fs.readFileSync(path.join(ROOT, "map.html"), "utf8");
  const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
  const noop = () => [];
  const el = () => new Proxy({ style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} } },
    { get: (t, k) => (k in t ? t[k] : noop), set: (t, k, v) => ((t[k] = v), true) });
  const ctx = {
    document: { getElementById: el, querySelector: el, querySelectorAll: () => [], createElement: el, addEventListener() {}, body: el(), hidden: false },
    navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => Promise.reject(new Error("no network in pages")), setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, console,
    location: { hash: "", href: "", hostname: "", pathname: "/" }, history: { replaceState() {} }, addEventListener() {}, innerWidth: 1000,
    matchMedia: () => ({ matches: false }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(code + "\n;globalThis.__api={SP,score,closedNow,regFor,regSize,areaOf,AREAS,FRESH_SP,setCommunity};", ctx, { filename: "map.html" });
  return ctx.__api;
}

const readJSON = (f, d) => { try { return JSON.parse(fs.readFileSync(path.join(DIR, f), "utf8")); } catch (e) { return d; } };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const band = v => (v >= 75 ? "Excellent" : v >= 55 ? "Good" : v >= 35 ? "Fair" : "Poor");
const SAFE_ID = /^[A-Za-z0-9_-]{1,40}$/;
// Search engines show about 160 characters; escaping (& becomes &amp;) must not push it past that
const clip = (t, n) => { if (esc(t).length <= n) return t; let c = t.slice(0, n); while (esc(c + "…").length > n) c = c.slice(0, c.lastIndexOf(" ")); return c.replace(/[ ,.:;]+$/, "") + "…"; };
// The fish most people come for lead a spot's page (lightly-modelled fish score high too easily)
const FEATURED = ["redfish", "trout", "flounder", "sheepshead", "drum", "bass", "crappie"];

// The shared look: about.html's security policy, header and footer, with links pointed one folder up
function template() {
  const about = fs.readFileSync(path.join(ROOT, "about.html"), "utf8");
  const csp = (/<meta http-equiv="Content-Security-Policy" content="([^"]*)"/.exec(about) || [])[1];
  const base = (/<link rel="canonical" href="([^"]*)about\.html"/.exec(about) || [])[1];
  if (!csp || !base) throw new Error("about.html has no security policy or canonical address");
  const up = h => h.replace(/(href|src)="(?!https?:|#|data:|mailto:|\/)([^"]+)"/g, '$1="../$2"');
  const header = up((/<header class="bar">[\s\S]*?<\/header>/.exec(about) || [""])[0]);
  const footer = up((/<footer class="foot">[\s\S]*?<\/footer>/.exec(about) || [""])[0].replace(/\s*aria-current="page"/g, ""));
  return { csp, base, header, footer };
}

function page(T, rel, title, desc, body) {
  const url = T.base + rel;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="Content-Security-Policy" content="${T.csp}">
<title>${esc(title)}</title>
<meta name="description" content="${esc(desc)}">
<meta property="og:title" content="${esc(title)}">
<meta property="og:description" content="${esc(desc)}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="OceanCast">
<meta property="og:url" content="${url}">
<meta property="og:image" content="${T.base}og-image.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="${T.base}og-image.png">
<link rel="canonical" href="${url}">
<meta name="theme-color" content="#081017">
<link rel="icon" href="../favicon.svg" type="image/svg+xml">
<script src="../site.js"></script>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Russo+One&display=swap">
<link rel="stylesheet" href="../site.css">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
${T.header}
<main id="main">
<div class="doc">
${body}
<p class="fine">Scores are estimates worked out from public NOAA, USGS and weather data, not a promise of fish. Size and bag limits come from the Louisiana Department of Wildlife &amp; Fisheries and can change: confirm before you keep fish.</p>
</div>
</main>
${T.footer}
</body>
</html>
`;
}

function main() {
  const latest = readJSON("latest.json", null);
  if (!latest || !Array.isArray(latest.spots) || !latest.spots.length) { console.log("pages: no latest.json, skipping"); return; }
  const api = loadScoring(), T = template();
  api.setCommunity(readJSON("community.json", null));
  const now = Date.now(), spots = latest.spots.filter(s => !s.user && SAFE_ID.test(s.id || ""));
  const fishIds = Object.keys(api.SP).filter(k => SAFE_ID.test(k));
  const when = new Date(latest.generated || now).toLocaleString("en-US", { timeZone: "America/Chicago", weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) + " Central";
  const areaName = s => (api.AREAS.find(a => a.k === api.areaOf(s)) || {}).n || "";
  // Every score once
  const S = {};
  for (const s of spots) {
    S[s.id] = {};
    for (const k of fishIds) {
      let r; try { r = api.score(k, s, now); } catch (e) { continue; }
      if (r && !r.gate && typeof r.total === "number" && (r.coverage == null || r.coverage >= 0.5)) S[s.id][k] = { v: Math.round(r.total), why: r.reason || "" };
    }
  }
  for (const d of ["spots", "fish"]) fs.mkdirSync(path.join(DIR, d), { recursive: true });
  const written = [];
  const write = (rel, html) => { fs.writeFileSync(path.join(DIR, rel), html); written.push(rel); };

  // ---- One page per spot
  for (const s of spots) {
    const list = Object.entries(S[s.id]).sort((a, b) => b[1].v - a[1].v);
    const top = list.find(([k]) => FEATURED.includes(k)) || list[0], c = s.conditions || {}, area = areaName(s);
    const reading = [["Water temperature", c.temp != null ? c.temp + " °F" : null], ["Wind", c.wind != null ? c.wind + " mph" : null], ["Tide", c.tide],
      ["Salinity", c.sal != null ? c.sal + " ppt" : null], ["Water clarity", c.clar], ["Waves", c.wave != null ? c.wave + " ft" : null], ["Pressure", c.pres]].filter(x => x[1] != null);
    const L = s.launch;
    const title = clip(`${s.name} fishing report: scores and conditions`, 75);
    const lead = [top, ...list.filter(x => x !== top && FEATURED.includes(x[0]))].filter(Boolean).slice(0, 3).map(([k]) => api.SP[k].name.toLowerCase());
    const desc = clip(`${s.name}${area ? `, ${area}` : ""}: hourly fishing scores for ${lead.length > 1 ? lead.slice(0, -1).join(", ") + " and " + lead[lead.length - 1] : lead[0] || "local fish"}, with water temperature, wind and tide.`, 160);
    const body = `<h1>${esc(s.name)} fishing report</h1>
<p class="meta">${esc(area ? area + ", Louisiana" : "Louisiana")} · updated ${esc(when)}</p>
${top ? `<p>${FEATURED.includes(top[0]) ? "Best of the popular fish right now" : "Best right now"}: <b>${esc(api.SP[top[0]].name)}, ${top[1].v} (${band(top[1].v)})</b>${top[1].why ? `. ${esc(top[1].why)}.` : "."} <a href="../map.html#${top[0]}/${s.id}">Open ${esc(s.name)} in the map</a> for the 7-day outlook, best times and wind and waves.</p>` : `<p>No fish has enough readings to score here right now. <a href="../map.html#redfish/${s.id}">Open it in the map</a>.</p>`}
${reading.length ? `<h2>Conditions now</h2><table>${reading.map(([l, v]) => `<tr><td>${esc(l)}</td><td>${esc(v)}</td></tr>`).join("")}</table>` : ""}
${list.length ? `<h2>Scores by fish</h2><table><tr><th>Fish</th><th>Score</th><th>Why</th></tr>${list.slice(0, 14).map(([k, r]) => `<tr><td><a href="../fish/${k}.html">${esc(api.SP[k].name)}</a></td><td>${r.v} ${band(r.v)}</td><td>${esc(r.why)}</td></tr>`).join("")}</table>` : ""}
${L && L.name ? `<h2>Nearest boat launch</h2><p>${esc(L.name)}, about ${esc(L.mi)} miles away${L.note ? ` (${esc(L.note)})` : ""}. From OpenStreetMap; check it's open before you tow.</p>` : ""}
<p><a href="index.html">All spots and fish</a></p>`;
    write(`spots/${s.id}.html`, page(T, `spots/${s.id}.html`, title, desc, body));
  }

  // ---- One page per fish
  for (const k of fishIds) {
    const P = api.SP[k], best = spots.map(s => [s, S[s.id][k]]).filter(x => x[1]).sort((a, b) => b[1].v - a[1].v);
    const closed = api.closedNow(P, now), R = api.regFor(k, null);
    const name = P.name, lc = name.toLowerCase();
    const title = clip(`${name} fishing in Louisiana: best spots today`, 75);
    const desc = clip(`Where ${lc} should bite best in Louisiana right now, scored hourly from water temperature, tide and wind${R ? `, plus the ${lc} limits` : ""}.`, 160);
    const body = `<h1>${esc(name)} fishing in Louisiana</h1>
<p class="meta">Updated ${esc(when)}</p>
${closed ? `<p><b>${esc(closed)}.</b></p>` : ""}
${best.length ? `<p>Best right now: <b>${esc(best[0][0].name)}, ${best[0][1].v} (${band(best[0][1].v)})</b>. <a href="../map.html#${k}/${best[0][0].id}">Open it in the map</a>.</p>
<h2>Top spots right now</h2><table><tr><th>Spot</th><th>Score</th><th>Why</th></tr>${best.slice(0, 15).map(([s, r]) => `<tr><td><a href="../spots/${s.id}.html">${esc(s.name)}</a></td><td>${r.v} ${band(r.v)}</td><td>${esc(r.why)}</td></tr>`).join("")}</table>`
      : `<p>No spot has a score for ${esc(lc)} right now${closed ? " (the season is closed)" : ""}.</p>`}
${R ? `<h2>Louisiana limits</h2><table><tr><td>Size</td><td>${esc(api.regSize(R))}${R.len && R.min ? ` (${esc(R.len)})` : ""}</td></tr><tr><td>Keep</td><td>${esc(R.bag)}</td></tr></table>${R.note ? `<p>${esc(R.note)}</p>` : ""}` : ""}
${P.tip ? `<h2>Tips</h2><p>${esc(P.tip)}</p>` : ""}
<p><a href="../spots/index.html">All spots and fish</a></p>`;
    write(`fish/${k}.html`, page(T, `fish/${k}.html`, title, desc, body));
  }

  // ---- The directory
  const byArea = {};
  for (const s of spots) (byArea[areaName(s) || "Other"] = byArea[areaName(s) || "Other"] || []).push(s);
  const dir = `<h1>Louisiana fishing spots and fish</h1>
<p>Every spot and fish OceanCast scores, updated hourly. Updated ${esc(when)}.</p>
<h2>Fish</h2><p>${fishIds.map(k => `<a href="../fish/${k}.html">${esc(api.SP[k].name)}</a>`).join(" · ")}</p>
${Object.keys(byArea).sort().map(a => `<h2>${esc(a)}</h2><p>${byArea[a].sort((x, y) => x.name.localeCompare(y.name)).map(s => `<a href="${s.id}.html">${esc(s.name)}</a>`).join(" · ")}</p>`).join("\n")}`;
  write("spots/index.html", page(T, "spots/index.html", "Louisiana fishing spots and fish | OceanCast", "Every Louisiana fishing spot and fish OceanCast scores, with today's best spots, conditions and limits, updated every hour.", dir));
  // fish/index.html: the same directory (its links are written for the spots/ folder, so fix them up)
  fs.writeFileSync(path.join(DIR, "fish/index.html"), page(T, "spots/index.html", "Louisiana fishing spots and fish | OceanCast", "Every Louisiana fishing spot and fish OceanCast scores, with today's best spots, conditions and limits, updated every hour.",
    dir.replace(/href="([a-z0-9_-]+\.html)"/gi, 'href="../spots/$1"')));

  // ---- Sitemap: the site's own pages plus these
  const day = new Date(now).toISOString().slice(0, 10);
  const own = ["", "map.html", "about.html", "contact.html", "terms.html", "privacy.html"];
  const urls = [...own.map(p => [T.base + p, "2026-11-24"]), ...written.map(p => [T.base + p, day])];
  fs.writeFileSync(path.join(DIR, "sitemap.xml"), `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls.map(([u, d]) => `  <url>\n    <loc>${u}</loc>\n    <lastmod>${d}</lastmod>\n  </url>`).join("\n")}\n</urlset>\n`);
  console.log(`pages: ${spots.length} spot pages, ${fishIds.length} fish pages, directory and sitemap (${urls.length} addresses)`);
}
main();
process.exit(0);
