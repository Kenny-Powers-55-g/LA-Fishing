// Shared by the landing page, About, Contact, Terms and Privacy. Loaded as a blocking <script> in <head>,
// before the stylesheet, so the saved theme is applied before anything is painted (no flash).
// Same storage key and behaviour as the app (map.html): the theme you pick carries between pages.
(function () {
  "use strict";
  var root = document.documentElement;

  // Never run inside someone else's frame (a meta CSP can't set frame-ancestors).
  try { if (self !== top) { root.style.display = "none"; top.location = self.location; } } catch (e) {}

  function pref() {
    try { var t = localStorage.getItem("oc-theme"); return t === "light" || t === "dark" ? t : "auto"; }
    catch (e) { return "auto"; }
  }
  function isDark(t) {
    return t === "dark" || (t === "auto" && !(window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches));
  }
  function apply(t) {
    if (t === "auto") delete root.dataset.theme; else root.dataset.theme = t;
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute("content", isDark(t) ? "#081017" : "#ffffff");
  }
  apply(pref());   // runs now, in <head>

  var ICON = {
    auto: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor"/></svg>',
    light: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4" fill="currentColor"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    dark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" fill="currentColor"/></svg>'
  };
  var LABEL = { auto: "Theme: automatic (follows your device)", light: "Theme: light", dark: "Theme: dark" };
  var NEXT = { auto: "light", light: "dark", dark: "auto" };

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("themebtn");
    function draw() {
      if (!btn) return;
      var t = pref();
      btn.innerHTML = ICON[t];
      btn.title = LABEL[t];
      btn.setAttribute("aria-label", LABEL[t] + ". Press to change.");
    }
    if (btn) {
      draw();
      btn.addEventListener("click", function () {
        var n = NEXT[pref()];
        try { if (n === "auto") localStorage.removeItem("oc-theme"); else localStorage.setItem("oc-theme", n); } catch (e) {}
        apply(n); draw();
      });
    }
    if (window.matchMedia) {
      try { matchMedia("(prefers-color-scheme: light)").addEventListener("change", function () { if (pref() === "auto") apply("auto"); }); } catch (e) {}
    }
    // Coming from "Open the map": the visitor has just read the intro, so the app skips its own welcome card.
    var go = document.querySelectorAll("[data-cta]");
    for (var i = 0; i < go.length; i++) {
      go[i].addEventListener("click", function () { try { localStorage.setItem("oc-welcomed", "1"); } catch (e) {} });
    }
  });
})();
