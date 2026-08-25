"""Меню: не только сборка экранов, но и сами колбэки.

Экраны собирались и раньше, а вот нажатия — нет: любая опечатка в разборе
callback_data («m:t:{cid}:{key}») ловилась только руками в Telegram. Здесь
хендлеры вызываются напрямую с подставными объектами, как их зовёт aiogram.
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix="slusha-menu-")
os.environ.update(SLUSHA_BOT_TOKEN="1:x", SLUSHA_ADMIN_IDS="424211817",
                  SLUSHA_DB_PATH=os.path.join(TMP, "t.sqlite3"),
                  SLUSHA_LOG_PATH=os.path.join(TMP, "t.log"),
                  AI_PROVIDER="ollama", AI_BASE_URL="http://127.0.0.1:11434",
                  AI_MODEL="gemma3:4b")
# корень проекта — на два уровня выше этого файла
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from slusha import ai, db, menu, schema      # noqa: E402

OWNER = 424211817
STRANGER = 999
CID = -1005555
FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL "), name)
    if not cond:
        FAILS.append(name)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.left = []

    async def me(self):
        return SimpleNamespace(id=1000, username="slusha_bot", full_name="Слюша")

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    async def edit_message_text(self, text, **kw):
        return True

    async def leave_chat(self, chat_id):
        self.left.append(chat_id)


class FakeMessage:
    def __init__(self, text=None, uid=OWNER):
        self.text = text
        self.document = None
        self.chat = SimpleNamespace(id=uid, type="private")
        self.from_user = SimpleNamespace(id=uid, username="mike", full_name="Майк")
        self.message_id = 10
        self.deleted = False

    async def delete(self):
        self.deleted = True

    async def answer(self, text, **kw):
        return SimpleNamespace(message_id=11)

    async def edit_text(self, text, **kw):
        self.text = text
        return True


class FakeCallback:
    """Достаточно того, чем пользуются хендлеры: data, кто нажал, что правим."""

    def __init__(self, data, uid=OWNER):
        self.data = data
        self.from_user = SimpleNamespace(id=uid, username="mike", full_name="Майк")
        self.message = FakeMessage(uid=uid)
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)
        return True


class FakeState:
    def __init__(self):
        self._data = {}
        self._state = None

    async def set_state(self, state):
        self._state = state

    async def get_state(self):
        return self._state

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data.clear()
        self._state = None


async def main():
    await db.init()
    await db.upsert_chat(CID, "Овощехранилище", None, OWNER)
    bot, state = FakeBot(), FakeState()

    # --- переключатели ---
    before = (await db.get_settings(CID)).ai_on
    await menu.cb_toggle(FakeCallback(f"m:t:{CID}:ai_on"), bot)
    check("тумблер переключился", (await db.get_settings(CID)).ai_on != before)
    await menu.cb_toggle(FakeCallback(f"m:t:{CID}:ai_vision"), bot)
    check("новый тумблер «смотреть картинки» работает",
          (await db.get_settings(CID)).ai_vision == 1)

    was = (await db.get_settings(CID)).ai_len
    await menu.cb_cycle(FakeCallback(f"m:y:{CID}:ai_len:+"))
    now = (await db.get_settings(CID)).ai_len
    check("селектор длины крутится", now == schema.cycle(schema.BY_KEY["ai_len"], was, 1))
    await menu.cb_cycle(FakeCallback(f"m:y:{CID}:ai_len:-"))
    check("и крутится обратно", (await db.get_settings(CID)).ai_len == was)
    await menu.cb_cycle(FakeCallback(f"m:y:{CID}:ai_lang:+"))
    check("селектор языка появился в меню",
          (await db.get_settings(CID)).ai_lang != 1)
    await db.set_setting(CID, "ai_lang", 1)

    # --- чужой чат ---
    other = -1006666
    await db.upsert_chat(other, "Чужой", None, 12345)
    cb = FakeCallback(f"m:t:{other}:ai_on", uid=STRANGER)
    await menu.cb_toggle(cb, bot)
    check("в чужой чат не пускает", (await db.get_settings(other)).ai_on == 0)
    check("и говорит об этом", any("не ваш" in a for a in cb.answers))

    # --- приветствие из карточки при включении ---
    await db.set_setting(CID, "ai_on", 0)
    await db.set_setting(CID, "ai_greeting", "Ну здравствуй, торговец.")
    bot.sent.clear()
    await menu.cb_toggle(FakeCallback(f"m:t:{CID}:ai_on"), bot)
    check("персонаж поздоровался сам",
          bot.sent and bot.sent[0] == (CID, "Ну здравствуй, торговец."))
    check("и это записано как своя реплика",
          (await ai.history(CID, 1))[0].who == ai.SELF)
    bot.sent.clear()
    await menu.cb_toggle(FakeCallback(f"m:t:{CID}:ai_on"), bot)
    await menu.cb_toggle(FakeCallback(f"m:t:{CID}:ai_on"), bot)
    check("но здоровается только при включении", len(bot.sent) == 1)

    # --- имена-обращения через FSM ---
    cb = FakeCallback(f"m:names:{CID}")
    await menu.cb_names(cb, state)
    check("экран имён ждёт ввода", await state.get_state() == menu.Input.names)
    m = FakeMessage("Холо*, Слюша")
    await menu.names_input(m, state, bot)
    check("имена сохранились", (await db.get_settings(CID)).ai_names == "холо*, слюша")
    check("сообщение пользователя убрано", m.deleted)
    check("состояние сброшено", await state.get_state() is None)

    # --- характер ---
    await menu.cb_persona(FakeCallback(f"m:persona:{CID}"), state)
    await menu.persona_input(FakeMessage("Ехидный торговец яблоками."), state, bot)
    check("характер сохранился",
          (await db.get_settings(CID)).ai_persona == "Ехидный торговец яблоками.")
    await menu.cb_persona(FakeCallback(f"m:persona:{CID}"), state)
    await menu.persona_input(FakeMessage("-"), state, bot)
    check("минус возвращает характер по умолчанию",
          (await db.get_settings(CID)).ai_persona is None)

    # --- лорбук ---
    await menu.cb_lore_add(FakeCallback(f"m:loreadd:{CID}"), state)
    await menu.lore_entry_input(FakeMessage("пиво, бар | В баре наливают тёмное."),
                                state, bot)
    rows = await db.lore_list(CID)
    check("запись лора добавилась", len(rows) == 1 and rows[0]["keys"] == "пиво, бар")

    await menu.cb_lore_add(FakeCallback(f"m:loreadd:{CID}"), state)
    bad = FakeMessage("без разделителя")
    await menu.lore_entry_input(bad, state, bot)
    check("кривой формат не добавляет запись", len(await db.lore_list(CID)) == 1)
    check("и оставляет ввод открытым", await state.get_state() == menu.Input.lore_entry)
    await state.clear()

    await menu.cb_lore_del(FakeCallback(f"m:lored:{CID}:{rows[0]['id']}:0"))
    check("запись удалилась", await db.lore_count(CID) == 0)
    await db.lore_add(CID, "*", "Мир суров.", 1)
    await menu.cb_lore_clear(FakeCallback(f"m:loreclr:{CID}"))
    check("книга чистится целиком", await db.lore_count(CID) == 0)

    # --- заметки ---
    from slusha import history as store
    await store.summary_set(CID, "Вася любит пиво.", 5)
    text, kb = await menu.view_notes(CID)
    check("экран заметок показывает их", "Вася любит пиво" in text)
    await menu.cb_notes_clear(FakeCallback(f"m:sumclr:{CID}"))
    check("кнопка очистки стирает заметки",
          (await store.summary_get(CID))[0] == "")

    # --- забыть переписку ---
    await ai.remember(CID, "@vasya", "что-то было", 1)
    cb = FakeCallback(f"m:forget:{CID}")
    await menu.cb_forget(cb)
    check("переписка забыта", await ai.history(CID, 10) == [])
    check("и об этом сказали", any("Забыто" in a for a in cb.answers))

    # --- доступ: только владелец ---
    cb = FakeCallback("m:acc", uid=STRANGER)
    await menu.cb_access(cb, state)
    check("посторонний в доступ не попадает",
          any("Нет доступа" in a for a in cb.answers))
    await menu.cb_access_add(FakeCallback("m:acca"), state)
    await menu.access_input(FakeMessage("@petya"), state, bot)
    check("добавление по @username работает",
          any(r["username"] == "petya" for r in await db.access_list()))
    await menu.cb_access_add(FakeCallback("m:acca"), state)
    await menu.access_input(FakeMessage("не id и не юзернейм"), state, bot)
    check("мусор в доступ не добавляется", len(await db.access_list()) == 1)
    await state.clear()
    row = (await db.access_list())[0]
    await menu.cb_access_del(FakeCallback(f"m:accd:{row['id']}"))
    check("удаление из доступа работает", await db.access_list() == [])

    # --- выход из чата ---
    await menu.cb_leave(FakeCallback(f"m:leave:{CID}"), bot)
    check("бот вышел", bot.left == [CID])
    check("чат помечен неактивным", (await db.get_chat(CID))["active"] == 0)

    await store.close()
    await db.close()
    print("\n" + ("ВСЁ ЗЕЛЁНОЕ" if not FAILS else "ПРОБЛЕМЫ:\n" + "\n".join(FAILS)))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
