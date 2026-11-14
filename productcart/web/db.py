"""SQLite сайту: користувачі, сесії, профілі, підключення «Сільпо», кеш акцій."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  pw_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
-- Профіль як JSON (models.Profile). health_consent: згода на обробку даних про дієти й алергії.
CREATE TABLE IF NOT EXISTS profiles (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  data TEXT NOT NULL,
  health_consent INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
-- Токени «Сільпо» кожного користувача, зашифровані (Fernet, ключ з APP_SECRET).
CREATE TABLE IF NOT EXISTS silpo_links (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  tokens_enc TEXT NOT NULL,
  expires_at TEXT,
  store_id TEXT,
  store_title TEXT,
  connected_at TEXT NOT NULL
);
-- Незавершені входи в «Сільпо»: state → користувач і PKCE verifier.
CREATE TABLE IF NOT EXISTS oauth_pending (
  state TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  verifier TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- Реєстрація нашого застосунку в «Сільпо» (client_id) та інші налаштування.
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Акції магазину: однакові для всіх покупців цього магазину, кешуємо на кілька годин.
CREATE TABLE IF NOT EXISTS offers_cache (
  chain TEXT NOT NULL, store_id TEXT NOT NULL, fetched_at TEXT NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (chain, store_id)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def get_setting(conn, key: str) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
