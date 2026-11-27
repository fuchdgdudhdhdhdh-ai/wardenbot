"""Каталог аккаунтов и панель аренды для участников."""
import asyncio, logging, re
from datetime import datetime, timezone
from aiogram import Router, F
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from telethon.errors import FloodWaitError
import bot as app
from services.lease import AccountBusy, AccountLimit, AccountUnavailable, NotYourAccount, LeaseExpired
from services.crypto import decrypt, SessionCryptoError

log = logging.getLogger(__name__)
router = Router()
CLASS_RU = {"ordinary": "❄️ Обычный", "premium": "🌟 Премиум"}
NEED_ROLE = {"ordinary": "Трафер", "premium": "Опытный"}


class LeaseFlow(StatesGroup):
    send_target = State()
    send_text = State()
    join_link = State()


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
    stats = await app.db.account_stats()
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
        return await c.answer(problem, show_alert=True)
    rows = await app.db.all(
        "SELECT id,label,phone FROM accounts WHERE account_class=? AND status='free' ORDER BY id LIMIT 10", (cls,))
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
    if not user or not acc:
        return await c.answer("Аккаунт не найден", show_alert=True)
    problem = await role_ok(user, acc["account_class"])
    if problem:
        return await c.answer(problem, show_alert=True)
    try:
        await app.lease_service.take_for_class(acc_id, c.from_user.id, acc["account_class"])
    except (AccountBusy, AccountLimit, AccountUnavailable) as exc:
        return await c.answer(str(exc), show_alert=True)
    await app.db.audit(c.from_user.id, "account_taken", acc_id)
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
    text = (f"Аккаунт #{acc['id']} · {CLASS_RU[acc['account_class']]}\n"
            f"Телефон: {acc['phone'] or 'нет'}\n@{acc['tg_username'] or 'нет'}\n"
            f"Осталось аренды: {remaining(acc['lease_until'])}\n\n"
            "Сессия хранится на сервере: session-строку и api_id/api_hash не показываем.")
    if acc["note"]:
        text += f"\n\nЗаметка: {acc['note'][:300]}"
    return text


