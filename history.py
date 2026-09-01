"""Переписка чатов: своя база, память поверх неё.

Отдельный файл, а не общая база с настройками, по двум причинам. Пишется он
на каждое сообщение — это самый горячий поток записи, и держать его рядом с
настройками, которые правятся раз в день, незачем. И выкинуть разговоры,
не трогая настройки, так можно одним удалением файла.

Память остаётся кешем: чтение промпта не должно ходить в базу на каждое
сообщение. Пишем сразу в оба места, читаем из памяти, а при первом обращении
к чату подтягиваем хвост переписки с диска — иначе после перезапуска бот
начинал разговор с чистого листа.

Кроме самих реплик здесь же лежат заметки о чате (ai_summary): выжимка всего,
что уже уехало за пределы окна контекста. Это тоже переписка, поэтому «забыть
переписку» стирает и её.
"""
import asyncio
import logging
import os
import time
from typing import NamedTuple

import aiosqlite

from . import config

logger = logging.getLogger("slusha.history")

_db: aiosqlite.Connection | None = None
# замок на открытие: см. _conn()
_opening = asyncio.Lock()

# сколько реплик чата держим на диске; в памяти — не больше того же
KEEP = 200
# чистим не на каждой вставке: лишние DELETE на горячем пути ни к чему
PRUNE_EVERY = 20
_since_prune: dict[int, int] = {}


class Line(NamedTuple):
    """Реплика чата со всем, что о ней известно.

    Кортеж, а не словарь, ради дешёвого распаковывания в промпте; поля со
    значениями по умолчанию — чтобы старые строки без msg_id читались как
    прежде.
    """
    who: str
    text: str
    msg_id: int | None = None
    reply_to: int | None = None
    thread_id: int | None = None
    reactions: str = ""
    # когда сказано: по разрывам во времени в промпте ставится метка, иначе
    # вчерашний разговор выглядит продолжением сегодняшнего
    ts: int = 0


