// Landing page: the live card shows the best spot right now for a popular fish (or the visitor's own fish) from
// board.json (written each hour by scripts/snapshot.js), with chips to switch fish, the best time and the boat check.
// If the file can't be read, the card stays hidden: the page never shows made-up numbers.
(function () {
  "use strict";
  var card = document.getElementById("demo");
  if (!card) return;
  function band(s) {
    return s >= 75 ? { w: "Excellent", k: "ex" } : s >= 55 ? { w: "Good", k: "gd" } : s >= 35 ? { w: "Fair", k: "fr" } : { w: "Poor", k: "po" };
  }
  function set(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }
  // The fish this visitor picked in the app (same site, same storage), if any
  function myFish() { try { var a = JSON.parse(localStorage.getItem("oc-myfish") || "[]"); return Array.isArray(a) ? a : []; } catch (e) { return []; } }
  // "Sat 12 PM–3 PM" -> "Today 12 PM–3 PM" / "Tomorrow …" when the day is today or tomorrow here
  function dayWord(t) {
    var m = /^([A-Z][a-z]{2}) (.*)$/.exec(t || ""); if (!m) return t || "";
    var d = new Date(), wd = function (x) { return x.toLocaleDateString("en-US", { weekday: "short" }); };
    if (m[1] === wd(d)) return "Today " + m[2];
    d.setDate(d.getDate() + 1);
    return m[1] === wd(d) ? "Tomorrow " + m[2] : t;
  }
  fetch("board.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; }).then(function (b) {
    if (!b || !Array.isArray(b.spots) || !b.spots.length) return;
    var spots = b.spots.filter(function (x) { return /^[a-z]{2,20}$/.test(x.k || "") && typeof x.score === "number"; });
    if (!spots.length) return;
    var c = b.counts || {};
    if (c.spots > 0) [].forEach.call(document.querySelectorAll(".nspots"), function (e) { e.textContent = c.spots; });
    if (c.fish > 0) [].forEach.call(document.querySelectorAll(".nfish"), function (e) { e.textContent = c.fish; });
    var mine = myFish(), cur = spots[0];
    for (var i = 0; i < mine.length; i++) { var m = spots.filter(function (x) { return x.k === mine[i]; })[0]; if (m) { cur = m; break; } }
    var chips = document.getElementById("demofishes");
    function show(s) {
      var k = band(s.score), score = document.getElementById("demoscore");
      score.textContent = s.score;
      score.className = "demo-score c-" + k.k;
      set("demoname", s.name);
      set("demofish", s.fish);
      var bd = document.getElementById("demoband");
      bd.className = "demo-band c-" + k.k;
      bd.textContent = k.w;
      set("demobest", s.best ? dayWord(s.best) : "See the spot for hours");
      set("demoboat", s.shore ? "Fished from the beach" : s.boat ? s.boat + " for a small boat" : "–");
      set("demowhy", s.reason ? "Why: " + s.reason : "");
      // Straight into the app at this fish and spot
      var go = document.getElementById("demogo");
      if (go && /^[A-Za-z0-9_-]{1,40}$/.test(s.id || "")) go.setAttribute("href", "map.html#" + s.k + "/" + s.id);
      if (chips) [].forEach.call(chips.children, function (x) { x.setAttribute("aria-pressed", x.getAttribute("data-k") === s.k ? "true" : "false"); });
    }
    if (chips) {
      chips.textContent = "";
      spots.forEach(function (s) {
        var bt = document.createElement("button");
        bt.type = "button"; bt.textContent = s.fish; bt.setAttribute("data-k", s.k);
        bt.addEventListener("click", function () { show(s); });
        chips.appendChild(bt);
      });
    }
    show(cur);
    var when = new Date(b.generated).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    set("demoup", "Updated " + when + " · estimates, not guarantees");
    var ll = document.getElementById("liveline");
    if (ll && c.spots > 0) { ll.textContent = "Last updated at " + when + ": " + c.spots + " spots scored for " + c.fish + " fish."; ll.hidden = false; }
    card.hidden = false;
  }).catch(function () {});
})();
