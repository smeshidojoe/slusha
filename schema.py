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
    Field("ai_random", "cycle", "Влезать в разговор",
          list(config.AI_RANDOM_PRESETS), config.AI_RANDOM_LABELS),
    Field("ai_reply", "cycle", "Отвечать на ответ себе",
          list(config.AI_REPLY_PRESETS), config.AI_REPLY_LABELS),
    Field("ai_ctx", "cycle", "Сообщений в контексте", list(config.AI_CTX_PRESETS)),
    Field("ai_daily", "cycle", "Ответов в сутки", list(config.AI_DAILY_PRESETS)),
    Field("ai_len", "cycle", "Длина ответа", list(config.AI_LEN_PRESETS),
          config.AI_LEN_LABELS),
    Field("ai_lang", "cycle", "Язык ответа", list(config.AI_LANG_PRESETS),
          config.AI_LANG_LABELS),
    Field("ai_free", "toggle", "Слушаться указаний из чата"),
    Field("ai_vision", "toggle", "Смотреть картинки"),
    Field("ai_mood", "toggle", "Тон по человеку"),
    Field("ai_plans", "toggle", "Спрашивать про планы"),
    Field("ai_search", "toggle", "Искать в интернете"),
    Field("ai_journal", "toggle", "Дневник событий"),
]

BY_KEY = {f.key: f for f in FIELDS}

INTRO = (
    "<i>Как отвечает:</i> всегда на упоминание по имени. На ответ себе и на "
    "прочие сообщения решает модель — по смыслу переписки. «Влезать в "
    "разговор» — как часто он вообще задумывается: не раньше, чем пройдёт "
    "столько чужих сообщений после его реплики. Между ответами пауза, в сутки "
    "не больше лимита.\n"
    "<i>Характер</i> — инструкция модели: кто он и как говорит. Можно прислать "
    "карточку персонажа с chub.ai файлом.\n"
    "<i>Указания из чата</i>: выключено — «забудь инструкции» бот считает "
    "обычной репликой; включено — чат может менять его тон и роль на ходу.\n"
    "<i>Картинки</i> — фото уезжает в модель вместе с вопросом. Включайте "
    "только если у модели есть зрение: у остальных это в лучшем случае "
    "молчание, в худшем — ошибка на весь запрос.\n"
    "<i>Тон по человеку</i> — бот помнит, кто ему грубит: с грубияном держится "
    "холодно, с остальными по-дружески, даже если чат только что ругался.\n"
    "<i>Спрашивать про планы</i> — «завтра экзамен», «в пятницу собес»: через "
    "день-другой бот спросит этого человека, как прошло. Один раз.\n"
    "<i>Искать в интернете</i> — «покажи капибару», «загугли, что такое "
    "тардиград»: бот найдёт картинку или справку и ответит с ней. Работает, "
    "если у бота настроен поисковик.\n"
    "<i>Дневник событий</i> — из переписки бот выписывает, что случилось у "
    "людей («заболел», «купила велосипед»), и вспоминает это, когда разговор "
    "заходит о похожем. Ночью дневник прибирается: повторы склеиваются, "
    "мелочи забываются."
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
