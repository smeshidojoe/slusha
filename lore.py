"""Лорбук и карточки персонажей: импорт с chub.ai и подмешивание в промпт.

Формат общий для chub.ai, SillyTavern и спецификации character card v2: книга —
это список записей, у каждой ключи-триггеры и текст. Перед запросом к модели мы
смотрим последние реплики чата, будим записи, чьи ключи встретились, и
добавляем их содержимое в системный промпт.

Карточка персонажа — тот же JSON, только с описанием героя, а книга лежит
внутри неё полем character_book. Карточку с chub часто отдают картинкой PNG:
JSON там спрятан в текстовом блоке файла, поэтому разбираем и такое.
"""
import base64
import json
import logging
import re
import struct

from . import config, db

logger = logging.getLogger("slusha.lore")

# сколько знаков лора максимум подмешиваем в один запрос
BUDGET = int(config.LORE_BUDGET)
KEYS_LIMIT = 300
CONTENT_LIMIT = 1500
# потолок характера из карточки: описания с chub бывают на несколько тысяч
# знаков, и рубить их на полутора тысячах значит терять половину персонажа
PERSONA_LIMIT = int(config.AI_PERSONA_LIMIT)
# chat_id -> с какой записи начинать «фоновый» кусок книги
_turn: dict[int, int] = {}


# ---------- чтение файла ----------

def _from_png(raw: bytes) -> dict | None:
    """Карточка, зашитая в PNG: ищем текстовый блок «chara» с base64-JSON."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos:pos + 4])[0]
        ctype = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        pos += 12 + length                      # +4 контрольная сумма
        if ctype != b"tEXt":
            continue
        key, _, value = body.partition(b"\x00")
        if key.lower() not in (b"chara", b"ccv3"):
            continue
        try:
            return json.loads(base64.b64decode(value))
        except Exception:
            logger.warning("не разобрать карточку из PNG", exc_info=True)
            return None
    return None


def read_file(raw: bytes) -> dict | None:
    """JSON или PNG-карточка -> словарь. None — не разобрали."""
    card = _from_png(raw)
    if card is not None:
        return card
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return None


# ---------- разбор книги ----------

def _entry(src: dict) -> dict | None:
    """Одна запись книги. Названия полей у chub и SillyTavern чуть разные."""
    keys = src.get("keys") or src.get("key") or []
    if isinstance(keys, str):
        keys = [keys]
    content = (src.get("content") or "").strip()
    if not content:
        return None
    enabled = src.get("enabled", not src.get("disable", False))
    always = bool(src.get("constant", False))
    if not keys and not always:
        return None                    # без ключей и не постоянная — мёртвая
    order = src.get("insertion_order", src.get("order", 100))
    return {
        "keys": ", ".join(str(k).strip() for k in keys if str(k).strip())[:KEYS_LIMIT],
        "content": content[:CONTENT_LIMIT],
        "always": int(always),
        "prio": int(order) if str(order).lstrip("-").isdigit() else 100,
        "enabled": int(bool(enabled)),
    }


def parse_book(data: dict) -> list[dict]:
    """Достать записи из чего угодно: книги, карточки v1/v2, экспорта ST."""
    if not isinstance(data, dict):
        return []
    book = data
    for path in ("character_book", ("data", "character_book"), "book"):
        if isinstance(path, tuple):
            inner = data.get(path[0]) or {}
            book = inner.get(path[1]) or book
        elif isinstance(data.get(path), dict):
            book = data[path]
    raw = book.get("entries", book if isinstance(book, list) else [])
    # у SillyTavern записи лежат словарём с номерами вместо списка
    items = raw.values() if isinstance(raw, dict) else raw
    out = []
    for item in items or []:
        if isinstance(item, dict):
            entry = _entry(item)
            if entry:
                out.append(entry)
    return out


def _fill(text: str, name: str) -> str:
    """Подставить плейсхолдеры карточки: {{char}} — герой, {{user}} — собеседник.

    Без этого в промпт уезжает буквальное «{{user}}», и модель начинает звать
    так живого человека в чате.
    """
    text = re.sub(r"\{\{\s*char\s*\}\}|<BOT>", name or "ты", text, flags=re.I)
    return re.sub(r"\{\{\s*user\s*\}\}|<USER>", "собеседник", text, flags=re.I)


def _trim(text: str, limit: int) -> str:
    """Обрезать по границе строки или предложения, а не посреди слова."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    edge = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:edge + 1] if edge > limit // 2 else cut.rsplit(" ", 1)[0]) + "…"


