// Emotes Downloader backend (Cloudflare Worker).
//
// Holds the public bot's token and the OAuth client secret, so they never ship inside the app.
// Rules this file must keep:
//   * The browser / desktop app never gets the bot's own server list. /api/guilds only returns the
//     logged-in user's servers, split into "ready" (bot in + user can add emojis) and "invite"
//     (bot not in + user can add bots).
//   * Every action on a server re-checks, live and with the bot, that this user is in that server and
//     has the permission. A guild_id from the client is never trusted on its own.
//   * Session tokens are only stored hashed; Discord tokens only encrypted.

import PAGE from "./web/index.html";
import CSS from "./web/style.css";
import CLIENT_JS from "./web/app.client.js";
import ICON from "./web/icon.png";
import { Buffer } from "node:buffer";
import { pieceGrid, pieceNames, cutWithImages, shrinkWithImages } from "./pieces.js";
import { botBio } from "./profile.js";

const ADMIN = 1n << 3n;
const MANAGE_GUILD = 1n << 5n;
const MANAGE_EXPR = 1n << 30n;
const CREATE_EXPR = 1n << 43n;
const CAN_UPLOAD = ADMIN | MANAGE_EXPR | CREATE_EXPR;
const CAN_MANAGE = ADMIN | MANAGE_EXPR;
const CAN_INVITE = ADMIN | MANAGE_GUILD;
const BOT_PERMS = MANAGE_EXPR | CREATE_EXPR;

const EMOJI_LIMITS = [50, 100, 150, 250];
const STICKER_LIMITS = [5, 15, 30, 60];
const MAX_EMOJI = 256 * 1024;
const MAX_STICKER = 512 * 1024;
const UPLOADS_PER_HOUR = 200;
const SESSION_DAYS = 30;
const APP_REDIRECT = "http://127.0.0.1:53682/callback";   // the desktop app's local login catcher (fixed)

// ---------------------------------------------------------------- small helpers

const enc = new TextEncoder();
const now = () => Date.now();

function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

class HttpError extends Error {
  constructor(status, message, extra = {}) {
    super(message);
    this.status = status;
    this.extra = extra;
  }
}

const fail = (status, message, extra) => { throw new HttpError(status, message, extra); };

function b64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64(bytes) {
  return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString("base64");   // native, fast
}

function unb64(text) {
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function randomToken(n = 32) {
  return b64url(crypto.getRandomValues(new Uint8Array(n)));
}

async function sha256hex(text) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(text)));
  return [...d].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256b64url(text) {
  return b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(text))));
}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

function cookies(req) {
  const out = {};
  for (const part of (req.headers.get("cookie") || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

function cleanEmojiName(name, fallback = "emote") {
  let n = String(name || "").replace(/[^A-Za-z0-9_]/g, "_").replace(/_+/g, "_").replace(/^_+|_+$/g, "");
  if (!n) n = fallback;
  if (n.length < 2) n += "_";
  return n.slice(0, 32);
}

const EMOJI_NAME = /^[A-Za-z0-9_]{2,32}$/;
const SNOWFLAKE = /^\d{15,21}$/;

// ---------------------------------------------------------------- encryption of stored Discord tokens

let _aesKey = null;
async function aesKey(env) {
  if (_aesKey) return _aesKey;
  const base = await crypto.subtle.importKey("raw", enc.encode(env.DISCORD_CLIENT_SECRET), "HKDF", false, ["deriveKey"]);
  _aesKey = await crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: enc.encode("emotes-downloader"), info: enc.encode("token-encryption-v1") },
    base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  return _aesKey;
}

async function seal(env, text) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await aesKey(env), enc.encode(text)));
  const out = new Uint8Array(iv.length + ct.length);
  out.set(iv);
  out.set(ct, iv.length);
  return b64(out);
}

async function unseal(env, sealed) {
  const raw = unb64(sealed);
  const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: raw.subarray(0, 12) }, await aesKey(env), raw.subarray(12));
  return new TextDecoder().decode(pt);
}

// ---------------------------------------------------------------- Discord API

async function discord(env, method, path, opts = {}) {
  const headers = { "User-Agent": "DiscordBot (https://github.com/ISdook/emotes-downloader, 2)" };
  if (opts.bot) headers.Authorization = `Bot ${env.DISCORD_BOT_TOKEN}`;
  else if (opts.bearer) headers.Authorization = `Bearer ${opts.bearer}`;
  if (opts.reason) headers["X-Audit-Log-Reason"] = encodeURIComponent(opts.reason).slice(0, 500);
  let body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.json);
  } else if (opts.form) {
    headers["Content-Type"] = "application/x-www-form-urlencoded";
    body = new URLSearchParams(opts.form).toString();
  } else if (opts.formData) {
    body = opts.formData;
  }
  const url = path.startsWith("http") ? path : (env.DISCORD_API || "https://discord.com/api/v10") + path;
  for (let attempt = 0; ; attempt++) {
    const r = await fetch(url, { method, headers, body });
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { message: text.slice(0, 200) }; }
    if (r.status === 429) {
      const wait = Number((data && data.retry_after) || r.headers.get("retry-after") || 1);
      if (wait <= 2 && attempt < 2 && !opts.formData) {          // short waits: just wait here
        await new Promise((res) => setTimeout(res, wait * 1000 + 200));
        continue;
      }
      return { status: 429, data: { message: "Discord asked to slow down.", retry_after: wait, code: 0 } };
    }
    return { status: r.status, data };
  }
}

const DISCORD_ERRORS = {
  10004: "The bot isn't in this server anymore.",
  10014: "That emoji doesn't exist anymore.",
  30008: "No free emoji slots left in this server.",
  30039: "No free sticker slots left in this server.",
  50001: "The bot can't see this server. Invite it again.",
  50013: "The bot is missing the “Manage Expressions” permission in this server.",
  50035: "Discord didn't accept this. Check the name (2–32 letters, numbers or _) and the file.",
  50045: "The file is too big for Discord.",
  50138: "Discord couldn't resize this image. Make it smaller first.",
};

