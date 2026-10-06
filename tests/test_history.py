"""История Слюши: переживает перезапуск, ловит вложения, чистит хвост."""
import asyncio
import os
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-hist-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, config, db, history      # noqa: E402

CID = -100555
FAILS = []
SENT = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class FakeBot:
    async def me(self):
        return SimpleNamespace(id=1000, username="slusha_bot", full_name="Слюша")

    async def send_chat_action(self, *a, **kw):
        return True

    async def send_message(self, *a, **kw):
        return SimpleNamespace(message_id=1)


def msg(text=None, caption=None, **media):
    base = dict(chat=SimpleNamespace(id=CID, title="Чат", type="supergroup"),
                from_user=SimpleNamespace(id=7, username="vasya", full_name="Вася",
                                          is_bot=False),
                text=text, caption=caption, message_id=1, reply_to_message=None,
                sticker=None, photo=None, animation=None, video=None, voice=None,
                video_note=None, audio=None, document=None, poll=None, dice=None,
                location=None, venue=None, contact=None, game=None)
    base.update(media)
    return SimpleNamespace(**base)


async def fake_ollama(system, messages, tokens=0, images=None):
    SENT.append(messages)
    return "ответ"


async def main():
    check("база переписки отдельная", config.HISTORY_DB != config.DB_PATH)
    check("и лежит рядом с временной", config.HISTORY_DB.startswith(TMP))

    await db.init()
    await db.upsert_chat(CID, "Чат", None, 424211817)
    await db.set_setting(CID, "ai_on", 1)
    s = await db.get_settings(CID)
    check("окно контекста по умолчанию как в config",
          s.ai_ctx == config.AI_CTX_DEFAULT)
    check("и это не полсотни: небольшая модель столько уже размазывает",
          config.AI_CTX_DEFAULT <= 30)
    check("буфер памяти не меньше самого большого окна",
          config.AI_HISTORY >= max(config.AI_CTX_PRESETS))

    # --- 1. переписка переживает перезапуск ---
    await ai.remember(CID, "@vasya", "первое")
    await ai.remember(CID, ai.SELF, "второе")
    ai._history.clear()                      # как будто процесс перезапустили
    ai._loaded.clear()
    rows = await ai.history(CID, 50)
    check("история поднялась из базы", [r[1] for r in rows] == ["первое", "второе"])
    check("автор сохранился", rows[1][0] == ai.SELF)

    # --- 2. вложения ---
    bot = FakeBot()
    ai._ask_ollama = fake_ollama
    cases = [
        (msg(sticker=SimpleNamespace(emoji="😀")), "[стикер 😀]"),
        (msg(photo=[object()]), "[фото]"),
        (msg(voice=object()), "[голосовое]"),
        (msg(video_note=object()), "[кружок]"),
        (msg(animation=object()), "[гифка]"),
        (msg(document=SimpleNamespace(file_name="смета.pdf")), "[файл: смета.pdf]"),
        (msg(poll=SimpleNamespace(question="пиво?")), "[опрос: пиво?]"),
        (msg(dice=SimpleNamespace(emoji="🎲", value=6)), "[кубик 🎲: 6]"),
    ]
    for m, want in cases:
        check(f"подпись {want}", ai.attachment_label(m) == want)
    check("служебное событие пропускаем", ai.attachment_label(msg()) == "")

    before = len(SENT)
    await ai.maybe_reply(bot, msg(photo=[object()]), s)
    check("на голое вложение бот не отвечает", len(SENT) == before)
    check("но в историю оно попало", (await ai.history(CID, 1))[0][1] == "[фото]")

    # --- 2б. стикеры: боту — описание словами, остальным — только метка ---
    from slusha import vision
    import io

    class StickerBot(FakeBot):
        downloads = 0

        async def download(self, fid):
            StickerBot.downloads += 1
            return io.BytesIO(b"RIFF\x00\x00\x00\x00WEBPVP8 ")

    seen_calls = []

    async def fake_seeing(system, messages, tokens=0, images=None):
        if images and messages[-1]["content"] == ai._SEE_STICKER:
            seen_calls.append(images)
            return "Стикер с грустной лягушкой [мем]."
        SENT.append(messages)
        return "ответ"

    ai._ask_ollama = fake_seeing
    sbot = StickerBot()
    own = SimpleNamespace(message_id=500, text="Ты опять за своё.", caption=None,
                          from_user=SimpleNamespace(id=1000, is_bot=True))
    static = SimpleNamespace(emoji="😞", file_id="s1", file_unique_id="u1",
                             file_size=900, is_animated=False, is_video=False,
                             thumbnail=None)
    animated = SimpleNamespace(emoji="😂", file_id="a1", file_unique_id="u2",
                               file_size=900, is_animated=True, is_video=False,
                               thumbnail=SimpleNamespace(file_id="t2", file_size=300))
    bare_anim = SimpleNamespace(emoji="😂", file_id="a3", file_unique_id="u3",
                                is_animated=True, is_video=False, thumbnail=None)
    check("обычный стикер показываем сам", vision._sticker(msg(sticker=static)) is static)
    check("анимированный — превью", vision._sticker(msg(sticker=animated)) is animated.thumbnail)
    check("без превью показать нечем", not vision.has_sticker(msg(sticker=bare_anim)))
    check("webp так и называем", ai._mime("UklGRgAAAABXRUJQ") == "image/webp")
    check("jpeg по умолчанию", ai._mime("/9j/4AAQ") == "image/jpeg")

    await db.set_setting(CID, "ai_reply", 100)
    await db.set_setting(CID, "ai_vision", 1)
    s = await db.get_settings(CID)
    ai._last_reply.clear()
    before = len(SENT)
    await ai.maybe_reply(sbot, msg(sticker=static, message_id=501, reply_to_message=own), s)
    await asyncio.sleep(0.2)
    last = [r[1] for r in await ai.history(CID, 3) if r[1].startswith("[стикер")][-1]
    check(f"стикер боту описан словами: {last}", last == "[стикер 😞: с грустной лягушкой (мем)]")
    check("и бот на него ответил", len(SENT) == before + 1)
    flat = str(SENT[-1])
    check("в задании описание, а не картинка", "грустной лягушкой" in flat)

    ai._last_reply.clear()
    await ai.maybe_reply(sbot, msg(sticker=static, message_id=502, reply_to_message=own), s)
    await asyncio.sleep(0.2)
    check("тот же стикер второй раз не описываем", len(seen_calls) == 1)
    check("и не скачиваем", StickerBot.downloads == 1)

    ai._last_reply.clear()
    before = len(SENT)
    await ai.maybe_reply(sbot, msg(sticker=animated, message_id=503), s)
    await asyncio.sleep(0.2)
    check("стикер не боту — только метка", (await ai.history(CID, 1))[0][1] == "[стикер 😂]")
    check("и без ответа и описания", len(SENT) == before and len(seen_calls) == 1)

    ai._last_reply.clear()
    before = len(SENT)
    await ai.maybe_reply(sbot, msg(sticker=bare_anim, message_id=504, reply_to_message=own), s)
    await asyncio.sleep(0.2)
    check("не разглядели — молчим", len(SENT) == before)
    check("но метку запомнили", (await ai.history(CID, 1))[0][1] == "[стикер 😂]")

    await db.set_setting(CID, "ai_reply", 0)
    s = await db.get_settings(CID)
    ai._last_reply.clear()
    await ai.maybe_reply(sbot, msg(sticker=animated, message_id=505, reply_to_message=own), s)
    await asyncio.sleep(0.2)
    check("реплаи выключены — на стикер не отвечаем", len(SENT) == before)
    check("и не тратим модель на описание", len(seen_calls) == 1)

    theirs = SimpleNamespace(message_id=506, sticker=animated, photo=None,
                             text=None, caption=None,
                             from_user=SimpleNamespace(id=8, username="petya",
                                                       full_name="Петя", is_bot=False))
    got = await vision.grab(sbot, msg("что это?", message_id=507, reply_to_message=theirs))
    check("стикер, на который отвечают, идёт картинкой", len(got) == 1)
    got = await vision.grab(sbot, msg(sticker=static, message_id=508))
    check("свой стикер картинкой не шлём — он уже словами", got == [])

    # вопрос текстом на стикер — описание в самом вопросе, без картинки
    pics = []

    async def fake_seeing2(system, messages, tokens=0, images=None):
        if images and messages[-1]["content"] == ai._SEE_STICKER:
            seen_calls.append(images)
            return "Утёнок в панике"
        pics.append(images)
        SENT.append(messages)
        return "утёнок паникует, а ты нет"

    async def always(*a, **k):
        return True

    ai._ask_ollama = fake_seeing2
    real_should = ai.should_reply
    ai.should_reply = always
    await db.set_setting(CID, "ai_reply", 50)
    s = await db.get_settings(CID)
    ai._last_reply.clear()
    before = len(SENT)
    await ai.maybe_reply(sbot, msg("что тут?", message_id=509, reply_to_message=theirs), s)
    await asyncio.sleep(0.2)
    check("на вопрос про стикер ответили", len(SENT) == before + 1)
    flat = str(SENT[-1])
    check("описание стикера в вопросе",
          "что тут? [в ответ на стикер 😂: Утёнок в панике]" in flat)
    check("вопрос в переписке один раз", flat.count("@vasya: что тут?") == 1)
    check("картинку стикера не прикладываем", not pics[-1])
    mine = [r[1] for r in await ai.history(CID, 4) if r[1].startswith("что тут")]
    check(f"в историю — без описания: {mine}", mine == ["что тут?"])

    # фото с подписью — описание в вопросе, картинка остаётся
    async def fake_seeing3(system, messages, tokens=0, images=None):
        if images and messages[-1]["content"] == ai._SEE_PHOTO:
            return "На картинке изображен интерфейс Steam с модами [Wallpaper Engine]."
        pics.append(images)
        SENT.append(messages)
        return "моды так себе"

    ai._ask_ollama = fake_seeing3
    ai._last_reply.clear()
    shot = [SimpleNamespace(file_id="p1", file_unique_id="pu1", file_size=900,
                            width=800, height=600)]
    await ai.maybe_reply(sbot, msg(caption="вот такая штука", message_id=510, photo=shot), s)
    await asyncio.sleep(0.2)
    flat = str(SENT[-1])
    check("описание фото в вопросе",
          "вот такая штука [на фото: интерфейс Steam с модами (Wallpaper Engine)]" in flat)
    check("картинка фото остаётся", bool(pics[-1]))
    mine = [r[1] for r in await ai.history(CID, 4) if r[1].startswith("вот такая")]
    check(f"фото в историю — без описания: {mine}", mine == ["вот такая штука"])
    ai.should_reply = real_should
    ai._ask_ollama = fake_ollama
    await db.set_setting(CID, "ai_reply", 50)
    s = await db.get_settings(CID)

    # --- 3. чистка хвоста пачками ---
    for i in range(history.KEEP + history.PRUNE_EVERY + 5):
        await ai.remember(CID, "@vasya", f"строка {i}")
    await history.close()
    con = sqlite3.connect(config.HISTORY_DB)
    left = con.execute("SELECT COUNT(*) FROM ai_history WHERE chat_id=?", (CID,)).fetchone()[0]
    con.close()
    check(f"в базе не копится лишнее (осталось {left})", left <= history.KEEP + history.PRUNE_EVERY)

    # --- 4. «забыть переписку» чистит и базу ---
    wiped = await ai.forget(CID)
    check("забыли не ноль", wiped > 0)
    check("в памяти пусто", await ai.history(CID, 50) == [])
    await history.close()
    con = sqlite3.connect(config.HISTORY_DB)
    left = con.execute("SELECT COUNT(*) FROM ai_history WHERE chat_id=?", (CID,)).fetchone()[0]
    con.close()
    check("и в базе пусто", left == 0)

    # --- 4b. одновременное первое обращение к базе ---
    # Реакции в чате прилетают пачкой, и каждая лезет в базу своим хендлером.
    # Без замка все они видели «соединения нет», открывали по своему и делали
    # PRAGMA поверх чужой транзакции: SQLite отвечал «Safety level may not be
    # changed inside a transaction», реакция терялась, в лог сыпались
    # трейсбеки. Ровно это и было видно в боевом логе.
    await history.close()
    results = await asyncio.gather(
        *[history.tail(CID, 5) for _ in range(12)], return_exceptions=True)
    beda = [r for r in results if isinstance(r, Exception)]
    check(f"пачка одновременных обращений не падает ({len(beda)} ошибок)", not beda)
    check("соединение при этом одно", history._db is not None)

    # --- 5. миграция окна контекста ---
    await db.set_setting(CID, "ai_ctx", 20)          # как было у старых чатов
    await db.kv_set("mig_ctx50", None)               # флаг снят — миграция повторится
    other = -100556
    await db.upsert_chat(other, "Второй", None, 424211817)
    await db.set_setting(other, "ai_ctx", 30)        # осознанный выбор человека
    await db._migrate()
    check("старый дефолт подняли до 50", (await db.get_settings(CID)).ai_ctx == 50)
    check("чужой выбор не тронули", (await db.get_settings(other)).ai_ctx == 30)
    await db.set_setting(CID, "ai_ctx", 20)
    await db._migrate()                              # флаг уже стоит
    check("повторно миграция не срабатывает", (await db.get_settings(CID)).ai_ctx == 20)

    await history.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
