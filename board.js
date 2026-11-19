// Landing page: the live example card shows the top spot from board.json (written each hour by scripts/snapshot.js).
// If the file can't be read, the card stays hidden: the page never shows made-up numbers.
(function () {
  "use strict";
  var card = document.getElementById("demo");
  if (!card) return;
  function band(s) {
    return s >= 75 ? { w: "Excellent", k: "ex" } : s >= 55 ? { w: "Good", k: "gd" } : s >= 35 ? { w: "Fair", k: "fr" } : { w: "Poor", k: "po" };
  }
  function set(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }
  fetch("board.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; }).then(function (b) {
    if (!b || !Array.isArray(b.spots) || !b.spots.length) return;
    var s = b.spots[0], k = band(s.score);
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
    var c = document.getElementById("lpcounts");
    card.hidden = false;
  }).catch(function () {});
})();