function discordError(r) {
  const code = (r.data && r.data.code) || 0;
  const msg = DISCORD_ERRORS[code] || (r.data && r.data.message) || `Discord answered ${r.status}.`;
  const status = r.status === 429 ? 429 : r.status >= 500 ? 502 : r.status === 401 ? 502 : r.status;
  return new HttpError(status, msg, { code, retry_after: r.data && r.data.retry_after });
}

// Small shared caches (per Worker instance, plus the edge cache for a few seconds).
async function cached(key, ttl, fn) {
  const cache = caches.default;
  const req = new Request(`https://cache.emotes-downloader.internal/${key}`);
  const hit = await cache.match(req);
  if (hit) return hit.json();
  const value = await fn();
  await cache.put(req, new Response(JSON.stringify(value), { headers: { "cache-control": `max-age=${ttl}` } }));
  return value;
}

async function forgetCache(key) {
  await caches.default.delete(new Request(`https://cache.emotes-downloader.internal/${key}`));
}

// IDs of the servers the bot is in. Server-side only, never sent to anyone.
async function botGuildIds(env) {
  return cached("bot-guilds", 60, async () => {
    const ids = [];
    let after = "";
    for (let i = 0; i < 50; i++) {
      const r = await discord(env, "GET", `/users/@me/guilds?limit=200${after ? `&after=${after}` : ""}`, { bot: true });
      if (r.status !== 200) throw discordError(r);
      ids.push(...r.data.map((g) => g.id));
      if (r.data.length < 200) break;
      after = r.data[r.data.length - 1].id;
    }
    return ids;
  });
}

// How many servers the bot is in (just the number; Discord keeps it up to date).
async function serverCount(env) {
  return cached("server-count", 60, async () => {
    const r = await discord(env, "GET", "/applications/@me", { bot: true });
    if (r.status !== 200) throw discordError(r);
    // approximate_guild_count can lag behind (it showed 0), so count the bot's servers ourselves.
    const ids = await botGuildIds(env).catch(() => null);
    const servers = ids ? ids.length : Number(r.data.approximate_guild_count || 0);
    return { servers, description: r.data.description || "" };
  });
}

// Keeps the bot's bio up to date with the server count (cron, every 30 minutes).
async function updateBio(env) {
  await forgetCache("server-count");
  await forgetCache("bot-guilds");
  const { servers, description } = await serverCount(env);
  const bio = botBio(env.PUBLIC_URL, servers);
  if (bio === description) return;
  const r = await discord(env, "PATCH", "/applications/@me", { bot: true, json: { description: bio } });
  if (r.status !== 200) console.log("bio not updated:", r.status, JSON.stringify(r.data).slice(0, 200));
  await forgetCache("server-count");
}

function computePerms(guild, member, userId) {
  if (guild.owner_id === userId) return { perms: (1n << 53n) - 1n, owner: true };
  const roles = new Map((guild.roles || []).map((r) => [r.id, BigInt(r.permissions || "0")]));
  let p = roles.get(guild.id) || 0n;                 // @everyone
  for (const rid of member.roles || []) p |= roles.get(rid) || 0n;
  return { perms: p, owner: false };
}

// Live check with the bot: is this user in this server right now, with this permission?
async function authorize(env, guildId, userId, need) {
  if (!SNOWFLAKE.test(guildId)) fail(400, "That isn't a server ID.");
  const guild = await cached(`guild-${guildId}`, 30, async () => {
    const r = await discord(env, "GET", `/guilds/${guildId}`, { bot: true });
    if (r.status === 403 || r.status === 404) return { missing: true };
    if (r.status !== 200) throw discordError(r);
    return { id: r.data.id, name: r.data.name, icon: r.data.icon, owner_id: r.data.owner_id, roles: r.data.roles };
  });
  if (guild.missing) fail(404, "The bot isn't in this server. Invite it first.", { code: "bot_not_in" });
  const m = await discord(env, "GET", `/guilds/${guildId}/members/${userId}`, { bot: true });   // live, every time
  if (m.status === 404) fail(403, "You're not in this server.", { code: "not_member" });
  if (m.status !== 200) throw discordError(m);
  const { perms } = computePerms(guild, m.data, userId);
  if (!(perms & need)) {
    fail(403, need === CAN_MANAGE
      ? "You need the “Manage Expressions” permission in this server."
      : "You need the “Manage Expressions” or “Create Expressions” permission in this server.",
      { code: "no_permission" });
  }
  return { guild, perms };
}

async function guildDetails(env, guildId) {
  const r = await discord(env, "GET", `/guilds/${guildId}`, { bot: true });
  if (r.status !== 200) throw discordError(r);
  const g = r.data;
  const tier = Math.max(0, Math.min(3, Number(g.premium_tier || 0)));
  let stickers = g.stickers;
  if (!stickers) {
    const s = await discord(env, "GET", `/guilds/${guildId}/stickers`, { bot: true });
    stickers = s.status === 200 ? s.data : [];
  }
  const emojis = (g.emojis || []).map((e) => ({ id: e.id, name: e.name, animated: !!e.animated }));
  return {
    id: g.id, name: g.name, icon: g.icon, tier,
    limit: EMOJI_LIMITS[tier], sticker_limit: STICKER_LIMITS[tier],
    static: emojis.filter((e) => !e.animated).length,
    animated: emojis.filter((e) => e.animated).length,
    stickers: stickers.map((s) => ({ id: s.id, name: s.name, format: s.format_type })),
    emojis,
  };
}

