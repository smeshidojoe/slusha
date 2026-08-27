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
    check("целевая реплика в промпте одна", q.count("@kolya: давай ты") == 1)

    # --- 4. цель названа дословно ---
    check("цель названа дословно", "Отвечай на эту реплику — @kolya: «давай ты»" in q)
    check("расплывчатой формулировки больше нет", "последней реплике" not in q)

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

    from slusha import history as store
    await store.close()

    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
