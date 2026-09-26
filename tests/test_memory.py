"""Заметки о чате и миграции баз.

Половина файла — про то, как новые колонки приезжают в УЖЕ СУЩЕСТВУЮЩУЮ базу.
Проверять это на пустой бесполезно: там всё создаётся сразу правильным, и
ошибка «no such column» вылезает только на боевой. Поэтому здесь база сначала
собирается по старой схеме и наполняется данными, и только потом открывается
рабочим кодом.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="slusha-mem-")
DB = os.path.join(TMP, "t.sqlite3")
HIST = os.path.join(TMP, "t_history.sqlite3")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=DB, SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b", AI_SUMMARY_EVERY="10")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

CID = -100321
FAILS = []
ASKED = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


def build_old_bases():
    """Собрать базы такими, какими они были до этой правки, и налить данных."""
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE chats(chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT,
                           owner_id INTEGER, active INTEGER NOT NULL DEFAULT 1,
                           added_at INTEGER NOT NULL);
        CREATE TABLE settings(chat_id INTEGER PRIMARY KEY,
                              ai_on INTEGER NOT NULL DEFAULT 0, ai_persona TEXT,
                              ai_random INTEGER NOT NULL DEFAULT 3,
                              ai_ctx INTEGER NOT NULL DEFAULT 50,
                              ai_daily INTEGER NOT NULL DEFAULT 100,
                              ai_names TEXT, ai_free INTEGER NOT NULL DEFAULT 0,
                              ai_len INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE lore(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                          keys TEXT, content TEXT NOT NULL,
                          always INTEGER NOT NULL DEFAULT 0,
                          prio INTEGER NOT NULL DEFAULT 100,
                          enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE access(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                            username TEXT, added INTEGER NOT NULL);
        CREATE TABLE users(user_id INTEGER PRIMARY KEY, username TEXT,
                           first_name TEXT, seen INTEGER NOT NULL);
        CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);
    """)
    con.execute("INSERT INTO chats VALUES (?,?,?,?,1,0)", (CID, "Овощехранилище", None, 424211817))
    # осознанные настройки живого чата: миграция не вправе их трогать
    con.execute("""INSERT INTO settings (chat_id, ai_on, ai_persona, ai_random, ai_ctx,
                                         ai_daily, ai_names, ai_free, ai_len)
                   VALUES (?,1,'ехидный торговец',10,80,500,'холо',1,2)""", (CID,))
    con.execute("INSERT INTO kv VALUES ('mig_ctx50','1')")
    con.commit()
    con.close()

    con = sqlite3.connect(HIST)
    con.executescript("""
        CREATE TABLE ai_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
                                chat_id INTEGER NOT NULL, who TEXT NOT NULL,
                                text TEXT NOT NULL, ts INTEGER NOT NULL);
        CREATE INDEX idx_ai_history_chat ON ai_history(chat_id, id);
    """)
    for i in range(40):
        con.execute("INSERT INTO ai_history (chat_id, who, text, ts) VALUES (?,?,?,?)",
                    (CID, "@vasya", f"старая реплика {i}", 1700000000 + i))
    con.commit()
    con.close()


async def settle(done, tries=200):
    """Подождать фоновую задачу. Голого sleep(0) мало: aiosqlite ходит в базу
    в отдельном потоке, и ему нужно настоящее время, а не просто уступка."""
    for _ in range(tries):
        await asyncio.sleep(0.01)
        if done():
            return True
    return False


async def fake_model(system, question, tokens=0, images=None):
    from slusha import ai
    text = ai.flatten(question) if isinstance(question, list) else question
    ASKED.append((system, text, tokens))
    return ("УЧАСТНИКИ:\n— @vasya — Вася — за пивом.\n— @petya — Петя всегда пас.\n"
            "ДОГОВОРЁННОСТИ И СОБЫТИЯ:\n—\n"
            "ШУТКИ И ПРОЗВИЩА:\n— Шутка про овощехранилище.\n"
            "ФАКТЫ О ТЕБЕ:\n—\n"
            "СЕЙЧАС ОБСУЖДАЮТ:\n— пиво.")


