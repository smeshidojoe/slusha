"""Русские ключи для лорбука — иначе он молчит.

Зачем это нужно. Записи книги просыпаются, когда их ключевое слово встретилось
в разговоре. Книги с chub.ai приходят с английскими ключами («cow», «beer»,
«sword»), а чат русский — совпадений не бывает никогда. Ровно поэтому в боте и
появился «фоновый» кусок книги по кругу: без него лор молчал бы всегда. Но фон
бьёт по площадям и небольшую модель сбивает, так что правильное решение —
дописать записям русские ключи, а фон выключить.

Английские ключи не удаляются: они ничего не стоят и продолжают работать, если
кто-то пишет по-английски.

Содержимое записей не трогаем. Перевод текста стоит дороже, чем кажется:
кириллица у большинства токенизаторов идёт вдвое дороже латиницы, а модель
английскую справку и так понимает — рамка промпта прямо говорит, что это
материал, а не язык ответа.

    python slusha/scripts/lore_ru.py                  # показать, ничего не менять
    python slusha/scripts/lore_ru.py --apply          # записать
    python slusha/scripts/lore_ru.py --chat -100123   # только один чат
    python slusha/scripts/lore_ru.py --junk           # отчёт про мусор в книге
    python slusha/scripts/lore_ru.py --drop-junk --apply

Запускать из папки НАД пакетом. Модель берётся из .env; если Ollama слушает не
там, где написано в AI_BASE_URL (например, .env настроен на Docker), адрес
можно переопределить: SLUSHA_LORE_BASE=http://127.0.0.1:11434
"""
import argparse
import asyncio
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

_override = os.getenv("SLUSHA_LORE_BASE")
if _override:
    os.environ["AI_BASE_URL"] = _override

from slusha import ai, config          # noqa: E402

CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
# Ключ длиннее этого — уже не ключ, а фраза: срабатывать будет никогда.
KEY_MAX = 32
# Сколько русских ключей дописываем к записи. Больше — только шум: лишние
# слова срабатывают на посторонних разговорах и тянут в промпт чужую справку.
KEYS_PER_ENTRY = 8

# Записи, которые лором не являются вовсе: это ролевой пресет, приехавший
# вместе с книгой. Он спорит с рамкой промпта за то, как боту себя вести,
# и ключи у него — английские местоимения, которые цепляются за любое
# латинское слово в чате.
JUNK_MARKERS = (
    "role-play", "roleplay", "{{user}}", "{{char}}", "system note",
    "you are the narrator", "ethical protocols", "stay in character",
    "usual ethical", "nsfw",
    # Пресет прячется и без слова roleplay: «повествование», «персонажи
    # должны», «выход из образа» — это указания о том, как себя вести, а не
    # справка о мире. Одна такая запись стояла с флагом «всегда» и уезжала
    # в каждый промпт мимо ключей.
    "narration", "narrations", "characters should", "your characters",
    "out of character", "the character's manner", "immersion",
)

_SYSTEM = (
    "Ты переводчик ключевых слов для поисковой книги. Тебе дают список "
    "английских слов и кусочек текста, к которому они относятся.\n"
    "Верни русские слова, по которым эту тему стали бы искать в русском чате: "
    "через запятую, одной строкой, без пояснений и без нумерации.\n"
    "Бери начальную форму и короткий корень, а не описательную фразу: поиск "
    "идёт по началу слова, поэтому короткий корень поймает и падежи.\n"
    "Переводи только то, о чём запись. Слов из этой инструкции в ответе быть "
    "не должно.\n"
    f"Не больше {KEYS_PER_ENTRY} слов. Только русские слова."
)

# Слова, которые ключами быть не могут. Местоимения и общие слова срабатывают
# на каждом сообщении подряд и тащат в промпт справку, к разговору отношения
# не имеющую. Небольшая модель охотно выдаёт именно их: переводит местоимения
# из ключей ролевого пресета буквально и подхватывает слова из самой
# инструкции — «пиво» из примера уезжало в ключи к цепному мечу.
STOP = {
    "они", "оно", "она", "себя", "сам", "сама", "мой", "моя", "моё", "твой",
    "твоя", "его", "её", "их", "нас", "вас", "нам", "вам", "это", "этот",
    "эта", "тот", "там", "тут", "кто", "что", "как", "где", "все", "весь",
    "вся", "наш", "ваш", "тебя", "меня", "себе", "него", "неё", "них",
    "слова", "слово", "текст", "ответ", "запись", "ключи", "пример",
}


# Потолок на строку ключей. Раньше тут стояло 300 знаков «как при импорте», и
# это молча резало УЖЕ ЛЕЖАЩИЕ английские ключи: у записей готовых книг их
# бывает под три сотни знаков и без нас. Старое не трогаем никогда — если не
# влезает, отказываемся от лишних новых.
KEYS_TOTAL_MAX = 1000


# Окончания, которые модель охотно оставляет: она выдаёт «коровы» и «некроны»,
# а в чате пишут «корову» и «некронов» — ключ не срабатывает. Дописываем корень
# рядом с полной формой. Планка в пять букв не случайна: от «вода» корень «вод»
# поймал бы и «водителя», и «водку», а от «коровы» корень «коров» — только то,
# что нужно.
ENDINGS = ("ами", "ями", "ов", "ев", "ей", "ы", "и", "а", "я", "ю", "у", "е")
STEM_MIN = 5


def stems(keys: list[str]) -> list[str]:
    """Корни к ключам: «коровы» -> «коров», чтобы ловились падежи."""
    out = list(keys)
    for key in keys:
        if " " in key:
            continue                   # у словосочетаний корень резать незачем
        for end in ENDINGS:
            if key.endswith(end) and len(key) - len(end) >= STEM_MIN:
                stem = key[:-len(end)]
                if stem not in out:
                    out.append(stem)
                break
    return out