def panel_kb(acc_id):
    return app.kb([
        [("✉️ Написать", f"uacc_send:{acc_id}"), ("📥 Входящие", f"uacc_inbox:{acc_id}")],
        [("➕ Вступить по ссылке", f"uacc_join:{acc_id}"), ("🔑 Код входа", f"uacc_code:{acc_id}")],
        [("⏱ Продлить", f"uacc_ext:{acc_id}"), ("🚪 Освободить", f"uacc_rel:{acc_id}")],
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
    except NotYourAccount as exc:
        return await c.answer(str(exc), show_alert=True)
    await app.db.audit(c.from_user.id, "account_released", acc_id)
    await app.edit_panel(c, "Аккаунт освобождён.", reply_markup=app.kb([[("К каталогу", "free_accounts")]]))
    await c.answer()


@router.callback_query(F.data.startswith("uacc_send:"))
async def send_start(c: CallbackQuery, state: FSMContext):
    acc_id = int(c.data.split(":")[1])
    if not await lease_guard(c, acc_id):
        return
    await state.set_state(LeaseFlow.send_target)
    await state.update_data(acc_id=acc_id)
    await app.edit_panel(c, "Кому написать? Пришлите @username или числовой id.")
    await c.answer()


@router.message(LeaseFlow.send_target)
async def send_target(m: Message, state: FSMContext):
    target = (m.text or "").strip()
    if not target:
        return await m.answer("Пришлите @username или id.")
    await state.update_data(target=target)
    await state.set_state(LeaseFlow.send_text)
    await m.answer("Текст сообщения:")


@router.message(LeaseFlow.send_text)
async def send_text(m: Message, state: FSMContext):
    data = await state.get_data()
    acc_id, target = data["acc_id"], data["target"]
    await state.clear()
    try:
        await app.lease_service.guard_lease(acc_id, m.from_user.id)
        client = await app.client_pool_service.get(acc_id)
        entity = int(target) if target.lstrip("-").isdigit() else target
        await client.send_message(entity, m.text or "")
    except (NotYourAccount, LeaseExpired, AccountUnavailable) as exc:
        return await m.answer(f"Не отправлено: {exc}")
    except Exception as exc:
        return await m.answer(f"Не отправлено: {exc}")
    await app.db.run("UPDATE accounts SET last_used_at=? WHERE id=?", (app.now(), acc_id))
    await m.answer("Отправлено.", reply_markup=app.kb([[("В панель", f"uacc_panel:{acc_id}")]]))


@router.callback_query(F.data.startswith("uacc_inbox:"))
async def inbox(c: CallbackQuery):
    acc_id = int(c.data.split(":")[1])
    if not await lease_guard(c, acc_id):
        return
    await c.answer("Загружаю…")
    try:
        client = await app.client_pool_service.get(acc_id)
        dialogs = []
        if hasattr(client, "iter_dialogs"):
            async for dialog in client.iter_dialogs(limit=10):
                dialogs.append(dialog)
        else:
            async for dialog in client.get_dialogs(limit=10):
                dialogs.append(dialog)
    except Exception as exc:
        return await c.message.answer(f"Не удалось получить диалоги: {exc}")
    if not dialogs:
        return await c.message.answer("Диалогов нет.")
    lines = []
    for d in dialogs:
        name = d.name or "без названия"
        unread = f" [{d.unread_count}]" if getattr(d, "unread_count", 0) else ""
        lines.append(f"• {name}{unread}")
    await c.message.answer("Последние диалоги:\n" + "\n".join(lines),
                           reply_markup=app.kb([[("В панель", f"uacc_panel:{acc_id}")]]))


@router.callback_query(F.data.startswith("uacc_join:"))
async def join_start(c: CallbackQuery, state: FSMContext):
    acc_id = int(c.data.split(":")[1])
    if not await lease_guard(c, acc_id):
        return
    await state.set_state(LeaseFlow.join_link)
    await state.update_data(acc_id=acc_id)
    await app.edit_panel(c, "Пришлите ссылку-инвайт или @username канала.")
    await c.answer()


@router.message(LeaseFlow.join_link)
async def join_do(m: Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data["acc_id"]
    link = (m.text or "").strip()
    await state.clear()
    try:
        await app.lease_service.guard_lease(acc_id, m.from_user.id)
        client = await app.client_pool_service.get(acc_id)
        if hasattr(client, "join_chat"):
            await client.join_chat(link)
        else:
            from telethon.tl.functions.channels import JoinChannelRequest
            from telethon.tl.functions.messages import ImportChatInviteRequest
            from urllib.parse import urlparse
            parsed = urlparse(link)
            invite_hash = parsed.path[2:] if parsed.path.startswith("/+") else (
                parsed.path.rsplit("/", 1)[-1] if parsed.path.startswith("/joinchat/") else None
            )
            if invite_hash:
                await client(ImportChatInviteRequest(invite_hash))
            else:
                await client(JoinChannelRequest(link))
    except (NotYourAccount, LeaseExpired) as exc:
        return await m.answer(f"Не вступил: {exc}")
    except Exception as exc:
        return await m.answer(f"Не вступил: {exc}")
    await m.answer("Заявка на вступление отправлена.", reply_markup=app.kb([[("В панель", f"uacc_panel:{acc_id}")]]))


@router.callback_query(F.data.startswith("uacc_code:"))
async def login_code(c: CallbackQuery):
    acc_id = int(c.data.split(":")[1])
    if app.LOGIN_MODE != "code_login":
        return await c.answer("Выдача кода отключена: работайте через панель бота", show_alert=True)
    if not await lease_guard(c, acc_id):
        return
    acc = await app.db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc or not acc["phone"]:
        return await c.answer("У аккаунта нет номера", show_alert=True)
    await c.answer("Запрашиваю код…")
    try:
        client = await app.client_pool_service.get(acc_id)
        await client.send_code_request(acc["phone"])
    except FloodWaitError as exc:
        return await c.message.answer(f"Telegram просит подождать {exc.seconds} сек.")
    except Exception as exc:
        return await c.message.answer(f"Код не запрошен: {exc}")
    code = await wait_code(client)
    body = (f"Код входа для #{acc_id}: {code}" if code
            else "Код не пришёл за 25 секунд. Проверьте сервисные сообщения на аккаунте.")
    if acc["twofa_shared"] and acc["twofa_enc"]:
        try:
            body += f"\nПароль 2FA: {decrypt(acc['twofa_enc'])}"
        except SessionCryptoError:
            body += "\nПароль 2FA сохранён, но не расшифровывается."
    elif acc["twofa_enc"]:
        body += "\nПароль 2FA у администрации."
    await c.message.answer(body, reply_markup=app.kb([[("В панель", f"uacc_panel:{acc_id}")]]))


async def wait_code(client):
    last = 0
    try:
        recent = await client.get_messages(777000, limit=1)
        if recent:
            last = recent[0].id
    except Exception:
        return None
    for _ in range(12):
        await asyncio.sleep(2)
        try:
            messages = await client.get_messages(777000, limit=5, min_id=last)
        except Exception:
            continue
        for msg in messages:
            message_text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
            found = re.search(r"(?<!\\d)(\\d{5,6})(?!\\d)", message_text)
            if found:
                return found.group(1)
    return None
