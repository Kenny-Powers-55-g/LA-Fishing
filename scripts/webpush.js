// Web Push without extra packages: the message encryption every browser expects (RFC 8291, "aes128gcm")
// and the signed "this is OceanCast" header push services ask for (VAPID, RFC 8292), using Node's own crypto.
// scripts/push.js uses it to send phone notifications.
//
//   node scripts/webpush.js keys     (makes a new key pair: the public one goes in config.js as pushKey,
//                                     the private one in the VAPID_PRIVATE_KEY repository secret)
const crypto = require("crypto");

const b64u = buf => Buffer.from(buf).toString("base64url");
const unb64u = s => Buffer.from(String(s || "").replace(/=+$/, ""), "base64url");
const hmac = (key, data) => crypto.createHmac("sha256", key).update(data).digest();

// The push services browsers use: Google (Chrome, Edge on Android, Samsung, Opera, Brave), Mozilla (Firefox),
// Apple (Safari and iPhone Home Screen apps) and Microsoft (Edge on Windows). The sender only ever posts to
// these, so an address saved in the database can't point it anywhere else. setup.sql checks the same list.
const PUSH_HOST = /^(fcm\.googleapis\.com|updates\.push\.services\.mozilla\.com|web\.push\.apple\.com|[a-z0-9-]+\.notify\.windows\.com)$/;
function endpointOk(ep) {
  if (typeof ep !== "string" || ep.length > 1024 || !/^https:\/\/[!-~]+$/.test(ep)) return false;
  try {
    const u = new URL(ep);
    return u.protocol === "https:" && !u.port && !u.username && !u.password && PUSH_HOST.test(u.hostname) && u.pathname.length > 1;
  } catch (e) { return false; }
}

// Encrypts one message for one browser. p256dh and auth are the browser's keys from its push subscription.
// salt and senderKey are only passed by the tests (the RFC's example uses fixed ones); normally both are random.
function encrypt(p256dh, auth, plaintext, { salt = crypto.randomBytes(16), senderKey = null } = {}) {
  const ua = unb64u(p256dh), secret = unb64u(auth);
  if (ua.length !== 65 || ua[0] !== 4) throw new Error("the browser's p256dh key isn't a P-256 public key");
  if (secret.length !== 16) throw new Error("the browser's auth secret isn't 16 bytes");
  const ecdh = crypto.createECDH("prime256v1");
  if (senderKey) ecdh.setPrivateKey(unb64u(senderKey)); else ecdh.generateKeys();
  const as = ecdh.getPublicKey();                                   // 65 bytes, uncompressed
  const shared = ecdh.computeSecret(ua);
  // RFC 8291 section 3.4: mix in the browser's auth secret, then RFC 8188's content key and nonce
  const ikm = hmac(hmac(secret, shared), Buffer.concat([Buffer.from("WebPush: info\0"), ua, as, Buffer.from([1])]));
  const prk = hmac(salt, ikm);
  const cek = hmac(prk, Buffer.from("Content-Encoding: aes128gcm\0\x01")).subarray(0, 16);
  const nonce = hmac(prk, Buffer.from("Content-Encoding: nonce\0\x01")).subarray(0, 12);
  const data = Buffer.concat([Buffer.from(plaintext), Buffer.from([2])]);   // 2 = the last (and only) record
  const c = crypto.createCipheriv("aes-128-gcm", cek, nonce);
  const sealed = Buffer.concat([c.update(data), c.final(), c.getAuthTag()]);
  const head = Buffer.alloc(21);
  salt.copy(head, 0); head.writeUInt32BE(4096, 16); head[20] = as.length;
  return Buffer.concat([head, as, sealed]);
}

// A P-256 key pair for VAPID, as base64url: publicKey is the 65-byte point, privateKey the 32-byte secret.
function makeKeys() {
  const e = crypto.createECDH("prime256v1");
  e.generateKeys();
  return { publicKey: b64u(e.getPublicKey()), privateKey: b64u(pad32(e.getPrivateKey())) };
}
function pad32(b) { return b.length >= 32 ? b : Buffer.concat([Buffer.alloc(32 - b.length), b]); }

