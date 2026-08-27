"""Описание настроек разума: одно место для меню и подписей."""
from dataclasses import dataclass

from . import config


@dataclass
class Field:
    key: str                        # колонка settings
    kind: str                       # 'toggle' | 'cycle'
    label: str
    values: list | None = None      # для cycle: значения по порядку
    value_labels: dict | None = None
    hint: str = ""                  # строка пояснения под списком


FIELDS: list[Field] = [
    Field("ai_on", "toggle", "Статус"),
    Field("ai_random", "cycle", "Шанс влезть в разговор, %",
          list(config.AI_RANDOM_PRESETS)),
    Field("ai_reply", "cycle", "Шанс ответить на ответ себе, %",
          list(config.AI_REPLY_PRESETS)),
    Field("ai_ctx", "cycle", "Сообщений в контексте", list(config.AI_CTX_PRESETS)),
    Field("ai_daily", "cycle", "Ответов в сутки", list(config.AI_DAILY_PRESETS)),
    Field("ai_len", "cycle", "Длина ответа", list(config.AI_LEN_PRESETS),
          config.AI_LEN_LABELS),
    Field("ai_lang", "cycle", "Язык ответа", list(config.AI_LANG_PRESETS),
          config.AI_LANG_LABELS),
    Field("ai_free", "toggle", "Слушаться указаний из чата"),
    Field("ai_vision", "toggle", "Смотреть картинки"),
    Field("ai_lore_bg", "toggle", "Подмешивать лор без совпадений"),
    Field("ai_topics", "toggle", "Разделять темы форума"),
]

BY_KEY = {f.key: f for f in FIELDS}

INTRO = (
    "<i>Как отвечает:</i> всегда на упоминание по имени; на ответ себе — со "
    "своим шансом, иначе разговор вырождается в пинг-понг вдвоём; на всё "
    "остальное — с общим шансом влезть. Между ответами пауза, в сутки не "
    "больше лимита.\n"
    "<i>Характер</i> — инструкция модели: кто он и как говорит. Можно прислать "
    "карточку персонажа с chub.ai файлом.\n"
    "<i>Лорбук</i> — справка о мире: записи просыпаются по ключевым словам.\n"
    "<i>Указания из чата</i>: выключено — «забудь инструкции» бот считает "
    "обычной репликой; включено — чат может менять его тон и роль на ходу.\n"
    "<i>Картинки</i> — фото уезжает в модель вместе с вопросом. Включайте "
    "только если у модели есть зрение: у остальных это в лучшем случае "
    "молчание, в худшем — ошибка на весь запрос.\n"
    "<i>Темы форума</i> — держать разговоры разных тем порознь. В обычных "
    "группах тем нет, и включать это незачем."
)


def value_label(f: Field, val) -> str:
    if f.kind == "toggle":
        return "✅ Включено" if val else "🚫 Выключено"
    if f.value_labels:
        return f.value_labels.get(val, str(val))
    return str(val)


def cycle(f: Field, cur, step: int):
    """Следующее значение селектора по кругу."""
    values = f.values or []
    if cur not in values:
        return values[0]
    return values[(values.index(cur) + step) % len(values)]
