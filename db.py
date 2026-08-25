"""База бота-собеседника: чаты, настройки разума, лорбук, доступ.

Своя SQLite, не общая с модератором: боты живут в разных процессах, а две
записи в один файл — это блокировки и «database is locked» на ровном месте.
"""
import logging
import os
import time
from dataclasses import dataclass, fields

import aiosqlite

from . import config

logger = logging.getLogger("slusha.db")

_db: aiosqlite.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    username  TEXT,
    owner_id  INTEGER,
    active    INTEGER NOT NULL DEFAULT 1,
    added_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
    chat_id     INTEGER PRIMARY KEY,
    ai_on       INTEGER NOT NULL DEFAULT 0,
    ai_persona  TEXT,
    ai_random   INTEGER NOT NULL DEFAULT 3,
    ai_ctx      INTEGER NOT NULL DEFAULT 50,
    ai_daily    INTEGER NOT NULL DEFAULT 100,
    ai_names    TEXT,
    ai_free     INTEGER NOT NULL DEFAULT 0,
    ai_len      INTEGER NOT NULL DEFAULT 1,
    ai_lang     INTEGER NOT NULL DEFAULT 1,
    ai_vision   INTEGER NOT NULL DEFAULT 0,
    ai_topics   INTEGER NOT NULL DEFAULT 0,
    ai_greeting TEXT
);
CREATE TABLE IF NOT EXISTS lore(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    keys     TEXT,
    content  TEXT NOT NULL,
    always   INTEGER NOT NULL DEFAULT 0,
    prio     INTEGER NOT NULL DEFAULT 100,
    enabled  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_lore_chat ON lore(chat_id);
CREATE TABLE IF NOT EXISTS access(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER,
    username TEXT,
    added    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users(
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    seen       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


@dataclass
class Settings:
    chat_id: int
    ai_on: int = 0
    ai_persona: str | None = None
    ai_random: int = 3
    ai_ctx: int = 50
    ai_daily: int = 100
    ai_names: str | None = None
    ai_free: int = 0
    ai_len: int = 1
    ai_lang: int = 1
    ai_vision: int = 0
    ai_topics: int = 0
    ai_greeting: str | None = None


_FIELDS = {f.name for f in fields(Settings)} - {"chat_id"}


async def init() -> None:
    global _db
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA synchronous=NORMAL")
    await _db.executescript(_SCHEMA)
    await _db.commit()
    await _migrate()


async def columns(table: str) -> set[str]:
    """Какие колонки есть в таблице сейчас."""
    cur = await _db.execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in await cur.fetchall()}


async def _add_column(table: str, name: str, decl: str) -> bool:
    """Дописать колонку, если её ещё нет.

    CREATE TABLE IF NOT EXISTS в уже существующую таблицу ничего не добавляет:
    таблица есть — и скрипт молча проходит мимо. Новые поля появляются только
    так, и только после проверки PRAGMA: повторный ALTER — это ошибка.
    """
    if name in await columns(table):
        return False
    await _db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    logger.info("база: добавлена колонка %s.%s", table, name)
    return True


async def _migrate() -> None:
    """Разовые правки старых баз. Флаг в kv, чтобы не повторялись при каждом старте."""
    # Новые настройки чата. Проверка по PRAGMA, а не по флагу: так миграция
    # безразлична к тому, с какой версии база приехала и что уже накатывали.
    for name, decl in (("ai_lang", "INTEGER NOT NULL DEFAULT 1"),
                       ("ai_vision", "INTEGER NOT NULL DEFAULT 0"),
                       ("ai_topics", "INTEGER NOT NULL DEFAULT 0"),
                       ("ai_greeting", "TEXT")):
        await _add_column("settings", name, decl)
    await _db.commit()

    # окно контекста подняли с 20 до 50: двадцати реплик в живом чате мало.
    # Трогаем только тех, у кого стоит ровно старый дефолт — если человек
    # выбрал 20 сам, его выбор не наше дело, но отличить одно от другого
    # нельзя, поэтому миграция одноразовая и больше не повторится.
    if await kv_get("mig_ctx50") is None:
        await _db.execute("UPDATE settings SET ai_ctx = 50 WHERE ai_ctx = 20")
        await kv_set("mig_ctx50", "1")
        await _db.commit()


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _now() -> int:
    return int(time.time())


# ---------- чаты ----------

async def upsert_chat(chat_id: int, title: str | None, username: str | None,
                      owner_id: int | None) -> None:
    await _db.execute(
        """INSERT INTO chats (chat_id, title, username, owner_id, active, added_at)
           VALUES (?, ?, ?, ?, 1, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
               title = excluded.title, username = excluded.username, active = 1,
               owner_id = COALESCE(chats.owner_id, excluded.owner_id)""",
        (chat_id, title, username, owner_id, _now()),
    )
    await _db.execute("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,))
    await _db.commit()


async def update_chat_title(chat_id: int, title: str | None, username: str | None) -> None:
    await _db.execute(
        "UPDATE chats SET title = ?, username = ? WHERE chat_id = ?",
        (title, username, chat_id),
    )
    await _db.commit()


async def set_chat_active(chat_id: int, active: bool) -> None:
    await _db.execute("UPDATE chats SET active = ? WHERE chat_id = ?",
                      (1 if active else 0, chat_id))
    await _db.commit()


async def get_chat(chat_id: int) -> aiosqlite.Row | None:
    cur = await _db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    return await cur.fetchone()


async def chats_for(user_id: int) -> list[aiosqlite.Row]:
    """Чаты, которыми человек вправе управлять: владелец бота видит все."""
    cur = await _db.execute("SELECT * FROM chats WHERE active = 1 ORDER BY added_at")
    chats = await cur.fetchall()
    if user_id in config.ADMIN_IDS:
        return chats
    return [c for c in chats if c["owner_id"] == user_id]


async def owns_chat(user_id: int, chat_id: int) -> bool:
    if user_id in config.ADMIN_IDS:
        return True
    ch = await get_chat(chat_id)
    return ch is not None and ch["owner_id"] == user_id


# ---------- настройки ----------

async def get_settings(chat_id: int) -> Settings:
    cur = await _db.execute("SELECT * FROM settings WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    if row is None:
        await _db.execute("INSERT OR IGNORE INTO settings (chat_id) VALUES (?)", (chat_id,))
        await _db.commit()
        return Settings(chat_id=chat_id)
    known = {k: row[k] for k in row.keys() if k in _FIELDS}
    return Settings(chat_id=chat_id, **known)


async def set_setting(chat_id: int, field: str, value) -> None:
    if field not in _FIELDS:
        raise ValueError(f"unknown settings field: {field}")
    await _db.execute(f"UPDATE settings SET {field} = ? WHERE chat_id = ?", (value, chat_id))
    await _db.commit()


# ---------- лорбук ----------

async def lore_add(chat_id: int, keys: str, content: str, always: int = 0,
                   prio: int = 100, enabled: int = 1) -> int:
    cur = await _db.execute(
        """INSERT INTO lore (chat_id, keys, content, always, prio, enabled)
           VALUES (?,?,?,?,?,?)""",
        (chat_id, keys, content, always, prio, enabled),
    )
    await _db.commit()
    return cur.lastrowid


async def lore_list(chat_id: int, only_enabled: bool = False) -> list[aiosqlite.Row]:
    q = "SELECT * FROM lore WHERE chat_id = ?"
    if only_enabled:
        q += " AND enabled = 1"
    cur = await _db.execute(q + " ORDER BY prio, id", (chat_id,))
    return await cur.fetchall()


async def lore_remove(row_id: int) -> None:
    await _db.execute("DELETE FROM lore WHERE id = ?", (row_id,))
    await _db.commit()


async def lore_clear(chat_id: int) -> int:
    cur = await _db.execute("DELETE FROM lore WHERE chat_id = ?", (chat_id,))
    await _db.commit()
    return cur.rowcount or 0


async def lore_count(chat_id: int) -> int:
    cur = await _db.execute("SELECT COUNT(*) AS c FROM lore WHERE chat_id = ?", (chat_id,))
    return (await cur.fetchone())["c"]


# ---------- люди и доступ ----------

async def track_user(user_id: int, username: str | None, first_name: str | None) -> None:
    await _db.execute(
        """INSERT INTO users (user_id, username, first_name, seen) VALUES (?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               username = excluded.username, first_name = excluded.first_name,
               seen = excluded.seen""",
        (user_id, username, first_name, _now()),
    )
    await _db.commit()


async def user_label(user_id: int | None, username: str | None = None) -> str:
    if not user_id and not username:
        return "не задан"
    if user_id:
        cur = await _db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            parts = [row["first_name"] or "", f"@{row['username']}" if row["username"] else ""]
            return " ".join(p for p in parts if p) or str(user_id)
    return f"@{username}" if username else str(user_id)


async def access_add(user_id: int | None, username: str | None) -> None:
    await _db.execute(
        "INSERT INTO access (user_id, username, added) VALUES (?, ?, ?)",
        (user_id, (username or None) and username.lower().lstrip("@"), _now()),
    )
    await _db.commit()


async def access_remove(row_id: int) -> None:
    await _db.execute("DELETE FROM access WHERE id = ?", (row_id,))
    await _db.commit()


async def access_list() -> list[aiosqlite.Row]:
    cur = await _db.execute("SELECT * FROM access ORDER BY id")
    return await cur.fetchall()


async def access_allowed(user_id: int, username: str | None) -> bool:
    cur = await _db.execute(
        "SELECT 1 FROM access WHERE user_id = ? OR (username IS NOT NULL AND username = ?)",
        (user_id, (username or "").lower()),
    )
    return await cur.fetchone() is not None


# ---------- kv (счётчики ответов за сутки) ----------

async def kv_get(key: str) -> str | None:
    cur = await _db.execute("SELECT v FROM kv WHERE k = ?", (key,))
    row = await cur.fetchone()
    return row["v"] if row else None


async def kv_set(key: str, value: str | None) -> None:
    if value is None:
        await _db.execute("DELETE FROM kv WHERE k = ?", (key,))
    else:
        await _db.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (key, value),
        )
    await _db.commit()