def fit(old: str, fresh: list[str]) -> str:
    """Дописать новые ключи к старым, ничего из старых не потеряв."""
    base = old.strip().rstrip(",")
    while fresh:
        merged = f"{base}, " + ", ".join(fresh) if base else ", ".join(fresh)
        if len(merged) <= KEYS_TOTAL_MAX:
            return merged
        fresh.pop()                    # места нет — жертвуем новым, не старым
    return base


def clean(raw: str, existing: set[str]) -> list[str]:
    """Разобрать ответ модели: только русские слова, без повторов и мусора."""
    out: list[str] = []
    for piece in re.split(r"[,\n;]", raw or ""):
        key = piece.strip().strip("-–—•*.\"'«»").lower()
        if not key or len(key) > KEY_MAX or len(key) < 3:
            continue
        if not CYRILLIC.search(key) or re.search(r"[a-z]", key):
            continue                       # латиница и полулатинские гибриды
        if key in STOP or key in existing or key in out:
            continue
        out.append(key)
    return out[:KEYS_PER_ENTRY]


async def translate(keys: str, content: str) -> list[str]:
    question = (
        f"Английские ключи: {keys[:400]}\n\n"
        f"Текст записи: {content[:400]}\n\n"
        "Русские ключи:"
    )
    try:
        answer = await ai.raw(_SYSTEM, question, 200)
    except Exception as e:
        print("  ! модель не ответила:", e)
        return []
    have = {k.strip().lower() for k in keys.split(",")}
    return clean(ai.strip_thoughts(answer or ""), have)


def is_junk(content: str) -> bool:
    low = (content or "").lower()
    return any(marker in low for marker in JUNK_MARKERS)


def db_path() -> str:
    """Где лежит база. В образе это /app/data, а при запуске с хоста — data/
    внутри самого пакета: BASE_DIR у config считает от родительской папки."""
    if os.path.exists(config.DB_PATH):
        return config.DB_PATH
    local = os.path.join(config.PKG_DIR, "data", "slusha.sqlite3")
    if os.path.exists(local):
        return local
    raise SystemExit(f"База не найдена: ни {config.DB_PATH}, ни {local}. "
                     f"Укажите путь через --db")


def rows(con, chat: int | None):
    q = "SELECT id, chat_id, keys, content FROM lore"
    args: tuple = ()
    if chat:
        q += " WHERE chat_id = ?"
        args = (chat,)
    return list(con.execute(q + " ORDER BY id", args))


async def main() -> int:
    ap = argparse.ArgumentParser(description="Дописать лорбуку русские ключи")
    ap.add_argument("--apply", action="store_true", help="записать в базу")
    ap.add_argument("--chat", type=int, help="только этот чат")
    ap.add_argument("--limit", type=int, help="обработать не больше N записей")
    ap.add_argument("--junk", action="store_true", help="показать мусор и выйти")
    ap.add_argument("--drop-junk", action="store_true",
                    help="удалить записи ролевого пресета")
    ap.add_argument("--db", help="путь к базе, если он не как в .env")
    args = ap.parse_args()

    con = sqlite3.connect(args.db or db_path())
    con.row_factory = sqlite3.Row
    todo = rows(con, args.chat)
    junk = [r for r in todo if is_junk(r["content"])]

    if args.junk or args.drop_junk:
        print(f"Записей, похожих на ролевой пресет, а не на лор: {len(junk)}")
        for r in junk:
            print(f"  #{r['id']} [{(r['keys'] or '')[:60]}…]")
            print(f"      {r['content'][:100].strip()}…")
        if args.drop_junk and junk:
            if args.apply:
                con.executemany("DELETE FROM lore WHERE id = ?",
                                [(r["id"],) for r in junk])
                con.commit()
                print(f"Удалено записей: {len(junk)}")
            else:
                print("Пробный прогон. Чтобы удалить — добавьте --apply")
        con.close()
        return 0

    todo = [r for r in todo if not CYRILLIC.search(r["keys"] or "")]
    todo = [r for r in todo if (r["keys"] or "").strip()]
    # Мусор не переводим никогда. У ролевого пресета ключи — английские
    # местоимения, и по-русски они превращаются в «они, себя, мой, это»:
    # такие сработают на каждом сообщении и потащат пресет в каждый промпт.
    # Это хуже, чем нынешнее положение дел.
    skipped = [r for r in todo if is_junk(r["content"])]
    todo = [r for r in todo if not is_junk(r["content"])]
    if args.limit:
        todo = todo[:args.limit]
    print(f"Модель: {ai.provider_label()}")
    print(f"Записей без русских ключей: {len(todo)}"
          + (f", из них мусора: {len(junk)}" if junk else ""))
    if skipped:
        print(f"Пропускаю как мусор: {len(skipped)} — их ключи и по-русски "
              f"будут местоимениями. Выкинуть: --drop-junk --apply")
    if not todo:
        con.close()
        return 0

    done = 0
    for r in todo:
        fresh = await translate(r["keys"], r["content"])
        if not fresh:
            print(f"  #{r['id']}: пусто, пропускаю")
            continue
        fresh = stems(fresh)
        merged = fit(r["keys"] or "", fresh)
        print(f"  #{r['id']}: + {', '.join(fresh)}")
        if args.apply:
            con.execute("UPDATE lore SET keys = ? WHERE id = ?",
                        (merged, r["id"]))
            con.commit()
        done += 1

    print(f"\nГотово: {done} записей." if args.apply
          else f"\nПробный прогон: {done} записей. Чтобы записать — --apply")
    con.close()
    return 0


sys.exit(asyncio.run(main()))
