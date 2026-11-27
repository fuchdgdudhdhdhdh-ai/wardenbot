"""Вход по номеру с вводом кода из админки и сохранением сессии."""
import asyncio, logging, os
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    FloodWaitError, AuthError, ApiIdInvalidError, PhoneNumberInvalidError,
)
from telethon.tl.types import SentCodeTypeApp, SentCodeTypeSms, SentCodeTypeCall, SentCodeTypeFlashCall
from services.crypto import encrypt, decrypt
from services.session_import import SessionImporter, SessionImportError

log = logging.getLogger(__name__)
LOGIN_TTL_MIN = 10

class CodeLoginError(RuntimeError): pass
class LoginNotFound(CodeLoginError): pass
class LoginExpired(CodeLoginError): pass
class NotYourLogin(CodeLoginError): pass
class NeedPassword(CodeLoginError): pass

def _now():
    return datetime.now(timezone.utc)

class CodeLoginManager:
    def __init__(self, db, pool):
        self.db, self.pool = db, pool
        self._clients = {}
        self._locks = {}

    def _lock(self, login_id):
        return self._locks.setdefault(int(login_id), asyncio.Lock())

    def _api(self):
        api_id = int(os.getenv("API_ID", "0") or 0)
        api_hash = os.getenv("API_HASH", "").strip()
        if not api_id or not api_hash:
            raise CodeLoginError("Задайте API_ID и API_HASH в переменных окружения.")
        return api_id, api_hash

    @staticmethod
    def describe(sent_type):
        if isinstance(sent_type, SentCodeTypeApp):
            return "код приходит в приложение Telegram на самом аккаунте"
        if isinstance(sent_type, SentCodeTypeSms):
            return "код приходит по SMS"
        if isinstance(sent_type, (SentCodeTypeCall, SentCodeTypeFlashCall)):
            return "код придёт звонком"
        return "код отправлен"

    async def _connect(self, api_id, api_hash, session=None):
        client = TelegramClient(StringSession(session or ""), api_id, api_hash,
                                timeout=25, connection_retries=2, request_retries=2)
        await asyncio.wait_for(client.connect(), 30)
        return client

    async def _row(self, login_id, admin_id=None):
        row = await self.db.one("SELECT * FROM account_logins WHERE id=?", (login_id,))
        if not row:
            raise LoginNotFound("Вход не найден. Начните заново.")
        if admin_id is not None and row["admin_id"] != admin_id:
            raise NotYourLogin("Этот вход начал другой администратор.")
        try:
            if datetime.fromisoformat(row["expires_at"]) <= _now():
                await self.cancel(login_id)
                raise LoginExpired("Время ввода кода истекло. Начните заново.")
        except ValueError:
            raise LoginExpired("Время ввода кода некорректно. Начните заново.")
        return row

    async def _client(self, row):
        client = self._clients.get(row["id"])
        if client is not None and client.is_connected():
            return client
        session = decrypt(row["temp_session_enc"]) if row["temp_session_enc"] else ""
        client = await self._connect(row["api_id"], row["api_hash"], session)
        self._clients[row["id"]] = client
        return client

    async def start(self, admin_id: int, phone: str):
        api_id, api_hash = self._api()
        client = await self._connect(api_id, api_hash)
        try:
            sent = await asyncio.wait_for(client.send_code_request(phone), 40)
        except FloodWaitError:
            await client.disconnect()
            raise
        except PhoneNumberInvalidError as exc:
            await client.disconnect()
            raise CodeLoginError("Telegram не принял номер. Проверьте формат.") from exc
        except Exception:
            await client.disconnect()
            raise
        temp_session = encrypt(StringSession.save(client.session))
        login_id = await self.db.run(
            """INSERT INTO account_logins(admin_id,phone,api_id,api_hash,phone_code_hash,temp_session_enc,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (admin_id, phone, api_id, api_hash, sent.phone_code_hash, temp_session,
             _now().isoformat(), (_now() + timedelta(minutes=LOGIN_TTL_MIN)).isoformat()))
        self._clients[login_id] = client
        return login_id, self.describe(sent.type), sent.timeout or 60

    async def _persist_temp(self, login_id, client):
        await self.db.run("UPDATE account_logins SET temp_session_enc=? WHERE id=?",
                          (encrypt(StringSession.save(client.session)), login_id))

    async def resend(self, login_id, admin_id):
        async with self._lock(login_id):
            row = await self._row(login_id, admin_id)
            client = await self._client(row)
            sent = await asyncio.wait_for(client.send_code_request(row["phone"]), 40)
            await self.db.run("""UPDATE account_logins SET phone_code_hash=?,expires_at=?,state='code',attempts=0
                                 WHERE id=?""",
                              (sent.phone_code_hash, (_now() + timedelta(minutes=LOGIN_TTL_MIN)).isoformat(), login_id))
            await self._persist_temp(login_id, client)
            return self.describe(sent.type)

    async def submit_code(self, login_id, admin_id, code: str):
        async with self._lock(login_id):
            row = await self._row(login_id, admin_id)
            client = await self._client(row)
            try:
                await client.sign_in(phone=row["phone"], code=code, phone_code_hash=row["phone_code_hash"])
            except SessionPasswordNeededError:
                await self.db.run("UPDATE account_logins SET state='2fa' WHERE id=?", (login_id,))
                raise NeedPassword("На аккаунте включена двухфакторная защита.")
            except PhoneCodeInvalidError:
                await self.db.run("UPDATE account_logins SET attempts=attempts+1 WHERE id=?", (login_id,))
                raise CodeLoginError("Неверный код. Попробуйте снова.")
            except PhoneCodeExpiredError:
                raise CodeLoginError("Код истёк. Запросите новый.")
            return await self._finish(row, client)

    async def submit_password(self, login_id, admin_id, password: str):
        async with self._lock(login_id):
            row = await self._row(login_id, admin_id)
            if row["state"] != "2fa":
                raise CodeLoginError("Пароль сейчас не требуется.")
            client = await self._client(row)
            try:
                await client.sign_in(password=password)
            except AuthError as exc:
                await self.db.run("UPDATE account_logins SET attempts=attempts+1 WHERE id=?", (login_id,))
                raise CodeLoginError("Пароль не принят.") from exc
            return await self._finish(row, client)

    async def _finish(self, row, client):
        me = await client.get_me()
        if not me:
            raise CodeLoginError("Telegram не вернул профиль аккаунта.")
        importer = SessionImporter(self.db, self.pool, row["admin_id"])
        acc_id = await importer._save(StringSession.save(client.session), row["api_id"], row["api_hash"],
                                      me, row["phone"], "telethon", None, "added_phone")
        await self._drop(row["id"])
        await self.db.run("DELETE FROM account_logins WHERE id=?", (row["id"],))
        return acc_id

    async def cancel(self, login_id):
        await self._drop(login_id)
        await self.db.run("DELETE FROM account_logins WHERE id=?", (login_id,))

    async def _drop(self, login_id):
        client = self._clients.pop(int(login_id), None)
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 10)
            except Exception:
                log.exception("Не удалось закрыть временный клиент входа %s", login_id)
        self._locks.pop(int(login_id), None)

    async def cleanup_expired(self):
        rows = await self.db.all("SELECT id,expires_at FROM account_logins")
        for row in rows:
            try:
                if datetime.fromisoformat(row["expires_at"]) <= _now():
                    await self.cancel(row["id"])
            except ValueError:
                await self.cancel(row["id"])
