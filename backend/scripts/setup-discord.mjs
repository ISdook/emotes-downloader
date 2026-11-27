// Runs after every deploy (GitHub Actions). Uses DISCORD_BOT_TOKEN from the environment; prints no secrets.
//  * registers the /emote command (add, dashboard)
//  * points Discord's "Interactions Endpoint URL" at the Worker (Discord checks it right away)
//  * keeps the bot's name and picture the same as the app ("Emotes Downloader", assets/icon.png)
//  * sets the bio (GitHub link, developer, server count). The Worker refreshes the count every 30 minutes.
// The picture is only uploaded when the name changes or FORCE_PICTURE=1 (Discord limits picture changes).
import { readFileSync } from "node:fs";
import { BOT_NAME, botBio } from "../src/profile.js";

const API = "https://discord.com/api/v10";
const TOKEN = process.env.DISCORD_BOT_TOKEN;
const PUBLIC_URL = (process.env.PUBLIC_URL || "").replace(/\/$/, "");
const NAME = BOT_NAME;
const ICON = readFileSync(new URL("../src/web/icon.png", import.meta.url));     // same picture as the app
const ICON_URI = `data:image/png;base64,${ICON.toString("base64")}`;
const FORCE_PICTURE = process.env.FORCE_PICTURE === "1" || process.env.FORCE_PICTURE === "true";

const toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");
const CLIENT_ID = /DISCORD_CLIENT_ID\s*=\s*"(\d+)"/.exec(toml)?.[1];
if (!CLIENT_ID) throw new Error("DISCORD_CLIENT_ID isn't set in wrangler.toml");

async function call(method, path, body) {
  for (let i = 0; i < 5; i++) {
    const r = await fetch(API + path, {
      method,
      headers: { Authorization: `Bot ${TOKEN}`, "Content-Type": "application/json", "User-Agent": "DiscordBot (emotes-downloader, 1)" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await r.text();
    const data = text ? JSON.parse(text) : null;
    if (r.status === 429) {
      await new Promise((res) => setTimeout(res, (data?.retry_after || 2) * 1000 + 300));
      continue;
    }
    if (!r.ok) throw new Error(`${method} ${path} → ${r.status} ${JSON.stringify(data).slice(0, 300)}`);
    return data;
  }
  throw new Error(`${method} ${path}: rate-limited too long`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 1. /emote command (guild only; the Worker checks Manage/Create Expressions for every use)
await call("PUT", `/applications/${CLIENT_ID}/commands`, [{
  name: "emote",
  description: "Add 7TV emotes to this server",
  contexts: [0],
  integration_types: [0],
  options: [
    {
      type: 1, name: "add", description: "Add a 7TV emote to this server",
      options: [
        { type: 3, name: "link", description: "7TV emote link, like https://7tv.app/emotes/…", required: true },
        { type: 3, name: "name", description: "Emoji name (optional: letters, numbers and _)", required: false, max_length: 32 },
        { type: 5, name: "cut", description: "Cut wide emotes into pieces like :name1: :name2: (default: yes)", required: false },
      ],
    },
    { type: 1, name: "dashboard", description: "Open the Emotes Downloader dashboard" },
  ],
}]);
console.log("✓ /emote command registered");

// 2. Interactions endpoint. A new workers.dev address can take a few minutes to start answering.
const app = await call("GET", "/applications/@me");
let servers = 0;
for (let after = "", i = 0; i < 50; i++) {                 // count the bot's servers (approximate_guild_count lags)
  const page = await call("GET", `/users/@me/guilds?limit=200${after ? `&after=${after}` : ""}`);
  servers += page.length;
  if (page.length < 200) break;
  after = page[page.length - 1].id;
}
const DESCRIPTION = botBio(PUBLIC_URL, servers);
const want = `${PUBLIC_URL}/interactions`;
const patch = {};
if (app.interactions_endpoint_url !== want) patch.interactions_endpoint_url = want;
if (app.description !== DESCRIPTION) patch.description = DESCRIPTION;
if (Object.keys(patch).length) {
  let done = false;
  for (let i = 0; i < 12 && !done; i++) {
    try {
      await call("PATCH", "/applications/@me", patch);
      done = true;
    } catch (e) {
      console.log(`… waiting for ${PUBLIC_URL} to answer Discord (${e.message.slice(0, 120)})`);
      await sleep(15000);
    }
  }
  if (!done) throw new Error("Discord couldn't reach the interactions URL. Run the workflow again in a few minutes.");
}
console.log("✓ interactions URL and description set");

// 3. Bot name and picture, the same as the app (Discord limits renames, so only when they differ)
const me = await call("GET", "/users/@me");
const renamed = me.username !== NAME;
const userPatch = {};
if (renamed) userPatch.username = NAME;
if (renamed || FORCE_PICTURE || !me.avatar) userPatch.avatar = ICON_URI;
if (Object.keys(userPatch).length) {
  try {
    await call("PATCH", "/users/@me", userPatch);
    console.log(`✓ bot ${Object.keys(userPatch).join(" and ")} updated`);
  } catch (e) {
    console.log(`! bot name / picture not changed: ${e.message.slice(0, 200)}`);
    if (userPatch.username && userPatch.avatar) {            // try the picture on its own
      try { await call("PATCH", "/users/@me", { avatar: ICON_URI }); console.log("✓ bot picture updated"); } catch { /* keep going */ }
    }
  }
}
if (renamed || FORCE_PICTURE || !app.icon) {
  try {
    await call("PATCH", "/applications/@me", { icon: ICON_URI });
    console.log("✓ app icon set");
  } catch (e) {
    console.log(`! app icon not set: ${e.message.slice(0, 200)}`);
  }
}
console.log(`Done. Dashboard: ${PUBLIC_URL}/dashboard`);