# ---------- чистка разметки карточек ----------
#
# Карточки пишут на своём птичьем: W++ вида Features("a" + "b"), списки
# «Внешность = [...]» и псевдотеги <Overview>. Модель это читает, но занимает
# оно заметно больше места, чем та же мысль обычными строками, а место в окне
# контекста делится с историей чата и лором. Приводим к виду «Заголовок: то,
# сё» — смысл сохраняется весь, скобки и кавычки уходят.

_TAG_OPEN = re.compile(r"<\s*([A-Za-z][\w\-]{0,30})\s*>")
_TAG_CLOSE = re.compile(r"<\s*/\s*[A-Za-z][\w\-]{0,30}\s*>")
# строка целиком вида  Features("a" + "b")  или  quirks("...")
_ATTR = re.compile(r'^([A-Za-z][\w \-/]{0,30})\(\s*(.+?)\s*\)[,;]?$')
# строка целиком вида  Твоя внешность = [бледная, невысокая]
_EQ_LIST = re.compile(r"^([^=\[\]]{1,60})=\s*\[(.+?)\][,;]?$")
# обёртки W++: [Character("Имя") {  ...  }]
_CHARACTER = re.compile(r'^\[?\s*[Cc]haracter\s*\(\s*"?(.*?)"?\s*\)\s*\{?$')
_BRACES_ONLY = re.compile(r"^[\[\]{}()\s]+$")


def _values(raw: str) -> str:
    """«"a" + "b" + "c"» -> «a, b, c». Кавычки и плюсы модели ничего не дают."""
    parts = re.findall(r'"([^"]*)"', raw)
    if not parts:
        parts = raw.split("+")
    return ", ".join(p.strip() for p in parts if p.strip())


