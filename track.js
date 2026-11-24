// About page: how the scores have done on anglers' anonymous trips, from community.json (built every hour
// by scripts/community.js). Shows nothing new until there are at least 20 trips and two score bands with
// 5 or more trips each, so a handful of reports can't make the scores look better or worse than they are.
(function () {
  "use strict";
  var box = document.getElementById("track");
  if (!box) return;
  var BANDS = [["ex", "Excellent (75 and up)"], ["gd", "Good (55 to 74)"], ["fr", "Fair (35 to 54)"], ["po", "Poor (under 35)"]];
  function el(tag, text, cls) { var e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
  fetch("community.json?t=" + Math.floor(Date.now() / 36e5)).then(function (r) { return r.ok ? r.json() : null; }).then(function (c) {
    if (!c || typeof c.total !== "number" || !c.bands) return;
    var rows = BANDS.filter(function (b) { var v = c.bands[b[0]]; return Array.isArray(v) && v[0] >= 5; });
    if (c.total < 20 || rows.length < 2) {
      box.appendChild(el("p", c.total + " trip" + (c.total === 1 ? "" : "s") + " shared so far. The results appear here once there are at least 20.", "fine"));
      return;
    }
    var t = el("table"), head = el("tr");
    ["Score that day", "Trips", "Caught fish"].forEach(function (h) { head.appendChild(el("th", h)); });
    t.appendChild(head);
    rows.forEach(function (b) {
      var v = c.bands[b[0]], tr = el("tr");
      tr.appendChild(el("td", b[1])); tr.appendChild(el("td", String(v[0]))); tr.appendChild(el("td", Math.round(100 * v[1] / v[0]) + "%"));
      t.appendChild(tr);
    });
    box.appendChild(t);
    var when = c.as_of || c.generated;
    box.appendChild(el("p", "From " + c.total + " anonymous trips over the last two years" + (when ? ", as of " + new Date(when).toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" }) : "") + ". If higher scores really mean more fish, the percentages should climb from Poor to Excellent.", "fine"));
  }).catch(function () {});
})();
