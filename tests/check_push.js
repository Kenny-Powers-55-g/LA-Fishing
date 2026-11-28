// Checks phone notifications without any network: the message encryption against the example in RFC 8291,
// the signed VAPID header, which addresses may ever be posted to, and whole runs of scripts/push.js against a
// made-up Supabase and push service (welcome message, no repeats, quiet hours, dropped phones, bad saved data).
// Run with: node tests/check_push.js
const fs = require("fs"), path = require("path"), os = require("os"), crypto = require("crypto");
const wp = require("../scripts/webpush"), push = require("../scripts/push");
let bad = 0;
const fail = m => { bad++; console.error("FAIL: " + m); };

// ---- Encryption: RFC 8291 appendix A, byte for byte ----
{
  const out = wp.encrypt("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", "BTBZMqHH6r4Tts7J_aSIgg",
    "When I grow up, I want to be a watermelon", { salt: wp.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), senderKey: "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw" });
  if (wp.b64u(out) !== "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
    fail("encryption doesn't match the RFC 8291 example");
  for (const [p, a] of [["AAAA", "BTBZMqHH6r4Tts7J_aSIgg"], ["BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4", "AAAA"]]) {
    let threw = false; try { wp.encrypt(p, a, "x"); } catch (e) { threw = true; }
    if (!threw) fail("encrypt must refuse a malformed browser key");
  }
}

// A browser on the receiving end: decrypts what the sender posts
const ua = crypto.createECDH("prime256v1"); ua.generateKeys();
const uaAuth = crypto.randomBytes(16);
function decrypt(body) {
  const H = (k, d) => crypto.createHmac("sha256", k).update(d).digest();
  const salt = body.subarray(0, 16), n = body[20], as = body.subarray(21, 21 + n), ct = body.subarray(21 + n);
  const prk = H(salt, H(H(uaAuth, ua.computeSecret(as)), Buffer.concat([Buffer.from("WebPush: info\0"), ua.getPublicKey(), as, Buffer.from([1])])));
  const d = crypto.createDecipheriv("aes-128-gcm", H(prk, Buffer.from("Content-Encoding: aes128gcm\0\x01")).subarray(0, 16), H(prk, Buffer.from("Content-Encoding: nonce\0\x01")).subarray(0, 12));
  d.setAuthTag(ct.subarray(-16));
  const pt = Buffer.concat([d.update(ct.subarray(0, -16)), d.final()]);
  return JSON.parse(pt.subarray(0, pt.lastIndexOf(2)).toString());
}
const sub = n => ({ endpoint: `https://fcm.googleapis.com/fcm/send/device-${n}`, p256dh: wp.b64u(ua.getPublicKey()), auth: wp.b64u(uaAuth) });

// ---- VAPID ----
const keys = wp.makeKeys();
{
  const v = wp.vapid(keys.privateKey, "https://example.org/oceancast");
  if (v.publicKey !== keys.publicKey) fail("the public key worked out from the private key should match the pair");
  const m = /^vapid t=([^.]+)\.([^.]+)\.([^,]+), k=(.+)$/.exec(v.header("https://web.push.apple.com/abc", Date.parse("2026-06-01T12:00:00Z")));
  if (!m) fail("VAPID header format");
  else {
    const pub = wp.unb64u(keys.publicKey), claims = JSON.parse(wp.unb64u(m[2]));
    const pk = crypto.createPublicKey({ format: "jwk", key: { kty: "EC", crv: "P-256", x: wp.b64u(pub.subarray(1, 33)), y: wp.b64u(pub.subarray(33)) } });
    if (!crypto.verify("sha256", Buffer.from(`${m[1]}.${m[2]}`), { key: pk, dsaEncoding: "ieee-p1363" }, wp.unb64u(m[3]))) fail("VAPID signature doesn't verify");
    if (claims.aud !== "https://web.push.apple.com" || claims.sub !== "https://example.org/oceancast") fail("VAPID audience/subject " + JSON.stringify(claims));
    if (claims.exp - Date.parse("2026-06-01T12:00:00Z") / 1000 > 24 * 3600) fail("VAPID expiry must be under 24 hours");
    if (m[4] !== keys.publicKey) fail("VAPID k= must be the public key");
  }
  for (const [k, s] of [["short", "https://x.org"], [keys.privateKey, "x.org"]]) {
    let threw = false; try { wp.vapid(k, s); } catch (e) { threw = true; }
    if (!threw) fail(`vapid() must refuse ${k === "short" ? "a bad key" : "a subject that isn't https: or mailto:"}`);
  }
}

