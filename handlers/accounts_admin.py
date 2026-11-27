"""Админская заливка аккаунтов: номер с вводом кода кнопками и импорт файлов."""
import logging, os, re, secrets
from pathlib import Path
from aiogram import Router, F
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from telethon.errors import FloodWaitError
import bot as app
from services.code_login import CodeLoginError, LoginExpired, LoginNotFound, NotYourLogin, NeedPassword
from services.session_import import SessionImporter, SessionImportError
from services.lease import NotYourAccount
from services.crypto import SessionCryptoError, encrypt

log = logging.getLogger(__name__)
router = Router()
CODE_LEN = 5
STATUS_RU = {"free": "свободен", "leased": "арендован", "disabled": "отключён", "dead": "мёртв"}


class AddFlow(StatesGroup):
    phone = State()
    code = State()
    password = State()
    label = State()
    note = State()
    twofa = State()
    import_file = State()


def mask(phone):
    if not phone:
        return "нет номера"
    digits = re.sub(r"\D", "", phone)
    return f"+{digits[:2]}***{digits[-2:]}" if len(digits) > 5 else "+" + digits


def keypad(buf):
    rows = []
    for group in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")):
        rows.append([(d, f"acc_code:d:{d}") for d in group])
    rows.append([("⌫", "acc_code:del"), ("0", "acc_code:d:0"), ("✅", "acc_code:ok")])
    rows.append([("🔄 Код заново", "acc_code:resend"), ("❌ Отмена", "acc_code:cancel")])
    return app.kb(rows)


async def guard(c):
    if not await app.require_admin(c):
        await c.answer("Нет доступа", show_alert=True)
        return False
    if not app.ACCOUNTS_READY:
        await c.answer("Аккаунты не настроены: задайте SESSION_ENC_KEY, API_ID, API_HASH", show_alert=True)
        return False
    return True


async def stock_text():
    stats = await app.db.account_stats()
    return ("Склад аккаунтов\n"
            f"❄️ Обычные: свободно {stats['ordinary']['free']}, в аренде {stats['ordinary']['leased']}\n"
            f"🌟 Премиум: свободно {stats['premium']['free']}, в аренде {stats['premium']['leased']}\n"
            f"Мёртвые: {stats['ordinary']['dead'] + stats['premium']['dead']}, "
            f"отключённые: {stats['ordinary']['disabled'] + stats['premium']['disabled']}")


def stock_kb():
    return app.kb([[("➕ По номеру", "acc_add")], [("📎 Импорт файла", "acc_imp")],
                   [("📋 Список аккаунтов", "acc_list:0")], [("Назад ↩️", "admin_home")]])


