/* Панель Слюши.
 *
 * Одна страница, два экрана: список чатов и карточка чата. Ни фреймворка, ни
 * сборки — весь смысл панели в том, что характер видно целиком и лорбук
 * правится без диалога с ботом, а для этого хватает разметки и fetch.
 *
 * Состояния на клиенте не держим: после каждого действия перечитываем чат
 * с сервера. Панель открыта в одном окне, запросов единицы — зато никогда
 * не разъезжается с тем, что реально лежит в базе.
 */
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const app = document.getElementById('app');
const INIT = (tg && tg.initData) || '';

async function api(path, opts = {}) {
  const headers = Object.assign({'X-Init-Data': INIT}, opts.headers || {});
  if (opts.body && !(opts.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, Object.assign({}, opts, {headers}));
  const data = await resp.json().catch(() => ({error: 'Сервер ответил не JSON'}));
  if (!resp.ok) throw new Error(data.error || resp.status);
  return data;
}

function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// Node.append() ничего не возвращает, поэтому строку в одно выражение тут
// не собрать: заводим ряд отдельно и складываем в него детей.
function row(parent, ...kids) {
  const r = el('<div class="row"></div>');
  r.append(...kids);
  parent.append(r);
  return r;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

function fail(e) {
  app.prepend(el(`<p class="err">${esc(e.message || e)}</p>`));
}

function confirmed(text) {
  // родного confirm() в WebView Telegram лучше избегать: на части клиентов он
  // просто не показывается, и кнопка молча ничего не делает
  return new Promise(resolve => {
    if (tg && tg.showConfirm) tg.showConfirm(text, ok => resolve(!!ok));
    else resolve(window.confirm(text));
  });
}

let STATE = null;

async function screenChats() {
  STATE = await api('/api/state');
  app.innerHTML = '';
  app.append(el(`<h1>🧠 Слюша</h1>`));
  app.append(el(`<p class="muted">Мозги: ${esc(STATE.provider)}${STATE.ready ? '' : ' · не настроены'}</p>`));
  if (!STATE.chats.length) {
    app.append(el(`<p class="muted">Пока пусто. Добавьте бота в свой чат.</p>`));
    return;
  }
  for (const c of STATE.chats) {
    const b = el(`<button class="chat-btn">${c.ai_on ? '✅' : '🚫'} ${esc(c.title || c.chat_id)}</button>`);
    b.onclick = () => screenChat(c.chat_id).catch(fail);
    app.append(b);
  }
}

async function screenChat(cid) {
  const d = await api(`/api/chat/${cid}`);
  const reload = () => screenChat(cid).catch(fail);
  app.innerHTML = '';

  const back = el(`<button class="ghost">← к чатам</button>`);
  back.onclick = () => screenChats().catch(fail);
  app.append(back);
  app.append(el(`<h1>${esc(d.title)}</h1>`));
  app.append(el(`<p class="muted">Сегодня ответов: ${d.spent}${d.capped ? '' : ' · без лимита'}</p>`));

  // ---- переключатели: описания полей пришли с сервера ----
  const box = el(`<div class="card"></div>`);
  for (const f of STATE.fields) {
    const val = d.settings[f.key];
    const line = el(`<div class="row"><span class="grow">${esc(f.label)}</span></div>`);
    if (f.kind === 'toggle') {
      const b = el(`<button class="${val ? '' : 'ghost'}">${val ? 'включено' : 'выключено'}</button>`);
      b.onclick = async () => {
        try { await api(`/api/chat/${cid}/set`, {method: 'POST', body: {key: f.key, value: !val}}); reload(); }
        catch (e) { fail(e); }
      };
      line.append(b);
    } else {
      const label = (f.labels && f.labels[String(val)]) || val;
      const b = el(`<button class="ghost">${esc(label)} ▸</button>`);
      b.onclick = async () => {
        const i = f.values.indexOf(val);
        const next = f.values[(i + 1) % f.values.length];
        try { await api(`/api/chat/${cid}/set`, {method: 'POST', body: {key: f.key, value: next}}); reload(); }
        catch (e) { fail(e); }
      };
      line.append(b);
    }
    box.append(line);
  }
  app.append(box);

  // ---- характер целиком, а не куском ----
  app.append(el(`<h2>🎭 Характер</h2>`));
  const persona = el(`<div class="card"></div>`);
  const area = el(`<textarea placeholder="${esc(d.persona_default)}"></textarea>`);
  area.value = d.persona;
  persona.append(area);
  const saveRow = el(`<div class="row"></div>`);
  // характер уезжает в модель на каждый запрос и делит окно с историей:
  // на пяти тысячах знаков вопрос собеседника в нём тонет
  const hint = n => n > d.persona_soft
    ? `${n} знаков — многовато, рабочий размер до ${d.persona_soft}`
    : `${n} знаков`;
  const counter = el(`<span class="muted grow">${hint(d.persona.length)}</span>`);
  area.oninput = () => { counter.textContent = hint(area.value.length); };
  const save = el(`<button>Сохранить</button>`);
  save.onclick = async () => {
    try {
      await api(`/api/chat/${cid}/persona`, {method: 'POST', body: {text: area.value}});
      counter.textContent = 'сохранено';
    } catch (e) { fail(e); }
  };
  saveRow.append(counter, save);
  persona.append(saveRow);
  app.append(persona);

  // ---- примеры реплик ----
  app.append(el(`<h2>💬 Примеры реплик</h2>`));
  const exBox = el(`<div class="card"></div>`);
  exBox.append(el(`<p class="muted">Как персонаж разговаривает — по строке на реплику, «ты:» и «собеседник:». Уходят в начало переписки образцом манеры; на небольших моделях действуют сильнее, чем описание словами.</p>`));
  const exArea = el(`<textarea placeholder="собеседник: почём яблоки?&#10;ты: дороже, чем вчера."></textarea>`);
  exArea.value = d.examples;
  exBox.append(exArea);
  const exSave = el(`<button>Сохранить</button>`);
  exSave.onclick = async () => {
    try { await api(`/api/chat/${cid}/examples`, {method: 'POST', body: {text: exArea.value}}); reload(); }
    catch (e) { fail(e); }
  };
  row(exBox, exSave);
  app.append(exBox);

  // ---- имена-обращения ----
  app.append(el(`<h2>🔔 Имена-обращения</h2>`));
  const names = el(`<div class="card"></div>`);
  const nameInput = el(`<input type="text" placeholder="слюша, слюш, холо*">`);
  nameInput.value = d.names;
  const nameSave = el(`<button>Сохранить</button>`);
  nameSave.onclick = async () => {
    try { await api(`/api/chat/${cid}/names`, {method: 'POST', body: {text: nameInput.value}}); reload(); }
    catch (e) { fail(e); }
  };
  names.append(el(`<p class="muted">Через запятую. Звёздочка на конце ловит падежи: «холо*» — это и «холой», и «холочка», но не «холодно».</p>`));
  row(names, nameInput);
  row(names, nameSave);
  app.append(names);

  // ---- заметки ----
  app.append(el(`<h2>🧠 Заметки о чате</h2>`));
  const notes = el(`<div class="card"></div>`);
  const when = d.notes_updated
    ? new Date(d.notes_updated * 1000).toLocaleString('ru-RU',
        {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'})
    : 'ни разу';
  notes.append(el(`<p class="muted">Обновлено: ${when} · новых сообщений с прошлой пересборки: ${d.notes_pending} · потолок ${d.notes_limit} знаков</p>`));
  notes.append(el(`<pre class="notes">${esc(d.notes || 'Пока пусто.')}</pre>`));
  if (d.notes) {
    const clr = el(`<button class="danger">Очистить заметки</button>`);
    clr.onclick = async () => {
      if (!await confirmed('Стереть заметки о чате?')) return;
      try { await api(`/api/chat/${cid}/notes/clear`, {method: 'POST'}); reload(); }
      catch (e) { fail(e); }
    };
    row(notes, clr);
  }
  app.append(notes);

  // ---- лорбук ----
  app.append(el(`<h2>📚 Лорбук (${d.lore.length})</h2>`));
  const lore = el(`<div class="card"></div>`);
  for (const r of d.lore) {
    const item = el(`<div class="lore-item">
        <div class="row">
          <span class="grow keys">${r.always ? '📌 всегда' : '🔑 ' + esc(r.keys || 'без ключей')}</span>
        </div>
        <div class="muted">${esc(r.content.slice(0, 200))}</div>
      </div>`);
    const del = el(`<button class="ghost danger">✕</button>`);
    del.onclick = async () => {
      try { await api(`/api/chat/${cid}/lore/${r.id}`, {method: 'DELETE'}); reload(); }
      catch (e) { fail(e); }
    };
    item.querySelector('.row').append(del);
    lore.append(item);
  }
  const keysIn = el(`<input type="text" placeholder="ключи через запятую, * — всегда">`);
  const textIn = el(`<textarea placeholder="текст записи"></textarea>`);
  const add = el(`<button>Добавить запись</button>`);
  add.onclick = async () => {
    try {
      await api(`/api/chat/${cid}/lore`, {method: 'POST', body: {keys: keysIn.value, content: textIn.value}});
      reload();
    } catch (e) { fail(e); }
  };
  const form = el(`<div class="lore-item"></div>`);
  form.append(keysIn, textIn);
  row(form, add);
  lore.append(form);
  app.append(lore);

  // ---- файл с chub.ai ----
  const file = el(`<div class="card"><p class="muted">Файл с chub.ai: книга лора или карточка персонажа (JSON или PNG).</p></div>`);
  const picker = el(`<input type="file" accept=".json,.png">`);
  picker.onchange = async () => {
    if (!picker.files.length) return;
    const fd = new FormData();
    fd.append('file', picker.files[0]);
    try { await api(`/api/chat/${cid}/upload`, {method: 'POST', body: fd}); reload(); }
    catch (e) { fail(e); }
  };
  file.append(picker);
  app.append(file);

  // ---- опасное — в самый низ ----
  const danger = el(`<div class="card"></div>`);
  const forget = el(`<button class="danger">🧹 Забыть переписку</button>`);
  forget.onclick = async () => {
    if (!await confirmed('Стереть переписку и заметки этого чата?')) return;
    try {
      const r = await api(`/api/chat/${cid}/forget`, {method: 'POST'});
      app.prepend(el(`<p class="ok">Забыто реплик: ${r.wiped}</p>`));
    } catch (e) { fail(e); }
  };
  const leave = el(`<button class="danger">🚪 Убрать бота из чата</button>`);
  leave.onclick = async () => {
    if (!await confirmed('Бот выйдет из чата. Точно?')) return;
    try { await api(`/api/chat/${cid}/leave`, {method: 'POST'}); screenChats(); }
    catch (e) { fail(e); }
  };
  row(danger, forget);
  row(danger, leave);
  app.append(danger);
}

screenChats().catch(e => {
  app.innerHTML = '';
  fail(e);
});