// ---- Only real push services, the same list setup.sql enforces ----
{
  const yes = ["https://fcm.googleapis.com/fcm/send/abc:DEF", "https://updates.push.services.mozilla.com/wpush/v2/gAAA", "https://web.push.apple.com/QWx0",
    "https://wns2-bl2p.notify.windows.com/w/?token=BQYAAA%3d%3d"];
  const no = ["http://fcm.googleapis.com/x", "https://fcm.googleapis.com.evil.com/x", "https://evil.com/fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x",
    "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com/", "https://localhost/x", "https://169.254.169.254/x", "https://notify.windows.com.evil.org/x",
    "https://fcm.googleapis.com/x y", "https://fcm.googleapis.com/" + "a".repeat(1100), null, 42];
  for (const u of yes) if (!wp.endpointOk(u)) fail("should accept " + u);
  for (const u of no) if (wp.endpointOk(u)) fail("should refuse " + String(u).slice(0, 60));
  // setup.sql must list the same hosts
  const sql = fs.readFileSync(path.join(__dirname, "..", "supabase", "setup.sql"), "utf8");
  if (!sql.includes(String.raw`^https://(fcm\.googleapis\.com|updates\.push\.services\.mozilla\.com|web\.push\.apple\.com|[a-z0-9-]+\.notify\.windows\.com)/[!-~]+$`))
    fail("setup.sql's push address check should match scripts/webpush.js");
}

