// Loads the site's own scoring code from map.html, so the hourly scripts (alerts.js, snapshot.js, pages.js)
// score exactly the way the page does. The page's scripts run in a sandbox with a do-nothing stand-in for
// the browser, and the pieces each script asks for by name are handed back.
//
//   const api = require("./scoring")(["SP", "score", "setCommunity"]);
const fs = require("fs"), path = require("path"), vm = require("vm");

module.exports = function loadScoring(names) {
  if (!names.every(n => /^[A-Za-z_$][\w$]*$/.test(n))) throw new Error("bad name in loadScoring");
  const html = fs.readFileSync(path.join(__dirname, "..", "map.html"), "utf8");
  const code = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
  if (!code.trim()) throw new Error("No scripts found in map.html");
  const noop = () => [];
  const el = () => new Proxy({ style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} } },
    { get: (t, k) => (k in t ? t[k] : noop), set: (t, k, v) => ((t[k] = v), true) });
  const ctx = {
    document: { getElementById: el, querySelector: el, querySelectorAll: () => [], createElement: el,
                addEventListener() {}, body: el(), hidden: false },
    navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: () => Promise.reject(new Error("no network in the hourly scripts")),
    // the page's own timers (like its 45 s "still loading" watchdog) must not keep a script running
    setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); if (t && t.unref) t.unref(); return t; }, clearTimeout,
    setInterval: () => 0, console, location: { hash: "", href: "", hostname: "", pathname: "/" },
    history: { replaceState() {} }, addEventListener() {}, innerWidth: 1000, matchMedia: () => ({ matches: false }),
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  // The page's top-level consts aren't visible from outside, so hand the requested pieces back.
  // setFC is the one setter: it swaps in the 7-day forecast the page would have fetched.
  const pick = names.map(n => (n === "setFC" ? "setFC:f=>{FC=f}" : n)).join(",");
  vm.runInContext(code + `\n;globalThis.__api={${pick}};`, ctx, { filename: "map.html" });
  return ctx.__api;
};
