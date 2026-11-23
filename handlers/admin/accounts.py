import asyncio, os, tempfile, time, logging
from pathlib import Path
from aiogram import Router, F
from aiogram.filters import BaseFilter
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError, FloodWaitError, PasswordHashInvalidError
from services.crypto import decrypt
from services.session_import import SessionImporter, SessionImportError
from services.lease import lease_service
from keyboards.accounts import admin_accounts_menu, admin_card, code_keyboard

class IsAdmin(BaseFilter):
    def __init__(self, db, owner_id): self.db,self.owner_id=db,owner_id
    async def __call__(self, event): return await self.db.is_admin(event.from_user.id,self.owner_id)

class AccountFSM(StatesGroup):
    wait_session=State(); wait_json=State(); wait_phone=State(); wait_code=State(); wait_2fa=State()

_phone_clients={}; _timeout_tasks={}
log = logging.getLogger(__name__)

def setup_admin_accounts(db, pool, owner_id):
    router=Router(name='admin_accounts')
    router.message.filter(IsAdmin(db,owner_id)); router.callback_query.filter(IsAdmin(db,owner_id))

    async def show_list(target, page=0):
        rows=await db.all("SELECT a.*,u.username AS renter_name FROM accounts a LEFT JOIN users u ON u.id=a.leased_by ORDER BY a.id DESC LIMIT 10 OFFSET ?",(page*10,))
        total=await db.one("SELECT COUNT(*) n FROM accounts")
        pages=max(1,(total['n']+9)//10)
        if not rows: text='Аккаунтов пока нет.'
        else:
            lines=[]
            for a in rows:
                until=a['lease_until'] or '—'; renter=(f"{a['leased_by']} (@{a['renter_name']})" if a['leased_by'] else '—')
                lines.append(f"#{a['id']} | {a['label'] or '—'} | @{a['tg_username'] or '—'} | {a['status']} | {renter} | до {until}")
            text='\n'.join(lines)
        from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
        rows_kb=[[InlineKeyboardButton(text=f"#{a['id']} · {a['label'] or a['tg_username'] or 'аккаунт'}",callback_data=f"accadm:card:{a['id']}")] for a in rows]
        nav=[]
        if page: nav.append(InlineKeyboardButton(text='◀️',callback_data=f'accadm:list:{page-1}'))
        nav.append(InlineKeyboardButton(text=f'{page+1}/{pages}',callback_data='accadm:noop'))
        if page+1<pages: nav.append(InlineKeyboardButton(text='▶️',callback_data=f'accadm:list:{page+1}'))
        rows_kb.append(nav); rows_kb.append([InlineKeyboardButton(text='➕ Завести аккаунт',callback_data='accadm:menu')])
        markup=InlineKeyboardMarkup(inline_keyboard=rows_kb)
        if isinstance(target,CallbackQuery): await target.message.edit_text(text,reply_markup=markup)
        else: await target.answer(text,reply_markup=markup)

    @router.callback_query(F.data=='accadm:menu')
    async def menu(c:CallbackQuery,state:FSMContext):
        await cancel_phone_import(c.from_user.id)
        await c.answer(); await c.message.edit_text('Выберите способ добавления аккаунта:',reply_markup=admin_accounts_menu())

    @router.callback_query(F.data.startswith('accadm:import:'))
    async def import_start(c:CallbackQuery,state:FSMContext):
        await cancel_phone_import(c.from_user.id)
        kind=c.data.rsplit(':',1)[1]
        await state.set_state(AccountFSM.wait_session if kind=='session' else AccountFSM.wait_json)
        await c.answer(); await c.message.answer('Отправьте файл .session (до 5 МБ).' if kind=='session' else 'Отправьте JSON-файл (до 1 МБ).')

    async def save_upload(m,state,kind):
        doc=m.document
        if not doc: return await m.answer('Пришлите документ Telegram.')
        name=doc.file_name or ''
        ext=Path(name).suffix.lower()
        if ext != ('.session' if kind=='session' else '.json'):
            return await m.answer('Неверное расширение файла.')
        limit=5*1024*1024 if kind=='session' else 1024*1024
        if doc.file_size and doc.file_size>limit: return await m.answer('Файл превышает допустимый размер.')
        Path('tmp').mkdir(exist_ok=True)
        fd,path=tempfile.mkstemp(prefix='account_',suffix=ext,dir='tmp'); os.close(fd)
        try:
            await m.bot.download(doc,destination=path)
            if os.path.getsize(path) > limit:
                raise SessionImportError("Файл превышает допустимый размер после загрузки.")
            importer=SessionImporter(db,pool,m.from_user.id)
            acc_id=await (importer.import_session_file(path) if kind=='session' else importer.import_json(path))
            await state.clear(); await m.answer(f'Аккаунт #{acc_id} успешно импортирован.',reply_markup=admin_accounts_menu())
        except (SessionImportError,RuntimeError) as exc:
            await m.answer(f'Импорт не выполнен: {exc}')
        except Exception:
            await m.answer('Не удалось импортировать файл. Проверьте формат и доступность Telegram.')
        finally:
            if os.path.exists(path): os.remove(path)

    @router.message(AccountFSM.wait_session)
    async def upload_session(m:Message,state:FSMContext): await save_upload(m,state,'session')
    @router.message(AccountFSM.wait_json)
    async def upload_json(m:Message,state:FSMContext): await save_upload(m,state,'json')

    @router.callback_query(F.data=='accadm:phone')
    async def phone_start(c:CallbackQuery,state:FSMContext):
        await cancel_phone_import(c.from_user.id)
        await c.answer(); await state.set_state(AccountFSM.wait_phone)
        async def expire_phone_step():
            await asyncio.sleep(300)
            if await state.get_state() == AccountFSM.wait_phone.state:
                await state.clear()
                await c.message.answer('Время ожидания номера истекло. Начните импорт заново.')
            _timeout_tasks.pop(c.from_user.id,None)
        old=_timeout_tasks.pop(c.from_user.id,None)
        if old: old.cancel()
        _timeout_tasks[c.from_user.id]=asyncio.create_task(expire_phone_step())
        await c.message.answer('Введите номер телефона в международном формате, например +79990000000.')

    def arm_timeout(uid,state_key,client):
        old=_timeout_tasks.pop(uid,None)
        if old: old.cancel()
        async def expire():
            await asyncio.sleep(300)
            current=await state_key.get_state()
            if current in (AccountFSM.wait_phone.state,AccountFSM.wait_code.state,AccountFSM.wait_2fa.state):
                try: await client.disconnect()
                except Exception: log.exception("Ошибка отключения клиента по таймауту")
                await state_key.clear()
                _phone_clients.pop(uid,None)
            _timeout_tasks.pop(uid,None)
        _timeout_tasks[uid]=asyncio.create_task(expire())

    @router.message(AccountFSM.wait_phone)
    async def phone_received(m:Message,state:FSMContext):
        phone='+'+''.join(ch for ch in (m.text or '') if ch.isdigit())
        if len(phone)<8: return await m.answer('Номер выглядит некорректно. Повторите ввод.')
        api_id=int(os.getenv('API_ID','0')); api_hash=os.getenv('API_HASH','')
        if not api_id or not api_hash: await state.clear(); return await m.answer('Не настроены API_ID/API_HASH в .env.')
        client=TelegramClient(StringSession(),api_id,api_hash)
        try:
            await asyncio.wait_for(client.connect(),20)
            sent=await asyncio.wait_for(client.send_code_request(phone),30)
            _phone_clients[m.from_user.id]={'client':client,'phone':phone,'hash':sent.phone_code_hash,'tries':0,'twofa_tries':0,'started':time.time()}
            await state.update_data(phone=phone,phone_code_hash=sent.phone_code_hash,code='')
            await state.set_state(AccountFSM.wait_code); arm_timeout(m.from_user.id,state,client)
            await m.answer('Введите код из Telegram кнопками ниже.\nВведено: —',reply_markup=code_keyboard())
        except Exception as exc:
            await client.disconnect(); await state.clear(); await m.answer(f'Не удалось отправить код: {exc}')

    @router.callback_query(AccountFSM.wait_code,F.data.startswith('acc:code:'))
    async def code_input(c:CallbackQuery,state:FSMContext):
        data=await state.get_data(); code=data.get('code',''); op=c.data.split(':',2)[2]
        if op=='back': code=code[:-1]
        elif op=='submit':
            info=_phone_clients.get(c.from_user.id)
            if not info: await c.answer('Сессия авторизации истекла.',show_alert=True); await state.clear(); return
            try:
                await info['client'].sign_in(phone=info['phone'],code=code,phone_code_hash=info['hash'])
                me=await info['client'].get_me(); session=StringSession.save(info['client'].session)
                acc_id=await SessionImporter(db,pool,c.from_user.id)._save(session,int(os.getenv('API_ID')),os.getenv('API_HASH'),me,info['phone'],'telethon',action='import_phone')
                await info['client'].disconnect(); _phone_clients.pop(c.from_user.id,None); await state.clear()
                task=_timeout_tasks.pop(c.from_user.id,None)
                if task: task.cancel()
                await c.answer('Аккаунт добавлен.')
                await c.message.edit_text(f'Аккаунт #{acc_id} добавлен.')
            except SessionPasswordNeededError:
                await state.set_state(AccountFSM.wait_2fa)
                await c.answer('Нужен пароль 2FA.')
                await c.message.edit_text('Включена двухфакторная защита. Отправьте пароль 2FA обычным сообщением.')
            except (PhoneCodeInvalidError,PhoneCodeExpiredError) as exc:
                info['tries']+=1
                if info['tries']>=3:
                    await info['client'].disconnect(); _phone_clients.pop(c.from_user.id,None); await state.clear()
                    await c.message.edit_text('Три неверные попытки. Авторизация отменена.')
                else: await c.answer('Код неверный или истёк. Попробуйте ещё раз.',show_alert=True)
            except FloodWaitError as exc:
                await c.answer(f'Слишком много запросов. Подождите {exc.seconds} сек.',show_alert=True)
            except Exception as exc:
                await c.answer(f'Ошибка авторизации: {exc}',show_alert=True)
            return
        elif op.isdigit() and len(code)<6: code+=op
        await state.update_data(code=code)
        await c.message.edit_text('Введите код из Telegram кнопками ниже.\nВведено: '+'•'*len(code) if code else 'Введено: —',reply_markup=code_keyboard())
        await c.answer()

    @router.message(AccountFSM.wait_2fa)
    async def twofa(m:Message,state:FSMContext):
        info=_phone_clients.get(m.from_user.id)
        completed=False
        abort=False
        try:
            if not info: raise RuntimeError('Сессия авторизации истекла.')
            await info['client'].sign_in(password=m.text or '')
            me=await info['client'].get_me(); session=StringSession.save(info['client'].session)
            acc_id=await SessionImporter(db,pool,m.from_user.id)._save(session,int(os.getenv('API_ID')),os.getenv('API_HASH'),me,info['phone'],'telethon',action='import_phone')
            await m.answer(f'Аккаунт #{acc_id} успешно добавлен.')
            completed=True
        except PasswordHashInvalidError:
            info['twofa_tries'] += 1
            if info['twofa_tries'] >= 3:
                await m.answer('Три неверные попытки пароля 2FA. Авторизация отменена.')
                abort=True
            else:
                await m.answer('Пароль 2FA неверный. Попробуйте ещё раз.')
        except Exception as exc:
            await m.answer(f'Не удалось проверить пароль 2FA: {exc}')
            abort=True
        finally:
            try: await m.delete()
            except Exception: log.exception('Не удалось удалить сообщение с паролем 2FA')
            if completed or abort:
                if info:
                    await info['client'].disconnect(); _phone_clients.pop(m.from_user.id,None)
                await state.clear()
                task=_timeout_tasks.pop(m.from_user.id,None)
                if task: task.cancel()

    @router.callback_query(F.data.startswith('accadm:list:'))
    async def list_cb(c:CallbackQuery):
        await c.answer(); await show_list(c,int(c.data.rsplit(':',1)[1]))
    @router.callback_query(F.data.startswith('accadm:card:'))
    async def card(c:CallbackQuery):
        acc_id=int(c.data.rsplit(':',1)[1]); a=await db.one('SELECT a.*,u.username renter FROM accounts a LEFT JOIN users u ON u.id=a.leased_by WHERE a.id=?',(acc_id,))
        await c.answer()
        if not a: return await c.message.edit_text('Аккаунт не найден.')
        text=f"#{a['id']} | {a['label'] or '—'} | @{a['tg_username'] or '—'}\nСтатус: {a['status']}\nАрендатор: {a['leased_by'] or '—'} (@{a['renter'] or '—'})\nДо: {a['lease_until'] or '—'}"
        await c.message.edit_text(text,reply_markup=admin_card(a))
    @router.callback_query(F.data.startswith('accadm:force:'))
    async def force(c:CallbackQuery):
        acc_id=int(c.data.rsplit(':',1)[1]);
        try: await lease_service.force_release(acc_id,c.from_user.id); await c.answer('Аккаунт освобождён.')
        except Exception as exc: await c.answer(str(exc),show_alert=True)
        await show_list(c)
    @router.callback_query(F.data.startswith('accadm:toggle:'))
    async def toggle(c:CallbackQuery):
        acc_id=int(c.data.rsplit(':',1)[1]); a=await db.one('SELECT status FROM accounts WHERE id=?',(acc_id,))
        if not a: return await c.answer('Аккаунт не найден.',show_alert=True)
        if a['status']=='disabled': await db.run("UPDATE accounts SET status='free' WHERE id=? AND status='disabled'",(acc_id,)); msg='Включён'
        else:
            await db.run("UPDATE accounts SET status='disabled',leased_by=NULL,lease_token=NULL,leased_at=NULL,lease_until=NULL WHERE id=?",(acc_id,)); await pool.disconnect(acc_id); msg='Отключён'
        await db.run("INSERT INTO account_events(account_id,user_id,action,detail) VALUES(?,?,?,?)",(acc_id,c.from_user.id,'disable',msg))
        await c.answer(msg); await show_list(c)
    @router.callback_query(F.data.startswith('accadm:session:'))
    async def show_session(c:CallbackQuery):
        acc_id=int(c.data.rsplit(':',1)[1]); a=await db.one('SELECT session_enc FROM accounts WHERE id=?',(acc_id,))
        await c.answer()
        if a: await c.message.answer('<code>'+decrypt(a['session_enc']).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')+'</code>',parse_mode='HTML')
    @router.callback_query(F.data=='accadm:noop')
    async def noop(c:CallbackQuery): await c.answer()
    return router

async def shutdown_phone_imports():
    """Отключить незавершённые клиенты авторизации при остановке бота."""
    for uid, info in list(_phone_clients.items()):
        try:
            await info['client'].disconnect()
        except Exception:
            log.exception("Ошибка отключения незавершённой авторизации администратора %s", uid)
        _phone_clients.pop(uid, None)
    for task in list(_timeout_tasks.values()):
        task.cancel()
    _timeout_tasks.clear()

async def cancel_phone_import(uid):
    """Отменить FSM-авторизацию и закрыть её временный Telegram-клиент."""
    info = _phone_clients.pop(uid, None)
    if info:
        try:
            await info['client'].disconnect()
        except Exception:
            log.exception("Ошибка отключения отменённой авторизации пользователя %s", uid)
    task = _timeout_tasks.pop(uid, None)
    if task:
        task.cancel()
