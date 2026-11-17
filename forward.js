// Loaded first on the landing page (index.html). The app used to live at this address, so links made
// before the move still arrive here. Anything that was meant for the app is sent on to map.html with
// its search and hash intact:
//   - deep links the app makes itself:  #redfish/calc   and   #tropical   (shared scores, phone alerts)
//   - sign-in, password-reset and email-change links from Supabase:  #access_token=...  #error=...  ?code=...
//   - the installed app (home-screen icon) opening cold: those icons still point at this address.
// The landing page's own anchors have no "/" in them, so they never match.
(function () {
  var h = location.hash || "", q = location.search || "";
  var deep = /^#[a-z]+\/[A-Za-z0-9_-]+$/.test(h) || h === "#tropical";
  var auth = /[#&](access_token|refresh_token|error|error_code|error_description|type)=/.test(h) || /[?&](code|error)=/.test(q);
  var standalone = (typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches) ||
    (typeof navigator !== "undefined" && navigator.standalone === true);
  // Only a cold launch (no referrer). Tapping About or the logo from inside the installed app has a referrer,
  // so it is not bounced back to the map.
  var coldLaunch = standalone && !document.referrer;
  if (deep || auth || coldLaunch) {
    document.documentElement.hidden = true;
    location.replace("map.html" + q + h);
  }
})();
