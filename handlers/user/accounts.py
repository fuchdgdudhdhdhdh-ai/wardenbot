from aiogram import Router,F
from aiogram.filters import BaseFilter
from aiogram.types import Message,CallbackQuery
from aiogram.fsm.state import State,StatesGroup
from aiogram.fsm.context import FSMContext
from services.lease import AccountBusy,NotYourAccount,LeaseExpired,lease_service
from keyboards.accounts import user_accounts,user_account_card

class IsNotAdmin(BaseFilter):
    def __init__(self,db,owner_id): self.db,self.owner_id=db,owner_id
    async def __call__(self,event): return not await self.db.is_admin(event.from_user.id,self.owner_id)
class UserAccountFSM(StatesGroup): send_text=State()

def setup_user_accounts(db,pool,owner_id):
    router=Router(name='user_accounts')
    router.message.filter(IsNotAdmin(db,owner_id)); router.callback_query.filter(IsNotAdmin(db,owner_id))

    async def render_list(target,uid,page=0):
        rows=await db.all("""SELECT * FROM accounts WHERE status='free' OR (status='leased' AND leased_by IS NOT NULL)
            ORDER BY CASE WHEN status='free' THEN 0 ELSE 1 END,id DESC LIMIT 10 OFFSET ?""",(page*10,))
        # В интерфейсе чужая аренда отображается без действия.
        for row in rows:
            if row['status']=='leased' and row['leased_by']!=uid: row['leased_by']=None
        total=await db.one("SELECT COUNT(*) n FROM accounts WHERE status='free' OR status='leased'")
        pages=max(1,(total['n']+9)//10)
        text='Доступные аккаунты:\n'+'\n'.join(
            f"#{a['id']} · {a['label'] or 'Аккаунт'} · {'Свободен' if a['status']=='free' else ('Ваш' if a.get('leased_by')==uid else 'Занят')}"
            for a in rows) if rows else 'Свободных аккаунтов пока нет.'
        markup=user_accounts(rows,page,pages)
        if isinstance(target,CallbackQuery): await target.message.edit_text(text,reply_markup=markup)
        else: await target.answer(text,reply_markup=markup)

    @router.callback_query(F.data=='user_accounts')
    async def entry(c:CallbackQuery): await c.answer(); await render_list(c,c.from_user.id)
    @router.callback_query(F.data.startswith('accusr:list:'))
    async def listing(c:CallbackQuery): await c.answer(); await render_list(c,c.from_user.id,int(c.data.rsplit(':',1)[1]))
    @router.callback_query(F.data.startswith('accusr:take:'))
    async def take(c:CallbackQuery):
        acc_id=int(c.data.split(':')[2])
        try:
            await lease_service.take(acc_id,c.from_user.id,ttl_min=lease_service.ttl_min)
            await c.answer('Аккаунт закреплён за вами на 30 минут.')
            a=await db.one('SELECT * FROM accounts WHERE id=?',(acc_id,))
            await c.message.edit_text(f"Аккаунт #{acc_id} арендован до {a['lease_until']}. Строка сессии недоступна.",reply_markup=user_account_card(a))
        except AccountBusy as exc: await c.answer(str(exc),show_alert=True)
        except Exception as exc: await c.answer(f'Не удалось взять аккаунт: {exc}',show_alert=True)
    @router.callback_query(F.data.startswith('accusr:open:'))
    async def open_account(c:CallbackQuery):
        acc_id=int(c.data.rsplit(':',1)[1])
        try:
            await lease_service.guard_lease(acc_id,c.from_user.id)
            a=await db.one('SELECT * FROM accounts WHERE id=?',(acc_id,))
            await c.answer(); await c.message.edit_text(f"Аккаунт #{acc_id}\nАренда активна до {a['lease_until']}.",reply_markup=user_account_card(a))
        except (NotYourAccount,LeaseExpired) as exc: await c.answer(str(exc),show_alert=True)
    @router.callback_query(F.data.startswith('accusr:return:'))
    async def return_account(c:CallbackQuery):
        acc_id=int(c.data.rsplit(':',1)[1])
        try: await lease_service.release(acc_id,c.from_user.id); await c.answer('Аккаунт возвращён.'); await render_list(c,c.from_user.id)
        except Exception as exc: await c.answer(str(exc),show_alert=True)

    @router.callback_query(F.data.startswith('accusr:dialogs:'))
    async def dialogs(c:CallbackQuery):
        acc_id=int(c.data.rsplit(':',1)[1])
        try:
            await lease_service.guard_lease(acc_id,c.from_user.id)
            client=await pool.get(acc_id)
            from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
            buttons=[]
            if hasattr(client,'iter_dialogs'):
                async for d in client.iter_dialogs(limit=20):
                    title=(d.name or str(d.id))[:45]
                    buttons.append([InlineKeyboardButton(text=title,callback_data=f'accusr:chat:{acc_id}:{d.id}')])
            else:
                async for d in client.get_dialogs(limit=20):
                    chat=d.chat; title=(getattr(chat,'title',None) or getattr(chat,'first_name',None) or str(chat.id))[:45]
                    buttons.append([InlineKeyboardButton(text=title,callback_data=f'accusr:chat:{acc_id}:{chat.id}')])
            buttons.append([InlineKeyboardButton(text='⬅️ Назад',callback_data=f'accusr:open:{acc_id}')])
            await c.answer(); await c.message.edit_text('Диалоги аккаунта:',reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        except Exception as exc: await c.answer(str(exc),show_alert=True)

    @router.callback_query(F.data.startswith('accusr:chat:'))
    async def chat_history(c:CallbackQuery,state:FSMContext):
        parts=c.data.split(':'); acc_id=int(parts[2]); chat_id=int(parts[3])
        try:
            await lease_service.guard_lease(acc_id,c.from_user.id); client=await pool.get(acc_id)
            if hasattr(client,'iter_messages'):
                msgs=[]
                async for m in client.iter_messages(chat_id,limit=10):
                    if m.message: msgs.append(m.message[:500])
            else:
                msgs=[]
                async for m in client.get_chat_history(chat_id,limit=10):
                    if m.text: msgs.append(m.text[:500])
            text='\n\n'.join(msgs) if msgs else 'Текстовых сообщений нет.'
            await state.update_data(account_id=acc_id,chat_id=chat_id); await state.set_state(UserAccountFSM.send_text)
            await c.answer(); await c.message.edit_text('Последние сообщения (новые сверху):\n\n'+text+'\n\nОтправьте текст следующим сообщением, чтобы написать в этот диалог.')
        except Exception as exc: await c.answer(str(exc),show_alert=True)

    @router.message(UserAccountFSM.send_text)
    async def send_message(m:Message,state:FSMContext):
        data=await state.get_data(); acc_id=data.get('account_id'); chat_id=data.get('chat_id')
        try:
            await lease_service.guard_lease(acc_id,m.from_user.id); client=await pool.get(acc_id)
            if hasattr(client,'send_message'): await client.send_message(chat_id,m.text or '')
            else: await client.send_message(chat_id,m.text or '')
            await m.answer('Сообщение отправлено через серверный клиент.')
        except Exception as exc: await m.answer(f'Не удалось отправить сообщение: {exc}')
        await state.clear()

    @router.callback_query(F.data=='accusr:noop')
    async def noop(c:CallbackQuery): await c.answer()
    return router