function inviteUrl(env, guildId) {
  const q = new URLSearchParams({
    client_id: env.DISCORD_CLIENT_ID, scope: "bot applications.commands",
    permissions: BOT_PERMS.toString(), integration_type: "0",
  });
  if (guildId) {
    q.set("guild_id", guildId);
    q.set("disable_guild_select", "true");
  }
  return `https://discord.com/oauth2/authorize?${q}`;
}

// ---------------------------------------------------------------- sessions

async function newSession(env, kind, tok, user) {
  const token = randomToken(32);
  const t = now();
  await env.DB.prepare(
    `INSERT INTO sessions (id, user_id, user_name, user_avatar, access_enc, refresh_enc, expires_at, kind, created_at, last_seen)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(
    await sha256hex(token), user.id, user.global_name || user.username, user.avatar || null,
    await seal(env, tok.access_token), tok.refresh_token ? await seal(env, tok.refresh_token) : null,
    t + (Number(tok.expires_in || 604800) * 1000), kind, t, t).run();
  return token;
}

function sessionToken(req) {
  const auth = req.headers.get("authorization") || "";
  if (auth.startsWith("Bearer ")) return { token: auth.slice(7).trim(), cookie: false };
  const c = cookies(req).ed_session;
  return c ? { token: c, cookie: true } : null;
}

async function getSession(req, env) {
  const st = sessionToken(req);
  if (!st || st.token.length < 20) return null;
  const id = await sha256hex(st.token);
  const row = await env.DB.prepare("SELECT * FROM sessions WHERE id = ?").bind(id).first();
  if (!row) return null;
  if (row.last_seen < now() - SESSION_DAYS * 864e5) {
    await env.DB.prepare("DELETE FROM sessions WHERE id = ?").bind(id).run();
    return null;
  }
  if (row.last_seen < now() - 3600e3) {
    await env.DB.prepare("UPDATE sessions SET last_seen = ? WHERE id = ?").bind(now(), id).run();
  }
  row.viaCookie = st.cookie;
  return row;
}

async function requireSession(req, env) {
  const s = await getSession(req, env);
  if (!s) fail(401, "Please log in with Discord.", { code: "login" });
  // Browsers: state-changing requests must come from our own pages (no CSRF).
  if (s.viaCookie && !["GET", "HEAD"].includes(req.method)) {
    const origin = req.headers.get("origin");
    if (origin !== new URL(req.url).origin) fail(403, "Blocked: this request didn't come from the dashboard.");
  }
  return s;
}

async function userAccessToken(env, s) {
  if (s.expires_at - now() > 60e3) return unseal(env, s.access_enc);
  if (!s.refresh_enc) return expired(env, s);
  const r = await discord(env, "POST", "/oauth2/token", {
    form: {
      client_id: env.DISCORD_CLIENT_ID, client_secret: env.DISCORD_CLIENT_SECRET,
      grant_type: "refresh_token", refresh_token: await unseal(env, s.refresh_enc),
    },
  });
  if (r.status !== 200) return expired(env, s);
  await env.DB.prepare("UPDATE sessions SET access_enc = ?, refresh_enc = ?, expires_at = ? WHERE id = ?").bind(
    await seal(env, r.data.access_token), r.data.refresh_token ? await seal(env, r.data.refresh_token) : s.refresh_enc,
    now() + Number(r.data.expires_in || 604800) * 1000, s.id).run();
  return r.data.access_token;
}

async function expired(env, s) {
  await env.DB.prepare("DELETE FROM sessions WHERE id = ?").bind(s.id).run();
  fail(401, "Your Discord login expired. Please log in again.", { code: "login" });
}

// ---------------------------------------------------------------- login with Discord

function authorizeUrl(env, origin, state, switchAccount) {
  const q = new URLSearchParams({
    client_id: env.DISCORD_CLIENT_ID, response_type: "code", scope: "identify guilds",
    redirect_uri: `${origin}/auth/callback`, state,
    prompt: switchAccount ? "consent" : "none",
  });
  return `https://discord.com/oauth2/authorize?${q}`;
}

async function loginWeb(req, env) {
  const url = new URL(req.url);
  const state = randomToken(24);
  await env.DB.prepare("INSERT INTO login_states (state, kind, switch, created_at) VALUES (?, 'web', ?, ?)")
    .bind(state, url.searchParams.has("switch") ? 1 : 0, now()).run();
  return new Response(null, {
    status: 302,
    headers: {
      location: authorizeUrl(env, url.origin, state, url.searchParams.has("switch")),
      // ties the login to this browser
      "set-cookie": `ed_state=${state}; Path=/auth; Max-Age=600; HttpOnly; Secure; SameSite=Lax`,
    },
  });
}

// Desktop app: /auth/app?state=<its state>&challenge=<base64url(sha256(verifier))>
async function loginApp(req, env) {
  const url = new URL(req.url);
  const appState = url.searchParams.get("state") || "";
  const challenge = url.searchParams.get("challenge") || "";
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(appState) || !/^[A-Za-z0-9_-]{43}$/.test(challenge)) {
    return new Response("Bad login request. Please try again from the app.", { status: 400 });
  }
  const state = randomToken(24);
  await env.DB.prepare(
    "INSERT INTO login_states (state, kind, app_state, challenge, switch, created_at) VALUES (?, 'app', ?, ?, ?, ?)")
    .bind(state, appState, challenge, url.searchParams.has("switch") ? 1 : 0, now()).run();
  return Response.redirect(authorizeUrl(env, url.origin, state, url.searchParams.has("switch")), 302);
}

