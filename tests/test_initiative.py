"""Бот заговаривает первым: вопрос про созревший план (initiative.py).

Модель подменена: проверяем, когда бот пишет сам, а когда молчит, — и что
реплика уходит реплаем на сам план.
"""
import asyncio
import os
import sys
import tempfile
import time
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-init-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_HISTORY_DB=os.path.join(TMP, "h.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b", AI_COLLECT_MODEL="", MEM_NOTES="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, config, db, initiative, plans          # noqa: E402
from slusha import history as store                           # noqa: E402

CID = -100777
FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def me(self):
        return SimpleNamespace(id=1000, username="slusha_bot", full_name="Слюша")

    async def send_chat_action(self, *a, **kw):
        return True

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))
        return SimpleNamespace(message_id=900 + len(self.sent))


def msg(text, who="vasya", uid=7, mid=1):
    return SimpleNamespace(
        chat=SimpleNamespace(id=CID, title="Чат", type="supergroup"),
        from_user=SimpleNamespace(id=uid, username=who, full_name=who, is_bot=False),
        text=text, caption=None, message_id=mid, reply_to_message=None,
        is_topic_message=False, message_thread_id=None)


async def reset():
    """Новый день для счётчиков: ни своих реплик, ни паузы после ответа."""
    await db.kv_set(initiative._day_key(CID), None)
    ai._last_reply.pop(CID, None)