@router.callback_query(F.data == "acc_menu")
async def menu(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.clear()
    await app.edit_panel(c, await stock_text(), reply_markup=stock_kb())
    await c.answer()


@router.callback_query(F.data.startswith("acc_list:"))
async def account_list(c: CallbackQuery):
    if not await guard(c):
        return
    offset = int(c.data.split(":")[1])
    rows = await app.db.all("SELECT * FROM accounts ORDER BY id DESC LIMIT 10 OFFSET ?", (offset,))
    if not rows:
        return await c.answer("Больше аккаунтов нет", show_alert=True)
    buttons = [[(f"#{r['id']} {r['label'][:24]} · {STATUS_RU.get(r['status'], r['status'])}", f"acc_ls:{r['id']}")]
               for r in rows]
    buttons.append([("Дальше ➡️", f"acc_list:{offset + 10}"), ("Назад ↩️", "acc_menu")])
    await app.edit_panel(c, "Аккаунты:", reply_markup=app.kb(buttons))
    await c.answer()


@router.callback_query(F.data == "acc_add")
async def add_phone(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.set_state(AddFlow.phone)
    await app.edit_panel(c, "Отправьте номер в формате +79991234567.\nВремя на ввод кода — 10 минут.")
    await c.answer()


@router.message(AddFlow.phone)
async def phone_entered(m: Message, state: FSMContext):
    if not await app.db.is_admin(m.from_user.id, app.OWNER_ID):
        return
    digits = re.sub(r"\D", "", m.text or "")
    if not 10 <= len(digits) <= 15:
        return await m.answer("Нужен номер из 10–15 цифр, например +79991234567.")
    phone = "+" + digits
    try:
        login_id, note, _timeout = await app.code_login.start(m.from_user.id, phone)
    except FloodWaitError as exc:
        return await m.answer(f"Telegram просит подождать {exc.seconds} сек. и повторить.")
    except CodeLoginError as exc:
        return await m.answer(f"Не удалось отправить код: {exc}")
    await state.set_state(AddFlow.code)
    await state.update_data(login_id=login_id, buf="")
    await m.answer(f"Код для {phone} отправлен.\n{note}.\n\nВведите код кнопками ниже.",
                   reply_markup=keypad(""))


async def current_login(c, state):
    data = await state.get_data()
    login_id = data.get("login_id")
    if not login_id:
        await c.answer("Вход не найден, начните заново", show_alert=True)
        return None
    return login_id


async def redraw(c, state, text=None, error=None):
    data = await state.get_data()
    buf = data.get("buf", "")
    body = text or f"Введите код ({len(buf)}/{CODE_LEN}): {' '.join(buf) or '· · · · ·'}"
    if error:
        body = f"{error}\n\n{body}"
    await app.edit_panel(c, body, reply_markup=keypad(buf))


async def submit(c, state):
    login_id = await current_login(c, state)
    if not login_id:
        return
    data = await state.get_data()
    code = data.get("buf", "")
    if len(code) < CODE_LEN:
        return await redraw(c, state, error=f"Код из {CODE_LEN} цифр, введено {len(code)}.")
    try:
        acc_id = await app.code_login.submit_code(login_id, c.from_user.id, code)
    except NeedPassword:
        await state.set_state(AddFlow.password)
        await app.edit_panel(c, "На аккаунте 2FA. Отправьте пароль сообщением.")
        return
    except LoginExpired as exc:
        await state.clear()
        await app.edit_panel(c, f"{exc}\n\nНачните заливку заново.",
                             reply_markup=app.kb([[("К складу", "acc_menu")]]))
        return
    except (CodeLoginError, LoginNotFound, NotYourLogin) as exc:
        return await redraw(c, state, error=str(exc))
    await state.clear()
    await app.db.audit(c.from_user.id, "account_added", acc_id, "phone")
    await app.edit_panel(c, f"Аккаунт #{acc_id} залит и добавлен на склад.",
                         reply_markup=app.kb([[("Карточка", f"acc_ls:{acc_id}")], [("К складу", "acc_menu")]]))


@router.callback_query(F.data.startswith("acc_code:"))
async def code_button(c: CallbackQuery, state: FSMContext):
    login_id = await current_login(c, state)
    if not login_id:
        return
    parts = c.data.split(":")
    action = parts[1]
    if action == "cancel":
        await app.code_login.cancel(login_id)
        await state.clear()
        await app.edit_panel(c, "Заливка отменена.", reply_markup=app.kb([[("К складу", "acc_menu")]]))
        return await c.answer()
    if action == "resend":
        try:
            note = await app.code_login.resend(login_id, c.from_user.id)
        except FloodWaitError as exc:
            return await c.answer(f"Подождите {exc.seconds} сек.", show_alert=True)
        except (CodeLoginError, LoginNotFound, NotYourLogin) as exc:
            return await c.answer(str(exc), show_alert=True)
        await state.update_data(buf="")
        await redraw(c, state, text=f"Код отправлен заново. {note}.")
        return await c.answer("Отправлено")
    data = await state.get_data()
    buf = data.get("buf", "")
    if action == "del":
        await state.update_data(buf=buf[:-1])
        return await redraw(c, state)
    if action == "d":
        if len(buf) >= CODE_LEN:
            buf = ""
        await state.update_data(buf=buf + parts[2])
        if len(buf) + 1 >= CODE_LEN:
            return await submit(c, state)
        return await redraw(c, state)
    if action == "ok":
        return await submit(c, state)
    await c.answer()


@router.message(AddFlow.password)
async def password_entered(m: Message, state: FSMContext):
    if not await app.db.is_admin(m.from_user.id, app.OWNER_ID):
        return
    data = await state.get_data()
    login_id = data.get("login_id")
    if not login_id:
        await state.clear()
        return await m.answer("Вход не найден. Начните заново.")
    try:
        acc_id = await app.code_login.submit_password(login_id, m.from_user.id, (m.text or "").strip())
    except (CodeLoginError, LoginNotFound, NotYourLogin, LoginExpired) as exc:
        return await m.answer(f"{exc}\n\nОтправьте пароль ещё раз или нажмите /cancel.")
    await state.clear()
    await app.db.audit(m.from_user.id, "account_added", acc_id, "phone+2fa")
    await m.answer(f"Аккаунт #{acc_id} залит.",
                   reply_markup=app.kb([[("Карточка", f"acc_ls:{acc_id}")], [("К складу", "acc_menu")]]))


@router.callback_query(F.data == "acc_imp")
async def import_start(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.set_state(AddFlow.import_file)
    await app.edit_panel(c, "Пришлите файл .session (Telethon/Pyrogram) или .json (session_string, "
                            "либо auth_key + dc_id). До 5 МБ.\nПодпись к файлу станет меткой аккаунта.")
    await c.answer()


def tmp_dir():
    return Path(os.getenv("DATABASE_PATH", "warden.db")).expanduser().resolve().parent / "tmp"


@router.message(AddFlow.import_file, F.document)
async def import_file(m: Message, state: FSMContext):
    if not await app.db.is_admin(m.from_user.id, app.OWNER_ID):
        return
    name = (m.document.file_name or "").lower()
    if not name.endswith((".session", ".json")):
        return await m.answer("Нужен файл .session или .json.")
    if m.document.file_size and m.document.file_size > 5 * 1024 * 1024:
        return await m.answer("Файл больше 5 МБ.")
    directory = tmp_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{secrets.token_hex(8)}_{Path(name).name}"
    label = (m.caption or "").strip()[:60] or None
    importer = SessionImporter(app.db, app.client_pool_service, m.from_user.id)
    try:
        await app.bot.download(m.document, destination=path)
        if name.endswith(".session"):
            acc_id = await importer.import_session_file(path, label)
        else:
            acc_id = await importer.import_json(path, label)
    except (SessionImportError, SessionCryptoError) as exc:
        return await m.answer(f"Импорт не удался: {exc}")
    except Exception as exc:
        log.exception("Ошибка импорта сессии")
        return await m.answer(f"Импорт не удался: {exc}")
    finally:
        path.unlink(missing_ok=True)
    await state.clear()
    await app.db.audit(m.from_user.id, "account_imported", acc_id, name)
    await m.answer(f"Аккаунт #{acc_id} импортирован.",
                   reply_markup=app.kb([[("Карточка", f"acc_ls:{acc_id}")], [("К складу", "acc_menu")]]))


@router.message(AddFlow.import_file)
async def import_no_file(m: Message):
    await m.answer("Пришлите именно файл .session или .json.")


async def card(acc_id):
    acc = await app.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return None, None
    lease = ""
    if acc["status"] == "leased":
        lease = f"\nАрендатор: {acc['leased_by']}\nДо: {acc['lease_until']}"
    text = (f"Аккаунт #{acc['id']}\nМетка: {acc['label']}\nКласс: {acc['account_class']}\n"
            f"Статус: {STATUS_RU.get(acc['status'], acc['status'])}\nТелефон: {mask(acc['phone'])}\n"
            f"@{acc['tg_username'] or 'нет'}\ntg_user_id: {acc['tg_user_id'] or 'нет'}\n"
            f"Тип клиента: {acc['client_type']}\n2FA в базе: {'да' if acc['twofa_enc'] else 'нет'}, "
            f"выдаётся: {'да' if acc['twofa_shared'] else 'нет'}{lease}")
    buttons = [
        [("🏷 Метка", f"acc_lbl:{acc_id}"), ("📝 Заметка", f"acc_note:{acc_id}")],
        [("🔑 2FA", f"acc_2fa:{acc_id}"), ("🔓 Показ 2FA", f"acc_share:{acc_id}")],
        [("🔁 Класс", f"acc_cls:{acc_id}"), ("🩺 Проверить", f"acc_chk:{acc_id}")],
        [("📜 События", f"acc_ev:{acc_id}"), ("🆓 Освободить", f"acc_free:{acc_id}")],
        [("📤 Убрать из выдачи" if acc["available_for_issue"] else "📥 Вернуть в выдачу", f"acc_issue:{acc_id}")],
        [("⛔ Отключить" if acc["status"] != "disabled" else "✅ Включить", f"acc_dis:{acc_id}")],
        [("🗑 Удалить", f"acc_del:{acc_id}")],
        [("Назад ↩️", "acc_menu")],
    ]
    return text, app.kb(buttons)


@router.callback_query(F.data.startswith("acc_ls:"))
async def card_view(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.clear()
    text, markup = await card(int(c.data.split(":")[1]))
    if not text:
        return await c.answer("Аккаунт не найден", show_alert=True)
    await app.edit_panel(c, text, reply_markup=markup)
    await c.answer()


@router.callback_query(F.data.startswith("acc_ev:"))
async def events(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    rows = await app.db.all("SELECT * FROM account_events WHERE account_id=? ORDER BY id DESC LIMIT 20", (acc_id,))
    if not rows:
        return await c.answer("Событий нет", show_alert=True)
    body = "\n".join(f"{r['created_at'][:16]} {r['action']} {r['user_id'] or ''} {r['detail'] or ''}".strip()
                     for r in rows)
    await app.edit_panel(c, f"События аккаунта #{acc_id}:\n{body}",
                         reply_markup=app.kb([[("Назад ↩️", f"acc_ls:{acc_id}")]]))
    await c.answer()


@router.callback_query(F.data.startswith("acc_lbl:"))
async def label_start(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.set_state(AddFlow.label)
    await state.update_data(acc_id=int(c.data.split(":")[1]))
    await app.edit_panel(c, "Новая метка (до 60 символов):")
    await c.answer()


@router.message(AddFlow.label)
async def label_save(m: Message, state: FSMContext):
    if not await app.db.is_admin(m.from_user.id, app.OWNER_ID):
        return
    label = (m.text or "").strip()
    if not 1 <= len(label) <= 60:
        return await m.answer("От 1 до 60 символов.")
    acc_id = (await state.get_data())["acc_id"]
    await app.db.run("UPDATE accounts SET label=? WHERE id=?", (label, acc_id))
    await state.clear()
    await m.answer("Метка обновлена.", reply_markup=app.kb([[("Карточка", f"acc_ls:{acc_id}")]]))


@router.callback_query(F.data.startswith("acc_note:"))
async def note_start(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.set_state(AddFlow.note)
    await state.update_data(acc_id=int(c.data.split(":")[1]))
    await app.edit_panel(c, "Заметка для администраторов (до 500 символов):")
    await c.answer()


@router.message(AddFlow.note)
async def note_save(m: Message, state: FSMContext):
    if not await app.db.is_admin(m.from_user.id, app.OWNER_ID):
        return
    acc_id = (await state.get_data())["acc_id"]
    await app.db.run("UPDATE accounts SET note=? WHERE id=?", ((m.text or "")[:500], acc_id))
    await state.clear()
    await m.answer("Заметка сохранена.", reply_markup=app.kb([[("Карточка", f"acc_ls:{acc_id}")]]))


@router.callback_query(F.data.startswith("acc_2fa:"))
async def twofa_start(c: CallbackQuery, state: FSMContext):
    if not await guard(c):
        return
    await state.set_state(AddFlow.twofa)
    await state.update_data(acc_id=int(c.data.split(":")[1]))
    await app.edit_panel(c, "Отправьте пароль 2FA аккаунта сообщением (до 100 символов):")
    await c.answer()


@router.message(AddFlow.twofa)
async def twofa_save(m: Message, state: FSMContext):
    if not await app.db.is_admin(m.from_user.id, app.OWNER_ID):
        return
    password = (m.text or "").strip()
    if not password:
        return await m.answer("Пароль пустой. Отправьте пароль или /cancel.")
    acc_id = (await state.get_data())["acc_id"]
    await app.db.run("UPDATE accounts SET twofa_enc=? WHERE id=?", (encrypt(password[:100]), acc_id))
    await state.clear()
    await m.answer("Пароль 2FA сохранён в зашифрованном виде.",
                   reply_markup=app.kb([[("Карточка", f"acc_ls:{acc_id}")]]))


@router.callback_query(F.data.startswith("acc_share:"))
async def share_toggle(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    acc = await app.db.one("SELECT twofa_enc,twofa_shared FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return await c.answer("Аккаунт не найден", show_alert=True)
    if not acc["twofa_enc"]:
        return await c.answer("Сначала сохраните пароль 2FA", show_alert=True)
    await app.db.run("UPDATE accounts SET twofa_shared=? WHERE id=?",
                     (0 if acc["twofa_shared"] else 1, acc_id))
    text, markup = await card(acc_id)
    await app.edit_panel(c, text, reply_markup=markup)
    await c.answer("Переключено")


@router.callback_query(F.data.startswith("acc_cls:"))
async def class_toggle(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    acc = await app.db.one("SELECT account_class,status FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return await c.answer("Аккаунт не найден", show_alert=True)
    if acc["status"] == "leased":
        return await c.answer("Сначала освободите аккаунт", show_alert=True)
    new = "premium" if acc["account_class"] == "ordinary" else "ordinary"
    await app.db.run("UPDATE accounts SET account_class=? WHERE id=?", (new, acc_id))
    text, markup = await card(acc_id)
    await app.edit_panel(c, text, reply_markup=markup)
    await c.answer(new)


@router.callback_query(F.data.startswith("acc_chk:"))
async def check(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    acc = await app.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return await c.answer("Аккаунт не найден", show_alert=True)
    await c.answer("Проверяю…")
    try:
        client = await app.client_pool_service.get(acc_id)
        me = await client.get_me()
        if not me:
            raise RuntimeError("Telegram не вернул профиль")
        await app.db.run("UPDATE accounts SET last_check_at=?,check_failures=0,tg_username=? WHERE id=?",
                         (app.now(), getattr(me, "username", None), acc_id))
        await app.db.run("INSERT INTO account_events(account_id,user_id,action,created_at) VALUES(?,?,?,?)",
                         (acc_id, c.from_user.id, "check_ok", app.now()))
        result = "Аккаунт жив, профиль получен."
    except Exception as exc:
        await app.client_pool_service.disconnect(acc_id)
        await app.db.run("UPDATE accounts SET last_check_at=?,check_failures=check_failures+1 WHERE id=?",
                         (app.now(), acc_id))
        row = await app.db.one("SELECT check_failures FROM accounts WHERE id=?", (acc_id,))
        failures = row["check_failures"] if row else 3
        if failures >= 3:
            await app.db.run("UPDATE accounts SET status='dead',leased_by=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                             (acc_id,))
            await app.db.run("INSERT INTO account_events(account_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                             (acc_id, c.from_user.id, "dead", str(exc)[:300], app.now()))
            try:
                await app.bot.send_message(app.OWNER_ID, f"Аккаунт #{acc_id} помечен мёртвым: {exc}")
            except Exception:
                pass
        result = f"Проверка не прошла ({failures}/3): {exc}"
    await c.message.answer(result, reply_markup=app.kb([[("Карточка", f"acc_ls:{acc_id}")]]))


@router.callback_query(F.data.startswith("acc_free:"))
async def force_free(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    try:
        await app.lease_service.force_release(acc_id, c.from_user.id)
    except NotYourAccount as exc:
        return await c.answer(str(exc), show_alert=True)
    await app.db.audit(c.from_user.id, "account_force_release", acc_id)
    text, markup = await card(acc_id)
    await app.edit_panel(c, text, reply_markup=markup)
    await c.answer("Освобождён")


@router.callback_query(F.data.startswith("acc_issue:"))
async def issue_toggle(c: CallbackQuery):
    if not await guard(c): return
    acc_id=int(c.data.split(":")[1])
    row=await app.db.one("SELECT available_for_issue FROM accounts WHERE id=?",(acc_id,))
    if not row: return await c.answer("Аккаунт не найден",show_alert=True)
    new_value=0 if row["available_for_issue"] else 1
    await app.db.run("UPDATE accounts SET available_for_issue=? WHERE id=?",(new_value,acc_id))
    await app.db.audit(c.from_user.id,"account_issue_visibility",acc_id,str(new_value))
    text,markup=await card(acc_id)
    await app.edit_panel(c,text,reply_markup=markup)
    await c.answer("Аккаунт убран из выдачи" if not new_value else "Аккаунт возвращён в выдачу")


@router.callback_query(F.data.startswith("acc_dis:"))
async def disable_toggle(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    acc = await app.db.one("SELECT status FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return await c.answer("Аккаунт не найден", show_alert=True)
    if acc["status"] == "disabled":
        await app.db.run("UPDATE accounts SET status='free' WHERE id=?", (acc_id,))
        action = "enabled"
    else:
        if acc["status"] == "leased":
            try:
                await app.lease_service.force_release(acc_id, c.from_user.id)
            except NotYourAccount:
                pass
        await app.db.run("UPDATE accounts SET status='disabled',leased_by=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                         (acc_id,))
        action = "disabled"
    await app.db.run("INSERT INTO account_events(account_id,user_id,action,created_at) VALUES(?,?,?,?)",
                     (acc_id, c.from_user.id, action, app.now()))
    await app.db.audit(c.from_user.id, "account_disable_toggle", acc_id)
    text, markup = await card(acc_id)
    await app.edit_panel(c, text, reply_markup=markup)
    await c.answer("Готово")


@router.callback_query(F.data.startswith("acc_del:"))
async def delete_ask(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    await app.edit_panel(c, f"Удалить аккаунт #{acc_id} из базы? Сессия будет потеряна.",
                         reply_markup=app.kb([[("Да, удалить", f"acc_delok:{acc_id}")],
                                              [("Нет", f"acc_ls:{acc_id}")]]))
    await c.answer()


@router.callback_query(F.data.startswith("acc_delok:"))
async def delete_confirm(c: CallbackQuery):
    if not await guard(c):
        return
    acc_id = int(c.data.split(":")[1])
    await app.client_pool_service.disconnect(acc_id)
    await app.db.run("DELETE FROM accounts WHERE id=?", (acc_id,))
    await app.db.audit(c.from_user.id, "account_deleted", acc_id)
    await app.edit_panel(c, f"Аккаунт #{acc_id} удалён.", reply_markup=app.kb([[("К складу", "acc_menu")]]))
    await c.answer()
