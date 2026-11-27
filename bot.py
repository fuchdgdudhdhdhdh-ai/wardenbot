import asyncio, os, re, logging
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated, FSInputFile
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ChatMemberStatus
from db import DB, now
from services import crypto, client_pool, lease
from services.code_login import CodeLoginManager

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("warden")
TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = int(os.getenv("TEAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "7955130892"))
SUPERADMIN_ID = int(os.getenv("SUPERADMIN_ID", "7696380514"))
INVITE = os.getenv("TEAM_INVITE", "https://t.me/+doX9--B6L-dmYzZi")
# Фоновое изображение входит в Docker-образ и отправляется как локальный файл.
MAIN_MENU_PHOTO_PATH = Path(__file__).resolve().parent / "assets" / "main_menu.jpg"

def main_menu_photo_is_valid() -> bool:
    """Проверяет целостность JPEG до отправки в Telegram."""
    try:
        with MAIN_MENU_PHOTO_PATH.open("rb") as photo:
            header = photo.read(3)
            photo.seek(-2, 2)
            footer = photo.read(2)
        return header == b"\xff\xd8\xff" and footer == b"\xff\xd9"
    except (OSError, ValueError):
        return False

OFFERS_VIDEO_ID = os.getenv("OFFERS_VIDEO_ID", "")
db = DB(os.getenv("DATABASE_PATH", "warden.db"))
bot = Bot(TOKEN)
dp = Dispatcher(storage=MemoryStorage())
log.warning("Redis is not configured; pending conversations will be lost on restart")
client_pool_service = None
lease_service = None
code_login = None
lease_stop = None
lease_task = None
ACCOUNTS_READY = False
LOGIN_MODE = os.getenv("ACCOUNT_LOGIN_MODE", "bot_only").strip().lower()

ROLES = ["Новичок", "Трафер", "Опытный", "Наставник"]

class Flow(StatesGroup):
    payout_invoice = State()
    team_name = State()
    team_tag = State()
    offer_title = State()
    offer_min = State()
    offer_channel = State()
    offer_links = State()
    offer_price = State()
    admin_reason = State()
    admin_cut = State()
    event_title = State()
    event_body = State()
    news_title = State()
    news_body = State()
    add_admin = State()
    demo_stock_label = State()
    withdraw_amount = State()

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows
    ])

def main_kb():
    return kb([[("👤Мой профиль","profile"),("📥Оферы","offers")],
               [("🗞Новости","news"),("🔰Команда","team")],
               [("Обучение 💻","learning")],
                              [("Бесплатный аккаунт ☎","free_accounts")]])

def back_menu():
    return kb([[("Назад ↩️","home")]])


# Единый рендер разделов: сохраняет одно сообщение и его фоновое фото.
async def edit_panel(c: CallbackQuery, text: str, reply_markup=None, **kwargs):
    msg = c.message
    if msg.photo or msg.video or msg.animation:
        try:
            return await msg.edit_caption(caption=text, reply_markup=reply_markup, **kwargs)
        except Exception as exc:
            if "message is not modified" in str(exc).lower():
                return msg
            raise
    try:
        return await msg.edit_text(text, reply_markup=reply_markup, **kwargs)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return msg
        raise

def is_admin(uid):
    return db.is_admin(uid, OWNER_ID)

async def user_row(uid):
    return await db.one("SELECT * FROM users WHERE id=?", (uid,))

async def member_ok(uid):
    if not CHAT_ID: return True
    try:
        m = await bot.get_chat_member(CHAT_ID, uid)
        return m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False

async def ensure_user(u, mentor=None):
    return await db.register(u, mentor)

class AccessGuard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user:
            return await handler(event, data)
        is_admin_user = await db.is_admin(user.id, OWNER_ID)
        message_text = getattr(event, "text", "") or ""
        allowed_commands = message_text.startswith(("/start", "/cancel", "/admin", "/continue", "/admdel_"))
        row = await user_row(user.id)
        if row and row["banned"] and not is_admin_user:
            answer = "⛔ Ваш доступ к боту заблокирован."
            if isinstance(event, CallbackQuery): await event.answer(answer, show_alert=True)
            elif isinstance(event, Message): await event.answer(answer)
            return
        if row and row["restricted_until"] and not is_admin_user and not allowed_commands:
            try:
                if datetime.fromisoformat(row["restricted_until"]) > datetime.now(timezone.utc):
                    answer = "⛔ Ваш доступ временно ограничен."
                    if isinstance(event, CallbackQuery): await event.answer(answer, show_alert=True)
                    elif isinstance(event, Message): await event.answer(answer)
                    return
            except ValueError:
                pass
        if not is_admin_user and not allowed_commands and not await member_ok(user.id):
            answer = "Чтобы пользоваться ботом, вступите в команду: " + INVITE
            if isinstance(event, CallbackQuery): await event.answer("Сначала вступите в группу", show_alert=True)
            elif isinstance(event, Message): await event.answer(answer)
            return
        return await handler(event, data)


async def send_home(target, user):
    """Показывает меню; повреждённая картинка не должна ломать /start."""
    name = user.get("username") or user.get("first_name") or "участник"
    caption = (f"Привет, {name}! Это панель тимы «Warden Team». С помощью бота ты можешь взять ссылку, "
               "посмотреть статистику и запросить выплату. Нажимай кнопки ниже и пользуйся ботом.")
    if main_menu_photo_is_valid():
        try:
            return await target.answer_photo(FSInputFile(str(MAIN_MENU_PHOTO_PATH)), caption=caption, reply_markup=main_kb())
        except Exception as exc:
            log.warning("Telegram отклонил фото главного меню (%s); использую текстовый вариант", type(exc).__name__)
    else:
        log.warning("Фото главного меню отсутствует или повреждено; использую текстовый вариант")
    return await target.answer(caption, reply_markup=main_kb())

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    args = (m.text or "").split(maxsplit=1)
    mentor = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try: mentor = int(args[1][4:])
        except ValueError: pass
    if mentor == m.from_user.id: mentor = None
    if mentor:
        mentor_user = await user_row(mentor)
        pupils = await db.one("SELECT COUNT(*) n FROM users WHERE mentor_id=?", (mentor,))
        if mentor_user and mentor_user["role"] == "Наставник" and pupils["n"] < 2:
            pass
        else: mentor = None
    user = await ensure_user(m.from_user, mentor)
    if user["banned"]:
        return
    if user["restricted_until"]:
        try:
            if datetime.fromisoformat(user["restricted_until"]) > datetime.now(timezone.utc):
                await m.answer("⛔ Ваш доступ временно ограничен.")
                return
        except ValueError: pass
    if not await member_ok(m.from_user.id):
        await m.answer("🫵Привет, тебя нету в нашей тиме что бы открыть панель вступи по ссылке: " + INVITE)
        return
    await send_home(m, user)


@dp.chat_member()
async def on_membership_update(event: ChatMemberUpdated):
    """Notify a user when they join; they must have opened the bot at least once."""
    if CHAT_ID and event.chat.id == CHAT_ID:
        old = event.old_chat_member.status
        new = event.new_chat_member.status
        joined = new in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR) and old not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        if joined:
            member = event.new_chat_member.user
            u = await user_row(member.id)
            if u and not u["banned"]:
                try:
                    await bot.send_photo(member.id, FSInputFile(str(MAIN_MENU_PHOTO_PATH)), caption="Вы вступили в Warden Team! Открываю панель.", reply_markup=main_kb())
                except Exception as exc:
                    log.info("Could not DM joining user %s: %s", member.id, exc)

@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await user_row(c.from_user.id)
    if not u or not await member_ok(c.from_user.id):
        await edit_panel(c, "Сначала вступите в команду: " + INVITE)
    else:
        await edit_panel(c, "Главное меню Warden Team:", reply_markup=main_kb())
    await c.answer()

@dp.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    u = await user_row(c.from_user.id)
    if not u: return await c.answer("Сначала нажмите /start", show_alert=True)
    link = await db.one("SELECT * FROM links WHERE id=?", (u["current_link_id"],)) if u["current_link_id"] else None
    mentor = await user_row(u["mentor_id"]) if u["mentor_id"] else None
    leads = await db.one("SELECT COALESCE(SUM(delta),0) n FROM lead_log WHERE user_id=? AND link_id=?",
                         (u["id"], u["current_link_id"])) if link else {"n":0}
    mentor_name = ("@" + mentor["username"]) if mentor and mentor["username"] else (mentor["first_name"] if mentor else "Нету")
    link_text = link["url"] if link else "Нет ссылки"
    number = str(link["id"]) if link else "Нет ссылки"
    text = (f"Профиль {u['first_name']}:\nБаланс: ${u.get('balance', 0):.1f}\nЗалито лидов: {leads['n'] if link else 'Нет ссылки'}\n"
            f"Ваша ссылка: {number} — {link_text}\nМой наставник: {mentor_name}\nБаланс: ${u.get('balance',0):.1f}\nДолжность: {u['role']}\n\nИспользуй кнопки ниже 👇")
    rows = [[("💸 Вывод средств","withdraw")],[("Назад ↩️","home")]]
    if link:
        offer = await db.one("SELECT * FROM offers WHERE id=?", (link["offer_id"],))
        used = await db.one("SELECT COALESCE(SUM(leads),0) n FROM payout_requests WHERE link_id=? AND status IN ('pending','paid')", (link["id"],))
        cycles = await db.one("SELECT COUNT(*) n FROM payout_requests WHERE link_id=? AND status IN ('pending','paid')", (link["id"],))
        wait_days = (cycles["n"] + 1) * (6 if u["role"] == "Новичок" else 3)
        assigned = datetime.fromisoformat(link["assigned_at"])
        available = max(0, leads["n"] - used["n"])
        if available >= max(2, offer["min_leads"]) and datetime.now(timezone.utc) >= assigned + timedelta(days=wait_days):
            rows.insert(0,[("ПОДАТЬ НА ВЫПЛАТУ✅","payout")])
    if mentor: rows.insert(1,[("Отвязаться от наставника","unmentor")])
    await edit_panel(c, text, reply_markup=kb(rows))
    await c.answer()

