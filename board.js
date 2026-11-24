// Landing page: the live example card shows the top spot for a popular fish (or the visitor's own fish) from board.json (written each hour by scripts/snapshot.js).
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
  fetch("board.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; }).then(function (b) {
    if (!b || !Array.isArray(b.spots) || !b.spots.length) return;
    var mine = myFish(), s = b.spots[0];
    for (var i = 0; i < mine.length; i++) { var m = b.spots.filter(function (x) { return x.k === mine[i]; })[0]; if (m) { s = m; break; } }
    var k = band(s.score);
    var score = document.getElementById("demoscore");
    score.textContent = s.score;
    score.className = "demo-score c-" + k.k;
    set("demoname", s.name);
    set("demofish", s.fish);
    var bd = document.getElementById("demoband");
    bd.className = "demo-band c-" + k.k;
    bd.innerHTML = "";
    var dot = document.createElement("i"); dot.className = "d-" + k.k; bd.appendChild(dot);
    bd.appendChild(document.createTextNode(k.w));
    set("demowhy", s.reason ? "Why: " + s.reason : "");
    set("demoup", "Updated " + new Date(b.generated).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) + " · estimates, not guarantees");
    // Straight into the app at this fish and spot
    var go = document.getElementById("demogo");
    if (go && /^[a-z]{2,20}$/.test(s.k || "") && /^[A-Za-z0-9_-]{1,40}$/.test(s.id || "")) go.setAttribute("href", "map.html#" + s.k + "/" + s.id);
    card.hidden = false;
  }).catch(function () {});
})();