async function callback(req, env) {
  const url = new URL(req.url);
  const state = url.searchParams.get("state") || "";
  const row = state && await env.DB.prepare("SELECT * FROM login_states WHERE state = ?").bind(state).first();
  if (row) await env.DB.prepare("DELETE FROM login_states WHERE state = ?").bind(state).run();
  if (!row || row.created_at < now() - 600e3) {
    return page(env, req, 400, "This login link has expired. Please try again.");
  }
  if (row.kind === "web" && !timingSafeEqual(cookies(req).ed_state || "", state)) {
    return page(env, req, 400, "This login was started in another browser. Please try again.");
  }
  const back = (params) => {
    if (row.kind === "app") {
      return Response.redirect(`${APP_REDIRECT}?${new URLSearchParams({ ...params, state: row.app_state })}`, 302);
    }
    return null;
  };
  const code = url.searchParams.get("code");
  if (!code) {
    // "prompt=none" fails when Discord has to ask the user: retry once with the consent screen.
    if (url.searchParams.get("error") === "consent_required" || url.searchParams.get("error") === "interaction_required") {
      const st2 = randomToken(24);
      await env.DB.prepare(
        "INSERT INTO login_states (state, kind, app_state, challenge, switch, created_at) VALUES (?, ?, ?, ?, 1, ?)")
        .bind(st2, row.kind, row.app_state, row.challenge, now()).run();
      const headers = { location: authorizeUrl(env, url.origin, st2, true) };
      if (row.kind === "web") headers["set-cookie"] = `ed_state=${st2}; Path=/auth; Max-Age=600; HttpOnly; Secure; SameSite=Lax`;
      return new Response(null, { status: 302, headers });
    }
    return back({ error: "cancelled" }) || Response.redirect(`${url.origin}/`, 302);
  }
  const tok = await discord(env, "POST", "/oauth2/token", {
    form: {
      client_id: env.DISCORD_CLIENT_ID, client_secret: env.DISCORD_CLIENT_SECRET,
      grant_type: "authorization_code", code, redirect_uri: `${url.origin}/auth/callback`,
    },
  });
  if (tok.status !== 200) {
    return back({ error: "discord" }) || page(env, req, 502, "Discord didn't accept the login. Please try again.");
  }
  const me = await discord(env, "GET", "/users/@me", { bearer: tok.data.access_token });
  if (me.status !== 200) return back({ error: "discord" }) || page(env, req, 502, "Couldn't read your Discord account.");

  if (row.kind === "app") {
    const token = await newSession(env, "app", tok.data, me.data);
    const ticket = randomToken(24);
    await env.DB.prepare("INSERT INTO tickets (ticket, session_enc, challenge, created_at) VALUES (?, ?, ?, ?)")
      .bind(ticket, await seal(env, token), row.challenge, now()).run();
    return back({ code: ticket });
  }
  const token = await newSession(env, "web", tok.data, me.data);
  return new Response(null, {
    status: 302,
    headers: [
      ["location", `${url.origin}/dashboard`],
      ["set-cookie", `ed_session=${token}; Path=/; Max-Age=${SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`],
      ["set-cookie", "ed_state=; Path=/auth; Max-Age=0; HttpOnly; Secure; SameSite=Lax"],
    ],
  });
}

// Desktop app swaps the one-time ticket (+ its secret verifier) for its session token.
async function redeem(req, env) {
  const body = await req.json().catch(() => ({}));
  const ticket = String(body.ticket || "");
  const verifier = String(body.verifier || "");
  const row = ticket && await env.DB.prepare("SELECT * FROM tickets WHERE ticket = ?").bind(ticket).first();
  if (row) await env.DB.prepare("DELETE FROM tickets WHERE ticket = ?").bind(ticket).run();   // one use only
  if (!row || row.created_at < now() - 120e3) fail(400, "This login expired. Please log in again.");
  if (!timingSafeEqual(await sha256b64url(verifier), row.challenge)) fail(400, "This login doesn't belong to this app.");
  const token = await unseal(env, row.session_enc);
  const s = await env.DB.prepare("SELECT user_id, user_name, user_avatar FROM sessions WHERE id = ?")
    .bind(await sha256hex(token)).first();
  if (!s) fail(400, "This login expired. Please log in again.");
  return json({ session: token, user: { id: s.user_id, name: s.user_name, avatar: s.user_avatar } });
}

async function logout(req, env) {
  const s = await getSession(req, env);
  if (s) {
    try {
      const access = await unseal(env, s.access_enc);
      await discord(env, "POST", "/oauth2/token/revoke", {
        form: { client_id: env.DISCORD_CLIENT_ID, client_secret: env.DISCORD_CLIENT_SECRET, token: access },
      });
    } catch { /* already invalid */ }
    await env.DB.prepare("DELETE FROM sessions WHERE id = ?").bind(s.id).run();
    await forgetCache(`user-guilds-${s.user_id}`);
  }
  const clear = "ed_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax";
  if (req.method === "GET") {
    return new Response(null, { status: 302, headers: { location: "/", "set-cookie": clear } });
  }
  return json({ ok: true }, 200, { "set-cookie": clear });
}

// ---------------------------------------------------------------- API

async function apiGuilds(req, env, s) {
  const fresh = new URL(req.url).searchParams.has("fresh");
  if (fresh) await forgetCache(`user-guilds-${s.user_id}`);
  const mine = await cached(`user-guilds-${s.user_id}`, 20, async () => {
    const r = await discord(env, "GET", "/users/@me/guilds?limit=200", { bearer: await userAccessToken(env, s) });
    if (r.status === 401) await expired(env, s);
    if (r.status !== 200) throw discordError(r);
    return r.data.map((g) => ({ id: g.id, name: g.name, icon: g.icon, owner: !!g.owner, permissions: g.permissions }));
  });
  if (fresh) await forgetCache("bot-guilds");
  const botIn = new Set(await botGuildIds(env));
  const ready = [];
  const invite = [];
  for (const g of mine) {
    const p = g.owner ? CAN_UPLOAD | CAN_INVITE : BigInt(g.permissions || "0");
    const item = { id: g.id, name: g.name, icon: g.icon };
    if (botIn.has(g.id) && (p & CAN_UPLOAD)) ready.push(item);
    else if (!botIn.has(g.id) && (p & CAN_INVITE)) invite.push({ ...item, invite: inviteUrl(env, g.id) });
  }
  const byName = (a, b) => a.name.localeCompare(b.name);
  return json({ ready: ready.sort(byName), invite: invite.sort(byName) });
}

