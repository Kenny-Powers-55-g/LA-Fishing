<picture>
  <source media="(prefers-color-scheme: dark)" srcset="logo-dark.png">
  <img src="logo.png" alt="OceanCast — Louisiana fishing" width="420">
</picture>

# OceanCast
Live Louisiana fishing conditions and scores, built from public NOAA, USGS and weather data. 

## Pages

- `index.html` is the landing page, with `about.html`, `contact.html`, `terms.html` and `privacy.html`. They share `site.css`
  and `site.js`; `forward.js` sends old app links (deep links, sign-in links, installed icons) on to the app.
- `map.html` is the app itself (the map, scores, accounts).
- Terms and Privacy are drafts marked "needs legal review"; fill in each **[PLACEHOLDER]** on the About, Terms and Privacy pages.
- To add the phone screenshot to the landing page, save it as `phone-map.png` and replace the `<div class="shot">` block
  (the HTML comment in `index.html` shows the exact `<img>`). Add `phone-map.png` to the `cp` list in `.github/workflows/update.yml` and to `ASSETS` in `sw.js`.
- Weather data from Open-Meteo is used under CC BY 4.0 and its free tier is for non-commercial use: check its terms before charging for the app or showing ads.
- Wind, gusts, wind direction and waves come from the National Weather Service's gridded forecast (api.weather.gov)
  wherever it covers a spot: bays, sounds, Lake Pontchartrain and the coastal waters out to about 60 miles. It's official,
  free and needs no key. Inland lakes get wind only, and spots farther offshore use the Open-Meteo marine model instead.
  The data job looks up each spot's NWS grid cell once and keeps it in `stations_cache.json`.

## Accounts (optional)

People can sign in with an emailed link (no passwords). Their saved spots, their own map pins,
catch log and settings then follow them to any phone or computer. Without this setup the site
works as before and saves everything on each device only.