async def main():
    await db.init()
    await db.upsert_chat(CID, "Чат", None, 424211817)
    await db.set_setting(CID, "ai_on", 1)
    await db.set_setting(CID, "ai_random", 0)        # сам в разговор не лезет
    await db.set_setting(CID, "ai_persona", "Ты Коул, язвительный учёный.")
    s = await db.get_settings(CID)
    bot = FakeBot()
    now = int(time.time())

    # --- словарь планов ---
    check("«на еду» — не «поеду»", plans.detect(
        "Блин, мы с соседом скидываемся на еду а холодильнике. Сосед на выходных не ест",
        now) is None)
    check("«на выходных еду на дачу» — план", plans.detect("на выходных еду на дачу", now))
    check("из плана — предложение со сроком", initiative.gist(
        "я думаю завтра забрать её домой.. но как её тащить, есть идеи?")
        == "я думаю завтра забрать её домой..")

    # --- реплика-вопрос: характер в системе, план в задании, без кавычек ---
    asked = []

    async def fake(system, messages, tokens, images):
        asked.append((system, messages))
        return "«@vasya, ну что, собес пережил?»"

    ai._ask_ollama = fake
    out = await initiative.write(s, CID, "@vasya", "в пятницу иду на собес. Кто со мной?")
    check("вопрос без кавычек", out == "@vasya, ну что, собес пережил?")
    check("характер — в системе", asked[-1][0] == "Ты Коул, язвительный учёный.")
    task = str(asked[-1][1])
    check("в задании — только предложение со сроком", "собес" in task and "Кто со мной" not in task)

    async def babble(system, messages, tokens, images):
        return "Хм. " * 200

    ai._ask_ollama = babble
    check("простыня — не вопрос", await initiative.write(s, CID, "@vasya", "завтра собес") == "")
    ai._ask_ollama = fake

    # --- готовим заранее: только то, что скоро созреет ---
    await store.plan_add(CID, "@vasya", "в пятницу иду на собес", now - 86400,
                         now + 3600, now + 4 * 86400, 3, 42, None)
    await store.plan_add(CID, "@masha", "в субботу едем на дачу", now - 86400,
                         now + 10 * 3600, now + 4 * 86400, 3, 43, None)
    check("готовим за два часа до срока", await initiative.prepare(CID, s, now) == 1)
    check("второй раз не пишем", await initiative.prepare(CID, s, now) == 0)
    check("не созрел — не спрашиваем", not await initiative.seen(bot, s, CID, "@vasya"))
    await store.plan_add(CID, "@mre", "скидываемся на еду, сосед на выходных не ест",
                         now - 86400, now - 3600, now + 3 * 86400, 3, 41, None)
    await initiative.prepare(CID, s, now)
    check("старый ложный план закрыт", not await store.plans_unwritten(CID, now, now)
          and not await store.plans_ripe(CID, now, who="@mre"))

    # --- ступень 1: человек появился ---
    later = now + 2 * 3600
    real_time = time.time
    time.time = lambda: later
    try:
        await reset()
        got = await initiative.seen(bot, s, CID, "@vasya")
        check("появился — спросили", got and bot.sent)
        _, text, kw = bot.sent[-1]
        check("реплаем на сам план", kw["reply_parameters"].message_id == 42)
        check("и готовой репликой", text == "@vasya, ну что, собес пережил?")
        check("в переписку записано", (await ai.history(CID, 5))[-1].who == ai.SELF)
        await reset()
        check("один раз", not await initiative.seen(bot, s, CID, "@vasya"))
    finally:
        time.time = real_time

    # --- ступень 1 из конвейера: человек пишет о другом ---
    await store.plan_add(CID, "@petya", "завтра сдаю на права", now - 2 * 86400,
                         now - 3600, now + 3 * 86400, 3, 44, None)
    await initiative.prepare(CID, s, now)
    await reset()
    n = len(bot.sent)
    await ai.maybe_reply(bot, msg("всем привет, что нового", who="petya", mid=50), s)
    check("пишет о другом — бот спросил про план", len(bot.sent) == n + 1
          and bot.sent[-1][2]["reply_parameters"].message_id == 44)

    # уже рассказал сам — не переспрашиваем, план закрыт
    await store.plan_add(CID, "@lena", "завтра сдаю на права", now - 2 * 86400,
                         now - 3600, now + 3 * 86400, 3, 45, None)
    await initiative.prepare(CID, s, now)
    await reset()
    n = len(bot.sent)
    await ai.maybe_reply(bot, msg("ура, права сдала с первого раза", who="lena", mid=51), s)
    check("рассказал сам — молчим", len(bot.sent) == n)
    check("и план закрыт", not await store.plans_ripe(CID, now, who="@lena"))

    # не больше одной своей реплики в сутки
    await store.plan_add(CID, "@kot", "завтра концерт у меня", now - 2 * 86400,
                         now - 3600, now + 3 * 86400, 3, 46, None)
    await initiative.prepare(CID, s, now)
    ai._last_reply.pop(CID, None)
    await db.kv_set(initiative._day_key(CID), "1")
    check("сутки исчерпаны — молчим", not await initiative.seen(bot, s, CID, "@kot"))

    # --- ступень 2: не появился — пишет сам ---
    await reset()
    real_day, real_wait = initiative._daytime, config.AI_INITIATIVE_WAIT
    initiative._daytime = lambda t: True
    try:
        await ai.remember(CID, "@x", "о, привет")        # последняя реплика — сейчас
        # план @kot созрел час назад: ждать, вдруг появится сам
        check("рано — ждём, вдруг появится сам",
              not await initiative.nudge(bot, s, CID, now + 1200))
        config.AI_INITIATIVE_WAIT = 0
        check("посреди разговора — ждём", not await initiative.nudge(bot, s, CID, now + 60))
        check("чат спит — молчим",
              not await initiative.nudge(bot, s, CID, now + initiative.ACTIVE + 3600))
        await db.set_setting(CID, "ai_initiative", 1)
        s1 = await db.get_settings(CID)
        check("«когда появится» — сам не пишет",
              not await initiative.nudge(bot, s1, CID, now + 1200))
        await db.set_setting(CID, "ai_initiative", 2)
        check("пауза в живом чате — пишет сам", await initiative.nudge(bot, s, CID, now + 1200)
              and bot.sent[-1][2]["reply_parameters"].message_id == 46)
    finally:
        initiative._daytime, config.AI_INITIATIVE_WAIT = real_day, real_wait

    # Москва: 03:00 — ночь, 15:00 — день
    midnight = now - (now + plans.TZ) % 86400
    check("ночью не пишет", not initiative._daytime(midnight + 3 * 3600))
    check("днём пишет", initiative._daytime(midnight + 15 * 3600))

    # --- выключено ---
    await db.set_setting(CID, "ai_initiative", 0)
    s0 = await db.get_settings(CID)
    await store.plan_add(CID, "@ivan", "завтра у меня экзамен", now - 2 * 86400,
                         now - 3600, now + 3 * 86400, 3, 47, None)
    await initiative.tick(bot, now)
    await reset()
    check("выключено — не готовим и не спрашиваем",
          not await initiative.seen(bot, s0, CID, "@ivan")
          and not await store.plans_ripe(CID, now, who="@ivan"))

    await store.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


code = asyncio.run(main())
sys.stdout.flush()
os._exit(code)