async function recentUploads(env, userId) {
  const r = await env.DB.prepare("SELECT COUNT(*) AS n FROM uploads WHERE user_id = ? AND created_at > ?")
    .bind(userId, now() - 3600e3).first();
  return r ? r.n : 0;
}

async function logUpload(env, row) {
  await env.DB.prepare(
    "INSERT INTO uploads (guild_id, user_id, user_name, kind, name, ok, error, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    .bind(row.guild_id, row.user_id, row.user_name || null, row.kind, row.name, row.ok ? 1 : 0, row.error || null,
      row.source, now()).run();
}

async function checkUploadLimit(env, userId) {
  const n = await recentUploads(env, userId);
  if (n < UPLOADS_PER_HOUR) return;
  // The limit is a rolling hour: uploads are allowed again once enough of the ones in the window are an
  // hour old to get back under the limit, i.e. when row number (n - limit) (oldest first) ages out.
  const row = await env.DB.prepare(
    "SELECT created_at FROM uploads WHERE user_id = ? AND created_at > ? ORDER BY created_at ASC LIMIT 1 OFFSET ?")
    .bind(userId, now() - 3600e3, n - UPLOADS_PER_HOUR).first();
  const wait = row ? Math.max(1, Math.ceil((row.created_at + 3600e3 - now()) / 1000)) : 60;
  const mins = Math.max(1, Math.ceil(wait / 60));
  fail(429, `You've added ${UPLOADS_PER_HOUR} emotes in the last hour. You can add more in about ${mins} minute${mins === 1 ? "" : "s"}.`,
    { retry_after: wait, code: "hourly_limit" });
}

function dataUri(bytes, mime) {
  return `data:${mime};base64,${b64(bytes)}`;
}

// The one place emojis are created (desktop app, dashboard, 7TV, /emote add all come here).
async function createEmoji(env, { guildId, user, name, image, source }) {
  if (!EMOJI_NAME.test(name)) fail(400, "Emoji names can only use letters, numbers and _, 2–32 characters.");
  const m = /^data:image\/(png|gif|jpeg|webp);base64,([A-Za-z0-9+/=]+)$/.exec(image || "");
  if (!m) fail(400, "The image must be a PNG, GIF, JPEG or WebP file.");
  if (Math.floor(m[2].length * 3 / 4) > MAX_EMOJI) fail(413, "Emojis must be 256 KB or smaller.");
  const r = await discord(env, "POST", `/guilds/${guildId}/emojis`, {
    bot: true, json: { name, image }, reason: `Added by ${user.name} (${user.id}) with Emotes Downloader`,
  });
  const ok = r.status === 200 || r.status === 201;
  const err = ok ? null : discordError(r);
  await logUpload(env, { guild_id: guildId, user_id: user.id, user_name: user.name, kind: "emoji", name, ok, error: err && err.message, source });
  if (!ok) throw err;
  return { id: r.data.id, name: r.data.name, animated: !!r.data.animated };
}

async function apiAddEmoji(req, env, s, guildId) {
  await authorize(env, guildId, s.user_id, CAN_UPLOAD);
  await checkUploadLimit(env, s.user_id);
  const body = await req.json().catch(() => fail(400, "Bad request."));
  const emoji = await createEmoji(env, {
    guildId, user: { id: s.user_id, name: s.user_name }, name: String(body.name || ""), image: String(body.image || ""),
    source: s.kind === "app" ? "app" : "web",
  });
  return json({ ok: true, emoji });
}

async function apiAddSticker(req, env, s, guildId) {
  await authorize(env, guildId, s.user_id, CAN_UPLOAD);
  await checkUploadLimit(env, s.user_id);
  const form = await req.formData().catch(() => fail(400, "Bad request."));
  const file = form.get("file");
  const name = String(form.get("name") || "").trim();
  if (name.length < 2 || name.length > 30) fail(400, "Sticker names must be 2–30 characters.");
  if (!file || typeof file === "string") fail(400, "Choose a sticker file.");
  if (file.size > MAX_STICKER) fail(413, "Stickers must be 512 KB or smaller.");
  if (!/^image\/(png|gif|apng)$/.test(file.type || "")) fail(400, "Stickers must be PNG, APNG or GIF files.");
  const out = new FormData();
  out.append("name", name);
  out.append("description", "");
  out.append("tags", String(form.get("tags") || "grinning").slice(0, 200));
  out.append("file", file, file.name || `${name}.${file.type.endsWith("gif") ? "gif" : "png"}`);
  const r = await discord(env, "POST", `/guilds/${guildId}/stickers`, {
    bot: true, formData: out, reason: `Added by ${s.user_name} (${s.user_id}) with Emotes Downloader`,
  });
  const ok = r.status === 200 || r.status === 201;
  const err = ok ? null : discordError(r);
  await logUpload(env, { guild_id: guildId, user_id: s.user_id, user_name: s.user_name, kind: "sticker", name, ok,
    error: err && err.message, source: s.kind === "app" ? "app" : "web" });
  if (!ok) throw err;
  return json({ ok: true, sticker: { id: r.data.id, name: r.data.name } });
}

// ---------------------------------------------------------------- 7TV

function sevenTvId(link) {
  const t = String(link || "").trim();
  const m = /(?:7tv\.app\/emotes\/|cdn\.7tv\.app\/emote\/|7tv\.io\/v3\/emotes\/)([0-9A-Za-z]{24,26})/.exec(t);
  if (m) return m[1];
  if (/^[0-9A-Za-z]{24,26}$/.test(t)) return t;
  return null;
}

