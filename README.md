<picture>
  <source media="(prefers-color-scheme: dark)" srcset="logo-dark.png">
  <img src="logo.png" alt="OceanCast — Louisiana fishing" width="420">
</picture>

# OceanCast
Live Louisiana fishing conditions and scores, built from public NOAA, USGS and weather data. 

## Accounts (optional)

People can sign in with an emailed link (no passwords). Their saved spots, their own map pins,
catch log and settings then follow them to any phone or computer. Without this setup the site
works as before and saves everything on each device only.

1. Create a free project at [supabase.com](https://supabase.com).
2. In the project, open **SQL Editor → New query**, paste everything from `supabase/setup.sql`, and press **Run**.
   This creates one private table; its rules only let a signed-in person read or change their own row.
3. Open **Authentication → URL Configuration**. Set **Site URL** to the site's address
   (for example `https://kenny-powers-55-g.github.io/LA-Fishing/`) and add the same address under **Redirect URLs**.
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

## Security

What the site does to keep visitors and their data safe:

- **Content Security Policy** (a `<meta>` tag at the top of `index.html`): the page may only run its own
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
