-- Logins. The session token itself is never stored, only its SHA-256.
-- Discord tokens are encrypted (AES-GCM, key derived from the client secret).
CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,          -- sha256(session token), hex
  user_id     TEXT NOT NULL,
  user_name   TEXT,
  user_avatar TEXT,
  access_enc  TEXT NOT NULL,
  refresh_enc TEXT,
  expires_at  INTEGER NOT NULL,          -- when the Discord access token expires (ms)
  kind        TEXT NOT NULL,             -- 'web' or 'app'
  created_at  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

-- One row per "Log in with Discord" that hasn't come back yet (10 minutes).
CREATE TABLE IF NOT EXISTS login_states (
  state      TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,              -- 'web' or 'app'
  app_state  TEXT,                       -- the desktop app's own state (sent back to it)
  challenge  TEXT,                       -- sha256(verifier) from the desktop app
  switch     INTEGER DEFAULT 0,
  created_at INTEGER NOT NULL
);

-- One-time tickets the desktop app swaps for its session (60 seconds).
CREATE TABLE IF NOT EXISTS tickets (
  ticket      TEXT PRIMARY KEY,
  session_enc TEXT NOT NULL,
  challenge   TEXT NOT NULL,
  created_at  INTEGER NOT NULL
);

-- Who added what, where (shown on the dashboard; also used for the per-user upload limit).
CREATE TABLE IF NOT EXISTS uploads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id   TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  user_name  TEXT,
  kind       TEXT NOT NULL,              -- 'emoji' or 'sticker'
  name       TEXT NOT NULL,
  ok         INTEGER NOT NULL,
  error      TEXT,
  source     TEXT NOT NULL,              -- 'app', 'web', '7tv', 'command'
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_guild ON uploads(guild_id, created_at);
CREATE INDEX IF NOT EXISTS uploads_user ON uploads(user_id, created_at);
