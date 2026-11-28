"""Каталог аккаунтов и панель аренды для участников."""
import asyncio, logging, re
from math import ceil
from datetime import datetime, timedelta, timezone
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from telethon.errors import FloodWaitError
import bot as app
from services.lease import (AccountBusy, AccountLimit, AccountUnavailable, NotYourAccount,
                            LeaseExpired, LeaseLocked)
from services.crypto import decrypt, SessionCryptoError

log = logging.getLogger(__name__)
router = Router()
CLASS_RU = {"ordinary": "❄️ Обычный", "premium": "🌟 Премиум"}
NEED_ROLE = {"ordinary": "Трафер", "premium": "Опытный"}


def mask(phone):
    digits = re.sub(r"\D", "", phone or "")
    return f"+{digits[:2]}***{digits[-2:]}" if len(digits) > 5 else "+" + (digits or "—")


def remaining(iso):
    try:
        left = datetime.fromisoformat(iso) - datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return "?"
    return f"{max(0, int(left.total_seconds() // 60))} мин."


async def role_ok(user, cls):
    need = NEED_ROLE[cls]
    if app.ROLE_LEVEL.get(user["role"], 0) < app.ROLE_LEVEL[need]:
        return f"Доступно с должности «{need}»."
    return None


async def render_catalog(c: CallbackQuery):
    user = await app.user_row(c.from_user.id)
    if not user:
        return await c.answer("Сначала нажмите /start", show_alert=True)
    if not app.ACCOUNTS_READY:
        await app.edit_panel(c, "Раздел аккаунтов ещё не настроен администрацией.",
                             reply_markup=app.kb([[("Назад ↩️", "home")]]))
        return await c.answer()
    visible = await app.db.all("SELECT account_class,COUNT(*) n FROM accounts WHERE status='free' AND available_for_issue=1 GROUP BY account_class")
    visible_counts = {row["account_class"]: row["n"] for row in visible}
    stats = {"ordinary": {"free": visible_counts.get("ordinary", 0)}, "premium": {"free": visible_counts.get("premium", 0)}}
    leases = await app.db.active_leases(c.from_user.id)
    mine = ""
    if leases:
        mine = "\n\nВаши аренды:\n" + "\n".join(
            f"#{a['id']} {CLASS_RU[a['account_class']]} · {mask(a['phone'])}" for a in leases)
    text = ("Аккаунты для работы.\n"
            f"❄️ Обычные свободны: {stats['ordinary']['free']}\n"
            f"🌟 Премиум свободны: {stats['premium']['free']}\n\n"
            "Правило: один обычный и один премиум на человека. "
            "Сессия остаётся на сервере, файл наружу не отдаётся." + mine)
    buttons = [[(f"Открыть #{a['id']}", f"uacc_panel:{a['id']}")] for a in leases]
    buttons.append([("❄️ Обычные", "uacc_list:ordinary"), ("🌟 Премиум", "uacc_list:premium")])
    buttons.append([("Назад ↩️", "home")])
    await app.edit_panel(c, text, reply_markup=app.kb(buttons))
    await c.answer()


@router.callback_query(F.data.startswith("uacc_list:"))
async def listing(c: CallbackQuery):
    cls = c.data.split(":")[1]
    if cls not in CLASS_RU:
        return await c.answer("Неверный тип", show_alert=True)
    user = await app.user_row(c.from_user.id)
    if not user:
        return await c.answer("Сначала /start", show_alert=True)
    problem = await role_ok(user, cls)
    if problem:
        await app.edit_panel(c, problem, reply_markup=app.kb([[("Назад ↩️","free_accounts")]]))
        return await c.answer()
    rows = await app.db.all(
        "SELECT id,label,phone FROM accounts WHERE account_class=? AND status='free' AND available_for_issue=1 ORDER BY id LIMIT 10", (cls,))
    if not rows:
        return await c.answer("Свободных аккаунтов этого типа нет", show_alert=True)
    buttons = [[(f"#{r['id']} {mask(r['phone'])} · {r['label'][:20]}", f"uacc_take:{r['id']}")] for r in rows]
    buttons.append([("Назад ↩️", "free_accounts")])
    await app.edit_panel(c, f"{CLASS_RU[cls]} — выберите аккаунт:", reply_markup=app.kb(buttons))
    await c.answer()