// ---- send(): what each push service answer means ----
(async () => {
  const signer = wp.vapid(keys.privateKey, "https://example.org");
  const answer = status => async (url, o) => { answer.last = { url, o }; return new Response(status === 201 ? "" : "nope", { status }); };
  const s = sub(0);
  let r = await wp.send(s, { title: "t" }, signer, { fetchFn: answer(201), ttl: 60, urgency: "high" });
  if (!r.ok) fail("201 should be ok");
  const h = answer.last.o.headers;
  if (h["Content-Encoding"] !== "aes128gcm" || h.TTL !== "60" || h.Urgency !== "high" || !/^vapid t=/.test(h.Authorization) || answer.last.o.redirect !== "error")
    fail("push request headers " + JSON.stringify(h));
  if (decrypt(Buffer.from(answer.last.o.body)).title !== "t") fail("the browser should be able to read the message");
  for (const [st, want] of [[410, "gone"], [404, "gone"], [429, "retry"], [503, "retry"], [403, "refused"], [400, "refused"]]) {
    r = await wp.send(s, { title: "t" }, signer, { fetchFn: answer(st) });
    const got = r.ok ? "ok" : r.gone ? "gone" : r.retry ? "retry" : "refused";
    if (got !== want) fail(`push service answer ${st} should count as ${want}, got ${got}`);
  }
  let fetched = false;
  r = await wp.send({ ...s, endpoint: "https://evil.example/x" }, { title: "t" }, signer, { fetchFn: async () => { fetched = true; return new Response("", { status: 201 }); } });
  if (fetched || !r.gone) fail("an address that isn't a push service must never be posted to");
  r = await wp.send(s, { title: "t" }, signer, { fetchFn: async () => { throw new Error("socket hang up"); } });
  if (!r.retry) fail("a network error should be retried next hour");

  // ---- Whole runs of scripts/push.js ----
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "oc-push-"));
  try {
    const cond = { temp: 70, wind: 8, tide: "Incoming", sal: 18, clar: "Stained", pres: "Steady", cloud: 30, trange: 1.2, dtemp: 0, front: 0, rain: 0 };
    const ends = new Date(Date.now() + 30 * 36e5).toISOString();
    fs.writeFileSync(path.join(dir, "latest.json"), JSON.stringify({ generated: new Date().toISOString(), spots: [
      { id: "barat", name: "Barataria Bay", lat: 29.27, lon: -89.95, zone: "inshore", conditions: cond, quality: {}, alerts: [{ event: "Small Craft Advisory", ends }] },
      { id: "cameron", name: "Cameron <Jetties>", lat: 29.77, lon: -93.34, zone: "inshore", conditions: cond, quality: {}, alerts: [{ event: "Hurricane Warning", ends }] },
      { id: "toledo", name: "Toledo Bend", lat: 31.4, lon: -93.7, zone: "inshore", conditions: { ...cond, sal: 0.1 }, quality: {} }] }));
    fs.writeFileSync(path.join(dir, "forecast.json"), JSON.stringify({ generated: new Date().toISOString(), spots: {} }));
    const DAYTIME = Date.parse("2026-12-05T17:00:00Z"), NIGHT = Date.parse("2026-12-05T09:00:00Z");   // 11 AM and 3 AM in Louisiana
    if (push.isQuietHour(DAYTIME) || !push.isQuietHour(NIGHT)) fail("quiet hours should be 9 PM to 6 AM Louisiana time");
    const config = { url: "https://example.supabase.co", pushKey: keys.publicKey };
    const env = { SUPABASE_SECRET_KEY: "sb_secret_test", VAPID_PRIVATE_KEY: keys.privateKey, SITE_URL: "https://example.org/site" };
    const warnRule = (id, where) => ({ id, type: "warn", where });
    let devices = [
      { device_id: "a", ...sub(1), watch: { rules: [warnRule("w1", "barat")], fish: "trout" }, sent: {}, fails: 0 },
      { device_id: "b", ...sub(2), watch: { rules: [warnRule("w2", "mine")], favs: ["cameron", "barat", "../x", 7] }, sent: { hello: "2026-12-04" }, fails: 0 },
      { device_id: "c", endpoint: "https://evil.example/x", p256dh: "x", auth: "y", watch: {}, sent: {}, fails: 0 },
      { device_id: "d", ...sub(4), watch: ["not", "an", "object"], sent: "nope", fails: 0 },
      { device_id: "e", ...sub(5), watch: { rules: "x", favs: { a: 1 }, boat: { x: 1 }, depth: "deep", tune: { fish: { redfish: { wind: 99 } } } }, sent: { hello: "2026-12-04" }, fails: 0 },
      // A fishing score alert goes through the page's real scoring, so whether it matches depends on today's light,
      // moon and season: only its shape is checked
      { device_id: "f", ...sub(6), watch: { rules: [{ id: "s1", type: "score", sp: "redfish", where: "barat", min: 55, days: "any" }], boat: "large", tune: { fish: { redfish: { wind: 1.1 } } } }, sent: {}, fails: 0 },
    ];
    const world = { pushes: [], marks: [], status: () => 201 };
    const fakeFetch = async (url, o = {}) => {
      if (url.startsWith("https://example.supabase.co/rest/v1/oc_push_devices?")) {
        if (o.headers.apikey !== "sb_secret_test" || o.headers.Authorization) fail("the device list must be read with the secret key as apikey");
        return new Response(JSON.stringify(devices), { status: 200 });
      }
      if (url === "https://example.supabase.co/rest/v1/rpc/oc_push_mark") { world.marks.push(JSON.parse(o.body)); return new Response(null, { status: 204 }); }
      if (url.startsWith("https://fcm.googleapis.com/")) {
        const st = world.status(url);
        if (st === 201) world.pushes.push({ to: url.split("-").pop(), m: decrypt(Buffer.from(o.body)), urgency: o.headers.Urgency, ttl: +o.headers.TTL });
        return new Response(st === 201 ? "" : "refused", { status: st });
      }
      fail("unexpected request to " + url);
      return new Response("", { status: 500 });
    };
    const go = (now, extra = {}) => { world.pushes = []; world.marks = []; return push.run({ env, dir, fetchFn: fakeFetch, now, config, log: () => {}, ...extra }); };
    const sentFor = id => { for (const m of world.marks) for (const x of m.p_sent) if (x.device_id === id) return x.sent; return null; };
    const goneIds = () => world.marks.flatMap(m => m.p_gone.map(g => g.device_id));

    // Not set up: says so, touches nothing
    let touched = false;
    const r0 = await push.run({ env: {}, dir, config: { url: "", pushKey: "" }, log: () => {}, fetchFn: async () => { touched = true; } });
    if (!r0.skipped || touched) fail("without keys the script should only explain what's missing");
    // Keys that don't belong together: stop loudly
    let threw = false;
    try { await push.run({ env: { ...env, VAPID_PRIVATE_KEY: wp.makeKeys().privateKey }, dir, fetchFn: fakeFetch, config, log: () => {} }); } catch (e) { threw = /doesn't belong/.test(e.message); }
    if (!threw) fail("a VAPID private key that doesn't match config.js pushKey should stop the run");

    // 1. Daytime: a's warning (with the welcome folded in), b's two warnings together, d's welcome, e has nothing new
    await go(DAYTIME);
    const pa = world.pushes.find(p => p.to === "1"), pb = world.pushes.find(p => p.to === "2"), pd = world.pushes.find(p => p.to === "4");
    if (!pa || !/Small Craft Advisory/.test(pa.m.title) || !/Barataria Bay/.test(pa.m.body) || pa.m.url !== "map.html#trout/barat") fail("device a should get its warning, linked to its own fish: " + JSON.stringify(pa));
    if (!pb || !/^2 OceanCast alerts$/.test(pb.m.title) || !/Hurricane Warning at Cameron Jetties/.test(pb.m.body) || !/Small Craft Advisory at Barataria Bay/.test(pb.m.body) || pb.urgency !== "high")
      fail("device b should get both warnings in one push, urgent: " + JSON.stringify(pb));
    if (world.pushes.some(p => /[<>]/.test(p.m.title + p.m.body))) fail("spot names should arrive cleaned the way the page cleans them");
    if (!pd || pd.m.title !== "Notifications are on") fail("a device with unreadable settings still gets the welcome: " + JSON.stringify(pd));
    if (world.pushes.some(p => p.to === "5")) fail("device e has nothing to send");
    const pf = world.pushes.find(p => p.to === "6");
    if (!pf || !(pf.m.title === "Notifications are on" || /^Redfish at Barataria Bay: (about \d+|\d+ now)$/.test(pf.m.title) && /^map\.html#redfish\/barat$/.test(pf.m.url)))
      fail("a fishing alert should read like 'Redfish at Barataria Bay: about 80': " + JSON.stringify(pf));
    if (world.pushes.some(p => p.m.title === "Notifications are on" && p.to === "1")) fail("the welcome should fold into a real alert, not come separately");
    if (!goneIds().includes("c") || world.pushes.some(p => p.to === "x")) fail("a device whose address isn't a push service should be removed, never posted to");
    if (world.pushes.some(p => !(p.ttl > 0 && p.ttl <= 2 * 86400))) fail("every push needs a TTL of up to two days");
    const sa = sentFor("a");
    if (!sa || !sa.hello || !Object.keys(sa).some(k => k.startsWith("w1|w|barat|Small Craft Advisory|"))) fail("what was sent to a should be recorded: " + JSON.stringify(sa));

    // 2. The same hour again, with what was recorded: nothing repeats
    devices = devices.filter(d => d.device_id !== "c").map(d => ({ ...d, sent: sentFor(d.device_id) || d.sent }));
    await go(DAYTIME + 36e5);
    if (world.pushes.length) fail("nothing should be sent twice: " + JSON.stringify(world.pushes.map(p => p.m.title)));

    // 3. 3 AM: a Small Craft Advisory waits for morning, a Hurricane Warning doesn't
    devices = devices.map(d => ({ ...d, sent: { hello: "2026-12-04" } }));
    await go(NIGHT);
    const nb = world.pushes.find(p => p.to === "2"), na = world.pushes.find(p => p.to === "1");
    if (na) fail("a Small Craft Advisory shouldn't wake anyone at 3 AM");
    if (!nb || !/Hurricane Warning/.test(nb.m.title) || /Small Craft/.test(nb.m.body)) fail("a Hurricane Warning should still go out at 3 AM, on its own: " + JSON.stringify(nb));

    // 4. A phone that dropped the subscription (410) is removed; a refused send counts a failure
    devices = devices.map(d => ({ ...d, sent: {} }));
    world.status = url => (url.endsWith("-1") ? 410 : url.endsWith("-2") ? 403 : 201);
    await go(DAYTIME);
    if (!goneIds().includes("a")) fail("a device the push service says is gone (410) should be removed");
    const fb = world.marks.flatMap(m => m.p_sent).find(x => x.device_id === "b");
    if (!fb || fb.fails !== 1 || Object.keys(fb.sent).length) fail("a refused send should count one failure and record nothing as sent: " + JSON.stringify(fb));
    // ...and is dropped once it has failed 10 times in a row
    devices = devices.map(d => (d.device_id === "b" ? { ...d, fails: 9 } : d));
    await go(DAYTIME);
    if (!goneIds().includes("b")) fail("a device refused 10 times in a row should be removed");

    // 5. Every send refused: the run fails so the workflow turns red (it's the keys, not the phones)
    world.status = () => 403;
    devices = [1, 2, 3].map(n => ({ device_id: "z" + n, ...sub(10 + n), watch: {}, sent: {}, fails: 0 }));
    threw = false;
    try { await go(DAYTIME); } catch (e) { threw = /refused/.test(e.message); }
    if (!threw) fail("a run where every notification is refused should fail");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }

  // ---- The saved copy is checked again before use ----
  const st = push.deviceSettings({ rules: new Array(30).fill({}), favs: ["barat", "nope", 5, "../x"], boat: "BOAT!", depth: -3 }, new Set(["barat"]));
  if (JSON.parse(st.get("oc-alerts")).length !== 20 || st.get("la-fish-favs") !== '["barat"]' || st.has("boat") || st.has("la-fish-depth"))
    fail("device settings should be trimmed and checked: " + JSON.stringify([...st]));
  const t = push.cleanTune({ fish: { redfish: { wind: 1.2, tide: 9, "x y": 1 }, trout: {} }, group: { salt: { temp: 0.8 } }, other: 1 });
  if (JSON.stringify(t) !== '{"fish":{"redfish":{"wind":1.2},"trout":{}},"group":{"salt":{"temp":0.8}}}') fail("tuning should keep only sane multipliers: " + JSON.stringify(t));

  console.log(bad ? `${bad} problem(s)` : "phone notifications OK");
  process.exit(bad ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
