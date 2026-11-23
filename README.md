# Warden Team Bot — Render Web Service

Проект запускается на Render как **Web Service с Telegram webhook**, а не как Background Worker и не через long polling. HTTP-сервер слушает `0.0.0.0:$PORT`, маршрут `/health` используется Render для health check, маршрут `/telegram/webhook` принимает обновления Telegram.

## 1. Подготовка GitHub

1. Распакуйте архив проекта.
2. Загрузите в отдельный GitHub-репозиторий **содержимое папки `warden_team_bot`**, а не сам ZIP.
3. Убедитесь, что в корне репозитория лежат `bot.py`, `db.py`, `requirements.txt` и `render.yaml`.
4. Не добавляйте `.env`, `warden.db`, файлы `.session`, JSON сессии, резервные копии и каталоги `tmp/` в Git.
5. Проверьте, что `.gitignore` исключает секреты и локальную БД.

## 2. Создание сервиса Render

Рекомендуемый вариант — Blueprint:

1. Откройте Render Dashboard → **New** → **Blueprint**.
2. Подключите GitHub и выберите репозиторий.
3. Render обнаружит `render.yaml`.
4. Подтвердите создание Web Service.
5. При первом создании заполните переменные окружения, помеченные как секретные/не заданные.

В Blueprint уже указаны:

- Runtime: Python 3.12;
- Build: `pip install --upgrade pip && pip install -r requirements.txt`;
- Start: `python bot.py`;
- Health check: `/health`;
- SQLite: `/var/data/warden.db`;
- Persistent Disk: `/var/data`;
- режим Telegram webhook.

**Важно про тариф Render:** для сохранения SQLite-файла между деплоями и перезапусками нужен Persistent Disk. В `render.yaml` выбран платный `starter` Web Service и диск 1 GB. Бесплатный Web Service не предоставляет постоянный диск: SQLite на его файловой системе может исчезнуть при redeploy/restart. Не переключайте сервис на Free, если хотите сохранить пользователей, заявки и зашифрованные аккаунты.

Запускайте только **один экземпляр** этого сервиса: SQLite и локальный пул клиентов рассчитаны на один процесс/одну реплику. Не создавайте второй Web Service из того же репозитория с тем же Telegram bot token.

## 3. Environment Variables

В Render → ваш сервис → **Environment** заполните:

| Переменная | Что указать |
|---|---|
| `BOT_TOKEN` | Токен бота от @BotFather |
| `WEBHOOK_SECRET` | Случайная строка 32–64 символа из `A-Z a-z 0-9 _ -` |
| `SESSION_ENC_KEY` | Ключ Fernet, см. генерацию ниже |
| `API_ID` | Telegram API ID с my.telegram.org |
| `API_HASH` | Telegram API Hash с my.telegram.org |
| `TEAM_CHAT_ID` | ID вашей группы, например `-100...`; если проверка членства не нужна — `0` |
| `OWNER_ID` | Telegram ID владельца бота, только цифры |
| `SUPERADMIN_ID` | Telegram ID суперадминистратора, только цифры |
| `TEAM_INVITE` | Пригласительная ссылка в команду |
| `REDIS_URL` | Необязательно: URL Redis/Render Key Value для сохранения FSM между перезапусками |

Не меняйте заданные в Blueprint `DATABASE_PATH=/var/data/warden.db`, `BOT_MODE=webhook`, `WEBHOOK_PATH=/telegram/webhook` и `ACCOUNT_LEASE_TTL_MIN=30` без необходимости.

Генерация ключей локально:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Первую команду используйте для `SESSION_ENC_KEY`, вторую — для `WEBHOOK_SECRET`. Сохраните `SESSION_ENC_KEY` отдельно в надёжном менеджере секретов и резервной копии. Потеря ключа означает потерю доступа ко всем зашифрованным строкам сессий. Не генерируйте новый ключ при каждом деплое.

## 4. Webhook и health check

Приложение само устанавливает webhook при запуске, используя `RENDER_EXTERNAL_URL`, и проверяет входящий секрет Telegram через `WEBHOOK_SECRET`. Вручную указывать домен Render не требуется. Render должен видеть успешный ответ `GET /health`.

Если меняете `WEBHOOK_PATH`, поменяйте его только в Environment; приложение использует одно и то же значение при регистрации webhook и при создании HTTP-маршрута.

Не запускайте параллельно локальный polling с тем же токеном, пока Render webhook активен. Telegram не поддерживает одновременный webhook и `getUpdates` для одного бота.

## 5. Данные и файловая система

- Основная SQLite БД и все бизнес-данные: `/var/data/warden.db` на Persistent Disk.
- Временные загруженные `.session`/`.json`: временная папка приложения; файлы удаляются после обработки и не должны коммититься.
- FSM: Redis, если задан `REDIS_URL`; без Redis используется память процесса, незавершённые шаги FSM теряются при перезапуске сервиса.
- Живые Telethon/Pyrogram клиенты существуют только в памяти процесса и корректно отключаются при штатном shutdown. После перезапуска клиент подключается лениво при следующем действии пользователя.

Настройте регулярные резервные копии SQLite с Persistent Disk во внешнее хранилище. Сам Persistent Disk не заменяет backup. Не включайте в Git `warden.db`, резервные копии или сессионные файлы.

## 6. Локальная разработка

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

Локально `BOT_MODE=polling` (по умолчанию). Для Render Blueprint автоматически задан `BOT_MODE=webhook`.

## 7. Деплой и обновление

При каждом push в подключённую ветку Render выполняет build и deploy. После деплоя проверьте:

1. В Render Events статус Deploy live.
2. `https://<имя-сервиса>.onrender.com/health` возвращает `{"status":"ok","service":"warden-team-bot"}`.
3. В логах есть сообщение `Webhook запущен для @...`.
4. В Telegram `/start` отвечает.
5. `/admin` открывается у владельца.
6. После тестового импорта и перезапуска сервиса аккаунт остаётся в списке.

## 8. Что важно не менять в production

- Не удаляйте и не пересоздавайте Persistent Disk без резервной копии.
- Не меняйте `SESSION_ENC_KEY` после импорта аккаунтов без процедуры перешифровки.
- Не запускайте две реплики приложения с одной SQLite БД и одним Telegram bot token.
- Не публикуйте `.env`, токен бота, API_HASH, WEBHOOK_SECRET, SESSION_ENC_KEY, файлы `.session` и JSON авторизации.
- Для нескольких реплик, высокой нагрузки и горизонтального масштабирования потребуется отдельная миграция SQLite на PostgreSQL и согласованный распределённый пул клиентов; текущий пул рассчитан на один процесс.

## 9. Тесты

```bash
python -m unittest discover -s tests/ -p 'test_*.py'
```

После публикации на Render дополнительно проверьте webhook, загрузку файлов, импорт сессий, аренду двумя пользователями, TTL, повторный импорт и восстановление данных после redeploy.
