# Warden Team Bot — Render Web Service

Warden Team работает на Render как **один Web Service** с Telegram webhook. Redis не требуется. Для SQLite используется Persistent Disk.

## Render Blueprint

1. Открой Render Dashboard → **New → Blueprint**.
2. Выбери GitHub-репозиторий `fuchdgdudhdhdhdh-ai/wardenbot`.
3. Render прочитает `render.yaml` и создаст Web Service.
4. Заполни секретные Environment Variables.

Настройки из `render.yaml`:

- Runtime: Python 3.12
- Build: `pip install --upgrade pip && pip install -r requirements.txt`
- Start: `python bot.py`
- Health check: `/health`
- Persistent Disk: `/var/data`, 1 GB
- SQLite: `/var/data/warden.db`
- Telegram mode: webhook
- Один экземпляр сервиса

Для сохранения SQLite между деплоями нужен тариф Render с Persistent Disk. Не удаляй диск при обновлении сервиса.

## Environment Variables

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | Токен от @BotFather |
| `WEBHOOK_SECRET` | Случайная строка из букв A-Z, a-z, цифр, `_` и `-` |
| `TEAM_CHAT_ID` | ID командной группы, например `-100...`; `0` отключает проверку членства |
| `OWNER_ID` | Telegram ID владельца |
| `SUPERADMIN_ID` | Telegram ID суперадминистратора |
| `TEAM_INVITE` | Пригласительная ссылка команды |
| `DATABASE_PATH` | Уже задано Blueprint: `/var/data/warden.db` |
| `API_ID`, `API_HASH` | Данные приложения Telegram с my.telegram.org; нужны для входа по номеру и импорта Telethon-сессий |
| `SESSION_ENC_KEY` | Ключ Fernet для шифрования сессий и паролей 2FA в SQLite |
| `ACCOUNT_LOGIN_MODE` | `bot_only` по умолчанию; `code_login` включает запрос кода в пользовательской панели |
| `ACCOUNT_LEASE_MIN` | Длительность аренды аккаунта в минутах, по умолчанию и максимум `900` (15 часов) |

Redis не требуется. Для работы склада реальных аккаунтов обязательно задай `API_ID`, `API_HASH` и `SESSION_ENC_KEY`. Без корректного `SESSION_ENC_KEY` раздел аккаунтов безопасно отключается, остальные разделы бота продолжают работать. Не публикуй эти значения и не меняй ключ шифрования после сохранения сессий: без прежнего ключа их нельзя расшифровать.

Сгенерировать секрет webhook можно локально:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Webhook и проверка

При запуске приложение берёт внешний URL из `RENDER_EXTERNAL_URL`, регистрирует webhook и проверяет секретный заголовок Telegram.

После деплоя:

1. В Render Events должен появиться статус **Live**.
2. Открой `https://<имя-сервиса>.onrender.com/health`.
3. Ожидаемый ответ:

```json
{"status":"ok","service":"warden-team-bot"}
```

4. В логах должно появиться `Webhook запущен для @...`.
5. Отправь боту `/start` и проверь меню.

Не запускай polling с тем же токеном одновременно с Render webhook.

## Склад аккаунтов

Администратор может добавить аккаунт по номеру с вводом кода и 2FA либо импортировать файл Telethon/Pyrogram `.session` или поддерживаемый `.json`. Сессии и пароли 2FA шифруются ключом `SESSION_ENC_KEY`; сами строки сессий пользователям не показываются.

- Обычные аккаунты доступны с роли «Трафер», премиум — с роли «Опытный».
- Участник может иметь не более одной активной аренды каждого класса.
- Аренда длится не более 15 часов и автоматически освобождается по истечении срока.
- Действия с аккаунтами записываются в журнал событий и аудит.
- `ACCOUNT_LOGIN_MODE=code_login` дополнительно включает запрос кода в пользовательской панели; по умолчанию эта возможность выключена.

Таблица демонстрационных слотов `demo_account_slots` сохранена отдельно для учебных сценариев. Старые таблицы и пользовательские данные автоматически не удаляются.

## Redis и FSM

Redis не нужен. Состояния FSM хранятся в памяти процесса и сбрасываются после перезапуска сервиса. Данные пользователей, оферов, выплат и демо-слотов хранятся в SQLite на Persistent Disk.

## Локальный запуск

```bash
python -m venv .venv
pip install -r requirements.txt
cp env.example .env
```

Для локального polling укажи `BOT_MODE=polling`. Для Render Blueprint используется `BOT_MODE=webhook`.

## Важно

- Оставь один экземпляр сервиса: SQLite рассчитана на одну реплику.
- Не публикуй токен бота и секрет webhook.
- Делай резервные копии SQLite с Persistent Disk.
- Не удаляй и не пересоздавай Persistent Disk без резервной копии.


## Render PostgreSQL

Для постоянного хранения данных можно подключить PostgreSQL Render. В настройках Web Service добавьте переменную окружения **DATABASE_URL** и вставьте **Internal Database URL** из созданной базы Render (если база в другом регионе/вне Render — используйте External Database URL).

При наличии `DATABASE_URL` бот автоматически создаёт таблицы в PostgreSQL. Если на подключённом Persistent Disk осталась старая база `/var/data/warden.db`, при первом запуске она автоматически импортируется в пустую PostgreSQL-базу. Поэтому не удаляйте старую базу/диск до успешного запуска и проверки данных. Если `DATABASE_URL` не задана, бот продолжает использовать SQLite по `DATABASE_PATH`.

После добавления переменной Render выполнит redeploy. Не публикуйте значение `DATABASE_URL` и не отправляйте его в чат.