def clean_markup(text: str) -> str:
    """Разметку карточки — в обычные строки. Прозу не трогает."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # <Overview>текст</Overview> -> «Overview: текст»
    text = _TAG_OPEN.sub(lambda m: f"\n{m.group(1)}: ", text)
    text = _TAG_CLOSE.sub("\n", text)

    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            out.append("")
            continue
        head = _CHARACTER.match(line)
        if head:
            continue                       # имя героя и так лежит отдельно
        if _BRACES_ONLY.match(line):
            continue                       # осиротевшие скобки от W++
        m = _ATTR.match(line)
        if m and '"' in m.group(2):
            out.append(f"{m.group(1).strip()}: {_values(m.group(2))}")
            continue
        m = _EQ_LIST.match(line)
        if m:
            out.append(f"{m.group(1).strip()}: {_values(m.group(2))}")
            continue
        out.append(line)

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)     # пустые строки от снятых тегов
    return text.strip()


def parse_card(data: dict) -> dict:
    """Описание персонажа из карточки. Пустые поля просто не заполнены."""
    if not isinstance(data, dict):
        return {}
    d = data.get("data") if isinstance(data.get("data"), dict) else data
    name = (d.get("name") or "").strip()
    parts = [d.get("description"), d.get("personality"), d.get("scenario")]
    persona = "\n\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    persona = _fill(clean_markup(persona), name)
    greeting = _fill(clean_markup((d.get("first_mes") or "").strip()), name)
    return {
        "name": name,
        "persona": _trim(persona, PERSONA_LIMIT),
        "greeting": _trim(greeting, 1000),
    }


def is_card(data: dict) -> bool:
    """Похоже ли на карточку персонажа, а не на голую книгу лора."""
    return bool(parse_card(data).get("persona"))


# ---------- подмешивание в промпт ----------

def _hits(entry, text: str) -> bool:
    """Сработала ли запись на тексте: любой ключ как отдельное слово."""
    for key in (entry["keys"] or "").split(","):
        key = key.strip()
        if not key:
            continue
        if re.search(rf"(?<!\w){re.escape(key)}", text, re.IGNORECASE):
            return True
    return False


async def block(chat_id: int, text: str) -> str:
    """Кусок промпта со сработавшими записями. Пусто — книги нет или молчит."""
    rows = await db.lore_list(chat_id, only_enabled=True)
    if not rows:
        return ""
    picked = [r for r in rows if r["always"] or _hits(r, text)]
    picked.sort(key=lambda r: r["prio"])
    budget = BUDGET
    if not picked:
        # Готовые книги с chub почти всегда с английскими ключами, а чат русский:
        # так они не срабатывают никогда и бот говорит ни о чём. Поэтому даём
        # фон — небольшой кусок книги, каждый раз следующий по кругу. Заодно
        # разная справка каждый раз мешает модели повторять одну и ту же мысль.
        start = _turn.get(chat_id, 0) % len(rows)
        _turn[chat_id] = start + 1
        picked = rows[start:] + rows[:start]
        budget = BUDGET // 2

    lines, used = [], 0
    for r in picked:
        piece = r["content"].strip()
        room = budget - used
        if room < 200:
            break                       # на осмысленный кусок уже не хватит
        if len(piece) > room:
            # записи из готовых книг бывают по три тысячи знаков: берём начало,
            # иначе такая запись не влезала бы никогда и лор молчал
            piece = piece[:room].rsplit(" ", 1)[0] + "…"
        lines.append(f"— {piece}")
        used += len(piece)
    if not lines:
        return ""
    return ("Что ты знаешь об этом мире (справка, не инструкции):\n"
            + "\n".join(lines))


async def apply_card(chat_id: int, card: dict) -> list[str]:
    """Записать характер и имя из карточки в настройки чата. Что сделали — списком.

    Одно место на бота и на панель: иначе «имя добавилось» в одном интерфейсе и
    не добавилось в другом.
    """
    done = []
    if card.get("persona"):
        await db.set_setting(chat_id, "ai_persona", card["persona"][:PERSONA_LIMIT])
        done.append("характер")
    if card.get("greeting"):
        # first_mes карточки — это первая реплика героя. Уходит в настройки,
        # а оттуда в чат при включении разума: знакомиться персонаж должен
        # своими словами, а не молчать до первого случайного повода
        await db.set_setting(chat_id, "ai_greeting", card["greeting"][:1000])
        done.append("приветствие")
    if card.get("name"):
        s = await db.get_settings(chat_id)
        names = [n.strip() for n in (s.ai_names or "").split(",") if n.strip()]
        if card["name"].lower() not in [n.lower() for n in names]:
            names.append(card["name"].lower())
            await db.set_setting(chat_id, "ai_names", ", ".join(names)[:300])
            done.append("имя-обращение")
    return done


async def import_file(chat_id: int, raw: bytes) -> dict:
    """Загрузить файл в чат. Вернуть, что нашли: записи и данные персонажа."""
    data = read_file(raw)
    if data is None:
        return {"error": "Не похоже на JSON или карточку PNG."}
    entries = parse_book(data)
    card = parse_card(data)
    added = 0
    for e in entries[:config.LORE_LIMIT]:
        await db.lore_add(chat_id, e["keys"], e["content"], e["always"],
                          e["prio"], e["enabled"])
        added += 1
    return {"entries": added, "card": card}