1. Create a free project at [supabase.com](https://supabase.com).
2. In the project, open **SQL Editor → New query**, paste everything from `supabase/setup.sql`, and press **Run**.
   This creates one private table; its rules only let a signed-in person read or change their own row.
3. Open **Authentication → URL Configuration**. Set **Site URL** to the site's address
   (for example `https://kenny-powers-55-g.github.io/LA-Fishing/`) and add `https://kenny-powers-55-g.github.io/LA-Fishing/**` (with the `**`) under **Redirect URLs**, because the
   app now lives at `map.html` and sign-in, reset and email-change links return there.
   Sign-in links only go to addresses listed here.
4. Open **Project Settings → API Keys** and copy the **Project URL** and the **publishable key** (`sb_publishable_…`).
   Never use the secret key in the site.
5. In GitHub, edit `config.js` and paste them into `supabaseUrl` and `supabaseKey`. Commit. The **Sign in** button appears
   after the next update.

### Usernames, passwords and friends

People can also make an account with just a username and password (email optional, for password resets),
add friends by username in the Friends tab, and choose for each friend whether they see their
pinned spots and/or their catch log (catch notes and amounts always stay private). A default
setting covers new friends and anyone not set individually. To turn this on in an existing project:

1. In Supabase, open **SQL Editor → New query**, paste everything from `supabase/setup.sql` again, and press **Run**.
   It's safe to run more than once; it only adds the new tables (`oc_profiles`, `oc_friends`, `oc_shares`)
   and rules. Run it again after updates that mention a new table.
2. Open **Authentication → Sign In / Providers → Email** and:
   - turn **off** "Confirm email" (username accounts have no inbox to confirm from), and
   - turn **off** "Secure email change" (otherwise adding a real email asks for a confirmation from the
     made-up username address too, which can never arrive).
   Email-link sign-in keeps working as before.

Usernames are visible to any signed-in person (that's how friends find each other). Nothing else is:
spots and catches are only readable by accepted friends, and only while sharing is switched on.

### Anonymous community reports (tune the scores from everyone's trips)

Anyone can tick **Also share anonymously to improve the scores** when they log a trip on the day they fish. No account
is needed. Once a fish has enough reports, the scores lean a little toward what the good trips had in common.
To turn it on in an existing project, run everything from `supabase/setup.sql` again in the **SQL Editor** (it's safe to
re-run). Until then the tick box still shows, and reports wait on each phone for up to 3 days.

What keeps people's spots safe:

- **Big areas only.** A report names one of the seven areas from the setup screen (Southwest, Vermilion & Acadiana,
  Terrebonne & Lafourche, New Orleans & Pontchartrain, Plaquemines & the Delta, North Louisiana lakes, Offshore).
  Never the spot, a pin, coordinates or depth. The phone works out the area and sends only its name.
- **Only what the model needs:** the fish, the result (caught / slow / nothing), the plain model's score rounded down to 5,
  and each reading's 0–1 grade rounded to 0.1. No time of day, sizes, counts, bait, notes, photos, username or account.
  Depth and light grades are left out because they hint at depth and time.
- **Not tied to anyone.** Reports go straight to the `oc_report_catch()` database function with the public key, never
  with the person's sign-in. The table has no user id, no timestamp (just the day) and no IP address.
- **Nothing to browse.** Nobody can read the table through the site. The only way out is `oc_community_stats()`, which
  returns totals and averages and leaves out any group of fewer than 5 trips. There is no feed or map of reports.
  You (the operator) can see single reports in the Supabase Table Editor (`oc_community_reports`).

How the scores use them: every hour `scripts/community.js` reads `oc_community_stats()` and writes `community.json`.
A fish is tuned only after **20 "caught" and 10 slow or empty trips**; a reading that graded higher on good trips counts
more, one that graded higher on slow trips counts less, never more than 30% either way, and less while reports are few.
One of the seven areas gets its own tuning when it passes the same bar on its own. The page, the daily snapshot and the
phone alerts all apply it, scores that use it say **Community-tuned**, and anyone can switch it off under My catches.
People's own personal tuning still takes priority on their device. Tests: `node tests/check_community.js`.

Abuse limits: 12 reports a day from one network address, 300 a day all together from senders whose address isn't
known, and 1,500 a day from everyone. The address comes only from Cloudflare's `cf-connecting-ip` header (a sender
can't fake it; `x-forwarded-for` is ignored because they can), and IPv6 senders are counted by their /64 network,
because a phone can use a new IPv6 address for every request. It is counted with a one-way code and a secret that's
thrown away daily, never stored with the report. To see whether Supabase passes that header, run
`select bucket = 'unknown' as no_address, sum(n) from public.oc_report_limits group by 1;` after a few reports: if
everything is `no_address`, reports still work but share the 300-a-day limit. The daily totals, the 20/10 minimum and
the 30% cap are what really limit how far bad reports can push a score. The public totals are worked out at most
once an hour and kept in `oc_community_snapshot`, so calling `oc_community_stats()` over and over costs almost nothing. If spam
ever gets in, delete it in the SQL Editor (for example `delete from public.oc_community_reports where reported_on = '2026-12-01';`).
To stop taking new reports, run `revoke execute on function public.oc_report_catch(text, text, text, integer, jsonb) from anon, authenticated;`
(the tick box then fails quietly and the phone drops each report after 3 days).

### Bot check (recommended)

"Confirm email" is off for username accounts, so a script could otherwise create accounts in bulk. Cloudflare
Turnstile is a free bot check, usually invisible, that Supabase supports. Do these in order (doing step 4 before
step 3 would stop everyone signing in until the site updates):

1. In the Cloudflare dashboard, open **Turnstile → Add widget**, name it OceanCast, add the hostname
   `kenny-powers-55-g.github.io`, choose **Managed**, and create it. Copy the **site key** and the **secret key**.
2. Paste the **site key** into `turnstileSiteKey` in `config.js` and commit. Never put the secret key in the repository.
3. Wait for the next update (or run "Update fishing data" in the Actions tab). The sign-in forms now show the check.
4. In Supabase, open **Authentication → Attack Protection**, turn on **CAPTCHA protection**, choose **Turnstile**,
   paste the **secret key** and save.

The iPhone app runs from its own address, which Turnstile may not accept; check sign-in there before turning step 4 on.
Each account's saved data is capped at about 1 MB (roughly 650 logged trips with their readings; photos stay on the phone).
If someone reaches it, the app tells them to download a backup and delete old trips.

Supabase's built-in email sender only allows a few sign-in emails per hour. Once more people use the site, add your own
email service under **Authentication → Emails → SMTP Settings** (Resend, SendGrid, Amazon SES and others have free tiers).

## Phone notifications

People can get their alerts (under **My alerts** in the app) as real phone notifications, even with OceanCast
closed: "Redfish at Venice / Delta Marsh: 87 now", "Speckled Trout at Barataria Bay: about 81, Sat 7 AM", or a Small
Craft Advisory at a starred spot. No account is needed. It works in Chrome, Edge, Firefox and Samsung Internet on
Android and computers, Safari on a Mac, and on iPhone (iOS 16.4 or later) once OceanCast is on the Home Screen.
The iPhone *app* doesn't get them yet (that needs Apple's own push service); inside the app, alerts still show while it's open.

To turn it on (once; until then the app works exactly as before):

1. In Supabase, open **SQL Editor → New query**, paste everything from `supabase/setup.sql`, and press **Run**
   (safe to re-run; it adds the `oc_push_devices` table and its functions).
2. Make the notification keys, once, on any computer with [Node.js](https://nodejs.org): `node scripts/webpush.js keys`.
   It prints two keys. Keep them: making new ones later stops every phone's notifications until it next opens OceanCast.
3. Paste the **public** key into `pushKey` in `config.js` and commit.
4. In GitHub, **Settings → Secrets and variables → Actions → New repository secret**:
   - `VAPID_PRIVATE_KEY`: the **private** key from step 2.
   - `SUPABASE_SECRET_KEY`: in Supabase, **Project Settings → API Keys → Secret keys**, create a key and copy it
     (`sb_secret_…`). An older `service_role` key works too. Only GitHub ever holds this key, never `config.js`.
5. After the next update, open **My alerts** on a phone: it offers **Turn on**. Within the hour that phone gets a
   "Notifications are on" message, which proves the whole path works. The run log line starting `push:` says how many
   devices were checked and sent to.

How it works:

- Turning notifications on asks the browser for a push address (from Google, Apple, Mozilla or Microsoft) and saves it in
  `oc_push_devices` with a copy of that device's alert rules and the settings that change their answers (starred spots,
  boat size, depth, personal and community tuning). It's saved through `oc_push_save()` with the public key, never
  with the person's sign-in, so it isn't tied to an account, and no name, email or network address is stored.
  A random key that stays on the device is the only way to change or delete the copy. The copy updates a few seconds
  after the person changes an alert, a star or a setting, and whenever the app opens.
- Every hour, right after the deploy, `scripts/push.js` loads each device's copy into the page's own code and asks
  `alertHits()` in `map.html` what matches, the same function that fills the My alerts card. So a notification and
  the app always agree. It then sends anything new, encrypted so only that browser can read it (`scripts/webpush.js`).
- **No repeats:** at most one notification per alert, spot and day (an area alert: one a day for the whole area). A
  warning is sent again only if it's extended. A device gets at most one push per hour; several matches are listed together.
- **Safety first:** fishing matches only count daylight windows the person's boat can go out in. A "right now" match
  is skipped while it's a no-go for their boat, and offshore fishing alerts stop while a tropical storm threatens the coast.
- **Quiet hours:** nothing between 9 PM and 6 AM Louisiana time except hurricane, tropical storm and storm surge
  warnings. Anything held back goes out after 6 AM if it still holds.
- **Cleanup:** a device is removed when someone turns notifications off, when the push service says the address is
  gone, after 10 refused sends in a row, or after 180 days with neither an app visit nor a delivered notification.
- **Limits:** 10 new devices a day from one network address, 3,000 a day from everyone, and 25,000 devices in total
  (`max_devices` in `oc_push_save()`; each row is about 2 KB, so raise it if the database has room). Only the push
  services' own addresses are ever accepted or posted to.
- Tests: `node tests/check_push.js` (encryption checked against RFC 8291's worked example, plus whole runs against a
  made-up push service). The site owner's own ntfy alerts in `alerts.json` keep working separately.

## Rules, boat launches and search pages

- **Size and bag limits** (`REGS` in `map.html`) are copied from LDWF's own saltwater table and 2026 regulations PDF,
  with the date they were checked (`REGS_CHECKED`). One-year closures such as red snapper live in `SEASON_RULES`
  (`SEASONS_CHECKED`). `node tests/check_scoring.js` warns when either is about 10 months old: re-check LDWF then.
  Fish LDWF doesn't list say so in the app instead of showing a guess.
- **Boat launches** come from OpenStreetMap (ramps tagged `leisure=slipway`, © OpenStreetMap contributors, ODbL),
  downloaded weekly into `launches.json`. Each inshore spot gets its nearest public launch within 15 miles. Missing
  or wrong ramps can be fixed by anyone at openstreetmap.org and show up within a week.
- **Search pages**: every hour `scripts/pages.js` writes a page per spot (`spots/<id>.html`) and per fish
  (`fish/<id>.html`), a directory (`spots/index.html`) and `sitemap.xml`, so search engines can find "redfish
  Lake Pontchartrain" and the like. They are built on the server only (not kept in the repository). To get them
  indexed faster, add the site to Google Search Console and submit `sitemap.xml`.

## Keeping it running

- The hourly update runs at :15, with a backup at :45 that only does the work when the live data is over
  25 minutes old. Once a day it re-enables both scheduled workflows, because GitHub switches schedules off
  after 60 days without a commit.
- It stops (and leaves the live site as it is) if it can't fetch the live history, trends or alert state,
  if far fewer spots than usual came in, if the forecast is missing for most spots, if readings are out of
  physical range, or if the history would shrink by half.
- Side steps (search pages, trends, community tuning, alerts, phone notifications) never block the update, but a
  failure in any of them turns the run red, so GitHub emails you.
- A data source that fails six calls in a row is skipped for the rest of that run, and after 20 minutes no
  new spots are started, so one slow service can't stop every deploy. A spot whose weather fetch fails keeps
  its last forecast for up to 6 hours, so its hour-by-hour view doesn't go blank.
- The hourly scripts that score spots (`snapshot.js` for the home page card and trends, `pages.js`, `alerts.js`)
  run the page's own scoring code from `map.html` through `scripts/scoring.js`, so they always agree with the app.
- Worth doing once: create a free check at healthchecks.io (period 1 hour, grace 2 hours) and save its ping
  URL as the `HEALTHCHECK_URL` repository secret; every successful update pings it, and it emails you if the
  pings stop. Also set the `USGS_API_KEY` repository secret (free at api.waterdata.usgs.gov/signup): the logs show it's empty, and the
  old USGS service the job falls back on retires in February 2027.

## iPhone app (in progress)

The page can also run inside an iPhone app built with Capacitor. Inside the app it loads the
hourly data files from the live site, set by `dataUrl` in `config.js`; the website ignores that
setting and keeps loading its own copy. If the site ever moves, change `dataUrl` and add the new
address to `connect-src` in the security policy at the top of `map.html`
(`node tests/check_page.js` checks the two match).

## Security

What the site does to keep visitors and their data safe:

- **Content Security Policy** (a `<meta>` tag at the top of `map.html`): the page may only run its own
  scripts plus two pinned libraries (named by their exact file, not their whole site) and Cloudflare's
  Turnstile folder for the bot check, and only talk to NOAA, NASA, OpenStreetMap, Open-Meteo and your own
  Supabase project (not any other `*.supabase.co`). If you change `supabaseUrl` in `config.js`, change its two
  addresses in `connect-src` too; `node tests/check_page.js` checks they match. If someone ever slipped code into a spot name or a friend's shared data, the browser
  would refuse to run it. The deploy refreshes the policy's script fingerprints automatically
  (`scripts/csp.js`); `node tests/check_page.js` warns if the repository copy is stale.
- **Pinned, fingerprinted libraries**: Leaflet and the Supabase library load at exact versions with
  `integrity` hashes, so a compromised CDN can't swap in different code.
- **Everything people type is escaped** before it's shown, and saved spots are cleaned when read
  (spots can arrive from another device, a backup file or a friend).
- **Database rules** (`supabase/setup.sql`): row level security on every table; friends only ever get
  the two lists a person chose to share, never notes; saved data is capped at about 1 MB per person;
  at most 20 unanswered friend requests at a time. Anonymous community reports can only be added through one
  checked function (daily limits, strict values) and only read back as totals of 5 or more.

Recommended Supabase settings (Authentication section of the dashboard):

- **Passwords**: set the minimum length to 8 and require letters and digits.
- **Leaked password protection** (if your plan has it): blocks passwords known from data breaches.
- **Rate limits**: leave the defaults on; they slow down password guessing and sign-up floods.
- Keep the **secret / service_role key** out of this repository. Only the publishable key belongs in `config.js`.