// The VAPID signer for one run. subject is a contact address for the push services (an https: or mailto: URL).
// Signed headers are reused per push service for the run (they're good for 12 hours).
function vapid(privateKey, subject) {
  const d = unb64u(privateKey);
  if (d.length !== 32) throw new Error("VAPID_PRIVATE_KEY should be 32 bytes in base64url (make one with: node scripts/webpush.js keys)");
  if (!/^(https:\/\/|mailto:)/.test(subject || "")) throw new Error("the VAPID subject must be an https: or mailto: address");
  const e = crypto.createECDH("prime256v1");
  e.setPrivateKey(d);
  const pub = e.getPublicKey(), publicKey = b64u(pub);
  const key = crypto.createPrivateKey({ format: "jwk", key: { kty: "EC", crv: "P-256", d: b64u(d), x: b64u(pub.subarray(1, 33)), y: b64u(pub.subarray(33)) } });
  const cache = new Map();
  function header(endpoint, now = Date.now()) {
    const aud = new URL(endpoint).origin, hit = cache.get(aud);
    if (hit && hit.until > now + 60e3) return hit.v;
    const exp = Math.floor(now / 1000) + 12 * 3600;
    const h = b64u(JSON.stringify({ typ: "JWT", alg: "ES256" })), p = b64u(JSON.stringify({ aud, exp, sub: subject }));
    const sig = crypto.sign("sha256", Buffer.from(`${h}.${p}`), { key, dsaEncoding: "ieee-p1363" });
    const v = `vapid t=${h}.${p}.${b64u(sig)}, k=${publicKey}`;
    cache.set(aud, { v, until: exp * 1000 });
    return v;
  }
  return { publicKey, header };
}

// Sends one notification. sub = {endpoint, p256dh, auth}; payload is any JSON (kept well under 4 KB).
// Answers {ok, status, gone, retry, why}: gone means the browser dropped this subscription for good.
async function send(sub, payload, signer, { ttl = 3600, urgency = "normal", topic = null, fetchFn = fetch } = {}) {
  if (!endpointOk(sub.endpoint)) return { ok: false, status: 0, gone: true, why: "not a known push service address" };
  const text = JSON.stringify(payload);
  if (Buffer.byteLength(text) > 3000) throw new Error("notification too long");
  const headers = { "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", TTL: String(Math.max(0, Math.round(ttl))),
    Urgency: urgency, Authorization: signer.header(sub.endpoint) };
  if (topic && /^[A-Za-z0-9_-]{1,32}$/.test(topic)) headers.Topic = topic;
  let r;
  try {
    r = await fetchFn(sub.endpoint, { method: "POST", headers, body: encrypt(sub.p256dh, sub.auth, text), redirect: "error", signal: AbortSignal.timeout(15000) });
  } catch (e) { return { ok: false, status: 0, retry: true, why: e.message }; }
  const status = r.status;
  if (status >= 200 && status < 300) return { ok: true, status };
  let why = "";
  try { why = (await r.text()).slice(0, 200).replace(/\s+/g, " "); } catch (e) {}
  // 404 / 410: the subscription expired or the person turned notifications off in the browser
  if (status === 404 || status === 410) return { ok: false, status, gone: true, why };
  // 429 and 5xx: the push service is busy; try again next hour. Anything else is a problem with this message or key.
  return { ok: false, status, retry: status === 429 || status >= 500, why };
}

module.exports = { encrypt, makeKeys, vapid, send, endpointOk, b64u, unb64u };

if (require.main === module) {
  if (process.argv[2] !== "keys") { console.log("Usage: node scripts/webpush.js keys"); process.exit(1); }
  const k = makeKeys();
  console.log("New notification keys. Make them once and keep them: changing them later turns off every phone's notifications until it opens OceanCast again.\n");
  console.log(`1. Public key: paste into config.js as pushKey:\n   ${k.publicKey}\n`);
  console.log(`2. Private key: save as the VAPID_PRIVATE_KEY repository secret (never commit it):\n   ${k.privateKey}`);
}