@router.callback_query(F.data.startswith("uacc_take:"))
async def take(c: CallbackQuery):
    acc_id = int(c.data.split(":")[1])
    user = await app.user_row(c.from_user.id)
    acc = await app.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not user or not acc or not acc["available_for_issue"]:
        return await c.answer("Аккаунт больше не доступен для выдачи", show_alert=True)
    problem = await role_ok(user, acc["account_class"])
    if problem:
        return await c.answer(problem, show_alert=True)
    try:
        await app.lease_service.take_for_class(acc_id, c.from_user.id, acc["account_class"])
    except (AccountBusy, AccountLimit, AccountUnavailable) as exc:
        return await c.answer(str(exc), show_alert=True)
    await app.db.audit(c.from_user.id, "account_taken", acc_id)
    await app.notify_admins("взял аккаунт", c.from_user.id,
                            f"Аккаунт #{acc_id} · {CLASS_RU[acc['account_class']]} · {mask(acc['phone'])}")
    await c.answer("Аккаунт ваш")
    await panel_edit(c, acc_id, c.from_user.id)


async def lease_guard(c, acc_id):
    try:
        return await app.lease_service.guard_lease(acc_id, c.from_user.id)
    except NotYourAccount as exc:
        await c.answer(str(exc), show_alert=True)
    except LeaseExpired as exc:
        await c.answer(str(exc), show_alert=True)
    return None


def panel_text(acc):
    left = app.lease_service.lock_left(acc["leased_at"]) if acc["leased_at"] else 0
    text = (f"Аккаунт #{acc['id']} · {CLASS_RU[acc['account_class']]}\n"
            f"Телефон: {acc['phone'] or 'нет'}\n@{acc['tg_username'] or 'нет'}\n"
            f"Осталось аренды: {remaining(acc['lease_until'])}\n")
    if left:
        until = datetime.fromisoformat(acc["leased_at"]) + timedelta(minutes=app.lease_service.lock_min)
        text += f"🔒 Закреплён за вами до {until:%d.%m %H:%M} (ещё {ceil(left / 60)} ч.), скинуть нельзя\n"
    else:
        text += "🔓 Закрепление истекло — аккаунт можно освободить\n"
    if acc["session_lost"]:
        text += "⚠️ Серверная сессия потеряна, зайдите заново по коду\n"
    text += "\nВход: «🔑 Код входа» → введите номер на своём устройстве → код придёт сюда."
    if acc["note"]:
        text += f"\n\nЗаметка: {acc['note'][:300]}"
    return text


def panel_kb(acc_id):
    return app.kb([
        [("🔑 Код входа", f"uacc_code:{acc_id}")],
        [("⏱ Продлить", f"uacc_ext:{acc_id}")],
        [("🚪 Освободить", f"uacc_rel:{acc_id}")],
        [("Назад ↩️", "free_accounts")],
    ])


async def panel_edit(c, acc_id, user_id):
    acc = await app.db.one("SELECT * FROM accounts WHERE id=? AND leased_by=?", (acc_id, user_id))
    if not acc:
        await c.message.answer("Аккаунт вам не принадлежит.")
        return
    await app.edit_panel(c, panel_text(acc), reply_markup=panel_kb(acc_id))


@router.callback_query(F.data.startswith("uacc_panel:"))
async def panel(c: CallbackQuery, state: FSMContext):
    acc_id = int(c.data.split(":")[1])
    await state.clear()
    if not await lease_guard(c, acc_id):
        return
    await panel_edit(c, acc_id, c.from_user.id)
    await c.answer()


@router.callback_query(F.data.startswith("uacc_ext:"))
async def extend(c: CallbackQuery):
    acc_id = int(c.data.split(":")[1])
    if not await lease_guard(c, acc_id):
        return
    try:
        await app.lease_service.renew(acc_id, c.from_user.id)
    except LeaseExpired as exc:
        return await c.answer(str(exc), show_alert=True)
    await c.answer("Аренда продлена")
    await panel_edit(c, acc_id, c.from_user.id)


@router.callback_query(F.data.startswith("uacc_rel:"))
async def release(c: CallbackQuery, state: FSMContext):
    acc_id = int(c.data.split(":")[1])
    if not await lease_guard(c, acc_id):
        return
    await state.clear()
    try:
        await app.lease_service.release(acc_id, c.from_user.id)
    except LeaseLocked as exc:
        return await c.answer(str(exc), show_alert=True)
    except NotYourAccount as exc:
        return await c.answer(str(exc), show_alert=True)
    await app.db.audit(c.from_user.id, "account_released", acc_id)
    await app.edit_panel(c, "Аккаунт освобождён.", reply_markup=app.kb([[("К каталогу", "free_accounts")]]))
    await c.answer()


