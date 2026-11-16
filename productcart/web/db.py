"""База сайту: SQLite локально й у тестах, Postgres на хостингу (Vercel + Neon).

Вибір: якщо задано DATABASE_URL (postgres://…), працюємо з Postgres, інакше — з SQLite за DB_PATH.
Запити пишемо один раз з плейсхолдерами `?`; для Postgres вони перетворюються на `%s`.
`INSERT … ON CONFLICT … DO UPDATE` і `RETURNING` однаково працюють в обох базах.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

TABLES = """
CREATE TABLE IF NOT EXISTS users (
  id {pk},
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
-- Telegram: чат, який людина сама прив'язала через /start, і що вже надіслали (щоб не повторюватись).
CREATE TABLE IF NOT EXISTS tg_links (
  user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  chat_id TEXT NOT NULL,
  linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_sent (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);
"""


import contextvars
import time as _time

# Заміри для Server-Timing: скільки запитів до бази і скільки часу вони зайняли в поточному HTTP-запиті
db_stats: contextvars.ContextVar[list | None] = contextvars.ContextVar("db_stats", default=None)


class Database:
    """Тонка обгортка: execute(sql, params) → курсор з fetchone()/fetchall(); рядки доступні як r["поле"]."""

    def __init__(self, url_or_path: str):
        self.target = url_or_path
        self.is_pg = url_or_path.startswith(("postgres://", "postgresql://"))
        self._conn = None
        self._open()
        self.ensure_schema("db", TABLES.format(pk="SERIAL PRIMARY KEY" if self.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"))

    def ensure_schema(self, name: str, script: str) -> None:
        """Створює таблиці лише раз на версію схеми. Холодний старт на Vercel тоді робить 1 запит замість ~20."""
        import hashlib
        version = hashlib.sha1(script.encode()).hexdigest()[:12]
        key = f"schema:{name}"
        try:
            r = self.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if r and r["value"] == version:
                return
        except Exception:
            pass   # таблиці settings ще немає — створимо нижче
        self.executescript(script)
        self.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, version))

    def _open(self):
        if self.is_pg:
            import psycopg
            from psycopg.rows import dict_row

            # prepare_threshold=None: без серверних prepared statements — сумісно з пулером з'єднань (Neon/PgBouncer)
            self._conn = psycopg.connect(self.target, autocommit=True, row_factory=dict_row, connect_timeout=10,
                                         prepare_threshold=None)
        else:
            if self.target != ":memory:":
                Path(self.target).parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.target, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            # WAL ненадійний на мережевих дисках (напр. Azure App Service /home) — там SQLITE_JOURNAL=DELETE.
            mode = os.environ.get("SQLITE_JOURNAL", "WAL").upper()
            c.execute(f"PRAGMA journal_mode = {mode if mode in ('WAL', 'DELETE', 'TRUNCATE') else 'WAL'}")
            c.execute("PRAGMA busy_timeout = 10000")
            self._conn = c

    def executescript(self, script: str) -> None:
        if not self.is_pg:
            self._conn.executescript(script)
            return
        body = "\n".join(line for line in script.splitlines() if not line.strip().startswith("--"))
        for stmt in body.split(";"):
            if stmt.strip():
                self.execute(stmt.strip())

    def execute(self, sql: str, params: tuple | list = ()):
        st = db_stats.get()
        if st is None:
            return self._execute(sql, params)
        t = _time.perf_counter()
        try:
            return self._execute(sql, params)
        finally:
            st[0] += 1
            st[1] += _time.perf_counter() - t

    def _execute(self, sql: str, params: tuple | list = ()):
        if not self.is_pg:
            return self._conn.execute(sql, params)
        import psycopg

        q = sql.replace("?", "%s")
        try:
            return self._conn.execute(q, params)
        except psycopg.OperationalError:
            # serverless: з'єднання могло закритися між викликами — відкриваємо знову один раз
            self._open()
            return self._conn.execute(q, params)


def connect(target: str | Path | None = None) -> Database:
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if target is None and url:
        return Database(url)
    return Database(str(target or os.environ.get("DB_PATH", "data/web.db")))


def get_setting(conn: Database, key: str) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(conn: Database, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
