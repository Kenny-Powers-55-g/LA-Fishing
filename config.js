// OceanCast settings you can change without touching the page code.
//
// Accounts (optional): create a free Supabase project, run supabase/setup.sql in its SQL Editor,
// then paste the project's URL and publishable key below. Steps are in README.md under "Accounts".
// Leave both empty and the site works as before, with everything saved on each device only.
// The publishable key is meant to be public: the database rules in setup.sql only ever let a
// signed-in person read and change their own data.
window.OC_CONFIG = {
  supabaseUrl: "https://qpbqepzdxkadmulkmfbj.supabase.co",   // e.g. "https://abcdefghijklm.supabase.co"
  supabaseKey: "sb_publishable_8EuhGmHdv5jJ3RaAAxUJAQ_Nc5Y4gdA",    // e.g. "sb_publishable_..."
  // Username + password accounts are stored as <username>@<this domain>. It never receives mail.
  // Leave it as is; changing it later stops existing username accounts from signing in.
  usernameEmailDomain: "users.oceancast.invalid",
  // Bot check on sign-up and sign-in (Cloudflare Turnstile, free). Leave empty until you've done the
  // steps in README.md under "Bot check": paste the Turnstile SITE key here (never the secret key).
  turnstileSiteKey: "0x4AAAAAAGRQAsS_dYzk1h1i",

  // Where the iPhone app loads the hourly data files (latest.json, forecast.json and the map layers).
  // The website ignores this and always loads its own copy, exactly as before. Must end with "/".
  dataUrl: "https://kenny-powers-55-g.github.io/LA-Fishing/"
};
