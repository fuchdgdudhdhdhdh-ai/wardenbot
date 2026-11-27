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

Не добавляй Redis, API_ID, API_HASH или SESSION_ENC_KEY: демонстрационный каталог не использует реальные Telegram-сессии.

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

## Бесплатный аккаунт ☎ — только демо-механика

Раздел не импортирует, не хранит и не выдаёт реальные Telegram-аккаунты, номера, коды, пароли, JSON-сессии или файлы `.session`.

- Доступ к обычным тестовым слотам — от роли «Трафер».
- Доступ к премиум тестовым слотам — от роли «Опытный».
- Администратор добавляет слот только текстовой меткой, например `DEMO-001`.
- Участник получает демонстрационный слот, открывает демо-панель и завершает учебный сценарий.
- Назначение и завершение записываются в SQLite и журнал аудита.

В проекте нет активных обработчиков импорта реальных сессий и аренды реальных аккаунтов. Старые таблицы, если они уже существовали в базе, не удаляются автоматически.

## Redis и FSM

Redis не нужен. Состояния FSM хранятся в памяти процесса и сбрасываются после перезапуска сервиса. Данные пользователей, оферов, выплат и демо-слотов хранятся в SQLite на Persistent Disk.

## Локальный запуск

```bash
python -m venv .venv
pip install -r requirements.txt
cp .env.example .env
```

Для локального polling укажи `BOT_MODE=polling`. Для Render Blueprint используется `BOT_MODE=webhook`.

## Важно

- Оставь один экземпляр сервиса: SQLite рассчитана на одну реплику.
- Не публикуй токен бота и секрет webhook.
- Делай резервные копии SQLite с Persistent Disk.
- Не удаляй и не пересоздавай Persistent Disk без резервной копии.
