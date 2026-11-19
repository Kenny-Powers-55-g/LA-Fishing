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

Supabase's built-in email sender only allows a few sign-in emails per hour. Once more people use the site, add your own
email service under **Authentication → Emails → SMTP Settings** (Resend, SendGrid, Amazon SES and others have free tiers).

## iPhone app (in progress)

The page can also run inside an iPhone app built with Capacitor. Inside the app it loads the
hourly data files from the live site, set by `dataUrl` in `config.js`; the website ignores that
setting and keeps loading its own copy. If the site ever moves, change `dataUrl` and add the new
address to `connect-src` in the security policy at the top of `map.html`
(`node tests/check_page.js` checks the two match).

## Security

What the site does to keep visitors and their data safe:

- **Content Security Policy** (a `<meta>` tag at the top of `map.html`): the page may only run its own
  scripts plus two pinned libraries, and only talk to NOAA, NASA, OpenStreetMap, Open-Meteo and your
  Supabase project. If someone ever slipped code into a spot name or a friend's shared data, the browser
  would refuse to run it. The deploy refreshes the policy's script fingerprints automatically
  (`scripts/csp.js`); `node tests/check_page.js` warns if the repository copy is stale.
- **Pinned, fingerprinted libraries**: Leaflet and the Supabase library load at exact versions with
  `integrity` hashes, so a compromised CDN can't swap in different code.
- **Everything people type is escaped** before it's shown, and saved spots are cleaned when read
  (spots can arrive from another device, a backup file or a friend).
- **Database rules** (`supabase/setup.sql`): row level security on every table; friends only ever get
  the two lists a person chose to share, never notes; saved data is capped at about 2 MB per person;
  at most 20 unanswered friend requests at a time.

Recommended Supabase settings (Authentication section of the dashboard):

- **Passwords**: set the minimum length to 8 and require letters and digits.
- **Leaked password protection** (if your plan has it): blocks passwords known from data breaches.
- **Rate limits**: leave the defaults on; they slow down password guessing and sign-up floods.
- Keep the **secret / service_role key** out of this repository. Only the publishable key belongs in `config.js`.