@router.callback_query(F.data.startswith("uacc_code:"))
async def login_code(c: CallbackQuery):
    acc_id = int(c.data.split(":")[1])
    if app.LOGIN_MODE != "code_login":
        return await c.answer("Выдача кода отключена: нужен ACCOUNT_LOGIN_MODE=code_login", show_alert=True)
    if not await lease_guard(c, acc_id):
        return
    acc = await app.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc or not acc["phone"]:
        return await c.answer("У аккаунта нет номера", show_alert=True)
    if acc["twofa_enc"] and not acc["twofa_shared"]:
        return await c.answer("Пароль 2FA у администрации — попросите его у админа.", show_alert=True)
    await c.answer("Проверяю сессию и запрашиваю код…")
    try:
        client = await app.client_pool_service.get(acc_id)
        await client.get_me()
    except Exception as exc:
        await app.db.run("UPDATE accounts SET session_lost=1 WHERE id=?", (acc_id,))
        await app.alert_session_lost(acc_id, c.from_user.id)
        return await c.message.answer(f"Серверная сессия не отвечает ({exc}). Администрация увидит потерю, попробуйте позже.",
                                      reply_markup=app.kb([[("В панель", f"uacc_panel:{acc_id}")]]))
    try:
        if hasattr(client, "send_code_request"):
            await client.send_code_request(acc["phone"])
        else:
            await client.send_code(acc["phone"])
    except FloodWaitError as exc:
        return await c.message.answer(f"Telegram просит подождать {exc.seconds} сек.")
    except Exception as exc:
        return await c.message.answer(f"Код не запрошен: {exc}")
    code = await wait_code(client)
    body = (f"Код входа для #{acc_id}: {code}" if code
            else "Код не пришёл за 25 секунд. Проверьте сервисные сообщения на аккаунте.")
    if acc["twofa_enc"] and acc["twofa_shared"]:
        try:
            body += f"\nПароль 2FA: {decrypt(acc['twofa_enc'])}"
        except SessionCryptoError:
            body += "\nПароль 2FA сохранён, но не расшифровывается."
    body += "\n\nНе нажимайте «Завершить все другие сессии» — так аккаунт останется в складе."
    await c.message.answer(body, reply_markup=app.kb([[("В панель", f"uacc_panel:{acc_id}")]]))
    asyncio.create_task(watch_after_login(acc_id, c.from_user.id))


async def watch_after_login(acc_id, user_id):
    """Три минуты после выдачи кода следим, что серверную сессию не скинули."""
    for _ in range(4):
        await asyncio.sleep(45)
        try:
            client = await app.client_pool_service.get(acc_id)
            alive = bool(await client.get_me())
        except Exception:
            alive = False
        if alive:
            await app.db.run("UPDATE accounts SET session_lost=0,last_check_at=? WHERE id=?",
                             (app.now(), acc_id))
            continue
        await app.db.run("UPDATE accounts SET session_lost=1 WHERE id=?", (acc_id,))
        await app.client_pool_service.disconnect(acc_id)
        await app.db.run("INSERT INTO account_events(account_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                         (acc_id, user_id, "session_lost", "сессия скинута после выдачи кода", app.now()))
        try:
            await app.alert_session_lost(acc_id, user_id)
        except Exception:
            log.exception("Не удалось уведомить о потере серверной сессии")
        return


async def wait_code(client):
    """Read the newest Telegram service message for either Telethon or Pyrogram."""
    is_telethon = hasattr(client, "iter_messages")
    try:
        if is_telethon:
            recent = await client.get_messages(777000, limit=1)
            last = recent[0].id if recent else 0
        else:
            recent = []
            async for message in client.get_chat_history(777000, limit=1):
                recent.append(message)
            last = recent[0].id if recent else 0
    except Exception:
        log.exception("Не удалось прочитать сервисный чат Telegram")
        return None

    for _ in range(12):
        await asyncio.sleep(2)
        try:
            if is_telethon:
                messages = await client.get_messages(777000, limit=5, min_id=last)
            else:
                messages = []
                async for message in client.get_chat_history(777000, limit=5):
                    if message.id > last:
                        messages.append(message)
        except Exception:
            continue
        for msg in messages:
            message_text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
            found = re.search(r"(?<!\d)(\d{5,6})(?!\d)", message_text)
            if found:
                return found.group(1)
    return None
