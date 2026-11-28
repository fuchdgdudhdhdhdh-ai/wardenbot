"""Каталог аккаунтов и панель аренды для участников."""
import asyncio, logging, re
from datetime import datetime, timezone
from html import escape
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
CARD_CLASS_RU = {"ordinary": "Обычный", "premium": "Премиум"}
NEED_ROLE = {"ordinary": "Трафер", "premium": "Опытный"}


def mask(phone):
    digits = re.sub(r"\D", "", phone or "")
    return f"+{digits[:2]}***{digits[-2:]}" if len(digits) > 5 else "+" + (digits or "—")


def remaining(iso):
    try:
        left = datetime.fromisoformat(iso) - datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return "?"
    hours, minutes = divmod(max(0, int(left.total_seconds() // 60)), 60)
    return f"{hours} ч. {minutes} мин." if hours else f"{minutes} мин."


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
    """Карточка арендованного аккаунта (HTML: номер в <code> копируется нажатием)."""
    digits = re.sub(r"\D", "", acc["phone"] or "")
    phone = f"<code>+{digits}</code>" if digits else "нет"
    username = f"@{escape(acc['tg_username'])}" if acc["tg_username"] else "нет"
    if not acc.get("twofa_enc"):
        twofa = "не указан"
    elif not acc.get("twofa_shared"):
        twofa = "скрыт администратором"
    else:
        try:
            twofa = escape(decrypt(acc["twofa_enc"]))
        except SessionCryptoError:
            twofa = "недоступен, обратитесь к администратору"
    return (f"Аккаунт #{acc['id']}:\n"
            "📊\n"
            f"Тип: {CARD_CLASS_RU[acc['account_class']]}\n"
            f"☎Номер телефона: {phone}\n"
            "💻\n"
            f"Юзернейм: {username}\n"
            "🔐\n"
            f"2FA: {twofa}\n"
            "⏰\n"
            f"Аренда: осталось {remaining(acc['lease_until'])}\n"
            "\n"
            "⚠️\n"
            "Коды телеграм будут автоматически приходить в этот чат")


def panel_kb(acc_id):
    return app.kb([
        [("🚪 Освободить", f"uacc_rel:{acc_id}")],
        [("Назад ↩️", "free_accounts")],
    ])


async def panel_edit(c, acc_id, user_id):
    acc = await app.db.one("SELECT * FROM accounts WHERE id=? AND leased_by=?", (acc_id, user_id))
    if not acc:
        await c.message.answer("Аккаунт вам не принадлежит.")
        return
    await app.edit_panel(c, panel_text(acc), reply_markup=panel_kb(acc_id), parse_mode="HTML")


@router.callback_query(F.data.startswith("uacc_panel:"))
async def panel(c: CallbackQuery, state: FSMContext):
    acc_id = int(c.data.split(":")[1])
    await state.clear()
    if not await lease_guard(c, acc_id):
        return
    await panel_edit(c, acc_id, c.from_user.id)
    await c.answer()


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
