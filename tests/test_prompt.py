"""Промпт Слюши: снимок истории, без дублей, контекст реплая, точная цель."""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-ctx-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, config, db      # noqa: E402

CID = -100777
FAILS = []
SENT = []          # что ушло бы в модель


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class FakeBot:
    async def me(self):
        return SimpleNamespace(id=1000, username="slusha_bot", full_name="Слюша")

    async def send_chat_action(self, *a, **kw):
        return True

    async def send_message(self, chat_id, text, **kw):
        return SimpleNamespace(message_id=1)


def msg(uid, uname, text, reply=None):
    return SimpleNamespace(
        chat=SimpleNamespace(id=CID, title="Овощехранилище", type="supergroup"),
        from_user=SimpleNamespace(id=uid, username=uname, full_name=uname,
                                  is_bot=False),
        text=text, caption=None, message_id=uid, reply_to_message=reply)


async def fake_ollama(system, messages, tokens=0, images=None):
    # переписка теперь едет ходами диалога; для проверок склеиваем обратно
    SENT.append((system, ai.flatten(messages), tokens, images))
    return "ответ бота"


async def main():
    await db.init()
    await db.upsert_chat(CID, "Овощехранилище", None, 424211817)
    await db.set_setting(CID, "ai_on", 1)
    await db.set_setting(CID, "ai_random", 100)      # отвечаем на всё
    s = await db.get_settings(CID)
    bot = FakeBot()
    ai._ask_ollama = fake_ollama                     # модель не дёргаем

    # переписка
    await ai.remember(CID, "@vasya", "кто пойдёт за пивом")
    await ai.remember(CID, "@petya", "я пас")

    # --- 2. дубль целевой реплики ---
    await ai.remember(CID, "@kolya", "давай ты")
    snap = await ai.history(CID, s.ai_ctx)
    await ai.ask(s, "Овощехранилище", CID, "@kolya", "давай ты", ["@slusha_bot"],
                 snapshot=snap)
    q = SENT[-1][1]
    check("целевая реплика в переписке одна", q.count("@kolya: давай ты") == 1)

    # --- 4. на цель показываем, а не повторяем ---
    # Реплика уже стоит последней строкой переписки, а при ответе реплаем
    # есть ещё и нота о нём. Дословный повтор в задании давал модели один и
    # тот же текст трижды, и она отвечала обрывками фраз.
    check("цель названа дословно",
          "Отвечай на эту реплику — @kolya: «давай ты»" in q)

    # --- нота о реплае на соседнюю реплику ---
    # Когда отвечают на последнюю же реплику бота, нота ничего не добавляет,
    # зато пересказывает её текст перед самым заданием и перевешивает само
    # сообщение: на «Привет.» бот отвечал продолжением прошлой мысли. С нотой
    # он здоровался в 2 случаях из 12, без неё — в 8 из 8.
    class _Msg:
        def __init__(self, parent_id):
            self.reply_to_message = SimpleNamespace(
                message_id=parent_id, text="Инструменты требуют умения!",
                caption=None, from_user=SimpleNamespace(id=1000, username="slusha_bot",
                                                        full_name="Слюша"))
    from slusha.history import Line as HLine
    seen = [HLine("ты", "Инструменты требуют умения!", 77)]
    check("на соседнюю реплику ноты нет",
          await ai._reply_note(bot, _Msg(77), "@kolya", seen) is None)
    check("на реплику вглубь нота остаётся",
          await ai._reply_note(bot, _Msg(55), "@kolya", seen) is not None)
    # --- 3. контекст реплая ---
    to_bot = SimpleNamespace(from_user=SimpleNamespace(id=1000, username="slusha_bot",
                                                       full_name="Слюша"),
                             text="я схожу", caption=None)
    note = await ai._reply_note(bot, msg(5, "@kolya", "точно?", reply=to_bot), "@kolya")
    check("реплай боту распознан",
          note == "@kolya отвечает на твоё сообщение: «я схожу».")

    to_other = SimpleNamespace(from_user=SimpleNamespace(id=2, username="petya",
                                                         full_name="Петя"),
                               text="я пас", caption=None)
    note2 = await ai._reply_note(bot, msg(5, "@kolya", "почему?", reply=to_other), "@kolya")
    check("реплай другому человеку распознан",
          note2 == "@kolya отвечает на сообщение @petya: «я пас».")
    check("без реплая строки нет",
          await ai._reply_note(bot, msg(5, "@kolya", "просто так"), "@kolya") is None)

    await ai.ask(s, "Овощехранилище", CID, "@kolya", "точно?", ["@slusha_bot"],
                 snapshot=snap, reply_note=note)
    check("контекст реплая уехал в промпт", note in SENT[-1][1])

    # --- 1. гонка: пришедшее во время генерации в промпт не попадает ---
    snap_before = await ai.history(CID, s.ai_ctx)
    await ai.remember(CID, "@stranger", "СРОЧНО КУПИ КВАРТИРУ")     # прилетело позже
    await ai.ask(s, "Овощехранилище", CID, "@kolya", "давай ты", ["@slusha_bot"],
                 snapshot=snap_before)
    check("чужое сообщение из будущего в промпт не попало",
          "СРОЧНО КУПИ КВАРТИРУ" not in SENT[-1][1])
    # а без снимка — попало бы
    await ai.ask(s, "Овощехранилище", CID, "@kolya", "давай ты", ["@slusha_bot"])
    check("без снимка оно бы просочилось (проверка самой проверки)",
          "СРОЧНО КУПИ КВАРТИРУ" in SENT[-1][1])

    # --- 5. свои реплики подписаны «ты» ---
    await ai._respond(bot, msg(3, "@kolya", "давай ты"), s, "@kolya", "давай ты",
                      snapshot=snap, note=None)
    last = (await ai.history(CID, 5))[-1]
    check("свой ответ записан как «ты»", last.who == ai.SELF)
    check("текст ответа сохранён", last.text == "ответ бота")

    snap2 = await ai.history(CID, s.ai_ctx)
    await ai.ask(s, "Овощехранилище", CID, "@kolya", "и?", ["@slusha_bot"], snapshot=snap2)
    check("свои реплики идут ходом assistant",
          "assistant: ответ бота" in SENT[-1][1])
    check("юзернейма бота в переписке нет",
          "@slusha_bot: ответ бота" not in SENT[-1][1])

    # --- 6. окно контекста ---
    check("num_ctx поднят до 16384", config.AI_NUM_CTX == 16384)

    # --- 7. привычки: зачин, своё имя, свои цитаты в заметках ---
    recent = ["Ёпта, пизда! Ну да, фута.", "Да ладно тебе. Не кипишь.",
              "Ёпта, пизда! Свинья с радио?"]
    check("прилипший зачин пойман, хоть его и говорил человек",
          ai.hooked("Ёпта, пизда! Да ты серьёзно?", recent, ["Я говорил Ёпта пизда"])
          == "Ёпта, пизда")
    check("зачин один раз — не привычка",
          ai.hooked("Да ладно тебе, Миша! Детям лучшее.", recent) == "")
    names = ["яни", "таба", "кошка"]
    check("своё имя в обращении вырезано",
          ai.strip_self_address("С днём рожденья, таба! Торт с меня.", names)
          == "С днём рожденья! Торт с меня.")
    check("и в начале", ai.strip_self_address("Таба, ну ты чего?", names) == "Ну ты чего?")
    check("имя без обращения остаётся",
          ai.strip_self_address("Ну ты и кошка, конечно.", names) == "Ну ты и кошка, конечно.")
    from slusha import summary
    notes = ("ФАКТЫ О ТЕБЕ:\n— Ты считаешься «овощем».\n"
             "— Бот предлагает расслабиться (“Ёпта, пизда! Да брось ты, что платить за дружбу?”).")
    check("образец из задания вырезан из записи о человеке",
          summary._junk([("@a", "чем занят, что о нём известно, как разговаривает. "
                                "(Смешливый, часто шутит.)"),
                         ("@b", "чем занят, что о нём известно, как разговаривает.")])
          == [("@a", "Смешливый, часто шутит.")])
    was = (config.AI_COLLECT_MODEL, config.AI_PROVIDER)
    config.AI_COLLECT_MODEL = "gemma3:12b"
    with ai.collecting():
        on = ai._collect.get()
        wait = ai._timeout()
    check("сборщик идёт к своей модели, если она задана и это Ollama",
          on == (ai.mode() == "ollama")
          and (wait == config.AI_COLLECT_TIMEOUT if on else True))
    check("вне блока — обычная модель", not ai._collect.get())
    config.AI_COLLECT_MODEL = ""
    with ai.collecting():
        check("без настройки блок ничего не меняет", not ai._collect.get())
    config.AI_COLLECT_MODEL = was[0]
    check("своя цитата внутри строки заметок вырезана",
          summary._drop_own(notes, ["Ёпта, пизда! Да брось ты, что платить за дружбу?"])
          == "ФАКТЫ О ТЕБЕ:\n— Ты считаешься «овощем».")

    from slusha import history as store
    await store.close()

    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
