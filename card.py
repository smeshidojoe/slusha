"""Карточки персонажей: импорт с chub.ai в характер чата.

Формат общий для chub.ai, SillyTavern и спецификации character card v2: JSON с
описанием героя, примерами реплик и приветствием. Карточку с chub часто отдают
картинкой PNG: JSON там спрятан в текстовом блоке файла, поэтому разбираем и
такое. Книгу лора, если она внутри, не берём — лорбук из ботов убран.
"""
import base64
import json
import logging
import re
import struct

from . import config, db

logger = logging.getLogger("slusha.card")

# потолок характера из карточки: описания с chub бывают на несколько тысяч
# знаков, и рубить их на полутора тысячах значит терять половину персонажа
PERSONA_LIMIT = int(config.AI_PERSONA_LIMIT)
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
# контекста делится с историей чата. Приводим к виду «Заголовок: то,
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


# Примеры диалога в карточках лежат одним куском, где обмены разделены
# маркером <START>, а говорящий подписан плейсхолдером: «{{char}}: …» —
# это герой, «{{user}}: …» — собеседник. Плейсхолдеры к этому моменту уже
# подставлены _fill(), поэтому ловим и то, и другое написание.
_EX_SPLIT = re.compile(r"<\s*START\s*>", re.IGNORECASE)


def parse_examples(raw: str, name: str) -> str:
    """mes_example карточки — в строки «ты: …» и «собеседник: …».

    Самое ценное, что есть в карточке для маленькой модели, и до сих пор мы
    это поле просто выбрасывали: показанная манера речи задаёт стиль сильнее,
    чем любое её описание словами.
    """
    if not raw:
        return ""
    who_self = re.escape(name) if name else "char"
    out = []
    for chunk in _EX_SPLIT.split(clean_markup(raw)):
        for row in chunk.split("\n"):
            row = row.strip()
            speaker, sep, text = row.partition(":")
            if not sep or not text.strip():
                continue
            speaker = speaker.strip().strip("*_ ")
            # «ты» — потому что _fill() подставляет его вместо {{char}},
            # когда имя героя в карточке не указано
            if re.fullmatch(rf"{who_self}|ты|\{{\{{char\}}\}}|char|bot", speaker,
                            re.IGNORECASE):
                out.append(f"ты: {text.strip()}")
            elif re.fullmatch(r"собеседник|\{\{user\}\}|user|you|human", speaker,
                              re.IGNORECASE):
                out.append(f"собеседник: {text.strip()}")
            # чужие подписи пропускаем: в карточках бывают ремарки и заголовки
    return "\n".join(out)


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
    examples = parse_examples(_fill(d.get("mes_example") or "", name), name)
    return {
        "name": name,
        "persona": _trim(persona, PERSONA_LIMIT),
        "greeting": _trim(greeting, 1000),
        "examples": examples[:config.AI_EXAMPLE_LIMIT],
    }


async def apply_card(chat_id: int, card: dict) -> list[str]:
    """Записать характер и имя из карточки в настройки чата. Что сделали — списком.

    Одно место на бота и на панель: иначе «имя добавилось» в одном интерфейсе и
    не добавилось в другом.
    """
    done = []
    if card.get("persona"):
        await db.set_setting(chat_id, "ai_persona", card["persona"][:PERSONA_LIMIT])
        done.append("характер")
    if card.get("examples"):
        await db.set_setting(chat_id, "ai_examples",
                             card["examples"][:config.AI_EXAMPLE_LIMIT])
        done.append("примеры реплик")
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


def load(raw: bytes) -> dict:
    """Разобрать файл карточки. Вернуть данные персонажа или ошибку."""
    data = read_file(raw)
    if data is None:
        return {"error": "Не похоже на JSON или карточку PNG."}
    return {"card": parse_card(data)}