# Базовая схема. Тут только то, что было с самого начала: всё, что появилось
# позже, добавляется через ALTER TABLE в _migrate(). Индекс по новой колонке
# в этот скрипт класть нельзя — он выполняется до миграции, и на старой базе
# падает «no such column».
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_history(
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    who     TEXT    NOT NULL,
    text    TEXT    NOT NULL,
    ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_history_chat ON ai_history(chat_id, id);
CREATE TABLE IF NOT EXISTS ai_summary(
    chat_id    INTEGER PRIMARY KEY,
    text       TEXT    NOT NULL,
    covered_id INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# Что дописываем к ai_history в старых базах. Только колонки: индексы по ним
# создаются отдельно и строго после ALTER TABLE.
_ADDED = (
    # id сообщения в Telegram: без него ветку реплаев не собрать
    ("msg_id", "INTEGER"),
    ("reply_to_id", "INTEGER"),
    # тема форума, если чат с темами
    ("thread_id", "INTEGER"),
    # реакции строкой вида «👍×2, 🔥» — в промпт уходит как есть
    ("reactions", "TEXT"),
)

_AFTER_MIGRATE = """
CREATE INDEX IF NOT EXISTS idx_ai_history_msg ON ai_history(chat_id, msg_id);
CREATE INDEX IF NOT EXISTS idx_ai_history_thread ON ai_history(chat_id, thread_id, id);
"""


async def _migrate(db: aiosqlite.Connection) -> None:
    """Дописать недостающие колонки и только потом — индексы по ним.

    CREATE TABLE IF NOT EXISTS существующую таблицу не трогает, поэтому новые
    поля появляются исключительно через ALTER TABLE. Проверяем PRAGMA, а не
    флаг в meta: так миграция безразлична к тому, с какой версии приехала база.
    """
    cur = await db.execute("PRAGMA table_info(ai_history)")
    have = {r["name"] for r in await cur.fetchall()}
    for name, decl in _ADDED:
        if name in have:
            continue
        await db.execute(f"ALTER TABLE ai_history ADD COLUMN {name} {decl}")
        logger.info("переписка: добавлена колонка %s", name)
    await db.executescript(_AFTER_MIGRATE)
    await db.commit()


async def _conn() -> aiosqlite.Connection:
    """Соединение с базой переписки. Открывается при первом обращении.

    Открытие под замком. Первое обращение приходит сразу из нескольких задач:
    реакции в чате прилетают пачкой, и каждая лезет в базу своим хендлером.
    Без замка все они видели `_db is None`, открывали по соединению и
    выполняли PRAGMA поверх чужой начатой транзакции. SQLite отвечал на это
    «Safety level may not be changed inside a transaction», реакция терялась,
    а в лог сыпались трейсбеки.
    """
    global _db
    if _db is not None:
        return _db
    async with _opening:
        if _db is not None:          # пока ждали замок, соединение уже открыли
            return _db
        os.makedirs(os.path.dirname(config.HISTORY_DB) or ".", exist_ok=True)
        conn = await aiosqlite.connect(config.HISTORY_DB)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        await _migrate(conn)
        # выставляем в самом конце: пока идёт разметка схемы, чужим задачам
        # это соединение отдавать нельзя
        _db = conn
    return _db


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def add(chat_id: int, who: str, text: str, msg_id: int | None = None,
              reply_to: int | None = None, thread_id: int | None = None) -> int:
    """Записать реплику, вернуть её id. Изредка подчищаем хвост, чтобы база не пухла."""
    db = await _conn()
    cur = await db.execute(
        """INSERT INTO ai_history (chat_id, who, text, ts, msg_id, reply_to_id, thread_id)
           VALUES (?,?,?,?,?,?,?)""",
        (chat_id, who, text, int(time.time()), msg_id, reply_to, thread_id),
    )
    await db.commit()
    row_id = cur.lastrowid

    n = _since_prune.get(chat_id, 0) + 1
    if n < PRUNE_EVERY:
        _since_prune[chat_id] = n
        return row_id
    _since_prune[chat_id] = 0
    await db.execute(
        """DELETE FROM ai_history WHERE chat_id = ? AND id NOT IN
               (SELECT id FROM ai_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?)""",
        (chat_id, chat_id, KEEP),
    )
    await db.commit()
    return row_id


def _line(r) -> Line:
    return Line(r["who"], r["text"], r["msg_id"], r["reply_to_id"],
                r["thread_id"], r["reactions"] or "", r["ts"] or 0)


async def tail(chat_id: int, limit: int) -> list[Line]:
    """Последние реплики чата в порядке разговора."""
    db = await _conn()
    cur = await db.execute(
        "SELECT * FROM ai_history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, max(1, limit)),
    )
    rows = await cur.fetchall()
    return [_line(r) for r in reversed(rows)]


async def clear(chat_id: int) -> int:
    """Забыть переписку чата насовсем. Вернуть, сколько реплик стёрли.

    Заметки — та же переписка, только пересказанная, и оставлять их после
    «забудь всё» было бы прямым обманом.
    """
    db = await _conn()
    cur = await db.execute("DELETE FROM ai_history WHERE chat_id = ?", (chat_id,))
    await db.execute("DELETE FROM ai_summary WHERE chat_id = ?", (chat_id,))
    await db.commit()
    _since_prune.pop(chat_id, None)
    return cur.rowcount or 0


# ---------- реакции ----------

async def set_reactions(chat_id: int, msg_id: int, text: str) -> bool:
    """Проставить строку реакций сообщению. False — такой реплики у нас нет."""
    db = await _conn()
    cur = await db.execute(
        "UPDATE ai_history SET reactions = ? WHERE chat_id = ? AND msg_id = ?",
        (text or None, chat_id, msg_id),
    )
    await db.commit()
    return bool(cur.rowcount)


async def reactions_of(chat_id: int, msg_id: int) -> str:
    """Что уже накопилось на сообщении. Пусто — реакций нет или реплика ушла."""
    db = await _conn()
    cur = await db.execute(
        "SELECT reactions FROM ai_history WHERE chat_id = ? AND msg_id = ?",
        (chat_id, msg_id),
    )
    row = await cur.fetchone()
    return (row["reactions"] or "") if row else ""


# ---------- заметки о чате ----------

async def summary_get(chat_id: int) -> tuple[str, int]:
    """Заметки и id последней сжатой реплики. Пусто — ещё ничего не сжимали."""
    db = await _conn()
    cur = await db.execute("SELECT * FROM ai_summary WHERE chat_id = ?", (chat_id,))
    row = await cur.fetchone()
    return (row["text"], row["covered_id"]) if row else ("", 0)


async def summary_updated(chat_id: int) -> int:
    """Когда заметки пересобирали в последний раз. 0 — ни разу."""
    db = await _conn()
    cur = await db.execute("SELECT updated_at FROM ai_summary WHERE chat_id = ?",
                           (chat_id,))
    row = await cur.fetchone()
    return row["updated_at"] if row else 0


async def summary_set(chat_id: int, text: str, covered_id: int) -> None:
    db = await _conn()
    await db.execute(
        """INSERT INTO ai_summary (chat_id, text, covered_id, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET
               text = excluded.text, covered_id = excluded.covered_id,
               updated_at = excluded.updated_at""",
        (chat_id, text, covered_id, int(time.time())),
    )
    await db.commit()


async def summary_clear(chat_id: int) -> bool:
    db = await _conn()
    cur = await db.execute("DELETE FROM ai_summary WHERE chat_id = ?", (chat_id,))
    await db.commit()
    return bool(cur.rowcount)


async def pending(chat_id: int, covered_id: int) -> int:
    """Сколько реплик накопилось после последнего сжатия."""
    db = await _conn()
    cur = await db.execute(
        "SELECT COUNT(*) AS c FROM ai_history WHERE chat_id = ? AND id > ?",
        (chat_id, covered_id),
    )
    return (await cur.fetchone())["c"]


async def since(chat_id: int, covered_id: int, limit: int) -> list[tuple[int, Line]]:
    """Реплики после последнего сжатия — вместе с их id, чтобы знать, докуда дошли."""
    db = await _conn()
    cur = await db.execute(
        "SELECT * FROM ai_history WHERE chat_id = ? AND id > ? ORDER BY id LIMIT ?",
        (chat_id, covered_id, max(1, limit)),
    )
    return [(r["id"], _line(r)) for r in await cur.fetchall()]
