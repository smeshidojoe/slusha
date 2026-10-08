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
import re
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
-- Память о людях: строка на человека. Раньше люди жили внутри заметок, и
-- заметки целиком уходили в каждый промпт — поэтому их держали впритык к
-- двенадцати людям. Отдельно в промпт берём только тех, кто сейчас в
-- разговоре, и помнить можно сколько угодно народу.
CREATE TABLE IF NOT EXISTS ai_people(
    chat_id    INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    who        TEXT    NOT NULL,
    text       TEXT    NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, key)
);
-- Как человек обращается с ботом: сколько грубостей и добрых слов ему
-- досталось. Счёт затухает со временем (см. mood.py). Отдельно от ai_people:
-- те записи переписывает пересборка заметок, а счёт копится с каждой реплики.
CREATE TABLE IF NOT EXISTS ai_mood(
    chat_id    INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    who        TEXT    NOT NULL,
    rude       REAL    NOT NULL DEFAULT 0,
    kind       REAL    NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, key)
);
-- Незакрытые темы: план человека со сроком — чтобы потом спросить, как
-- прошло (см. plans.py). ask_from/ask_to — окно, когда вопрос уместен.
CREATE TABLE IF NOT EXISTS ai_plans(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    who      TEXT    NOT NULL,
    text     TEXT    NOT NULL,
    ts       INTEGER NOT NULL,
    ask_from INTEGER NOT NULL,
    ask_to   INTEGER NOT NULL,
    done     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_plans ON ai_plans(chat_id, key, done);
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY,
    v TEXT
);
-- Долгая память (memory.py): события из жизни участников с датой и важностью.
-- kind: event — случившееся, insight — вывод о человеке из нескольких событий.
CREATE TABLE IF NOT EXISTS ai_events(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    who        TEXT    NOT NULL,
    text       TEXT    NOT NULL,
    importance INTEGER NOT NULL DEFAULT 5,
    ts         INTEGER NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'event',
    hits       INTEGER NOT NULL DEFAULT 0,
    used_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_events ON ai_events(chat_id, ts);
-- Эмбеддинги фраз: считаются один раз, ключ — хэш модели и текста.
CREATE TABLE IF NOT EXISTS ai_vectors(
    hash TEXT PRIMARY KEY,
    vec  BLOB NOT NULL
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
    # план: на какое сообщение ответить, когда бот спросит о нём сам
    cur = await db.execute("PRAGMA table_info(ai_plans)")
    have = {r["name"] for r in await cur.fetchall()}
    # и готовая реплика-вопрос: NULL — ещё не написана, '' — не вышло
    for name, decl in (("msg_id", "INTEGER"), ("thread_id", "INTEGER"), ("opener", "TEXT")):
        if name not in have:
            await db.execute(f"ALTER TABLE ai_plans ADD COLUMN {name} {decl}")
            logger.info("переписка: добавлена колонка ai_plans.%s", name)
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
    await db.execute("DELETE FROM ai_people WHERE chat_id = ?", (chat_id,))
    await db.execute("DELETE FROM ai_mood WHERE chat_id = ?", (chat_id,))
    await db.execute("DELETE FROM ai_plans WHERE chat_id = ?", (chat_id,))
    await db.execute("DELETE FROM ai_events WHERE chat_id = ?", (chat_id,))
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
    """Стереть заметки вместе с памятью о людях: это одна память."""
    db = await _conn()
    cur = await db.execute("DELETE FROM ai_summary WHERE chat_id = ?", (chat_id,))
    gone = await db.execute("DELETE FROM ai_people WHERE chat_id = ?", (chat_id,))
    moods = await db.execute("DELETE FROM ai_mood WHERE chat_id = ?", (chat_id,))
    plans = await db.execute("DELETE FROM ai_plans WHERE chat_id = ?", (chat_id,))
    events = await db.execute("DELETE FROM ai_events WHERE chat_id = ?", (chat_id,))
    await db.commit()
    return bool(cur.rowcount or gone.rowcount or moods.rowcount or plans.rowcount
                or events.rowcount)


# ---------- память о людях ----------

# Сколько людей помним на чат. В промпт их уходит несколько, так что потолок
# тут только от разрастания базы: вытесняются те, о ком дольше всех молчат.
PEOPLE_KEEP = 500


def person_key(who: str) -> str:
    """Ключ человека: ник без учёта регистра — модель пишет его как попало."""
    return who.strip().lower()


async def people_get(chat_id: int, whos) -> list[tuple[str, str]]:
    """Записи о названных людях, в том же порядке. О ком ничего нет — пропуск."""
    keys = []
    for who in whos:
        k = person_key(who)
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return []
    db = await _conn()
    marks = ",".join("?" * len(keys))
    cur = await db.execute(
        f"SELECT key, who, text FROM ai_people WHERE chat_id = ? AND key IN ({marks})",
        (chat_id, *keys),
    )
    found = {r["key"]: (r["who"], r["text"]) for r in await cur.fetchall()}
    return [found[k] for k in keys if k in found]


async def people_all(chat_id: int) -> list[tuple[str, str]]:
    """Все, кого помним, — свежие первыми."""
    db = await _conn()
    cur = await db.execute(
        "SELECT who, text FROM ai_people WHERE chat_id = ? ORDER BY updated_at DESC, who",
        (chat_id,),
    )
    return [(r["who"], r["text"]) for r in await cur.fetchall()]


async def people_count(chat_id: int) -> int:
    db = await _conn()
    cur = await db.execute("SELECT COUNT(*) AS c FROM ai_people WHERE chat_id = ?",
                           (chat_id,))
    return (await cur.fetchone())["c"]


async def people_set(chat_id: int, people) -> None:
    """Записать или обновить людей: [(ник, описание)]. Лишних старых — вытеснить."""
    now = int(time.time())
    db = await _conn()
    await db.executemany(
        """INSERT INTO ai_people (chat_id, key, who, text, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(chat_id, key) DO UPDATE SET
               who = excluded.who, text = excluded.text,
               updated_at = excluded.updated_at""",
        [(chat_id, person_key(who), who, text, now) for who, text in people],
    )
    await db.execute(
        """DELETE FROM ai_people WHERE chat_id = ? AND key NOT IN
               (SELECT key FROM ai_people WHERE chat_id = ?
                ORDER BY updated_at DESC LIMIT ?)""",
        (chat_id, chat_id, PEOPLE_KEEP),
    )
    await db.commit()


def _keys_of(who: str) -> set[str]:
    """Ключи, под которыми человек может лежать: с собакой и без.

    В меню ник набирают как придётся — «vasya», «@Vasya», — а в базе он такой,
    каким подписан в переписке.
    """
    k = person_key(who)
    if not k:
        return set()
    bare = k.lstrip("@")
    return {k, bare, "@" + bare} if bare else {k}


async def people_forget(chat_id: int, whos) -> list[str]:
    """Забыть названных людей целиком. Вернуть, кого нашли.

    Стираем запись о человеке, счёт его обращения с ботом и строки общих
    заметок, где он назван: иначе из «шуток и прозвищ» он вернулся бы в
    первую же пересборку.
    """
    db = await _conn()
    found = []
    text, covered = await summary_get(chat_id)
    lines = text.splitlines()
    for who in whos:
        keys = _keys_of(who)
        if not keys:
            continue
        marks = ",".join("?" * len(keys))
        hit = False
        for table in ("ai_people", "ai_mood", "ai_plans", "ai_events"):
            cur = await db.execute(
                f"DELETE FROM {table} WHERE chat_id = ? AND key IN ({marks})",
                (chat_id, *keys))
            hit = hit or bool(cur.rowcount)
        bare = person_key(who).lstrip("@")
        # Ник ищем целым словом: «@kat» не должен сносить строки про @katieboots.
        named = re.compile(rf"(?<![\w@])@?{re.escape(bare)}(?!\w)", re.IGNORECASE)
        # события других людей, где он назван, — тоже о нём
        cur = await db.execute("SELECT id, text FROM ai_events WHERE chat_id = ?", (chat_id,))
        about = [r["id"] for r in await cur.fetchall() if named.search(r["text"])]
        if about:
            await db.execute(f"DELETE FROM ai_events WHERE id IN ({','.join('?' * len(about))})",
                             about)
            hit = True
        kept = [ln for ln in lines
                if not (ln.lstrip().startswith("—") and named.search(ln))]
        if len(kept) != len(lines):
            hit, lines = True, kept
        if hit:
            found.append(who.strip())
    if "\n".join(lines) != text:
        await summary_set(chat_id, "\n".join(lines), covered)
    await db.commit()
    return found


# ---------- как люди обращаются с ботом ----------

async def mood_get(chat_id: int, who: str) -> tuple[float, float, int] | None:
    """Счёт человека: (грубость, доброта, когда обновлён). None — ни разу не считали."""
    db = await _conn()
    cur = await db.execute(
        "SELECT rude, kind, updated_at FROM ai_mood WHERE chat_id = ? AND key = ?",
        (chat_id, person_key(who)))
    row = await cur.fetchone()
    return (row["rude"], row["kind"], row["updated_at"]) if row else None


async def mood_set(chat_id: int, who: str, rude: float, kind: float) -> None:
    db = await _conn()
    await db.execute(
        """INSERT INTO ai_mood (chat_id, key, who, rude, kind, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(chat_id, key) DO UPDATE SET
               who = excluded.who, rude = excluded.rude, kind = excluded.kind,
               updated_at = excluded.updated_at""",
        (chat_id, person_key(who), who, rude, kind, int(time.time())))
    await db.commit()


async def mood_all(chat_id: int) -> dict[str, tuple[float, float, int, str]]:
    """Все счета чата: ключ -> (грубость, доброта, когда обновлён, ник)."""
    db = await _conn()
    cur = await db.execute(
        "SELECT key, who, rude, kind, updated_at FROM ai_mood WHERE chat_id = ?"
        " ORDER BY updated_at DESC", (chat_id,))
    return {r["key"]: (r["rude"], r["kind"], r["updated_at"], r["who"])
            for r in await cur.fetchall()}


# ---------- незакрытые темы ----------

async def plan_add(chat_id: int, who: str, text: str, ts: int,
                   ask_from: int, ask_to: int, keep: int,
                   msg_id: int | None = None, thread_id: int | None = None) -> None:
    """Запомнить план. У человека держим keep самых свежих, отжившие — стираем."""
    db = await _conn()
    key = person_key(who)
    await db.execute("DELETE FROM ai_plans WHERE ask_to < ?", (ts,))
    await db.execute(
        """INSERT INTO ai_plans (chat_id, key, who, text, ts, ask_from, ask_to,
                                 msg_id, thread_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (chat_id, key, who, text, ts, ask_from, ask_to, msg_id, thread_id))
    await db.execute(
        """DELETE FROM ai_plans WHERE chat_id = ? AND key = ? AND id NOT IN
               (SELECT id FROM ai_plans WHERE chat_id = ? AND key = ?
                ORDER BY id DESC LIMIT ?)""",
        (chat_id, key, chat_id, key, keep))
    await db.commit()


async def plans_due(chat_id: int, who: str, now: int) -> list[tuple[int, str, int]]:
    """Созревшие и не спрошенные планы человека: [(id, текст, когда сказано)]."""
    db = await _conn()
    cur = await db.execute(
        """SELECT id, text, ts FROM ai_plans WHERE chat_id = ? AND key = ? AND done = 0
           AND ask_from <= ? AND ask_to >= ? ORDER BY ts""",
        (chat_id, person_key(who), now, now))
    return [(r["id"], r["text"], r["ts"]) for r in await cur.fetchall()]


async def plans_ripe(chat_id: int, now: int, wait: int = 0,
                     who: str | None = None) -> list[dict]:
    """Не спрошенные планы чата с готовой репликой, созревшие wait секунд назад.

    who — только этого человека.
    """
    db = await _conn()
    sql = ("""SELECT id, who, text, ts, ask_to, msg_id, thread_id, opener FROM ai_plans
              WHERE chat_id = ? AND done = 0 AND ask_from + ? <= ? AND ask_to >= ?
              AND opener IS NOT NULL AND opener != ''""")
    args = [chat_id, wait, now, now]
    if who is not None:
        sql += " AND key = ?"
        args.append(person_key(who))
    cur = await db.execute(sql + " ORDER BY ask_to", args)
    return [dict(r) for r in await cur.fetchall()]


async def plans_unwritten(chat_id: int, until: int, now: int) -> list[dict]:
    """Планы, которые созреют к until, а реплики-вопроса к ним ещё нет."""
    db = await _conn()
    cur = await db.execute(
        """SELECT id, who, text, ts FROM ai_plans WHERE chat_id = ? AND done = 0
           AND opener IS NULL AND ask_from <= ? AND ask_to >= ? ORDER BY ask_from""",
        (chat_id, until, now))
    return [dict(r) for r in await cur.fetchall()]


async def plan_opener(plan_id: int, text: str) -> None:
    db = await _conn()
    await db.execute("UPDATE ai_plans SET opener = ? WHERE id = ?", (text, plan_id))
    await db.commit()


async def plans_open(chat_id: int) -> list[tuple[str, str, int]]:
    """Все ещё не спрошенные планы чата: [(ник, текст, с какого момента спросить)]."""
    db = await _conn()
    cur = await db.execute(
        """SELECT who, text, ask_from FROM ai_plans WHERE chat_id = ? AND done = 0
           AND ask_to >= ? ORDER BY ask_from""", (chat_id, int(time.time())))
    return [(r["who"], r["text"], r["ask_from"]) for r in await cur.fetchall()]


async def plan_done(plan_id: int) -> None:
    db = await _conn()
    await db.execute("UPDATE ai_plans SET done = 1 WHERE id = ?", (plan_id,))
    await db.commit()


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