async def main():
    build_old_bases()
    from slusha import ai, config, db, history as store, summary   # noqa: E402

    # --- 1. миграция основной базы на копии боевой ---
    await db.init()
    cols = await db.columns("settings")
    check("новые колонки настроек дописаны",
          {"ai_reply", "ai_lang", "ai_vision", "ai_topics", "ai_greeting"} <= cols)
    s = await db.get_settings(CID)
    check("старые значения уцелели",
          (s.ai_random, s.ai_ctx, s.ai_daily, s.ai_len, s.ai_free) == (10, 80, 500, 2, 1))
    check("характер на месте", s.ai_persona == "ехидный торговец")
    check("у новых полей значения по умолчанию",
          (s.ai_lang, s.ai_vision, s.ai_topics) == (1, 0, 0))
    # ALTER TABLE с DEFAULT заполняет и уже существующие строки: чат из старой
    # базы получает 35%, а не ноль
    check("шанс ответить на ответ себе доехал до старого чата", s.ai_reply == 50)
    await db._migrate()                       # повторный прогон ничего не ломает
    check("миграция идемпотентна", (await db.get_settings(CID)).ai_ctx == 80)

    # --- 2. миграция базы переписки ---
    rows = await store.tail(CID, 100)
    check("старая переписка читается", len(rows) == 40)
    check("у старых строк msg_id пуст", rows[0].msg_id is None)
    check("и реакции пусты", rows[0].reactions == "")
    con = sqlite3.connect(HIST)
    cols = {r[1] for r in con.execute("PRAGMA table_info(ai_history)")}
    idx = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ai_history'")}
    con.close()
    check("колонки переписки дописаны",
          {"msg_id", "reply_to_id", "thread_id", "reactions"} <= cols)
    check("индекс по новой колонке создан после ALTER",
          "idx_ai_history_thread" in idx and "idx_ai_history_msg" in idx)

    await store.add(CID, "@petya", "новая реплика", 777, None, 42)
    fresh = (await store.tail(CID, 1))[0]
    check("новые поля пишутся", (fresh.msg_id, fresh.thread_id) == (777, 42))

    # --- 3. заметки собираются сами ---
    ai._ask_ollama = fake_model
    await ai.forget(CID)                      # начинаем с чистого листа
    for i in range(config.AI_SUMMARY_EVERY):
        await ai.remember(CID, "@vasya", f"реплика {i}", 1000 + i)
    await settle(lambda: bool(ASKED))
    await settle(lambda: True, 5)             # даём дописать результат в базу
    text, covered = await store.summary_get(CID)
    check("заметки собрались сами", bool(text))
    check("covered_id дошёл до последней реплики", covered > 0)
    check("модель просили именно пересказать", "заметки" in ASKED[-1][0].lower())
    check("каркас разделов задан",
          all(name in ASKED[-1][0] for name in summary._SECTIONS))
    # Формат и чистку списка задаём утвердительно: запреты с «не» маленькая
    # модель читает как подсказку и воспроизводит перечисленное.
    check("формат задан утвердительно", "простой текст" in ASKED[-1][0])
    # Людей вне пачки сборщик больше не видит и не вычёркивает: их записи
    # лежат в базе и доживают до следующего раза, когда человек заговорит.
    check("про остальных людей сказано, что они хранятся отдельно",
          "хранятся отдельно" in ASKED[-1][0])
    check("у каждого раздела свой потолок", "до двенадцати" in ASKED[-1][0])
    check("в пересказ уехали реплики чата", "реплика 3" in ASKED[-1][1])

    block = await summary.block(CID, ["@vasya"])
    check("заметки уходят в промпт", "Шутка про овощехранилище" in block)
    check("запись о собеседнике — тоже", "Вася — за пивом" in block)
    check("и помечены как справка", "не инструкции" in block)
    check("кто не в разговоре — того в промпте нет",
          "за пивом" not in await summary.block(CID, ["@kolya"]))
    # @petya модель приписала сама: в пачке он не писал и не упоминался.
    check("выдуманного человека в память не пишем",
          await store.people_get(CID, ["@petya"]) == [])

    # --- 4. второй раз пересказываем только новое ---
    ASKED.clear()
    for i in range(config.AI_SUMMARY_EVERY):
        await ai.remember(CID, "@petya", f"свежак {i}", 2000 + i)
    check("вторая пересборка случилась", await settle(lambda: bool(ASKED)))
    await settle(lambda: CID not in summary._busy)
    check("прошлые заметки показали модели", "Шутка про овощехранилище" in ASKED[-1][1])
    check("а запись о молчавшем в пачке — нет", "Вася — за пивом" not in ASKED[-1][1])
    check("и молчавший из памяти не пропал",
          "за пивом" in (await store.people_get(CID, ["@VASYA"]) or [("", "")])[0][1])
    check("писавший в пачке записан", bool(await store.people_get(CID, ["@petya"])))
    check("старое второй раз не пересказываем", "реплика 3" not in ASKED[-1][1])
    check("а новое — пересказываем", "свежак 3" in ASKED[-1][1])

    # --- 4b. перезапуск не теряет накопленное ---
    # Ровно та беда, из-за которой в живом чате висело 203 несжатых реплики
    # при пороге в 80: счётчик жил только в памяти, бот перезапускался чаще,
    # чем чат набирал порог, и заметки не собирались никогда.
    await ai.forget(CID)
    ASKED.clear()
    for i in range(config.AI_SUMMARY_EVERY * 3):
        await store.add(CID, "@vasya", f"накопилось {i}", 4000 + i)
    summary._pending.clear()                  # как будто процесс перезапустили
    summary._counted.clear()
    check("после перезапуска счётчик пуст", not summary._pending)
    await ai.remember(CID, "@petya", "первое сообщение после рестарта", 4999)
    check("накопленное подхватили из базы", await settle(lambda: bool(ASKED)))
    check("и пересказали именно его", "накопилось 5" in ASKED[-1][1])

    # --- 5. флаг «уже сжимаю» ---
    summary._busy.add(CID)
    ASKED.clear()
    for i in range(config.AI_SUMMARY_EVERY * 2):
        await ai.remember(CID, "@kolya", f"пока занято {i}", 3000 + i)
    await settle(lambda: bool(ASKED), 30)
    check("пока идёт пересборка, вторую не запускаем", not ASKED)
    summary._busy.discard(CID)

    # --- 5b. сорвавшийся запрос не роняет счётчик в минус ---
    async def broken(system, question, tokens=0, images=None):
        raise RuntimeError("модель недоступна")

    await ai.forget(CID)
    summary._counted.add(CID)                 # чат уже сверен, считаем от нуля
    ai._ask_ollama = broken
    for i in range(config.AI_SUMMARY_EVERY):
        await ai.remember(CID, "@vasya", f"сорвётся {i}", 5000 + i)
    await settle(lambda: CID not in summary._busy)
    check("после неудачи заметок нет", (await store.summary_get(CID))[0] == "")
    check("и счётчик сброшен, чтобы не дёргать модель каждым сообщением",
          summary._pending.get(CID, 0) == 0)
    ai._ask_ollama = fake_model

    # --- 6. слишком длинные заметки режутся ---
    async def verbose(system, question, tokens=0, images=None):
        head = "".join(f"{name}:\n— коротко\n" for name in summary._SECTIONS[1:])
        return head + "УЧАСТНИКИ:\n" + "".join(
            f"— @user{i} — очень подробно про всё на свете\n" for i in range(400))

    ai._ask_ollama = verbose
    await summary._compact(CID)
    text, _ = await store.summary_get(CID)
    check("заметки обрезаны до потолка", 0 < len(text) <= config.AI_SUMMARY_LIMIT)
    check("люди в общие заметки не попали", "@user" not in text)
    check("а общие разделы целы", text.rstrip().endswith("коротко"))
    check("четыреста выдуманных людей в память не легли",
          not await store.people_get(CID, [f"@user{i}" for i in range(400)]))

    # --- 6а. битые заметки не сохраняем ---
    # От пяти разделов оставался один, а хвост становился сырой перепиской.
    # Такие заметки уходили на вход следующей пересборке, и порча копилась.
    before, _ = await store.summary_get(CID)

    async def broken(system, question, tokens=0, images=None):
        return "УЧАСТНИКИ:\n— @vasya — пьёт\n@vasya: привет\n@petya: ку\n@kolya: йо\n@misha: ага"

    ai._ask_ollama = broken
    for _ in range(4):
        await store.add(CID, "@vasya", "ещё реплика")
    await summary._compact(CID)
    after, _ = await store.summary_get(CID)
    check("заметки без разделов не записаны", after == before)
    check("и сырая переписка в заметки не попадает",
          bool(summary._defect("УЧАСТНИКИ:\n" + "@a: б\n" * 5)))

    # --- 6б. список участников: потолок, слияние повторов, свежие вперёд ---
    # Модель переписывала старый список целиком и дописывала новых: 19 строк,
    # один человек трижды. Раздутый список съел бы лимит и последний раздел.
    tail = "".join(f"{n}:\n—\n" for n in summary._SECTIONS[1:])
    many = "УЧАСТНИКИ:\n" + "".join(f"— @u{i} — что-то\n" for i in range(20)) + tail
    trimmed = summary._trim_people(many)
    nicks = [ln for ln in trimmed.splitlines() if ln.startswith("— @")]
    check("участников не больше потолка", len(nicks) == summary.PEOPLE_MAX)
    check("остальные разделы целы", all(f"{n}:" in trimmed for n in summary._SECTIONS))

    # Новое о человеке модель пишет второй строкой ниже старой. Раньше
    # оставалась первая — и новое терялось ровно тогда, когда появлялось.
    dup = ("УЧАСТНИКИ:\n— @vasya — пьёт пиво\n— @petya — молчит\n"
           "— @vasya — купил велосипед\n" + tail)
    merged = summary._trim_people(dup)
    vasya = [ln for ln in merged.splitlines() if "@vasya" in ln]
    check("человек остался одной строкой", len(vasya) == 1)
    check("новое о нём не потерялось", "велосипед" in vasya[0])
    check("и старое тоже", "пиво" in vasya[0])
    check("свежее идёт первым", vasya[0].index("велосипед") < vasya[0].index("пиво"))

    # Новичков модель дописывает в конец. При полном списке их отрезало,
    # и в память не попадал никто из тех, кто только что пришёл.
    full = "УЧАСТНИКИ:\n" + "".join(f"— @old{i} — давно\n" for i in range(12))
    full += "— @newbie — только пришёл\n" + tail
    kept = summary._trim_people(full, recent={"@newbie"})
    check("писавший только что попадает в полный список", "@newbie" in kept)
    check("а потолок всё равно соблюдён",
          sum(ln.startswith("— @") for ln in kept.splitlines()) == summary.PEOPLE_MAX)

    # Слитая строка тоже не бесконечная.
    long_dup = "УЧАСТНИКИ:\n" + "".join(f"— @vasya — факт номер {i}\n" for i in range(60)) + tail
    line = [ln for ln in summary._trim_people(long_dup).splitlines() if "@vasya" in ln][0]
    check("слитая строка укладывается", len(line) <= summary.PERSON_CHARS + 20)
    # --- 6в. заметки в markdown приводятся к одной форме ---
    # Модель копирует оформление прошлых заметок. Раз съехав в «**Участники:**»,
    # она держалась его, проверка не находила разделов и браковала всё подряд.
    md = ("**Участники:**\n*   detective\\_official: Драматичный\n"
          "**О чём договорились/обсуждали:**\n*   спорят\n"
          "**Шутки и прозвища:**\n*   нет\n**Факты о тебе:**\n*   зовут Холо\n"
          "**Сейчас обсуждают:**\n*   погода")
    norm = summary._normalize(md)
    check("markdown-заметки узнаются", summary._defect(norm) == "")
    check("человек в старом оформлении стал строкой с ником",
          "— @detective_official — Драматичный" in norm)
    check("звёздочки-маркеры стали тире", "*" not in norm)

    # --- 6г. память о людях отдельно от заметок ---
    await ai.forget(CID)
    # Старые заметки с людьми внутри при первом касании раскладываются.
    tail = "".join(f"{n}:\n— коротко\n" for n in summary._SECTIONS[1:])
    await store.summary_set(CID, "УЧАСТНИКИ:\n— @vasya — пьёт пиво\n"
                            "— Иван Петров — без юзернейма\n" + tail, 7)
    block = await summary.block(CID, ["@vasya", "Иван Петров"])
    text, covered = await store.summary_get(CID)
    check("старые заметки разложены: людей в заметках нет", "УЧАСТНИКИ" not in text)
    check("и прочие разделы на месте", "ШУТКИ И ПРОЗВИЩА:" in text)
    check("и отметка, докуда пересказано, та же", covered == 7)
    check("люди переехали в таблицу", len(await store.people_all(CID)) == 2)
    check("человек без юзернейма тоже", "без юзернейма" in block)

    # В промпт — только те, кто в разговоре, и не больше потолка.
    await store.people_set(CID, [(f"@p{i}", f"человек {i}") for i in range(50)])
    block = await summary.block(CID, [f"@p{i}" for i in range(20)])
    check("в промпт уходит не больше потолка людей",
          block.count("— @p") == config.AI_PROMPT_PEOPLE)
    check("и первыми — самые важные", "@p0 —" in block and "@p19 —" not in block)
    check("а помним всех", await store.people_count(CID) == 52)

    # Кто в разговоре: собеседник, потом кого он упомянул, потом свежие авторы.
    L = store.Line
    rows = [L("@old", "давно"), L("ты", "я бот"), L("@kolya", "привет @petya"),
            L("@vasya", "а где @misha?")]
    who = summary.present(rows, "@vasya", self_names=["Слюша"])
    check("первым — тот, кому отвечаем", who[0] == "@vasya")
    check("затем упомянутый им", who[1] == "@misha")
    check("затем свежие авторы", who[2:4] == ["@kolya", "@old"])
    check("себя в списке нет", "ты" not in who)
    check("упомянутые в переписке — в хвосте", who[-1] == "@petya")

    # Мусор вместо описаний: модель раздала чужую пустую запись «вот да»
    # полудюжине человек и склеивала реплики через запятую.
    said = ["кушаю мозги с личинками", "надо посмотреть чо там за салатик",
            "вот да"]
    kept = dict(summary._junk([
        ("@a", "вот да"), ("@b", "вот да"),
        ("@c", "кушаю мозги с личинками, надо посмотреть чо там за салатик"),
        ("@d", "любит салаты; кушаю мозги с личинками"),
        ("@e", "[фото]"),
        ("@f", "студентка, пьёт чай литрами"),
    ], said))
    check("одна запись на многих — мусор", "@a" not in kept and "@b" not in kept)
    check("склеенные реплики — мусор", "@c" not in kept)
    check("цитату вырезаем, описание рядом оставляем", kept.get("@d") == "любит салаты")
    check("метка вложения — мусор", "@e" not in kept)
    check("настоящее описание остаётся", kept.get("@f") == "студентка, пьёт чай литрами")

    # Общие разделы: только строки «— …», не больше потолка, новое — в конце.
    raw = ("Вот заметки:\nУЧАСТНИКИ:\n— @a — б\nДОГОВОРЁННОСТИ И СОБЫТИЯ:\n"
           + "".join(f"— событие {i}\n" for i in range(9))
           + "ШУТКИ И ПРОЗВИЩА:\n—\nФАКТЫ О ТЕБЕ:\n—\nСЕЙЧАС ОБСУЖДАЮТ:\n— чай\n"
           "Дополнительные заметки:\n@a: привет\n@b: ку\n@c: йо\n@d: ага\n")
    tidy = summary._cap_sections(raw)
    check("преамбула и хвост с репликами выкинуты",
          "Вот заметки" not in tidy and "@b: ку" not in tidy)
    check("и пересборка из-за хвоста не бракуется", summary._defect(tidy) == "")
    check("раздел подрезан до потолка, новое осталось",
          "событие 8" in tidy and "событие 3" not in tidy)
    check("последний раздел цел", "— чай" in tidy)

    # Реплики самого бота в заметках возвращаются в ответы шаблоном.
    own = summary._drop_own(
        "ФАКТЫ О ТЕБЕ:\n— считают прохиндеем\n— Бот: Миша – это аномалия.\n"
        "— говорил, что ошибка в коде, испорченность неизбежна\n",
        ["Ошибка в коде. Испорченность неизбежна. ⚙️"])
    check("реплика бота с подписью выкинута", "аномалия" not in own)
    check("и пересказанная дословно тоже", "неизбежна" not in own)
    check("а мнение людей о боте осталось", "прохиндеем" in own)
    await store.summary_set(CID, "ФАКТЫ О ТЕБЕ:\n— бот: Итс овер.\n— считают умным\n", 0)
    blk = await summary.block(CID)
    check("старые реплики бота не доезжают до промпта",
          "Итс овер" not in blk and "умным" in blk)

    # Очистка заметок стирает и людей: это одна память.
    await summary.clear(CID)
    check("очистка заметок стирает людей", await store.people_count(CID) == 0)

    # --- 7. «забыть переписку» стирает и заметки ---
    await ai.forget(CID)
    check("заметок не осталось", (await store.summary_get(CID))[0] == "")
    check("и блок в промпте пуст", await summary.block(CID) == "")
    await store.people_set(CID, [("@vasya", "пьёт")])
    await ai.forget(CID)
    check("и люди забыты", await store.people_count(CID) == 0)

    await store.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
