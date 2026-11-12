// OceanCast settings you can change without touching the page code.
//
// Accounts (optional): create a free Supabase project, run supabase/setup.sql in its SQL Editor,
// then paste the project's URL and publishable key below. Steps are in README.md under "Accounts".
// Leave both empty and the site works as before, with everything saved on each device only.
// The publishable key is meant to be public: the database rules in setup.sql only ever let a
// signed-in person read and change their own data.
window.OC_CONFIG = {
  supabaseUrl: "",   // e.g. "https://abcdefghijklm.supabase.co"
  supabaseKey: ""    // e.g. "sb_publishable_..."
};