// Reads a 7TV emote: its name, whether it moves, and the files 7TV has for it.
async function sevenTvLookup(env, link) {
  const id = sevenTvId(link);
  if (!id) fail(400, "Paste a 7TV emote link, like https://7tv.app/emotes/01FD667N200009ZAD9VPCVD4JX");
  const r = await fetch(`${env.SEVENTV_API || "https://7tv.io/v3"}/emotes/${id}`, { headers: { "User-Agent": "EmotesDownloader" } })
    .catch(() => fail(502, "Couldn't reach 7TV. Try again in a moment."));
  if (r.status === 404) fail(404, "7TV doesn't have this emote.");
  if (!r.ok) fail(502, "Couldn't reach 7TV. Try again in a moment.");
  const e = await r.json();
  const host = e.host || {};
  const fmt = e.animated ? "GIF" : "PNG";
  const files = (host.files || []).filter((f) => String(f.format || "").toUpperCase() === fmt
    || String(f.name || "").toLowerCase().endsWith(`.${fmt.toLowerCase()}`));
  files.sort((a, b) => (b.width * b.height) - (a.width * a.height));
  if (!files.length) fail(415, "7TV has no GIF or PNG file for this emote.");
  const base = (host.url || `//cdn.7tv.app/emote/${id}`).replace(/^\/\//, "https://");
  return { id, name: cleanEmojiName(e.name, "emote"), animated: !!e.animated, files, base };
}

async function sevenTvFile(info, file) {
  const f = await fetch(`${info.base}/${file.name}`).catch(() => fail(502, "Couldn't download the emote from 7TV."));
  if (!f.ok) fail(502, "Couldn't download the emote from 7TV.");
  return new Uint8Array(await f.arrayBuffer());
}

// Turns a 7TV emote into what gets uploaded: one emoji, or several pieces for a wide emote.
//   cut = false  -> never cut
// Returns { items: [{ name, image }], pieces, note }
async function sevenTvEmoji(env, link, { name, cut = true } = {}) {
  const info = await sevenTvLookup(env, link);
  const baseName = name ? cleanEmojiName(name) : info.name;
  const mime = info.animated ? "image/gif" : "image/png";
  const biggest = info.files[0];
  const grid = pieceGrid(biggest.width, biggest.height);
  let note = "";
  let cutError = null;

  if (cut && grid.count > 1) {
    if (env.IMAGES) {
      try {
        const src = await sevenTvFile(info, biggest);
        const pieces = await cutWithImages(env.IMAGES, src, { ...grid, animated: info.animated, limit: MAX_EMOJI });
        const names = pieceNames(baseName, pieces.length);
        return { items: pieces.map((p, i) => ({ name: names[i], image: dataUri(p.bytes, mime) })), pieces: pieces.length, animated: info.animated, note };
      } catch (e) {
        console.log("cut failed:", e && e.message);
        cutError = String((e && e.message) || e).slice(0, 200);
      }
    }
    note = "It's a wide emote, but it couldn't be cut into pieces right now, so it was added in one piece.";
  } else if (grid.count > 1) {
    note = "It's a wide emote, so Discord shows it small.";
  }

  // One piece: the biggest 7TV file that fits in 256 KB, or a smaller copy made with Images.
  const fits = info.files.find((f) => f.size && f.size <= MAX_EMOJI) || info.files.find((f) => !f.size);
  let bytes = fits ? await sevenTvFile(info, fits) : null;
  if ((!bytes || bytes.length > MAX_EMOJI) && env.IMAGES) {
    try {
      bytes = await shrinkWithImages(env.IMAGES, bytes || await sevenTvFile(info, biggest), { animated: info.animated, limit: MAX_EMOJI });
    } catch {
      bytes = null;
    }
  }
  if (!bytes || bytes.length > MAX_EMOJI) fail(413, "This emote is too big for Discord. Use the Emotes Downloader app to shrink it.");
  return { items: [{ name: baseName, image: dataUri(bytes, mime) }], pieces: 1, animated: info.animated, note, cutError };
}

// Free emoji slots of one kind (animated or not) in a server, read live with the bot.
async function freeSlots(env, guildId, animated) {
  const r = await discord(env, "GET", `/guilds/${guildId}`, { bot: true });
  if (r.status !== 200) throw discordError(r);
  const tier = Math.max(0, Math.min(3, Number(r.data.premium_tier || 0)));
  const used = (r.data.emojis || []).filter((e) => !!e.animated === animated).length;
  return Math.max(0, EMOJI_LIMITS[tier] - used);
}

// Adds every item (one emoji or all pieces). One failed piece stops the rest so a half emote isn't left
// with gaps; what was added is still returned.
async function addSevenTv(env, { guildId, user, link, name, cut, source }) {
  await checkUploadLimit(env, user.id);
  const em = await sevenTvEmoji(env, link, { name, cut });
  const free = await freeSlots(env, guildId, em.animated);
  const kind = em.animated ? "animated" : "static";
  if (free < em.items.length) {
    fail(409, em.items.length > 1
      ? `This emote is cut into ${em.items.length} pieces, but the server only has ${free} free ${kind} emoji slot${free === 1 ? "" : "s"}. `
        + "Free some slots, or add it without cutting."
      : `No free ${kind} emoji slots left in this server.`, { code: "no_slots" });
  }
  const added = [];
  let error = null;
  for (const it of em.items) {
    try {
      added.push(await createEmoji(env, { guildId, user, name: it.name, image: it.image, source }));
    } catch (e) {
      error = e;
      break;
    }
  }
  if (!added.length && error) throw error;
  return { added, pieces: em.pieces, note: em.note, error: error ? (error instanceof HttpError ? error.message : "Something went wrong.") : null };
}

async function apiAdd7tv(req, env, s, guildId) {
  await authorize(env, guildId, s.user_id, CAN_UPLOAD);
  const body = await req.json().catch(() => fail(400, "Bad request."));
  if (body.dry === true) {                 // preview: how it would be cut, nothing is added
    await checkUploadLimit(env, s.user_id);
    let em;
    try {
      em = await sevenTvEmoji(env, body.link, { name: body.name, cut: body.cut !== false });
    } finally {
      // A preview cuts with Cloudflare Images (5,000 cuts a month on the free plan), so it counts toward the
      // hourly limit like an upload. kind 'preview' rows are left out of the server's history.
      await logUpload(env, { guild_id: guildId, user_id: s.user_id, user_name: s.user_name, kind: "preview",
        name: cleanEmojiName(body.name || "preview"), ok: !!em, source: "preview" });
    }
    return json({ ok: true, dry: true, pieces: em.pieces, note: em.note, cut_error: em.cutError || null,
      items: em.items.map((it) => ({ name: it.name, kb: Math.round(it.image.length * 3 / 4 / 1024), image: it.image })) });
  }
  const res = await addSevenTv(env, {
    guildId, user: { id: s.user_id, name: s.user_name }, link: body.link, name: body.name,
    cut: body.cut !== false, source: "7tv",
  });
  return json({ ok: true, emoji: res.added[0], emojis: res.added, pieces: res.pieces, note: res.note, error: res.error });
}

// ---------------------------------------------------------------- /emote command (Discord interactions)

async function verifyInteraction(req, env, body) {
  const sig = req.headers.get("x-signature-ed25519");
  const ts = req.headers.get("x-signature-timestamp");
  if (!sig || !ts || !/^[0-9a-f]{128}$/i.test(sig)) return false;
  let key;
  try {
    key = await crypto.subtle.importKey("raw", hexToBytes(env.DISCORD_PUBLIC_KEY), { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify("Ed25519", key, hexToBytes(sig), enc.encode(ts + body));
  } catch {
    key = await crypto.subtle.importKey("raw", hexToBytes(env.DISCORD_PUBLIC_KEY),
      { name: "NODE-ED25519", namedCurve: "NODE-ED25519" }, false, ["verify"]);
    return crypto.subtle.verify("NODE-ED25519", key, hexToBytes(sig), enc.encode(ts + body));
  }
}

async function interactions(req, env, ctx) {
  const body = await req.text();
  if (!(await verifyInteraction(req, env, body))) return new Response("Bad signature", { status: 401 });
  const it = JSON.parse(body);
  if (it.type === 1) return json({ type: 1 });                       // PING
  const reply = (content, ephemeral = true) => json({ type: 4, data: { content, flags: ephemeral ? 64 : 0, allowed_mentions: { parse: [] } } });
  if (it.type !== 2 || !it.data || it.data.name !== "emote") return reply("Unknown command.");
  const sub = (it.data.options || [])[0] || {};
  const origin = new URL(req.url).origin;
  if (sub.name === "dashboard") return reply(`Manage this server's emotes here: ${origin}/dashboard`);
  if (sub.name !== "add") return reply("Unknown command.");
  if (!it.guild_id || !it.member) return reply("Use this command in a server.");
  const perms = BigInt(it.member.permissions || "0");
  if (!(perms & CAN_UPLOAD)) return reply("You need the “Manage Expressions” or “Create Expressions” permission to add emotes.");
  const opts = Object.fromEntries((sub.options || []).map((o) => [o.name, o.value]));
  const user = { id: it.member.user.id, name: it.member.user.global_name || it.member.user.username };

  const followUp = async (content) => {
    await fetch(`${env.DISCORD_API || "https://discord.com/api/v10"}/webhooks/${env.DISCORD_CLIENT_ID}/${it.token}/messages/@original`, {
      method: "PATCH", headers: { "content-type": "application/json" },
      body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
    });
  };
  ctx.waitUntil((async () => {
    try {
      const res = await addSevenTv(env, {
        guildId: it.guild_id, user, link: opts.link, name: opts.name, cut: opts.cut !== false, source: "command",
      });
      const tags = res.added.map((e) => `<${e.animated ? "a" : ""}:${e.name}:${e.id}>`).join("");
      let msg;
      if (res.pieces > 1) {
        msg = `${tags}\nAdded in ${res.pieces} pieces: ` + res.added.map((e) => `\`:${e.name}:\``).join(" ")
          + "\nType them together, with no spaces, to show the whole emote.";
      } else {
        msg = `${tags} Added **:${res.added[0].name}:**`;
      }
      if (res.note) msg += `\n${res.note}`;
      if (res.error) msg += `\n❌ Only ${res.added.length} of ${res.pieces} pieces were added: ${res.error}`;
      await followUp(msg.slice(0, 1990));
    } catch (err) {
      await followUp(`❌ ${err instanceof HttpError ? err.message : "Something went wrong. Please try again."}`);
    }
  })());
  return json({ type: 5 });                                         // "thinking…", answered by followUp
}

// ---------------------------------------------------------------- pages

const SECURITY = {
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
  "referrer-policy": "strict-origin-when-cross-origin",
};

function page(env, req, status = 200, error = "") {
  const cfg = JSON.stringify({ client_id: env.DISCORD_CLIENT_ID, error }).replace(/</g, "\\u003c");
  return new Response(PAGE.replace("__CONFIG__", cfg), {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8", "cache-control": "no-store", ...SECURITY,
      "content-security-policy": "default-src 'self'; img-src 'self' https://cdn.discordapp.com https://media.discordapp.net "
        + "https://avatars.githubusercontent.com https://cdn.7tv.app data: blob:; "
        + "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    },
  });
}

// ---------------------------------------------------------------- router

async function route(req, env, ctx) {
  const url = new URL(req.url);
  const p = url.pathname.replace(/\/+$/, "") || "/";
  const M = req.method;

  if (M === "GET" && p === "/app.css") return new Response(CSS, { headers: { "content-type": "text/css; charset=utf-8", "cache-control": "max-age=300", ...SECURITY } });
  if (M === "GET" && (p === "/icon.png" || p === "/favicon.ico")) {
    return new Response(ICON, { headers: { "content-type": "image/png", "cache-control": "max-age=86400", ...SECURITY } });
  }
  if (M === "GET" && p === "/api/stats") {
    const { servers } = await serverCount(env);
    return json({ servers }, 200, { "cache-control": "max-age=30" });
  }
  if (M === "GET" && p === "/app.js") return new Response(CLIENT_JS, { headers: { "content-type": "text/javascript; charset=utf-8", "cache-control": "max-age=300", ...SECURITY } });
  if (M === "GET" && (p === "/" || p === "/dashboard" || p.startsWith("/dashboard/") || p === "/privacy" || p === "/terms")) {
    return page(env, req);
  }
  if (M === "POST" && p === "/interactions") return interactions(req, env, ctx);
  if (M === "GET" && p === "/login") return loginWeb(req, env);
  if (M === "GET" && p === "/auth/app") return loginApp(req, env);
  if (M === "GET" && p === "/auth/callback") return callback(req, env);
  if (M === "POST" && p === "/auth/redeem") return redeem(req, env);
  if ((M === "POST" || M === "GET") && p === "/logout") return logout(req, env);

  if (p === "/api/config" && M === "GET") {
    return json({ client_id: env.DISCORD_CLIENT_ID, invite: inviteUrl(env), upload_limit_per_hour: UPLOADS_PER_HOUR });
  }
  if (!p.startsWith("/api/")) return page(env, req, 404, "This page doesn't exist.");

  const s = await requireSession(req, env);
  if (p === "/api/me" && M === "GET") {
    return json({ id: s.user_id, name: s.user_name, avatar: s.user_avatar, uploads_last_hour: await recentUploads(env, s.user_id) });
  }
  if (p === "/api/guilds" && M === "GET") return apiGuilds(req, env, s);

  let m = /^\/api\/guilds\/(\d{15,21})(\/.*)?$/.exec(p);
  if (!m) fail(404, "Not found.");
  const gid = m[1];
  const rest = m[2] || "";
  if (rest === "" && M === "GET") {
    const { perms } = await authorize(env, gid, s.user_id, CAN_UPLOAD);   // checked first, once
    const d = await guildDetails(env, gid);
    return json({ ...d, can_manage: !!(perms & CAN_MANAGE) });
  }
  if (rest === "/emojis" && M === "POST") return apiAddEmoji(req, env, s, gid);
  if (rest === "/stickers" && M === "POST") return apiAddSticker(req, env, s, gid);
  if (rest === "/7tv" && M === "POST") return apiAdd7tv(req, env, s, gid);
  if (rest === "/history" && M === "GET") {
    await authorize(env, gid, s.user_id, CAN_UPLOAD);
    const rows = await env.DB.prepare(
      "SELECT user_name, kind, name, ok, error, source, created_at FROM uploads WHERE guild_id = ? AND kind != 'preview' "
      + "ORDER BY created_at DESC LIMIT 50")
      .bind(gid).all();
    return json({ items: rows.results || [] });
  }
  m = /^\/(emojis|stickers)\/(\d{15,21})$/.exec(rest);
  if (m && (M === "PATCH" || M === "DELETE")) {
    await authorize(env, gid, s.user_id, CAN_MANAGE);
    const what = m[1];
    const reason = `${M === "DELETE" ? "Deleted" : "Renamed"} by ${s.user_name} (${s.user_id}) with Emotes Downloader`;
    let r;
    if (M === "PATCH") {
      const body = await req.json().catch(() => fail(400, "Bad request."));
      const name = String(body.name || "");
      if (what === "emojis" && !EMOJI_NAME.test(name)) fail(400, "Emoji names can only use letters, numbers and _, 2–32 characters.");
      if (what === "stickers" && (name.length < 2 || name.length > 30)) fail(400, "Sticker names must be 2–30 characters.");
      r = await discord(env, "PATCH", `/guilds/${gid}/${what}/${m[2]}`, { bot: true, json: { name }, reason });
    } else {
      r = await discord(env, "DELETE", `/guilds/${gid}/${what}/${m[2]}`, { bot: true, reason });
    }
    if (r.status !== 200 && r.status !== 204) throw discordError(r);
    return json({ ok: true });
  }
  fail(404, "Not found.");
}

export default {
  async fetch(req, env, ctx) {
    try {
      return await route(req, env, ctx);
    } catch (e) {
      if (e instanceof HttpError) return json({ error: e.message, ...e.extra }, e.status);
      console.error(e && e.stack || e);
      return json({ error: "Something went wrong on our side. Please try again." }, 500);
    }
  },

  async scheduled(event, env) {
    if (event.cron !== "17 3 * * *") {           // every 30 minutes: the bio with the server count
      await updateBio(env);
      return;
    }
    const t = now();
    await env.DB.batch([
      env.DB.prepare("DELETE FROM login_states WHERE created_at < ?").bind(t - 3600e3),
      env.DB.prepare("DELETE FROM tickets WHERE created_at < ?").bind(t - 3600e3),
      env.DB.prepare("DELETE FROM sessions WHERE last_seen < ?").bind(t - SESSION_DAYS * 864e5),
      env.DB.prepare("DELETE FROM uploads WHERE created_at < ?").bind(t - 90 * 864e5),
    ]);
  },
};

export { cleanEmojiName, sevenTvId, computePerms };

