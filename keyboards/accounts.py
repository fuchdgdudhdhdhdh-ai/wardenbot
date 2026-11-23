from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows])

def admin_accounts_menu():
    return _kb([[('📄 .session','accadm:import:session'),('🧾 .json','accadm:import:json')],
                [('☎️ По номеру','accadm:phone'),('📋 Аккаунты','accadm:list:0')]])

def admin_card(acc):
    rows = [[('Освободить','accadm:force:'+str(acc['id']))],
            [('Отключить' if acc['status']!='disabled' else 'Включить','accadm:toggle:'+str(acc['id']))],
            [('Показать строку сессии','accadm:session:'+str(acc['id']))],
            [('⬅️ К списку','accadm:list:0')]]
    return _kb(rows)

def user_accounts(rows, page, pages):
    buttons=[]
    for a in rows:
        if a['status']=='free':
            buttons.append([(f"🔑 Взять #{a['id']} · {a.get('label') or 'Аккаунт'}",f"accusr:take:{a['id']}:")])
        elif a['status']=='leased' and a.get('leased_by') is not None:
            buttons.append([(f"🟢 Мой аккаунт #{a['id']} · открыть",f"accusr:open:{a['id']}")])
    nav=[]
    if page>0: nav.append(('◀️',f'accusr:list:{page-1}'))
    nav.append((f'{page+1}/{max(1,pages)}','accusr:noop'))
    if page+1<pages: nav.append(('▶️',f'accusr:list:{page+1}'))
    buttons.append(nav)
    return _kb(buttons)

def user_account_card(acc, owner=True):
    rows=[[('📨 Диалоги','accusr:dialogs:'+str(acc['id']))], [('↩️ Вернуть', 'accusr:return:'+str(acc['id']))]]
    return _kb(rows)

def code_keyboard():
    return _kb([[('1','acc:code:1'),('2','acc:code:2'),('3','acc:code:3')],
                [('4','acc:code:4'),('5','acc:code:5'),('6','acc:code:6')],
                [('7','acc:code:7'),('8','acc:code:8'),('9','acc:code:9')],
                [('⌫','acc:code:back'),('0','acc:code:0'),('✅','acc:code:submit')]])
