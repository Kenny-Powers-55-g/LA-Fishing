// Landing page: the "board" of the six best spots, from board.json (written by scripts/snapshot.js each hour).
// If the file can't be read, the card stays hidden: the page never shows made-up numbers.
(function () {
  "use strict";
  var card = document.getElementById("board");
  if (!card) return;
  // Band colours live in site.css (classes, not inline styles, so the page's CSP allows them)
  function band(s) {
    return s >= 75 ? { w: "Excellent", k: "ex" } : s >= 55 ? { w: "Good", k: "gd" } : s >= 35 ? { w: "Fair", k: "fr" } : { w: "Poor", k: "po" };
  }
  function part(d) { var h = d.getHours(); return h < 12 ? "morning" : h < 17 ? "afternoon" : "evening"; }
  function esc(t) { return String(t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  fetch("board.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; }).then(function (b) {
    if (!b || !Array.isArray(b.spots) || !b.spots.length) return;
    var now = new Date();
    var day = document.getElementById("boardday");
    if (day) day.textContent = now.toLocaleDateString([], { weekday: "long" }) + " " + part(now);
    var up = document.getElementById("boardup");
    if (up) up.textContent = "Updated " + new Date(b.generated).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    var rows = document.getElementById("boardrows");
    rows.innerHTML = b.spots.map(function (s) {
      var k = band(s.score);
      return '<li class="lp-row"><span class="lp-score lp-c-' + k.k + '">' + s.score + '</span>' +
        '<span class="lp-rowt"><b>' + esc(s.name) + '</b><span class="lp-why">' + esc(s.fish) + (s.reason ? " · " + esc(s.reason) : "") + '</span></span>' +
        '<span class="lp-band lp-c-' + k.k + '"><i class="lp-d-' + k.k + '"></i>' + k.w + '</span></li>';
    }).join("");
    if (b.counts) {
      var c = document.getElementById("lpcounts");
      if (c) c.textContent = b.counts.spots + " SPOTS · " + b.counts.fish + " FISH · RESCORED EVERY HOUR";
    }
    card.hidden = false;
  }).catch(function () {});
})();
