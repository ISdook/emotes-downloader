// The bot's public profile. Used by the Worker (bio with the live server count, every 30 minutes)
// and by scripts/setup-discord.mjs (name, picture and bio right after a deploy).

export const BOT_NAME = "Emotes Downloader";
export const DEVELOPER = "ISdook";
export const GITHUB = "https://github.com/ISdook/emotes-downloader";

export function serversText(n) {
  n = Math.max(0, Number(n) || 0);
  return `${n.toLocaleString("en-US")} server${n === 1 ? "" : "s"}`;
}

// Discord's limit for the bio ("description") is 400 characters.
export function botBio(publicUrl, servers) {
  const url = String(publicUrl || "").replace(/\/$/, "");
  const lines = [
    "Add 7TV emotes to your Discord server.",
    "• /emote add with a 7TV link (wide emotes are cut into pieces)",
    `• Dashboard: ${url}/dashboard`,
    `• Windows app: ${GITHUB}`,
    "",
    `Made by ${DEVELOPER} · In ${serversText(servers)}`,
  ];
  return lines.join("\n").slice(0, 400);
}