@dp.callback_query(F.data == "unmentor")
async def unmentor(c: CallbackQuery):
    await db.run("UPDATE users SET mentor_id=NULL WHERE id=?", (c.from_user.id,))
    await edit_panel(c, "Вы отвязались от наставника. Теперь действует стандартный прайс.")
    await c.answer()


@dp.callback_query(F.data == "withdraw")
async def withdraw_start(c: CallbackQuery, state: FSMContext):
    u = await user_row(c.from_user.id)
    if not u: return await c.answer("Сначала нажмите /start", show_alert=True)
    await state.set_state(Flow.withdraw_amount)
    await edit_panel(c, f"Ваш баланс: ${u.get('balance', 0):.1f}. Введите сумму вывода (не больше баланса):",
                     reply_markup=kb([[("Отмена ↩️","profile")]]))
    await c.answer()

@dp.message(Flow.withdraw_amount)
async def withdraw_submit(m: Message, state: FSMContext):
    try:
        amount = Decimal((m.text or "").replace(",", ".")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if amount <= 0: raise ValueError()
    except Exception:
        return await m.answer("Введите положительную сумму, например 15.5.")
    u = await user_row(m.from_user.id)
    if not u or Decimal(str(u.get("balance", 0))) < amount:
        return await m.answer("Недостаточно средств на балансе. Введите сумму в пределах баланса.")
    async def create(conn):
        cur = await conn.execute("SELECT balance FROM users WHERE id=?", (m.from_user.id,))
        row = await cur.fetchone()
        if not row or Decimal(str(row["balance"])) < amount: return None
        cur = await conn.execute("UPDATE users SET balance=balance-? WHERE id=? AND balance>=? RETURNING id", (float(amount), m.from_user.id, float(amount)))
        if not await cur.fetchone(): return None
        cur = await conn.execute("INSERT INTO withdrawals(user_id,amount,created_at) VALUES(?,?,?)", (m.from_user.id,float(amount),now()))
        return cur.lastrowid
    try:
        request_id = await db.transaction(create)
    except Exception:
        log.exception("Не удалось создать заявку на вывод")
        return await m.answer("Не удалось создать заявку. Попробуйте позже.")
    if not request_id:
        return await m.answer("Баланс изменился. Проверьте сумму и попробуйте снова.")
    await state.clear()
    link = await db.one("SELECT id,url FROM links WHERE id=?", (u["current_link_id"],)) if u.get("current_link_id") else None
    link_info = f"#{link['id']} — {link['url']}" if link else "нет активной ссылки"
    details = f"Заявка на вывод #{request_id}\nУчастник: {u['first_name']} (@{u['username'] or 'нет'}) · ID {u['id']}\nСумма: ${amount:.1f}\nСсылка: {link_info}"
    for admin_id in await admin_ids():
        try:
            await bot.send_message(admin_id, details, reply_markup=kb([[("Отклонить ❌",f"wd_reject:{request_id}"),("Выплачено ✅",f"wd_paid:{request_id}")]]), disable_web_page_preview=True)
        except Exception:
            log.exception("Не удалось отправить заявку на вывод администратору %s", admin_id)
    await m.answer(f"Заявка на вывод #{request_id} на сумму ${amount:.1f} отправлена администрации.")

@dp.callback_query(F.data == "payout")
async def payout(c: CallbackQuery, state: FSMContext):
    u = await user_row(c.from_user.id)
    link = await db.one("SELECT * FROM links WHERE id=?", (u["current_link_id"],)) if u and u["current_link_id"] else None
    if not link: return await c.answer("У вас нет активной ссылки", show_alert=True)
    leads = await db.one("SELECT COALESCE(SUM(delta),0) n FROM lead_log WHERE user_id=? AND link_id=?",
                         (u["id"], link["id"]))
    used = await db.one("SELECT COALESCE(SUM(leads),0) n FROM payout_requests WHERE link_id=? AND status IN ('pending','paid')", (link["id"],))
    available = max(0, leads["n"] - used["n"])
    paid_cycles = await db.one("SELECT COUNT(*) n FROM payout_requests WHERE link_id=? AND status IN ('paid','pending')", (link["id"],))
    cycle = paid_cycles["n"]
    wait_days = (cycle + 1) * (6 if u["role"] == "Новичок" else 3)
    assigned = datetime.fromisoformat(link["assigned_at"])
    offer = await db.one("SELECT * FROM offers WHERE id=?", (link["offer_id"],))
    minimum = max(2, offer["min_leads"])
    if available < minimum or datetime.now(timezone.utc) < assigned + timedelta(days=wait_days):
        return await c.answer(f"Для следующей выплаты нужно минимум {minimum} новых лидов и {wait_days} дней после выдачи ссылки. Сейчас доступно: {available}.", show_alert=True)
    bonus = 0.1 if u["role"] in ("Опытный","Наставник") else 0
    mentor_discount = 0.1 if u["mentor_id"] else 0
    price = float((Decimal(str(offer["price"])) + Decimal(str(bonus)) - Decimal(str(mentor_discount))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    price = max(0.0, price)
    try:
        req_id, payable_leads, amount = await db.create_payout(u["id"], link["id"], leads["n"], price, u["mentor_id"])
    except ValueError as e:
        return await c.answer(str(e), show_alert=True)
    await state.update_data(payout_id=req_id)
    await state.set_state(Flow.payout_invoice)
    await edit_panel(c, f"Заявка #{req_id}: {payable_leads} лидов × ${price:.2f} = ${amount:.2f}.\n"
                           "Пожалуйста пришлите свой счет в CryptoBot.\n\n"
                           "Инструкция: Откройте главное меню крипто бота ⮕ Счета ⮕ Создать счёт ⮕ "
                           "Многоразовый ⮕ USDT ⮕ Далее ⮕ Скопируйте ссылку и отправьте её сюда.",
                           reply_markup=kb([[("В меню ↩️","home")]]))
    await c.answer()

@dp.message(Flow.payout_invoice)
async def payout_invoice(m: Message, state: FSMContext):
    data = await state.get_data()
    invoice = (m.text or "").strip()
    parsed = urlparse(invoice)
    if parsed.scheme != "https" or parsed.hostname not in ("t.me", "telegram.me") or "cryptobot" not in parsed.path.lower():
        return await m.answer("Пришлите именно ссылку CryptoBot вида https://t.me/CryptoBot?start=... .")
    await db.run("UPDATE payout_requests SET invoice=? WHERE id=?", (invoice,data["payout_id"]))
    await state.clear()
    await m.answer("Ваша заявка отправлена на рассмотрение администратором. Пожалуйста дождитесь проверки и не спамьте в личные сообщения иначе вы будете заблокированы.")
    for a in await admin_ids():
        await bot.send_message(a, f"Новая заявка #{data['payout_id']} от @{m.from_user.username or m.from_user.id}")

async def admin_ids():
    rows = await db.all("SELECT user_id FROM admins")
    return list({OWNER_ID, *[x["user_id"] for x in rows]})


async def admin_logging_enabled():
    row = await db.one("SELECT value FROM settings WHERE key='admin_logging'")
    return bool(row and row["value"] == "1")


async def notify_admins(event: str, user_id: int, details: str = ""):
    if not await admin_logging_enabled():
        return
    user = await user_row(user_id)
    who = f"{user['first_name']} (@{user['username'] or 'нет'}) · ID {user_id}" if user else f"ID {user_id}"
    message = f"🔔 Событие: {event}\nПользователь: {who}"
    if details:
        message += f"\n{details}"
    for admin_id in await admin_ids():
        try:
            await bot.send_message(admin_id, message, disable_web_page_preview=True)
        except Exception as exc:
            log.warning("Не удалось отправить лог администратору %s: %s", admin_id, exc)


async def build_admin_keyboard():
    enabled = await admin_logging_enabled()
    label = "🟢 Логирование: ВКЛ" if enabled else "🔴 Логирование: ВЫКЛ"
    return kb([[("Участники 👥","adm_users")],[("Выдать офер📥","adm_offers")],
               [("Заявки 💰","adm_payouts")],[("💸 Заявки на вывод","adm_withdrawals")],[("🕴️Добавить администрацию","adm_add")],
               [("Новости 🗞","adm_news")],
               [("📲 Аккаунты","acc_menu")],[("🔎 Ссылки пользователей","adm_monitor")],
               [(label,"adm_log_toggle")]])

@dp.callback_query(F.data == "offers")
async def offers(c: CallbackQuery):
    items = await db.all("SELECT * FROM offers WHERE active=1 ORDER BY id")
    if not items:
        await edit_panel(c, "Пока нет доступных офферов.", reply_markup=back_menu())
    else:
        rows = [[(f"{x['title']} - {x['price']:.1f}$",f"offer:{x['id']}")] for x in items]
        rows.append([("Назад ↩️","home")])
        if OFFERS_VIDEO_ID:
            await edit_panel(c, "Выбери канал на который будешь лить трафик:", reply_markup=kb(rows))
        else:
            await edit_panel(c, "Выбери канал на который будешь лить трафик:", reply_markup=kb(rows))
    await c.answer()

@dp.callback_query(F.data.startswith("offer:"))
async def offer_detail(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = await db.one("SELECT * FROM offers WHERE id=? AND active=1",(oid,))
    if not o: return await c.answer("Оффер недоступен",show_alert=True)
    await edit_panel(c, f"{o['title']}\nКурс: {o['price']:.1f}$\nМинимально лидов: {o['min_leads']}\nКанал: {o['channel']}\n\nВы точно хотите выбрать этот офер?",
                           reply_markup=kb([[("Да ✅",f"take:{oid}")],[("Назад ↩️","offers")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("take:"))
async def take_link(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    current = await user_row(c.from_user.id)
    if current and current["current_link_id"]:
        return await c.answer("У вас уже есть активная ссылка. Обратитесь к администратору для её замены.", show_alert=True)
    link = await db.take_link(c.from_user.id, oid)
    if not link: return await edit_panel(c, "Свободных ссылок для этого оффера нет. Обратитесь к администратору.")
    await notify_admins("взял ссылку", c.from_user.id, f"Ссылка #{link['id']} · Оффер ID {oid}")
    await edit_panel(c, f"Вы успешно взяли ссылку!\nВаш номер: {link['id']}\nВаша ссылка: {link['url']}\n\nВы можете посмотреть свою ссылку в профиле в любой момент",
                           reply_markup=kb([[("В меню","home")]]))
    await c.answer()

@dp.callback_query(F.data == "news")
async def news(c: CallbackQuery):
    items = await db.all("SELECT * FROM news ORDER BY id DESC LIMIT 20")
    await edit_panel(c, "Ознакомиться с актуальными данными, конкурсами или прайсами можно тут:",
                           reply_markup=kb([[ (x["title"],f"readnews:{x['id']}")] for x in items] + [[("Назад ↩️","home")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("readnews:"))
async def read_news(c: CallbackQuery):
    n = await db.one("SELECT * FROM news WHERE id=?",(int(c.data.split(":")[1]),))
    if n: await edit_panel(c, n["title"]+"\n\n"+n["body"]+("\n"+n["url"] if n["url"] else ""),reply_markup=back_menu())
    await c.answer()

@dp.callback_query(F.data == "team")
async def team_menu(c: CallbackQuery):
    u = await user_row(c.from_user.id)
    t = await db.one("SELECT * FROM teams WHERE id=?",(u["team_id"],)) if u and u["team_id"] else None
    if t:
        owner = t["owner_id"] == u["id"]
        caption = f"Команда «{t['name']}» ({t['tag']})."
        rows = [[("🗞 Мероприятия", "events")], [("📊 Статистика", "team_stats")]]
        rows.append([("Назад ↩️", "home")])
        await edit_panel(c, caption, reply_markup=kb(rows))
    else:
        await edit_panel(c, "Привет! Если ты хочешь, чтобы тебе помогали в трафике или хочешь помогать в трафике, нажимай подходящую кнопку 👇",
            reply_markup=kb([[ ("Создать команду ➕","team_create")],[("Вступить в команду 👥","team_join")],[("Назад ↩️","home")]]))
    await c.answer()

@dp.callback_query(F.data == "team_create")
async def team_create(c: CallbackQuery,state: FSMContext):
    u=await user_row(c.from_user.id)
    if not u or u["role"]!="Наставник":
        await edit_panel(c, "❌ Сначала нужно повысится на должность «Наставник», а затем создавать команду.")
    else:
        await state.set_state(Flow.team_name); await edit_panel(c, "Введите название команды:")
    await c.answer()

@dp.message(Flow.team_name)
async def team_name(m:Message,state:FSMContext):
    if len(m.text)>60: return await m.answer("Название слишком длинное (максимум 60 символов).")
    await state.update_data(team_name=m.text.strip()); await state.set_state(Flow.team_tag)
    await m.answer("Введите сокращенное название (до 5 букв):")

@dp.message(Flow.team_tag)
async def team_tag(m:Message,state:FSMContext):
    tag=m.text.strip().upper()
    if not 1<=len(tag)<=5: return await m.answer("Сокращение должно содержать от 1 до 5 символов.")
    await state.update_data(team_tag=tag); d=await state.get_data()
    await m.answer(f"Вы действительно хотите создать команду {d['team_name']}, все участники будут носить тег {tag}.",
                    reply_markup=kb([[("Отменить ❌","home"),("Создать ✅","team_confirm")]]))
    await state.update_data(team_confirm=True)

@dp.callback_query(F.data=="team_confirm")
async def team_confirm(c:CallbackQuery,state:FSMContext):
    d=await state.get_data(); u=await user_row(c.from_user.id)
    if not d.get("team_name") or not u or u["role"]!="Наставник": return await c.answer("Сессия истекла",show_alert=True)
    existing=await db.one("SELECT * FROM teams WHERE owner_id=?",(u["id"],))
    if existing:
        await state.clear(); await edit_panel(c, f"У вас уже есть команда: {existing['name']}.",reply_markup=back_menu()); return await c.answer()
    tid=await db.run("INSERT INTO teams(owner_id,name,tag,created_at) VALUES(?,?,?,?)",
                     (u["id"],d["team_name"],d["team_tag"],now()))
    await db.run("UPDATE users SET team_id=? WHERE id=?",(tid,u["id"]))
    await notify_admins("создал команду", c.from_user.id, f"Команда: {d['team_name']} ({d['team_tag']}) · ID {tid}")
    await state.clear(); await edit_panel(c, f"Команда {d['team_name']} создана!",reply_markup=back_menu()); await c.answer()

@dp.callback_query(F.data=="team_join")
async def team_join(c:CallbackQuery):
    teams=await db.all("SELECT * FROM teams ORDER BY id")
    await edit_panel(c, "Выбери команду в которую хочешь вступить:",
        reply_markup=kb([[(f"{t['tag']} — {await team_rating(t['id'])}⭐",f"jointeam:{t['id']}")] for t in teams]+[[("Назад ↩️","team")]]))
    await c.answer()

async def team_rating(tid):
    # Отклонённые заявки / все рассмотренные заявки; при отсутствии решений рейтинг 100%.
    x=await db.one("SELECT COALESCE(SUM(CASE WHEN status='rejected' THEN leads WHEN status='paid' THEN cuts ELSE 0 END),0) bad, COALESCE(SUM(CASE WHEN status IN ('paid','rejected') THEN leads ELSE 0 END),0) total FROM payout_requests p JOIN users u ON u.id=p.user_id WHERE u.team_id=?",(tid,))
    return round(100*(1-(x["bad"] or 0)/(x["total"] or 1))) if x["total"] else 100

@dp.callback_query(F.data.startswith("jointeam:"))
async def join_team(c:CallbackQuery):
    tid=int(c.data.split(":")[1]); t=await db.one("SELECT * FROM teams WHERE id=?",(tid,))
    if not t: return await c.answer("Команда не найдена",show_alert=True)
    member=await user_row(c.from_user.id)
    if member and member["team_id"] and member["team_id"] != tid:
        return await c.answer("Вы уже состоите в другой команде. Сначала выйдите из неё.",show_alert=True)
    if t["owner_id"] == c.from_user.id:
        return await c.answer("Вы владелец этой команды.",show_alert=True)
    await edit_panel(c, f"Вы выбрали команду:\nНазвание команды: {t['name']}\nРейтинг: {await team_rating(tid)}\nСокращение: {t['tag']}.",
        reply_markup=kb([[("Вступить 👉",f"joinconfirm:{tid}")],[("Назад ↩️","team_join")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("joinconfirm:"))
async def join_confirm(c:CallbackQuery):
    tid=int(c.data.split(":")[1]); t=await db.one("SELECT * FROM teams WHERE id=?",(tid,))
    if t:
        await db.run("UPDATE users SET team_id=? WHERE id=?",(tid,c.from_user.id))
        await notify_admins("вступил в команду", c.from_user.id, f"Команда: {t['name']} · ID {tid}")
        await edit_panel(c, f"Вы успешно вступили в команду {t['name']} ✅",reply_markup=back_menu())
    await c.answer()

@dp.callback_query(F.data=="team_members")
async def team_members(c:CallbackQuery):
    t=await db.one("SELECT * FROM teams WHERE owner_id=?",(c.from_user.id,))
    if not t: return await c.answer("Нет команды",show_alert=True)
    members=await db.all("SELECT id,username,first_name FROM users WHERE team_id=? AND id<>?",(t["id"],c.from_user.id))
    rows=[[(("@"+x["username"]) if x["username"] else x["first_name"],f"kick:{x['id']}")] for x in members]
    await edit_panel(c, f"Всего в команде {len(members)+1} участников, детально:",reply_markup=kb(rows+[[("Назад ↩️","team")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("kick:"))
async def kick_member(c:CallbackQuery):
    uid=int(c.data.split(":")[1]); t=await db.one("SELECT * FROM teams WHERE owner_id=?",(c.from_user.id,))
    if t: await db.run("UPDATE users SET team_id=NULL WHERE id=? AND team_id=?",(uid,t["id"]))
    await edit_panel(c, "Участник исключён.",reply_markup=back_menu()); await c.answer()

@dp.callback_query(F.data=="team_delete")
async def team_delete(c:CallbackQuery):
    t=await db.one("SELECT * FROM teams WHERE owner_id=?",(c.from_user.id,))
    if t:
        await db.run("UPDATE users SET team_id=NULL WHERE team_id=?",(t["id"],))
        await db.run("DELETE FROM events WHERE team_id=?",(t["id"],))
        await db.run("DELETE FROM teams WHERE id=?",(t["id"],))
    await edit_panel(c, "Команда расформирована.",reply_markup=back_menu()); await c.answer()

@dp.callback_query(F.data=="team_stats")
async def team_stats(c:CallbackQuery):
    u=await user_row(c.from_user.id)
    t=await db.one("SELECT * FROM teams WHERE id=?",(u["team_id"],)) if u and u["team_id"] else None
    if not t: return await c.answer("Вы не состоите в команде",show_alert=True)
    members=await db.all("SELECT id,first_name,username FROM users WHERE team_id=? ORDER BY id",(t["id"],))
    member_ids=[x["id"] for x in members]
    links=await db.all("SELECT l.id,l.url,l.assigned_user,o.title FROM links l LEFT JOIN offers o ON o.id=l.offer_id WHERE l.assigned_user IN (SELECT id FROM users WHERE team_id=?) ORDER BY l.id DESC",(t["id"],))
    total_leads=0
    lines=[]
    for link in links:
        stats=await db.one("SELECT COALESCE(SUM(delta),0) n FROM lead_log WHERE link_id=?",(link["id"],))
        total_leads += stats["n"]
        owner=next((x for x in members if x["id"]==link["assigned_user"]),None)
        label=(owner["first_name"] if owner else str(link["assigned_user"]))
        lines.append(f"#{link['id']} · {label} · {link['title'] or 'оффер'} · {stats['n']} лидов")
    profit=await db.one("SELECT COALESCE(SUM(amount),0) n FROM payout_requests p JOIN users u ON u.id=p.user_id WHERE u.team_id=? AND p.status='paid'",(t["id"],))
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(t["created_at"])).days
    body=(f"Команда: {t['name']} ({t['tag']})\nУчастников: {len(members)}\nСсылок: {len(links)}\n"
          f"Всего лидов по ссылкам команды: {total_leads}\nОплачено участникам: ${profit['n']:.1f}\n"
          f"Возраст команды: {age} дней\nРейтинг: {await team_rating(t['id'])}%")
    if lines: body += "\n\nСсылки участников:\n" + "\n".join(lines[:35])
    await edit_panel(c,body,reply_markup=back_menu())
    await c.answer()
@dp.callback_query(F.data=="events")
async def events(c:CallbackQuery):
    u=await user_row(c.from_user.id)
    t=await db.one("SELECT * FROM teams WHERE id=?",(u["team_id"],)) if u and u["team_id"] else None
    if not t: return await c.answer("Вы не состоите в команде",show_alert=True)
    buttons=[[("Мероприятия и новости","my_events")]]
    if t["owner_id"]==c.from_user.id:
        buttons.append([("Создать мероприятие","event_create")])
    buttons.append([("Назад ↩️","team")])
    await edit_panel(c,"Мероприятия команды и новости администрации:",reply_markup=kb(buttons))
    await c.answer()


@dp.callback_query(F.data=="event_create")
async def event_create(c:CallbackQuery,state:FSMContext):
    u=await user_row(c.from_user.id)
    t=await db.one("SELECT * FROM teams WHERE id=?",(u["team_id"],)) if u and u["team_id"] else None
    if not (t and t["owner_id"]==c.from_user.id):
        return await c.answer("Создать мероприятие может только владелец команды",show_alert=True)
    await state.set_state(Flow.event_title); await edit_panel(c, "Введи название мероприятия (до 15 символов):"); await c.answer()

@dp.message(Flow.event_title)
async def event_title(m:Message,state:FSMContext):
    if len(m.text)>15: return await m.answer("Максимум 15 символов.")
    await state.update_data(event_title=m.text); await state.set_state(Flow.event_body)
    await m.answer("Введи текст, условия мероприятия:")

@dp.message(Flow.event_body)
async def event_body(m:Message,state:FSMContext):
    d=await state.get_data(); t=await db.one("SELECT * FROM teams WHERE owner_id=?",(m.from_user.id,))
    if not t:
        await state.clear()
        return await m.answer("Команда не найдена. Создать мероприятие может владелец команды.")
    await db.run("INSERT INTO events(team_id,title,body,created_at) VALUES(?,?,?,?)",(t["id"],d["event_title"],m.text,now()))
    await state.clear(); await m.answer("Мероприятие создано.",reply_markup=back_menu())

@dp.callback_query(F.data=="my_events")
async def my_events(c:CallbackQuery):
    u=await user_row(c.from_user.id)
    t=await db.one("SELECT * FROM teams WHERE id=?",(u["team_id"],)) if u and u["team_id"] else None
    ev=await db.all("SELECT * FROM events WHERE team_id=? ORDER BY id DESC",(t["id"],)) if t else []
    news_rows=await db.all("SELECT id,title FROM news ORDER BY id DESC LIMIT 20")
    rows=[[(e["title"],f"event:{e['id']}")] for e in ev]
    rows += [[(f"🗞 {n['title']}",f"readnews:{n['id']}")] for n in news_rows]
    await edit_panel(c,"Мероприятия команды и новости администрации:",reply_markup=kb(rows+[[("Назад ↩️","events")]]))
    await c.answer()


@dp.callback_query(F.data.startswith("event:"))
async def event_open(c:CallbackQuery):
    e=await db.one("SELECT * FROM events WHERE id=?",(int(c.data.split(":")[1]),))
    u=await user_row(c.from_user.id)
    t=await db.one("SELECT * FROM teams WHERE id=?",(u["team_id"],)) if u and u["team_id"] else None
    can_delete=bool(e and (await require_admin(c) or (t and t["id"]==e["team_id"] and t["owner_id"]==c.from_user.id)))
    buttons=([[("Удалить ✖️",f"eventdel:{e['id']}")]] if can_delete else [])
    buttons.append([("Назад ↩️","my_events")])
    if e: await edit_panel(c,e["title"]+"\n\n"+e["body"],reply_markup=kb(buttons))
    else: await c.answer("Мероприятие не найдено",show_alert=True)
    await c.answer()

@dp.callback_query(F.data.startswith("eventdel:"))
async def event_delete(c:CallbackQuery):
    eid=int(c.data.split(":")[1]); t=await db.one("SELECT * FROM teams WHERE owner_id=?",(c.from_user.id,))
    if await require_admin(c): await db.run("DELETE FROM events WHERE id=?",(eid,))
    elif t: await db.run("DELETE FROM events WHERE id=? AND team_id=?",(eid,t["id"]))
    else: return await c.answer("Нет доступа",show_alert=True)
    await edit_panel(c, "Мероприятие удалено.",reply_markup=back_menu()); await c.answer()

@dp.callback_query(F.data=="learning")
async def learning(c:CallbackQuery):
    u=await user_row(c.from_user.id)
    if not u or u["role"]!="Наставник":
        return await c.answer("Раздел доступен только наставникам.",show_alert=True)
    pupils=await db.one("SELECT COUNT(*) n FROM users WHERE mentor_id=?",(u["id"],))
    earned=await db.one("""SELECT COALESCE(SUM(p.leads)*0.1,0) n FROM payout_requests p
                           WHERE p.mentor_id=? AND p.status='paid'""",(u["id"],))
    await edit_panel(c, f"Ваша ссылка: https://t.me/{(await bot.get_me()).username}?start=ref_{u['id']}\n"
                           f"Кол-во обучающихся: {pupils['n']}/2\nСколько вы заработали: ${earned['n']:.2f}",
                           reply_markup=back_menu())
    await c.answer()

# ---------------- DEMO ACCOUNT CATALOG (NO REAL ACCOUNTS) ----------------
ROLE_LEVEL = {"Новичок": 0, "Трафер": 1, "Опытный": 2, "Наставник": 3}

@dp.callback_query(F.data == "free_accounts")
async def free_accounts(c: CallbackQuery):
    u = await user_row(c.from_user.id)
    if not u or ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL["Трафер"]:
        return await edit_panel(c, 'Недоступно для вашей должности, минимальная должность: «Трафер».', reply_markup=back_menu())
    from handlers import accounts_user
    await accounts_user.render_catalog(c)

@dp.callback_query(F.data.startswith("demo_type:"))
async def demo_type(c: CallbackQuery):
    u = await user_row(c.from_user.id)
    kind = c.data.split(":", 1)[1]
    if kind not in ("ordinary", "premium") or not u:
        return await c.answer("Некорректный запрос", show_alert=True)
    min_role = "Опытный" if kind == "premium" else "Трафер"
    if ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL[min_role]:
        await edit_panel(c, f"Доступ к этому разделу открывается с должности «{min_role}».", reply_markup=kb([[("Назад ↩️","free_accounts")]]))
        return await c.answer()
    title = "Премиум" if kind == "premium" else "Обычные"
    count = await db.one("SELECT COUNT(*) n FROM demo_account_slots WHERE account_type=? AND status='available'", (kind,))
    await edit_panel(c, 
        f"{title} — демонстрационный каталог\nДоступно тестовых слотов: {count['n']}\n\n"
        "Это учебная имитация: реальные Telegram-аккаунты и данные для входа не выдаются.",
        reply_markup=kb([[('Получить тестовый слот', f'demo_take:{kind}')], [('Назад ↩️', 'free_accounts')]])
    )
    await c.answer()

@dp.callback_query(F.data.startswith("demo_take:"))
async def demo_take(c: CallbackQuery):
    u = await user_row(c.from_user.id)
    kind = c.data.split(":", 1)[1]
    if kind not in ("ordinary", "premium") or not u:
        return await c.answer("Некорректный запрос", show_alert=True)
    min_role = "Опытный" if kind == "premium" else "Трафер"
    if ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL[min_role]:
        await edit_panel(c, f"Доступно с должности «{min_role}».", reply_markup=kb([[("Назад ↩️","free_accounts")]]))
        return await c.answer()
    async def allocate(conn):
        cur = await conn.execute("SELECT id,label FROM demo_account_slots WHERE account_type=? AND status='available' ORDER BY id LIMIT 1", (kind,))
        slot = await cur.fetchone()
        if not slot:
            return None
        cur = await conn.execute("UPDATE demo_account_slots SET status='assigned',assigned_user=?,assigned_at=? WHERE id=? AND status='available' RETURNING id", (c.from_user.id, now(), slot['id']))
        if not await cur.fetchone():
            return None
        return dict(slot)
    slot = await db.transaction(allocate)
    if not slot:
        await c.answer("Свободных тестовых слотов пока нет.", show_alert=True)
    else:
        await db.audit(c.from_user.id, "demo_slot_assigned", slot['id'], f"type={kind}; label={slot['label']}")
        await edit_panel(c, 
            f"Тестовый слот #{slot['id']} назначен вам.\nМетка: {slot['label']}\n\n"
            "Это только демонстрационная запись: номера, кода авторизации и реального аккаунта здесь нет.",
            reply_markup=kb([[('Открыть демо-панель', f'demo_open:{slot["id"]}')], [('В меню', 'home')]])
        )
        await c.answer("Слот назначен")

@dp.callback_query(F.data.startswith("demo_open:"))
async def demo_open(c: CallbackQuery):
    try: slot_id = int(c.data.split(":", 1)[1])
    except ValueError: return await c.answer("Некорректный слот", show_alert=True)
    slot = await db.one("SELECT * FROM demo_account_slots WHERE id=?", (slot_id,))
    if not slot or slot['assigned_user'] != c.from_user.id:
        return await c.answer("Этот слот вам не назначен", show_alert=True)
    await edit_panel(c, 
        f"Демо-панель слота #{slot_id}\nСтатус: {'завершён' if slot['status']=='completed' else 'назначен'}\n\n"
        "В настоящую авторизацию здесь не выполняется вход. Нажмите кнопку, чтобы завершить учебный сценарий.",
        reply_markup=kb([[('Завершить демо-сценарий', f'demo_complete:{slot_id}')], [('В меню', 'home')]])
    )
    await c.answer()

@dp.callback_query(F.data.startswith("demo_complete:"))
async def demo_complete(c: CallbackQuery):
    try: slot_id = int(c.data.split(":", 1)[1])
    except ValueError: return await c.answer("Некорректный слот", show_alert=True)
    slot = await db.one("SELECT * FROM demo_account_slots WHERE id=?", (slot_id,))
    if not slot or slot['assigned_user'] != c.from_user.id:
        return await c.answer("Этот слот вам не назначен", show_alert=True)
    if slot['status'] == 'assigned':
        await db.run("UPDATE demo_account_slots SET status='completed',completed_at=? WHERE id=? AND assigned_user=? AND status='assigned'", (now(), slot_id, c.from_user.id))
    await edit_panel(c, "Демо-сценарий завершён. Спасибо!", reply_markup=back_menu())
    await c.answer()

# ---------------- ADMIN ----------------
async def require_admin(event):
    return await db.is_admin(event.from_user.id,OWNER_ID)

async def deny_admin_command(m: Message):
    # Для Message Telegram не поддерживает show_alert; удаляем команду
    # и показываем короткое уведомление с последующим удалением.
    try:
        await m.delete()
    except Exception:
        pass
    notice = await bot.send_message(m.chat.id, "⛔ У вас нет прав администратора.")
    async def remove_notice():
        await asyncio.sleep(4)
        try:
            await notice.delete()
        except Exception:
            pass
    asyncio.create_task(remove_notice())


@dp.message(Command("admin"))
async def admin(m:Message):
    if not await require_admin(m):
        return await deny_admin_command(m)
    admin_caption = f"Добро пожаловать в админ-панель, {m.from_user.first_name}, используйте кнопки ниже:"
    admin_markup = await build_admin_keyboard()
    try:
        if main_menu_photo_is_valid():
            await m.answer_photo(FSInputFile(str(MAIN_MENU_PHOTO_PATH)), caption=admin_caption, reply_markup=admin_markup)
        else:
            await m.answer(admin_caption, reply_markup=admin_markup)
    except Exception:
        log.exception("Не удалось отправить фото админ-панели; использую текстовый вариант")
        await m.answer(admin_caption, reply_markup=admin_markup)


@dp.callback_query(F.data == "admin_home")
async def admin_home(c: CallbackQuery):
    if not await require_admin(c):
        return await c.answer("Нет доступа", show_alert=True)
    await edit_panel(c, f"Админ-панель, {c.from_user.first_name}.", reply_markup=await build_admin_keyboard())
    await c.answer()


@dp.callback_query(F.data == "adm_log_toggle")
async def admin_log_toggle(c: CallbackQuery):
    if not await require_admin(c):
        return await c.answer("Нет доступа", show_alert=True)
    enabled = await admin_logging_enabled()
    value = "0" if enabled else "1"
    await db.run("INSERT INTO settings(key,value) VALUES('admin_logging',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
    await edit_panel(c, "Логирование включено 🟢" if value == "1" else "Логирование выключено 🔴",
                     reply_markup=await build_admin_keyboard())
    await c.answer("Настройка сохранена")


@dp.callback_query(F.data == "adm_monitor")
async def admin_link_monitor(c: CallbackQuery):
    if not await require_admin(c):
        return await c.answer("Нет доступа", show_alert=True)
    rows = await db.all("SELECT id,username,first_name,current_link_id FROM users WHERE current_link_id IS NOT NULL ORDER BY joined_at DESC LIMIT 100")
    buttons = []
    for user in rows:
        link = await db.one("SELECT id,url FROM links WHERE id=? AND assigned_user=?", (user["current_link_id"], user["id"]))
        if link:
            label = f"{user['first_name']} (@{user['username'] or user['id']}) · ссылка #{link['id']}"
            buttons.append([(label, f"admuser:{user['id']}")])
    buttons.append([("Назад ↩️", "admin_home")])
    await edit_panel(c, "Активные ссылки участников. Нажмите на пользователя, чтобы увидеть точный URL:",
                     reply_markup=kb(buttons))
    await c.answer()


@dp.callback_query(F.data.startswith("adm_"))
async def admin_router(c:CallbackQuery,state:FSMContext):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    action=c.data
    if action=="adm_users":
        users=await db.all("SELECT * FROM users ORDER BY joined_at DESC LIMIT 100")
        await edit_panel(c, "Участники:",reply_markup=kb([[(("@"+u["username"]) if u["username"] else str(u["first_name"]),f"admuser:{u['id']}")] for u in users]+[[("Назад ↩️","admin_home")]]))
    elif action=="adm_offers":
        await edit_panel(c, "Выбери действие:",reply_markup=kb([[("Добавить ссылки🔗","adm_addlinks")],[("Прикрепить статистику📊","adm_stats")],[("Назад ↩️","admin_home")]]))
    elif action=="adm_payouts":
        reqs=await db.all("""SELECT p.* FROM payout_requests p JOIN users u ON u.id=p.user_id
                           WHERE p.status='pending'
                           ORDER BY CASE u.role WHEN 'Наставник' THEN 0 WHEN 'Опытный' THEN 1
                           WHEN 'Трафер' THEN 2 ELSE 3 END, p.created_at ASC""")
        await edit_panel(c, "Заявки на выплату:",reply_markup=kb([[(f"{r['leads']} пдп — ${r['amount']:.2f}",f"payoutreq:{r['id']}")] for r in reqs]+[[("Назад ↩️","admin_home")]]))
    elif action=="adm_withdrawals":
        reqs=await db.all("SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at")
        await edit_panel(c, "Заявки на вывод:", reply_markup=kb([[(f"#{x['id']} · ID {x['user_id']} · ${x['amount']:.1f}",f"wd_detail:{x['id']}")] for x in reqs]+[[( "Назад ↩️","admin_home")]]))
    elif action=="adm_add":
        await state.set_state(Flow.add_admin); await edit_panel(c, "Введите id пользователя для добавления в администрацию:")
    elif action=="adm_news":
        await state.set_state(Flow.news_title); await edit_panel(c, "Введите заголовок новости:")
    elif action=="adm_stock":
        ordinary = await db.one("SELECT COUNT(*) n FROM demo_account_slots WHERE account_type='ordinary' AND status='available'")
        premium = await db.one("SELECT COUNT(*) n FROM demo_account_slots WHERE account_type='premium' AND status='available'")
        assigned = await db.one("SELECT COUNT(*) n FROM demo_account_slots WHERE status='assigned'")
        await edit_panel(c, 
            f"Тестовый склад\n❄️ Обычные свободны: {ordinary['n']}\n🌟 Премиум свободны: {premium['n']}\nНазначено участникам: {assigned['n']}\n\n"
            "Добавляются только тестовые слоты, без телефонных номеров и данных авторизации.",
            reply_markup=kb([[('Завезти обычный акк', 'stock_add:ordinary')], [('Завезти премиум акк', 'stock_add:premium')], [('Назад ↩️', 'admin_home')]])
        )
    elif action=="adm_addlinks":
        await state.set_state(Flow.offer_title); await edit_panel(c, "Для добавления офера укажи его название:")
    elif action=="adm_stats":
        links=await db.all("SELECT * FROM links ORDER BY id")
        await edit_panel(c, "Выберите ссылку:",reply_markup=kb([[(f"{x['id']} ссылка",f"linkstats:{x['id']}")] for x in links]+[[("Назад ↩️","adm_offers")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("stock_add:"))
async def stock_add_start(c: CallbackQuery, state: FSMContext):
    if not await require_admin(c): return await c.answer("Нет доступа", show_alert=True)
    kind = c.data.split(":", 1)[1]
    if kind not in ("ordinary", "premium"): return await c.answer("Некорректный тип", show_alert=True)
    await state.update_data(demo_stock_type=kind)
    await state.set_state(Flow.demo_stock_label)
    await edit_panel(c, "Введите метку тестового слота (например, DEMO-001). Не отправляйте номер телефона, код, пароль или файл сессии.")
    await c.answer()

@dp.message(Flow.demo_stock_label)
async def stock_add_label(m: Message, state: FSMContext):
    if not await db.is_admin(m.from_user.id, OWNER_ID): return
    label = (m.text or "").strip()
    if not label or len(label) > 60:
        return await m.answer("Метка должна содержать от 1 до 60 символов.")
    data = await state.get_data()
    kind = data.get("demo_stock_type")
    if kind not in ("ordinary", "premium"):
        await state.clear(); return await m.answer("Тип слота потерян. Начните добавление заново.")
    slot_id = await db.run("INSERT INTO demo_account_slots(account_type,label) VALUES(?,?)", (kind, label))
    await db.audit(m.from_user.id, "demo_slot_added", slot_id, f"type={kind}; label={label}")
    await state.clear()
    await m.answer(f"Тестовый слот #{slot_id} добавлен на склад.", reply_markup=kb([[('Открыть склад', 'adm_stock')], [('В админ-панель', 'admin_home')]]))

@dp.callback_query(F.data.startswith("admuser:"))
async def admin_user(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    uid=int(c.data.split(":")[1]); u=await user_row(uid)
    if not u: return await c.answer("Пользователь не найден",show_alert=True)
    count=await db.one("SELECT COUNT(*) n FROM links WHERE assigned_user=?",(uid,))
    current_link = await db.one("SELECT id,url FROM links WHERE id=? AND assigned_user=?", (u["current_link_id"], uid)) if u["current_link_id"] else None
    paid=await db.one("SELECT COALESCE(SUM(amount),0) n FROM payout_requests WHERE user_id=? AND status='paid'",(uid,))
    link_info = f"\nТекущая ссылка: #{current_link['id']} — {current_link['url']}" if current_link else "\nТекущая ссылка: нет"
    await edit_panel(c, f"Участник {u['first_name']} (@{u['username'] or 'нет'})\nID: {uid}\nРегистрация: {u['joined_at']}\nВзято ссылок: {count['n']}{link_info}\nЗаработал: ${paid['n']:.2f}\nДолжность: {u['role']}",
      reply_markup=kb([[("Бан ⛔",f"ban:{uid}")],[("Ограничить доступ ⚠️",f"restrict:{uid}")],
                       [("Забрать ссылку🫴",f"revoke:{uid}")],[("Управление должностью🚀",f"role:{uid}")],
                       [("Назад↩️","adm_users")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("ban:"))
async def ban_user(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    uid=int(c.data.split(":")[1]); await db.run("UPDATE users SET banned=1 WHERE id=?",(uid,)); await db.audit(c.from_user.id,"user_banned",uid)
    await edit_panel(c, "Пользователь заблокирован."); await c.answer()

@dp.callback_query(F.data.startswith("restrict:"))
async def restrict(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    uid=int(c.data.split(":")[1])
    await edit_panel(c, "На какой срок ограничить доступ?",reply_markup=kb([[("1 час",f"restrictset:{uid}:1")],[("1 день",f"restrictset:{uid}:24")],[("Месяц",f"restrictset:{uid}:720")],[("Назад","adm_users")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("restrictset:"))
async def restrict_set(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    _,uid,hours=c.data.split(":")
    until=(datetime.now(timezone.utc)+timedelta(hours=int(hours))).isoformat()
    await db.run("UPDATE users SET restricted_until=? WHERE id=?",(until,int(uid))); await db.audit(c.from_user.id,"user_restricted",int(uid),f"hours={hours}")
    await edit_panel(c, "Доступ ограничен."); await c.answer()

@dp.callback_query(F.data.startswith("revoke:"))
async def revoke(c:CallbackQuery,state:FSMContext):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    uid=int(c.data.split(":")[1]); await state.update_data(revoke_uid=uid)
    await state.set_state(Flow.admin_reason); await edit_panel(c, "Введите причину по которой отбираете ссылку:"); await c.answer()

@dp.message(Flow.admin_reason)
async def revoke_reason(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    d=await state.get_data(); uid=d["revoke_uid"]
    u=await user_row(uid)
    if u and u["current_link_id"]:
        await db.run("UPDATE links SET active=0 WHERE id=?",(u["current_link_id"],))
        await db.run("UPDATE users SET current_link_id=NULL,current_offer_id=NULL WHERE id=?",(uid,))
    await bot.send_message(uid,f"Администрация забрала у вас ссылку ✊.\nПричина: {m.text}")
    await state.clear(); await m.answer("Ссылка отозвана.")

@dp.callback_query(F.data.startswith("role:"))
async def role_menu(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    uid=int(c.data.split(":")[1]); u=await user_row(uid)
    rows=[]
    if u["role"]!="Наставник": rows.append([("Повысить📈",f"roleup:{uid}")])
    if u["role"]!="Новичок": rows.append([("Понизить📉",f"roledown:{uid}")])
    rows.append([("Назад↩️","adm_users")])
    await edit_panel(c, "Выберите кнопку ниже, чтобы управлять должностью:",reply_markup=kb(rows)); await c.answer()

async def change_role(c,uid,up):
    u=await user_row(uid); idx=ROLES.index(u["role"])
    new=ROLES[min(3,idx+1) if up else max(0,idx-1)]
    await db.run("UPDATE users SET role=? WHERE id=?",(new,uid)); await db.audit(c.from_user.id,"role_changed",uid,f"{u['role']} -> {new}")
    if u["role"] == "Наставник" and new != "Наставник":
        # Existing pupils return to the standard price; historical paid commissions
        # remain attached to payout_requests.mentor_id for accurate accounting.
        await db.run("UPDATE users SET mentor_id=NULL WHERE mentor_id=?",(uid,))
        t=await db.one("SELECT * FROM teams WHERE owner_id=?",(uid,))
        if t:
            await db.run("UPDATE users SET team_id=NULL WHERE team_id=?",(t["id"],))
            await db.run("DELETE FROM events WHERE team_id=?",(t["id"],))
            await db.run("DELETE FROM teams WHERE id=?",(t["id"],))
    await edit_panel(c, f"Новая должность: {new}"); await c.answer()

@dp.callback_query(F.data.startswith("roleup:"))
async def role_up(c:CallbackQuery):
    if await require_admin(c): await change_role(c,int(c.data.split(":")[1]),True)
    else: await c.answer("Нет доступа",show_alert=True)

@dp.callback_query(F.data.startswith("roledown:"))
async def role_down(c:CallbackQuery):
    if await require_admin(c): await change_role(c,int(c.data.split(":")[1]),False)
    else: await c.answer("Нет доступа",show_alert=True)

@dp.message(Flow.offer_title)
async def offer_title(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    if not m.text or len(m.text.strip()) > 45: return await m.answer("Название оффера должно быть не длиннее 45 символов.")
    await state.update_data(offer_title=m.text.strip()); await state.set_state(Flow.offer_min)
    await m.answer("Укажите минимальное число лидов для выплаты:")

@dp.message(Flow.offer_min)
async def offer_min(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    try: n=int(m.text); assert n>=1
    except: return await m.answer("Введите целое число больше нуля.")
    await state.update_data(offer_min=n,links=[]); await state.set_state(Flow.offer_channel)
    await m.answer("Укажите название канала/источника трафика для этого оффера:")

@dp.message(Flow.offer_channel)
async def offer_channel(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    if not m.text or len(m.text)>100: return await m.answer("Название канала должно быть до 100 символов.")
    await state.update_data(offer_channel=m.text.strip()); await state.set_state(Flow.offer_links)
    await m.answer("Скидывайте ссылки для офера в формате: 1. https://...\n"
                   "Каждая ссылка отдельным сообщением. Когда закончите, нажмите /continue.")

@dp.message(Flow.offer_links)
async def offer_link_add(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    # This state handler is registered before the generic /continue handler,
    # so handle the finish command here instead of trying to parse it as a URL.
    command = (m.text or "").strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    if command == "/continue":
        try:
            await m.delete()
        except Exception:
            pass
        d = await state.get_data()
        if not d.get("links"):
            return await m.answer("Добавьте хотя бы одну ссылку.")
        await state.set_state(Flow.offer_price)
        return await m.answer("Укажите прайс за 1 лида (например 0.8):")
    match=re.search(r"https?://\S+",m.text or "")
    if not match: return await m.answer("Не нашёл ссылку. Отправьте URL, начинающийся с http:// или https://.")
    d=await state.get_data(); links=d.get("links",[]); links.append(match.group(0).rstrip(".,)"))
    await state.update_data(links=links)
    await m.answer(f"Получено ссылок *{len(links)}* ✅",parse_mode="Markdown")

@dp.message(Command("continue"))
async def continue_offer(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID):
        return await deny_admin_command(m)
    try:
        await m.delete()
    except Exception:
        pass
    if await state.get_state()!=Flow.offer_links.state: return
    d=await state.get_data()
    if not d.get("links"): return await m.answer("Добавьте хотя бы одну ссылку.")
    await state.set_state(Flow.offer_price); await m.answer("Укажите прайс за 1 лида (например 0.8):")

@dp.message(Flow.offer_price)
async def offer_price(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    try:
        if not m.text: raise ValueError()
        price=float(Decimal(m.text.replace(",",".")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)); assert price>0 and price <= 1000
    except: return await m.answer("Введите положительное число, например 0.8.")
    d=await state.get_data()
    oid=await db.run("INSERT INTO offers(title,min_leads,price,channel,created_at) VALUES(?,?,?,?,?)",
                     (d["offer_title"],d["offer_min"],price,d["offer_channel"],now()))
    added=0
    for url in d["links"]:
        try:
            await db.run("INSERT INTO links(offer_id,url) VALUES(?,?)",(oid,url)); added += 1
        except Exception:
            pass
    await state.clear(); await m.answer(f"Оффер создан. Ссылок добавлено: {added} (дубликаты пропущены).",reply_markup=kb([[("В админ-панель", "admin_home")]]))

@dp.callback_query(F.data.startswith("linkstats:"))
async def link_stats(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    lid=int(c.data.split(":")[1]); link=await db.one("SELECT * FROM links WHERE id=?",(lid,))
    if not link: return await c.answer("Ссылка не найдена")
    await edit_panel(c, "Выберете что нужно сделать со ссылкой:",reply_markup=kb([[("+1 лид",f"lead:{lid}:1")],[("-1 лид",f"lead:{lid}:-1")],[("Назад ↩️","adm_offers")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("lead:"))
async def lead_change(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    _,lid,delta=c.data.split(":"); link=await db.one("SELECT * FROM links WHERE id=?",(int(lid),))
    if link and link["assigned_user"]:
        current = await db.one("SELECT COALESCE(SUM(delta),0) n FROM lead_log WHERE link_id=?", (int(lid),))
        reserved = await db.one("SELECT COALESCE(SUM(leads),0) n FROM payout_requests WHERE link_id=? AND status IN ('pending','paid')", (int(lid),))
        if int(delta) < 0 and current["n"] + int(delta) < reserved["n"]:
            await c.answer("Нельзя уменьшить лиды ниже уже поданных/оплаченных.", show_alert=True)
            return
        if current["n"] + int(delta) < 0:
            await c.answer("Количество лидов не может быть отрицательным.", show_alert=True)
            return
        await db.run("INSERT INTO lead_log(user_id,link_id,delta,created_at,admin_id) VALUES(?,?,?,?,?)",
                     (link["assigned_user"],int(lid),int(delta),now(),c.from_user.id))
        await db.audit(c.from_user.id,"lead_adjusted",link["assigned_user"],f"link={lid}; delta={delta}")
        await edit_panel(c, "Статистика обновлена.")
        try: await c.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
    else: await edit_panel(c, "Ссылка ещё не выдана участнику.")
    await c.answer()



@dp.callback_query(F.data.startswith("wd_detail:"))
async def withdrawal_detail(c: CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа", show_alert=True)
    rid=int(c.data.split(":")[1])
    r=await db.one("SELECT * FROM withdrawals WHERE id=?",(rid,))
    u=await user_row(r["user_id"]) if r else None
    link=await db.one("SELECT id,url FROM links WHERE id=?",(u["current_link_id"],)) if u and u["current_link_id"] else None
    if not r or not u: return await c.answer("Заявка не найдена",show_alert=True)
    link_label = f"#{link['id']} — {link['url']}" if link else "нет"
    text_body=f"Заявка #{rid}\nУчастник: {u['first_name']} (@{u['username'] or 'нет'}) · ID {u['id']}\nСумма: ${r['amount']:.1f}\nСсылка: {link_label}"
    await edit_panel(c,text_body,reply_markup=kb([[("Отклонить ❌",f"wd_reject:{rid}"),("Выплачено ✅",f"wd_paid:{rid}")],[("Назад ↩️","adm_withdrawals")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("wd_"))
async def withdrawal_review(c: CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа", show_alert=True)
    action, raw_id = c.data.split(":")
    request_id = int(raw_id)
    status = "paid" if action == "wd_paid" else "rejected"
    row = await db.one("UPDATE withdrawals SET status=?,reviewed_at=?,admin_id=? WHERE id=? AND status='pending' RETURNING user_id,amount",
                       (status, now(), c.from_user.id, request_id))
    if not row: return await c.answer("Заявка уже обработана", show_alert=True)
    user = await user_row(row["user_id"])
    if status == "paid":
        message = f"Заявка на вывод #{request_id} одобрена и отмечена как выплаченная. Сумма: ${row['amount']:.1f}."
    else:
        message = f"Заявка на вывод #{request_id} отклонена администрацией. Сумма ${row['amount']:.1f} не возвращается на баланс согласно правилам."
    try: await bot.send_message(row["user_id"], message)
    except Exception: log.exception("Не удалось уведомить пользователя о заявке на вывод")
    await db.audit(c.from_user.id, f"withdrawal_{status}", row["user_id"], f"request={request_id}; amount={row['amount']}")
    await edit_panel(c, f"Заявка на вывод #{request_id}: { 'выплачена' if status == 'paid' else 'отклонена'}.")
    await c.answer("Решение сохранено")

@dp.callback_query(F.data.startswith("payoutreq:"))
async def payout_detail(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    rid=int(c.data.split(":")[1]); r=await db.one("SELECT * FROM payout_requests WHERE id=?",(rid,))
    u=await user_row(r["user_id"]) if r else None
    if not r or not u: return await c.answer("Заявка не найдена",show_alert=True)
    details = (f"Трафер: @{u['username'] or u['first_name']}\nНомер ссылки: {r['link_id']}\n"
        f"Кол-во лидов: {r['leads']}\nСумма к оплате: ${r['amount']:.2f}\nСчет для оплаты: {r['invoice'] or 'не прислан'}")
    if not r["invoice"]:
        await edit_panel(c, details + "\n\nОжидается ссылка на счёт CryptoBot от участника.", reply_markup=kb([[("Назад ↩️", "adm_payouts")]]))
    else:
        await edit_panel(c, details, reply_markup=kb([[ ("Оплачено ✅",f"paid:{rid}")],[ ("Срезать ✂️",f"cut:{rid}")],[ ("Отклонить ❌",f"reject:{rid}")],[ ("Назад ↩️","adm_payouts")]]))
    await c.answer()

@dp.callback_query(F.data.startswith("paid:"))
async def payout_paid(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    rid=int(c.data.split(":")[1]); r=await db.one("SELECT * FROM payout_requests WHERE id=?",(rid,))
    if not r or not r["invoice"]: return await c.answer("Счёт CryptoBot ещё не получен",show_alert=True)
    settled = await db.settle_payout(rid, c.from_user.id, "paid")
    if not settled: return await c.answer("Заявка уже обработана",show_alert=True)
    await db.audit(c.from_user.id,"payout_paid",settled["user_id"],f"request={rid}; amount={settled['amount']}")
    await bot.send_message(settled["user_id"],f"Вам пришла выплата в размере ${settled['amount']:.2f} на CryptoBot. Срезов нет")
    await edit_panel(c, "Заявка отмечена как оплаченная."); await c.answer()

@dp.callback_query(F.data.startswith("reject:"))
async def payout_reject(c:CallbackQuery):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    rid=int(c.data.split(":")[1]); r=await db.one("SELECT * FROM payout_requests WHERE id=?",(rid,))
    if not r or not r["invoice"]: return await c.answer("Счёт CryptoBot ещё не получен",show_alert=True)
    settled = await db.settle_payout(rid, c.from_user.id, "rejected")
    if not settled: return await c.answer("Заявка уже обработана",show_alert=True)
    await db.audit(c.from_user.id,"payout_rejected",settled["user_id"],f"request={rid}")
    try: await bot.send_message(settled["user_id"],f"Ваша заявка на выплату #{rid} отклонена администрацией. Обратитесь к администратору за подробностями.")
    except Exception: pass
    await edit_panel(c, "Заявка отклонена; лиды не списаны и могут быть поданы повторно.")
    await c.answer()

@dp.callback_query(F.data.startswith("cut:"))
async def payout_cut(c:CallbackQuery,state:FSMContext):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    rid=int(c.data.split(":")[1]); req=await db.one("SELECT * FROM payout_requests WHERE id=?",(rid,))
    if not req or not req["invoice"]: return await c.answer("Счёт CryptoBot ещё не получен",show_alert=True)
    await state.update_data(cut_request=rid)
    await state.set_state(Flow.admin_cut); await edit_panel(c, "Сколько лидов срезать?"); await c.answer()

@dp.message(Flow.admin_cut)
async def payout_cut_count(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    try: cuts=int(m.text)
    except: return await m.answer("Введите целое число.")
    d=await state.get_data(); r=await db.one("SELECT * FROM payout_requests WHERE id=?",(d["cut_request"],))
    if not r or r["status"]!="pending" or cuts<0 or cuts>r["leads"]: return await m.answer("Заявка уже обработана либо число срезов некорректно.")
    try: settled=await db.settle_payout(r["id"],m.from_user.id,"paid",cuts)
    except ValueError: return await m.answer("Некорректное число срезов.")
    if not settled: return await m.answer("Заявка уже обработана другим администратором.")
    amount=settled["amount"]
    await db.audit(m.from_user.id,"payout_cut",settled["user_id"],f"request={r['id']}; cuts={cuts}; amount={amount}")
    await bot.send_message(settled["user_id"],f"Вам пришла выплата в размере: ${amount:.2f} на CryptoBot. Срезы: {cuts} пдп")
    await state.clear(); await m.answer("Выплата обработана.")

@dp.message(Flow.add_admin)
async def add_admin(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    try: uid=int(m.text)
    except: return await m.answer("Нужен числовой Telegram ID.")
    await state.update_data(new_admin=uid)
    await m.answer(f"Точно добавить id {uid}?",reply_markup=kb([[("Да", "addadmin_yes"),("Нет","addadmin_no")]]))

@dp.callback_query(F.data.in_({"addadmin_yes","addadmin_no"}))
async def add_admin_confirm(c:CallbackQuery,state:FSMContext):
    if not await require_admin(c): return await c.answer("Нет доступа",show_alert=True)
    d=await state.get_data(); uid=d.get("new_admin")
    if c.data=="addadmin_yes" and uid:
        await db.run("INSERT OR IGNORE INTO admins(user_id,added_at) VALUES(?,?)",(uid,now())); await db.audit(c.from_user.id,"admin_added",uid)
        await edit_panel(c, f"Пользователь {uid} теперь администратор! Для удаления используйте /admdel_{uid}")
        try: await bot.send_message(uid,"Поздравляем! Вы стали администратором!")
        except: pass
    else: await edit_panel(c, "Действие отменено.")
    await state.clear(); await c.answer()

@dp.message(F.text.regexp(r"^/admdel_\d+$"))
async def del_admin(m:Message):
    if m.from_user.id!=SUPERADMIN_ID: return
    uid=int(m.text.split("_",1)[1])
    await db.run("DELETE FROM admins WHERE user_id=?",(uid,)); await db.audit(m.from_user.id,"admin_removed",uid)
    await m.answer(f"Администратор {uid} удалён.")

@dp.message(Flow.news_title)
async def news_title(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    if not m.text or len(m.text.strip()) > 60: return await m.answer("Заголовок новости должен быть не длиннее 60 символов.")
    await state.update_data(news_title=m.text.strip()); await state.set_state(Flow.news_body)
    await m.answer("Введите текст новости:")

@dp.message(Flow.news_body)
async def news_body(m:Message,state:FSMContext):
    if not await db.is_admin(m.from_user.id,OWNER_ID): return
    d=await state.get_data()
    await db.run("INSERT INTO news(title,body,created_at) VALUES(?,?,?)",(d["news_title"],m.text,now()))
    await state.clear(); await m.answer("Новость опубликована.")


@dp.message(Command("plus"))
async def balance_plus(m: Message):
    if not await require_admin(m): return await deny_admin_command(m)
    parts = (m.text or "").split()
    if len(parts) == 2:
        uid = m.reply_to_message.from_user.id if m.reply_to_message else m.from_user.id
        raw_amount = parts[1]
    elif len(parts) == 3 and parts[1].isdigit():
        uid, raw_amount = int(parts[1]), parts[2]
    else:
        return await m.answer("Формат: /plus сумма (ответом на сообщение пользователя) или /plus ID сумма")
    try: amount = Decimal(raw_amount.replace(",", ".")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except Exception: return await m.answer("Сумма должна быть числом, например /plus 10.5")
    if amount <= 0: return await m.answer("Сумма должна быть больше нуля.")
    await db.run("UPDATE users SET balance=balance+? WHERE id=?", (float(amount), uid))
    u = await user_row(uid)
    if not u: return await m.answer("Пользователь не найден.")
    await db.audit(m.from_user.id,"balance_plus",uid,str(amount))
    await m.answer(f"Баланс пользователя {uid} увеличен на ${amount:.1f}. Новый баланс: ${u['balance']:.1f}.")

@dp.message(Command("minus"))
async def balance_minus(m: Message):
    if not await require_admin(m): return await deny_admin_command(m)
    parts = (m.text or "").split()
    if len(parts) == 2:
        uid = m.reply_to_message.from_user.id if m.reply_to_message else m.from_user.id
        raw_amount = parts[1]
    elif len(parts) == 3 and parts[1].isdigit():
        uid, raw_amount = int(parts[1]), parts[2]
    else:
        return await m.answer("Формат: /minus сумма (ответом на сообщение пользователя) или /minus ID сумма")
    try: amount = Decimal(raw_amount.replace(",", ".")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    except Exception: return await m.answer("Сумма должна быть числом, например /minus 10.5")
    if amount <= 0: return await m.answer("Сумма должна быть больше нуля.")
    updated = await db.one("UPDATE users SET balance=balance-? WHERE id=? AND balance>=? RETURNING balance", (float(amount),uid,float(amount)))
    u = await user_row(uid)
    if not u: return await m.answer("Пользователь не найден.")
    if not updated: return await m.answer("Недостаточно средств на балансе пользователя.")
    await db.audit(m.from_user.id,"balance_minus",uid,str(amount))
    await m.answer(f"Баланс пользователя {uid}: ${updated['balance']:.1f}.")

@dp.message(Command("cancel"))
async def cancel(m:Message,state:FSMContext):
    await state.clear()
    user = await user_row(m.from_user.id)
    if not user:
        user = await ensure_user(m.from_user)
    if user["banned"]:
        try:
            await m.delete()
        except Exception:
            pass
        return
    # Сначала отправляем ответ, только затем удаляем команду.
    if not await member_ok(m.from_user.id):
        await m.answer("Чтобы открыть панель, вступите в команду: " + INVITE)
    else:
        await send_home(m, user)
    try:
        await m.delete()
    except Exception:
        log.debug("Не удалось удалить команду /cancel", exc_info=True)

async def health(request: web.Request):
    """Проверка доступности веб-сервиса Render."""
    return web.json_response({"status": "ok", "service": "warden-team-bot"})


async def on_startup(app: web.Application):
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения Render")

    # Каталог /var/data подключён как Persistent Disk в render.yaml.
    await db.init()
    global client_pool_service, lease_service, code_login, lease_stop, lease_task, ACCOUNTS_READY
    try:
        crypto.validate_key()
        ACCOUNTS_READY = True
    except Exception as exc:
        ACCOUNTS_READY = False
        log.warning("Раздел аккаунтов отключён: %s", exc)
    client_pool_service = client_pool.configure(db)
    lease_service = lease.configure(db, client_pool_service, int(os.getenv("ACCOUNT_LEASE_MIN", os.getenv("ACCOUNT_LEASE_TTL_MIN", "30"))))
    code_login = CodeLoginManager(db, client_pool_service)
    await code_login.cleanup_expired()
    lease_stop = asyncio.Event()
    lease_task = asyncio.create_task(lease_service.scheduler(lease_stop))
    guard = AccessGuard()
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)

    me = await bot.get_me()
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть панель"),
        BotCommand(command="cancel", description="Отменить действие"),
    ])

    webhook_base = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    webhook_path = os.getenv("WEBHOOK_PATH", "/telegram/webhook")
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if not webhook_base:
        raise RuntimeError("Render не передал RENDER_EXTERNAL_URL. Проверьте тип сервиса Web Service.")
    if not webhook_secret or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", webhook_secret):
        raise RuntimeError("Задайте WEBHOOK_SECRET: 1–256 символов A-Z, a-z, 0-9, _ или -.")
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path

    await bot.set_webhook(
        url=f"{webhook_base}{webhook_path}",
        secret_token=webhook_secret,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    await dp.emit_startup(bot=bot)

    log.info("Webhook запущен для @%s; URL=%s%s", me.username, webhook_base, webhook_path)


async def on_shutdown(app: web.Application):
    await dp.emit_shutdown(bot=bot)
    global lease_stop, lease_task
    if lease_stop:
        lease_stop.set()
    if lease_task:
        lease_task.cancel()
    if client_pool_service:
        await client_pool_service.shutdown()
    await bot.session.close()
    log.info("Warden Team Web Service остановлен")



async def run_polling_local():
    """Режим polling оставлен для локальной разработки; Render работает через webhook."""
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    await db.init()
    global client_pool_service, lease_service, code_login, lease_stop, lease_task, ACCOUNTS_READY
    try:
        crypto.validate_key()
        ACCOUNTS_READY = True
    except Exception as exc:
        ACCOUNTS_READY = False
        log.warning("Раздел аккаунтов отключён: %s", exc)
    client_pool_service = client_pool.configure(db)
    lease_service = lease.configure(db, client_pool_service, int(os.getenv("ACCOUNT_LEASE_MIN", os.getenv("ACCOUNT_LEASE_TTL_MIN", "30"))))
    code_login = CodeLoginManager(db, client_pool_service)
    await code_login.cleanup_expired()
    lease_stop = asyncio.Event()
    lease_task = asyncio.create_task(lease_service.scheduler(lease_stop))
    dp.message.middleware(AccessGuard())
    dp.callback_query.middleware(AccessGuard())
    me = await bot.get_me()
    log.info("Локальный polling запущен для @%s (id=%s)", me.username, me.id)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    webhook_path = os.getenv("WEBHOOK_PATH", "/telegram/webhook")
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path
    webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=webhook_secret or None,
    ).register(app, path=webhook_path)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    # Handler modules import bot as app; alias this running module to avoid a second bot instance.
    import sys
    sys.modules.setdefault("bot", sys.modules[__name__])

from handlers import accounts_admin, accounts_user as accounts_user_router
dp.include_router(accounts_admin.router)
dp.include_router(accounts_user_router.router)

if __name__ == "__main__":
    if os.getenv("BOT_MODE", "polling").lower() == "polling":
        asyncio.run(run_polling_local())
    else:
        port = int(os.getenv("PORT", "10000"))
        web.run_app(create_app(), host="0.0.0.0", port=port, access_log=log)

