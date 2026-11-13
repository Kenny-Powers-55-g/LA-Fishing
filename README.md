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
add friends by username, and choose whether friends see their pinned spots and/or their catch log
(catch notes always stay private). To turn this on in an existing project:

1. In Supabase, open **SQL Editor → New query**, paste everything from `supabase/setup.sql` again, and press **Run**.
   It's safe to run more than once; it only adds the new tables (`oc_profiles`, `oc_friends`) and rules.
2. Open **Authentication → Sign In / Providers → Email** and:
   - turn **off** "Confirm email" (username accounts have no inbox to confirm from), and
   - turn **off** "Secure email change" (otherwise adding a real email asks for a confirmation from the
     made-up username address too, which can never arrive).
   Email-link sign-in keeps working as before.

Usernames are visible to any signed-in person (that's how friends find each other). Nothing else is:
spots and catches are only readable by accepted friends, and only while sharing is switched on.

Supabase's built-in email sender only allows a few sign-in emails per hour. Once more people use the site, add your own
email service under **Authentication → Emails → SMTP Settings** (Resend, SendGrid, Amazon SES and others have free tiers).
