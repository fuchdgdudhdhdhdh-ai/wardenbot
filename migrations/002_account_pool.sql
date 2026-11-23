-- Реальный пул Telegram-аккаунтов. Все DDL операции идемпотентны.
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT,
  api_id INTEGER NOT NULL,
  api_hash TEXT NOT NULL,
  session_enc BLOB NOT NULL,
  phone TEXT,
  tg_user_id INTEGER,
  tg_username TEXT,
  status TEXT NOT NULL DEFAULT 'free',
  leased_by INTEGER,
  lease_token TEXT,
  leased_at TEXT,
  lease_until TEXT,
  added_by INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  client_type TEXT NOT NULL DEFAULT 'telethon'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_tg ON accounts(tg_user_id) WHERE tg_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_accounts_status ON accounts(status, lease_until);
CREATE TABLE IF NOT EXISTS account_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  user_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
