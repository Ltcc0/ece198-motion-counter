CREATE TABLE accounts (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  salt TEXT NOT NULL,
  verifier TEXT NOT NULL
);
CREATE TABLE logins (
  digest TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL
);
CREATE INDEX logins_expiry ON logins(expires_at);
CREATE TABLE sessions (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  capture_key TEXT NOT NULL,
  device_id TEXT NOT NULL,
  id INTEGER NOT NULL,
  training_mode TEXT NOT NULL CHECK(training_mode IN ('free','count','timed')),
  target INTEGER,
  count INTEGER NOT NULL,
  seconds INTEGER NOT NULL,
  started_at REAL NOT NULL,
  saved_at REAL NOT NULL,
  training_day TEXT NOT NULL,
  UNIQUE(user_id, capture_key)
);
CREATE INDEX sessions_user_day ON sessions(user_id, training_day);
